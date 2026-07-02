"""Centralized configuration with a fail-fast prod-URL guardrail.

All secrets and environment-specific values live here. ``load_config()`` reads the
environment (loading a local ``.env`` first, if present) and returns a frozen
``Config``. Before returning it asserts that ``STAGING_BASE_URL`` points at a
non-production host (see ``_assert_non_prod_url``) so a misconfigured URL fails
immediately — before any browser is launched or any model is contacted. The
pipeline drives a real browser and runs generated tests against that URL; this is
a hard architectural constraint: staging only, never production.

Implements AI_TEST_GENERATION_GUIDE.md §3.4 + §3.5b.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Substring markers (case-insensitive) that identify a NON-production host. The
# guardrail is fail-closed: STAGING_BASE_URL's host must contain at least one of
# these, or it is treated as a suspected production URL and load_config() raises.
# Kept deliberately tight/collision-light — extend per-team via the
# NON_PROD_URL_MARKERS env var rather than shipping broad tokens like "dev"/"test"
# that also appear inside prod hostnames (e.g. "latest." contains "test").
DEFAULT_NON_PROD_MARKERS: tuple[str, ...] = ("localhost", "127.0.0.1", "staging", "qa", "demo")


class ProductionURLError(RuntimeError):
    """Raised when STAGING_BASE_URL looks like a production host.

    Subclasses RuntimeError so callers that broadly catch config failures still
    catch it, while tests (and targeted handlers) can match it precisely.
    """


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return val


def _required_if(name: str, *, required: bool) -> str | None:
    """``_required(name)`` when ``required`` (GitLab enabled), else the raw value or None."""
    return _required(name) if required else os.environ.get(name)


def _non_prod_markers() -> tuple[str, ...]:
    """Default markers plus any added via the NON_PROD_URL_MARKERS env var."""
    extra = os.environ.get("NON_PROD_URL_MARKERS", "")
    parsed = tuple(m.strip().lower() for m in extra.split(",") if m.strip())
    return DEFAULT_NON_PROD_MARKERS + parsed


def _assert_non_prod_url(url: str, markers: Sequence[str]) -> None:
    """Fail-closed guardrail: raise unless ``url``'s host contains a non-prod marker."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise ProductionURLError(
            f"STAGING_BASE_URL={url!r} has no parseable host — include the scheme, "
            "e.g. https://staging.example.com"
        )
    if not any(marker in host for marker in markers):
        raise ProductionURLError(
            f"Refusing to run: STAGING_BASE_URL host {host!r} contains none of the "
            f"non-prod markers {tuple(markers)} and is treated as a suspected "
            "production URL. The pipeline is staging-only. If this IS a non-prod "
            "environment, add its marker to NON_PROD_URL_MARKERS in .env."
        )


def _testcase_source() -> Literal["xray", "local"]:
    """Test-case source from ``TESTCASE_SOURCE``: 'xray' (default) or 'local'.

    Fails fast on any other value — a typo'd source that silently fell back to Xray
    would surface as confusing "missing JIRA_*" errors during a local demo run.
    """
    source = os.environ.get("TESTCASE_SOURCE", "xray").strip().lower()
    if source == "xray":
        return "xray"
    if source == "local":
        return "local"
    raise RuntimeError(
        f"TESTCASE_SOURCE={source!r} is not valid; use 'xray' (live Jira/Xray) or "
        "'local' (raw-Xray-shaped JSON from LOCAL_TESTCASE_DIR)."
    )


