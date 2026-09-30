"""Unit tests for the orchestrator — fully local (every agent + integration is mocked).

The agents, the runner, and the GitLab client are monkeypatched in the ``orchestrator``
namespace and the test-case sources in ``testcases``, so no network, browser, or subprocess is
touched. Coroutines are driven with ``asyncio.run`` (no pytest-asyncio). ``Test*`` models are
built via the ``models`` module so pytest does not collect them as test classes.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock, MagicMock

from pydantic_ai.usage import RunUsage

from ai_test_gen import orchestrator, testcases
from ai_test_gen.core import models
from ai_test_gen.core.usage import UsageLog
from ai_test_gen.pipeline import heal_loop


def _manual_case():
    return models.ManualTestCase(
        key="QA-1",
        title="Login",
        steps=[models.ManualStep(action="log in", expected="dashboard")],
    )


def _plan():
    return models.TestPlan(
        test_case_key="QA-1",
        title="Login",
        target_url="https://staging.example.internal",
        steps=[models.PlanStep(action="log in")],
    )


def _generated():
    return models.GeneratedTest(file_name="QA-1-login.spec.ts", code="// spec", description="login")


def _healed():
    return models.HealedTest(
        file_name="QA-1-login.spec.ts", code="// healed", changes_summary="fixed selector"
    )


def _healer():
    """Healer double whose every completed heal CHANGES the code: "// healed", "// healed 2", …

    (A heal that returns its input unchanged stops the loop, so multi-attempt tests need this.)
    """
    calls = 0

    def heal(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        suffix = "" if calls == 1 else f" {calls}"
        return models.HealedTest(
            file_name="QA-1-login.spec.ts",
            code=f"// healed{suffix}",
            changes_summary="fixed selector",
        )

    return AsyncMock(side_effect=heal)


def _result(status, did_run=True):
    return models.TestRunResult(status=status, did_run=did_run, stdout="", stderr="")


def _wire(monkeypatch, cfg, run_results):
    """Monkeypatch the whole pipeline; return the mock GitLab client."""
    cfg.plans_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(orchestrator, "load_config", lambda: cfg)

    fake_xray = MagicMock()
    fake_xray.fetch.return_value = _manual_case()
    monkeypatch.setattr(testcases, "XrayClient", MagicMock(return_value=fake_xray))

    monkeypatch.setattr(orchestrator, "plan_test_case", AsyncMock(return_value=_plan()))
    monkeypatch.setattr(orchestrator, "generate_test", AsyncMock(return_value=_generated()))
    monkeypatch.setattr(orchestrator, "run_test", AsyncMock(side_effect=list(run_results)))
    monkeypatch.setattr(orchestrator, "heal_test", _healer())

    gl = MagicMock()
    gl.open_mr.return_value = "https://gitlab/mr/1"
    monkeypatch.setattr(orchestrator, "GitLabClient", MagicMock(return_value=gl))
    return gl


def test_heals_until_pass_then_opens_mr(cfg, monkeypatch):
    gl = _wire(monkeypatch, cfg, [_result("failed"), _result("passed")])
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert out["status"] == "passed"
    assert out["heal_attempts"] == 1
    assert out["mr_url"] == "https://gitlab/mr/1"
    kwargs = gl.open_mr.call_args.kwargs
    assert kwargs["heal_attempts"] == 1
    assert kwargs["final_status"] == "passed"
    assert kwargs["heal_summaries"] == ["fixed selector"]


def test_respects_max_heal_attempts_and_opens_mr_on_failure(cfg, monkeypatch):
    gl = _wire(monkeypatch, cfg, [_result("failed")] * 5)  # never passes
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=2))
    assert out["status"] == "failed"
    assert out["heal_attempts"] == 2
    gl.open_mr.assert_called_once()
    assert gl.open_mr.call_args.kwargs["final_status"] == "failed"


def test_no_heal_when_first_run_passes(cfg, monkeypatch):
    gl = _wire(monkeypatch, cfg, [_result("passed")])
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert out["heal_attempts"] == 0
    assert gl.open_mr.call_args.kwargs["heal_summaries"] == []


def test_saved_plan_json_has_context_hash(cfg, monkeypatch):
    gl = _wire(monkeypatch, cfg, [_result("passed")])
    asyncio.run(orchestrator.process_test_case("QA-1"))
    saved = (cfg.plans_dir / "QA-1.json").read_text()
    assert "context_hash" in saved
    # The same enriched JSON is handed to the GitLab client.
    assert "context_hash" in gl.open_mr.call_args.kwargs["plan_json"]


def test_snapshots_dir_wiped_at_start_but_gitkeep_survives(cfg, monkeypatch):
    cfg.snapshots_dir.mkdir(parents=True, exist_ok=True)
    stale = cfg.snapshots_dir / "page-001.png"
    stale.write_text("stale")
    keep = cfg.snapshots_dir / ".gitkeep"
    keep.write_text("")
    _wire(monkeypatch, cfg, [_result("passed")])
    asyncio.run(orchestrator.process_test_case("QA-1"))
    assert not stale.exists()  # MCP snapshot artifacts cleared
    assert keep.exists()  # .gitkeep preserved so the folder stays tracked
    assert cfg.snapshots_dir.exists()


def test_heal_exception_still_opens_mr(cfg, monkeypatch):
    # A Healer that crashes EVERY attempt: each crash consumes an attempt, and after
    # MAX_CONSECUTIVE_ABORTED_HEALS (2) back-to-back crashes healing stops early — but the
    # run is never discarded: the MR still opens with the best test so far.
    gl = _wire(monkeypatch, cfg, [_result("failed")])
    heal = AsyncMock(side_effect=RuntimeError("browser_click exceeded max retries"))
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=15))
    assert out["status"] == "failed"
    assert out["heal_attempts"] == heal_loop.MAX_CONSECUTIVE_ABORTED_HEALS == 2
    assert heal.call_count == 2  # stopped by the consecutive-abort cap, not the budget
    assert out["mr_url"] == "https://gitlab/mr/1"
    gl.open_mr.assert_called_once()
    summaries = gl.open_mr.call_args.kwargs["heal_summaries"]
    assert sum("aborted" in s for s in summaries) == 2


def test_heal_crash_consumes_attempt_and_continues(cfg, monkeypatch):
    # THE 5-of-15 bug: one crashed heal attempt must not abandon the remaining budget.
    # Attempt 1 crashes -> attempt 2 runs with a fresh Healer and heals the test green.
    gl = _wire(monkeypatch, cfg, [_result("failed"), _result("passed")])
    heal = AsyncMock(side_effect=[RuntimeError("gateway 502"), _healed()])
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=15))
    assert out["status"] == "passed"
    assert out["heal_attempts"] == 2  # the crash consumed attempt 1
    summaries = gl.open_mr.call_args.kwargs["heal_summaries"]
    assert "aborted" in summaries[0]
    assert summaries[1] == "fixed selector"


def test_nonconsecutive_heal_crashes_do_not_stop_healing(cfg, monkeypatch):
    # crash, heal, crash, heal: the abort counter resets on every completed attempt, so
    # scattered crashes never trip the consecutive-abort cap.
    _wire(monkeypatch, cfg, [_result("failed"), _result("failed"), _result("passed")])
    healed_again = models.HealedTest(
        file_name="QA-1-login.spec.ts", code="// healed 2", changes_summary="fixed again"
    )
    heal = AsyncMock(
        side_effect=[RuntimeError("boom 1"), _healed(), RuntimeError("boom 2"), healed_again]
    )
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=6))
    assert out["status"] == "passed"
    assert out["heal_attempts"] == 4
    assert heal.call_count == 4


def test_open_mr_failure_returns_error_without_crashing(cfg, monkeypatch):
    gl = _wire(monkeypatch, cfg, [_result("passed")])
    gl.open_mr.side_effect = RuntimeError("403 insufficient_scope")
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert out["mr_url"] is None
    assert "MR creation failed" in out["error"]
    assert out["status"] == "passed"


def test_gitlab_disabled_skips_mr(cfg, monkeypatch):
    # GITLAB_ENABLED=false (the local-without-GitLab path): pipeline runs, test+plan are
    # saved, but no MR is opened and the GitLab client is never even constructed.
    cfg = dataclasses.replace(cfg, gitlab_enabled=False)
    _wire(monkeypatch, cfg, [_result("passed")])
    # Local handle so the assert is typed (the monkeypatch swap is opaque to pyright).
    gl_cls = MagicMock()
    monkeypatch.setattr(orchestrator, "GitLabClient", gl_cls)
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert out["status"] == "passed"
    assert out["heal_attempts"] == 0
    assert out["mr_url"] is None
    assert "error" not in out
    gl_cls.assert_not_called()  # GitLab client never even constructed
    assert (cfg.plans_dir / "QA-1.json").exists()  # plan still persisted for review


def test_resolve_max_heal_attempts_reads_env_with_fallbacks(monkeypatch):
    monkeypatch.delenv("MAX_HEAL_ATTEMPTS", raising=False)
    assert heal_loop.resolve_max_heal_attempts() == heal_loop.MAX_HEAL_ATTEMPTS
    monkeypatch.setenv("MAX_HEAL_ATTEMPTS", "5")
    assert heal_loop.resolve_max_heal_attempts() == 5
    monkeypatch.setenv("MAX_HEAL_ATTEMPTS", "-3")  # negative is clamped to 0
    assert heal_loop.resolve_max_heal_attempts() == 0
    monkeypatch.setenv("MAX_HEAL_ATTEMPTS", "not-a-number")  # invalid -> default
    assert heal_loop.resolve_max_heal_attempts() == heal_loop.MAX_HEAL_ATTEMPTS


def test_max_heal_attempts_env_honored_when_arg_omitted(cfg, monkeypatch):
    monkeypatch.setenv("MAX_HEAL_ATTEMPTS", "1")
    _wire(monkeypatch, cfg, [_result("failed"), _result("failed")])
    out = asyncio.run(orchestrator.process_test_case("QA-1"))  # no max_heal_attempts arg
    assert out["heal_attempts"] == 1
    assert out["status"] == "failed"


def test_two_round_heal_accumulates_summaries(cfg, monkeypatch):
    gl = _wire(monkeypatch, cfg, [_result("failed"), _result("failed"), _result("passed")])
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=3))
    assert out["heal_attempts"] == 2
    assert out["status"] == "passed"
    # The healer double returns the same summary each call -> one entry per heal attempt.
    assert gl.open_mr.call_args.kwargs["heal_summaries"] == ["fixed selector", "fixed selector"]


def test_each_heal_attempt_written_to_its_own_file(cfg, monkeypatch):
    # The output folder keeps every iteration: the first generation under its own name,
    # then one sibling file per heal attempt — nothing is overwritten.
    _wire(monkeypatch, cfg, [_result("passed")])
    # Local handle so .call_args_list is typed (the _wire monkeypatch swap is opaque to pyright).
    run = AsyncMock(side_effect=[_result("failed"), _result("failed"), _result("passed")])
    monkeypatch.setattr(orchestrator, "run_test", run)
    asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=2))
    written = [call.args[1].file_name for call in run.call_args_list]
    assert written == [
        "QA-1-login.spec.ts",
        "QA-1-login.healer-attempt-1.spec.ts",
        "QA-1-login.healer-attempt-2.spec.ts",
    ]


def test_mr_commits_under_original_filename(cfg, monkeypatch):
    # The committed file PATH is the first-iteration filename (not the per-attempt sibling),
    # and the final code matches the last attempt. _healed() returns code "// healed".
    gl = _wire(monkeypatch, cfg, [_result("failed"), _result("passed")])
    asyncio.run(orchestrator.process_test_case("QA-1"))
    mr_test = gl.open_mr.call_args.args[0]
    assert mr_test.file_name == "QA-1-login.spec.ts"
    assert mr_test.code == "// healed"


def test_mr_revisions_one_commit_per_attempt(cfg, monkeypatch):
    # The MR gets the full attempt chain as `revisions` — one per code-producing attempt —
    # so a reviewer can diff attempt-to-attempt in GitLab. Here: initial gen + one heal.
    gl = _wire(monkeypatch, cfg, [_result("failed"), _result("passed")])
    asyncio.run(orchestrator.process_test_case("QA-1"))
    revisions = gl.open_mr.call_args.kwargs["revisions"]
    assert [r.code for r in revisions] == ["// spec", "// healed"]
    assert "initial generated test" in revisions[0].message
    assert "heal attempt 1" in revisions[1].message
    # The Healer's changes_summary rides in the heal commit's body.
    assert "fixed selector" in revisions[1].message


def test_mr_revisions_label_each_heal_attempt(cfg, monkeypatch):
    gl = _wire(monkeypatch, cfg, [_result("failed"), _result("failed"), _result("passed")])
    asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=3))
    revisions = gl.open_mr.call_args.kwargs["revisions"]
    subjects = [r.message.splitlines()[0] for r in revisions]
    assert subjects == [
        "[AI] QA-1: initial generated test",
        "[AI] QA-1: heal attempt 1",
        "[AI] QA-1: heal attempt 2",
    ]


def test_mr_revisions_include_compile_retry(cfg, monkeypatch):
    # did_run=False -> a Generator regeneration; that regen is its own MR commit, between
    # the initial generation and any heal.
    gl = _wire(monkeypatch, cfg, [_result("failed", did_run=False), _result("passed")])
    asyncio.run(orchestrator.process_test_case("QA-1"))
    subjects = [r.message.splitlines()[0] for r in gl.open_mr.call_args.kwargs["revisions"]]
    assert subjects == [
        "[AI] QA-1: initial generated test",
        "[AI] QA-1: regenerate after compile/collection error",
    ]


def test_mr_single_revision_when_first_run_passes(cfg, monkeypatch):
    # A test that passes on the first run yields exactly one revision (one commit).
    gl = _wire(monkeypatch, cfg, [_result("passed")])
    asyncio.run(orchestrator.process_test_case("QA-1"))
    revisions = gl.open_mr.call_args.kwargs["revisions"]
    assert len(revisions) == 1
    assert revisions[0].code == "// spec"


def test_healer_returned_filename_is_ignored(cfg, monkeypatch):
    # Whatever name the Healer returns, the orchestrator names the artifact itself, so a
    # drifting/hallucinated file_name can never fork or clobber the wrong file.
    _wire(monkeypatch, cfg, [_result("passed")])
    run = AsyncMock(side_effect=[_result("failed"), _result("passed")])
    monkeypatch.setattr(orchestrator, "run_test", run)
    monkeypatch.setattr(
        orchestrator,
        "heal_test",
        AsyncMock(
            return_value=models.HealedTest(
                file_name="some-other-name.spec.ts", code="// healed", changes_summary="x"
            )
        ),
    )
    asyncio.run(orchestrator.process_test_case("QA-1"))
    assert run.call_args_list[1].args[1].file_name == "QA-1-login.healer-attempt-1.spec.ts"


def test_compile_retry_regeneration_written_to_its_own_file(cfg, monkeypatch):
    # did_run=False routes to a Generator retry; that regeneration is also a separate
    # artifact (.regen.spec.ts), leaving the failed first attempt on disk.
    _wire(monkeypatch, cfg, [_result("passed")])
    run = AsyncMock(side_effect=[_result("failed", did_run=False), _result("passed")])
    monkeypatch.setattr(orchestrator, "run_test", run)
    asyncio.run(orchestrator.process_test_case("QA-1"))
    written = [call.args[1].file_name for call in run.call_args_list]
    assert written == ["QA-1-login.spec.ts", "QA-1-login.regen.spec.ts"]


def test_iteration_file_name_inserts_label_before_suffix():
    f = heal_loop.iteration_file_name
    assert f("QA-1-login.spec.ts", "healer-attempt-1") == "QA-1-login.healer-attempt-1.spec.ts"
    assert f("QA-1.test.ts", "regen") == "QA-1.regen.test.ts"
    assert f("plain.ts", "regen") == "plain.regen.ts"
    assert f("noext", "regen") == "noext.regen"


def test_commit_message_subject_only_and_with_body():
    m = heal_loop.commit_message
    # No detail -> subject only.
    assert m("QA-1", "initial generated test") == "[AI] QA-1: initial generated test"
    # Detail -> subject + blank line + full (possibly multi-line) body.
    msg = m("QA-1", "heal attempt 2", "fixed the login selector\nand awaited submit")
    assert msg.startswith("[AI] QA-1: heal attempt 2\n\n")
    assert "and awaited submit" in msg
    # Whitespace-only detail collapses to subject only.
    assert m("QA-1", "regenerate", "   ") == "[AI] QA-1: regenerate"


def test_heal_receives_plan_and_test_case(cfg, monkeypatch):
    # Path A: the Healer must get the originating plan + manual test case (intent), not just the
    # failing code + error — so it can diagnose/reconcile against what the test is meant to do.
    _wire(monkeypatch, cfg, [_result("failed"), _result("passed")])
    heal = AsyncMock(return_value=_healed())
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    asyncio.run(orchestrator.process_test_case("QA-1"))
    kwargs = heal.call_args.kwargs
    assert kwargs["plan"].test_case_key == "QA-1"
    assert kwargs["test_case"].key == "QA-1"


def test_second_heal_attempt_receives_first_attempts_summary(cfg, monkeypatch):
    # Attempt 1 gets an empty history; attempt 2 must see attempt 1's changes_summary
    # so the whole-file rewrite builds on the fix instead of undoing it.
    _wire(monkeypatch, cfg, [_result("failed"), _result("failed"), _result("passed")])
    heal = AsyncMock(return_value=_healed())
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=3))
    first, second = heal.call_args_list
    assert first.kwargs["heal_history"] == []
    assert second.kwargs["heal_history"] == ["fixed selector"]


def test_context_hash_changes_with_context_content(cfg, monkeypatch):
    _wire(monkeypatch, cfg, [_result("passed"), _result("passed")])
    cfg.project_context_path.write_text("context version A")
    asyncio.run(orchestrator.process_test_case("QA-1"))
    hash_a = json.loads((cfg.plans_dir / "QA-1.json").read_text())["context_hash"]

    cfg.project_context_path.write_text("context version B — materially different")
    asyncio.run(orchestrator.process_test_case("QA-1"))
    hash_b = json.loads((cfg.plans_dir / "QA-1.json").read_text())["context_hash"]

    assert hash_a != hash_b


def test_empty_plan_short_circuits_as_refused(cfg, monkeypatch):
    # planner.md's refusal contract: empty steps + reason in notes. Nothing runnable
    # exists, so generation/run/heal/MR must all be skipped — not burn heal attempts
    # on a stepless test and open a junk MR.
    gl = _wire(monkeypatch, cfg, [_result("passed")])
    refusal = models.TestPlan(
        test_case_key="QA-1",
        title="Login",
        target_url="https://staging.example.internal",
        steps=[],
        notes="touches forbidden /admin/billing — refusing per project_map.md",
    )
    monkeypatch.setattr(orchestrator, "plan_test_case", AsyncMock(return_value=refusal))
    gen = AsyncMock()
    run = AsyncMock()
    heal = AsyncMock()
    monkeypatch.setattr(orchestrator, "generate_test", gen)
    monkeypatch.setattr(orchestrator, "run_test", run)
    monkeypatch.setattr(orchestrator, "heal_test", heal)

    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert out["status"] == "refused"
    assert out["heal_attempts"] == 0
    assert out["mr_url"] is None
    assert "forbidden" in out["notes"]  # the Planner's reason is surfaced
    gen.assert_not_called()
    run.assert_not_called()
    heal.assert_not_called()
    gl.open_mr.assert_not_called()
    assert (cfg.plans_dir / "QA-1.json").exists()  # refusal plan persisted for audit


def test_no_report_failure_retries_generator_not_healer(cfg, monkeypatch):
    # did_run=False = compile/collection error: the Generator gets ONE retry with the
    # error; the browser-driving Healer is never invoked for code that never ran.
    _wire(monkeypatch, cfg, [_result("failed", did_run=False), _result("passed")])
    gen = AsyncMock(return_value=_generated())
    heal = AsyncMock(return_value=_healed())
    monkeypatch.setattr(orchestrator, "generate_test", gen)
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert out["status"] == "passed"
    assert out["heal_attempts"] == 0
    assert gen.call_count == 2  # initial generation + one compile retry
    retry_kwargs = gen.call_args.kwargs
    assert retry_kwargs["previous_code"] == "// spec"
    heal.assert_not_called()


def test_real_failure_goes_to_healer_without_generator_retry(cfg, monkeypatch):
    _wire(monkeypatch, cfg, [_result("failed"), _result("passed")])  # did_run=True
    gen = AsyncMock(return_value=_generated())
    heal = AsyncMock(return_value=_healed())
    monkeypatch.setattr(orchestrator, "generate_test", gen)
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert out["heal_attempts"] == 1
    assert gen.call_count == 1  # no compile retry for a test that actually ran
    heal.assert_called_once()


def test_persistent_compile_error_still_reaches_heal_loop_and_mr(cfg, monkeypatch):
    # The Generator retry is bounded to ONE attempt: if the regenerated file still
    # doesn't run, the normal heal loop + MR path takes over (a human always reviews).
    gl = _wire(
        monkeypatch,
        cfg,
        [_result("failed", did_run=False), _result("failed", did_run=False), _result("failed")],
    )
    gen = AsyncMock(return_value=_generated())
    monkeypatch.setattr(orchestrator, "generate_test", gen)
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=1))
    assert gen.call_count == 2  # initial + exactly one retry, never more
    assert out["heal_attempts"] == 1
    gl.open_mr.assert_called_once()


def test_planning_failure_returns_error_without_crashing(cfg, monkeypatch):
    # A Planner/Generator crash (e.g. an MCP tool exceeding its retry budget) must fail
    # cleanly — no plan/test means no MR, but no stack trace either.
    gl = _wire(monkeypatch, cfg, [_result("passed")])
    monkeypatch.setattr(
        orchestrator,
        "plan_test_case",
        AsyncMock(side_effect=RuntimeError("Tool 'browser_type' exceeded max retries count of 2")),
    )
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert out["status"] == "error"
    assert "Planning/generation failed" in out["error"]
    assert out["mr_url"] is None
    gl.open_mr.assert_not_called()


def test_local_source_uses_local_loader_not_xray_client(cfg, monkeypatch, tmp_path):
    # TESTCASE_SOURCE=local routes through load_local_test_case; XrayClient is never built.
    local_cfg = dataclasses.replace(cfg, testcase_source="local", local_testcase_dir=tmp_path)
    _wire(monkeypatch, local_cfg, [_result("passed")])
    xray_cls = MagicMock()
    monkeypatch.setattr(testcases, "XrayClient", xray_cls)
    loader = MagicMock(return_value=_manual_case())
    monkeypatch.setattr(testcases, "load_local_test_case", loader)
    out = asyncio.run(orchestrator.process_test_case("NOTE-1"))
    assert out["status"] == "passed"
    loader.assert_called_once()
    assert loader.call_args.args[1] == "NOTE-1"  # called as (config, issue_key)
    xray_cls.assert_not_called()


def test_xray_source_uses_xray_client_not_local_loader(cfg, monkeypatch):
    # Default (xray) mode must NOT touch the local loader.
    _wire(monkeypatch, cfg, [_result("passed")])  # cfg.testcase_source == "xray"
    loader = MagicMock()
    monkeypatch.setattr(testcases, "load_local_test_case", loader)
    asyncio.run(orchestrator.process_test_case("QA-1"))
    loader.assert_not_called()


def _failed(error_message, failed_test="QA-1: login", error_line=None):
    return models.TestRunResult(
        status="failed", stdout="", stderr="", failed_test=failed_test,
        error_message=error_message, error_line=error_line,
    )


_NOT_FOUND = (
    "Error: Save visible before click\n\nexpect(locator).toBeVisible() failed\n\n"
    "Locator: {loc}\nExpected: visible\nTimeout: 5000ms\nError: element(s) not found\n"
)
_DISABLED = (
    "Error: expect(locator).toBeDisabled() failed\n\nLocator:  getByTestId('save')\n"
    "Expected: disabled\nReceived: enabled\nTimeout:  5000ms\n"
)
_STEPPED_SPEC = """test('QA-1: login', async ({ page }) => {
  await test.step('Open the login page', async () => {
    await page.goto('/');
  });
  await test.step('Click "Save"', async () => {
    await expect(page.getByTestId('save'), 'Save visible before click').toBeVisible();
    await page.getByTestId('save').click();
  });
  await test.step('Verify the note is listed', async () => {
    await expect(page.getByTestId('note')).toBeVisible();
  });
});"""


def test_failure_signature_is_selector_agnostic():
    # Two locator failures on the SAME test but DIFFERENT selectors are the same recurring
    # failure — this is what makes a heal that swapped the selector (and still failed) escalate.
    a = _failed(
        "locator.click: Timeout 30000ms exceeded.\nCall log:\n  - waiting for getByTestId('x')"
    )
    b = _failed(_NOT_FOUND.format(loc="locator('xpath=//y')"))
    assert heal_loop.failure_signature(a) == heal_loop.failure_signature(b)


def test_failure_signature_differs_by_kind_and_test():
    locator = _failed(_NOT_FOUND.format(loc="getByTestId('save')"))
    assertion = _failed(_DISABLED)
    other_test = _failed(_NOT_FOUND.format(loc="getByTestId('save')"), failed_test="QA-1: logout")
    sig = heal_loop.failure_signature
    assert sig(locator) != sig(assertion)
    assert sig(locator) != sig(other_test)


def test_failure_signature_keys_on_the_enclosing_step_not_the_line():
    sig = heal_loop.failure_signature
    in_save = _failed(_NOT_FOUND.format(loc="getByTestId('save')"), error_line=6)
    # A heal that inserts a line above shifts the line number but not the step: still a repeat.
    shifted_code = _STEPPED_SPEC.replace("await page.goto('/');", "await page.goto('/');\n    //")
    in_save_shifted = _failed(_NOT_FOUND.format(loc="getByTestId('save2')"), error_line=7)
    assert sig(in_save, _STEPPED_SPEC) == sig(in_save_shifted, shifted_code)
    # The failure moved on to a LATER step: not a repeat.
    in_verify = _failed(_NOT_FOUND.format(loc="getByTestId('note')"), error_line=10)
    assert sig(in_save, _STEPPED_SPEC) != sig(in_verify, _STEPPED_SPEC)


def test_failing_step_finds_the_enclosing_step_title():
    assert heal_loop.failing_step(_STEPPED_SPEC, 6) == 'click "save"'
    assert heal_loop.failing_step(_STEPPED_SPEC, 1) == ""  # before any step
    assert heal_loop.failing_step(_STEPPED_SPEC, None) == ""


def test_consecutive_repeats_counts_trailing_matches():
    assert heal_loop.consecutive_repeats([], "a") == 0
    assert heal_loop.consecutive_repeats(["a"], "a") == 1
    assert heal_loop.consecutive_repeats(["a", "a"], "a") == 2
    assert heal_loop.consecutive_repeats(["a", "b"], "a") == 0  # streak broken
    assert heal_loop.consecutive_repeats(["b", "a"], "a") == 1


def test_recurring_failure_raises_the_repeat_count(cfg, monkeypatch):
    # A failure that recurs the SAME way across completed heals raises failure_repeats:
    # 0 on the first sighting, then 1, 2 as it persists.
    _wire(
        monkeypatch, cfg,
        [_result("failed"), _result("failed"), _result("failed"), _result("failed")],
    )
    heal = _healer()
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=3))
    assert [c.kwargs["failure_repeats"] for c in heal.call_args_list] == [0, 1, 2]


def test_failure_that_moved_to_a_later_step_is_not_a_repeat(cfg, monkeypatch):
    _wire(monkeypatch, cfg, [])
    stepped = models.GeneratedTest(
        file_name="QA-1-login.spec.ts", code=_STEPPED_SPEC, description="x"
    )
    monkeypatch.setattr(orchestrator, "generate_test", AsyncMock(return_value=stepped))
    run = AsyncMock(side_effect=[
        _failed(_NOT_FOUND.format(loc="getByTestId('save')"), error_line=6),
        _failed(_NOT_FOUND.format(loc="getByTestId('note')"), error_line=10),
        _result("passed"),
    ])
    monkeypatch.setattr(orchestrator, "run_test", run)
    heal = AsyncMock(side_effect=[
        models.HealedTest(file_name="x", code=_STEPPED_SPEC + "\n// fix 1", changes_summary="a"),
        models.HealedTest(file_name="x", code=_STEPPED_SPEC + "\n// fix 2", changes_summary="b"),
    ])
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=3))
    assert out["status"] == "passed"
    assert [c.kwargs["failure_repeats"] for c in heal.call_args_list] == [0, 0]


def test_crashed_attempt_neither_escalates_nor_enters_heal_history(cfg, monkeypatch):
    # Attempt 1 crashes: no fix was tried, so attempt 2 must NOT be told the failure persisted,
    # and the crash note must stay out of the "code already contains these changes" history.
    # Attempt 3 follows a COMPLETED heal that didn't fix it — that one is a real repeat.
    gl = _wire(monkeypatch, cfg, [_result("failed"), _result("failed"), _result("failed")])
    heal = AsyncMock(side_effect=[
        RuntimeError("gateway 502"),
        _healed(),
        models.HealedTest(file_name="x", code="// healed 2", changes_summary="escalated"),
    ])
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=3))
    assert out["heal_attempts"] == 3
    calls = heal.call_args_list
    assert [c.kwargs["failure_repeats"] for c in calls] == [0, 0, 1]
    assert calls[1].kwargs["heal_history"] == []
    assert calls[2].kwargs["heal_history"] == ["fixed selector"]
    # The reviewer still sees the crash in the MR.
    summaries = gl.open_mr.call_args.kwargs["heal_summaries"]
    assert "aborted" in summaries[0] and "gateway 502" in summaries[0]


def test_unchanged_heal_stops_the_loop_with_a_verdict(cfg, monkeypatch):
    # The Healer returns its input unchanged (modulo trailing whitespace / CRLF) = "no fix: app bug
    # or spec divergence". Re-running identical code is pointless: stop, write no attempt file,
    # add no MR revision, and surface the verdict + the Healer's explanation.
    gl = _wire(monkeypatch, cfg, [])
    run = AsyncMock(side_effect=[_failed(_DISABLED)])
    monkeypatch.setattr(orchestrator, "run_test", run)
    heal = AsyncMock(return_value=models.HealedTest(
        file_name="QA-1-login.spec.ts",
        code="// spec  \r\n\n",
        changes_summary="Save stays enabled for an empty title; the case expects it disabled.",
    ))
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=3))
    assert heal.call_count == 1
    assert run.call_count == 1  # no re-run, so no healer-attempt-1 file either
    assert out["status"] == "failed"
    assert out["heal_attempts"] == 1
    assert out["heal_verdict"].startswith(heal_loop.NO_FIX_VERDICT)
    assert "stays enabled" in out["heal_verdict"]
    kwargs = gl.open_mr.call_args.kwargs
    assert [r.message.splitlines()[0] for r in kwargs["revisions"]] == [
        "[AI] QA-1: initial generated test"
    ]
    assert kwargs["heal_verdict"] == out["heal_verdict"]
    assert kwargs["final_status"] == "failed"


def test_error_status_is_never_healed(cfg, monkeypatch):
    # status "error" = the run itself broke (whole-run timeout, Playwright failed to launch):
    # nothing the Healer can fix. No heal call; the MR still opens and carries the error.
    timed_out = models.TestRunResult(
        status="error", stdout="", stderr="", error_message="Playwright run timed out after 300s"
    )
    gl = _wire(monkeypatch, cfg, [timed_out])
    heal = AsyncMock(return_value=_healed())
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    heal.assert_not_called()
    assert out["status"] == "error"
    assert out["heal_attempts"] == 0
    assert "timed out after 300s" in out["heal_verdict"]
    gl.open_mr.assert_called_once()
    kwargs = gl.open_mr.call_args.kwargs
    assert kwargs["final_status"] == "error"
    assert "timed out after 300s" in kwargs["heal_verdict"]


def test_error_on_a_rerun_stops_healing(cfg, monkeypatch):
    crashed = models.TestRunResult(
        status="error", stdout="", stderr="", error_message="Could not launch Playwright: npx"
    )
    gl = _wire(monkeypatch, cfg, [_result("failed"), crashed])
    heal = _healer()
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=3))
    assert heal.call_count == 1
    assert out["status"] == "error"
    gl.open_mr.assert_called_once()


# --- multi-environment runs + blocked runs -------------------------------------------

_QA2 = "https://qa2.example.internal"
_QA3 = "https://qa3.example.internal"


def _multi_env(cfg):
    return dataclasses.replace(
        cfg, staging_base_urls=(cfg.staging_base_url, _QA2, _QA3)
    )


def test_single_environment_output_is_unchanged(cfg, monkeypatch):
    gl = _wire(monkeypatch, cfg, [_result("passed")])
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert "environments" not in out
    assert gl.open_mr.call_args.kwargs["environment_results"] == []


def test_secondary_environments_run_final_spec_without_planner_or_healer(cfg, monkeypatch):
    cfg = _multi_env(cfg)
    gl = _wire(monkeypatch, cfg, [])
    failed_qa3 = models.TestRunResult(
        status="failed", stdout="", stderr="", error_message="locator timeout\nstack…"
    )
    run = AsyncMock(
        side_effect=[_result("failed"), _result("passed"), _result("passed"), failed_qa3]
    )
    heal = AsyncMock(return_value=_healed())
    plan = AsyncMock(return_value=_plan())
    monkeypatch.setattr(orchestrator, "run_test", run)
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    monkeypatch.setattr(orchestrator, "plan_test_case", plan)

    out = asyncio.run(orchestrator.process_test_case("QA-1"))

    plan.assert_awaited_once()
    heal.assert_awaited_once()  # healing only on the primary, despite the qa3 failure
    # Primary runs use the default base URL; each secondary gets its own URL + results dir
    # and runs the FINAL (healed) spec.
    primary_runs, secondary_runs = run.call_args_list[:2], run.call_args_list[2:]
    assert all("base_url" not in c.kwargs for c in primary_runs)
    assert [(c.kwargs["base_url"], c.kwargs["results_dir"]) for c in secondary_runs] == [
        (_QA2, "test-results/env-2"),
        (_QA3, "test-results/env-3"),
    ]
    assert {c.args[1].code for c in secondary_runs} == {"// healed"}
    assert out["status"] == "passed"  # the primary (healed) result
    assert out["environments"] == [
        {"base_url": cfg.staging_base_url, "primary": True, "status": "passed", "error": None},
        {"base_url": _QA2, "primary": False, "status": "passed", "error": None},
        {"base_url": _QA3, "primary": False, "status": "failed", "error": "locator timeout"},
    ]
    envs = gl.open_mr.call_args.kwargs["environment_results"]
    assert [e.base_url for e in envs] == [cfg.staging_base_url, _QA2, _QA3]
    gl.open_mr.assert_called_once()


def test_multi_environment_results_reach_the_summary_without_gitlab(cfg, monkeypatch):
    cfg = dataclasses.replace(_multi_env(cfg), gitlab_enabled=False)
    _wire(monkeypatch, cfg, [_result("passed"), _result("error"), _result("passed")])
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert [e["status"] for e in out["environments"]] == ["passed", "error", "passed"]


def test_blocked_run_is_not_healed(cfg, monkeypatch):
    blocked = models.TestRunResult(
        status="error", did_run=False, blocked=True, stdout="", stderr="",
        error_message="Run blocked before Playwright started: …",
    )
    gl = _wire(monkeypatch, cfg, [blocked])
    heal = AsyncMock(return_value=_healed())
    gen = AsyncMock(return_value=_generated())
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    monkeypatch.setattr(orchestrator, "generate_test", gen)
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    heal.assert_not_called()
    assert gen.await_count == 1  # no compile-retry regeneration either
    assert out["status"] == "error"
    assert out["heal_attempts"] == 0
    assert "navigation allow-list" in out["heal_verdict"]
    gl.open_mr.assert_called_once()  # still surfaced to a human


def test_runs_pass_the_plan_for_the_preflight_check(cfg, monkeypatch):
    _wire(monkeypatch, cfg, [])
    run = AsyncMock(side_effect=[_result("passed")])
    monkeypatch.setattr(orchestrator, "run_test", run)
    asyncio.run(orchestrator.process_test_case("QA-1"))
    assert run.call_args.kwargs["plan"] == _plan()


# --- Vision Aid line in the run summary ----------------------------------------


def test_summary_has_no_vision_line_when_vision_is_off(cfg, monkeypatch):
    _wire(monkeypatch, cfg, [_result("passed")])
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert "vision" not in out


def test_summary_reports_vision_gaps_per_agent(cfg, monkeypatch, caplog):
    vcfg = dataclasses.replace(cfg, vision_max_calls=3)
    _wire(monkeypatch, vcfg, [_result("failed"), _result("failed"), _result("passed")])

    async def plan(config, test_case, *, vision_stats, **_kwargs):
        vision_stats.checks, vision_stats.no_screenshot = 3, 3  # every check lacked a screenshot
        return _plan()

    heals = _healer()

    async def heal(*args, vision_stats, **kwargs):
        vision_stats.checks += 1  # pooled across heal attempts
        return await heals(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "plan_test_case", plan)
    monkeypatch.setattr(orchestrator, "heal_test", heal)
    with caplog.at_level("WARNING", logger="ai_test_gen.pipeline.summary"):
        out = asyncio.run(orchestrator.process_test_case("QA-1"))
    expected = "Planner: 3 of 3 checks had no screenshot; Healer: 2 checks, all captured"
    assert out["vision"] == expected
    assert any(expected in r.getMessage() for r in caplog.records)  # WARNING: degraded


def test_summary_vision_line_on_early_refusal(cfg, monkeypatch):
    vcfg = dataclasses.replace(cfg, vision_max_calls=2)
    _wire(monkeypatch, vcfg, [])
    refusal = models.TestPlan(test_case_key="QA-1", title="t", target_url="https://x", steps=[])
    monkeypatch.setattr(orchestrator, "plan_test_case", AsyncMock(return_value=refusal))
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert out["status"] == "refused"
    assert out["vision"] == "Planner: no checks; Healer: no checks"


# --- Per-agent usage in the run summary ---------------------------------------------


def _assert_usage_shape(out):
    report = out["usage"]
    assert set(report) == {"agents", "total"}
    assert set(report["total"]) == {
        "requests", "input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens",
        "reasoning_only_retries", "wall_s",
    }
    assert json.loads(json.dumps(report)) == report  # batch scripts aggregate it as JSON


def _recording_planner(*, fail=False):
    """Planner double that spends usage into the run's log (partially, then raises if ``fail``)."""

    async def plan(config, test_case, *, vision_stats, usage):
        usage.record(
            "Planner", "planner-model",
            RunUsage(requests=4, input_tokens=4_000, output_tokens=40), 2.0, ok=not fail,
        )
        if fail:
            raise RuntimeError("UsageLimitExceeded: request_limit of 4")
        return _plan()

    return plan


