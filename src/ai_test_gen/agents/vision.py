"""The Vision Aid Agent: hand a screenshot to a vision-capable model, get a short text answer.

Backs the optional ``inspect_screen`` tool that BOTH browser agents (Planner and Healer) register
from ``agents/_vision_aid.py``, enabled via the ``AGENT_VISION`` env var. The text-only agents
(gpt-oss) cannot ingest images, so this converts a screenshot into a one-or-two-sentence text
observation they CAN act on — the Vision Aid Agent is the eye, the browser agent stays the brain and
the MCP driver. It only ever *describes what is rendered*; it never produces a selector (element
targeting stays on ``browser_generate_locator``).

Deliberately tiny and side-effect free (no filesystem or browser access) so the Planner's tool
stays the only place that decides WHEN to look, and so this unit-tests with a mocked model (no
network). Reuses ``build_openai_model`` so the vision call shares the same gateway httpx/mTLS
policy as every other agent.
"""
from __future__ import annotations

from pydantic_ai import Agent, BinaryContent

from ..config import Config
from ..llm import build_openai_model

_SYSTEM_PROMPT = (
    "You are a vision sensor for a web-UI test-automation agent. You are given a screenshot of "
    "the current page and a question about it. The agent may be DISORIENTED — its question can "
    "assume a page or element that is not what the screenshot shows — so respond in EXACTLY two "
    "labeled parts, each 1-2 plain sentences:\n"
    "Answer: the direct answer to the question. If the question's premise contradicts what the "
    "image shows (it asks about a dialog/page/element that is not what is on screen), SAY SO "
    "explicitly. If the answer is not visible in the image, say so plainly.\n"
    "On screen: what is actually rendered, concretely — the visible heading or page title, the "
    "main content, and any dialog, overlay, banner, toast, error, or empty state. Only what is "
    "visible; if you cannot tell what page this is, say so.\n"
    "In both parts describe ONLY what is actually visible in the image (text, dialogs, "
    "overlays/banners, toasts, enabled/disabled state, whether an element is shown or hidden). "
    "Do NOT guess and do NOT suggest code. If you are asked for an element id, data-testid, "
    "CSS/HTML selector, or locator, do NOT provide one — those cannot be read from an image; "
    "reply that the agent must capture it with browser_generate_locator."
)

# Cap the returned text so a verbose model cannot bloat the Planner's context/history.
# 900 (was 600) fits the two-part contract: Answer: + On screen: at 1-2 sentences each.
_MAX_CHARS = 900


def build_vision_agent(config: Config) -> Agent[None, str]:
    """Build the one-shot vision agent (plain-text output, no toolset)."""
    model = build_openai_model(config, config.vision_model)
    return Agent(model=model, output_type=str, system_prompt=_SYSTEM_PROMPT)


async def ask_vision(config: Config, question: str, image_png: bytes) -> str:
    """Ask the vision model ``question`` about ``image_png``; return a short text answer.

    A pure pass-through to the gateway vision model. The screenshot bytes go ONLY to this model;
    the caller hands the returned text back to the (text-only) Planner.
    """
    agent = build_vision_agent(config)
    result = await agent.run(
        [question, BinaryContent(data=image_png, media_type="image/png")]
    )
    return result.output.strip()[:_MAX_CHARS]
