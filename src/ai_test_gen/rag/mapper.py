"""Suite-map pass — phase 0 of KB seeding (RETRIEVAL_MEMORY_PLAN.md §1.15/§5.2).

Before any per-test distillation, one browsable map of the corpus is produced per
project: ``output/suite_map/<KEY>.suite_map.md``. A minimal static skeleton (tree,
suites, test counts — from :mod:`.discover`) is assembled in code; a **Mapper agent**
(``DISTILLER_MODEL`` + the shared read-only repo tools) refines it into the parts a
per-test distiller would otherwise rediscover on every test: package roles, locator
idioms (with cited code examples), the most-reused helpers, and the login/lifecycle
convention. The map's §0 at-a-glance index (≤1.2k chars) is injected into every
distill call so shared knowledge is read once, not once per test.

**Every model claim cites a path.** The schema requires a citation on each idiom
example / helper / convention / lifecycle note; the code resolves each citation to a
real corpus file and flags any that don't (recall-with-flagging — a bad citation is
surfaced in the map's "unmapped / uncertain" section, never silently dropped).

**Per-section source-hash cache** (``<KEY>.suite_map.cache.json``). Each map section
is fingerprinted by the content of the files it cites. A re-seed recomputes those
fingerprints from disk and refreshes only the sections whose cited files changed
(a file added/removed, or ``--refresh-map``, refreshes everything; nothing changed
skips the model entirely). Unchanged sections are byte-preserved from cache, so the
map only churns where the corpus actually moved. One Mapper call regenerates a whole
draft when anything is stale — cheaper than one exploration per section, and the
section-level cache still gives the "only changed sections re-refine" guarantee.

**Overrides.** Human corrections in ``<KEY>.suite_map.overrides.md`` are overlaid at
render time (matched by section heading) and survive every regeneration.

**Core-knowledge records.** The rendered lifecycle and conventions sections are also
emitted as two ``kind=knowledge`` :class:`KBRecord`s so the Planner can retrieve the
suite's login flow and conventions directly (embedding + upsert happen in ``seed_kb``,
never here — this module stays offline and pure).
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent, AgentRetries

from ..agents._context import agent_output_retries, agent_retries
from ..agents._run_failure import run_agent_logged
from ..config import Config
from ..llm import build_openai_model
from .discover import DiscoveryResult, discover_tests
from .models import (
    ExplorationTrace,
    KBRecord,
    KBSource,
    ReconstructedPlan,
    make_record_id,
)
from .tools import RepoTools

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Top-N core helpers by fan-in kept in the map (§1.15). A hard cap in code; the model
# ranks, the code truncates so an over-eager draft can't bloat the map.
CORE_HELPERS_N = 20
# §0 index is injected into EVERY distill call, so it is hard-bounded (§1.15).
_INDEX_CHAR_CAP = 1200
# Cap the free-text a knowledge record embeds — a section, not a whole file.
_KNOWLEDGE_TEXT_CAP = 2000
_CACHE_VERSION = 3

# Ordered map sections that the MODEL authors (field name == section key == cache key).
# skeleton / index / metadata are code-built and re-derived every render (never cached).
_SECTION_ORDER = (
    "suites",
    "locator_idioms",
    "core_helpers",
    "lifecycle",
    "data",
    "conventions",
    "unmapped",
)
_SECTION_TITLES = {
    "suites": "Suites & package roles",
    "locator_idioms": "Locator idioms",
    "core_helpers": "Core helpers",
    "lifecycle": "Lifecycle & login",
    "data": "Data & fixtures",
    "conventions": "Conventions & gotchas",
    "unmapped": "Unmapped / uncertain",
}


# --- the Mapper agent's structured output (every claim carries a citation) ----
class CitedNote(BaseModel):
    """A one-line observation with the file that evidences it."""

    text: str = Field(description="The observation, in the app's user-facing terms")
    source: str = Field(
        description="The file (or 'file#symbol') that evidences this — a citation is required"
    )


class CodeExample(BaseModel):
    """A short verbatim snippet illustrating a locator idiom, with its source."""

    code: str = Field(description="A short verbatim code snippet copied from the source")
    source: str = Field(description="The 'file#symbol' the snippet was copied from")


class LocatorIdiom(BaseModel):
    """How locators are expressed in this suite (e.g. By.id via a String constant)."""

    name: str = Field(description="Short name of the idiom, e.g. 'By.id via String constant'")
    how: str = Field(description="One line: how a locator is expressed with this idiom")
    examples: list[CodeExample] = Field(
        default_factory=list, description="Up to 3 cited code examples of the idiom"
    )


class HelperSummary(BaseModel):
    """A frequently-reused helper explained in user-visible terms."""

    symbol: str = Field(description="The helper, e.g. 'BasePage.click(By)'")
    summary: str = Field(
        description="What it does for the test, in user terms (not a code paraphrase)"
    )
    source: str = Field(description="The 'file#symbol' where the helper is defined")


class SuiteNote(BaseModel):
    """What a package/directory in the corpus is for."""

    path: str = Field(description="The package or directory, repo-relative")
    role: str = Field(description="What lives here / what it is responsible for")


class LifecycleNote(BaseModel):
    """The suite's login + setup/teardown convention, as user-visible steps."""

    summary: str = Field(default="", description="How login and lifecycle work, in user terms")
    login_steps: list[str] = Field(
        default_factory=list, description="Ordered, user-visible login steps"
    )
    sources: list[str] = Field(
        default_factory=list, description="Files that evidence the lifecycle/login flow"
    )


