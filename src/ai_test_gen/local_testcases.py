"""Load test cases from local JSON files shaped like raw Xray REST payloads.

The "local" test-case source (``TESTCASE_SOURCE=local``) lets the pipeline run with no
Jira/Xray tenant — handy for demos and for evaluating the scaffold against the bundled
demo app. Each ``<LOCAL_TESTCASE_DIR>/<issue_key>.json`` file mirrors the two REST
payloads the Server/DC Xray path consumes:

- the Jira issue's ``fields`` (``summary``, ``description``, ``labels``), and
- the Xray Raven step array (``GET /rest/raven/1.0/api/test/<KEY>/step``): a list of
  ``{"step": {"raw", "rendered"}, "data": {...}, "result": {"raw", "rendered"}}`` rows.

It is normalized to ``ManualTestCase`` by the SAME helpers the live Server/DC path uses
(``xray_client._cell_text`` / ``_strip_adf``), so a locally-sourced case is byte-identical
to one fetched from Xray — only the transport differs (a file read, not an HTTP call).

Example file (``NOTE-2.json``)::

    {
      "key": "NOTE-2",
      "fields": {
        "summary": "Log in and create a note",
        "description": "Verifies a seeded user can log in and add a note.",
        "labels": ["smoke", "notes"]
      },
      "steps": [
        {"step": {"raw": "Open the app and click Login"},
         "data": {"raw": ""},
         "result": {"raw": "The login form is shown"}}
      ]
    }

An optional ``{"issue": {...}}`` wrapper is tolerated (``key`` / ``fields`` may live under
it), matching the shape of a raw ``GET /rest/api/2/issue/<KEY>`` response.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config
from .models import ManualStep, ManualTestCase
from .xray_client import _cell_text, _strip_adf


def load_local_test_case(config: Config, issue_key: str) -> ManualTestCase:
    """Read ``<local_testcase_dir>/<issue_key>.json`` and normalize it to a ManualTestCase."""
    if config.local_testcase_dir is None:
        raise RuntimeError(
            "TESTCASE_SOURCE=local requires LOCAL_TESTCASE_DIR to point at a directory of "
            "test-case JSON files."
        )
    path = config.local_testcase_dir / f"{issue_key}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"No local test case for {issue_key!r}: expected a file at {path}. "
            f"Available keys: {_available(config.local_testcase_dir)}"
        )
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"{path} must contain a JSON object (key / fields / steps), "
            f"got {type(raw).__name__}."
        )
    return _from_raw_xray(raw, issue_key)


def _from_raw_xray(raw: dict[str, Any], issue_key: str) -> ManualTestCase:
    """Normalize a raw-Xray-shaped dict to ManualTestCase (mirrors xray_client._fetch_server).

    ``key`` is forced to ``issue_key`` (the filename / CLI arg) so the loaded case and the
    pipeline's output artifacts always agree, even if the file's own ``key`` has drifted.
    """
    # Tolerate a {"issue": {...}} wrapper (a raw GET /issue/<KEY> response).
    issue = raw["issue"] if isinstance(raw.get("issue"), dict) else raw

    fields_obj = issue.get("fields")
    fields: dict[str, Any] = fields_obj if isinstance(fields_obj, dict) else {}

    # Steps live at the top level (the Raven array) or under the issue wrapper.
    rows_obj = raw.get("steps")
    if not isinstance(rows_obj, list):
        rows_obj = issue.get("steps")
    rows: list[Any] = rows_obj if isinstance(rows_obj, list) else []

    steps: list[ManualStep] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        steps.append(
            ManualStep(
                action=_cell_text(row.get("step")),
                data=_cell_text(row.get("data")),
                expected=_cell_text(row.get("result")),
            )
        )

    return ManualTestCase(
        key=issue_key,
        title=_strip_adf(fields.get("summary")),
        description=_strip_adf(fields.get("description")),
        # Xray keeps preconditions in linked issues; the live Server/DC path returns [] too.
        preconditions=[],
        steps=steps,
        labels=list(fields.get("labels") or []),
    )


def _available(directory: Path) -> str:
    """Comma-separated issue keys (the ``.json`` stems) in ``directory``, for error messages."""
    try:
        keys = sorted(p.stem for p in directory.glob("*.json"))
    except OSError:
        return "(could not list directory)"
    return ", ".join(keys) if keys else "(none)"
