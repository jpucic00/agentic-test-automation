"""Unit tests for the shared direct-MCP helpers (agents/tools/mcp_direct.py) — fully local."""

from __future__ import annotations

from ai_test_gen.agents.tools.mcp_direct import (
    RESULT_CHAR_CAP,
    clean_result,
    result_text,
    underlying_mcp,
)


def test_clean_strips_trailing_page_snapshot():
    raw = (
        '### Result\n{"matchCount": 2}\n\n'
        "### Page state\n- Page Snapshot\n- generic:\n  - text: x"
    )
    out = clean_result(raw)
    assert '{"matchCount": 2}' in out
    assert "generic" not in out  # everything from the snapshot marker on is gone


def test_clean_caps_result_size():
    out = clean_result("x" * 10_000)
    assert len(out) <= RESULT_CHAR_CAP + 40
    assert out.endswith("…[probe result truncated]")


def test_clean_joins_content_item_lists():
    class _Item:
        def __init__(self, text):
            self.text = text

    assert clean_result([_Item("part-a"), {"text": "part-b"}]) == "part-a\npart-b"


def test_result_text_keeps_plain_string_items_in_a_list():
    # pydantic-ai returns a list of plain strings when a tool result has several text items.
    assert result_text(["### Result", '"[]"']) == '### Result\n"[]"'


def test_underlying_mcp_unwraps_filtered_layers():
    class _Raw:
        async def direct_call_tool(self, name, args): ...

    class _Wrap:
        def __init__(self, wrapped):
            self.wrapped = wrapped

    raw = _Raw()
    assert underlying_mcp(_Wrap(_Wrap(raw))) is raw  # walks the wrapper chain
    assert underlying_mcp(object()) is None  # nothing exposes direct_call_tool
