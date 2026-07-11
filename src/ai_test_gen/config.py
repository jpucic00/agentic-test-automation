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

# Default KB-seeding test-discovery marker (RETRIEVAL_MEMORY_PLAN.md §1.13/§5.1):
# a method whose decoration zone matches this regex is a test, and group(1)
# captures the linked Xray key. The company's Selenium suite is fully annotated
# with @Xray(testCase = "KEY"); other corpora override via TEST_MARKER_REGEX (the
# pattern MUST keep exactly one capture group for the key).
DEFAULT_TEST_MARKER_REGEX = r'@Xray\s*\(\s*testCase\s*=\s*"([^"]+)"\s*\)'


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


def _distiller_extra_body() -> dict[str, object] | None:
    """Optional JSON object from ``DISTILLER_EXTRA_BODY``, merged into every seeding
    agent request body (Mapper + Distiller share the serving path).

    The escape hatch for gateway serving quirks that only bite the offline seeding
    loop: pin a provider on a load-balanced gateway (e.g. OpenRouter's ``provider``
    routing — live 2026-07-11: one provider in its gemma-4 pool returns tool calls
    as empty turns), or pass vLLM extras like ``chat_template_kwargs``. Fails fast
    on anything that isn't a JSON object — a typo'd body silently dropped would
    masquerade as "the pin doesn't work".
    """
    raw = os.environ.get("DISTILLER_EXTRA_BODY", "").strip()
    if not raw:
        return None
    import json

    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"DISTILLER_EXTRA_BODY is not valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"DISTILLER_EXTRA_BODY must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _distiller_mode() -> Literal["agentic", "two-call"]:
    """Distillation mode from ``DISTILLER_MODE``: 'agentic' (default) or 'two-call'.

    'two-call' is the degraded mode for a gateway that fails the tool-loop serving
    check (tool calls returned as text): the Distiller then never uses tools — one
    structured call requests the files it needs, the code reads them, a second
    structured call produces the plan. Fails fast on any other value.
    """
    mode = os.environ.get("DISTILLER_MODE", "agentic").strip().lower()
    if mode == "agentic":
        return "agentic"
    if mode == "two-call":
        return "two-call"
    raise RuntimeError(
        f"DISTILLER_MODE={mode!r} is not valid; use 'agentic' (repo-exploring agent) or "
        "'two-call' (degraded no-tools mode for gateways that fail the tool-loop check)."
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


# API-key placeholder for a keyless overridden Planner endpoint (e.g. a local
# Ollama-style server that ignores the key). Used only when PLANNER_LLM_BASE_URL is
# set without PLANNER_LLM_API_KEY — the shared gateway key is NEVER sent to a
# different host. OpenAIProvider requires a non-empty api_key, hence a placeholder.
_KEYLESS_API_KEY_PLACEHOLDER = "not-needed"


def _planner_endpoint(shared_base_url: str, shared_api_key: str) -> tuple[str, str]:
    """Resolve the Planner's ``(base_url, api_key)``, defaulting to the shared gateway.

    ``PLANNER_LLM_BASE_URL`` / ``PLANNER_LLM_API_KEY`` point ONLY the Planner at a
    separately-hosted OpenAI-compatible model (every other agent stays on the shared
    gateway). Resolution:

    - Neither set → the shared gateway ``(base_url, api_key)`` (unchanged behavior).
    - ``PLANNER_LLM_API_KEY`` set → that key (with the override base URL if given, else
      the shared one).
    - Base overridden without a key → the override base + a keyless placeholder, so a
      keyless endpoint (e.g. a local Ollama server) works and the shared gateway key is
      never sent to another host.
    """
    override_base = os.environ.get("PLANNER_LLM_BASE_URL", "").strip()
    override_key = os.environ.get("PLANNER_LLM_API_KEY", "").strip()
    if not override_base and not override_key:
        return shared_base_url, shared_api_key
    base_url = override_base or shared_base_url
    if override_key:
        return base_url, override_key
    return base_url, _KEYLESS_API_KEY_PLACEHOLDER


@dataclass(frozen=True)
class Config:
    # LLM gateway
    llm_base_url: str
    llm_api_key: str
    # Optional Planner-only endpoint override (PLANNER_LLM_BASE_URL / PLANNER_LLM_API_KEY):
    # point JUST the Planner at a separately-hosted OpenAI-compatible model while every
    # other agent stays on {llm_base_url}. Both resolve to the shared gateway when unset;
    # a base override with no key yields a keyless placeholder (see _planner_endpoint).
    planner_base_url: str
    planner_api_key: str
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

    # Retrieval memory (optional, OFF by default — RETRIEVAL_MEMORY_PLAN.md). When
    # rag_enabled is False nothing below is consulted and no rag/ module (or qdrant)
    # is imported — the pipeline stays byte-identical. kb_path hosts the EMBEDDED
    # (local-mode) vector DB: a directory, not a server. The embedding + rerank
    # models resolve on the same gateway (rag/embeddings.py); the reranker default
    # is the cross-encoder zerank-1-small (bge-reranker-v2-m3 is the measured A/B
    # alternative via RERANKER_MODEL), rerank_endpoint overrides where /rerank
    # lives outside {llm_base_url}.
    rag_enabled: bool = False
    kb_path: Path = PROJECT_ROOT / "qdrant_storage"
    embedding_model: str = "mxbai-embed-large"
    reranker_model: str = "zeroentropy/zerank-1-small"
    rerank_endpoint: str | None = None
    # Per-record word budget for the Planner hint block (§1.19 / §6): the total
    # word count of all compact hints (title + flow + outcome + ~4 selectors) is
    # capped here. The best-match record always fits; later ones fill the remainder.
    # 250 words ≈ 2–4 records; raise if the Planner model handles a larger context
    # well (env: RAG_HINT_WORD_BUDGET).
    rag_hint_word_budget: int = 250
    # Offline KB seeding only (never part of the run loop). Regex that marks a
    # corpus test for discovery: a method whose decoration zone matches is one
    # record, and group(1) is its Xray key (RETRIEVAL_MEMORY_PLAN.md §5.1).
    test_marker_regex: str = DEFAULT_TEST_MARKER_REGEX
    # Model the agentic Distiller uses to reconstruct plans from corpus code
    # (offline seeding). Defaults to GENERATOR_MODEL (a code-reading class whose id
    # is valid on whichever gateway this .env targets).
    distiller_model: str = "mistralai/devstral-small-2-2512"
    # Per-exploration tool-call budget for the offline seeding agents (Mapper +
    # Distiller): pydantic-ai's UsageLimits.request_limit, bounding how many
    # read/search/list round-trips one map or one per-test distillation may spend.
    distiller_request_limit: int = 40
    # How the Distiller reaches the corpus: 'agentic' (read/search/list tools) or
    # 'two-call' — the degraded mode for a gateway that fails the tool-loop serving
    # check (RETRIEVAL_MEMORY_PLAN.md §1.12): structured file-request call, code
    # reads the files, structured distill call. No tools ever cross the wire.
    distiller_mode: Literal["agentic", "two-call"] = "agentic"
    # Optional JSON object merged into every seeding-agent request body
    # (DISTILLER_EXTRA_BODY) — provider pinning on load-balanced gateways,
    # vLLM chat_template_kwargs, and similar serving workarounds.
    distiller_extra_body: dict[str, object] | None = None


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

    llm_base_url = _required("LLM_BASE_URL")
    llm_api_key = _required("LLM_API_KEY")
    # Planner-only endpoint override (see Config.planner_base_url). Unset → shared gateway.
    planner_base_url, planner_api_key = _planner_endpoint(llm_base_url, llm_api_key)

    return Config(
        gitlab_enabled=gitlab_enabled,
        testcase_source=testcase_source,
        local_testcase_dir=local_testcase_dir,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        planner_base_url=planner_base_url,
        planner_api_key=planner_api_key,
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
        # Retrieval memory — same truthy set as GITLAB_ENABLED, but defaults OFF.
        rag_enabled=os.environ.get("RAG_ENABLED", "false").strip().lower()
        in {"1", "true", "yes", "on"},
        kb_path=_resolve_under_root(os.environ.get("KB_PATH", "qdrant_storage")),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "mxbai-embed-large"),
        reranker_model=os.environ.get("RERANKER_MODEL", "zeroentropy/zerank-1-small"),
        rerank_endpoint=os.environ.get("RERANK_ENDPOINT") or None,
        rag_hint_word_budget=_positive_int("RAG_HINT_WORD_BUDGET", default=250),
        test_marker_regex=os.environ.get("TEST_MARKER_REGEX") or DEFAULT_TEST_MARKER_REGEX,
        # Unset → follow the generator model: same code-reading class, and the id is
        # always valid on whichever gateway this .env targets.
        distiller_model=os.environ.get("DISTILLER_MODEL")
        or os.environ.get("GENERATOR_MODEL", "mistralai/devstral-small-2-2512"),
        distiller_request_limit=_positive_int("DISTILLER_REQUEST_LIMIT", default=40),
        distiller_mode=_distiller_mode(),
        distiller_extra_body=_distiller_extra_body(),
    )


def _positive_int(name: str, *, default: int) -> int:
    """A positive-integer env var (``name``), falling back to ``default``.

    Unset / non-numeric / non-positive → ``default`` (a typo'd budget should not
    silently disable the exploration; it just keeps the shipped bound).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default