def test_usage_summary_on_success_counts_every_agent_run(cfg, monkeypatch):
    _wire(monkeypatch, cfg, [_result("failed"), _result("failed"), _result("passed")])
    monkeypatch.setattr(orchestrator, "plan_test_case", _recording_planner())
    heal = _healer()
    monkeypatch.setattr(orchestrator, "heal_test", heal)

    out = asyncio.run(orchestrator.process_test_case("QA-1"))

    _assert_usage_shape(out)
    assert out["usage"]["agents"][0]["agent"] == "Planner"
    assert out["usage"]["total"]["requests"] == 4
    # Each heal attempt is labelled with its own attempt number and shares the run's log.
    assert [c.kwargs["attempt"] for c in heal.call_args_list] == [1, 2]
    assert len({id(c.kwargs["usage"]) for c in heal.call_args_list}) == 1
    gen = orchestrator.generate_test
    assert isinstance(gen, AsyncMock)
    assert gen.call_args.kwargs["usage"] is heal.call_args.kwargs["usage"]


def test_usage_summary_on_planning_error_keeps_the_aborted_planner(cfg, monkeypatch):
    _wire(monkeypatch, cfg, [])
    monkeypatch.setattr(orchestrator, "plan_test_case", _recording_planner(fail=True))

    out = asyncio.run(orchestrator.process_test_case("QA-1"))

    assert out["status"] == "error"
    _assert_usage_shape(out)
    [planner] = out["usage"]["agents"]
    assert (planner["outcome"], planner["requests"]) == ("error", 4)


