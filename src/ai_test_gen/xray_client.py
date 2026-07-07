"""Fetch test cases from Jira/Xray and normalize to ManualTestCase.

Supports both Xray Cloud (GraphQL/custom fields) and Xray Server/Data Center
(REST) behind a single ``XrayClient.fetch(issue_key) -> ManualTestCase``
interface, selected by ``config.xray_is_cloud`` (AI_TEST_GENERATION_GUIDE.md §3.6).

Server/DC specifics were resolved in Phase 0 (task j1cnfng, runtime-confirmed on
the company laptop) and differ from the guide template:

- Auth is a **Bearer Personal Access Token** (``JIRA_TOKEN``). The atlassian
  client sends Bearer only when username/password are omitted.
- Manual test steps come from the **Xray Raven REST API**
  (``/rest/raven/1.0/api/test/<KEY>/step``), an array of ``{step, data, result}``
  cells (each a ``{raw, rendered}`` pair). The "Manual Test Steps" custom field
  (default ``customfield_11006``, override via ``XRAY_STEPS_FIELD_ID``) is parsed
  as a fallback. Phase 0 (j1cnfng) found the field id; runtime (2026-05-30) showed
  its value isn't the flat ``{step, result}`` shape first assumed (cells are nested
  under ``fields``), so the Raven API — whose structure is canonical — is primary.
"""
from __future__ import annotations

import os
import re
from typing import Any

from atlassian import Jira

from . import mtls
from .config import Config
from .models import ManualTestCase

# Server/DC custom field holding the manual test steps. Phase 0 finding (j1cnfng):
# "Manual Test Steps" on the company tenant. Override per adopter via env.
DEFAULT_STEPS_FIELD_ID = "customfield_11006"


