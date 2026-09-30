"""Hold the Generator's locators to the plan's verified text, character for character.

The Generator works blind from the ``TestPlan``, so every text inside a spec locator — the
``getByText``/``getByLabel``/``getByTestId`` argument, a ``getByRole`` ``name``, a CSS/XPath
string, a ``filter`` ``hasText`` — must come from a selector the Planner verified live. Two
failures slip through a prompt rule:

- **Drift.** The model retypes a literal and loses the page's typographic characters
  (``“New note`` → ``"New note``), so the locator no longer matches. When a spec literal equals a
  plan literal after folding typographic quotes, non-breaking spaces, ellipses and whitespace, it
  is rewritten in place to the plan's exact text — deterministic, no model retry.
- **Invention.** The model asserts text it made up from the ``expected`` prose
  (``getByRole('heading', { name: 'Login' })`` on a page whose heading is "Log in"). Anything not
  traceable to the plan is bounced back to the Generator with a ``ModelRetry`` naming the locator.

A literal also counts as verified when it equals a name quoted in a step's ``container`` hint
(``dialog 'Delete note'`` → ``getByRole('dialog', { name: 'Delete note' })``). Variables,
``${…}`` templates and role-only ``getByRole('dialog')`` carry no literal and are not checked.
The scanner is a small TypeScript tokenizer (strings, templates, regex literals, comments), not
a parser: it only needs to find string arguments of locator calls.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from pydantic_ai.exceptions import ModelRetry

from ...core.models import GeneratedTest, TestPlan

logger = logging.getLogger(__name__)

# Methods whose FIRST positional argument is the locator text/selector.
_POSITIONAL = frozenset(
    {"getByText", "getByLabel", "getByTestId", "getByPlaceholder", "getByAltText", "getByTitle",
     "locator"}
)
_METHODS = _POSITIONAL | {"getByRole", "filter"}
# Keys of a locator call's options object whose value is locator text.
_OPTION_KEYS = frozenset({"name", "hasText", "hasNotText"})

# A `/` after one of these starts a regex literal; after anything else it is division.
_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};")

TYPOGRAPHIC = str.maketrans(
    {
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
        "\u00a0": " ", "\u2026": "...",
    }
)
_ESCAPE = re.compile(r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "f": "\f", "v": "\v"}
_CONTAINER_NAME = re.compile(r"'([^']*)'|\"([^\"]*)\"|\u201c([^\u201d]*)\u201d")

MAX_REPORTED = 6


@dataclass(frozen=True)
class _Token:
    # "str" | "tpl" (static template) | "dyn" (template with ${}) | "regex" | "ident" | "punct"
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class LocatorLiteral:
    """One text argument of a locator call, as found in the source."""

    method: str
    value: str  # decoded string value (a regex literal keeps its source form, e.g. /Save/i)
    quote: str  # ', " or ` — "" for a regex literal
    start: int  # source span of the literal token (quotes included)
    end: int
    call: str  # the whole call, for messages


def _decode(inner: str) -> str:
    def replace(match: re.Match[str]) -> str:
        esc = match.group(1)
        if esc.startswith("u{"):
            return chr(int(esc[2:-1], 16))
        if len(esc) == 5 and esc[0] == "u":
            return chr(int(esc[1:], 16))
        if len(esc) == 3 and esc[0] == "x":
            return chr(int(esc[1:], 16))
        if esc == "\n":  # line continuation
            return ""
        return _SIMPLE_ESCAPES.get(esc, esc)

    return _ESCAPE.sub(replace, inner)


def quote_literal(value: str, quote: str) -> str:
    """``value`` as a JS string literal in ``quote`` (', " or `)."""
    body = value.replace("\\", "\\\\").replace(quote, "\\" + quote).replace("\n", "\\n")
    if quote == "`":
        body = body.replace("${", "\\${")
    return f"{quote}{body}{quote}"


def _skip_string(code: str, i: int, quote: str) -> tuple[int, bool]:
    """Index just past the string opened at ``i``; plus whether a template has ``${…}``."""
    j, n, dynamic = i + 1, len(code), False
    while j < n and code[j] != quote:
        if code[j] == "\\":
            j += 2
            continue
        if quote == "`" and code.startswith("${", j):
            dynamic, depth, j = True, 1, j + 2
            while j < n and depth:
                if code[j] in "'\"`":
                    j, _ = _skip_string(code, j, code[j])
                    continue
                depth += {"{": 1, "}": -1}.get(code[j], 0)
                j += 1
            continue
        j += 1
    return min(j + 1, n), dynamic


def _skip_regex(code: str, i: int) -> int:
    j, n, in_class = i + 1, len(code), False
    while j < n:
        c = code[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            return j
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and code[j].isalpha():
                j += 1
            return j
        j += 1
    return j


def _tokenize(code: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c.isspace():
            i += 1
        elif code.startswith("//", i):
            nl = code.find("\n", i)
            i = n if nl < 0 else nl
        elif code.startswith("/*", i):
            close = code.find("*/", i + 2)
            i = n if close < 0 else close + 2
        elif c in "'\"`":
            end, dynamic = _skip_string(code, i, c)
            kind = "str" if c != "`" else ("dyn" if dynamic else "tpl")
            tokens.append(_Token(kind, code[i:end], i, end))
            i = end
        elif c == "/" and (
            not tokens or (tokens[-1].kind == "punct" and tokens[-1].text in _REGEX_PRECEDERS)
        ):
            end = _skip_regex(code, i)
            tokens.append(_Token("regex", code[i:end], i, end))
            i = end
        elif c.isalnum() or c in "_$":
            j = i + 1
            while j < n and (code[j].isalnum() or code[j] in "_$"):
                j += 1
            tokens.append(_Token("ident", code[i:j], i, j))
            i = j
        else:
            tokens.append(_Token("punct", c, i, i + 1))
            i += 1
    return tokens


def _literal(method: str, tok: _Token, call: str) -> LocatorLiteral:
    if tok.kind == "regex":
        return LocatorLiteral(method, tok.text, "", tok.start, tok.end, call)
    return LocatorLiteral(method, _decode(tok.text[1:-1]), tok.text[0], tok.start, tok.end, call)


def _key(tok: _Token) -> str:
    return _decode(tok.text[1:-1]) if tok.kind in ("str", "tpl") else tok.text


def locator_literals(code: str) -> list[LocatorLiteral]:
    """Every text argument of a locator call in ``code``, in source order."""
    tokens = _tokenize(code)
    found: list[LocatorLiteral] = []
    for k, tok in enumerate(tokens):
        if tok.kind != "ident" or tok.text not in _METHODS:
            continue
        if k + 1 >= len(tokens) or tokens[k + 1].text != "(":
            continue
        stack: list[str] = []
        arg = 0
        hits: list[_Token] = []
        j = k + 2
        while j < len(tokens):
            t = tokens[j]
            if t.kind == "punct" and t.text in "([{":
                stack.append(t.text)
            elif t.kind == "punct" and t.text in ")]}":
                if not stack:
                    break
                stack.pop()
            elif t.kind == "punct" and t.text == "," and not stack:
                arg += 1
            elif t.kind in ("str", "tpl", "regex"):
                positional = not stack and arg == 0 and tok.text in _POSITIONAL
                keyed = (
                    stack == ["{"]
                    and tokens[j - 1].text == ":"
                    and tokens[j - 2].kind in ("ident", "str", "tpl")
                    and _key(tokens[j - 2]) in _OPTION_KEYS
                )
                if positional or keyed:
                    hits.append(t)
            j += 1
        end = tokens[j].end if j < len(tokens) else len(code)
        call = code[tok.start:end]
        found.extend(_literal(tok.text, t, call) for t in hits)
    return found


def fold_text(value: str) -> str:
    """``value`` with typographic quotes, NBSPs and ellipses made plain and spacing collapsed."""
    return " ".join(value.translate(TYPOGRAPHIC).split())


def _plan_literals(plan: TestPlan) -> tuple[set[str], dict[str, str]]:
    exact: set[str] = set()
    folded: dict[str, str] = {}
    for step in plan.steps:
        for selector in (step.target_selector, step.assert_selector):
            for lit in locator_literals(selector or ""):
                exact.add(lit.value)
                if lit.quote:
                    folded.setdefault(fold_text(lit.value), lit.value)
        for match in _CONTAINER_NAME.finditer(step.container or ""):
            exact.add(next(g for g in match.groups() if g is not None))
    return exact, folded


def check_spec_literals(code: str, plan: TestPlan) -> tuple[str, int, list[LocatorLiteral]]:
    """Repair drifted locator literals; return ``(code, repaired_count, unverified)``.

    ``unverified`` lists the literals that match no plan selector even after folding —
    invented text the Generator must replace.
    """
    exact, folded = _plan_literals(plan)
    repairs: list[tuple[LocatorLiteral, str]] = []
    unverified: list[LocatorLiteral] = []
    for lit in locator_literals(code):
        if lit.value in exact:
            continue
        fix = folded.get(fold_text(lit.value)) if lit.quote else None
        if fix is None:
            unverified.append(lit)
        else:
            repairs.append((lit, fix))
    for lit, fix in sorted(repairs, key=lambda r: r[0].start, reverse=True):
        code = code[: lit.start] + quote_literal(fix, lit.quote) + code[lit.end :]
    return code, len(repairs), unverified


def bounce_message(unverified: list[LocatorLiteral]) -> str:
    lines = [f"- `{lit.call}` — {lit.value!r} is not in any plan selector" for lit in
             unverified[:MAX_REPORTED]]
    if len(unverified) > MAX_REPORTED:
        lines.append(f"- … and {len(unverified) - MAX_REPORTED} more")
    return (
        "These locators use text that no plan `target_selector` / `assert_selector` contains, "
        "so nothing verified it on the live page:\n"
        + "\n".join(lines)
        + "\nReplace only these locators; keep every other line as it was. Copy plan selectors "
        "character for character. Where a step has no `assert_selector`, don't assert made-up "
        "text — use `page.waitForURL(page_url)`, the next step's `target_selector`, or no "
        "after-assertion. Return the corrected GeneratedTest."
    )


def spec_literal_validator(plan: TestPlan) -> Callable[[GeneratedTest], GeneratedTest]:
    """An output validator binding the Generator's locators to ``plan``'s verified text."""

    def validate(output: GeneratedTest) -> GeneratedTest:
        code, repaired, unverified = check_spec_literals(output.code, plan)
        if unverified:
            logger.info(
                "Generator used %d locator text(s) not in the plan (%s) — asking it to fix them",
                len(unverified),
                ", ".join(repr(lit.value) for lit in unverified[:MAX_REPORTED]),
            )
            raise ModelRetry(bounce_message(unverified))
        if repaired:
            logger.info(
                "Generator: restored %d locator text(s) to the plan's exact characters "
                "(typographic quotes / spacing)",
                repaired,
            )
            return output.model_copy(update={"code": code})
        return output

    return validate
