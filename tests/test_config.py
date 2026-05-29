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

# Every env var that must trigger a clear error when missing.
_REQUIRED_VARS = list(_BASE_ENV)

_OPTIONAL_VARS = (
    "PLANNER_MODEL",
    "GENERATOR_MODEL",
    "HEALER_MODEL",
    "XRAY_IS_CLOUD",
    "GITLAB_TARGET_BRANCH",
    "NON_PROD_URL_MARKERS",
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
    for key in (*_REQUIRED_VARS, *_OPTIONAL_VARS):
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
    assert cfg.gitlab_target_branch == "main"
    # bool parsing
    assert cfg.xray_is_cloud is False
    # paths resolved under tmp_path; output dirs created
    assert cfg.project_context_path == tmp_path / "project_context.md"
    assert cfg.project_map_path == tmp_path / "project_map.md"
    assert cfg.plans_dir.is_dir()
    assert cfg.tests_dir.is_dir()
    assert cfg.runs_dir.is_dir()


@pytest.mark.usefixtures("env")
def test_xray_is_cloud_defaults_true():
    assert load_config().xray_is_cloud is True


@pytest.mark.parametrize("missing", _REQUIRED_VARS)
def test_missing_required_var_raises_clear_message(env, missing):
    env.delenv(missing, raising=False)
    with pytest.raises(RuntimeError) as exc:
        load_config()
    assert missing in str(exc.value)


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