class MapDraft(BaseModel):
    """The Mapper agent's output — the model-authored sections of the suite map.

    Every field is optional-by-default so a sparse draft still validates (mid-tier
    reliability); the prompt demands completeness and the code flags empties into the
    'unmapped / uncertain' section rather than failing the run.
    """

    suites: list[SuiteNote] = Field(default_factory=list)
    locator_idioms: list[LocatorIdiom] = Field(default_factory=list)
    core_helpers: list[HelperSummary] = Field(default_factory=list)
    lifecycle: LifecycleNote = Field(default_factory=LifecycleNote)
    data: list[CitedNote] = Field(default_factory=list)
    conventions: list[CitedNote] = Field(default_factory=list)
    unmapped: list[str] = Field(
        default_factory=list,
        description="Anything you could not map or are unsure about (REQUIRED even if empty)",
    )


# A run_draft is injected in tests (a recorded transcript + canned draft); production
# builds and runs the real Mapper agent.
RunDraft = Callable[[RepoTools, str], Awaitable[MapDraft]]


@dataclass
class SuiteMapResult:
    """Outcome of a suite-map build — what ``seed_kb`` needs to write/embed/report."""

    project_key: str
    markdown: str
    index: str  # §0 at-a-glance block, for later distill injection
    path: Path  # where the map is (or would be) written
    knowledge_records: list[KBRecord]
    from_cache: bool
    stale_sections: list[str] = field(default_factory=list)
    unresolved_citations: list[str] = field(default_factory=list)
    files_opened: list[str] = field(default_factory=list)
    tool_calls: int = 0
    # The merged draft the map was rendered from — the distill phase derives each
    # test's own suite block from it (§5.3: "§0 + the test's own suite block").
    draft: MapDraft | None = None


# --- the agent ---------------------------------------------------------------
def build_mapper(config: Config, tools: RepoTools) -> Agent[None, MapDraft]:
    """Build the Mapper agent: DISTILLER_MODEL + the shared read-only repo tools."""
    from .distiller import seeding_model_settings  # local: avoid a module-load cycle

    model = build_openai_model(config, config.distiller_model)
    system_prompt = (PROMPTS_DIR / "mapper.md").read_text()
    agent = Agent(
        model=model,
        output_type=MapDraft,
        system_prompt=system_prompt,
        model_settings=seeding_model_settings(config),
        retries=AgentRetries(tools=agent_retries(), output=agent_output_retries()),
    )
    tools.register(agent)
    return agent


