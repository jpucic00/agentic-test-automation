"""Unit tests for ai_test_gen.xray_client — fully local (Jira is mocked, no network)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest import mock

import pytest

from ai_test_gen import xray_client
from ai_test_gen.config import Config
from ai_test_gen.models import ManualTestCase

# --- _strip_adf -------------------------------------------------------------


def test_strip_adf_none_returns_empty():
    assert xray_client._strip_adf(None) == ""


def test_strip_adf_passes_through_strings():
    assert xray_client._strip_adf("plain text") == "plain text"


def test_strip_adf_flattens_nested_adf():
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "World"}]},
        ],
    }
    assert xray_client._strip_adf(adf) == "Hello\nWorld"


# --- _parse_manual_steps ----------------------------------------------------


def test_parse_manual_steps_list_of_objects():
    raw = [
        {"step": "Go to /login", "data": "", "result": "Login form visible"},
        {"step": "Submit valid creds", "data": "u/p", "result": "Dashboard shown"},
    ]
    steps = xray_client._parse_manual_steps(raw)
    assert [s.action for s in steps] == ["Go to /login", "Submit valid creds"]
    assert [s.data for s in steps] == ["", "u/p"]  # the data cell is kept, not dropped
    assert [s.expected for s in steps] == ["Login form visible", "Dashboard shown"]


def test_parse_manual_steps_flattens_adf_values_and_missing_keys():
    # action is an ADF dict; no result key present.
    raw = [
        {"step": {"type": "doc", "content": [{"type": "text", "text": "Click"}]}},
    ]
    steps = xray_client._parse_manual_steps(raw)
    assert [s.action for s in steps] == ["Click"]
    assert [s.expected for s in steps] == [""]


@pytest.mark.parametrize("raw", [None, "", {}, 42])
def test_parse_manual_steps_non_list_returns_empty(raw):
    assert xray_client._parse_manual_steps(raw) == []


# --- fetch() wiring (Jira mocked, Server/DC path) ---------------------------


def test_fetch_server_returns_populated_test_case(monkeypatch):
    monkeypatch.delenv("XRAY_STEPS_FIELD_ID", raising=False)
    config = SimpleNamespace(
        xray_is_cloud=False,
        jira_base_url="https://jira.internal",
        jira_email="qa.bot",
        jira_token="fake-pat",
    )
    canned_issue = {
        "fields": {
            "summary": "Login happy path",
            "description": "User can log in with valid credentials.",
            "labels": ["smoke", "auth"],
        }
    }
    raven_steps = [
        {
            "id": 1,
            "index": 1,
            "step": {"raw": "Navigate to /login", "rendered": "<p>Navigate to /login</p>"},
            "data": {"raw": "", "rendered": ""},
            "result": {"raw": "Login form is visible", "rendered": "<p>...</p>"},
        },
        {
            "id": 2,
            "index": 2,
            "step": {"raw": "Submit valid credentials", "rendered": "<p>...</p>"},
            "data": {"raw": "u/p", "rendered": "u/p"},
            "result": {"raw": "Redirected to /dashboard", "rendered": "<p>...</p>"},
        },
    ]

    with mock.patch("ai_test_gen.xray_client.Jira") as mock_jira_cls:
        jira = mock_jira_cls.return_value
        jira.issue.return_value = canned_issue
        jira.get.return_value = raven_steps  # Xray Raven steps endpoint
        result = xray_client.XrayClient(cast(Config, config)).fetch("QA-1234")

    assert isinstance(result, ManualTestCase)
    assert result.key == "QA-1234"
    assert result.title == "Login happy path"
    assert result.description == "User can log in with valid credentials."
    assert [s.action for s in result.steps] == ["Navigate to /login", "Submit valid credentials"]
    assert [s.data for s in result.steps] == ["", "u/p"]  # per-step data cell kept
    assert [s.expected for s in result.steps] == [
        "Login form is visible",
        "Redirected to /dashboard",
    ]
    assert result.labels == ["smoke", "auth"]
    jira.issue.assert_called_once_with("QA-1234", expand="names")
    jira.get.assert_called_once_with("rest/raven/1.0/api/test/QA-1234/step")


def test_fetch_server_falls_back_to_custom_field_when_raven_empty(monkeypatch):
    # Raven returns no steps -> parse the "Manual Test Steps" custom field, whose
    # Server/DC shape nests the cells under "fields" (action / data / expected_result).
    monkeypatch.delenv("XRAY_STEPS_FIELD_ID", raising=False)
    config = SimpleNamespace(
        xray_is_cloud=False,
        jira_base_url="https://jira.internal",
        jira_email="qa.bot",
        jira_token="fake-pat",
    )
    canned_issue = {
        "fields": {
            "summary": "Steps only in the custom field",
            "customfield_11006": [
                {
                    "id": 1,
                    "index": 1,
                    "fields": {"action": "Open app", "data": "", "expected_result": "App loads"},
                },
            ],
        }
    }
    with mock.patch("ai_test_gen.xray_client.Jira") as mock_jira_cls:
        jira = mock_jira_cls.return_value
        jira.issue.return_value = canned_issue
        jira.get.return_value = []  # Raven yields nothing
        result = xray_client.XrayClient(cast(Config, config)).fetch("QA-2")

    assert [s.action for s in result.steps] == ["Open app"]
    assert [s.expected for s in result.steps] == ["App loads"]


def test_parse_manual_steps_nested_fields_shape():
    raw = [
        {
            "id": 1,
            "index": 1,
            "fields": {"action": "Click X", "data": "row 7", "expected_result": "Y is shown"},
        },
    ]
    steps = xray_client._parse_manual_steps(raw)
    assert [s.action for s in steps] == ["Click X"]
    assert [s.data for s in steps] == ["row 7"]  # nested-shape data cell kept
    assert [s.expected for s in steps] == ["Y is shown"]


def test_cell_text_handles_raw_rendered_string_and_adf():
    assert xray_client._cell_text({"raw": "plain", "rendered": "<p>plain</p>"}) == "plain"
    assert xray_client._cell_text("just text") == "just text"
    assert xray_client._cell_text(None) == ""
    adf = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hi"}]}],
    }
    assert xray_client._cell_text(adf) == "Hi"


def test_fetch_server_raises_clear_error_on_dict_error_payload():
    # Jira can return a dict-shaped error payload instead of an issue; _get_issue
    # must raise a clear, key-named RuntimeError rather than a bare KeyError when a
    # caller reaches for issue["fields"].
    config = SimpleNamespace(
        xray_is_cloud=False,
        jira_base_url="https://jira.internal",
        jira_email="qa.bot",
        jira_token="fake-pat",
    )
    error_payload = {
        "errorMessages": ["Issue does not exist or you do not have permission to see it."],
        "errors": {},
    }

    with mock.patch("ai_test_gen.xray_client.Jira") as mock_jira_cls:
        mock_jira_cls.return_value.issue.return_value = error_payload
        with pytest.raises(RuntimeError, match="QA-9999"):
            xray_client.XrayClient(cast(Config, config)).fetch("QA-9999")


def test_diagnose_steps_reports_fields_and_raven(monkeypatch):
    # diagnose_steps must surface the configured field's value, step-named fields,
    # populated custom fields (skipping empty ones), and the Raven endpoint results
    # — all without raising, so it's usable to pin the steps source on the laptop.
    monkeypatch.delenv("XRAY_STEPS_FIELD_ID", raising=False)
    config = SimpleNamespace(
        xray_is_cloud=False,
        jira_base_url="https://jira.internal",
        jira_email="qa.bot",
        jira_token="fake-pat",
    )
    issue = {
        "names": {"customfield_11006": "Manual Test Steps", "summary": "Summary"},
        "fields": {
            "summary": "Login happy path",
            "customfield_11006": None,  # configured field is empty on this tenant
            "customfield_12000": [{"step": "Navigate"}],  # steps actually live here
        },
    }
    with mock.patch("ai_test_gen.xray_client.Jira") as mock_jira_cls:
        jira = mock_jira_cls.return_value
        jira.issue.return_value = issue
        jira.get.return_value = [{"id": 1, "step": "Navigate", "result": "OK"}]
        out = xray_client.XrayClient(cast(Config, config)).diagnose_steps("QA-1")

    assert out["title"] == "Login happy path"
    assert out["configured_steps_field_id"] == "customfield_11006"
    assert out["configured_steps_field_value"] is None
    assert "customfield_11006" in out["step_named_fields"]
    populated_ids = [f["id"] for f in out["populated_custom_fields"]]
    assert "customfield_12000" in populated_ids
    assert "customfield_11006" not in populated_ids  # empty field skipped
    assert len(out["raven_attempts"]) == 2  # both endpoints attempted
