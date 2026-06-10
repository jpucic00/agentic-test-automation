"""Unit tests for the snapshot-history trimmer — fully local (no model, no MCP).

Builds synthetic pydantic-ai message histories shaped like a Playwright MCP run:
browser tool returns embed a "Page URL:" header and a "Page Snapshot" section;
``browser_generate_locator`` returns a bare locator expression. Anchor semantics:
the snapshot preceding a locator capture is a milestone and survives trimming,
deduped to the latest state per (page URL, dialog-open?).
"""
from __future__ import annotations

import logging

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)

from ai_test_gen.agents._history import (
    anchor_snapshots_enabled,
    snapshot_history_keep,
    trim_stale_snapshots,
)

_STUB_MARKER = "[older page snapshot trimmed"
_HISTORY_LOGGER = "ai_test_gen.agents._history"


def _browser_return(call_id, action, *, url=None, dialog=False, tool="browser_click"):
    url_line = f"\n- Page URL: {url}" if url else ""
    dialog_line = '\n- dialog "Create user":\n  - textbox "Email"' if dialog else ""
    content = (
        f"{action}{url_line}\n- Page Snapshot\n"
        f"```yaml{dialog_line}\n- button \"Save\" [ref=e{call_id}]\n```"
    )
    return ToolReturnPart(tool_name=tool, content=content, tool_call_id=call_id)


def _locator_return(call_id, locator="getByTestId('save-button')"):
    return ToolReturnPart(
        tool_name="browser_generate_locator", content=locator, tool_call_id=call_id
    )


def _flow(*parts):
    """One ModelResponse+ModelRequest pair per tool return, after a user prompt."""
    messages: list = [ModelRequest(parts=[UserPromptPart(content="plan QA-1")])]
    for i, part in enumerate(parts):
        messages.append(ModelResponse(parts=[TextPart(content=f"step {i}")]))
        messages.append(ModelRequest(parts=[part]))
    return messages


def _history(num_snapshots: int):
    return _flow(*[_browser_return(str(i), f"Clicked step {i}") for i in range(num_snapshots)])


def _tool_returns(messages):
    return [
        p for m in messages if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart)
    ]


def _kept_ids(messages):
    return {
        p.tool_call_id for p in _tool_returns(messages) if "```yaml" in str(p.content)
    }


def _stubbed_ids(messages):
    return {
        p.tool_call_id for p in _tool_returns(messages) if _STUB_MARKER in str(p.content)
    }


def _no_anchor_env(monkeypatch, keep="2"):
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", keep)
    monkeypatch.delenv("ANCHOR_SNAPSHOTS", raising=False)


# --- transient window (no locator captures → no anchors) ---------------------------


def test_keeps_latest_n_and_stubs_older(monkeypatch):
    _no_anchor_env(monkeypatch, keep="2")
    out = trim_stale_snapshots(_history(5))
    assert _kept_ids(out) == {"3", "4"}
    assert _stubbed_ids(out) == {"0", "1", "2"}
    # The action confirmation survives the trim — only the snapshot body is gone —
    # and the stub points forward instead of inviting re-verification.
    stub = next(p for p in _tool_returns(out) if p.tool_call_id == "0")
    assert str(stub.content).startswith("Clicked step 0")
    assert "re-capture only if you changed the page since" in str(stub.content)


def test_no_trim_when_at_or_under_keep(monkeypatch):
    _no_anchor_env(monkeypatch, keep="2")
    messages = _history(2)
    assert trim_stale_snapshots(messages) is messages  # untouched, same object


def test_non_snapshot_messages_never_altered(monkeypatch):
    _no_anchor_env(monkeypatch, keep="1")
    locator = _locator_return("loc1")
    messages = _history(3)
    messages.insert(2, ModelRequest(parts=[locator]))
    out = trim_stale_snapshots(messages)
    # The user prompt, model turns, and the verified locator are byte-identical.
    assert out[0] is messages[0]
    kept_locator = [
        p for p in _tool_returns(out) if p.tool_name == "browser_generate_locator"
    ]
    assert kept_locator[0].content == "getByTestId('save-button')"
    for original, processed in zip(messages, out, strict=True):
        if isinstance(original, ModelResponse):
            assert processed is original


def test_mcp_content_item_lists_are_handled(monkeypatch):
    # pydantic-ai may surface MCP tool results as content-item lists, not plain strings.
    _no_anchor_env(monkeypatch, keep="0")
    part = ToolReturnPart(
        tool_name="browser_click",
        content=[{"type": "text", "text": "Clicked\n- Page Snapshot\n```yaml\nbig\n```"}],
        tool_call_id="1",
    )
    out = trim_stale_snapshots([ModelRequest(parts=[part])])
    (new_part,) = _tool_returns(out)
    assert isinstance(new_part.content, str)
    assert _STUB_MARKER in new_part.content


# --- anchors ------------------------------------------------------------------------


def _users_flow():
    """Capture on /users, then four transit pages."""
    return _flow(
        _browser_return("1", "Opened users", url="https://s/users"),
        _locator_return("L1"),
        _browser_return("2", "Clicked a", url="https://s/o1"),
        _browser_return("3", "Clicked b", url="https://s/o2"),
        _browser_return("4", "Clicked c", url="https://s/o3"),
        _browser_return("5", "Clicked d", url="https://s/o4"),
    )


