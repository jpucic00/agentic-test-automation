"""Unit tests for the snapshot-history trimmer — fully local (no model, no MCP).

Builds synthetic pydantic-ai message histories shaped like a Playwright MCP run:
browser tool returns embed a "Page Snapshot" section; ``browser_generate_locator``
returns a bare locator expression.
"""
from __future__ import annotations

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)

from ai_test_gen.agents._history import snapshot_history_keep, trim_stale_snapshots

_STUB_MARKER = "[stale page snapshot removed"


def _browser_return(call_id: str, action: str, *, tool: str = "browser_click"):
    content = f"{action}\n- Page Snapshot\n```yaml\n- button \"Save\" [ref=e{call_id}]\n```"
    return ToolReturnPart(tool_name=tool, content=content, tool_call_id=call_id)


def _history(num_snapshots: int):
    messages: list = [ModelRequest(parts=[UserPromptPart(content="plan QA-1")])]
    for i in range(num_snapshots):
        messages.append(ModelResponse(parts=[TextPart(content=f"clicking step {i}")]))
        messages.append(ModelRequest(parts=[_browser_return(str(i), f"Clicked step {i}")]))
    return messages


def _tool_returns(messages):
    return [
        p for m in messages if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart) and isinstance(p.content, str)
    ]


def test_keeps_latest_n_and_stubs_older(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "2")
    out = trim_stale_snapshots(_history(5))
    returns = _tool_returns(out)
    stubbed = [str(p.content) for p in returns if _STUB_MARKER in str(p.content)]
    intact = [p for p in returns if "```yaml" in str(p.content)]
    assert len(stubbed) == 3  # oldest three trimmed
    assert len(intact) == 2  # newest two verbatim
    assert intact[0].tool_call_id == "3" and intact[1].tool_call_id == "4"
    # The action confirmation survives the trim — only the snapshot body is gone.
    assert stubbed[0].startswith("Clicked step 0")


def test_no_trim_when_at_or_under_keep(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "2")
    messages = _history(2)
    assert trim_stale_snapshots(messages) is messages  # untouched, same object


def test_non_snapshot_messages_never_altered(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "1")
    locator = ToolReturnPart(
        tool_name="browser_generate_locator",
        content="getByTestId('save-button')",
        tool_call_id="loc1",
    )
    messages = _history(3)
    messages.insert(2, ModelRequest(parts=[locator]))
    out = trim_stale_snapshots(messages)
    # The user prompt, model turns, and the verified locator are byte-identical.
    assert out[0] is messages[0]
    kept_locator = [
        p for m in out if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == "browser_generate_locator"
    ]
    assert kept_locator[0].content == "getByTestId('save-button')"
    # Model turns are passed through as the SAME objects, never rebuilt.
    for original, processed in zip(messages, out, strict=True):
        if isinstance(original, ModelResponse):
            assert processed is original


def test_idempotent_across_requests(monkeypatch):
    # The processor runs before EVERY model request; stubs from earlier passes must not
    # count as snapshots or be re-mangled.
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "1")
    once = trim_stale_snapshots(_history(4))
    twice = trim_stale_snapshots(once)
    assert [str(m) for m in twice] == [str(m) for m in once]


def test_mcp_content_item_lists_are_handled(monkeypatch):
    # pydantic-ai may surface MCP tool results as content-item lists, not plain strings.
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "0")
    part = ToolReturnPart(
        tool_name="browser_click",
        content=[{"type": "text", "text": "Clicked\n- Page Snapshot\n```yaml\nbig\n```"}],
        tool_call_id="1",
    )
    out = trim_stale_snapshots([ModelRequest(parts=[part])])
    (new_part,) = _tool_returns(out)
    assert isinstance(new_part.content, str)
    assert _STUB_MARKER in new_part.content


def test_snapshot_history_keep_env_and_default(monkeypatch):
    monkeypatch.delenv("SNAPSHOT_HISTORY_KEEP", raising=False)
    assert snapshot_history_keep() == 2
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "5")
    assert snapshot_history_keep() == 5
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "-1")  # clamped
    assert snapshot_history_keep() == 0
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "x")  # invalid -> default
    assert snapshot_history_keep() == 2