def _default_run_draft(config: Config) -> RunDraft:
    async def run(tools: RepoTools, message: str) -> MapDraft:
        agent = build_mapper(config, tools)
        # The Mapper has no MCP toolset, so run_agent_logged's context-enter is a no-op;
        # we reuse it for the same structured-output failure evidence the agents get.
        return await run_agent_logged(
            agent, message, agent_label="Mapper", request_limit=config.distiller_request_limit
        )

    return run


def _build_mapper_message(project_key: str, discovery: DiscoveryResult, tools: RepoTools) -> str:
    """The Mapper's user message: the static skeleton + a file inventory to explore from."""
    inventory = tools.inventory()
    return f"""Map the **{project_key}** test corpus into a suite map.

## Static skeleton (deterministic — counts you can trust)
{discovery.skeleton}

## Repository files ({len(inventory)} shown)
{chr(10).join(inventory)}

Explore with read_file / search / list_dir, then produce the suite map. Read the shared
page objects, base classes, helpers and any locator/resource files — follow how a real test
reaches the app. Cite a real file (as 'path' or 'path#symbol') on EVERY idiom example,
helper, convention and lifecycle claim; copy locator/code snippets verbatim, never invent one.
Give the top {CORE_HELPERS_N} most-reused helpers. If something is unclear or unmapped, say so
in 'unmapped' rather than guessing."""


# --- public entry point ------------------------------------------------------
async def build_suite_map(
    config: Config,
    project_key: str,
    *,
    selenium_root: Path | None = None,
    playwright_dir: Path | None = None,
    discovery: DiscoveryResult | None = None,
    map_dir: Path | None = None,
    refresh: bool = False,
    write: bool = True,
    run_draft: RunDraft | None = None,
) -> SuiteMapResult:
    """Build (or load from cache) the suite map for a project + corpus.

    ``run_draft`` is injected in tests (a recorded tool transcript + a canned draft);
    unset → the real Mapper agent runs. ``write`` controls whether the map + cache are
    persisted; embedding/upsert of the returned knowledge records is the caller's job.
    """
    roots = [r for r in (selenium_root, playwright_dir) if r is not None]
    if not roots:
        raise ValueError("build_suite_map needs at least one corpus root (selenium/playwright)")
    if discovery is None:
        discovery = discover_tests(
            project_key,
            selenium_root=selenium_root,
            playwright_dir=playwright_dir,
            marker_regex=config.test_marker_regex,
        )
    tools = RepoTools(roots)
    out_dir = map_dir or (config.output_dir / "suite_map")
    key = project_key.strip().upper()
    map_path = out_dir / f"{key}.suite_map.md"
    cache_path = out_dir / f"{key}.suite_map.cache.json"
    overrides_path = out_dir / f"{key}.suite_map.overrides.md"

    current_files = _corpus_files(tools)
    cache = _load_cache(cache_path)
    stale = _stale_sections(cache, current_files, tools, refresh=refresh)

    from_cache = bool(cache) and not stale
    if from_cache:
        assert cache is not None  # bool(cache) above; narrows for the type checker
        draft = MapDraft.model_validate(cache["draft"])
        logger.info("Suite map [%s]: cache hit — no Mapper call", key)
    else:
        run = run_draft or _default_run_draft(config)
        message = _build_mapper_message(key, discovery, tools)
        logger.info(
            "Suite map [%s]: refreshing section(s): %s",
            key,
            ", ".join(sorted(stale)) or "(all)",
        )
        fresh = await run(tools, message)
        cached_draft = MapDraft.model_validate(cache["draft"]) if cache else fresh
        draft = _merge_draft(cached_draft, fresh, stale)

    overrides = _load_overrides(overrides_path)
    unresolved = _unresolved_citations(draft, tools)
    markdown, index = _render_map(key, discovery, draft, roots, config, overrides, unresolved)
    # A knowledge record's provenance follows the corpus it was distilled from.
    source: KBSource = "selenium-import" if selenium_root is not None else "playwright-import"
    knowledge = _knowledge_records(key, draft, source, overrides, tools)

    if write and not from_cache:
        out_dir.mkdir(parents=True, exist_ok=True)
        _save_cache(cache_path, key, draft, current_files, tools)
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        map_path.write_text(markdown)

    return SuiteMapResult(
        project_key=key,
        markdown=markdown,
        index=index,
        path=map_path,
        knowledge_records=knowledge,
        from_cache=from_cache,
        stale_sections=sorted(stale),
        unresolved_citations=unresolved,
        files_opened=sorted(tools.files_opened),
        tool_calls=tools.tool_calls,
        draft=draft,
    )