class XrayClient:
    def __init__(self, config: Config) -> None:
        # The xray source needs Jira creds; in local mode they are None and this client
        # is never constructed (mirrors GitLabClient when GITLAB_ENABLED=false). Fail fast
        # with an actionable message instead of deep inside the atlassian client.
        if not (config.jira_base_url and config.jira_email and config.jira_token):
            raise RuntimeError(
                "The 'xray' test-case source requires JIRA_BASE_URL, JIRA_EMAIL, and "
                "JIRA_TOKEN. Set TESTCASE_SOURCE=local to read test cases from local JSON."
            )
        self.config = config
        # Read after config has loaded .env. Default keeps the company tenant
        # working with zero extra config; the env var keeps the scaffold shareable.
        self.steps_field_id = os.environ.get("XRAY_STEPS_FIELD_ID", DEFAULT_STEPS_FIELD_ID)
        self.jira = _build_jira(config)

    def fetch(self, issue_key: str) -> ManualTestCase:
        if self.config.xray_is_cloud:
            return self._fetch_cloud(issue_key)
        return self._fetch_server(issue_key)

    def diagnose_steps(self, issue_key: str) -> dict[str, Any]:
        """Diagnostic: reveal where this tenant's manual steps live (laptop only).

        Returns a JSON-able dict for ``scripts/test_xray.py --raw``. Shows the
        configured field's raw value, any step-named fields, every populated
        custom field, and what the Xray Raven REST endpoints return — enough to
        pin the Server/DC steps source in one run when ``fetch()`` yields no steps.
        """
        issue = self._get_issue(issue_key, expand="names")
        fields = issue["fields"]
        names = issue.get("names") or {}
        pattern = re.compile(r"step|manual|expected|action|result", re.I)

        def preview(value: Any, limit: int = 200) -> str:
            text = repr(value)
            return text if len(text) <= limit else text[:limit] + "…"

        step_named_fields = {
            fid: name
            for fid, name in names.items()
            if isinstance(name, str) and pattern.search(name)
        }
        populated_custom_fields = [
            {"id": fid, "name": names.get(fid), "preview": preview(value)}
            for fid, value in fields.items()
            if fid.startswith("customfield_") and value not in (None, "", [], {})
        ]
        raven_attempts: dict[str, Any] = {}
        for path in (
            f"rest/raven/1.0/api/test/{issue_key}/step",
            f"rest/raven/2.0/api/test/{issue_key}/steps",
        ):
            try:
                raven_attempts[path] = self.jira.get(path)
            except Exception as exc:
                raven_attempts[path] = f"ERROR: {type(exc).__name__}: {exc}"

        return {
            "issue_key": issue_key,
            "title": fields.get("summary"),
            "configured_steps_field_id": self.steps_field_id,
            "configured_steps_field_value": fields.get(self.steps_field_id),
            "step_named_fields": step_named_fields,
            "populated_custom_fields": populated_custom_fields,
            "raven_attempts": raven_attempts,
        }

    def _get_issue(self, issue_key: str, expand: str | None = None) -> dict[str, Any]:
        issue = self.jira.issue(issue_key, expand=expand)
        if not isinstance(issue, dict):
            raise RuntimeError(f"Jira returned no issue for {issue_key!r}")
        # Jira can return a dict-shaped error payload (e.g. {"errorMessages": [...]})
        # instead of an issue; guard here so every caller's issue["fields"] access
        # fails with a clear, key-named message rather than a bare KeyError.
        if not isinstance(issue.get("fields"), dict):
            raise RuntimeError(
                f"Jira response for {issue_key!r} has no 'fields' object "
                f"(keys: {sorted(issue)}) — likely an error payload, not an issue"
            )
        return issue

    def _fetch_server(self, issue_key: str) -> ManualTestCase:
        # Title/description/labels come from the standard issue fields; steps come
        # from the Xray Raven API (canonical Server/DC source), falling back to the
        # "Manual Test Steps" custom field if Raven returns nothing. expand=names so
        # diagnose_steps() can show the field's display name on the --raw smoke run.
        issue = self._get_issue(issue_key, expand="names")
        fields = issue["fields"]
        steps, expected_results = self._xray_server_steps(issue_key)
        if not steps:
            steps, expected_results = _parse_manual_steps(fields.get(self.steps_field_id))
        return ManualTestCase(
            key=issue_key,
            title=fields.get("summary") or "",
            description=_strip_adf(fields.get("description")),
            preconditions=[],
            steps=steps,
            expected_results=expected_results,
            labels=fields.get("labels") or [],
        )

    def _xray_server_steps(self, issue_key: str) -> tuple[list[str], list[str]]:
        """Fetch manual steps from the Xray Server/DC Raven REST API.

        ``GET /rest/raven/1.0/api/test/<KEY>/step`` returns an array of step
        objects: ``{"id", "index", "step": {"raw", "rendered"}, "data": {...},
        "result": {"raw", "rendered"}, "attachments": [...]}``. ``step`` is the
        action and ``result`` the expected result; each cell is flattened by
        ``_cell_text`` (preferring the plain ``raw`` value).
        """
        raw = self.jira.get(f"rest/raven/1.0/api/test/{issue_key}/step")
        if not isinstance(raw, list):
            return [], []
        steps: list[str] = []
        expected_results: list[str] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            steps.append(_cell_text(item.get("step")))
            expected_results.append(_cell_text(item.get("result")))
        return steps, expected_results

    def _fetch_cloud(self, issue_key: str) -> ManualTestCase:
        # Path exists for completeness; the company tenant is Server/DC, so this is
        # not exercised today (AC #3). Kept faithful to the guide template.
        issue = self._get_issue(issue_key)
        fields = issue["fields"]
        steps = self._xray_cloud_steps(issue_key)
        return ManualTestCase(
            key=issue_key,
            title=fields.get("summary") or "",
            description=_strip_adf(fields.get("description")),
            # Xray preconditions live in linked issues; fetch separately if needed.
            preconditions=[],
            steps=[s.get("action", "") for s in steps],
            expected_results=[s.get("result", "") for s in steps],
            labels=fields.get("labels") or [],
        )

    def _xray_cloud_steps(self, issue_key: str) -> list[dict[str, Any]]:
        """Best-effort Xray Cloud step extraction (placeholder, per §3.6).

        The proper Xray Cloud path authenticates with separate API client
        credentials and queries GraphQL at ``/api/v2/graphql``. For the PoC we read
        common step custom fields off the issue; replace with a real GraphQL call
        when the Cloud flavor is actually needed.
        """
        issue = self._get_issue(issue_key)
        for cf in ("customfield_10100", "customfield_10200"):
            steps = issue["fields"].get(cf)
            if steps:
                return [
                    {"action": s.get("step", ""), "result": s.get("result", "")}
                    for s in steps
                ]
        return []


