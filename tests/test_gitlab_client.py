"""Unit tests for the GitLab MR opener — fully local (python-gitlab is mocked).

``models.TestPlan`` is referenced via the module so pytest does not collect it as a
test class (its name starts with "Test").
"""
from __future__ import annotations

from unittest.mock import MagicMock

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
