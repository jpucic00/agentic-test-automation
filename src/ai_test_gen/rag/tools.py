"""Read-only repo tools shared by the Mapper and the Distiller (RETRIEVAL_MEMORY_PLAN.md §5.2/§5.3).

The offline seeding agents don't parse the corpus statically — they READ it, the
way a person browsing an unfamiliar suite would: open a file, grep for a symbol,
list a directory. Three trivial, read-only tools cover that, and cover it for ANY
language/framework (the reason the v1 Java-only static walker was retired):

- ``read_file(path[, start, end])`` — a file's text, optionally a line range.
- ``search(pattern[, glob])`` — regex/literal grep across the corpus.
- ``list_dir(path)`` — a directory's entries.

**Sandbox.** The agent supplies paths/patterns as *data*; a model can hallucinate
``../../etc/passwd`` as easily as a real path. Every path is resolved and confirmed
to stay inside one of the corpus roots (``.resolve()`` collapses ``..`` and follows
symlinks first, so an escape via either is rejected). Reads are byte-capped so one
pathological file can't flood the model's context.

**Instrumentation.** ``files_opened`` and ``tool_calls`` accumulate across a run so
the Distiller can fold them into each record's ``ExplorationTrace`` (§5.4) and the
Mapper can report exploration breadth — without either agent having to self-report.

**Addressing.** One corpus root → paths are relative to it, no prefix. Two or more
roots (e.g. a Selenium tree + a Playwright dir) → each gets a short label and paths
read ``label/rest`` so they're unambiguous. Nested roots are de-duplicated (the
parent already covers the child) so the same file never has two addresses.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Result caps — recon must inform the model, never bury it. A single read is bounded,
# a search returns the strongest few matches, a listing the first screenful.
_MAX_READ_CHARS = 16_000
_MAX_SEARCH_MATCHES = 60
_MAX_SEARCH_CHARS = 8_000
_MAX_LIST_ENTRIES = 200
_MAX_INVENTORY_FILES = 500

# Directories that are never source: VCS, dependency caches, build output, IDE state.
_IGNORE_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "target", "build", "out", "dist",
        "__pycache__", ".venv", "venv", ".idea", ".gradle", ".mvn", "bin", "obj",
        ".pytest_cache", ".ruff_cache", "coverage", ".next",
    }
)

# Extensions treated as readable source for search + inventory. read_file itself will
# open anything under a root (a distiller may want an odd config file), refusing only
# on detected binary content — but broad scans stay on text to avoid noise.
_TEXT_EXTENSIONS = frozenset(
    {
        ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
        ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rb", ".go", ".cs",
        ".properties", ".xml", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".html", ".htm", ".css", ".scss", ".sql", ".feature", ".md", ".txt", ".csv",
        ".env", ".sh",
    }
)


def _is_text_file(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_EXTENSIONS


class RepoTools:
    """Sandboxed, read-only file access over one or more corpus roots.

    Register on a pydantic-ai agent with :meth:`register`; the three bound methods
    become the agent's ``read_file`` / ``search`` / ``list_dir`` tools (their
    docstrings are the tool descriptions the model reads).
    """

    def __init__(self, roots: Sequence[Path]) -> None:
        resolved: list[Path] = []
        for root in roots:
            rp = Path(root).expanduser().resolve()
            if not rp.is_dir():
                logger.warning("RepoTools: corpus root %s is not a directory — skipping", rp)
                continue
            resolved.append(rp)
        # De-nest: drop any root contained in another (its files already addressable
        # via the ancestor, which would otherwise give the same file two addresses).
        deduped: list[Path] = []
        for rp in sorted(set(resolved), key=lambda p: len(p.parts)):
            if any(rp == kept or rp.is_relative_to(kept) for kept in deduped):
                continue
            deduped.append(rp)
        if not deduped:
            raise ValueError("RepoTools needs at least one existing corpus root directory")
        self._roots = deduped
        # Label roots only when there is ambiguity (2+). Labels de-collide by suffixing.
        self._labeled_roots: list[tuple[str, Path]] = self._assign_labels(deduped)
        self.files_opened: set[str] = set()
        self.tool_calls = 0

    @staticmethod
    def _assign_labels(roots: list[Path]) -> list[tuple[str, Path]]:
        if len(roots) == 1:
            return [("", roots[0])]
        labeled: list[tuple[str, Path]] = []
        used: set[str] = set()
        for root in roots:
            base = root.name or "root"
            label = base
            i = 2
            while label in used:
                label = f"{base}-{i}"
                i += 1
            used.add(label)
            labeled.append((label, root))
        return labeled

    # --- addressing ----------------------------------------------------------
    def _resolve(self, raw: str) -> Path | None:
        """The absolute path for a model-supplied address, or None if it escapes/misses.

        Confirms containment AFTER ``.resolve()`` (which collapses ``..`` and follows
        symlinks), so neither can walk outside a root.
        """
        raw = (raw or "").strip().lstrip("/")
        for label, root in self._labeled_roots:
            rel = raw
            if label:
                if raw == label:
                    rel = ""
                elif raw.startswith(label + "/"):
                    rel = raw[len(label) + 1 :]
                else:
                    continue
            candidate = (root / rel).resolve()
            if candidate == root or candidate.is_relative_to(root):
                return candidate
        return None

    def _address(self, path: Path) -> str:
        """The labeled, root-relative address of an absolute path inside a root."""
        for label, root in self._labeled_roots:
            if path == root or path.is_relative_to(root):
                rel = "" if path == root else str(path.relative_to(root))
                return f"{label}/{rel}" if label else rel
        return str(path)

    def address_of(self, path: Path) -> str:
        """Public alias of :meth:`_address` — the map's fingerprinting uses it."""
        return self._address(path)

    def resolve_citation(self, citation: str) -> Path | None:
        """Resolve a ``file`` or ``file#symbol`` citation to a corpus file, or None.

        Used to string-check that a model's path citation names a real file (the map's
        recall-with-flagging), and to fingerprint the files a map section depends on.
        """
        ref = (citation or "").split("#", 1)[0].strip()
        if not ref:
            return None
        resolved = self._resolve(ref)
        return resolved if resolved is not None and resolved.is_file() else None

    # --- inventory (for the Mapper's file list; not an agent tool) -----------
    def inventory(self, max_files: int = _MAX_INVENTORY_FILES) -> list[str]:
        """All source-file addresses under the roots (sorted, capped) — the map's file list."""
        found: list[str] = []
        for _, root in self._labeled_roots:
            for path in sorted(root.rglob("*")):
                if len(found) >= max_files:
                    return found
                if not path.is_file() or not _is_text_file(path):
                    continue
                if any(part in _IGNORE_DIRS for part in path.relative_to(root).parts):
                    continue
                found.append(self._address(path))
        return found

    # --- agent tools ---------------------------------------------------------
    def read_file(self, path: str, start: int | None = None, end: int | None = None) -> str:
        """Read a corpus file's text, optionally just a line range.

        Args:
            path: Repo-relative path to the file (as shown in the file list or a citation).
            start: Optional 1-indexed first line to return (inclusive).
            end: Optional 1-indexed last line to return (inclusive).

        Returns the file's contents (or the requested line span), truncated if very
        large. Use this to follow a helper, page object, or locator file into its source.
        """
        self.tool_calls += 1
        resolved = self._resolve(path)
        if resolved is None or not resolved.is_file():
            return f"read_file: no such file under the corpus roots: {path!r}"
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            return f"read_file: could not read {path!r}: {exc}"
        if b"\x00" in raw[:4096]:
            return f"read_file: {path!r} looks binary — skipped"
        text = raw.decode("utf-8", errors="replace")
        self.files_opened.add(self._address(resolved))
        if start is not None or end is not None:
            lines = text.splitlines()
            lo = max(1, start or 1)
            hi = min(len(lines), end or len(lines))
            if lo > hi:
                return (
                    f"read_file: empty line range {start}..{end} for {path!r} "
                    f"({len(lines)} lines)"
                )
            text = "\n".join(lines[lo - 1 : hi])
            header = f"# {self._address(resolved)} lines {lo}-{hi} of {len(lines)}\n"
        else:
            header = f"# {self._address(resolved)}\n"
        if len(text) > _MAX_READ_CHARS:
            text = text[:_MAX_READ_CHARS] + "\n…[truncated — narrow the range with start/end]"
        return header + text

    def search(self, pattern: str, glob: str | None = None) -> str:
        """Search the corpus for a regex (or literal) across files.

        Args:
            pattern: Regex to match line-by-line; if it isn't a valid regex it is matched literally.
            glob: Optional filename/path glob to restrict the files searched, e.g. "*.properties"
                or "**/*.java".

        Returns up to a screenful of ``path:line: text`` matches. Use this to find where a
        helper/constant is defined or used, or to locate locator files a listing wouldn't reveal.
        """
        self.tool_calls += 1
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        matches: list[str] = []
        total = 0
        chars = 0
        for _, root in self._labeled_roots:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or not _is_text_file(path):
                    continue
                rel_parts = path.relative_to(root).parts
                if any(part in _IGNORE_DIRS for part in rel_parts):
                    continue
                address = self._address(path)
                if glob and not _glob_matches(glob, address):
                    continue
                try:
                    lines = path.read_text(errors="replace").splitlines()
                except OSError:
                    continue
                for lineno, line in enumerate(lines, 1):
                    if regex.search(line):
                        total += 1
                        if len(matches) < _MAX_SEARCH_MATCHES and chars < _MAX_SEARCH_CHARS:
                            entry = f"{address}:{lineno}: {line.strip()[:200]}"
                            matches.append(entry)
                            chars += len(entry)
        if not matches:
            return f"search: no matches for {pattern!r}" + (f" in {glob!r}" if glob else "")
        header = f"# {total} match(es) for {pattern!r}"
        if total > len(matches):
            header += f" (showing first {len(matches)})"
        return header + "\n" + "\n".join(matches)

    def list_dir(self, path: str = ".") -> str:
        """List a corpus directory's entries (directories marked with a trailing '/').

        Args:
            path: Repo-relative directory path; "." (default) lists the corpus root(s).

        Use this to orient — see a suite's package layout, find where page objects or
        resources live — before reading individual files.
        """
        self.tool_calls += 1
        # "." with multiple roots → show each root as a top-level entry.
        if path.strip() in ("", ".") and len(self._labeled_roots) > 1:
            return "\n".join(f"{label}/" for label, _ in self._labeled_roots)
        resolved = self._resolve(path)
        if resolved is None or not resolved.is_dir():
            return f"list_dir: no such directory under the corpus roots: {path!r}"
        entries: list[str] = []
        for child in sorted(resolved.iterdir(), key=lambda c: (c.is_file(), c.name)):
            if child.name in _IGNORE_DIRS:
                continue
            entries.append(f"{child.name}/" if child.is_dir() else child.name)
            if len(entries) >= _MAX_LIST_ENTRIES:
                entries.append("…[more entries omitted]")
                break
        base = self._address(resolved) or "."
        return f"# {base}/\n" + ("\n".join(entries) if entries else "(empty)")

    # --- registration --------------------------------------------------------
    def register(self, agent: Any) -> None:
        """Attach ``read_file`` / ``search`` / ``list_dir`` to a pydantic-ai agent."""
        agent.tool_plain(self.read_file)
        agent.tool_plain(self.search)
        agent.tool_plain(self.list_dir)


def _glob_matches(glob: str, address: str) -> bool:
    """Forgiving glob match: full-path when the glob has a separator, else filename.

    ``**`` is tolerated (fnmatch treats it like ``*``) so a model passing ``**/*.java``
    still matches ``a/b/C.java``.
    """
    normalized = glob.replace("**/", "*/").replace("**", "*")
    if "/" in glob:
        return fnmatch.fnmatch(address, normalized) or fnmatch.fnmatch(address, "*/" + normalized)
    return fnmatch.fnmatch(os.path.basename(address), normalized)