def test_anchor_survives_while_transit_stubs(monkeypatch):
    _no_anchor_env(monkeypatch, keep="2")
    out = trim_stale_snapshots(_users_flow())
    # "1" is the anchor (a locator was captured there); "4"/"5" are the transient
    # window; the equally-old transit pages "2"/"3" are stubbed.
    assert _kept_ids(out) == {"1", "4", "5"}
    assert _stubbed_ids(out) == {"2", "3"}


def test_anchor_dedup_keeps_latest_per_page_state(monkeypatch):
    _no_anchor_env(monkeypatch, keep="1")
    out = trim_stale_snapshots(
        _flow(
            _browser_return("1", "Opened users", url="https://s/users"),
            _locator_return("L1"),
            _browser_return("2", "Filled email", url="https://s/users"),
            _locator_return("L2"),
            _browser_return("3", "Clicked a", url="https://s/o1"),
            _browser_return("4", "Clicked b", url="https://s/o2"),
        )
    )
    # Two captures on the same page state collapse to ONE anchor — the latest ("2").
    assert _kept_ids(out) == {"2", "4"}
    assert _stubbed_ids(out) == {"1", "3"}


def test_modal_and_page_anchors_coexist_for_same_url(monkeypatch):
    _no_anchor_env(monkeypatch, keep="1")
    out = trim_stale_snapshots(
        _flow(
            _browser_return("1", "Opened users", url="https://s/users"),
            _locator_return("L1"),
            _browser_return("2", "Opened Create user modal", url="https://s/users", dialog=True),
            _locator_return("L2"),
            _browser_return("3", "Closed modal", url="https://s/users"),
            _browser_return("4", "Clicked elsewhere", url="https://s/o1"),
        )
    )
    # Modals don't change the URL: the dialog-open flag keys them separately, so the
    # page-state anchor ("1") and the modal-state anchor ("2") both survive.
    assert _kept_ids(out) == {"1", "2", "4"}
    assert _stubbed_ids(out) == {"3"}


def test_anchor_snapshots_off_reproduces_chronological(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "2")
    monkeypatch.setenv("ANCHOR_SNAPSHOTS", "off")
    out = trim_stale_snapshots(_users_flow())
    # The escape hatch: pure keep-newest-N, the anchor is stubbed like any transit.
    assert _kept_ids(out) == {"4", "5"}
    assert _stubbed_ids(out) == {"1", "2", "3"}


def test_idempotent_across_requests_with_anchors(monkeypatch):
    # The processor runs before EVERY model request; anchors must be stable fixed
    # points (the locator returns defining them are never trimmed) and stubs must
    # not be re-mangled or re-counted.
    _no_anchor_env(monkeypatch, keep="1")
    once = trim_stale_snapshots(_users_flow())
    twice = trim_stale_snapshots(once)
    assert [str(m) for m in twice] == [str(m) for m in once]


def test_capture_before_any_snapshot_is_ignored(monkeypatch):
    _no_anchor_env(monkeypatch, keep="1")
    out = trim_stale_snapshots(
        _flow(
            _locator_return("L0"),  # nothing to anchor yet
            _browser_return("1", "Opened a", url="https://s/a"),
            _browser_return("2", "Opened b", url="https://s/b"),
        )
    )
    assert _kept_ids(out) == {"2"}
    assert _stubbed_ids(out) == {"1"}


def test_tripwire_warning_when_anchor_count_excessive(monkeypatch, caplog):
    _no_anchor_env(monkeypatch, keep="1")
    parts = []
    for i in range(12):  # 12 distinct pages, each with a capture
        parts.append(_browser_return(str(i), f"Opened page {i}", url=f"https://s/p{i}"))
        parts.append(_locator_return(f"L{i}"))
    parts.append(_browser_return("99", "One more transit", url="https://s/extra"))
    with caplog.at_level(logging.WARNING, logger=_HISTORY_LOGGER):
        trim_stale_snapshots(_flow(*parts))
    warnings = [r.getMessage() for r in caplog.records if "anchor snapshots" in r.getMessage()]
    assert warnings and "12" in warnings[0]


# --- env knobs ----------------------------------------------------------------------


def test_snapshot_history_keep_env_and_default(monkeypatch):
    monkeypatch.delenv("SNAPSHOT_HISTORY_KEEP", raising=False)
    assert snapshot_history_keep() == 2
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "5")
    assert snapshot_history_keep() == 5
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "-1")  # clamped
    assert snapshot_history_keep() == 0
    monkeypatch.setenv("SNAPSHOT_HISTORY_KEEP", "x")  # invalid -> default
    assert snapshot_history_keep() == 2


def test_anchor_snapshots_enabled_env_and_default(monkeypatch):
    monkeypatch.delenv("ANCHOR_SNAPSHOTS", raising=False)
    assert anchor_snapshots_enabled() is True
    for off in ("off", "FALSE", "0", "no"):
        monkeypatch.setenv("ANCHOR_SNAPSHOTS", off)
        assert anchor_snapshots_enabled() is False
    monkeypatch.setenv("ANCHOR_SNAPSHOTS", "on")
    assert anchor_snapshots_enabled() is True
