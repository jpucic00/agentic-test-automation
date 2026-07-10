"""Minimal static layer for KB seeding (RETRIEVAL_MEMORY_PLAN.md §5.1).

Everything deterministic about the corpus lives here — and ONLY these things
(the v1 call-graph traversal, receiver typing and constant folding retired, they
never generalized past the one Java suite they were tuned for):

- **Tree/suite skeleton** — a browsable folder + file inventory.
- **Test discovery** — every method whose *decoration zone* matches a configurable
  ``TEST_MARKER_REGEX`` (default ``@Xray(testCase = "KEY")``) is one test/record;
  ``group(1)`` is its linked key. Hand-written ``*.spec.ts`` files are one record
  each, keyed by a marker in the file if present.
- **Parity accounting** — markers seen in the tree vs tests actually discovered.
  A gap means the scanner lost a test; it is always counted and reported, never
  silent (the failure mode that repeatedly bit the v1 extractor).
- **Stable record ids** — computed BEFORE any model call, so a re-seed skips
  already-stored records without paying for an LLM.

Comment-, string- and text-block-aware scanning survives from the old extractor
because discovery needs it: a marker inside a ``//`` comment or a ``"…"`` string
must not fake a test, and a SQL/JSON text block must not desynchronize the brace
scanner and swallow a whole class. Nothing else is parsed — the agentic Distiller
reads whatever else it needs through its own repo tools.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..config import DEFAULT_TEST_MARKER_REGEX
from .models import KBSource, make_record_id

logger = logging.getLogger(__name__)

# The test's own code is stored payload / the distiller's exploration seed, not
# something we parse — capped so a pathological file can't bloat a record.
_SOURCE_CAP = 20_000

# A Java type: dotted name, optional one-level generics, optional arrays. Contains
# NO bare space/comma alternatives — comment-masking turns comments into long
# space runs, and a type class that matches raw spaces sent this regex into
# catastrophic backtracking (the fe0e389 lesson). Spaces live only inside <...>.
_TYPE_RE = r"[\w.$]+(?:<[^;{}()]*>)?(?:\[\])*"
# Method signature: same-line annotations (anchored by `@`), modifier keywords, an
# optional method type-parameter list, then `TYPE name(params) {`. Parses
# `@Override public void x() {` and `public <T> T x()` without backtracking.
_METHOD_SIG_RE = re.compile(
    r"^[ \t]*(?:@\w+(?:\([^)]*\))?[ \t]+)*"
    r"(?:(?:public|protected|private|static|final|synchronized|abstract|default)\s+)*"
    r"(?:<[^<>;{}()]{0,200}(?:<[^<>;{}()]{0,100}>[^<>;{}()]{0,100})?>\s+)?"
    r"(" + _TYPE_RE + r")\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w, .]+\s*)?\{",
    re.M,
)
# Class declarations are scanned on string-blanked text so `"class Foo"` in a
# literal cannot fake a declaration.
_CLASS_DECL_RE = re.compile(r"\b(?:class|interface|enum)\s+(\w+)")
# Return types / names that _METHOD_SIG_RE can match but are not methods:
# `new Thread() {` (type `new`) and `else if (…) {` (type `else`, name `if`).
_NON_METHOD_RETURNS = {"new", "else"}
_NON_METHOD_NAMES = {"if", "for", "while", "switch", "catch", "synchronized", "return", "do"}


@dataclass
class DiscoveredTest:
    """One test found in the corpus — a future KB record, id already fixed."""

    ref: str  # repo-relative "path#symbol" (java) or "path" (ts) — the id ref when unlinked
    path: str  # repo-relative file path
    symbol: str  # "Class.method" (java) or the spec file stem (ts)
    language: Literal["java", "ts"]
    source: KBSource  # selenium-import (java) | playwright-import (ts)
    xray_key: str  # the marker's key, or "" when unlinked
    record_id: str  # make_record_id(project, source, xray_key or ref) — stable, pre-model
    code: str  # the test's own source (comments intact, capped) — the distiller's seed


@dataclass
class DiscoveryResult:
    """Everything the deterministic layer knows about a corpus + project."""

    tests: list[DiscoveredTest] = field(default_factory=list)
    markers_seen: int = 0  # marker matches across Java files — the parity numerator
    java_files: int = 0
    spec_files: int = 0
    fallback_files: list[str] = field(default_factory=list)  # files that needed whole-file scan
    skeleton: str = ""  # tree/suite outline (browsable artifact)

    @property
    def discovered(self) -> int:
        return len(self.tests)

    @property
    def java_discovered(self) -> int:
        return sum(1 for t in self.tests if t.language == "java")

    @property
    def parity_gap(self) -> int:
        """Java markers seen minus Java tests discovered — >0 means tests were lost."""
        return self.markers_seen - self.java_discovered


def _compile_marker(pattern: str) -> re.Pattern[str]:
    """Compile ``pattern`` and require exactly the one key-capturing group."""
    try:
        marker = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"TEST_MARKER_REGEX {pattern!r} is not a valid regex: {exc}") from exc
    if marker.groups < 1:
        raise ValueError(
            f"TEST_MARKER_REGEX {pattern!r} must capture the test key in group(1) "
            "(add a capturing group, e.g. '\"([^\"]+)\"')"
        )
    return marker


def discover_tests(
    project_key: str,
    *,
    selenium_root: Path | None = None,
    playwright_dir: Path | None = None,
    marker_regex: str = DEFAULT_TEST_MARKER_REGEX,
) -> DiscoveryResult:
    """Discover every corpus test under the given roots, with parity + stable ids.

    ``selenium_root`` is walked for ``*.java`` (marker-annotated methods);
    ``playwright_dir`` for ``*.spec.ts`` (one record per file). ``project_key``
    routes the records and seeds their ids; the id ref is the marker key when
    linked, else the repo-relative ``path#symbol``.
    """
    project = project_key.strip().upper()
    marker = _compile_marker(marker_regex)
    result = DiscoveryResult()

    if selenium_root is not None:
        _discover_java(result, project, selenium_root, marker)
    if playwright_dir is not None:
        _discover_playwright(result, project, playwright_dir, marker)

    result.skeleton = _render_skeleton(result, selenium_root, playwright_dir)
    if result.parity_gap > 0:
        logger.warning(
            ":: DISCOVERY GAP — %d Java marker(s) in the tree but only %d test(s) "
            "discovered; %d unaccounted for",
            result.markers_seen,
            result.java_discovered,
            result.parity_gap,
        )
    return result


def _discover_java(
    result: DiscoveryResult, project: str, root: Path, marker: re.Pattern[str]
) -> None:
    for path in sorted(root.rglob("*.java")):
        result.java_files += 1
        text = path.read_text(errors="replace")
        masked = _mask_comments(text)  # comments blanked, string/marker literals intact
        declscan = _mask_strings(masked)  # + string contents blanked, for decl scans
        rel = str(path.relative_to(root))

        # Count markers on the string-BLANKED text: a real @Xray annotation is code
        # (its position + structure survive), but a marker sitting inside a string
        # literal is not a test and must not inflate the parity numerator. The
        # per-method key capture below still runs on comment-masked text (strings
        # intact) so the KEY literal survives.
        marker_starts = [m.start() for m in marker.finditer(declscan)]
        result.markers_seen += len(marker_starts)

        decls = _class_decls(declscan)
        uncovered = [
            at for at in marker_starts if not any(s <= at < e for _, s, e in decls)
        ]
        if uncovered:
            # A marker landed outside every parsed class — a construct the scanner
            # cannot span. Losing tests silently is the one unacceptable outcome:
            # index the WHOLE FILE as one class (pre-slicing behavior) and say so.
            name = decls[0][0] if decls else path.stem
            decls = [(name, 0, len(text))]
            result.fallback_files.append(rel)
            logger.warning(
                ":: %s: class structure not fully parsed — whole-file fallback "
                "(%d marker(s) outside every parsed class)",
                rel,
                len(uncovered),
            )

        for class_name, start, end in decls:
            class_text = text[start:end]
            class_masked = masked[start:end]
            for method_name, m_start, m_end, first_line in _methods_in(class_masked):
                zone = _decoration_zone(class_masked, m_start, first_line)
                hit = marker.search(zone)
                if hit is None:
                    continue
                key = hit.group(1).strip()
                ref = f"{rel}#{method_name}"
                result.tests.append(
                    DiscoveredTest(
                        ref=ref,
                        path=rel,
                        symbol=f"{class_name}.{method_name}",
                        language="java",
                        source="selenium-import",
                        xray_key=key,
                        record_id=make_record_id(project, "selenium-import", key or ref),
                        code=class_text[m_start:m_end][:_SOURCE_CAP],
                    )
                )


def _discover_playwright(
    result: DiscoveryResult, project: str, directory: Path, marker: re.Pattern[str]
) -> None:
    for path in sorted(directory.rglob("*.spec.ts")):
        result.spec_files += 1
        text = path.read_text(errors="replace")
        rel = str(path.relative_to(directory))
        # A hand-written spec may name its Xray key in a comment — search the raw
        # text (comments included), unlike Java where the marker is real code.
        hit = marker.search(text)
        key = hit.group(1).strip() if hit else ""
        result.tests.append(
            DiscoveredTest(
                ref=rel,
                path=rel,
                symbol=path.stem,
                language="ts",
                source="playwright-import",
                xray_key=key,
                record_id=make_record_id(project, "playwright-import", key or rel),
                code=text[:_SOURCE_CAP],
            )
        )


def render_discovery_summary(result: DiscoveryResult) -> str:
    """The parity/inventory block for the seeding summary (§5.1 — always written)."""
    gap_note = " — **DISCOVERY GAP, investigate**" if result.parity_gap > 0 else ""
    fallback = (
        "; ".join(result.fallback_files) if result.fallback_files else "(none)"
    )
    return "\n".join(
        [
            f"- Java files scanned: {result.java_files}",
            f"- Playwright specs scanned: {result.spec_files}",
            f"- markers seen (Java): {result.markers_seen} "
            f"(Java tests discovered: {result.java_discovered}){gap_note}",
            f"- total tests discovered: {result.discovered}",
            f"- whole-file fallback files: {len(result.fallback_files)} — {fallback}",
        ]
    )


# --- salvaged scanning primitives (discovery-only subset of the v1 extractor) ---


def _text_block_end(text: str, start: int) -> int:
    """Index just past the closing ``\"\"\"`` of a Java text block opening at
    ``start`` (fail-safe: end of text). Text blocks (SQL/JSON in DB/API tests)
    desynchronize a pairwise quote scanner, so every scanner treats one as a
    single literal (the regression that silently dropped whole classes)."""
    i, n = start, len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"' and text[i + 1 : i + 3] == '""':
            return i + 3
        i += 1
    return n


def _mask_comments(text: str) -> str:
    """Length-preserving copy with comment bodies blanked to spaces (newlines and
    string/char literals kept). All structural scanning runs on the masked text so
    a brace/apostrophe in a comment can't corrupt it, and a commented-out marker
    can't fake a test; snippets are still sliced from the ORIGINAL at the same
    offsets."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if char == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif char == "/" and nxt == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
        elif char == '"' and text[i + 1 : i + 3] == '""':
            i = _text_block_end(text, i + 3)  # text block: one literal, kept verbatim
        elif char in ('"', "'"):
            quote = char
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
        else:
            i += 1
    return "".join(out)


