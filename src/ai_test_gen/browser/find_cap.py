"""Cap ``browser_find`` results so one search on a large page can't flood the context.

Playwright MCP's ``browser_find`` has no result limit: it returns every matching snapshot line
with 3 lines of context and its full path from the root. A broad search on a large page (a
Wikipedia article, a long table) returns tens of thousands of tokens — and that result is resent
with every later request of the run. On the demo's NOTE-6 one call was ~24k tokens, 72–75% of
the Planner's peak context.

``FindResultCap`` rewrites an over-budget result in three tiers, so no match silently vanishes:

1. **Full snippets** (MCP's own text, untouched) for the likeliest targets, up to half the
   budget. Likeliest = the element's name or text equals the query, then interactive/structural
   roles (button, link, textbox, heading, dialog, …), then page order.
2. **One line per remaining match** — the element line with its role, name and ``ref``, which is
   all an agent needs to act on it or to ``browser_snapshot`` it by ``target``. A matching
   property line (``/url:``) is shown as the element that owns it.
3. Only when even those lines exceed the budget: a count of the rest and how to narrow the search.

Results within the budget, "No matches found" and errors pass through unchanged. The budget is
``BROWSER_FIND_MAX_CHARS`` (0 turns the cap off).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.mcp import CallToolFunc, ProcessToolCallback, ToolResult

logger = logging.getLogger(__name__)

FIND_TOOL = "browser_find"
SNIPPET_SEPARATOR = "\n\n----\n\n"
ONE_LINE_MAX = 120
_NOTES_RESERVE = 800  # room for the explanation and the narrowing tip

# Roles an agent acts on or anchors to; a match on one of these outranks page text.
_TARGET_ROLES = frozenset(
    {
        "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch",
        "slider", "spinbutton", "option", "listbox", "menu", "menuitem", "menuitemcheckbox",
        "menuitemradio", "tab", "heading", "dialog", "alertdialog", "alert", "row", "cell",
        "columnheader", "img",
    }
)
_HEADER = re.compile(r"^Found (\d+) match(?:es)? for (.+):$")
_ROLE = re.compile(r"^- ([a-z]+)\b")
_NAME = re.compile(r'^- [a-z]+ "((?:[^"\\]|\\.)*)"')
_TEXT = re.compile(r"^- [a-z]+(?: \[[^\]]*\])*: (.*)$")


@dataclass(frozen=True)
class _Match:
    snippet: int  # index of the snippet (page order) that shows it
    element: str  # the element line, stripped: `link "Note-taking" [ref=e312]`
    rank: int  # 0 exact name/text, 1 target role, 2 other text, 3 only a property (/url:) matched


def _matcher(tool_args: dict[str, Any]) -> tuple[Callable[[str], bool], str] | None:
    """The line test MCP used for this search, plus the query as the agent wrote it."""
    text = tool_args.get("text")
    if isinstance(text, str) and text:
        needle = text.lower()
        return (lambda line: needle in line.lower()), text
    pattern = tool_args.get("regex")
    if isinstance(pattern, str) and pattern:
        flags = 0
        literal = re.fullmatch(r"/(.*)/([a-z]*)", pattern, re.S)
        if literal:
            pattern, js_flags = literal.groups()
            flags = (re.I if "i" in js_flags else 0) | (re.M if "m" in js_flags else 0)
            flags |= re.S if "s" in js_flags else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error:
            return None
        return (lambda line: compiled.search(line) is not None), pattern
    return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _parent(lines: list[str], index: int, *, with_ref: bool = False) -> int | None:
    indent = _indent(lines[index])
    for j in range(index - 1, -1, -1):
        if lines[j].strip() and _indent(lines[j]) < indent:
            if not with_ref or "[ref=" in lines[j]:
                return j
            indent = _indent(lines[j])
    return None


def _element_line(lines: list[str], index: int) -> str:
    """The element a matched line belongs to, stripped, always naming a ``ref`` if one exists.

    A property line (``/url: …``) stands for the element that owns it; a text node, which has
    no ``ref`` of its own, is shown with the closest ancestor that has one.
    """
    if lines[index].lstrip().startswith("- /"):
        index = _parent(lines, index) or index
    line = lines[index].strip()
    if "[ref=" in line:
        return line
    owner = _parent(lines, index, with_ref=True)
    if owner is None:
        return line
    ref = re.search(r"\[ref=[^\]]+\]", lines[owner])
    if ref is None:
        return line
    role = _ROLE.match(lines[owner].strip())
    return f"{line[: ONE_LINE_MAX // 2]} (inside {role.group(1) if role else 'element'} {ref[0]})"


def _rank(element: str, query: str) -> int:
    name = _NAME.match(element)
    text = _TEXT.match(element)
    wanted = query.strip().lower()
    for value in (name.group(1) if name else None, text.group(1) if text else None):
        if value is not None and value.strip().rstrip(":").lower() == wanted:
            return 0
    role = _ROLE.match(element)
    return 1 if role and role.group(1) in _TARGET_ROLES else 2


def _one_line(element: str) -> str:
    return element if len(element) <= ONE_LINE_MAX else element[: ONE_LINE_MAX - 1] + "…"


def cap_find_result(text: str, tool_args: dict[str, Any], max_chars: int) -> str:
    """``text`` rewritten to fit ``max_chars`` (see the module docstring), or unchanged."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head, sep, body = text.partition("\n\n")
    header_lines = head.split("\n")
    header = _HEADER.match(header_lines[-1]) if header_lines else None
    search = _matcher(tool_args)
    if not sep or header is None or search is None:
        return text
    matches_line, (line_matches, query) = header_lines[-1], search
    tail = ""
    section_at = body.find("\n### ")
    if section_at >= 0:  # keep any later response section (### Page, …) intact
        body, tail = body[:section_at], body[section_at:]
    snippets = body.split(SNIPPET_SEPARATOR)

    # Each snippet repeats its ancestor path, so a matching ancestor shows up in many snippets;
    # it belongs to the first one (page order) and must not lift the others' rank.
    matches: list[_Match] = []
    seen: set[str] = set()
    for number, snippet in enumerate(snippets):
        lines = snippet.split("\n")
        for i, line in enumerate(lines):
            if not line_matches(line):
                continue
            element = _element_line(lines, i)
            if element in seen:
                continue
            seen.add(element)
            # A hit inside a link's URL (`returnto=Note-taking`) is the weakest kind of match.
            rank = 3 if line.lstrip().startswith("- /") else _rank(element, query)
            matches.append(_Match(number, element, rank))
    if not matches:
        return text

    best: dict[int, int] = {}
    for m in matches:
        best[m.snippet] = min(best.get(m.snippet, m.rank), m.rank)
    order = sorted(best, key=lambda n: (best[n], n))

    # The header, the explanatory notes and any later section come out of the same budget.
    budget = max_chars - len(head) - len(tail) - _NOTES_RESERVE
    full: list[int] = []
    used = 0
    for n in order:
        cost = len(snippets[n]) + len(SNIPPET_SEPARATOR)
        if used + cost > budget // 2:
            break
        full.append(n)
        used += cost
    shown_in_full = set(full)

    rest = sorted(
        (m for m in matches if m.snippet not in shown_in_full), key=lambda m: (m.rank, m.snippet)
    )
    one_lines: list[str] = []
    listed: set[str] = set()
    for m in rest:
        if m.element in listed:
            continue
        entry = _one_line(m.element)
        if used + len(entry) + 1 > budget:
            break
        one_lines.append(entry)
        listed.add(m.element)
        used += len(entry) + 1
    in_full = sum(1 for m in matches if m.snippet in shown_in_full)
    unlisted = len({m.element for m in rest} - listed)

    parts = [
        "\n".join(header_lines[:-1] + [matches_line]),
        (
            f"(Too many matches to show in full. The {len(full)} most likely "
            f"snippet(s) — exact name, then buttons/links/fields/headings — are shown whole, "
            f"covering {in_full} match(es); the other matches are listed one line each with "
            "their ref. To see an element's surroundings, call browser_snapshot with its ref "
            "as `target`.)"
        ),
    ]
    if full:
        parts.append(SNIPPET_SEPARATOR.join(snippets[n] for n in sorted(full)))
    if one_lines:
        parts.append("Other matches:\n" + "\n".join(one_lines))
    if unlisted:
        parts.append(
            f"… and {unlisted} more matching element(s) not listed. Narrow the search: use a "
            f'regex that includes the role (e.g. /heading "{query}"/ or /button "{query}"/), '
            "search the exact label, or take a browser_snapshot of just the region (a dialog, "
            "form or list) by its ref."
        )
    capped = "\n\n".join(parts) + tail
    logger.info(
        "browser_find result capped: %d → %d chars (%d snippet(s) in full, %d one-line, "
        "%d not listed) for %s",
        len(text),
        len(capped),
        len(full),
        len(one_lines),
        unlisted,
        query,
    )
    return capped


class FindResultCap:
    """``process_tool_call`` hook applying ``cap_find_result`` to ``browser_find``; wraps a hook."""

    def __init__(self, max_chars: int, inner: ProcessToolCallback | None = None) -> None:
        self.max_chars = max_chars
        self.inner = inner

    async def __call__(
        self,
        ctx: RunContext[Any],
        call_tool: CallToolFunc,
        name: str,
        tool_args: dict[str, Any],
    ) -> ToolResult:
        if self.inner is not None:
            result = await self.inner(ctx, call_tool, name, tool_args)
        else:
            result = await call_tool(name, tool_args)
        if name != FIND_TOOL or self.max_chars <= 0:
            return result
        if isinstance(result, str):
            return cap_find_result(result, tool_args, self.max_chars)
        if isinstance(result, list):
            return [
                cap_find_result(item, tool_args, self.max_chars) if isinstance(item, str) else item
                for item in result
            ]
        return result
