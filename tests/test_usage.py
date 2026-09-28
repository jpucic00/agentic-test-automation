"""Unit tests for per-agent usage accounting — offline (FunctionModel doubles, no network).

Covers the ``UsageLog`` aggregation math, that ``run_agent_logged`` records a successful AND an
aborted run (partial usage, outcome ``error``), that heal attempts get their own records, that
Vision Aid usage lands under its owning agent run, and the ``format_usage`` table.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage

from ai_test_gen import models
from ai_test_gen.agents import _vision_aid as vision_aid_mod
from ai_test_gen.agents import generator as generator_mod
from ai_test_gen.agents import healer as healer_mod
from ai_test_gen.agents import planner as planner_mod
from ai_test_gen.agents import vision as vision_mod
from ai_test_gen.agents._run_failure import run_agent_logged
from ai_test_gen.usage import UsageLog, describe, format_duration, format_usage

# --- aggregation math -------------------------------------------------------------


def test_records_merge_by_label_and_totals_sum_every_record():
    log = UsageLog()
    log.record("Planner", "planner-model", RunUsage(requests=3, input_tokens=100, output_tokens=10,
               cache_read_tokens=40, details={"reasoning_tokens": 7}), 12.5, ok=True)
    log.record("Planner vision", "vision-model", RunUsage(requests=1, input_tokens=50,
               output_tokens=5), 1.0, ok=True)
    log.record("Planner vision", "vision-model", RunUsage(requests=1, input_tokens=60,
               output_tokens=6), 2.0, ok=False)
    log.record("Generator", "generator-model", RunUsage(requests=1, input_tokens=30,
               output_tokens=300), 3.25, ok=True)

    report = log.summary(100.0)

    assert [a["agent"] for a in report["agents"]] == ["Planner", "Planner vision", "Generator"]
    vision = report["agents"][1]
    assert (vision["runs"], vision["requests"], vision["input_tokens"]) == (2, 2, 110)
    assert vision["wall_s"] == 3.0
    assert vision["outcome"] == "error"  # one failed call marks the merged record
    planner = report["agents"][0]
    assert (planner["cache_read_tokens"], planner["reasoning_tokens"]) == (40, 7)
    assert report["total"] == {
        "requests": 6,
        "input_tokens": 240,
        "output_tokens": 321,
        "cache_read_tokens": 40,
        "reasoning_tokens": 7,
        "wall_s": 100.0,  # the whole run, not a sum of the (nested) agent walls
    }
    assert json.loads(json.dumps(report)) == report  # plain JSON-serialisable data


def test_empty_log_summary_has_zero_totals():
    report = UsageLog().summary(0.5)
    assert report["agents"] == []
    assert report["total"]["requests"] == 0
    assert report["total"]["wall_s"] == 0.5


def test_describe_and_duration_formatting():
    assert describe(41, 512_340, 6_210, 243) == "41 requests, in=512,340 out=6,210 tokens, 4m03s"
    assert describe(1, 5, 2, 1.23) == "1 request, in=5 out=2 tokens, 1.2s"
    assert format_duration(59.94) == "59.9s"
    assert format_duration(3600) == "60m00s"


def test_format_usage_is_an_aligned_table_with_a_total_line():
    log = UsageLog()
    log.record("Planner", "gpt-oss-120b", RunUsage(requests=41, input_tokens=512_340,
               output_tokens=6_210), 243.0, ok=True)
    log.record("Healer attempt 1", "gpt-oss-120b", RunUsage(requests=9, input_tokens=88_000,
               output_tokens=1_500), 61.0, ok=False)

    table = format_usage(log.summary(420.0))
    lines = table.splitlines()

    assert lines[0].split() == ["agent", "model", "requests", "in", "out", "wall"]
    assert lines[1].split() == ["Planner", "gpt-oss-120b", "41", "512,340", "6,210", "4m03s"]
    assert lines[2].endswith("(aborted)")
    assert lines[3].split() == ["total", "50", "600,340", "7,710", "7m00s"]
    # Right-aligned numeric columns: the "requests" figures end in the same column.
    assert lines[1].index("41") + 2 == lines[3].index("50") + 2


# --- run_agent_logged: success and abort ------------------------------------------


def _tool_then(final):
    """FunctionModel: first turn calls tool ``t`` (100 in / 7 out), then ``final(messages)``."""
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart("t", {})], usage=RequestUsage(input_tokens=100, output_tokens=7)
            )
        return final(messages)

    return fn


def _agent(fn) -> Agent[None, str]:
    agent = Agent(FunctionModel(fn, model_name="fn-model"), output_type=str)

    @agent.tool_plain
    def t() -> str:
        return "ok"

    return agent


def test_successful_run_records_usage_and_logs_one_line(caplog):
    def done(_messages):
        return ModelResponse(
            parts=[TextPart("done")], usage=RequestUsage(input_tokens=120, output_tokens=3)
        )

    log = UsageLog()
    with caplog.at_level(logging.INFO, logger="ai_test_gen.usage"):
        out = asyncio.run(
            run_agent_logged(_agent(_tool_then(done)), "go", agent_label="Generator", usage=log)
        )

    assert out == "done"
    [rec] = log.summary(1.0)["agents"]
    assert rec["agent"] == "Generator"
    assert rec["model"] == "fn-model"
    assert (rec["requests"], rec["input_tokens"], rec["output_tokens"]) == (2, 220, 10)
    assert rec["tool_calls"] == 1
    assert rec["outcome"] == "ok"
    lines = [r.getMessage() for r in caplog.records if "usage:" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("Generator usage: 2 requests, in=220 out=10 tokens, ")
    assert "(aborted)" not in lines[0]


def test_aborted_run_still_records_partial_usage(caplog):
    def crash(_messages):
        raise RuntimeError("gateway dropped the connection")

    log = UsageLog()
    with caplog.at_level(logging.INFO, logger="ai_test_gen.usage"):
        with pytest.raises(RuntimeError):
            asyncio.run(
                run_agent_logged(_agent(_tool_then(crash)), "go", agent_label="Planner", usage=log)
            )

    [rec] = log.summary(1.0)["agents"]
    # The completed first turn is reported; the request that died carried no usage.
    assert (rec["requests"], rec["input_tokens"], rec["output_tokens"]) == (1, 100, 7)
    assert rec["outcome"] == "error"
    assert any(
        r.getMessage().startswith("Planner usage: 1 request, in=100 out=7 tokens")
        and r.getMessage().endswith("(aborted)")
        for r in caplog.records
    )


def test_request_limit_abort_records_what_was_spent(monkeypatch):
    # UsageLimitExceeded (AGENT_REQUEST_LIMIT) — the classic "ran out of turns" abort.
    def loop(_messages):
        return ModelResponse(
            parts=[ToolCallPart("t", {})], usage=RequestUsage(input_tokens=10, output_tokens=1)
        )

    log = UsageLog()
    with pytest.raises(UsageLimitExceeded):
        asyncio.run(
            run_agent_logged(
                _agent(_tool_then(loop)), "go", agent_label="Healer", usage=log, request_limit=3
            )
        )

    [rec] = log.summary(1.0)["agents"]
    assert rec["requests"] == 3
    assert rec["input_tokens"] == 100 + 2 * 10
    assert rec["outcome"] == "error"


def test_run_without_a_log_still_runs_and_logs(caplog):
    def done(_messages):
        return ModelResponse(parts=[TextPart("ok")])

    with caplog.at_level(logging.INFO, logger="ai_test_gen.usage"):
        asyncio.run(run_agent_logged(_agent(_tool_then(done)), "go", agent_label="Mapper"))
    assert any(r.getMessage().startswith("Mapper usage:") for r in caplog.records)


# --- heal attempts: one record each ------------------------------------------------


def test_each_heal_attempt_is_its_own_record(cfg, monkeypatch):
    def heal_model(messages, info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"file_name": "x.spec.ts", "code": "// fixed", "changes_summary": "s"},
                )
            ],
            usage=RequestUsage(input_tokens=500, output_tokens=50),
        )

    labels: list[str] = []

    def fake_build(config, *, storage_state, vision_stats, usage, usage_label):
        labels.append(usage_label)
        return Agent(FunctionModel(heal_model, model_name="healer-model"),
                     output_type=models.HealedTest)

    monkeypatch.setattr(healer_mod, "build_healer", fake_build)
    test = models.GeneratedTest(file_name="x.spec.ts", code="// broken", description="d")
    failure = models.TestRunResult(status="failed", did_run=True, stdout="", stderr="")
    plan = models.TestPlan(test_case_key="QA-1", title="t", target_url="https://x", steps=[])
    case = models.ManualTestCase(key="QA-1", title="t")
    log = UsageLog()
    for attempt in (1, 2):
        asyncio.run(
            healer_mod.heal_test(cfg, test, failure, plan, case, usage=log, attempt=attempt)
        )

    records = log.summary(1.0)["agents"]
    assert [r["agent"] for r in records] == ["Healer attempt 1", "Healer attempt 2"]
    assert all(r["input_tokens"] == 500 and r["runs"] == 1 for r in records)
    assert labels == ["Healer attempt 1", "Healer attempt 2"]  # vision label derives from it


# --- Vision Aid: attributed to the owning agent run --------------------------------


def test_vision_usage_is_recorded_under_the_owning_run(cfg, monkeypatch):
    vcfg = dataclasses.replace(cfg, vision_max_calls=3)
    vcfg.snapshots_dir.mkdir(parents=True, exist_ok=True)
    (vcfg.snapshots_dir / "shot.png").write_bytes(b"img")
    calls = {"n": 0}

    def vision_model(messages, info):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("502 from the vision backend")
        return ModelResponse(
            parts=[TextPart("Answer: yes. On screen: notes list.")],
            usage=RequestUsage(input_tokens=1_000, output_tokens=20),
        )

    monkeypatch.setattr(
        vision_mod,
        "build_vision_agent",
        lambda config: Agent(FunctionModel(vision_model), output_type=str),
    )
    log = UsageLog()
    agent = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("x")])))
    tool = vision_aid_mod.register_inspect_screen(
        agent, vcfg, agent_label="Healer", usage=log, usage_label="Healer attempt 2 vision"
    )
    asyncio.run(tool("Is a toast visible?"))
    asyncio.run(tool("Did the dialog close?"))
    asyncio.run(tool("Anything else?"))  # backend error -> recorded, outcome error

    [rec] = log.summary(1.0)["agents"]
    assert rec["agent"] == "Healer attempt 2 vision"
    assert rec["model"] == "vision-model"  # config.vision_model, not the owner's model
    assert (rec["runs"], rec["requests"], rec["input_tokens"]) == (3, 2, 2_000)
    assert rec["outcome"] == "error"


def test_vision_label_defaults_to_the_agent_label(cfg, monkeypatch):
    vcfg = dataclasses.replace(cfg, vision_max_calls=1)
    vcfg.snapshots_dir.mkdir(parents=True, exist_ok=True)
    (vcfg.snapshots_dir / "shot.png").write_bytes(b"img")
    monkeypatch.setattr(
        vision_mod,
        "build_vision_agent",
        lambda config: Agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("ok")]))),
    )
    log = UsageLog()
    agent = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("x")])))
    tool = vision_aid_mod.register_inspect_screen(agent, vcfg, usage=log)
    asyncio.run(tool("q"))
    assert [r["agent"] for r in log.summary(1.0)["agents"]] == ["Planner vision"]


def test_builders_hand_the_usage_log_to_the_vision_sensor(cfg, monkeypatch):
    vcfg = dataclasses.replace(cfg, vision_max_calls=2)
    seen: dict[str, object] = {}

    def spy(agent, config, **kwargs):
        seen.update(kwargs)
        return None

    log = UsageLog()
    monkeypatch.setattr(planner_mod, "_register_inspect_screen", spy)
    planner_mod.build_planner(vcfg, usage=log)
    assert seen["usage"] is log
    assert seen.get("usage_label") is None  # -> "Planner vision"

    seen.clear()
    monkeypatch.setattr(healer_mod, "register_inspect_screen", spy)
    healer_mod.build_healer(vcfg, usage=log, usage_label="Healer attempt 3")
    assert seen["usage"] is log
    assert seen["usage_label"] == "Healer attempt 3 vision"


def test_generator_compile_retry_is_its_own_record(cfg, monkeypatch):
    def gen_model(messages, info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"file_name": "x.spec.ts", "code": "// spec", "description": "d"},
                )
            ],
            usage=RequestUsage(input_tokens=300, output_tokens=40),
        )

    monkeypatch.setattr(
        generator_mod,
        "build_generator",
        lambda config: Agent(FunctionModel(gen_model), output_type=models.GeneratedTest),
    )
    plan = models.TestPlan(test_case_key="QA-1", title="t", target_url="https://x", steps=[])
    log = UsageLog()
    asyncio.run(generator_mod.generate_test(cfg, plan, usage=log))
    asyncio.run(
        generator_mod.generate_test(cfg, plan, previous_code="// bad", error_text="E", usage=log)
    )
    assert [r["agent"] for r in log.summary(1.0)["agents"]] == ["Generator", "Generator retry"]
