"""The Distiller agent: one pre-assembled test bundle → one normalized record.

Offline seeding only (RETRIEVAL_MEMORY_PLAN.md §5) — never part of the per-run
pipeline loop. The agent has NO tools and never reads the repo: the seeding CLI
assembles each test's context deterministically (static extraction + bounded
helper resolution in ``extract.py``) and the agent answers one structured
"normalize this" call per test. The CLI — not the model — embeds the result.

``DISTILLER_MODEL`` selects the model (default: the Devstral/generator class —
code-reading, structured output, cheap per call). Escalate to a reasoning model
only if dry-run review files show shallow step expansion.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from ..config import Config
from ..llm import build_openai_model
from ..models import ManualTestCase
from .extract import TestBundle
from .models import KBSelector

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# One distill call is a single structured completion — bound it so a hung or
# queued gateway surfaces as an error within minutes instead of dangling on the
# client library's 10-minute-per-attempt default with zero output.
_DISTILL_TIMEOUT_S = 240.0


class DistilledCase(BaseModel):
    """The Distiller's structured output — the LLM-authored slice of a KBRecord.

    Everything else on the record (ids, provenance, source code, timestamps) is
    attached by the seeding CLI in plain code.
    """

    title: str = Field(description="Short human title; prefer the manual case's title")
    intent_text: str = Field(
        description="Embedding text: title, then 'Steps: ...', then 'Expected: ...' — "
        "in the material's original language, never translated"
    )
    steps: list[str] = Field(
        description="Ordered one-action steps of the flow, helper calls expanded to "
        "what their bodies actually do; navigation included"
    )
    selectors: list[KBSelector] = Field(
        default_factory=list,
        description="The extracted locators, kind+value verbatim, with a short "
        "description and route each — never invented, never rewritten",
    )
    routes: list[str] = Field(
        default_factory=list, description="Pages/paths the flow touches, as evidenced"
    )


def build_distiller(config: Config) -> Agent[None, DistilledCase]:
    """Build the Distiller agent (no toolset, no context files — bundle-only input)."""
    model = build_openai_model(config, config.distiller_model, timeout_s=_DISTILL_TIMEOUT_S)
    system_prompt = (PROMPTS_DIR / "distiller.md").read_text()
    return Agent(
        model=model,
        output_type=DistilledCase,
        system_prompt=system_prompt,
        retries=2,
    )


def build_distill_message(bundle: TestBundle, case: ManualTestCase | None) -> str:
    """The one user message for a bundle: test + helpers + ground truth + case."""
    flavor = "Selenium/Java" if bundle.language == "java" else "Playwright/TypeScript"
    parts = [
        f"Normalize this {flavor} test into a knowledge-base record.",
        f"## Test `{bundle.class_name}.{bundle.test_name}`\n"
        f"```{bundle.language}\n{bundle.code}\n```",
    ]
    if bundle.helper_snippets:
        helpers = "\n\n".join(bundle.helper_snippets)
        parts.append(f"## Resolved helpers it calls\n```{bundle.language}\n{helpers}\n```")
    if bundle.locators:
        lines = "\n".join(
            f"- {loc.kind}: {loc.value}  (declared in {loc.declared_in})"
            for loc in bundle.locators
        )
        parts.append(f"## EXTRACTED LOCATORS (ground truth — copy, never invent)\n{lines}")
    if bundle.unresolved_locators:
        parts.append(
            "## Locators in the code whose VALUE is unknown (dynamic/unresolvable — "
            "NEVER emit a selector for these, and never use a constant name as a value)\n"
            + "\n".join(f"- {entry}" for entry in bundle.unresolved_locators)
        )
    if bundle.urls:
        parts.append("## URLs/routes seen in the code\n" + "\n".join(f"- {u}" for u in bundle.urls))
    if bundle.helper_refs:
        unresolved = [ref for ref in bundle.helper_refs if ref.startswith("unresolved:")]
        if unresolved:
            parts.append(
                "## Unresolved calls (bodies unavailable — keep each as ONE opaque step)\n"
                + "\n".join(f"- {ref}" for ref in unresolved)
            )
    if case is not None:
        steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(case.steps))
        expected = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(case.expected_results))
        parts.append(
            f"## Linked manual test case {case.key}: {case.title}\n"
            f"Steps:\n{steps or '(none)'}\nExpected results:\n{expected or '(none)'}"
        )
    return "\n\n".join(parts)
