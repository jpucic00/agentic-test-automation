"""End-to-end pipeline: Xray → Plan → Generate → Run → (Heal) → GitLab MR.

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
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil

from .agents.generator import generate_test
from .agents.healer import heal_test
from .agents.planner import plan_test_case
from .config import Config, load_config
from .gitlab_client import GitLabClient
from .models import GeneratedTest, TestPlan
from .test_runner import run_test
from .xray_client import XrayClient

logger = logging.getLogger(__name__)

# Default heal cap; override per run via the MAX_HEAL_ATTEMPTS env var (read after
# load_config() so a value in .env is honored) or the process_test_case argument.
MAX_HEAL_ATTEMPTS = 2


async def process_test_case(issue_key: str, *, max_heal_attempts: int | None = None) -> dict:
    """Run the full pipeline for one Jira/Xray issue key. Returns a result summary."""
    config = load_config()
    if max_heal_attempts is None:
        max_heal_attempts = _resolve_max_heal_attempts()

    _clear_snapshots_dir(config)

    logger.info("[%s] Fetching from Xray", issue_key)
    xray = XrayClient(config)
    test_case = xray.fetch(issue_key)
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
            test = await generate_test(
                config,
                plan,
                previous_code=test.code,
                error_text=result.error_message or result.stderr[:2000],
            )
            logger.info("[%s] Re-running regenerated test", issue_key)
            result = await run_test(config, test)
        except Exception as exc:
            # Regeneration is best-effort: on a Generator/gateway crash keep the
            # original failure and let the normal heal/MR path handle it.
            logger.warning("[%s] Generator retry failed: %s", issue_key, exc)

    heal_summaries: list[str] = []
    heal_attempts = 0
    while result.status != "passed" and heal_attempts < max_heal_attempts:
        heal_attempts += 1
        logger.info(
            "[%s] Test %s — healing (attempt %d/%d)",
            issue_key, result.status, heal_attempts, max_heal_attempts,
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
            )
        except Exception as exc:
            # An agent/MCP failure (e.g. "browser_click exceeded max retries") must not
            # discard the run — stop healing and fall through to open the MR with the best
            # test so far, so a human still gets something to review.
            logger.warning("[%s] Heal attempt %d aborted: %s", issue_key, heal_attempts, exc)
            heal_summaries.append(f"(attempt {heal_attempts} aborted before completing: {exc})")
            break
        heal_summaries.append(healed.changes_summary)
        test = GeneratedTest(
            file_name=healed.file_name, code=healed.code, description=test.description
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
    try:
        gitlab_client = GitLabClient(config)
        mr_url = gitlab_client.open_mr(
            test,
            plan,
            issue_key,
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


def _resolve_max_heal_attempts() -> int:
    raw = os.environ.get("MAX_HEAL_ATTEMPTS")
    if raw is None:
        return MAX_HEAL_ATTEMPTS
    try:
        return max(0, int(raw))
    except ValueError:
        return MAX_HEAL_ATTEMPTS


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the AI test-generation pipeline for one Jira/Xray test case."
    )
    parser.add_argument("issue_key", help="Jira issue key, e.g. QA-1234")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    result = asyncio.run(process_test_case(args.issue_key))
    print("\n=== Result ===")
    for key, value in result.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
