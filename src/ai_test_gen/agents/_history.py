"""History processor: trim stale accessibility snapshots from browser-agent runs.

**OPT-IN EXPERIMENT — disabled by default.** Trimming was built to cut the cost of
replaying dozens of large page snapshots (every Playwright MCP browser tool result
embeds the full YAML accessibility tree), but live runs showed the mid-tier
reasoning model losing coherence with it enabled: giving up exploration early,
skipping pages, and fabricating plan steps. Until a controlled A/B proves a net
win, the default is the full untrimmed history; set ``SNAPSHOT_HISTORY_KEEP`` to a
number to enable trimming for an experiment.

When ENABLED — two retention rules, two different jobs:

- **Transient window** (``SNAPSHOT_HISTORY_KEEP``, default 2): the newest snapshots,
  i.e. "what's in front of me right now". Deliberately small and independent of
  reasoning effort — high effort multiplies *exchanges per page*, not pages, so a
  bigger chronological window only buys more frames of the same page.
- **Anchors** (``ANCHOR_SNAPSHOTS``, default on): for every ``browser_generate_locator``
  return, the snapshot the agent was looking at when it captured the locator — a
  *milestone* page the test verifiably acts on. Anchors are kept unconditionally,
  deduped to the latest state per ``(page URL, dialog-open?)`` key, so repeated
  fill→capture cycles on one page collapse to a single anchor while a modal and its
  underlying page coexist. Anchor count is bounded by the flow's shape (pages +
  their modals), not by exchange count or effort.

Everything else is untouched: the system/user prompts, the model's own turns,
and — critically — ``browser_generate_locator`` results (the verified locators the
whole selector strategy depends on) are never trimmed, so a captured locator can
never be lost regardless of which snapshots are evicted.

Attached to the Planner/Healer via ``Agent(capabilities=[ProcessHistory(...)])``.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart

logger = logging.getLogger(__name__)

# Playwright MCP marks the accessibility tree section of every browser tool result.
_SNAPSHOT_MARKER = "Page Snapshot"
# Deliberately instruction-free: stub wording steers the model. "Only the most
# recent are kept" provoked re-verification loops; "re-capture only if you changed
# the page" made it stop verifying and fabricate. State what happened, nothing more.
_STUB = "[page snapshot omitted]"

# Tool returns that must NEVER be trimmed, regardless of size or content.
_PROTECTED_TOOLS = ("browser_generate_locator",)

# The MCP result header line naming the page the tool acted on.
_PAGE_URL_RE = re.compile(r"Page URL:\s*(\S+)")
# A dialog/alertdialog node in the snapshot YAML = a modal is open. Modals don't
# change the URL, so this flag is the second half of the anchor dedup key.
_DIALOG_RE = re.compile(r"^\s*-\s+(?:alert)?dialog\b", re.MULTILINE)

# Anchors are uncapped (their count is bounded by the flow's pages, not by
# exchanges), but an unexpectedly high count is worth a loud signal.
_ANCHOR_TRIPWIRE = 10


def snapshot_history_keep(default: int | None = None) -> int | None:
    """Transient-window size, or ``None`` when trimming is disabled (the default).

    Trimming is an opt-in experiment: set the ``SNAPSHOT_HISTORY_KEEP`` env var to
    a number to enable it (that many newest snapshots kept verbatim, plus anchors).
    Unset or unparseable → ``None`` → the history passes through untouched, which is
    the proven-safe behavior for the mid-tier gateway models.
    """
    raw = os.environ.get("SNAPSHOT_HISTORY_KEEP")
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def anchor_snapshots_enabled(default: bool = True) -> bool:
    """Whether milestone (anchor) snapshots are retained — ``ANCHOR_SNAPSHOTS`` env var.

    ``off``/``false``/``0``/``no`` disables anchors, reproducing the pure
    chronological keep-newest-N behavior (the A/B escape hatch).
    """
    raw = os.environ.get("ANCHOR_SNAPSHOTS")
    if raw is None:
        return default
    return raw.strip().lower() not in {"off", "false", "0", "no"}


def trim_stale_snapshots(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Truncate stale snapshot-bearing browser tool returns.

    No-op unless ``SNAPSHOT_HISTORY_KEEP`` is set (trimming is opt-in). When enabled,
    keeps the newest N snapshots (transient window) plus all anchor snapshots (latest
    state per ``(page URL, dialog-open?)`` of every page a locator was captured on).
    Pure with respect to its input: returns new message/part objects for anything it
    changes. Idempotent — stubbed parts no longer carry the snapshot marker and are
    skipped on later passes.
    """
    keep = snapshot_history_keep()
    if keep is None:
        return messages  # trimming disabled (the default) — full history untouched

    snapshot_locs: list[tuple[tuple[int, int], ToolReturnPart]] = []
    locator_locs: list[tuple[int, int]] = []
    for msg_idx, msg in enumerate(messages):
        if not isinstance(msg, ModelRequest):
            continue
        for part_idx, part in enumerate(msg.parts):
            if not isinstance(part, ToolReturnPart):
                continue
            if part.tool_name in _PROTECTED_TOOLS:
                locator_locs.append((msg_idx, part_idx))
            elif _is_snapshot_bearing(part):
                snapshot_locs.append(((msg_idx, part_idx), part))

    keep_set: set[tuple[int, int]] = (
        {loc for loc, _ in snapshot_locs[-keep:]} if keep > 0 else set()
    )
    if anchor_snapshots_enabled():
        keep_set |= _anchor_locations(snapshot_locs, locator_locs)

    stale = {loc for loc, _ in snapshot_locs if loc not in keep_set}
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


