"""Unit tests for the GitLab MR opener — fully local (python-gitlab is mocked).

``models.TestPlan`` is referenced via the module so pytest does not collect it as a
test class (its name starts with "Test").
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

from ai_test_gen import gitlab_client, models


def _plan():
    return models.TestPlan(
        test_case_key="QA-1",
        title="Login happy path",
        target_url="https://staging.example.internal",
        preconditions=["user exists"],
        steps=[
            models.PlanStep(action="log in", target_selector="#metaMenuItem5", expected="dashboard")
        ],
        notes="logs in as Admin",
    )


def _generated():
    return models.GeneratedTest(
        file_name="QA-1-login.spec.ts", code="// spec", description="login happy path"
    )


def _client(monkeypatch, cfg):
    gl = MagicMock()
    project = gl.projects.get.return_value
    project.mergerequests.create.return_value.web_url = (
        "https://gitlab.internal/x/-/merge_requests/1"
    )
    monkeypatch.setattr(gitlab_client.gitlab, "Gitlab", MagicMock(return_value=gl))
    return gitlab_client.GitLabClient(cfg), project


def test_open_mr_returns_web_url_and_makes_calls(cfg, monkeypatch):
    client, project = _client(monkeypatch, cfg)
    url = client.open_mr(_generated(), _plan(), "QA-1")
    assert url == "https://gitlab.internal/x/-/merge_requests/1"

    branch_arg = project.branches.create.call_args[0][0]
    assert branch_arg["ref"] == "main"
    branch = branch_arg["branch"]
    assert branch.startswith("ai-gen/qa-1-")

    # Commit carries the test file + the plan JSON.
    actions = project.commits.create.call_args[0][0]["actions"]
    paths = {a["file_path"] for a in actions}
    assert paths == {"tests/generated/QA-1-login.spec.ts", "tests/generated/_plans/QA-1.json"}

    mr_arg = project.mergerequests.create.call_args[0][0]
    assert mr_arg["labels"] == ["ai-generated", "qa-review-needed"]
    assert mr_arg["source_branch"] == branch
    assert mr_arg["remove_source_branch"] is True


def test_branch_name_uses_ci_job_id_when_present(cfg, monkeypatch):
    monkeypatch.setenv("CI_JOB_ID", "987654")
    client, project = _client(monkeypatch, cfg)
    client.open_mr(_generated(), _plan(), "QA-1")
    branch = project.branches.create.call_args[0][0]["branch"]
    assert branch.endswith("-987654")


def test_plan_json_argument_is_committed_verbatim(cfg, monkeypatch):
    client, project = _client(monkeypatch, cfg)
    client.open_mr(_generated(), _plan(), "QA-1", plan_json='{"context_hash": "abc"}')
    actions = project.commits.create.call_args[0][0]["actions"]
    plan_action = next(a for a in actions if a["file_path"].endswith("/_plans/QA-1.json"))
    assert plan_action["content"] == '{"context_hash": "abc"}'


def test_mr_description_renders_healer_section(cfg, monkeypatch):
    client, project = _client(monkeypatch, cfg)
    client.open_mr(
        _generated(),
        _plan(),
        "QA-1",
        heal_summaries=["fixed #login selector", "added await on submit"],
        heal_attempts=2,
        final_status="passed",
    )
    desc = project.mergerequests.create.call_args[0][0]["description"]
    assert "Healer attempts (2)" in desc
    assert "fixed #login selector" in desc
    assert "added await on submit" in desc
    assert "Heal attempts:** 2" in desc


def test_mr_description_omits_healer_section_when_no_heals(cfg, monkeypatch):
    client, project = _client(monkeypatch, cfg)
    client.open_mr(_generated(), _plan(), "QA-1", heal_attempts=0)
    desc = project.mergerequests.create.call_args[0][0]["description"]
    assert "Healer attempts" not in desc


def test_mr_description_no_summary_recorded_when_heals_but_empty_summaries(cfg, monkeypatch):
    client, project = _client(monkeypatch, cfg)
    client.open_mr(_generated(), _plan(), "QA-1", heal_attempts=2, heal_summaries=None)
    desc = project.mergerequests.create.call_args[0][0]["description"]
    assert "Healer attempts (2)" in desc
    assert "- (no summary recorded)" in desc


def test_mr_description_renders_trace_path_when_present(cfg, monkeypatch):
    client, project = _client(monkeypatch, cfg)
    client.open_mr(
        _generated(), _plan(), "QA-1",
        final_status="failed",
        trace_path="output/test-results/QA-1-login/trace.zip",
    )
    desc = project.mergerequests.create.call_args[0][0]["description"]
    assert "Playwright trace" in desc
    assert "output/test-results/QA-1-login/trace.zip" in desc


def test_mr_description_omits_trace_line_without_trace(cfg, monkeypatch):
    client, project = _client(monkeypatch, cfg)
    client.open_mr(_generated(), _plan(), "QA-1", final_status="passed")
    desc = project.mergerequests.create.call_args[0][0]["description"]
    assert "Playwright trace" not in desc


def test_branch_name_random_suffix_shape_and_uniqueness(cfg, monkeypatch):
    monkeypatch.delenv("CI_JOB_ID", raising=False)
    client, project = _client(monkeypatch, cfg)
    client.open_mr(_generated(), _plan(), "QA-1")
    branch1 = project.branches.create.call_args[0][0]["branch"]
    assert re.fullmatch(r"ai-gen/qa-1-\d{8}-\d{6}-[0-9a-f]{6}", branch1)
    client.open_mr(_generated(), _plan(), "QA-1")
    branch2 = project.branches.create.call_args[0][0]["branch"]
    assert branch1 != branch2  # collision-resistant token differs per run


def test_orphan_branch_deleted_when_commit_fails(cfg, monkeypatch):
    client, project = _client(monkeypatch, cfg)
    project.commits.create.side_effect = RuntimeError("gitlab 500")
    with pytest.raises(RuntimeError):
        client.open_mr(_generated(), _plan(), "QA-1")
    created_branch = project.branches.create.call_args[0][0]["branch"]
    project.branches.delete.assert_called_once_with(created_branch)


def test_open_mr_commits_one_commit_per_revision(cfg, monkeypatch):
    # The per-attempt history -> one commit each, all on the same file path, so the MR's
    # commit view shows the diff from one attempt to the next.
    client, project = _client(monkeypatch, cfg)
    revisions = [
        gitlab_client.TestRevision(message="[AI] QA-1: initial generated test", code="// v1"),
        gitlab_client.TestRevision(message="[AI] QA-1: heal attempt 1", code="// v2"),
        gitlab_client.TestRevision(message="[AI] QA-1: heal attempt 2", code="// v3"),
    ]
    client.open_mr(_generated(), _plan(), "QA-1", revisions=revisions)

    calls = project.commits.create.call_args_list
    assert len(calls) == 3  # one commit per attempt

    # First commit CREATES the test file + the plan JSON.
    first = calls[0].args[0]
    assert first["commit_message"] == "[AI] QA-1: initial generated test"
    first_actions = {a["file_path"]: a for a in first["actions"]}
    assert set(first_actions) == {
        "tests/generated/QA-1-login.spec.ts",
        "tests/generated/_plans/QA-1.json",
    }
    assert first_actions["tests/generated/QA-1-login.spec.ts"]["action"] == "create"
    assert first_actions["tests/generated/QA-1-login.spec.ts"]["content"] == "// v1"

    # Later commits UPDATE the same test path (plan is not re-committed), one per attempt.
    second = calls[1].args[0]
    assert second["commit_message"] == "[AI] QA-1: heal attempt 1"
    assert second["actions"] == [
        {"action": "update", "file_path": "tests/generated/QA-1-login.spec.ts", "content": "// v2"}
    ]
    assert calls[2].args[0]["actions"][0]["content"] == "// v3"


def test_open_mr_skips_identical_consecutive_revision(cfg, monkeypatch):
    # A heal that returns code identical to the previous attempt makes no commit (GitLab
    # rejects an empty diff); the surrounding attempts still commit.
    client, project = _client(monkeypatch, cfg)
    revisions = [
        gitlab_client.TestRevision(message="m1", code="// same"),
        gitlab_client.TestRevision(message="m2", code="// same"),  # no change -> skipped
        gitlab_client.TestRevision(message="m3", code="// different"),
    ]
    client.open_mr(_generated(), _plan(), "QA-1", revisions=revisions)
    messages = [c.args[0]["commit_message"] for c in project.commits.create.call_args_list]
    assert messages == ["m1", "m3"]


def test_open_mr_without_revisions_makes_one_commit(cfg, monkeypatch):
    # Back-compat: no revisions -> a single commit with the test code + the plan JSON.
    client, project = _client(monkeypatch, cfg)
    client.open_mr(_generated(), _plan(), "QA-1")
    assert project.commits.create.call_count == 1


def test_orphan_branch_deleted_when_a_later_commit_fails(cfg, monkeypatch):
    # Cleanup must also fire when a NON-first commit in the revision chain fails.
    client, project = _client(monkeypatch, cfg)
    project.commits.create.side_effect = [MagicMock(), RuntimeError("gitlab 500 on commit 2")]
    revisions = [
        gitlab_client.TestRevision(message="m1", code="// v1"),
        gitlab_client.TestRevision(message="m2", code="// v2"),
    ]
    with pytest.raises(RuntimeError):
        client.open_mr(_generated(), _plan(), "QA-1", revisions=revisions)
    created_branch = project.branches.create.call_args[0][0]["branch"]
    project.branches.delete.assert_called_once_with(created_branch)