def test_usage_summary_on_refusal_mr_failure_and_gitlab_off(cfg, monkeypatch):
    _wire(monkeypatch, cfg, [])
    refusal = models.TestPlan(test_case_key="QA-1", title="t", target_url="https://x", steps=[])
    monkeypatch.setattr(orchestrator, "plan_test_case", AsyncMock(return_value=refusal))
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert out["status"] == "refused"
    _assert_usage_shape(out)

    gl = _wire(monkeypatch, cfg, [_result("passed")])
    gl.open_mr.side_effect = RuntimeError("401")
    out = asyncio.run(orchestrator.process_test_case("QA-1"))
    assert "MR creation failed" in out["error"]
    _assert_usage_shape(out)

    _wire(monkeypatch, dataclasses.replace(cfg, gitlab_enabled=False), [_result("passed")])
    _assert_usage_shape(asyncio.run(orchestrator.process_test_case("QA-1")))


def test_usage_total_is_logged_at_the_end_of_the_run(cfg, monkeypatch, caplog):
    _wire(monkeypatch, cfg, [_result("passed")])
    monkeypatch.setattr(orchestrator, "plan_test_case", _recording_planner())
    with caplog.at_level("INFO", logger="ai_test_gen.pipeline.summary"):
        asyncio.run(orchestrator.process_test_case("QA-1"))
    [line] = [r.getMessage() for r in caplog.records if "Usage total" in r.getMessage()]
    assert "[QA-1] Usage total: 4 requests, in=4,000 out=40 tokens" in line
    assert "Planner" in line  # the per-agent table follows the total


def test_main_prints_usage_as_a_table_not_a_raw_dict(monkeypatch, capsys, tmp_path):
    log = UsageLog()
    log.record("Planner", "planner-model", RunUsage(requests=41, input_tokens=512_340,
               output_tokens=6_210), 243.0, ok=True)
    result = {"issue_key": "QA-1", "status": "passed", "usage": log.summary(300.0)}

    async def fake_process(_key):
        return result

    monkeypatch.setattr(orchestrator, "process_test_case", fake_process)
    monkeypatch.setattr(
        orchestrator, "_configure_logging", lambda key, verbose: tmp_path / "run.log"
    )
    monkeypatch.setattr("sys.argv", ["run_one.py", "QA-1"])

    orchestrator.main()

    printed = capsys.readouterr().out
    assert "usage: {" not in printed  # not the raw dict
    assert "=== Model usage ===" in printed
    assert "  Planner  planner-model        41  512,340  6,210       0  4m03s" in printed
    assert "  total                         41  512,340  6,210       0  5m00s" in printed
