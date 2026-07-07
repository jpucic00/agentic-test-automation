"""Unit tests for ai_test_gen.config — fully local (no network, no real .env)."""
from __future__ import annotations

import pytest

from ai_test_gen import config
from ai_test_gen.config import Config, ProductionURLError, load_config

# Complete, valid fake environment with a SAFE (non-prod) staging URL.
_FAKE_PASSWORD = "s3cr3t-staging-pw"
_BASE_ENV = {
    "LLM_BASE_URL": "https://gateway.internal/v1",
    "LLM_API_KEY": "fake-llm-key",
    "JIRA_BASE_URL": "https://jira.internal",
    "JIRA_EMAIL": "qa.bot@example.com",
    "JIRA_TOKEN": "fake-jira-token",
    "STAGING_BASE_URL": "https://staging.example.internal",
    "STAGING_USERNAME": "qa.bot",
    "STAGING_PASSWORD": _FAKE_PASSWORD,
    "GITLAB_BASE_URL": "https://gitlab.internal",
    "GITLAB_TOKEN": "fake-gitlab-token",
    "GITLAB_PROJECT_ID": "qa/playwright-tests",
}

# Staging creds are optional (legacy — only save_auth_state.py consumes them).
_LEGACY_OPTIONAL_VARS = ("STAGING_USERNAME", "STAGING_PASSWORD")

# Every env var that must trigger a clear error when missing.
_REQUIRED_VARS = [k for k in _BASE_ENV if k not in _LEGACY_OPTIONAL_VARS]

