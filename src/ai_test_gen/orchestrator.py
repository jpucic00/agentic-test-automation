"""End-to-end pipeline: test case (Xray or local JSON) → Plan → Generate → Run → (Heal) → GitLab MR.

Phase 1.D — task ``kd2pvze`` (AI_TEST_GENERATION_GUIDE.md §3.13). Wires the three
agents and the two integrations into a single run for one Jira/Xray test case.

Improvements over the guide's template:

- **Context-driven auth, no ``storage_state``.** The agents log in live from the
  ``project_context.md`` dummy creds; the generated test embeds them as literals — so
  nothing here resolves or passes a saved session.
- **``context_hash`` in the saved plan** (sha256 of ``project_context.md`` +
  ``project_map.md``): a later audit can tell a plan was generated against stale context.
- **Snapshot auto-clean.** ``output/snapshots/`` (the Playwright MCP snapshot/png output,
  regenerated every run) is emptied at the start of each run so it doesn't accumulate.
- **Heal transparency.** Every Healer ``changes_summary`` is collected and rendered into
  the MR; an MR is opened even when healing is exhausted, so a human always reviews.
- **Heal resilience.** A heal attempt that crashes (agent/gateway/MCP exception) consumes
  its attempt and healing continues with a fresh Healer; only
  ``MAX_CONSECUTIVE_ABORTED_HEALS`` back-to-back crashes end the loop early.
- **Heal stop rules.** Only a ``failed`` run is healed (``error`` = infrastructure/blocked,
  surfaced as the ``heal_verdict``); a heal that returns the code unchanged ends the loop with
  the "no fix — probable app bug or spec divergence" verdict instead of re-running it.
- **Per-iteration artifacts + per-attempt MR commits.** The first generated spec keeps its
  filename; the compile retry and each heal attempt are written to their own sibling files
  (``<name>.healer-attempt-N.spec.ts``) so no iteration overwrites another and the full
  history stays on disk. The MR then commits one revision per attempt (initial → optional
  regen → each heal) to a single committed file path, so a reviewer can diff one attempt
  against the next in GitLab's commit view.
- **Multi-environment runs.** Plan → generate → run → heal happen once, against the primary
  (first) ``STAGING_BASE_URL`` entry. The final spec then runs once on every other configured
  environment — no Planner, no healing: a failure there is a real environment difference and
  is reported per environment (summary, run log, MR description), not "fixed".
- **Navigation allow-list.** Every run is pre-checked for absolute navigation outside the
  allowed hosts (``test_runner``); a blocked run is an error surfaced for review, never healed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import textwrap
import time
from datetime import datetime
from pathlib import Path

from .agents._vision_aid import VisionStats
from .agents.generator import generate_test
from .agents.healer import heal_test
from .agents.planner import plan_test_case
from .config import PROJECT_ROOT, Config, load_config
from .gitlab_client import GitLabClient, TestRevision
from .local_testcases import load_local_test_case
from .models import (
    EnvironmentRunResult,
    GeneratedTest,
    ManualTestCase,
    TestPlan,
    TestRunResult,
)
from .test_runner import classify_failure, run_test
from .usage import UsageLog, describe, format_usage
from .xray_client import XrayClient

logger = logging.getLogger(__name__)

# Default heal cap; override per run via the MAX_HEAL_ATTEMPTS env var (read after
# load_config() so a value in .env is honored) or the process_test_case argument.
# 3 (was 2) gives the locator-kind escalation room to descend the resilience ladder: a
# persistently-failing step needs one attempt to confirm the failure recurs and another to
# escalate to a different locator kind (e.g. roll a hallucinated id over to a verified XPath).
MAX_HEAL_ATTEMPTS = 3

# A heal attempt that CRASHES (agent/gateway/MCP exception) consumes its attempt but does NOT
# end healing — each attempt builds a fresh Healer + browser, so the next one starts clean.
# Only this many CONSECUTIVE crashed attempts stop the loop early: back-to-back crashes mean
# something environmental (gateway down, browser broken) that more attempts won't heal. Without
# this, one crashed attempt used to abandon the entire remaining MAX_HEAL_ATTEMPTS budget.
MAX_CONSECUTIVE_ABORTED_HEALS = 2

# Verdict recorded (run summary + MR description) when a completed heal returns the code
# unchanged — healer.md's signal for a genuine app bug or a spec-vs-app divergence.
NO_FIX_VERDICT = "Healer found no fix — probable app bug or spec divergence"


def _load_test_case(config: Config, issue_key: str) -> ManualTestCase:
    """Fetch one test case from the configured source.

    ``TESTCASE_SOURCE=local`` reads a raw-Xray-shaped JSON file from ``LOCAL_TESTCASE_DIR``
    (no Jira needed); the default ``xray`` source fetches it live from Jira/Xray. Both yield
    the same ``ManualTestCase``, so everything downstream is identical.
    """
    if config.testcase_source == "local":
        return load_local_test_case(config, issue_key)
    return XrayClient(config).fetch(issue_key)


async def process_test_case(issue_key: str, *, max_heal_attempts: int | None = None) -> dict:
    """Run the full pipeline for one Jira/Xray issue key. Returns a result summary."""
    started = time.monotonic()
    config = load_config()
    if max_heal_attempts is None:
        max_heal_attempts = _resolve_max_heal_attempts()

    _clear_snapshots_dir(config)
    # Vision Aid counts per agent (all heal attempts pooled under "Healer"); reported in the
    # summary when vision is on, so a run whose screenshots never arrived is visible at a glance.
    vision = {"Planner": VisionStats(), "Healer": VisionStats()}
    # Model requests, tokens and wall time per agent run (each heal attempt and each agent's
    # Vision Aid calls on their own line); every summary below carries it as "usage".
    usage = UsageLog()
    logger.info(
        "[%s] Environments (primary first): %s; extra allowed hosts: %s",
        issue_key,
        ", ".join(config.staging_base_urls),
        ", ".join(config.staging_extra_urls) or "(none)",
    )

    logger.info("[%s] Loading test case (source=%s)", issue_key, config.testcase_source)
    test_case = _load_test_case(config, issue_key)
    (config.plans_dir / f"{issue_key}-input.json").write_text(
        test_case.model_dump_json(indent=2)
    )

    logger.info("[%s] Planning", issue_key)
    try:
        plan = await plan_test_case(
            config, test_case, vision_stats=vision["Planner"], usage=usage
        )
        plan_json = plan_json_with_context_hash(plan, config)
        (config.plans_dir / f"{issue_key}.json").write_text(plan_json)

        if not plan.steps:
            # planner.md instructs the Planner to REFUSE unclear/unsafe cases (forbidden
            # routes, PII, production) by returning a plan with no steps and the reason
            # in notes. Nothing runnable exists — generating, running, healing, or
            # opening an MR for a stepless test would only burn heal attempts on junk.
            # The plan JSON is already on disk for audit; surface the refusal instead.
            logger.warning(
                "[%s] Planner returned no steps (refusal) — stopping. Notes: %s",
                issue_key,
                plan.notes or "(none)",
            )
            return _with_usage(
                _with_vision(
                    {
                        "issue_key": issue_key,
                        "status": "refused",
                        "heal_attempts": 0,
                        "mr_url": None,
                        "notes": plan.notes,
                    },
                    config,
                    vision,
                ),
                usage,
                started,
            )

        logger.info("[%s] Generating Playwright code", issue_key)
        test = await generate_test(config, plan, usage=usage)
    except Exception as exc:
        # No plan/test means nothing to run or open an MR for. A Planner/Generator crash
        # (e.g. an MCP tool exceeding its retry budget) must fail cleanly, not dump a stack
        # trace — there's no partial artifact to salvage here.
        logger.error("[%s] Planning/generation failed: %s", issue_key, exc)
        return _with_usage(
            _with_vision(
                {
                    "issue_key": issue_key,
                    "status": "error",
                    "heal_attempts": 0,
                    "mr_url": None,
                    "error": f"Planning/generation failed: {exc}",
                },
                config,
                vision,
            ),
            usage,
            started,
        )

    # The Generator owns the canonical filename. Every later iteration (the compile-retry
    # regeneration, each heal attempt) is written to its OWN sibling file so nothing
    # overwrites the first iteration and the full history stays on disk. Each iteration is
    # ALSO captured as a `revisions` entry: the MR commits one per attempt under this base
    # name (see the open_mr call below), so attempt-to-attempt diffs show up in GitLab.
    base_file_name = test.file_name
    description = test.description
    revisions: list[TestRevision] = [
        TestRevision(
            message=_commit_message(issue_key, "initial generated test", description),
            code=test.code,
        )
    ]

    logger.info("[%s] Running test (attempt 1)", issue_key)
    result = await run_test(config, test, plan=plan)

    # A failure with did_run=False is a compile/collection error — the spec never
    # executed, so there is nothing for the browser-driving Healer to inspect. Give the
    # Generator ONE retry with its own output + the error; a persistent compile error
    # still falls through to the heal loop / MR so a human always gets something.
    if result.status == "failed" and not result.did_run:
        logger.info(
            "[%s] Test never ran (no test executed — compile/collection error); "
            "regenerating once via the Generator",
            issue_key,
        )
        try:
            regenerated = await generate_test(
                config,
                plan,
                previous_code=test.code,
                error_text=result.error_message or result.stderr[:2000],
                usage=usage,
            )
            # Keep the failed first attempt on disk; the regeneration is its own artifact.
            test = GeneratedTest(
                file_name=_iteration_file_name(base_file_name, "regen"),
                code=regenerated.code,
                description=description,
            )
            revisions.append(
                TestRevision(
                    message=_commit_message(
                        issue_key, "regenerate after compile/collection error"
                    ),
                    code=regenerated.code,
                )
            )
            logger.info("[%s] Re-running regenerated test", issue_key)
            result = await run_test(config, test, plan=plan)
        except Exception as exc:
            # Regeneration is best-effort: on a Generator/gateway crash keep the
            # original failure and let the normal heal/MR path handle it.
            logger.warning("[%s] Generator retry failed: %s", issue_key, exc)

    # heal_summaries feeds the MR (every attempt, crashes included); heal_history feeds the
    # Healer and holds only COMPLETED heals — its block says "the code already contains these
    # changes", which a crashed attempt never made.
    heal_summaries: list[str] = []
    heal_history: list[str] = []
    # Signatures of the failures that COMPLETED heals were given (appended after the re-run).
    # A crashed attempt adds none, so it can't fake a "the failure persisted" escalation.
    failure_signatures: list[str] = []
    heal_verdict: str | None = None
    heal_attempts = 0
    consecutive_aborts = 0
    # Only a test failure is healable. status "error" — a run blocked by the pre-run navigation
    # check, Playwright failing to launch, the whole-run timeout — is an infrastructure/safety
    # stop the Healer cannot fix; it is surfaced in the MR instead (see below).
    while result.status == "failed" and heal_attempts < max_heal_attempts:
        heal_attempts += 1
        # How many completed heals in a row this same failure (test + step + kind) survived.
        # The Healer turns it into kind-specific guidance: descend the locator ladder for a
        # locator failure, suspect a spec-vs-app divergence for an assertion failure.
        signature = _failure_signature(result, test.code)
        repeats = _consecutive_repeats(failure_signatures, signature)
        repeat_note = (
            f" — same {classify_failure(result.error_message)} failure survived "
            f"{repeats} heal(s)"
            if repeats
            else ""
        )
        logger.info(
            "[%s] Test %s — healing (attempt %d/%d)%s",
            issue_key, result.status, heal_attempts, max_heal_attempts, repeat_note,
        )
        try:
            # Pass a snapshot of the summaries so far: the Healer rewrites the whole
            # file, and without the history attempt 2 can silently undo attempt 1.
            healed = await heal_test(
                config,
                test,
                result,
                plan=plan,
                test_case=test_case,
                heal_history=list(heal_history),
                failure_repeats=repeats,
                vision_stats=vision["Healer"],
                usage=usage,
                attempt=heal_attempts,
            )
        except Exception as exc:
            # An agent/MCP failure (e.g. "browser_click exceeded max retries") must not discard
            # the run — NOR the remaining heal budget: the attempt is consumed and healing
            # CONTINUES with a fresh Healer (each attempt already builds its own agent+browser).
            # Only MAX_CONSECUTIVE_ABORTED_HEALS back-to-back crashes stop the loop early (that
            # pattern is environmental, not healable); either way the MR below still opens with
            # the best test so far, so a human always gets something to review.
            consecutive_aborts += 1
            heal_summaries.append(f"(attempt {heal_attempts} aborted before completing: {exc})")
            if consecutive_aborts >= MAX_CONSECUTIVE_ABORTED_HEALS:
                logger.warning(
                    "[%s] Heal attempt %d aborted (%d in a row) — stopping healing: %s",
                    issue_key, heal_attempts, consecutive_aborts, exc,
                )
                break
            logger.warning(
                "[%s] Heal attempt %d aborted (continuing, %d/%d consecutive): %s",
                issue_key, heal_attempts, consecutive_aborts,
                MAX_CONSECUTIVE_ABORTED_HEALS, exc,
            )
            continue
        consecutive_aborts = 0
        heal_summaries.append(healed.changes_summary)
        if _normalized_code(healed.code) == _normalized_code(test.code):
            # healer.md: on a genuine app bug / spec divergence the Healer returns the code
            # unchanged. Re-running identical code can only fail the same way, so stop here —
            # no no-op attempt file, no empty MR revision — and flag the verdict for review.
            heal_verdict = f"{NO_FIX_VERDICT}. Healer: {healed.changes_summary}"
            logger.warning(
                "[%s] Heal attempt %d returned the code unchanged — stopping healing: %s",
                issue_key, heal_attempts, heal_verdict,
            )
            break
        heal_history.append(healed.changes_summary)
        # Each heal lands in its OWN file (<name>.healer-attempt-N.spec.ts). The Healer's
        # returned file_name is deliberately ignored so an attempt can never overwrite an
        # earlier iteration; the MR commits this code under base_file_name as its own commit.
        test = GeneratedTest(
            file_name=_iteration_file_name(base_file_name, f"healer-attempt-{heal_attempts}"),
            code=healed.code,
            description=description,
        )
        revisions.append(
            TestRevision(
                message=_commit_message(
                    issue_key, f"heal attempt {heal_attempts}", healed.changes_summary
                ),
                code=healed.code,
            )
        )
        logger.info("[%s] Re-running test", issue_key)
        result = await run_test(config, test, plan=plan)
        failure_signatures.append(signature)

    if result.status == "error":
        reason = (
            "run blocked by the navigation allow-list" if result.blocked else "infrastructure error"
        )
        heal_verdict = f"Not healed — {reason}, not a test failure: {result.error_message}"
        logger.error("[%s] %s", issue_key, heal_verdict)
    elif result.status != "passed" and heal_verdict is None:
        logger.warning(
            "[%s] Still %s after %d heal attempt(s); opening MR for review anyway",
            issue_key, result.status, heal_attempts,
        )

    env_results = await _run_other_environments(config, test, plan, issue_key, result)

    if not config.gitlab_enabled:
        logger.info(
            "[%s] GITLAB_ENABLED=false — skipping MR. Test saved at %s (plan: %s)",
            issue_key,
            config.tests_dir / test.file_name,
            config.plans_dir / f"{issue_key}.json",
        )
        summary = {
            "issue_key": issue_key,
            "status": result.status,
            "heal_attempts": heal_attempts,
            "mr_url": None,
        }
        if heal_verdict:
            summary["heal_verdict"] = heal_verdict
        if result.trace_path:
            summary["trace_path"] = result.trace_path
        return _with_usage(
            _with_vision(_with_environments(summary, env_results), config, vision),
            usage,
            started,
        )

    logger.info("[%s] Opening GitLab MR", issue_key)
    # The MR carries ONE file path (the original first-iteration filename) but one commit
    # per attempt (the `revisions` list), so a reviewer can diff one attempt against the
    # next. The per-attempt artifacts (<name>.healer-attempt-N.spec.ts) stay local too.
    mr_test = GeneratedTest(
        file_name=base_file_name, code=test.code, description=description
    )
    try:
        gitlab_client = GitLabClient(config)
        mr_url = gitlab_client.open_mr(
            mr_test,
            plan,
            issue_key,
            revisions=revisions,
            plan_json=plan_json,
            heal_summaries=heal_summaries,
            heal_attempts=heal_attempts,
            final_status=result.status,
            heal_verdict=heal_verdict,
            trace_path=result.trace_path,
            environment_results=env_results,
        )
    except Exception as exc:
        # GitLab/auth/network failure must not discard the run: the generated test and plan
        # are already on disk — point the user at them instead of crashing.
        logger.error("[%s] Could not open MR: %s", issue_key, exc)
        logger.error(
            "[%s] Test saved at %s (plan: %s) — open an MR manually if needed.",
            issue_key,
            config.tests_dir / test.file_name,
            config.plans_dir / f"{issue_key}.json",
        )
        summary = {
            "issue_key": issue_key,
            "status": result.status,
            "heal_attempts": heal_attempts,
            "mr_url": None,
            "error": f"MR creation failed: {exc}",
        }
        if heal_verdict:
            summary["heal_verdict"] = heal_verdict
        return _with_usage(
            _with_vision(_with_environments(summary, env_results), config, vision),
            usage,
            started,
        )
    logger.info("[%s] MR opened: %s", issue_key, mr_url)

    summary = {
        "issue_key": issue_key,
        "status": result.status,
        "heal_attempts": heal_attempts,
        "mr_url": mr_url,
    }
    if heal_verdict:
        summary["heal_verdict"] = heal_verdict
    if result.trace_path:
        summary["trace_path"] = result.trace_path
    return _with_usage(
        _with_vision(_with_environments(summary, env_results), config, vision), usage, started
    )


async def _run_other_environments(
    config: Config,
    test: GeneratedTest,
    plan: TestPlan,
    issue_key: str,
    primary: TestRunResult,
) -> list[EnvironmentRunResult]:
    """Run the final spec once on every secondary environment; ``[]`` for a single env.

    No Planner and no healing here: the spec was healed against the primary environment, so
    a failure on another one is a real environment difference to surface, not to "fix".
    Each secondary run writes Playwright output to its own ``test-results/env-N`` folder so
    the primary run's trace survives. The returned list starts with the primary result.
    """
    if len(config.staging_base_urls) < 2:
        return []
    results = [_environment_result(config.staging_base_url, primary, primary=True)]
    total = len(config.staging_base_urls)
    for index, base_url in enumerate(config.staging_base_urls[1:], start=2):
        logger.info(
            "[%s] Running final spec on environment %d/%d: %s", issue_key, index, total, base_url
        )
        run = await run_test(
            config, test, plan=plan, base_url=base_url, results_dir=f"test-results/env-{index}"
        )
        env_result = _environment_result(base_url, run, primary=False)
        log = logger.info if env_result.status == "passed" else logger.warning
        log(
            "[%s] Environment %s: %s%s",
            issue_key, base_url, env_result.status,
            f" — {env_result.error}" if env_result.error else "",
        )
        results.append(env_result)
    return results


def _environment_result(
    base_url: str, result: TestRunResult, *, primary: bool
) -> EnvironmentRunResult:
    """Per-environment summary: status plus the error's first line (capped) when not passed."""
    error = None
    if result.status != "passed":
        lines = (result.error_message or result.stderr or "").strip().splitlines()
        error = (lines[0] if lines else "(no error output)")[:300]
    return EnvironmentRunResult(
        base_url=base_url, primary=primary, status=result.status, error=error
    )


