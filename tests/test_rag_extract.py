"""Static-extraction tests, driven by the committed demo legacy suite.

The fixture at packages/demo-notes-app/legacy-suite mirrors the real repo's
shape (per the 2026-07-07 real-corpus review): suite main/test packages with a
`pages` directory, a shared core package with a BasePage the pages extend,
selector values held in String CONSTANTS wrapped by By fields, page objects
created in @BeforeEach setUp fields, fluent chains, static imports, comments
containing apostrophes and braces, the @Xray(testCase = "...") annotation, and
one call into a class that is NOT in the tree (ReportingClient) — the
unresolved case.
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


class TestRealSuiteShapes:
    """The four real-corpus failure modes (2026-07-07 laptop review), covered."""

    def test_const_string_selectors_resolve_to_literals(self, java_bundles) -> None:
        """By.id(EMAIL_ID) with `String EMAIL_ID = "login-email"` → the literal;
        const NAMES must never leak into selector values."""
        values = {loc.value for loc in java_bundles["NOTE-4"].locators}
        assert 'By.id("login-email")' in values
        assert 'By.id("login-error")' in values
        assert not any("EMAIL_ID" in v or "_ID" in v for v in values)

    def test_const_resolved_locator_keeps_field_attribution(self, java_bundles) -> None:
        by_value = {loc.value: loc for loc in java_bundles["NOTE-4"].locators}
        assert by_value['By.id("login-email")'].declared_in == "LoginPage.EMAIL"

    def test_setup_initialized_field_receiver_resolves(self, java_bundles) -> None:
        """loginPage is a class field assigned in @BeforeEach setUp — its calls
        must still resolve into LoginPage (the old extractor dropped them silently)."""
        refs = " ".join(java_bundles["NOTE-4"].helper_refs)
        assert "LoginPage.open" in refs
        assert "LoginPage.loginAs" in refs

    def test_inherited_base_page_helpers_resolve(self, java_bundles) -> None:
        """click/type live on BasePage; pages call them bare via `extends`."""
        refs = " ".join(java_bundles["NOTE-2"].helper_refs)
        assert "BasePage.type" in refs
        assert "BasePage.click" in refs

    def test_fluent_chain_is_followed(self, java_bundles) -> None:
        """new LoginPage(driver).open(...).loginAs(...) → NotesPage: every hop
        resolves and the declared type wins for the chained result variable."""
        refs = " ".join(java_bundles["NOTE-2"].helper_refs)
        assert "LoginPage.open" in refs
        assert "LoginPage.loginAs" in refs
        assert "NotesPage.createNote" in refs

    def test_static_import_bare_call_resolves(self, java_bundles) -> None:
        refs = " ".join(java_bundles["NOTE-4"].helper_refs)
        assert "Waits.visible" in refs

    def test_this_call_resolves_same_class(self, java_bundles) -> None:
        refs = " ".join(java_bundles["NOTE-4"].helper_refs)
        assert "LoginTest.verifyStillOnLogin" in refs

    def test_comment_braces_and_apostrophes_do_not_truncate(self, java_bundles) -> None:
        """loginAs carries an apostrophe comment, errorText a brace comment —
        the old brace matcher mis-sliced both method bodies."""
        helpers = "\n".join(java_bundles["NOTE-4"].helper_snippets)
        assert "click(SUBMIT)" in helpers  # line AFTER the apostrophe comment
        assert "return new NotesPage(driver)" in helpers
        assert "findElement(ERROR)" in helpers  # line AFTER the brace comment

    def test_external_static_imports_stay_silent(self, java_bundles) -> None:
        """JUnit assertions are static imports from OUTSIDE the repo — framework,
        not an unresolved helper."""
        for bundle in java_bundles.values():
            assert not any("assertTrue" in r or "assertEquals" in r for r in bundle.helper_refs)

    def test_own_signature_is_not_a_helper(self, java_bundles) -> None:
        refs = " ".join(java_bundles["NOTE-4"].helper_refs)
        assert "loginFailsWithWrongPassword" not in refs


class TestVisibilityOfGaps:
    """Whatever cannot be resolved must be FLAGGED — never silent, never guessed."""

    def test_untyped_receiver_is_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "T.java").write_text(
            """
package t;
public class T {
    @Xray(testCase = "QA-2")
    public void t() {
        mystery.doThing();
    }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        assert "unresolved:mystery.doThing (untyped receiver)" in bundle.helper_refs