def _mask_strings(masked: str) -> str:
    """Blank string/char literal CONTENTS (quotes kept) of comment-masked text —
    for declaration scans where ``"class Foo"`` must not fake a declaration. Java
    text blocks are blanked as one literal (their quote-heavy bodies must never
    desynchronize the scan)."""
    out = list(masked)
    i, n = 0, len(masked)
    while i < n:
        char = masked[i]
        if char == '"' and masked[i + 1 : i + 3] == '""':
            close = _text_block_end(masked, i + 3)
            content_end = close - 3 if masked[close - 3 : close] == '"""' else close
            for j in range(i + 3, content_end):
                if masked[j] != "\n":
                    out[j] = " "
            i = close
        elif char in ('"', "'"):
            quote = char
            i += 1
            while i < n:
                if masked[i] == "\\":
                    out[i] = " "
                    if i + 1 < n:
                        out[i + 1] = " "
                    i += 2
                    continue
                if masked[i] == quote:
                    i += 1
                    break
                if masked[i] != "\n":
                    out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def _matching_brace(masked: str, open_index: int) -> int | None:
    """Index of the ``}`` matching the ``{`` at ``open_index`` in comment-masked
    text; string/char literals and text blocks are skipped so a brace inside them
    never miscounts."""
    depth = 0
    in_string: str | None = None
    i = open_index
    while i < len(masked):
        char = masked[i]
        if in_string:
            if char == "\\":
                i += 2
                continue
            if char == in_string:
                in_string = None
        elif char == '"' and masked[i + 1 : i + 3] == '""':
            i = _text_block_end(masked, i + 3)
            continue
        elif char in ('"', "'"):
            in_string = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _class_decls(declscan: str) -> list[tuple[str, int, int]]:
    """Every top-level ``(name, start, end)`` in string-blanked text. Nested
    declarations stay inside the enclosing class's slice."""
    decls: list[tuple[str, int, int]] = []
    taken: list[tuple[int, int]] = []
    for match in _CLASS_DECL_RE.finditer(declscan):
        if any(start <= match.start() < end for start, end in taken):
            continue
        brace_at = declscan.find("{", match.end())
        if brace_at == -1:
            continue
        body_end = _matching_brace(declscan, brace_at)
        if body_end is None:
            continue
        decls.append((match.group(1), match.start(), body_end + 1))
        taken.append((match.start(), body_end + 1))
    return decls


