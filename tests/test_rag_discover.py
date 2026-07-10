"""Offline tests for the minimal static discovery layer (rag/discover.py).

The v2 seeding foundation keeps ONLY discovery, parity accounting, skeleton and
stable ids deterministic (RETRIEVAL_MEMORY_PLAN.md §5.1). These tests pin the
guarantees that repeatedly broke the v1 extractor: a marker in a comment/string
never fakes a test, a Java text block never swallows a class, a parse-hostile
file falls back instead of losing tests silently, and every gap is counted.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_test_gen.rag.discover import discover_tests, render_discovery_summary
from ai_test_gen.rag.models import make_record_id


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


_ONE_TEST_JAVA = 'public class T {\n  @Xray(testCase = "QA-5")\n  void t() {}\n}\n'


class TestJavaDiscovery:
    def test_finds_exactly_the_marked_methods_with_their_keys(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "login/LoginTest.java",
            """
            package acme.login;
            public class LoginTest {
                @Xray(testCase = "QA-1")
                @Test
                public void logsIn() { driver.get("/login"); }

                @Test
                public void notATest() { /* no marker */ }

                @Xray(testCase = "QA-2")
                public void createsUser() { helper.doThing(); }
            }
            """,
        )
        result = discover_tests("QA", selenium_root=tmp_path)

        assert {t.xray_key for t in result.tests} == {"QA-1", "QA-2"}
        assert {t.symbol for t in result.tests} == {"LoginTest.logsIn", "LoginTest.createsUser"}
        assert result.markers_seen == 2
        assert result.parity_gap == 0
        assert all(t.language == "java" and t.source == "selenium-import" for t in result.tests)

    def test_captured_source_is_the_method_body(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "T.java",
            'public class T {\n  @Xray(testCase = "QA-9")\n'
            '  public void go() { click("save-btn"); }\n}\n',
        )
        result = discover_tests("QA", selenium_root=tmp_path)
        (test,) = result.tests
        assert 'click("save-btn")' in test.code
        assert "class T" not in test.code  # the method, not the whole class

    def test_marker_in_comment_or_string_does_not_fake_a_test(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "C.java",
            """
            public class C {
                // @Xray(testCase = "QA-COMMENT") -- commented out, not a test
                @Xray(testCase = "QA-REAL")
                public void real() {
                    String note = "@Xray(testCase = \\"QA-STRING\\")";
                }
            }
            """,
        )
        result = discover_tests("QA", selenium_root=tmp_path)

        assert [t.xray_key for t in result.tests] == ["QA-REAL"]
        # The commented + stringified markers must not inflate the parity numerator.
        assert result.markers_seen == 1
        assert result.parity_gap == 0

    def test_configurable_marker_regex(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "T.java",
            'public class T {\n  @TestCaseId("ACME-7")\n  public void t() {}\n}\n',
        )
        result = discover_tests(
            "ACME", selenium_root=tmp_path, marker_regex=r'@TestCaseId\("([^"]+)"\)'
        )
        assert [t.xray_key for t in result.tests] == ["ACME-7"]

    def test_marker_regex_must_capture_a_group(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="group"):
            discover_tests("QA", selenium_root=tmp_path, marker_regex=r"@Xray")

    def test_invalid_marker_regex_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="valid regex"):
            discover_tests("QA", selenium_root=tmp_path, marker_regex=r"@Xray(([")


class TestTextBlockRobustness:
    def test_text_block_does_not_swallow_a_later_test(self, tmp_path: Path) -> None:
        # A quote/brace-heavy Java text block must be treated as one literal — the
        # regression (v1) desynchronized the brace scanner and dropped whole classes.
        _write(
            tmp_path,
            "DbTest.java",
            '''
            public class DbTest {
                @Xray(testCase = "QA-DB")
                public void query() {
                    String sql = """
                        SELECT * FROM t WHERE name = 'a}{b' AND x = "y";
                        """;
                    run(sql);
                }

                @Xray(testCase = "QA-AFTER")
                public void after() { assertTrue(true); }
            }
            ''',
        )
        result = discover_tests("QA", selenium_root=tmp_path)

        assert {t.xray_key for t in result.tests} == {"QA-DB", "QA-AFTER"}
        assert result.parity_gap == 0
        assert result.fallback_files == []


class TestVisibilityOfGaps:
    def test_whole_file_fallback_when_marker_outside_any_parsed_class(
        self, tmp_path: Path
    ) -> None:
        # An unterminated/parse-hostile class body leaves the marker outside every
        # parsed class span → the file is re-scanned whole and named, never dropped.
        _write(
            tmp_path,
            "Weird.java",
            'class Holder { int broken = 1 // no semicolon, brace confusion\n'
            '  void x() { if (true) { } }\n'
            '  @Xray(testCase = "QA-EDGE")\n'
            '  public void edge() { doThing(); }\n',
        )
        result = discover_tests("QA", selenium_root=tmp_path)

        assert "QA-EDGE" in {t.xray_key for t in result.tests}
        assert result.fallback_files == ["Weird.java"]

    def test_parity_gap_when_a_marker_maps_to_no_method(self, tmp_path: Path) -> None:
        # A marker with no method after it (here: trailing the last method) is
        # counted but never discovered — the gap is surfaced, not silent.
        _write(
            tmp_path,
            "G.java",
            'public class G {\n  @Xray(testCase = "QA-OK")\n  public void ok() {}\n\n'
            '  @Xray(testCase = "QA-ORPHAN")\n}\n',
        )
        result = discover_tests("QA", selenium_root=tmp_path)

        assert [t.xray_key for t in result.tests] == ["QA-OK"]
        assert result.markers_seen == 2
        assert result.parity_gap == 1


class TestStableIds:
    def test_ids_are_pre_model_and_match_make_record_id(self, tmp_path: Path) -> None:
        _write(tmp_path, "T.java", _ONE_TEST_JAVA)
        (test,) = discover_tests("QA", selenium_root=tmp_path).tests
        assert test.record_id == make_record_id("QA", "selenium-import", "QA-5")

    def test_discovery_is_idempotent(self, tmp_path: Path) -> None:
        _write(tmp_path, "T.java", _ONE_TEST_JAVA)
        first = discover_tests("QA", selenium_root=tmp_path)
        second = discover_tests("QA", selenium_root=tmp_path)
        assert [t.record_id for t in first.tests] == [t.record_id for t in second.tests]

    def test_unlinked_java_test_ids_off_its_ref(self, tmp_path: Path) -> None:
        # A custom marker with an empty-ish key still yields a stable id off the ref.
        _write(tmp_path, "T.java", 'public class T {\n  @Mark("")\n  void t() {}\n}\n')
        result = discover_tests("QA", selenium_root=tmp_path, marker_regex=r'@Mark\("([^"]*)"\)')
        # empty key ([^"]*) → the ref (path#symbol) drives the id
        (test,) = result.tests
        assert test.record_id == make_record_id("QA", "selenium-import", test.ref)


class TestPlaywrightDiscovery:
    def test_one_record_per_spec_with_comment_marker(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "note.spec.ts",
            "// @Xray(testCase = \"NOTE-3\")\n"
            "import { test } from '@playwright/test';\n"
            "test('adds a note', async ({ page }) => { await page.goto('/'); });\n",
        )
        result = discover_tests("NOTE", playwright_dir=tmp_path)

        (test,) = result.tests
        assert test.language == "ts"
        assert test.source == "playwright-import"
        assert test.xray_key == "NOTE-3"
        assert test.record_id == make_record_id("NOTE", "playwright-import", "NOTE-3")

    def test_spec_without_marker_keys_off_its_path(self, tmp_path: Path) -> None:
        _write(tmp_path, "sub/plain.spec.ts", "test('x', () => {});\n")
        (test,) = discover_tests("NOTE", playwright_dir=tmp_path).tests
        assert test.xray_key == ""
        assert test.record_id == make_record_id("NOTE", "playwright-import", "sub/plain.spec.ts")


class TestSummaryAndSkeleton:
    def test_summary_reports_parity_and_gap(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "G.java",
            'public class G {\n  @Xray(testCase = "QA-OK")\n  public void ok() {}\n\n'
            '  @Xray(testCase = "QA-ORPHAN")\n}\n',
        )
        summary = render_discovery_summary(discover_tests("QA", selenium_root=tmp_path))
        assert "markers seen (Java): 2" in summary
        assert "DISCOVERY GAP" in summary

    def test_skeleton_lists_suites_and_counts(self, tmp_path: Path) -> None:
        _write(tmp_path, "login/A.java", 'class A {\n  @Xray(testCase = "QA-1")\n  void a(){}\n}\n')
        _write(tmp_path, "notes/B.java", 'class B {\n  @Xray(testCase = "QA-2")\n  void b(){}\n}\n')
        result = discover_tests("QA", selenium_root=tmp_path)
        assert "login: 1 test(s)" in result.skeleton
        assert "notes: 1 test(s)" in result.skeleton