    def test_dynamic_locator_resolves_to_template_or_stays_flagged(
        self, tmp_path: Path
    ) -> None:
        """Static skeletons become TEMPLATE locators ({name}/%s kept); anything
        beyond that (method-call args, unknown UPPER consts) stays unresolved —
        flagged, never guessed."""
        (tmp_path / "RowsPage.java").write_text(
            """
package t;
public class RowsPage {
    private static final String ROW_TPL = "//table//tr[";
    private static final String CELL_FMT = "//tr[%s]/td[%s]";
    public void openRow(int index) {
        driver.findElement(By.xpath(ROW_TPL + index + "]")).click();
    }
    public void openCell(int row, int col) {
        driver.findElement(By.xpath(String.format(CELL_FMT, row, col))).click();
    }
    public void openComputed(int index) {
        driver.findElement(By.xpath(buildRow(index))).click();
    }
    public void openLost() {
        driver.findElement(By.id(UNKNOWN_CONST)).click();
    }
}
"""
        )
        (tmp_path / "T.java").write_text(
            """
package t;
public class T {
    @Xray(testCase = "QA-3")
    public void t() {
        RowsPage rows = new RowsPage();
        rows.openRow(2);
        rows.openCell(1, 2);
        rows.openComputed(3);
        rows.openLost();
    }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        by_value = {loc.value: loc for loc in bundle.locators}
        concat = by_value.get('By.xpath("//table//tr[{index}]")')
        assert concat is not None and concat.template
        formatted = by_value.get('By.xpath("//tr[%s]/td[%s]")')
        assert formatted is not None and formatted.template
        assert any("By.xpath(buildRow(index))" in u for u in bundle.unresolved_locators)
        assert any("By.id(UNKNOWN_CONST)" in u for u in bundle.unresolved_locators)
        assert not any("UNKNOWN_CONST" in loc.value for loc in bundle.locators)

    def test_qualified_and_imported_constants_resolve(self, tmp_path: Path) -> None:
        (tmp_path / "Ids.java").write_text(
            'package t;\npublic class Ids { public static final String SAVE = "save-btn"; }\n'
        )
        (tmp_path / "SavePage.java").write_text(
            """
package t;
import static t.Ids.SAVE;
public class SavePage {
    private static final String LIST_CSS = ".notes-list";
    public static final By SAVE_BTN = By.id(Ids.SAVE);
    public static final By SAVE_TOO = By.id(SAVE);
    public static final By TITLES = By.cssSelector(LIST_CSS + " h3");
}
"""
        )
        (tmp_path / "T.java").write_text(
            """
package t;
public class T {
    @Xray(testCase = "QA-4")
    public void t() {
        SavePage page = new SavePage();
    }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        values = {loc.value for loc in bundle.locators}
        assert 'By.id("save-btn")' in values  # Ids.SAVE and imported SAVE
        assert 'By.cssSelector(".notes-list h3")' in values  # concat folding
        assert bundle.unresolved_locators == []

    def test_findby_annotations_extract(self, tmp_path: Path) -> None:
        (tmp_path / "ProfilePage.java").write_text(
            """
package t;
public class ProfilePage {
    private static final String AVATAR_CSS = ".profile img.avatar";
    @FindBy(id = "profile-name")
    private WebElement name;
    @FindBy(how = How.CSS, using = AVATAR_CSS)
    private WebElement avatar;
    @FindBy(xpath = "//button[contains(text(),'Save (draft)')]")
    private WebElement save;
}
"""
        )
        (tmp_path / "T.java").write_text(
            """
package t;
public class T {
    @Xray(testCase = "QA-5")
    public void t() {
        ProfilePage page = new ProfilePage();
    }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        locators = {(loc.kind, loc.value) for loc in bundle.locators}
        assert ("testid", 'By.id("profile-name")') in locators
        assert ("css", 'By.cssSelector(".profile img.avatar")') in locators
        assert ("xpath", 'By.xpath("//button[contains(text(),\'Save (draft)\')]")') in locators

    def test_budget_overflow_skips_snippets_but_keeps_locators(self, tmp_path: Path) -> None:
        """The old extractor RETURNED on first overflow, losing every later
        helper AND its locators. Now only snippet text is capped."""
        (tmp_path / "APage.java").write_text(
            'package t;\npublic class APage {\n    public static final By A = By.id("a-field");\n'
            "    public void act() { int x = 1; }\n}\n"
        )
        (tmp_path / "BPage.java").write_text(
            'package t;\npublic class BPage {\n    public static final By B = By.id("b-field");\n'
            "    public void act() { int x = 1; }\n}\n"
        )
        (tmp_path / "T.java").write_text(
            """
package t;
public class T {
    @Xray(testCase = "QA-6")
    public void t() {
        APage a = new APage();
        a.act();
        BPage b = new BPage();
        b.act();
    }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path, helper_char_cap=10)
        values = {loc.value for loc in bundle.locators}
        assert 'By.id("a-field")' in values
        assert 'By.id("b-field")' in values  # extraction survived the budget
        assert any(r.startswith("truncated:") for r in bundle.helper_refs)
        assert bundle.helper_snippets == []  # nothing fit the 10-char cap


class TestExecutionPathTraversal:
    """The 2026-07-08 real-corpus gaps: the extractor must FOLLOW the test's
    execution path — @Before lifecycle, shared flow classes, base wrappers,
    locator-repository classes — wherever it leads (unbounded by default)."""

    def test_before_lifecycle_login_is_part_of_the_bundle(self, tmp_path: Path) -> None:
        """Login lives in @BeforeMethod on a base test class — the dominant
        real-suite shape. Its body, locators and URL must land in the bundle."""
        (tmp_path / "LoginPage.java").write_text(
            """
package t;
public class LoginPage {
    public static final By EMAIL = By.id("login-email");
    public static final By PASSWORD = By.id("login-password");
    public static final By SUBMIT = By.id("login-submit");
    public void loginAs(String email, String password) {
        driver.findElement(EMAIL).sendKeys(email);
        driver.findElement(PASSWORD).sendKeys(password);
        driver.findElement(SUBMIT).click();
    }
}
"""
        )
        (tmp_path / "BaseTest.java").write_text(
            """
package t;
import org.openqa.selenium.WebDriver;
public class BaseTest {
    protected WebDriver driver;
    protected LoginPage loginPage;
    @BeforeMethod
    public void setUp() {
        driver.get("http://localhost:3000/login");
        loginPage = new LoginPage(driver);
        loginPage.loginAs("demo@demo.test", "pw");
    }
}
"""
        )
        (tmp_path / "NoteTest.java").write_text(
            """
package t;
public class NoteTest extends BaseTest {
    @Xray(testCase = "QA-10")
    @Test
    public void createsNote() {
        int x = 1;
    }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        assert any("BaseTest.setUp" in ref for ref in bundle.lifecycle_refs)
        assert any(
            s.startswith("// setup (@Before): BaseTest.setUp") for s in bundle.helper_snippets
        )
        assert any("LoginPage.loginAs" in s for s in bundle.helper_snippets)
        values = {loc.value for loc in bundle.locators}
        assert {
            'By.id("login-email")',
            'By.id("login-password")',
            'By.id("login-submit")',
        } <= values
        assert "http://localhost:3000/login" in bundle.urls

    def test_default_traversal_reaches_the_whole_chain_and_depth_still_bounds(
        self, tmp_path: Path
    ) -> None:
        """test → flow → page → base wrapper (the click-helper-used-everywhere
        shape): default traversal reaches the deepest locator; --helper-depth 2
        still bounds the walk."""
        (tmp_path / "FlowA.java").write_text(
            "package t;\npublic class FlowA {\n"
            "    public void start() { new FlowB().mid(); }\n}\n"
        )
        (tmp_path / "FlowB.java").write_text(
            "package t;\npublic class FlowB {\n"
            "    public void mid() { new DeepPage().submit(); }\n}\n"
        )
        (tmp_path / "DeepPage.java").write_text(
            """
package t;
public class DeepPage extends BaseWidget {
    public static final By DEEP = By.id("deep-btn");
    public void submit() { click(DEEP); }
}
"""
        )
        (tmp_path / "BaseWidget.java").write_text(
            "package t;\npublic class BaseWidget {\n"
            "    protected void click(By target) { driver.findElement(target).click(); }\n}\n"
        )
        (tmp_path / "T.java").write_text(
            """
package t;
public class T {
    @Xray(testCase = "QA-11")
    public void t() { new FlowA().start(); }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        assert 'By.id("deep-btn")' in {loc.value for loc in bundle.locators}
        assert any("BaseWidget.click" in s for s in bundle.helper_snippets)  # the wrapper body
        [bounded] = extract_java_tests(tmp_path, helper_depth=2)
        assert 'By.id("deep-btn")' not in {loc.value for loc in bounded.locators}

    def test_field_access_only_locator_repository_is_visited(self, tmp_path: Path) -> None:
        """`Locators.SAVE` is a field access, never a call — the class must be
        visited anyway, or repository-style suites lose every locator."""
        (tmp_path / "Locators.java").write_text(
            'package t;\npublic class Locators {\n'
            '    public static final By SAVE = By.id("save-btn");\n}\n'
        )
        (tmp_path / "T.java").write_text(
            """
package t;
public class T {
    @Xray(testCase = "QA-12")
    public void t() {
        driver.findElement(Locators.SAVE).click();
    }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        assert 'By.id("save-btn")' in {loc.value for loc in bundle.locators}

    def test_simple_name_collisions_resolve_via_imports(self, tmp_path: Path) -> None:
        """Two suites, both with a LoginPage: the caller's import decides —
        never last-indexed-wins. Without an import, the call is flagged
        ambiguous instead of silently resolving to the wrong suite."""
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        (tmp_path / "a" / "LoginPage.java").write_text(
            "package a.pages;\npublic class LoginPage {\n"
            '    public static final By EMAIL = By.id("a-email");\n'
            "    public void go() { int x = 1; }\n}\n"
        )
        (tmp_path / "b" / "LoginPage.java").write_text(
            "package b.pages;\npublic class LoginPage {\n"
            '    public static final By EMAIL = By.id("b-email");\n'
            "    public void go() { int x = 1; }\n}\n"
        )
        (tmp_path / "T.java").write_text(
            """
package a.tests;
import a.pages.LoginPage;
public class T {
    @Xray(testCase = "QA-13")
    public void t() { new LoginPage().go(); }
}
"""
        )
        (tmp_path / "T2.java").write_text(
            """
package c.tests;
public class T2 {
    @Xray(testCase = "QA-14")
    public void t2() { LoginPage.open(); }
}
"""
        )
        bundles = {b.xray_key: b for b in extract_java_tests(tmp_path)}
        values = {loc.value for loc in bundles["QA-13"].locators}
        assert 'By.id("a-email")' in values
        assert 'By.id("b-email")' not in values
        assert any(
            "ambiguous: 2 classes named LoginPage" in ref
            for ref in bundles["QA-14"].helper_refs
        )

    def test_calls_into_imported_external_libraries_stay_silent(self, tmp_path: Path) -> None:
        """RestAssured/JDBC-style calls are known-external (the import points
        outside the tree) — they must not pollute the unresolved stats."""
        (tmp_path / "T.java").write_text(
            """
package t;
import io.restassured.RestAssured;
public class T {
    @Xray(testCase = "QA-15")
    public void t() {
        RestAssured.given();
    }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        assert not any("RestAssured" in ref for ref in bundle.helper_refs)
        assert bundle.unresolved_count == 0

    def test_generic_and_annotated_one_liner_signatures_parse(self, tmp_path: Path) -> None:
        """`public <T> T pick(...)` and `@Override public void open() {` used to
        be invisible to the signature regex — every call into them a false
        `unresolved:`."""
        (tmp_path / "Util.java").write_text(
            "package t;\npublic class Util {\n"
            "    public <T> T pick(T value) { return value; }\n}\n"
        )
        (tmp_path / "Nav.java").write_text(
            "package t;\npublic class Nav {\n"
            '    @Override public void open() { driver.get("http://localhost/open"); }\n}\n'
        )
        (tmp_path / "T.java").write_text(
            """
package t;
public class T {
    @Xray(testCase = "QA-16")
    public void t() {
        Util u = new Util();
        u.pick("x");
        new Nav().open();
    }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        refs = " | ".join(bundle.helper_refs)
        assert "Util.pick" in refs and "unresolved:Util.pick" not in refs
        assert "Nav.open" in refs and "unresolved:Nav.open" not in refs
        assert "http://localhost/open" in bundle.urls

    def test_multiple_top_level_classes_in_one_file_are_indexed(self, tmp_path: Path) -> None:
        (tmp_path / "Pages.java").write_text(
            """
package t;
class HeaderPage {
    public static final By LOGO = By.id("logo");
    public void go() { int x = 1; }
}
class FooterPage {
    public static final By LINK = By.id("footer-link");
    public void go() { int x = 1; }
}
"""
        )
        (tmp_path / "T.java").write_text(
            """
package t;
public class T {
    @Xray(testCase = "QA-17")
    public void t() { new FooterPage().go(); }
}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        values = {loc.value for loc in bundle.locators}
        assert 'By.id("footer-link")' in values  # the SECOND class in the file
        assert 'By.id("logo")' not in values  # unreferenced sibling stays out

    def test_unresolved_count_is_not_display_capped(self, tmp_path: Path) -> None:
        calls = "\n        ".join(f"u{i}.act();" for i in range(35))
        (tmp_path / "T.java").write_text(
            f"""
package t;
public class T {{
    @Xray(testCase = "QA-18")
    public void t() {{
        {calls}
    }}
}}
"""
        )
        [bundle] = extract_java_tests(tmp_path)
        assert bundle.unresolved_count == 35
        shown = [r for r in bundle.helper_refs if r.startswith("unresolved:")]
        assert len(shown) == 31  # 30 + the "… and N more" line
        assert any("and 5 more" in r for r in shown)


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
