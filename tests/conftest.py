"""Shared pytest fixtures for the ai_test_gen suite.

``cfg`` is a hermetic ``Config`` (dummy values, tmp_path-backed paths) for tests that
need a Config without touching the environment, the network, or real secrets.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_test_gen.config import Config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """A fully-populated, hermetic Config.

    The context/map files are NOT created — tests that need them write into
    ``tmp_path`` themselves (their paths point under ``tmp_path``).
    """
    return Config(
        llm_base_url="https://gateway.internal/v1",
        llm_api_key="fake-key",
        planner_base_url="https://gateway.internal/v1",
        planner_api_key="fake-key",
        planner_model="planner-model",
        generator_model="generator-model",
        healer_model="healer-model",
        vision_model="vision-model",
        vision_max_calls=0,
        dom_probe_max_calls=0,
        testcase_source="xray",
        local_testcase_dir=None,
        jira_base_url="https://jira.internal",
        jira_email="qa.bot@example.com",
        jira_token="fake-token",
        xray_is_cloud=False,
        staging_base_url="https://staging.example.internal",
        staging_username="qa.bot",
        staging_password="fake-pw",
        gitlab_enabled=True,
        gitlab_base_url="https://gitlab.internal",
        gitlab_token="fake-token",
        gitlab_project_id="qa/playwright-tests",
        gitlab_target_branch="main",
        output_dir=tmp_path,
        plans_dir=tmp_path / "plans",
        tests_dir=tmp_path / "tests",
        snapshots_dir=tmp_path / "snapshots",
        project_context_path=tmp_path / "project_context.md",
        project_map_path=tmp_path / "project_map.md",
        rag_enabled=False,
        kb_path=tmp_path / "kb",
        embedding_model="embed-model",
        reranker_model="rerank-model",
        rerank_endpoint=None,
        distiller_model="distiller-model",
    )