# --- caching -----------------------------------------------------------------
def _corpus_files(tools: RepoTools) -> dict[str, str]:
    """``{address: sha256}`` for every source file — the corpus fingerprint."""
    out: dict[str, str] = {}
    for address in tools.inventory():
        path = tools.resolve_citation(address)
        if path is not None:
            out[address] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _field_citations(section: str, draft: MapDraft) -> list[str]:
    """The file citations a section depends on (drives its source-hash)."""
    if section == "locator_idioms":
        return [ex.source for idiom in draft.locator_idioms for ex in idiom.examples]
    if section == "core_helpers":
        return [h.source for h in draft.core_helpers]
    if section == "lifecycle":
        return list(draft.lifecycle.sources)
    if section == "data":
        return [n.source for n in draft.data]
    if section == "conventions":
        return [n.source for n in draft.conventions]
    # suites (dir roles) and unmapped (uncertainty) have no per-file dependency — they
    # refresh only when the file SET changes (which forces an all-sections refresh).
    return []


def _section_fingerprint(
    section: str, draft: MapDraft, tools: RepoTools, files: dict[str, str]
) -> str:
    """A stable hash of the CURRENT contents of the files a section cites."""
    parts: list[str] = []
    for citation in _field_citations(section, draft):
        path = tools.resolve_citation(citation)
        if path is None:
            parts.append(f"MISSING:{citation.split('#', 1)[0].strip()}")
        else:
            address = tools.address_of(path)
            parts.append(f"{address}={files.get(address, '?')}")
    return hashlib.sha256("\n".join(sorted(set(parts))).encode()).hexdigest()


def _stale_sections(
    cache: dict | None, files: dict[str, str], tools: RepoTools, *, refresh: bool
) -> set[str]:
    """Which model sections must be regenerated. Empty set ⇒ pure cache hit."""
    if refresh or not cache:
        return set(_SECTION_ORDER)
    if set(files) != set(cache.get("corpus_files", {})):
        # A file appeared or vanished — structure changed; refresh everything.
        return set(_SECTION_ORDER)
    draft = MapDraft.model_validate(cache["draft"])
    stored = cache.get("section_hashes", {})
    return {
        section
        for section in _SECTION_ORDER
        if _section_fingerprint(section, draft, tools, files) != stored.get(section)
    }


def _merge_draft(cached: MapDraft, fresh: MapDraft, stale: set[str]) -> MapDraft:
    """Take fresh content for stale sections, keep cached content for the rest."""
    return MapDraft(
        **{
            section: getattr(fresh if section in stale else cached, section)
            for section in _SECTION_ORDER
        }
    )


def _load_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == _CACHE_VERSION else None


def _save_cache(
    path: Path, key: str, draft: MapDraft, files: dict[str, str], tools: RepoTools
) -> None:
    section_hashes = {s: _section_fingerprint(s, draft, tools, files) for s in _SECTION_ORDER}
    path.write_text(
        json.dumps(
            {
                "version": _CACHE_VERSION,
                "project_key": key,
                "corpus_files": files,
                "section_hashes": section_hashes,
                "draft": draft.model_dump(),
            },
            indent=2,
        )
    )


# --- overrides + citation flags ----------------------------------------------
def _load_overrides(path: Path) -> dict[str, str]:
    """Parse ``<KEY>.suite_map.overrides.md`` into ``{section-title-lower: correction}``.

    Corrections are keyed by ``## <Section Title>`` headings; text before the first
    heading is filed under a catch-all so a freeform note is never lost.
    """
    if not path.exists():
        return {}
    overrides: dict[str, str] = {}
    current = "(general)"
    buffer: list[str] = []
    for line in path.read_text().splitlines():
        heading = line.strip()
        if heading.startswith("## "):
            if buffer:
                overrides[current] = "\n".join(buffer).strip()
                buffer = []
            current = heading[3:].strip().lower()
        else:
            buffer.append(line)
    if buffer:
        overrides[current] = "\n".join(buffer).strip()
    return {k: v for k, v in overrides.items() if v}