def _resolve_under_root(raw: str) -> Path:
    """Expand ``~`` and resolve ``raw``; a relative path is taken relative to PROJECT_ROOT.

    So an env value like ``packages/demo-notes-app/test-cases`` works regardless of the
    process's current working directory.
    """
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _call_budget(env_var: str, what: str) -> int:
    """Tri-state per-agent-run call budget from ``env_var`` (0 = feature off).

    Mirrors the env-knob style of ``agent_request_limit`` / ``reasoning_effort``:
    unset / ``false`` / ``0`` / ``off`` / ``no`` → 0 (disabled); a positive integer → that cap.
    Any other value fails fast — a typo that silently disabled the feature would masquerade as
    "the feature isn't helping". ``what`` names the capped thing in the error message.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return 0
    value = raw.strip().lower()
    if value in ("", "false", "0", "off", "no"):
        return 0
    try:
        n = int(value)
    except ValueError:
        raise RuntimeError(
            f"{env_var}={raw!r} is not valid; use 'false' to disable or a positive "
            f"integer (max {what} per agent run)."
        ) from None
    if n < 0:
        raise RuntimeError(
            f"{env_var}={raw!r} must be a positive integer (or 'false' to disable)."
        )
    return n


def _vision_max_calls() -> int:
    """Max Vision Aid calls per agent run from ``AGENT_VISION`` (0 = feature off).

    Single shared knob for BOTH browser agents — the Planner and the Healer each get this many
    ``inspect_screen`` calls per run (per planning run; per heal attempt).
    """
    return _call_budget("AGENT_VISION", "vision calls")


def _dom_probe_max_calls() -> int:
    """Max DOM-probe calls per agent run from ``AGENT_DOM_PROBE`` (0 = feature off).

    Single shared knob for BOTH browser agents — the Planner and the Healer each get this many
    ``probe_dom`` calls per run (per planning run; per heal attempt). Unlike the Vision Aid it
    needs no extra model (the probe never calls an LLM), but it ships OFF by default like every
    optional sensor so a default run's prompts and toolset stay byte-identical.
    """
    return _call_budget("AGENT_DOM_PROBE", "DOM-probe calls")


@dataclass(frozen=True)
class Config:
    # LLM gateway
    llm_base_url: str
    llm_api_key: str
    planner_model: str
    generator_model: str
    healer_model: str
    # Optional Vision Aid sensor shared by the Planner AND Healer (agents/vision.py +
    # agents/_vision_aid.py inspect_screen). vision_max_calls == 0 means the feature is OFF
    # (AGENT_VISION unset or false); >0 = per-agent-run call cap.
    vision_model: str
    vision_max_calls: int
    # Optional DOM Probe shared by the Planner AND Healer (agents/_dom_probe.py probe_dom):
    # read-only recon of elements the a11y snapshot can't name. 0 = OFF (AGENT_DOM_PROBE unset
    # or false); >0 = per-agent-run call cap. No LLM involved — fixed JS via the MCP server.
    dom_probe_max_calls: int

    # Test-case source: "xray" (live Jira/Xray) or "local" (raw-Xray-shaped JSON on disk)
    testcase_source: Literal["xray", "local"]
    local_testcase_dir: Path | None  # required when testcase_source == "local"

    # Jira / Xray. Optional: only the "xray" source needs them, so they are None in local
    # mode. XrayClient — constructed only for the xray source — asserts they are present.
    jira_base_url: str | None
    jira_email: str | None
    jira_token: str | None
    xray_is_cloud: bool  # True for Xray Cloud, False for Server/DC

    # Staging app. Username/password are LEGACY: the pipeline authenticates from the
    # test users in project_context.md; only scripts/save_auth_state.py reads these.
    staging_base_url: str
    staging_username: str | None
    staging_password: str | None

    # GitLab (optional — GITLAB_ENABLED=false runs the pipeline without opening an MR)
    gitlab_enabled: bool
    gitlab_base_url: str | None
    gitlab_token: str | None
    gitlab_project_id: str | None  # e.g. "group/playwright-tests" or numeric ID
    gitlab_target_branch: str  # usually "main"

    # Paths
    output_dir: Path
    plans_dir: Path
    tests_dir: Path
    snapshots_dir: Path
    project_context_path: Path
    project_map_path: Path


def load_config() -> Config:
    load_dotenv()  # Load .env if present; does not override real env (override=False).

    # Guard FIRST — fail before any filesystem/model side effects.
    staging_base_url = _required("STAGING_BASE_URL")
    _assert_non_prod_url(staging_base_url, _non_prod_markers())

    output_dir = PROJECT_ROOT / "output"
    plans_dir = output_dir / "plans"
    tests_dir = output_dir / "tests"
    snapshots_dir = output_dir / "snapshots"
    for d in (plans_dir, tests_dir, snapshots_dir):
        d.mkdir(parents=True, exist_ok=True)

    # GitLab is optional: GITLAB_ENABLED=false lets the container run end-to-end
    # (Xray → plan → generate → run → heal) without GITLAB_* set, skipping the MR.
    # Default true preserves the laptop/CI behavior (MR opened against GitLab).
    gitlab_enabled = os.environ.get("GITLAB_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # Test-case source. "local" reads raw-Xray-shaped JSON from LOCAL_TESTCASE_DIR and
    # needs no Jira/Xray; "xray" (default) requires the JIRA_* vars below.
    testcase_source = _testcase_source()
    local_dir_raw = (
        _required("LOCAL_TESTCASE_DIR")
        if testcase_source == "local"
        else os.environ.get("LOCAL_TESTCASE_DIR")
    )
    local_testcase_dir = _resolve_under_root(local_dir_raw) if local_dir_raw else None

    # Human-authored context files default to the repo root but are overridable, so a
    # different app under test (e.g. the bundled demo) can point at its own committed
    # context files without overwriting the root ones.
    context_override = os.environ.get("PROJECT_CONTEXT_PATH")
    map_override = os.environ.get("PROJECT_MAP_PATH")
    project_context_path = (
        _resolve_under_root(context_override)
        if context_override
        else PROJECT_ROOT / "project_context.md"
    )
    project_map_path = (
        _resolve_under_root(map_override) if map_override else PROJECT_ROOT / "project_map.md"
    )

    return Config(
        gitlab_enabled=gitlab_enabled,
        testcase_source=testcase_source,
        local_testcase_dir=local_testcase_dir,
        llm_base_url=_required("LLM_BASE_URL"),
        llm_api_key=_required("LLM_API_KEY"),
        planner_model=os.environ.get("PLANNER_MODEL", "openai/gpt-oss-120b"),
        generator_model=os.environ.get("GENERATOR_MODEL", "mistralai/devstral-small-2-2512"),
        healer_model=os.environ.get("HEALER_MODEL", "openai/gpt-oss-120b"),
        vision_model=os.environ.get("VISION_MODEL", "mistralai/devstral-small-2-2512"),
        vision_max_calls=_vision_max_calls(),
        dom_probe_max_calls=_dom_probe_max_calls(),
        jira_base_url=_required_if("JIRA_BASE_URL", required=testcase_source == "xray"),
        jira_email=_required_if("JIRA_EMAIL", required=testcase_source == "xray"),
        jira_token=_required_if("JIRA_TOKEN", required=testcase_source == "xray"),
        xray_is_cloud=os.environ.get("XRAY_IS_CLOUD", "true").lower() == "true",
        staging_base_url=staging_base_url,
        # Optional: only the legacy save_auth_state.py needs these; the pipeline's
        # test logins come from project_context.md, so a missing value is fine.
        staging_username=os.environ.get("STAGING_USERNAME"),
        staging_password=os.environ.get("STAGING_PASSWORD"),
        gitlab_base_url=_required_if("GITLAB_BASE_URL", required=gitlab_enabled),
        gitlab_token=_required_if("GITLAB_TOKEN", required=gitlab_enabled),
        gitlab_project_id=_required_if("GITLAB_PROJECT_ID", required=gitlab_enabled),
        gitlab_target_branch=os.environ.get("GITLAB_TARGET_BRANCH", "main"),
        output_dir=output_dir,
        plans_dir=plans_dir,
        tests_dir=tests_dir,
        snapshots_dir=snapshots_dir,
        project_context_path=project_context_path,
        project_map_path=project_map_path,
    )
