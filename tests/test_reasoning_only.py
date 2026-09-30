"""Unit tests for the reasoning-only retry nudge — offline (FunctionModel doubles, no network).

A response made only of reasoning gets the named ``NUDGE`` retry prompt (not pydantic-ai's generic
one) and is counted in the run's usage; every other response passes through untouched; the output
retry budget still ends a run that never stops doing it; and the Planner and Healer carry it.
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, AgentRetries
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage

from ai_test_gen.agents import healer as healer_mod
from ai_test_gen.agents import planner as planner_mod
from ai_test_gen.agents.runtime.reasoning_only import NUDGE, ReasoningOnlyRetry, is_reasoning_only
from ai_test_gen.core.usage import REASONING_ONLY_RETRIES, UsageLog


class Out(BaseModel):
    value: str


def _final(info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"value": "done"})])


def _retry_prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, RetryPromptPart)
    ]


def _agent(model: FunctionModel, *, output_retries: int = 5) -> Agent[None, Out]:
    return Agent(
        model,
        output_type=Out,
        retries=AgentRetries(output=output_retries),
        capabilities=[ReasoningOnlyRetry()],
    )


def test_is_reasoning_only():
    assert is_reasoning_only(ModelResponse(parts=[ThinkingPart("call browser_click {")]))
    assert is_reasoning_only(ModelResponse(parts=[ThinkingPart("a"), ThinkingPart("b")]))
    assert not is_reasoning_only(ModelResponse(parts=[]))
    assert not is_reasoning_only(ModelResponse(parts=[ThinkingPart("a"), ToolCallPart("t", {})]))
    assert not is_reasoning_only(ModelResponse(parts=[ThinkingPart("a"), TextPart("hi")]))


def test_reasoning_only_reply_gets_the_named_nudge_and_is_counted():
    calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ThinkingPart("Use browser_click on ref e12.{")])
        return _final(info)

    usage = RunUsage()
    result = asyncio.run(_agent(FunctionModel(model)).run("go", usage=usage))

    assert result.output == Out(value="done")
    assert _retry_prompts(result.all_messages()) == [NUDGE]
    assert usage.details[REASONING_ONLY_RETRIES] == 1


def test_other_responses_pass_through_untouched():
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ThinkingPart("done, returning"), *_final(info).parts])

    usage = RunUsage()
    result = asyncio.run(_agent(FunctionModel(model)).run("go", usage=usage))

    assert result.output == Out(value="done")
    assert _retry_prompts(result.all_messages()) == []
    assert REASONING_ONLY_RETRIES not in usage.details


def test_output_retry_budget_still_ends_a_run_that_never_calls():
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ThinkingPart("I should click it.")])

    usage = RunUsage()
    with pytest.raises(UnexpectedModelBehavior, match="output retries"):
        asyncio.run(_agent(FunctionModel(model), output_retries=2).run("go", usage=usage))
    # every reasoning-only reply was nudged and counted, including the one that ended the run
    assert usage.details[REASONING_ONLY_RETRIES] == 3


def test_usage_log_reports_reasoning_only_retries():
    log = UsageLog()
    log.record("Planner", "m", RunUsage(requests=4, details={REASONING_ONLY_RETRIES: 2}), 1.0,
               ok=True)
    log.record("Healer attempt 1", "m", RunUsage(requests=3, details={REASONING_ONLY_RETRIES: 1}),
               1.0, ok=False)
    log.record("Generator", "g", RunUsage(requests=1), 1.0, ok=True)

    report = log.summary(3.0)

    assert [a["reasoning_only_retries"] for a in report["agents"]] == [2, 1, 0]
    assert report["total"]["reasoning_only_retries"] == 3


@pytest.mark.parametrize("build", [planner_mod.build_planner, healer_mod.build_healer])
def test_browser_agents_carry_the_nudge(build, cfg):
    agent = build(cfg)
    capabilities = agent._root_capability.capabilities  # pyright: ignore[reportPrivateUsage]
    assert any(isinstance(c, ReasoningOnlyRetry) for c in capabilities)
