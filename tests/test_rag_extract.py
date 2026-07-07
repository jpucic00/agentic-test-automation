"""Static-extraction tests, driven by the committed demo legacy suite.

The fixture at packages/demo-notes-app/legacy-suite mirrors the real repo's
shape: suite main/test packages, a shared core package, page objects holding
the By.* locators, the @Xray(testCase = "...") annotation, and one call into a
class that is NOT in the tree (ReportingClient) — the unresolved case.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_test_gen.config import PROJECT_ROOT
from ai_test_gen.rag.extract import (
    JavaIndex,
    extract_java_tests,
    extract_playwright_specs,
)

LEGACY = PROJECT_ROOT / "packages" / "demo-notes-app" / "legacy-suite"


@pytest.fixture(scope="module")
def java_bundles():
    return {b.xray_key: b for b in extract_java_tests(LEGACY, JavaIndex.build(LEGACY))}


class TestJavaDiscovery:
    def test_finds_exactly_the_annotated_tests(self, java_bundles) -> None:
        assert set(java_bundles) == {"NOTE-2", "NOTE-4"}
        assert java_bundles["NOTE-4"].test_name == "loginFailsWithWrongPassword"
        assert java_bundles["NOTE-2"].test_name == "seededUserCreatesANote"

    def test_xray_key_comes_from_the_annotation(self, java_bundles) -> None:
        bundle = java_bundles["NOTE-4"]
        assert bundle.xray_key == "NOTE-4"
        assert bundle.language == "java"


class TestHelperResolution:
    def test_page_object_locators_land_in_the_record(self, java_bundles) -> None:
        """The By.* fields live in main-package page objects, not the test —
        resolution must surface them, attributed to where they are declared."""
        locators = {(loc.kind, loc.value): loc for loc in java_bundles["NOTE-4"].locators}
        assert ("testid", 'By.id("login-email")') in locators
        assert ("testid", 'By.id("login-submit")') in locators
        assert locators[("testid", 'By.id("login-email")')].declared_in == "LoginPage.EMAIL"

    def test_kind_classification_covers_the_ladder(self, java_bundles) -> None:
        kinds = {(loc.kind, loc.value) for loc in java_bundles["NOTE-2"].locators}
        new_note_xpath = (
            'By.xpath("//div[contains(@class,\'btn\') and normalize-space()=\'New note\']")'
        )
        assert ("xpath", new_note_xpath) in kinds
        assert ("css", 'By.name("title")') in kinds
        assert ("css", 'By.cssSelector(".notes-list .note-item h3")') in kinds

    def test_depth_two_reaches_core_helpers(self, java_bundles) -> None:
        """CreateNoteTest → NotesPage.createNote → Waits.visible (shared core)."""
        refs = " ".join(java_bundles["NOTE-2"].helper_refs)
        assert "NotesPage.createNote" in refs
        assert "Waits.visible" in refs

    def test_unresolvable_call_is_flagged_never_silent(self, java_bundles) -> None:
        assert "unresolved:ReportingClient.record" in java_bundles["NOTE-4"].helper_refs

    def test_routes_from_helper_navigation(self, java_bundles) -> None:
        # LoginPage.open does driver.get(baseUrl + "/login")
        assert "/login" in java_bundles["NOTE-4"].urls

    def test_source_code_bundle_carries_test_and_helpers(self, java_bundles) -> None:
        source = java_bundles["NOTE-2"].source_code
        assert "seededUserCreatesANote" in source
        assert "createNote" in source  # helper body included


class TestKindClassificationUnit:
    def test_every_by_variant_maps_to_a_ladder_kind(self, tmp_path: Path) -> None:
        (tmp_path / "KindsTest.java").write_text(
            """
package t;
import com.demo.core.xray.Xray;
public class KindsTest {
    @Xray(testCase = "QA-1")
    public void kinds() {
        driver.findElement(By.id("a"));
        driver.findElement(By.cssSelector(".b"));
        driver.findElement(By.xpath("//c"));
        driver.findElement(By.name("d"));
        driver.findElement(By.className("e"));
        driver.findElement(By.linkText("f"));
    }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        kinds = {loc.value: loc.kind for loc in bundle.locators}
        assert kinds['By.id("a")'] == "testid"
        assert kinds['By.cssSelector(".b")'] == "css"
        assert kinds['By.xpath("//c")'] == "xpath"
        assert kinds['By.name("d")'] == "css"
        assert kinds['By.className("e")'] == "css"
        assert kinds['By.linkText("f")'] == "text"


class TestPlaywrightExtraction:
    def test_spec_bundle_with_comment_annotation(self) -> None:
        [bundle] = extract_playwright_specs(LEGACY / "playwright")
        assert bundle.language == "ts"
        assert bundle.xray_key == "NOTE-3"  # from the // @Xray(...) comment
        kinds = {loc.kind for loc in bundle.locators}
        assert {"testid", "text", "css"} <= kinds
        values = " ".join(loc.value for loc in bundle.locators)
        assert "getByTestId('login-email')" in values.replace('"', "'")
        assert "http://localhost:3000/login" in bundle.urls
        assert bundle.code.strip()
