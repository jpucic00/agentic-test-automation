"""Regression guard for the shipped context templates — fully local.

The pipeline's auth model is context-driven live login (each scenario logs in as
the role it needs); the earlier saved-session (``storage_state``) model was dropped.
These tests pin the shipped example templates to the live-login model so the dropped
model can't creep back in and contradict the agent prompts ("You start
UNauthenticated — no saved session").
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = (
    REPO_ROOT / "project_context.example.md",
    REPO_ROOT / "project_map.example.md",
)

# Phrases that only existed in the dropped saved-session auth model.
_DROPPED_MODEL_PHRASES = ("storage_state", "storage state", "storageState", "switchRole")


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_template_exists(template):
    assert template.is_file(), f"shipped template missing: {template.name}"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
@pytest.mark.parametrize("phrase", _DROPPED_MODEL_PHRASES)
def test_templates_free_of_dropped_auth_model(template, phrase):
    assert phrase not in template.read_text(), (
        f"{template.name} mentions {phrase!r} — the saved-session auth model was "
        "dropped; templates must describe context-driven live login only"
    )


def test_context_template_documents_live_login_and_session_killers():
    text = (REPO_ROOT / "project_context.example.md").read_text()
    # The healer prompt points agents at these two pieces of the Project Context.
    assert "No saved session" in text
    assert "Session-invalidating actions" in text
