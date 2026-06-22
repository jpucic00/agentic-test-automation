"""Unit tests for the orchestrator — fully local (every agent + integration is mocked).

The agents, the runner, the Xray client, and the GitLab client are monkeypatched in the
``orchestrator`` namespace, so no network, browser, or subprocess is touched. Coroutines
are driven with ``asyncio.run`` (no pytest-asyncio). ``Test*`` models are built via the
``models`` module so pytest does not collect them as test classes.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock, MagicMock

from ai_test_gen import models, orchestrator


def _manual_case():
    return models.ManualTestCase(
        key="QA-1", title="Login", steps=["log in"], expected_results=["dashboard"]
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


def _result(status, did_run=True):
    return models.TestRunResult(status=status, did_run=did_run, stdout="", stderr="")


def _wire(monkeypatch, cfg, run_results):
    """Monkeypatch the whole pipeline; return the mock GitLab client."""
    cfg.plans_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(orchestrator, "load_config", lambda: cfg)

    fake_xray = MagicMock()
    fake_xray.fetch.return_value = _manual_case()
    monkeypatch.setattr(orchestrator, "XrayClient", MagicMock(return_value=fake_xray))

    monkeypatch.setattr(orchestrator, "plan_test_case", AsyncMock(return_value=_plan()))
    monkeypatch.setattr(orchestrator, "generate_test", AsyncMock(return_value=_generated()))
    monkeypatch.setattr(orchestrator, "run_test", AsyncMock(side_effect=list(run_results)))
    monkeypatch.setattr(orchestrator, "heal_test", AsyncMock(return_value=_healed()))

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
    # A Healer crash (e.g. browser_click exceeded max retries) must not discard the run.
    gl = _wire(monkeypatch, cfg, [_result("failed")])
    monkeypatch.setattr(
        orchestrator,
        "heal_test",
        AsyncMock(side_effect=RuntimeError("browser_click exceeded max retries")),
    )
    out = asyncio.run(orchestrator.process_test_case("QA-1", max_heal_attempts=3))
    assert out["status"] == "failed"
    assert out["heal_attempts"] == 1
    assert out["mr_url"] == "https://gitlab/mr/1"
    gl.open_mr.assert_called_once()
    summaries = gl.open_mr.call_args.kwargs["heal_summaries"]
    assert any("aborted" in s for s in summaries)


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
    assert orchestrator._resolve_max_heal_attempts() == orchestrator.MAX_HEAL_ATTEMPTS
    monkeypatch.setenv("MAX_HEAL_ATTEMPTS", "5")
    assert orchestrator._resolve_max_heal_attempts() == 5
    monkeypatch.setenv("MAX_HEAL_ATTEMPTS", "-3")  # negative is clamped to 0
    assert orchestrator._resolve_max_heal_attempts() == 0
    monkeypatch.setenv("MAX_HEAL_ATTEMPTS", "not-a-number")  # invalid -> default
    assert orchestrator._resolve_max_heal_attempts() == orchestrator.MAX_HEAL_ATTEMPTS


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
    # _healed() returns the same summary each call -> one entry per heal attempt.
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


def test_mr_commits_latest_code_under_original_filename(cfg, monkeypatch):
    # The MR gets ONE file: the final healed code, named like the FIRST iteration (not the
    # per-attempt sibling). _healed() returns code "// healed".
    gl = _wire(monkeypatch, cfg, [_result("failed"), _result("passed")])
    asyncio.run(orchestrator.process_test_case("QA-1"))
    mr_test = gl.open_mr.call_args.args[0]
    assert mr_test.file_name == "QA-1-login.spec.ts"
    assert mr_test.code == "// healed"


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
    f = orchestrator._iteration_file_name
    assert f("QA-1-login.spec.ts", "healer-attempt-1") == "QA-1-login.healer-attempt-1.spec.ts"
    assert f("QA-1.test.ts", "regen") == "QA-1.regen.test.ts"
    assert f("plain.ts", "regen") == "plain.regen.ts"
    assert f("noext", "regen") == "noext.regen"


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
