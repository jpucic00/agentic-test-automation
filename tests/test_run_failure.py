"""Unit tests for the run-failure summarizer — offline, synthetic message histories."""
from __future__ import annotations

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from ai_test_gen.agents._run_failure import summarize_run_failure


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
    assert summarize_run_failure(RuntimeError("boom"), []) == "(no captured messages)"
