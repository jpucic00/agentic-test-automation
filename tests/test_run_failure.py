"""Unit tests for the run-failure summarizer — offline, synthetic message histories."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from ai_test_gen.agents._run_failure import (
    _leaf_exceptions,
    run_agent_logged,
    summarize_run_failure,
)


def test_summary_shows_causes_retry_errors_and_keeps_args_tail():
    exc = RuntimeError("Exceeded maximum retries (5) for output validation")
    exc.__cause__ = ValueError("1 validation error for TestPlan")
    # An unterminated emission, like a token-capped final_result call: the proof is the tail.
    big_args = '{"steps": [' + ('{"action": "click"}, ' * 300) + '{"action": "trunca'
    messages = [
        ModelRequest(parts=[UserPromptPart(content="plan QA-1")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="final_result", args=big_args, tool_call_id="c1")]
        ),
        ModelRequest(parts=[RetryPromptPart(content="Invalid JSON: unterminated string")]),
    ]

    summary = summarize_run_failure(exc, messages)

    assert "cause: ValueError('1 validation error for TestPlan')" in summary
    assert "tool=final_result" in summary
    assert f"args {len(big_args)} chars" in summary
    assert big_args[-80:] in summary  # tail survives clipping — the truncation evidence
    assert "unterminated string" in summary  # the retry prompt (validation error) is shown
    assert "plan QA-1" not in summary  # user prompt is noise, skipped


def test_snapshot_noise_is_skipped_and_empty_history_is_graceful():
    messages = [
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="browser_snapshot", content="huge a11y tree", tool_call_id="c2"
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content="stray thinking text in the output turn")]),
    ]

    summary = summarize_run_failure(RuntimeError("boom"), messages)

    assert "huge a11y tree" not in summary  # ToolReturnPart is replay noise
    assert "stray thinking text in the output turn" in summary  # TextPart is evidence
    # An empty capture is stated as a fact, never silent.
    assert "captured 0 message(s)" in summarize_run_failure(RuntimeError("boom"), [])


def test_partless_responses_are_flagged_with_their_metadata():
    # An EMPTY model response (no text, no thinking, no tool call) is only diagnosable via
    # its metadata: the summary must flag NO PARTS and print usage/model when available.
    empty = ModelResponse(parts=[])
    summary = summarize_run_failure(RuntimeError("boom"), [empty])

    assert "ModelResponse: NO PARTS" in summary
    assert "usage in=" in summary  # metadata line present even without parts


def test_cause_tree_descends_groups_to_the_inner_errors():
    # The exact laptop shape: UnexpectedModelBehavior -> ExceptionGroup ->
    # UnexpectedModelBehavior -> ValidationError. The summary must surface the innermost
    # detail, not just repeat the wrapper reprs.
    inner = ValueError("3 validation errors for TestPlan")
    mid = UnexpectedModelBehavior("Exceeded maximum retries (5) for output validation")
    mid.__cause__ = inner
    outer = UnexpectedModelBehavior("exceeded maximum output retries (5)")
    outer.__cause__ = BaseExceptionGroup("unhandled errors in a TaskGroup", [mid])

    summary = summarize_run_failure(outer, [])

    assert "Exceeded maximum retries (5) for output validation" in summary
    assert "3 validation errors for TestPlan" in summary


def test_leaf_exceptions_flatten_nested_groups():
    inner = ValueError("inner")
    group = BaseExceptionGroup("outer", [BaseExceptionGroup("nested", [inner]), KeyError("k")])
    leaves = _leaf_exceptions(group)
    assert inner in leaves
    assert len(leaves) == 2


class _FakeAgent:
    """Raises a task-group-wrapped retry exhaustion, like the agent/MCP internals can."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def run(self, *args: object, **kwargs: object) -> object:
        raise BaseExceptionGroup(
            "unhandled errors in a TaskGroup",
            [UnexpectedModelBehavior("Exceeded maximum retries (5) for output validation")],
        )


def test_taskgroup_wrapped_exhaustion_still_logs_evidence(caplog):
    # The laptop failure mode: pydantic-ai's exhaustion surfaces inside an ExceptionGroup, so
    # a plain `except UnexpectedModelBehavior` never fires — the group handler must log the
    # leaves AND the evidence block, then re-raise the group unchanged.
    with caplog.at_level(logging.ERROR, logger="ai_test_gen.agents._run_failure"):
        with pytest.raises(BaseExceptionGroup):
            asyncio.run(run_agent_logged(cast(Any, _FakeAgent()), "go", agent_label="Planner"))

    assert "task-group failure" in caplog.text
    assert "Exceeded maximum retries (5) for output validation" in caplog.text
    assert "retry-exhaustion evidence" in caplog.text


class _FakePlainCrashAgent:
    """Raises an exception that is neither UnexpectedModelBehavior nor a group."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def run(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("connection dropped mid-turn")


def test_any_exception_logs_marker_and_evidence_backstop(caplog):
    # Nothing may leave the frame silently: the catch-all logs any exception shape, and the
    # INFO start marker proves in the run log that the evidence-capture code is running.
    with caplog.at_level(logging.INFO, logger="ai_test_gen.agents._run_failure"):
        with pytest.raises(RuntimeError):
            asyncio.run(
                run_agent_logged(cast(Any, _FakePlainCrashAgent()), "go", agent_label="Planner")
            )

    assert "failure-evidence capture armed" in caplog.text
    assert "RuntimeError('connection dropped mid-turn')" in caplog.text