def _with_environments(summary: dict, env_results: list[EnvironmentRunResult]) -> dict:
    """Add the per-environment results to a run summary (multi-environment runs only)."""
    if env_results:
        summary["environments"] = [r.model_dump() for r in env_results]
    return summary


def _with_vision(summary: dict, config: Config, vision: dict[str, VisionStats]) -> dict:
    """Add the per-agent Vision Aid line to a run summary (only when vision is enabled)."""
    if config.vision_max_calls > 0:
        line = "; ".join(f"{agent}: {stats.describe()}" for agent, stats in vision.items())
        summary["vision"] = line
        degraded = any(stats.degraded for stats in vision.values())
        logger.log(
            logging.WARNING if degraded else logging.INFO,
            "[%s] Vision Aid — %s",
            summary["issue_key"],
            line,
        )
    return summary


def _with_usage(summary: dict, usage: UsageLog, started: float) -> dict:
    """Add the per-agent usage records + run totals to a run summary; log the totals.

    ``started`` is the run's ``time.monotonic()`` start, so ``total.wall_s`` is the whole run's
    wall time (test runs and the MR included), not a sum of the agent runs.
    """
    report = usage.summary(time.monotonic() - started)
    summary["usage"] = report
    total = report["total"]
    logger.info(
        "[%s] Usage total: %s (%d agent run record(s))\n%s",
        summary["issue_key"],
        describe(total["requests"], total["input_tokens"], total["output_tokens"], total["wall_s"]),
        len(report["agents"]),
        format_usage(report),
    )
    return summary


