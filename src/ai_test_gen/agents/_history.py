"""History processor: trim stale accessibility snapshots from browser-agent runs.

Every Playwright MCP browser tool result embeds a full page snapshot (the YAML
accessibility tree). pydantic-ai replays the complete message history on every
model request, so a Planner/Healer exploration at up to ``AGENT_REQUEST_LIMIT``
round-trips drags dozens of large snapshots along — almost all stale the moment
the agent navigates on. That pushes the conversation deep into the token range
where mid-tier models degrade (the same heuristic behind the ~800-word prompt
budget) and inflates gateway cost per run.

``trim_stale_snapshots`` keeps the most recent ``SNAPSHOT_HISTORY_KEEP`` snapshot
tool-returns verbatim and truncates older ones to a short stub, preserving the
pre-snapshot action confirmation ("Clicked …") so the agent still sees what it
did. Everything else is untouched: the system/user prompts, the model's own
turns, and — critically — ``browser_generate_locator`` results (the verified
locators the whole selector strategy depends on) are never trimmed.

Attached to the Planner/Healer via ``Agent(capabilities=[ProcessHistory(...)])``.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart

# Playwright MCP marks the accessibility tree section of every browser tool result.
_SNAPSHOT_MARKER = "Page Snapshot"
_STUB = "[stale page snapshot removed: only the most recent page snapshots are kept]"

# Tool returns that must NEVER be trimmed, regardless of size or content.
_PROTECTED_TOOLS = ("browser_generate_locator",)


def snapshot_history_keep(default: int = 2) -> int:
    """How many of the most recent page snapshots to keep verbatim in history.

    Override via the ``SNAPSHOT_HISTORY_KEEP`` env var. The latest snapshot is what
    the agent acts on; one or two earlier ones help it backtrack. Anything older is
    stale weight.
    """
    raw = os.environ.get("SNAPSHOT_HISTORY_KEEP")
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def trim_stale_snapshots(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Truncate all but the newest N snapshot-bearing browser tool returns.

    Pure with respect to its input: returns new message/part objects for anything
    it changes (the stored run history is not mutated). Idempotent — already-stubbed
    parts are not snapshot-bearing and are skipped on later passes.
    """
    keep = snapshot_history_keep()
    locations: list[tuple[int, int]] = []
    for msg_idx, msg in enumerate(messages):
        if not isinstance(msg, ModelRequest):
            continue
        for part_idx, part in enumerate(msg.parts):
            if isinstance(part, ToolReturnPart) and _is_snapshot_bearing(part):
                locations.append((msg_idx, part_idx))

    stale = set(locations[: len(locations) - keep]) if len(locations) > keep else set()
    if not stale:
        return messages

    trimmed: list[ModelMessage] = []
    for msg_idx, msg in enumerate(messages):
        if isinstance(msg, ModelRequest) and any(
            (msg_idx, part_idx) in stale for part_idx in range(len(msg.parts))
        ):
            new_parts = [
                _truncate(part) if (msg_idx, part_idx) in stale else part  # type: ignore[arg-type]
                for part_idx, part in enumerate(msg.parts)
            ]
            trimmed.append(dataclasses.replace(msg, parts=new_parts))
        else:
            trimmed.append(msg)
    return trimmed


def _is_snapshot_bearing(part: ToolReturnPart) -> bool:
    """True for a browser tool return that still carries a full page snapshot."""
    if part.tool_name in _PROTECTED_TOOLS or not part.tool_name.startswith("browser_"):
        return False
    text = _content_text(part.content)
    return text is not None and _SNAPSHOT_MARKER in text and _STUB not in text


def _truncate(part: ToolReturnPart) -> ToolReturnPart:
    """Replace the snapshot section with a stub, keeping the action confirmation."""
    text = _content_text(part.content) or ""
    marker_at = text.find(_SNAPSHOT_MARKER)
    head = text[:marker_at].rstrip() if marker_at >= 0 else ""
    new_content = f"{head}\n{_STUB}" if head else _STUB
    return dataclasses.replace(part, content=new_content)


def _content_text(content: Any) -> str | None:
    """Best-effort text of a tool return (plain string or MCP content-item list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if text:
                texts.append(text)
        return "\n".join(texts) or None
    return None