def _build_jira(config: Config) -> Jira:
    # jira_* are Optional on Config (None in local mode) but always set here: the only
    # caller, XrayClient.__init__, guards on them first. Assert to narrow the type for
    # the Jira(...) construction below.
    assert (
        config.jira_base_url is not None
        and config.jira_email is not None
        and config.jira_token is not None
    ), "XrayClient.__init__ guarantees JIRA_* are set before _build_jira runs"
    # Share the gateway proxy/CA/mTLS policy (direct over VPN, ignoring env
    # HTTP(S)_PROXY unless USE_HTTP_PROXY=true). atlassian-python-api uses the passed
    # session as-is, preserving its trust_env / verify / cert.
    session = mtls.build_requests_session()
    # A healthy tenant answers in seconds; 30s (down from the library's 75) makes a
    # black-holed route fail fast enough that per-key tolerant callers (KB seeding)
    # stay visibly alive instead of appearing hung.
    if config.xray_is_cloud:
        # Xray Cloud: HTTP Basic with the Atlassian account email + API token.
        return Jira(
            url=config.jira_base_url,
            username=config.jira_email,
            password=config.jira_token,
            cloud=True,
            session=session,
            timeout=30,
        )
    # Xray Server/DC: Bearer PAT. The atlassian client uses Bearer only when
    # username/password are omitted. If a Server/DC instance needs Basic instead,
    # pass username=config.jira_email, password=config.jira_token.
    return Jira(
        url=config.jira_base_url, token=config.jira_token, cloud=False, session=session, timeout=30
    )


def _parse_manual_steps(raw: Any) -> tuple[list[str], list[str]]:
    """Fallback parser for the "Manual Test Steps" custom-field value.

    The value is a list of step objects. Newer Xray Server shapes nest the cells
    under ``fields`` (``{"fields": {"action", "data", "expected_result"}}``);
    older/loose shapes put them at the top level. Each cell may be a plain string,
    a ``{"raw", "rendered"}`` pair, or an ADF dict — all handled by ``_cell_text``.
    """
    if not isinstance(raw, list):
        return [], []
    steps: list[str] = []
    expected_results: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cells = item["fields"] if isinstance(item.get("fields"), dict) else item
        steps.append(_first_cell(cells, ("action", "step", "field")))
        expected_results.append(
            _first_cell(cells, ("expected_result", "result", "expectedResult", "expected"))
        )
    return steps, expected_results


def _first_cell(cells: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Return the first present, non-empty cell among ``keys``, flattened to text."""
    for key in keys:
        value = cells.get(key)
        if value not in (None, ""):
            return _cell_text(value)
    return ""


def _cell_text(value: Any) -> str:
    """Flatten an Xray step cell to plain text.

    A cell may be a Raven ``{"raw", "rendered"}`` pair, a plain string, or an ADF
    dict. Prefer the plain ``raw`` value; fall back to ADF flattening.
    """
    if isinstance(value, dict):
        for key in ("raw", "rendered"):
            cell = value.get(key)
            if cell not in (None, ""):
                return _strip_adf(cell)
    return _strip_adf(value)


def _strip_adf(value: Any) -> str:
    """Atlassian Document Format -> plain text (lossy but adequate for PoC)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    # ADF is a nested JSON structure; grab all text nodes.
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node.get("text", ""))
            for child in node.get("content", []) or []:
                walk(child)

    walk(value)
    return "\n".join(out)
