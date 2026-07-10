"""Unit tests for the local (raw-Xray-shaped JSON) test-case source — fully offline.

Mirrors the real Server/DC Xray path: a file carries the Jira issue ``fields`` plus the
Raven step array, and is normalized via the same ``xray_client`` helpers, so a locally
sourced ``ManualTestCase`` is identical to a fetched one.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from ai_test_gen import local_testcases
from ai_test_gen.config import Config

# A complete raw-Xray-shaped payload: issue fields + Raven step rows ({raw, rendered} cells).
RAW_XRAY = {
    "key": "IGNORED-IN-FILE",
    "fields": {
        "summary": "Log in and create a note",
        "description": "Seeded user logs in and adds a note.",
        "labels": ["smoke", "notes"],
    },
    "steps": [
        {
            "id": 1,
            "index": 1,
            "step": {"raw": "Open the login page", "rendered": "<p>Open the login page</p>"},
            "data": {"raw": "", "rendered": ""},
            "result": {"raw": "Login form is shown", "rendered": "<p>Login form is shown</p>"},
        },
        {
            "id": 2,
            "index": 2,
            "step": {"raw": "Submit valid credentials", "rendered": "<p>...</p>"},
            "data": {"raw": "demo@demo.test / Passw0rd!", "rendered": "<p>...</p>"},
            "result": {"raw": "Notes page is shown", "rendered": "<p>...</p>"},
        },
    ],
}


def _local_cfg(cfg: Config, directory) -> Config:
    return dataclasses.replace(cfg, testcase_source="local", local_testcase_dir=directory)


def _write(directory, key: str, payload) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{key}.json").write_text(json.dumps(payload))


def test_loads_raw_xray_shape(cfg, tmp_path):
    _write(tmp_path, "NOTE-2", RAW_XRAY)
    tc = local_testcases.load_local_test_case(_local_cfg(cfg, tmp_path), "NOTE-2")
    assert tc.title == "Log in and create a note"
    assert tc.description == "Seeded user logs in and adds a note."
    assert tc.labels == ["smoke", "notes"]
    assert [s.action for s in tc.steps] == ["Open the login page", "Submit valid credentials"]
    assert [s.expected for s in tc.steps] == ["Login form is shown", "Notes page is shown"]
    # The data cell is KEPT on its own field, never folded into the action text.
    assert [s.data for s in tc.steps] == ["", "demo@demo.test / Passw0rd!"]
    assert all("demo@demo.test" not in s.action for s in tc.steps)


def test_key_is_forced_to_issue_key(cfg, tmp_path):
    # The file's own "key" is IGNORED-IN-FILE; the loaded key must match the filename/CLI arg.
    _write(tmp_path, "NOTE-2", RAW_XRAY)
    tc = local_testcases.load_local_test_case(_local_cfg(cfg, tmp_path), "NOTE-2")
    assert tc.key == "NOTE-2"


def test_cell_prefers_raw_over_rendered(cfg, tmp_path):
    _write(
        tmp_path,
        "T-1",
        {
            "fields": {"summary": "x"},
            "steps": [
                {
                    "step": {"raw": "RAW action", "rendered": "<p>RENDERED action</p>"},
                    "result": {"raw": "RAW result", "rendered": "<p>RENDERED result</p>"},
                }
            ],
        },
    )
    tc = local_testcases.load_local_test_case(_local_cfg(cfg, tmp_path), "T-1")
    assert [s.action for s in tc.steps] == ["RAW action"]
    assert [s.expected for s in tc.steps] == ["RAW result"]


def test_description_adf_is_flattened(cfg, tmp_path):
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "World"}]},
        ],
    }
    _write(tmp_path, "T-2", {"fields": {"summary": "s", "description": adf}, "steps": []})
    tc = local_testcases.load_local_test_case(_local_cfg(cfg, tmp_path), "T-2")
    assert tc.description == "Hello\nWorld"
    assert tc.steps == []


def test_issue_wrapper_is_tolerated(cfg, tmp_path):
    # A raw GET /issue/<KEY> response nests fields under "issue"; accept that too.
    _write(
        tmp_path,
        "T-3",
        {
            "issue": {"fields": {"summary": "Wrapped"}},
            "steps": [{"step": {"raw": "a"}, "result": {"raw": "b"}}],
        },
    )
    tc = local_testcases.load_local_test_case(_local_cfg(cfg, tmp_path), "T-3")
    assert tc.title == "Wrapped"
    assert [s.action for s in tc.steps] == ["a"]
    assert [s.expected for s in tc.steps] == ["b"]


def test_missing_file_raises_filenotfound(cfg, tmp_path):
    with pytest.raises(FileNotFoundError, match="NOPE"):
        local_testcases.load_local_test_case(_local_cfg(cfg, tmp_path), "NOPE")


def test_dir_none_raises(cfg):
    with pytest.raises(RuntimeError, match="LOCAL_TESTCASE_DIR"):
        local_testcases.load_local_test_case(_local_cfg(cfg, None), "NOTE-2")


def test_non_object_json_raises(cfg, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "BAD.json").write_text("[1, 2, 3]")
    with pytest.raises(RuntimeError, match="JSON object"):
        local_testcases.load_local_test_case(_local_cfg(cfg, tmp_path), "BAD")


def test_invalid_json_raises(cfg, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "BROKEN.json").write_text("{ not valid json ]")
    with pytest.raises(RuntimeError, match="valid JSON"):
        local_testcases.load_local_test_case(_local_cfg(cfg, tmp_path), "BROKEN")