def plan_json_with_context_hash(plan: TestPlan, config: Config) -> str:
    """Serialize ``plan`` to JSON with an added ``context_hash`` of the context files.

    Kept separate from the ``TestPlan`` schema so the model is never asked to fill the
    hash; the orchestrator writes the same string locally and into the GitLab commit.
    """
    data = plan.model_dump()
    data["context_hash"] = _context_hash(config)
    return json.dumps(data, indent=2)


def _context_hash(config: Config) -> str:
    """sha256 over the two human-authored context files (missing file → empty)."""
    digest = hashlib.sha256()
    for path in (config.project_context_path, config.project_map_path):
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            digest.update(b"")
    return digest.hexdigest()


def _clear_snapshots_dir(config: Config) -> None:
    """Empty ``output/snapshots/`` (regenerated MCP snapshot/png output) before a run,
    keeping the directory and its tracked ``.gitkeep``."""
    snapshots = config.snapshots_dir
    snapshots.mkdir(parents=True, exist_ok=True)
    for child in snapshots.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _iteration_file_name(base_file_name: str, label: str) -> str:
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


def _commit_message(issue_key: str, label: str, detail: str | None = None) -> str:
    """Commit message for one MR revision: a short subject, full ``detail`` in the body.

    GitLab shows the subject in the commit list, so each attempt is identifiable at a
    glance (``[AI] QA-1: heal attempt 2``); the Healer's ``changes_summary`` — which can be
    long or multi-line — goes in the commit body where it doesn't clutter that list.
    """
    subject = f"[AI] {issue_key}: {label}"
    detail = (detail or "").strip()
    return f"{subject}\n\n{detail}" if detail else subject


