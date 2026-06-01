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


@dataclass(frozen=True)
class Config:
    # LLM gateway
    llm_base_url: str
    llm_api_key: str
    planner_model: str
    generator_model: str
    healer_model: str

    # Jira / Xray
    jira_base_url: str
    jira_email: str
    jira_token: str
    xray_is_cloud: bool  # True for Xray Cloud, False for Server/DC

    # Staging app
    staging_base_url: str
    staging_username: str
    staging_password: str

    # GitLab
    gitlab_base_url: str
    gitlab_token: str
    gitlab_project_id: str  # e.g. "group/playwright-tests" or numeric ID
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

    return Config(
        llm_base_url=_required("LLM_BASE_URL"),
        llm_api_key=_required("LLM_API_KEY"),
        planner_model=os.environ.get("PLANNER_MODEL", "openai/gpt-oss-120b"),
        generator_model=os.environ.get("GENERATOR_MODEL", "mistralai/devstral-small-2-2512"),
        healer_model=os.environ.get("HEALER_MODEL", "openai/gpt-oss-120b"),
        jira_base_url=_required("JIRA_BASE_URL"),
        jira_email=_required("JIRA_EMAIL"),
        jira_token=_required("JIRA_TOKEN"),
        xray_is_cloud=os.environ.get("XRAY_IS_CLOUD", "true").lower() == "true",
        staging_base_url=staging_base_url,
        staging_username=_required("STAGING_USERNAME"),
        staging_password=_required("STAGING_PASSWORD"),
        gitlab_base_url=_required("GITLAB_BASE_URL"),
        gitlab_token=_required("GITLAB_TOKEN"),
        gitlab_project_id=_required("GITLAB_PROJECT_ID"),
        gitlab_target_branch=os.environ.get("GITLAB_TARGET_BRANCH", "main"),
        output_dir=output_dir,
        plans_dir=plans_dir,
        tests_dir=tests_dir,
        snapshots_dir=snapshots_dir,
        project_context_path=PROJECT_ROOT / "project_context.md",
        project_map_path=PROJECT_ROOT / "project_map.md",
    )
