"""Unit tests for the three agents — fully local (no network, no npx subprocess).

Each agent is built with the hermetic ``cfg`` fixture and exercised through a
``TestModel`` via the ``agent.override(model=..., toolsets=[])`` seam, so no real LLM
gateway is contacted and the Playwright MCP subprocess is never started. Coroutines
are driven with ``asyncio.run`` so this needs no ``pytest-asyncio`` (no new dependency).

Models are referenced via the ``models`` module because ``TestPlan`` / ``TestRunResult``
start with "Test" and would otherwise be collected by pytest as test classes.
"""
from __future__ import annotations

import asyncio

from pydantic_ai.models.test import TestModel

from ai_test_gen import models
from ai_test_gen.agents import generator as generator_mod
from ai_test_gen.agents import healer as healer_mod
from ai_test_gen.agents import planner as planner_mod
from ai_test_gen.agents.generator import build_generator
from ai_test_gen.agents.healer import build_healer
from ai_test_gen.agents.planner import build_planner


def _run_offline(agent):
    """Run an agent with a TestModel and no toolsets — no network, no subprocess."""
    with agent.override(model=TestModel(), toolsets=[]):
        return asyncio.run(agent.run("sample")).output


def test_generator_builds_and_returns_generated_test(cfg):
    out = _run_offline(build_generator(cfg))
    assert isinstance(out, models.GeneratedTest)


def test_planner_builds_and_returns_test_plan(cfg):
    out = _run_offline(build_planner(cfg))
    assert isinstance(out, models.TestPlan)


def test_healer_builds_and_returns_healed_test(cfg):
    out = _run_offline(build_healer(cfg))
    assert isinstance(out, models.HealedTest)


def test_generator_has_no_playwright_mcp():
    # The Generator deliberately does not use Playwright MCP (smaller scope = better code).
    assert not hasattr(generator_mod, "build_playwright_mcp")


def test_planner_attaches_playwright_mcp(cfg, monkeypatch):
    calls: list[object] = []
    real = planner_mod.build_playwright_mcp

    def spy(config, storage_state=None):
        calls.append(storage_state)
        return real(config, storage_state=storage_state)

    monkeypatch.setattr(planner_mod, "build_playwright_mcp", spy)
    build_planner(cfg)
    assert len(calls) == 1


def test_healer_attaches_playwright_mcp(cfg, monkeypatch):
    calls: list[object] = []
    real = healer_mod.build_playwright_mcp

    def spy(config, storage_state=None):
        calls.append(storage_state)
        return real(config, storage_state=storage_state)

    monkeypatch.setattr(healer_mod, "build_playwright_mcp", spy)
    build_healer(cfg)
    assert len(calls) == 1