def _failure_signature(result: TestRunResult, code: str = "") -> str:
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
    return f"{test}|{_failing_step(code, result.error_line)}|{kind}"


_STEP_TITLE_RE = re.compile(r"""test\.step\(\s*(['"`])(.*?)\1""")


def _failing_step(code: str, error_line: int | None) -> str:
    """Title of the last ``test.step('…')`` opened at or before ``error_line`` ("" if none)."""
    if not error_line:
        return ""
    for line in reversed(code.splitlines()[:error_line]):
        match = _STEP_TITLE_RE.search(line)
        if match:
            return match.group(2).strip().lower()
    return ""


def _normalized_code(code: str) -> str:
    """``code`` with line endings and trailing whitespace normalized, for no-op detection."""
    return "\n".join(line.rstrip() for line in code.splitlines()).rstrip()


def _consecutive_repeats(history: list[str], signature: str) -> int:
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


def _resolve_max_heal_attempts() -> int:
    raw = os.environ.get("MAX_HEAL_ATTEMPTS")
    if raw is None:
        return MAX_HEAL_ATTEMPTS
    try:
        return max(0, int(raw))
    except ValueError:
        return MAX_HEAL_ATTEMPTS


class _ExcludeLoggers(logging.Filter):
    """Drop records emitted by the given logger-name prefixes.

    Used on the FILE handler only, to keep the gateway's HTTP/SDK chatter — and any request
    headers that carry the API key — out of the on-disk log. The console handler is left
    unfiltered, so ``--verbose`` still streams that activity live in the terminal.
    """

    def __init__(self, prefixes: tuple[str, ...]) -> None:
        super().__init__()
        self._prefixes = prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(self._prefixes)


