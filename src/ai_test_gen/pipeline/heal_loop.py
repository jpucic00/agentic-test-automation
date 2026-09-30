"""Heal-loop bookkeeping: the crashed-attempt cap, stop verdicts, failure fingerprints, and the
per-iteration artifact names and MR commit messages.

The loop itself stays in the orchestrator; these are the pure helpers it decides with. The heal
budget itself is ``config.max_heal_attempts`` (``MAX_HEAL_ATTEMPTS``, see ``core/config.py``).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..browser.runner import classify_failure
from ..core.models import TestRunResult

# A heal attempt that CRASHES (agent/gateway/MCP exception) consumes its attempt but does NOT
# end healing — each attempt builds a fresh Healer + browser, so the next one starts clean.
# Only this many CONSECUTIVE crashed attempts stop the loop early: back-to-back crashes mean
# something environmental (gateway down, browser broken) that more attempts won't heal. Without
# this, one crashed attempt used to abandon the entire remaining heal budget.
MAX_CONSECUTIVE_ABORTED_HEALS = 2

# Verdict recorded (run summary + MR description) when a completed heal returns the code
# unchanged — healer.md's signal for a genuine app bug or a spec-vs-app divergence.
NO_FIX_VERDICT = "Healer found no fix — probable app bug or spec divergence"


def iteration_file_name(base_file_name: str, label: str) -> str:
    """Sibling filename for one pipeline iteration, e.g. ``QA-1.healer-attempt-1.spec.ts``.

    The first generated test keeps ``base_file_name``; every later iteration (the
    compile-retry regeneration, each heal attempt) gets its own file so no iteration
    overwrites another and the full history stays on disk for inspection. The ``label`` is
    inserted before the ``.spec.ts`` / ``.test.ts`` compound suffix when present, else
    before the final extension.
    """
    name = Path(base_file_name).name
    for compound in (".spec.ts", ".test.ts"):
        if name.endswith(compound):
            return f"{name[: -len(compound)]}.{label}{compound}"
    stem, dot, ext = name.rpartition(".")
    return f"{stem}.{label}.{ext}" if dot else f"{name}.{label}"


def commit_message(issue_key: str, label: str, detail: str | None = None) -> str:
    """Commit message for one MR revision: a short subject, full ``detail`` in the body.

    GitLab shows the subject in the commit list, so each attempt is identifiable at a
    glance (``[AI] QA-1: heal attempt 2``); the Healer's ``changes_summary`` — which can be
    long or multi-line — goes in the commit body where it doesn't clutter that list.
    """
    subject = f"[AI] {issue_key}: {label}"
    detail = (detail or "").strip()
    return f"{subject}\n\n{detail}" if detail else subject


def failure_signature(result: TestRunResult, code: str = "") -> str:
    """A selector-AGNOSTIC fingerprint of a run failure, used to detect a recurring failure.

    Keys on the failing test title + the enclosing ``test.step`` title + the failure KIND
    (``classify_failure``: locator / assertion / navigation / other). Deliberately ignores the
    specific locator text so that a heal which swaps the selector but STILL fails the same way
    is recognized as the SAME failure recurring. The step title (not the line number, which
    shifts when a heal adds or removes a step) keeps a failure that moved on to a later step
    from counting as a repeat. For ``other``, a digit-stripped head of the message is used so
    timeouts/line numbers don't fragment otherwise-identical failures.
    """
    kind = classify_failure(result.error_message)
    if kind == "other":
        msg = (result.error_message or "").lower()
        head = re.sub(r"\s+", " ", re.sub(r"\d+", "", msg)).strip()[:80]
        kind = head or "unknown"
    test = (result.failed_test or "").strip().lower()
    return f"{test}|{failing_step(code, result.error_line)}|{kind}"


_STEP_TITLE_RE = re.compile(r"""test\.step\(\s*(['"`])(.*?)\1""")


def failing_step(code: str, error_line: int | None) -> str:
    """Title of the last ``test.step('…')`` opened at or before ``error_line`` ("" if none)."""
    if not error_line:
        return ""
    for line in reversed(code.splitlines()[:error_line]):
        match = _STEP_TITLE_RE.search(line)
        if match:
            return match.group(2).strip().lower()
    return ""


def normalized_code(code: str) -> str:
    """``code`` with line endings and trailing whitespace normalized, for no-op detection."""
    return "\n".join(line.rstrip() for line in code.splitlines()).rstrip()


def consecutive_repeats(history: list[str], signature: str) -> int:
    """Count how many trailing entries of ``history`` equal ``signature`` (0 if none/new).

    This is the escalation level handed to the Healer: 0 = first time we've seen this failure
    (heal normally); >= 1 = the failure persisted across that many prior attempts (escalate the
    locator kind down the ladder).
    """
    count = 0
    for prev in reversed(history):
        if prev == signature:
            count += 1
        else:
            break
    return count