def _unresolved_citations(draft: MapDraft, tools: RepoTools) -> list[str]:
    """Cited paths that resolve to no corpus file — flagged, never dropped (§1.14 spirit)."""
    seen: list[str] = []
    for section in _SECTION_ORDER:
        for citation in _field_citations(section, draft):
            ref = citation.split("#", 1)[0].strip()
            if ref and tools.resolve_citation(citation) is None and ref not in seen:
                seen.append(ref)
    return seen


# --- rendering ---------------------------------------------------------------
def _render_map(
    key: str,
    discovery: DiscoveryResult,
    draft: MapDraft,
    roots: Sequence[Path],
    config: Config,
    overrides: dict[str, str],
    unresolved: list[str],
) -> tuple[str, str]:
    """Render the full map markdown and return ``(markdown, index_block)``."""
    sections = {name: _render_section(name, draft) for name in _SECTION_ORDER}
    if unresolved:
        flags = "\n".join(f"- ⚠ cited but not found in the corpus: `{ref}`" for ref in unresolved)
        sections["unmapped"] = (sections["unmapped"].rstrip() + "\n" + flags).strip()
    # Overlay human corrections under the matching section.
    for name in _SECTION_ORDER:
        override = overrides.get(_SECTION_TITLES[name].lower())
        if override:
            quoted = "\n".join(f"> {line}" for line in override.splitlines())
            sections[name] += "\n\n> **Human corrections:**\n>\n" + quoted

    index = _render_index(key, discovery, draft)
    body = [
        f"# Suite map — {key}",
        "",
        "> Generated by the Mapper agent (offline seeding). Human corrections go in "
        f"`{key}.suite_map.overrides.md` and survive regeneration.",
        "",
        "## §0 At a glance",
        "",
        index,
        "",
        "## Skeleton",
        "",
        "```",
        discovery.skeleton,
        "```",
    ]
    for name in _SECTION_ORDER:
        body += ["", f"## {_SECTION_TITLES[name]}", "", sections[name]]
    body += ["", "## Metadata", "", _render_metadata(discovery, draft, roots, config)]
    if overrides.get("(general)"):
        body += ["", "## Human corrections (unfiled)", "", overrides["(general)"]]
    return "\n".join(body).rstrip() + "\n", index


def _render_section(name: str, draft: MapDraft) -> str:
    if name == "suites":
        items = draft.suites
        return _bullets(f"`{s.path}` — {s.role}" for s in items) if items else "_(none mapped)_"
    if name == "locator_idioms":
        if not draft.locator_idioms:
            return "_(none mapped)_"
        blocks: list[str] = []
        for idiom in draft.locator_idioms:
            lines = [f"**{idiom.name}** — {idiom.how}"]
            for ex in idiom.examples[:3]:
                lines.append(f"\n```\n{ex.code.strip()}\n```\n_— {ex.source}_")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
    if name == "core_helpers":
        items = draft.core_helpers[:CORE_HELPERS_N]
        return (
            _bullets(f"`{h.symbol}` — {h.summary} _({h.source})_" for h in items)
            if items
            else "_(none mapped)_"
        )
    if name == "lifecycle":
        lc = draft.lifecycle
        if not (lc.summary or lc.login_steps):
            return "_(not documented)_"
        parts = [lc.summary] if lc.summary else []
        if lc.login_steps:
            parts.append(_numbered(lc.login_steps))
        if lc.sources:
            parts.append("_Sources: " + ", ".join(lc.sources) + "_")
        return "\n\n".join(parts)
    if name == "data":
        items = draft.data
        return _bullets(f"{n.text} _({n.source})_" for n in items) if items else "_(none mapped)_"
    if name == "conventions":
        items = draft.conventions
        return _bullets(f"{n.text} _({n.source})_" for n in items) if items else "_(none mapped)_"
    if name == "unmapped":
        # REQUIRED section even when empty (§1.15).
        return _bullets(draft.unmapped) if draft.unmapped else "- (nothing flagged)"
    return ""


