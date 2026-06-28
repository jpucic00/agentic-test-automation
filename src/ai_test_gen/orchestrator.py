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
- **Per-iteration artifacts + per-attempt MR commits.** The first generated spec keeps its
  filename; the compile retry and each heal attempt are written to their own sibling files
  (``<name>.healer-attempt-N.spec.ts``) so no iteration overwrites another and the full
  history stays on disk. The MR then commits one revision per attempt (initial → optional
  regen → each heal) to a single committed file path, so a reviewer can diff one attempt
  against the next in GitLab's commit view.
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
from datetime import datetime
from pathlib import Path

from .agents.generator import generate_test
from .agents.healer import heal_test
from .agents.planner import plan_test_case
from .config import PROJECT_ROOT, Config, load_config
from .gitlab_client import GitLabClient, TestRevision
from .local_testcases import load_local_test_case
from .models import GeneratedTest, ManualTestCase, TestPlan, TestRunResult
from .test_runner import run_test
from .xray_client import XrayClient

logger = logging.getLogger(__name__)

# Default heal cap; override per run via the MAX_HEAL_ATTEMPTS env var (read after
# load_config() so a value in .env is honored) or the process_test_case argument.
# 3 (was 2) gives the locator-kind escalation room to descend the resilience ladder: a
# persistently-failing step needs one attempt to confirm the failure recurs and another to
# escalate to a different locator kind (e.g. roll a hallucinated id over to a verified XPath).
MAX_HEAL_ATTEMPTS = 3


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
    config = load_config()
    if max_heal_attempts is None:
        max_heal_attempts = _resolve_max_heal_attempts()

    _clear_snapshots_dir(config)

    logger.info("[%s] Loading test case (source=%s)", issue_key, config.testcase_source)
    test_case = _load_test_case(config, issue_key)
    (config.plans_dir / f"{issue_key}-input.json").write_text(
        test_case.model_dump_json(indent=2)
    )

    logger.info("[%s] Planning", issue_key)
    try:
        plan = await plan_test_case(config, test_case)
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
            return {
                "issue_key": issue_key,
                "status": "refused",
                "heal_attempts": 0,
                "mr_url": None,
                "notes": plan.notes,
            }

        logger.info("[%s] Generating Playwright code", issue_key)
        test = await generate_test(config, plan)
    except Exception as exc:
        # No plan/test means nothing to run or open an MR for. A Planner/Generator crash
        # (e.g. an MCP tool exceeding its retry budget) must fail cleanly, not dump a stack
        # trace — there's no partial artifact to salvage here.
        logger.error("[%s] Planning/generation failed: %s", issue_key, exc)
        return {
            "issue_key": issue_key,
            "status": "error",
            "heal_attempts": 0,
            "mr_url": None,
            "error": f"Planning/generation failed: {exc}",
        }

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
    result = await run_test(config, test)

    # A failure with did_run=False is a compile/collection error — the spec never
    # executed, so there is nothing for the browser-driving Healer to inspect. Give the
    # Generator ONE retry with its own output + the error; a persistent compile error
    # still falls through to the heal loop / MR so a human always gets something.
    if result.status == "failed" and not result.did_run:
        logger.info(
            "[%s] Test never ran (no JSON report — compile/collection error); "
            "regenerating once via the Generator",
            issue_key,
        )
        try:
            regenerated = await generate_test(
                config,
                plan,
                previous_code=test.code,
                error_text=result.error_message or result.stderr[:2000],
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
            result = await run_test(config, test)
        except Exception as exc:
            # Regeneration is best-effort: on a Generator/gateway crash keep the
            # original failure and let the normal heal/MR path handle it.
            logger.warning("[%s] Generator retry failed: %s", issue_key, exc)

    heal_summaries: list[str] = []
    failure_signatures: list[str] = []
    heal_attempts = 0
    while result.status != "passed" and heal_attempts < max_heal_attempts:
        heal_attempts += 1
        # How many times in a row this exact failure has already recurred. When the SAME
        # step keeps failing the same way, re-trying the same locator kind isn't working —
        # tell the Healer to escalate down the resilience ladder (→ CSS → XPath) instead of
        # re-emitting (and often re-hallucinating) the locator that already failed.
        signature = _failure_signature(result)
        escalation = _consecutive_repeats(failure_signatures, signature)
        failure_signatures.append(signature)
        escalation_note = (
            f" — same failure recurred {escalation}×, escalating locator kind" if escalation else ""
        )
        logger.info(
            "[%s] Test %s — healing (attempt %d/%d)%s",
            issue_key, result.status, heal_attempts, max_heal_attempts, escalation_note,
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
                heal_history=list(heal_summaries),
                locator_escalation=escalation,
            )
        except Exception as exc:
            # An agent/MCP failure (e.g. "browser_click exceeded max retries") must not
            # discard the run — stop healing and fall through to open the MR with the best
            # test so far, so a human still gets something to review.
            logger.warning("[%s] Heal attempt %d aborted: %s", issue_key, heal_attempts, exc)
            heal_summaries.append(f"(attempt {heal_attempts} aborted before completing: {exc})")
            break
        heal_summaries.append(healed.changes_summary)
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
        result = await run_test(config, test)

    if result.status != "passed":
        logger.warning(
            "[%s] Still %s after %d heal attempt(s); opening MR for review anyway",
            issue_key, result.status, heal_attempts,
        )

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
        if result.trace_path:
            summary["trace_path"] = result.trace_path
        return summary

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
            trace_path=result.trace_path,
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
        return {
            "issue_key": issue_key,
            "status": result.status,
            "heal_attempts": heal_attempts,
            "mr_url": None,
            "error": f"MR creation failed: {exc}",
        }
    logger.info("[%s] MR opened: %s", issue_key, mr_url)

    summary = {
        "issue_key": issue_key,
        "status": result.status,
        "heal_attempts": heal_attempts,
        "mr_url": mr_url,
    }
    if result.trace_path:
        summary["trace_path"] = result.trace_path
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


def _failure_signature(result: TestRunResult) -> str:
    """A selector-AGNOSTIC fingerprint of a run failure, used to detect a recurring failure.

    Keys on the failing test title + a coarse failure *category* (timeout / strict-mode /
    navigation / assertion / other). Deliberately ignores the specific locator text so that a
    heal which swaps the selector but STILL fails the same way (e.g. timeout → timeout) is
    recognized as the SAME failure recurring — which is what should trigger locator-kind
    escalation. Digits are stripped from the fallback head so timeouts/line numbers don't
    fragment otherwise-identical failures.
    """
    msg = (result.error_message or "").lower()
    test = (result.failed_test or "").strip().lower()
    if "strict mode" in msg or ("resolved" in msg and "element" in msg):
        category = "strict-mode"
    elif "timeout" in msg or "exceeded" in msg or "waiting for" in msg:
        category = "timeout"
    elif "err_" in msg or "net::" in msg or "tohaveurl" in msg:
        category = "navigation"
    elif "expect(" in msg or "assertion" in msg or "to be" in msg:
        category = "assertion"
    else:
        head = re.sub(r"\s+", " ", re.sub(r"\d+", "", msg)).strip()[:80]
        category = head or "unknown"
    return f"{test}|{category}"


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
        print(f"  {key}: {value}")
    print(f"\nFull DEBUG log: {log_path}")


if __name__ == "__main__":
    main()