def _methods_in(class_masked: str) -> list[tuple[str, int, int, str]]:
    """``(name, start, end, first_line)`` for each real method in a class slice.

    Control-flow blocks that _METHOD_SIG_RE can match (``new X() {``,
    ``else if (…) {``) are filtered out — they never carry a test marker anyway,
    but skipping them keeps the decoration-zone search honest.
    """
    methods: list[tuple[str, int, int, str]] = []
    for match in _METHOD_SIG_RE.finditer(class_masked):
        name, return_type = match.group(2), match.group(1)
        if name in _NON_METHOD_NAMES or return_type.strip() in _NON_METHOD_RETURNS:
            continue
        end = _matching_brace(class_masked, match.end() - 1)
        if end is None:
            continue
        first_line = class_masked[match.start() : end + 1].split("\n", 1)[0]
        methods.append((name, match.start(), end + 1, first_line))
    return methods


def _decoration_zone(class_masked: str, method_start: int, first_line: str) -> str:
    """The annotation zone of a method: the text above the signature (after the
    previous member's closing brace) plus the signature's own first line — so
    one-line forms like ``@Xray(...) public void t() {`` are covered too."""
    preamble = class_masked[max(0, method_start - 400) : method_start]
    return preamble.rsplit("}", 1)[-1] + "\n" + first_line


def _render_skeleton(
    result: DiscoveryResult,
    selenium_root: Path | None,
    playwright_dir: Path | None,
) -> str:
    """A compact folder/suite outline: per top-level suite dir, test counts."""
    roots = ", ".join(
        str(r) for r in (selenium_root, playwright_dir) if r is not None
    )
    lines = [f"Corpus roots: {roots or '(none)'}"]
    by_suite: dict[str, int] = {}
    for test in result.tests:
        suite = test.path.split("/", 1)[0] if "/" in test.path else "(root)"
        by_suite[suite] = by_suite.get(suite, 0) + 1
    if by_suite:
        lines.append("Suites (by top-level folder):")
        lines.extend(
            f"  {suite}: {count} test(s)" for suite, count in sorted(by_suite.items())
        )
    lines.append(
        f"Files: {result.java_files} Java, {result.spec_files} Playwright spec(s); "
        f"{result.discovered} test(s) discovered"
    )
    return "\n".join(lines)


def record_ids(tests: Sequence[DiscoveredTest]) -> list[str]:
    """The stable ids of ``tests`` — the seeding CLI checks these against the store
    to skip already-seeded records before spending any model call."""
    return [test.record_id for test in tests]