def _configure_logging(issue_key: str, *, verbose: bool) -> Path:
    """Log to the console AND to a persistent per-run file under ``output/runs/``.

    Console honors ``--verbose`` (DEBUG vs INFO). The file captures **INFO by default** —
    enough to diagnose a failed run, because the pipeline logs every step and every failure
    (the Planner/Healer exception text, including the gateway's error body, is logged at
    WARNING/ERROR). The file deliberately does NOT replay the agents' conversations: the large
    accessibility snapshots stay in the agents' in-memory history and nothing logs them, so the
    file stays small and easy to read/share. Set ``RUN_LOG_LEVEL=DEBUG`` for a deeper dive.
    The console is unfiltered, so ``--verbose`` streams the live HTTP/agent activity as before;
    the file excludes the noisy HTTP/SDK loggers (httpx/openai) so it stays readable and never
    records the gateway request headers (which carry the API key). Returns the log file path.
    """
    runs_dir = PROJECT_ROOT / "output" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_path = runs_dir / f"run-{issue_key}-{stamp}.log"

    level_name = os.environ.get("RUN_LOG_LEVEL", "INFO").strip().upper()
    file_level = getattr(logging, level_name, logging.INFO)
    console_level = logging.DEBUG if verbose else logging.INFO

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(fmt)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(file_level)
    file_handler.setFormatter(fmt)
    # File only: drop HTTP/SDK chatter and any header dumps that could leak the key. The console
    # keeps them, so --verbose still shows live request activity.
    file_handler.addFilter(_ExcludeLoggers(("httpx", "httpcore", "openai", "urllib3")))

    root = logging.getLogger()
    root.setLevel(min(file_level, console_level))  # don't starve either handler
    root.handlers.clear()  # drop any prior handler so there's no duplicate console line
    root.addHandler(console)
    root.addHandler(file_handler)

    return log_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the AI test-generation pipeline for one Jira/Xray test case."
    )
    parser.add_argument("issue_key", help="Jira issue key, e.g. QA-1234")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    log_path = _configure_logging(args.issue_key, verbose=args.verbose)
    logger.info("Run log: %s", log_path)

    result = asyncio.run(process_test_case(args.issue_key))
    print("\n=== Result ===")
    for key, value in result.items():
        if key != "usage":
            print(f"  {key}: {value}")
    if "usage" in result:
        print("\n=== Model usage ===")
        print(textwrap.indent(format_usage(result["usage"]), "  "))
    print(f"\nFull DEBUG log: {log_path}")


if __name__ == "__main__":
    main()