_OPTIONAL_VARS = (
    "PLANNER_MODEL",
    "PLANNER_LLM_BASE_URL",
    "PLANNER_LLM_API_KEY",
    "GENERATOR_MODEL",
    "HEALER_MODEL",
    "VISION_MODEL",
    "AGENT_VISION",
    "AGENT_DOM_PROBE",
    "XRAY_IS_CLOUD",
    "GITLAB_ENABLED",
    "GITLAB_TARGET_BRANCH",
    "NON_PROD_URL_MARKERS",
    "TESTCASE_SOURCE",
    "LOCAL_TESTCASE_DIR",
    "PROJECT_CONTEXT_PATH",
    "PROJECT_MAP_PATH",
    "RAG_ENABLED",
    "KB_PATH",
    "EMBEDDING_MODEL",
    "RERANKER_MODEL",
    "RERANK_ENDPOINT",
    "DISTILLER_MODEL",
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Hermetic environment: no real .env, no repo pollution.

    Neutralizes load_dotenv, redirects PROJECT_ROOT (output dirs + context paths)
    into tmp_path, clears every known config var, then applies the fake base env.
    Returns the monkeypatch so tests can delete/override keys before loading.
    """
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    for key in (*_REQUIRED_VARS, *_LEGACY_OPTIONAL_VARS, *_OPTIONAL_VARS):
        monkeypatch.delenv(key, raising=False)
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_happy_path_loads_full_config(env, tmp_path):
    env.setenv("XRAY_IS_CLOUD", "false")
    cfg = load_config()

    assert isinstance(cfg, Config)
    assert cfg.llm_base_url == "https://gateway.internal/v1"
    assert cfg.staging_password == _FAKE_PASSWORD
    # defaults applied for the optional vars
    assert cfg.planner_model == "openai/gpt-oss-120b"
    assert cfg.generator_model == "mistralai/devstral-small-2-2512"
    assert cfg.healer_model == "openai/gpt-oss-120b"
    # vision sensor: configured model present, disabled by default
    assert cfg.vision_model == "mistralai/devstral-small-2-2512"
    assert cfg.vision_max_calls == 0
    assert cfg.gitlab_target_branch == "main"
    # bool parsing
    assert cfg.xray_is_cloud is False
    # paths resolved under tmp_path; output dirs created
    assert cfg.project_context_path == tmp_path / "project_context.md"
    assert cfg.project_map_path == tmp_path / "project_map.md"
    assert cfg.plans_dir.is_dir()
    assert cfg.tests_dir.is_dir()
    assert cfg.snapshots_dir.is_dir()


@pytest.mark.usefixtures("env")
def test_xray_is_cloud_defaults_true():
    assert load_config().xray_is_cloud is True


@pytest.mark.parametrize("missing", _REQUIRED_VARS)
def test_missing_required_var_raises_clear_message(env, missing):
    env.delenv(missing, raising=False)
    with pytest.raises(RuntimeError) as exc:
        load_config()
    assert missing in str(exc.value)


def test_staging_creds_are_optional(env):
    """STAGING_USERNAME/PASSWORD are legacy (save_auth_state.py only) — the pipeline
    must load without them."""
    for var in _LEGACY_OPTIONAL_VARS:
        env.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.staging_username is None
    assert cfg.staging_password is None


@pytest.mark.usefixtures("env")
def test_gitlab_enabled_defaults_true():
    cfg = load_config()
    assert cfg.gitlab_enabled is True
    assert cfg.gitlab_base_url == "https://gitlab.internal"


def test_gitlab_disabled_allows_missing_gitlab_vars(env):
    """GITLAB_ENABLED=false lets the container run end-to-end with no GITLAB_* set."""
    env.setenv("GITLAB_ENABLED", "false")
    for var in ("GITLAB_BASE_URL", "GITLAB_TOKEN", "GITLAB_PROJECT_ID"):
        env.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.gitlab_enabled is False
    assert cfg.gitlab_base_url is None
    assert cfg.gitlab_token is None
    assert cfg.gitlab_project_id is None


@pytest.mark.parametrize(
    "prod_url",
    [
        "https://app.acme.com",
        "https://acme.com",
        "https://www.acme.com",
        "https://api.acme.io",
    ],
)
def test_prod_url_blocked(env, prod_url):
    env.setenv("STAGING_BASE_URL", prod_url)
    with pytest.raises(ProductionURLError):
        load_config()


@pytest.mark.parametrize(
    "safe_url",
    [
        "https://baseurl.qa.com",
        "https://demo.acme.com",
        "http://localhost:3000",
        "https://staging.yourapp.internal",
        "https://127.0.0.1:8443",
    ],
)
def test_safe_url_allowed(env, safe_url):
    env.setenv("STAGING_BASE_URL", safe_url)
    assert load_config().staging_base_url == safe_url


def test_custom_host_blocked_without_override(env):
    env.setenv("STAGING_BASE_URL", "https://uat.acme.com")
    with pytest.raises(ProductionURLError):
        load_config()


def test_env_marker_override_allows_custom_non_prod_host(env):
    env.setenv("STAGING_BASE_URL", "https://uat.acme.com")
    env.setenv("NON_PROD_URL_MARKERS", "uat, sandbox")
    assert load_config().staging_base_url == "https://uat.acme.com"


def test_url_without_host_raises(env):
    env.setenv("STAGING_BASE_URL", "not-a-url")
    with pytest.raises(ProductionURLError):
        load_config()


@pytest.mark.usefixtures("env")
def test_no_secret_leak_to_stdout(capsys):
    cfg = load_config()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _FAKE_PASSWORD not in captured.out
    assert _FAKE_PASSWORD not in captured.err
    # sanity: the secret really is loaded, so the assertion above is meaningful
    assert cfg.staging_password == _FAKE_PASSWORD


def test_agent_vision_disabled_by_default_false_and_zero(env):
    assert load_config().vision_max_calls == 0  # unset -> off
    for off in ("false", "0", "off", "no"):
        env.setenv("AGENT_VISION", off)
        assert load_config().vision_max_calls == 0, off


def test_agent_vision_positive_int_sets_budget(env):
    # AGENT_VISION is the single shared knob for both the Planner and the Healer.
    env.setenv("AGENT_VISION", "4")
    assert load_config().vision_max_calls == 4


def test_agent_vision_invalid_fails_fast(env):
    env.setenv("AGENT_VISION", "lots")
    with pytest.raises(RuntimeError, match="AGENT_VISION"):
        load_config()


def test_vision_model_defaults_and_override(env):
    assert load_config().vision_model == "mistralai/devstral-small-2-2512"
    env.setenv("VISION_MODEL", "custom/vision-model")
    assert load_config().vision_model == "custom/vision-model"


def test_agent_dom_probe_disabled_by_default_false_and_zero(env):
    assert load_config().dom_probe_max_calls == 0  # unset -> off
    for off in ("false", "0", "off", "no"):
        env.setenv("AGENT_DOM_PROBE", off)
        assert load_config().dom_probe_max_calls == 0, off


def test_agent_dom_probe_positive_int_sets_budget(env):
    # AGENT_DOM_PROBE is the single shared knob for both the Planner and the Healer.
    env.setenv("AGENT_DOM_PROBE", "10")
    assert load_config().dom_probe_max_calls == 10


def test_agent_dom_probe_invalid_fails_fast(env):
    env.setenv("AGENT_DOM_PROBE", "plenty")
    with pytest.raises(RuntimeError, match="AGENT_DOM_PROBE"):
        load_config()


# --- test-case source (xray | local) + path overrides -----------------------


@pytest.mark.usefixtures("env")
def test_defaults_to_xray_source():
    cfg = load_config()
    assert cfg.testcase_source == "xray"
    assert cfg.local_testcase_dir is None


def test_invalid_testcase_source_fails_fast(env):
    env.setenv("TESTCASE_SOURCE", "bogus")
    with pytest.raises(RuntimeError, match="TESTCASE_SOURCE"):
        load_config()


def test_local_source_makes_jira_optional(env, tmp_path):
    # In local mode the pipeline needs no Jira/Xray: the three JIRA_* vars become optional.
    env.setenv("TESTCASE_SOURCE", "local")
    env.setenv("LOCAL_TESTCASE_DIR", str(tmp_path / "cases"))
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN"):
        env.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.testcase_source == "local"
    assert cfg.jira_base_url is None and cfg.jira_email is None and cfg.jira_token is None
    assert cfg.local_testcase_dir == (tmp_path / "cases").resolve()


def test_local_source_requires_local_testcase_dir(env):
    env.setenv("TESTCASE_SOURCE", "local")
    env.delenv("LOCAL_TESTCASE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="LOCAL_TESTCASE_DIR"):
        load_config()


def test_local_testcase_dir_relative_resolves_under_project_root(env, tmp_path):
    env.setenv("TESTCASE_SOURCE", "local")
    env.setenv("LOCAL_TESTCASE_DIR", "demo/test-cases")
    assert load_config().local_testcase_dir == (tmp_path / "demo/test-cases").resolve()


def test_context_path_overrides_respected(env, tmp_path):
    ctx = tmp_path / "custom" / "ctx.md"
    mp = tmp_path / "custom" / "map.md"
    env.setenv("PROJECT_CONTEXT_PATH", str(ctx))
    env.setenv("PROJECT_MAP_PATH", str(mp))
    cfg = load_config()
    assert cfg.project_context_path == ctx.resolve()
    assert cfg.project_map_path == mp.resolve()


@pytest.mark.usefixtures("env")
def test_rag_defaults_off_with_sane_models(tmp_path):
    cfg = load_config()
    assert cfg.rag_enabled is False
    assert cfg.kb_path == (tmp_path / "qdrant_storage").resolve()
    assert cfg.embedding_model == "mxbai-embed-large"
    assert cfg.reranker_model == "zeroentropy/zerank-1-small"
    assert cfg.rerank_endpoint is None
    assert cfg.distiller_model == cfg.generator_model  # unset → follows the generator


def test_rag_knobs_override(env, tmp_path):
    env.setenv("RAG_ENABLED", "true")
    env.setenv("KB_PATH", "kb-alt")
    env.setenv("EMBEDDING_MODEL", "custom-embed")
    env.setenv("RERANKER_MODEL", "custom-rerank")
    env.setenv("RERANK_ENDPOINT", "https://gateway.internal/api/rerank")
    env.setenv("DISTILLER_MODEL", "custom-distiller")
    cfg = load_config()
    assert cfg.rag_enabled is True
    assert cfg.kb_path == (tmp_path / "kb-alt").resolve()  # relative → under PROJECT_ROOT
    assert cfg.embedding_model == "custom-embed"
    assert cfg.reranker_model == "custom-rerank"
    assert cfg.rerank_endpoint == "https://gateway.internal/api/rerank"
    assert cfg.distiller_model == "custom-distiller"


# --- Planner-only LLM endpoint override --------------------------------------


@pytest.mark.usefixtures("env")
def test_planner_endpoint_defaults_to_shared_gateway():
    """Unset PLANNER_LLM_* → the Planner uses the shared gateway, unchanged."""
    cfg = load_config()
    assert cfg.planner_base_url == cfg.llm_base_url == "https://gateway.internal/v1"
    assert cfg.planner_api_key == cfg.llm_api_key == "fake-llm-key"


def test_planner_base_override_without_key_uses_keyless_placeholder(env):
    """Base override with no key → override base + placeholder key; the shared gateway
    key is NEVER reused for the override endpoint, and other agents stay on the gateway."""
    env.setenv("PLANNER_LLM_BASE_URL", "https://ollama.internal/v1")
    cfg = load_config()
    assert cfg.planner_base_url == "https://ollama.internal/v1"
    assert cfg.planner_api_key == config._KEYLESS_API_KEY_PLACEHOLDER
    assert cfg.planner_api_key != cfg.llm_api_key
    assert cfg.llm_base_url == "https://gateway.internal/v1"  # others untouched


def test_planner_base_and_key_override_both_respected(env):
    env.setenv("PLANNER_LLM_BASE_URL", "https://second-gateway/v1")
    env.setenv("PLANNER_LLM_API_KEY", "planner-key")
    cfg = load_config()
    assert cfg.planner_base_url == "https://second-gateway/v1"
    assert cfg.planner_api_key == "planner-key"


def test_planner_key_override_without_base_stays_on_shared_url(env):
    """Only the key overridden → same shared base URL, the given key."""
    env.setenv("PLANNER_LLM_API_KEY", "planner-key")
    cfg = load_config()
    assert cfg.planner_base_url == cfg.llm_base_url == "https://gateway.internal/v1"
    assert cfg.planner_api_key == "planner-key"
