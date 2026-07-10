"""Offline tests for the shared read-only repo tools (rag/tools.py).

The Mapper and Distiller feed model-supplied paths/patterns straight into these
tools, so the sandbox is a security boundary: a path can never escape a corpus
root, reads are capped, and instrumentation is honest. Multi-root addressing and
the citation helpers the suite map relies on are pinned here too.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_test_gen.rag.tools import RepoTools


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "suite"
    _write(
        root,
        "pages/LoginPage.java",
        'class LoginPage {\n  By EMAIL = By.id("login-email");\n}\n',
    )
    _write(root, "core/BasePage.java", "class BasePage {\n  void click(By b) {}\n}\n")
    _write(root, "resources/locators.properties", "login.email=login-email\n")
    _write(root, "node_modules/junk.js", "should be ignored")
    return root


class TestSandbox:
    def test_read_file_returns_content_and_records_instrumentation(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        out = tools.read_file("pages/LoginPage.java")
        assert "By.id(\"login-email\")" in out
        assert tools.tool_calls == 1
        assert "pages/LoginPage.java" in tools.files_opened

    def test_read_file_line_range(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        out = tools.read_file("pages/LoginPage.java", 2, 2)
        assert "login-email" in out
        assert "class LoginPage" not in out

    def test_parent_traversal_is_rejected(self, corpus: Path, tmp_path: Path) -> None:
        (tmp_path / "secret.txt").write_text("top secret")
        tools = RepoTools([corpus])
        out = tools.read_file("../secret.txt")
        assert "no such file" in out
        assert "top secret" not in out
        assert tools.files_opened == set()  # nothing outside the root was opened

    def test_absolute_path_escape_is_neutralized(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        assert "no such file" in tools.read_file("/etc/hosts")

    def test_binary_file_is_refused(self, corpus: Path) -> None:
        (corpus / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        tools = RepoTools([corpus])
        assert "looks binary" in tools.read_file("blob.bin")


class TestSearch:
    def test_search_finds_matches_with_locations(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        out = tools.search(r'By\.id\("([^"]+)"\)')
        assert "pages/LoginPage.java:2:" in out

    def test_search_glob_narrows_files(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        out = tools.search("login-email", glob="*.properties")
        assert "locators.properties" in out
        assert "LoginPage.java" not in out

    def test_invalid_regex_falls_back_to_literal(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        # An unbalanced paren is not a valid regex; it must be matched literally, not raise.
        # The source line contains `By.id("login-email"` as a substring.
        out = tools.search('By.id("login-email"')
        assert "LoginPage.java" in out

    def test_no_match_is_a_message_not_an_error(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        assert "no matches" in tools.search("zzz-not-present")

    def test_ignored_dirs_are_not_searched(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        assert "no matches" in tools.search("should be ignored")


class TestListingAndInventory:
    def test_list_dir_marks_directories(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        out = tools.list_dir("pages")
        assert "LoginPage.java" in out

    def test_list_root_omits_ignored_dirs(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        out = tools.list_dir(".")
        assert "core/" in out
        assert "node_modules" not in out

    def test_inventory_lists_source_files_only(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        inv = tools.inventory()
        assert "pages/LoginPage.java" in inv
        assert "resources/locators.properties" in inv
        assert all("node_modules" not in f for f in inv)


class TestMultiRoot:
    def test_two_roots_get_labels_and_resolve(self, tmp_path: Path) -> None:
        sel = tmp_path / "selenium"
        pw = tmp_path / "playwright"
        _write(sel, "A.java", "class A {}\n")
        _write(pw, "b.spec.ts", "test('b', () => {});\n")
        tools = RepoTools([sel, pw])
        listing = tools.list_dir(".")
        assert "selenium/" in listing and "playwright/" in listing
        assert "class A" in tools.read_file("selenium/A.java")
        assert "test('b'" in tools.read_file("playwright/b.spec.ts")

    def test_nested_roots_are_denested(self, tmp_path: Path) -> None:
        parent = tmp_path / "suite"
        _write(parent, "child/x.java", "class X {}\n")
        # Passing both the parent and its child must not double-address x.java.
        tools = RepoTools([parent, parent / "child"])
        assert "class X" in tools.read_file("child/x.java")  # single-root addressing, no label


class TestCitationHelpers:
    def test_resolve_citation_strips_symbol(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        resolved = tools.resolve_citation("pages/LoginPage.java#EMAIL")
        assert resolved is not None and resolved.name == "LoginPage.java"

    def test_resolve_citation_missing_returns_none(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        assert tools.resolve_citation("pages/Ghost.java") is None

    def test_address_of_round_trips(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        resolved = tools.resolve_citation("core/BasePage.java")
        assert resolved is not None
        assert tools.address_of(resolved) == "core/BasePage.java"

    def test_empty_roots_raise(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="at least one existing corpus root"):
            RepoTools([tmp_path / "does-not-exist"])