def _anchor_locations(
    snapshot_locs: list[tuple[tuple[int, int], ToolReturnPart]],
    locator_locs: list[tuple[int, int]],
) -> set[tuple[int, int]]:
    """Locations of anchor snapshots: latest per ``(page URL, dialog-open?)``.

    An anchor is the nearest snapshot-bearing return *preceding* each
    ``browser_generate_locator`` call — the page state the agent was looking at when
    it captured the locator. Later anchors for the same key replace earlier ones, so
    a 6-field fill→capture cycle on one page keeps one snapshot, not six; a snapshot
    without a parseable URL gets a unique key (kept individually, fail-open).
    """
    anchors: dict[object, tuple[int, int]] = {}
    snap_i = 0
    last_preceding: tuple[tuple[int, int], ToolReturnPart] | None = None
    for locator_loc in locator_locs:
        while snap_i < len(snapshot_locs) and snapshot_locs[snap_i][0] < locator_loc:
            last_preceding = snapshot_locs[snap_i]
            snap_i += 1
        if last_preceding is None:
            continue  # capture before any snapshot — nothing to anchor
        loc, part = last_preceding
        text = _content_text(part.content) or ""
        url_match = _PAGE_URL_RE.search(text)
        key: object = (
            (url_match.group(1), bool(_DIALOG_RE.search(text))) if url_match else loc
        )
        anchors[key] = loc

    if len(anchors) > _ANCHOR_TRIPWIRE:
        logger.warning(
            "Snapshot trimmer is retaining %d anchor snapshots (> %d) — unusually "
            "many distinct pages/modals for one run; check the flow or disable "
            "anchors via ANCHOR_SNAPSHOTS=off.",
            len(anchors),
            _ANCHOR_TRIPWIRE,
        )
    return set(anchors.values())


def _is_snapshot_bearing(part: ToolReturnPart) -> bool:
    """True for a browser tool return that still carries a full page snapshot."""
    if part.tool_name in _PROTECTED_TOOLS or not part.tool_name.startswith("browser_"):
        return False
    text = _content_text(part.content)
    return text is not None and _SNAPSHOT_MARKER in text and _STUB not in text


def _truncate(part: ToolReturnPart) -> ToolReturnPart:
    """Replace the snapshot section with a stub, keeping the action confirmation.

    The text before the snapshot marker (the "Ran Playwright code …" / "Page URL: …"
    header) survives, so the trail of what the agent did — and where — stays
    readable even after the heavy YAML is dropped.
    """
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