def _render_index(key: str, discovery: DiscoveryResult, draft: MapDraft) -> str:
    """The ≤1.2k-char §0 digest injected into every distill call."""
    idiom_names = ", ".join(i.name for i in draft.locator_idioms[:4]) or "(none)"
    helpers = ", ".join(h.symbol for h in draft.core_helpers[:8]) or "(none)"
    steps = draft.lifecycle.login_steps
    login = draft.lifecycle.summary.strip() or (
        "; ".join(steps[:3]) if steps else "(not documented)"
    )
    lines = [
        f"**{key}** — {len(discovery.tests)} test(s) across "
        f"{len({t.path.split('/', 1)[0] for t in discovery.tests})} suite(s).",
        f"**Login:** {login}",
        f"**Locator idioms:** {idiom_names}.",
        f"**Core helpers:** {helpers}.",
        "See the sections below for cited examples, conventions and lifecycle.",
    ]
    index = "\n".join(lines)
    if len(index) > _INDEX_CHAR_CAP:
        index = index[: _INDEX_CHAR_CAP - 1].rstrip() + "…"
    return index


def _render_metadata(
    discovery: DiscoveryResult, draft: MapDraft, roots: Sequence[Path], config: Config
) -> str:
    """Deterministic generation facts — no wall-clock, so an unchanged corpus is byte-stable."""
    return _bullets(
        [
            f"Corpus roots: {', '.join(str(r) for r in roots)}",
            f"Tests discovered: {len(discovery.tests)} "
            f"(Java {discovery.java_discovered}, specs {discovery.spec_files})",
            f"Mapper model: {config.distiller_model}",
            f"Sections authored: {len(draft.suites)} suite(s), "
            f"{len(draft.locator_idioms)} idiom(s), {len(draft.core_helpers)} helper(s), "
            f"{len(draft.conventions)} convention(s)",
            f"Marker regex: `{config.test_marker_regex}`",
        ]
    )


def _bullets(items: object) -> str:
    return "\n".join(f"- {line}" for line in items)  # type: ignore[union-attr]


def _numbered(items: Sequence[str]) -> str:
    return "\n".join(f"{i}. {line}" for i, line in enumerate(items, 1))


# --- core-knowledge records --------------------------------------------------
def _knowledge_records(
    key: str, draft: MapDraft, source: KBSource, overrides: dict[str, str], tools: RepoTools
) -> list[KBRecord]:
    """Lifecycle + conventions sections → two ``kind=knowledge`` KBRecords for the Planner."""
    records: list[KBRecord] = []
    for section, title in (("lifecycle", "lifecycle & login"), ("conventions", "conventions")):
        body = _render_section(section, draft)
        override = overrides.get(_SECTION_TITLES[section].lower())
        if override:
            body = f"{body}\n\nHuman corrections:\n{override}"
        if body.strip() in ("_(not documented)_", "_(none mapped)_", ""):
            continue  # nothing worth retrieving
        ref = f"suite-map#{section}"
        record_title = f"{key} suite — {title}"
        records.append(
            KBRecord(
                record_id=make_record_id(key, source, ref),
                project_key=key,
                xray_key="",
                title=record_title,
                intent_text=_knowledge_intent(record_title, body),
                plan=ReconstructedPlan(title=record_title, notes=body[:_KNOWLEDGE_TEXT_CAP]),
                manual_steps=[],
                kind="knowledge",
                routes=[],
                explored=ExplorationTrace(
                    files_opened=sorted(tools.files_opened), tool_calls=tools.tool_calls
                ),
                outcome="legacy",
                source=source,
            )
        )
    return records


def _knowledge_intent(title: str, body: str) -> str:
    """Code-built embed text for a knowledge record (§1.17 — the model authors none)."""
    plain = body.replace("`", "").replace("*", "").replace("_", "")
    plain = " ".join(plain.split())
    return f"{title}\n{plain}"[: _KNOWLEDGE_TEXT_CAP]
