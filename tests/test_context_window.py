"""Unit tests for the per-run context profile — offline, hand-built message histories.

Covers the per-request curve (first / peak / final), the composition estimate (measured growth
split by character share, the first request's remainder as tool schemas), the window share from
``MODEL_CONTEXT_WINDOWS``, the near-limit WARNING, and that merged records keep the higher peak.
"""
from __future__ import annotations

import json
import logging

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage, RunUsage

from ai_test_gen.core.context_window import (
    SCHEMAS,
    ContextProfile,
    context_windows,
    describe_context,
    format_peak,
    log_context,
    profile_context,
)
from ai_test_gen.core.usage import UsageLog, format_context


def _response(input_tokens: int, *parts) -> ModelResponse:
    return ModelResponse(parts=list(parts), usage=RequestUsage(input_tokens=input_tokens))


def _profile(history, model: str = "m") -> ContextProfile:
    profile = profile_context(history, model)
    assert profile is not None
    return profile


def _history():
    """System 400 chars + task 400 chars; two snapshot steps, then a small final turn.

    Requests: 1000 → 1600 (+600: call + 2000-char snapshot) → 2200 (+600) → 2150 (trimmed).
    """
    call = ToolCallPart("browser_snapshot", {}, tool_call_id="c")
    return [
        ModelRequest(parts=[SystemPromptPart("s" * 400), UserPromptPart("t" * 400)]),
        _response(1000, call),
        ModelRequest(parts=[ToolReturnPart("browser_snapshot", "x" * 2000, tool_call_id="c")]),
        _response(1600, call),
        ModelRequest(parts=[ToolReturnPart("browser_snapshot", "y" * 2000, tool_call_id="c")]),
        _response(2200, call),
        ModelRequest(parts=[ToolReturnPart("browser_click", "ok", tool_call_id="c")]),
        _response(2150, TextPart("done")),
    ]


def test_profile_reads_each_requests_own_prompt_size(monkeypatch):
    monkeypatch.delenv("MODEL_CONTEXT_WINDOWS", raising=False)
    profile = _profile(_history())

    assert profile["per_request"] == [1000, 1600, 2200, 2150]
    assert (profile["first"], profile["peak"], profile["final"]) == (1000, 2200, 2150)
    assert (profile["peak_request"], profile["requests"]) == (3, 4)
    assert profile["window"] is None and profile["peak_pct"] is None
    assert json.loads(json.dumps(profile)) == profile


def test_composition_splits_measured_growth_and_sums_to_the_peak(monkeypatch):
    monkeypatch.delenv("MODEL_CONTEXT_WINDOWS", raising=False)
    composition = _profile(_history())["composition"]

    # Growth steps are 100% attributed: snapshots get almost all of the 2 x 600 tokens.
    assert composition["tool: browser_snapshot"] > 1100
    assert composition["model tool calls"] > 0
    # First request: prompt parts at ~4 chars/token (prose), the remainder = tool schemas.
    assert composition["system prompt"] == composition["task message"] == 100
    assert composition[SCHEMAS] > 0
    assert abs(sum(composition.values()) - 2200) <= len(composition)
    assert list(composition.values()) == sorted(composition.values(), reverse=True)
    # The request after the peak (browser_click) is not part of the peak prompt.
    assert "tool: browser_click" not in composition


def test_single_request_prompt_is_estimated_as_prose(monkeypatch):
    monkeypatch.delenv("MODEL_CONTEXT_WINDOWS", raising=False)
    history = [ModelRequest(parts=[UserPromptPart("t" * 400)]), _response(300, TextPart("x"))]
    composition = _profile(history)["composition"]
    assert composition == {SCHEMAS: 200, "task message": 100}


def test_instructions_count_as_the_system_prompt_once(monkeypatch):
    monkeypatch.delenv("MODEL_CONTEXT_WINDOWS", raising=False)
    history = [
        ModelRequest(parts=[UserPromptPart("t" * 40)], instructions="i" * 400),
        _response(500, TextPart("x")),
    ]
    assert _profile(history)["composition"]["system prompt"] == 100


def test_no_per_response_usage_gives_no_profile():
    history = [ModelRequest(parts=[UserPromptPart("hi")]), ModelResponse(parts=[TextPart("x")])]
    assert profile_context(history, "m") is None
    assert format_peak(None) == "-"


def test_window_share_comes_from_model_context_windows(monkeypatch):
    monkeypatch.setenv(
        "MODEL_CONTEXT_WINDOWS", " openai/gpt-oss-120b = 4400 , junk, bad=x, zero=0, m=8000"
    )
    assert context_windows() == {"openai/gpt-oss-120b": 4400, "m": 8000}
    profile = _profile(_history(), "openai/gpt-oss-120b")
    assert (profile["window"], profile["peak_pct"]) == (4400, 50.0)
    assert format_peak(profile) == "2,200 (50%)"
    assert describe_context(profile, top=0) == (
        "peak 2,200 (50%) of 4,400 at request 3/4, first 1,000, final 2,150"
    )


def test_near_the_window_limit_logs_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("MODEL_CONTEXT_WINDOWS", "m=2500")
    with caplog.at_level(logging.INFO, logger="ai_test_gen.core.context_window"):
        log_context("Planner", _profile(_history()))
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("Planner context: peak 2,200 (88%) of 2,500")
    assert "NEAR THE CONTEXT LIMIT" in record.getMessage()


def test_merged_records_keep_the_higher_peak_and_format_the_composition(monkeypatch):
    monkeypatch.delenv("MODEL_CONTEXT_WINDOWS", raising=False)
    big = _profile(_history())
    small = _profile(
        [ModelRequest(parts=[UserPromptPart("t")]), _response(10, TextPart("x"))]
    )
    log = UsageLog()
    log.record("Planner", "m", RunUsage(requests=4), 1.0, ok=True, context=big)
    log.record("Planner", "m", RunUsage(requests=1), 1.0, ok=True, context=small)
    log.record("Planner vision", "v", RunUsage(requests=1), 1.0, ok=True)

    report = log.summary(2.0)
    assert report["agents"][0]["context"] == big
    assert report["agents"][1]["context"] is None

    lines = format_context(report).splitlines()
    assert lines[0] == "Planner (m): peak 2,200 at request 3/4, first 1,000, final 2,150"
    snapshot = big["composition"]["tool: browser_snapshot"]
    assert lines[1].split()[:3] == ["tool:", "browser_snapshot", f"{snapshot:,}"]
    assert not any("vision" in line for line in lines)
