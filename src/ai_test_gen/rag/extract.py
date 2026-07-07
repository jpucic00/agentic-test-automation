"""Static extraction for KB seeding: tests, locators, and helper resolution.

Everything here is deterministic code — the Distiller agent never searches the
repo (RETRIEVAL_MEMORY_PLAN.md §5). Discovery: any Java method annotated
``@Xray(testCase = "KEY")`` is a test; the rest of the walked tree becomes the
helper pool. Per test, **bounded static resolution** (default depth 2,
size-capped) follows the test's calls into helpers found in the tree — that is
where page objects keep their ``By.*`` locators — and anything unresolvable is
recorded as ``unresolved:...`` so extraction completeness is visible, never
silent. Heuristic name-based parsing; no Java compiler.

Extracted locators are ground truth the Distiller must not contradict; their
``kind`` maps onto the selector resilience ladder used everywhere else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# The real repo's annotation form (user-confirmed): @Xray(testCase = "QA-123").
# Also matches the same marker inside a comment (hand-written Playwright specs).
XRAY_RE = re.compile(r'@Xray\s*\(\s*testCase\s*=\s*"([^"]+)"\s*\)')

# --- Java --------------------------------------------------------------------

# By.<method>("...") → resilience-ladder kind. `id` maps to testid (ids are the
# pipeline's testIdAttribute); name/class/tag are css-expressible; link text is text.
_BY_KIND = {
    "id": "testid",
    "cssSelector": "css",
    "xpath": "xpath",
    "name": "css",
    "className": "css",
    "tagName": "css",
    "linkText": "text",
    "partialLinkText": "text",
}
BY_RE = re.compile(
    r'By\.(id|cssSelector|xpath|name|className|tagName|linkText|partialLinkText)'
    r'\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)'
)
_BY_FIELD_RE = re.compile(r'By\s+(\w+)\s*=\s*(By\.\w+\s*\(\s*"(?:[^"\\]|\\.)*"\s*\))')
_METHOD_SIG_RE = re.compile(
    r'^[ \t]*(?:(?:public|protected|private|static|final|synchronized)\s+)*'
    r'[\w<>\[\], ?.]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w, .]+\s*)?\{',
    re.M,
)
_CLASS_RE = re.compile(r'\b(?:class|@interface|interface|enum)\s+(\w+)')
_PACKAGE_RE = re.compile(r'^\s*package\s+([\w.]+)\s*;', re.M)
_NEW_VAR_RE = re.compile(r'\b(\w+)\s+(\w+)\s*=\s*new\s+(\w+)\s*\(')
_VAR_CALL_RE = re.compile(r'\b([a-z]\w*)\.(\w+)\s*\(')
_STATIC_CALL_RE = re.compile(r'\b([A-Z]\w*)\.(\w+)\s*\(')
_JAVA_URL_RE = re.compile(r'(?:driver\.get|navigate\(\)\.to)\s*\(\s*"([^"]+)"')
_BASEURL_ROUTE_RE = re.compile(r'baseUrl\s*\+\s*"([^"]+)"')

# Receivers that are platform/framework API, not repo helpers — never "unresolved".
_KNOWN_RECEIVERS = {
    "By", "String", "System", "Thread", "Duration", "Assertions", "Math",
    "Integer", "Long", "Double", "Boolean", "List", "Arrays", "Collections",
    "Objects", "ExpectedConditions", "WebDriverWait", "Keys", "TimeUnit",
}

DEFAULT_HELPER_DEPTH = 2
DEFAULT_HELPER_CHAR_CAP = 12_000
_TS_CODE_CAP = 8_000


@dataclass
class ExtractedLocator:
    kind: str
    value: str
    declared_in: str  # e.g. "LoginPage.EMAIL" or "CreateNoteTest#seededUserCreatesANote"


@dataclass
class TestBundle:
    """One test, fully assembled for a single Distiller call."""

    __test__ = False  # "Test"-prefixed dataclass, not a pytest class

    ref: str  # repo-relative "path#method" — the stable id ref when unlinked
    test_name: str
    class_name: str
    language: str  # "java" | "ts"
    xray_key: str | None
    code: str
    helper_snippets: list[str] = field(default_factory=list)
    helper_refs: list[str] = field(default_factory=list)
    locators: list[ExtractedLocator] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    @property
    def source_code(self) -> str:
        """Test + helper bundle, for the record's ``source_code`` payload."""
        return "\n\n".join([self.code, *self.helper_snippets])


@dataclass
class _JavaClass:
    name: str
    package: str
    path: Path
    rel: str
    text: str
    methods: dict[str, str]  # method name → source (signature + body)


class JavaIndex:
    """All classes/methods under a repo root, keyed for heuristic resolution."""

    def __init__(self, classes: dict[str, _JavaClass]) -> None:
        self.classes = classes

    @classmethod
    def build(cls, root: Path) -> JavaIndex:
        classes: dict[str, _JavaClass] = {}
        for path in sorted(root.rglob("*.java")):
            text = path.read_text(errors="replace")
            class_match = _CLASS_RE.search(text)
            if not class_match:
                continue
            name = class_match.group(1)
            package_match = _PACKAGE_RE.search(text)
            classes[name] = _JavaClass(
                name=name,
                package=package_match.group(1) if package_match else "",
                path=path,
                rel=str(path.relative_to(root)),
                text=text,
                methods=_split_methods(text),
            )
        return cls(classes)


def _split_methods(class_text: str) -> dict[str, str]:
    """Method name → full source (signature through matching close brace)."""
    methods: dict[str, str] = {}
    for match in _METHOD_SIG_RE.finditer(class_text):
        body_end = _matching_brace(class_text, match.end() - 1)
        if body_end is not None:
            methods[match.group(1)] = class_text[match.start() : body_end + 1]
    return methods


def _matching_brace(text: str, open_index: int) -> int | None:
    """Index of the ``}`` matching ``{`` at ``open_index`` (string-literal aware)."""
    depth = 0
    in_string: str | None = None
    i = open_index
    while i < len(text):
        char = text[i]
        if in_string:
            if char == "\\":
                i += 2
                continue
            if char == in_string:
                in_string = None
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


def extract_java_tests(root: Path, index: JavaIndex | None = None) -> list[TestBundle]:
    """One bundle per ``@Xray``-annotated method found under ``root``."""
    index = index or JavaIndex.build(root)
    bundles: list[TestBundle] = []
    for java_class in index.classes.values():
        for method_name, method_source in java_class.methods.items():
            # The annotation sits in the decoration zone directly above the
            # signature — after the previous member's closing brace — so a key
            # mentioned inside another method's body can never be picked up.
            sig_at = java_class.text.find(method_source)
            preamble = java_class.text[max(0, sig_at - 400) : sig_at]
            decoration_zone = preamble.rsplit("}", 1)[-1]
            annotation = XRAY_RE.search(decoration_zone)
            if not annotation:
                continue
            bundle = TestBundle(
                ref=f"{java_class.rel}#{method_name}",
                test_name=method_name,
                class_name=java_class.name,
                language="java",
                xray_key=annotation.group(1),
                code=f"// {java_class.rel}\n{method_source}",
            )
            _resolve_helpers(bundle, method_source, java_class, index)
            _collect_java_locators(bundle, java_class, method_source)
            _collect_java_urls(bundle, method_source)
            bundles.append(bundle)
    return bundles


def _resolve_helpers(
    bundle: TestBundle,
    test_source: str,
    test_class: _JavaClass,
    index: JavaIndex,
    depth: int = DEFAULT_HELPER_DEPTH,
    char_cap: int = DEFAULT_HELPER_CHAR_CAP,
) -> None:
    """Bounded resolution of the test's calls into repo helpers (plan §5.2)."""
    included: set[tuple[str, str]] = set()
    included_classes: set[str] = set()
    queue: list[tuple[str, str, int]] = _calls_of(test_source, test_class, index, depth)
    used = 0
    while queue:
        class_name, method_name, remaining = queue.pop(0)
        key = (class_name, method_name)
        if key in included:
            continue
        java_class = index.classes.get(class_name)
        if java_class is None or (
            method_name != "*" and method_name not in java_class.methods
        ):
            unresolved = f"unresolved:{class_name}.{method_name}"
            if unresolved not in bundle.helper_refs:
                bundle.helper_refs.append(unresolved)
            continue
        included.add(key)

        snippets: list[str] = []
        if class_name not in included_classes:
            included_classes.add(class_name)
            fields = _BY_FIELD_RE.findall(java_class.text)
            if fields:
                rendered = "\n".join(f"By {name} = {value};" for name, value in fields)
                snippets.append(f"// locator fields of {class_name} ({java_class.rel})\n{rendered}")
                for field_name, value in fields:
                    for by_method, selector in BY_RE.findall(value):
                        bundle.locators.append(
                            ExtractedLocator(
                                kind=_BY_KIND[by_method],
                                value=f'By.{by_method}("{selector}")',
                                declared_in=f"{class_name}.{field_name}",
                            )
                        )
        method_source = java_class.methods.get(method_name, "")
        if method_source:
            snippets.append(f"// {class_name}.{method_name} ({java_class.rel})\n{method_source}")
            bundle.helper_refs.append(f"{class_name}.{method_name} ({java_class.rel})")
            if remaining > 1:
                queue.extend(_calls_of(method_source, java_class, index, remaining - 1))
            _collect_java_urls(bundle, method_source)

        for snippet in snippets:
            if used + len(snippet) > char_cap:
                bundle.helper_refs.append("truncated:helper budget reached")
                return
            bundle.helper_snippets.append(snippet)
            used += len(snippet)


def _calls_of(
    source: str, owner: _JavaClass, index: JavaIndex, depth: int
) -> list[tuple[str, str, int]]:
    """(class, method, depth) targets this source calls, heuristically resolved."""
    var_types: dict[str, str] = {name: cls for _, name, cls in _NEW_VAR_RE.findall(source)}
    targets: list[tuple[str, str, int]] = []
    for var_name, method_name in _VAR_CALL_RE.findall(source):
        class_name = var_types.get(var_name)
        if class_name:
            targets.append((class_name, method_name, depth))
    for class_name, method_name in _STATIC_CALL_RE.findall(source):
        if class_name in _KNOWN_RECEIVERS:
            continue
        targets.append((class_name, method_name, depth))
    # constructors of resolved page objects → make sure their class is visited so
    # locator fields are extracted even if only the constructor is called
    for class_name in var_types.values():
        if class_name in index.classes and (class_name, "*") not in targets:
            targets.append((class_name, "*", depth))
    # bare same-class calls
    for match in re.finditer(r'(?<![.\w])([a-z]\w*)\s*\(', source):
        name = match.group(1)
        if name in owner.methods:
            targets.append((owner.name, name, depth))
    return targets


def _collect_java_locators(
    bundle: TestBundle, java_class: _JavaClass, method_source: str
) -> None:
    """Inline By.* locators written directly in the test method."""
    for by_method, selector in BY_RE.findall(method_source):
        bundle.locators.append(
            ExtractedLocator(
                kind=_BY_KIND[by_method],
                value=f'By.{by_method}("{selector}")',
                declared_in=f"{java_class.name}#{bundle.test_name}",
            )
        )


def _collect_java_urls(bundle: TestBundle, source: str) -> None:
    for url in _JAVA_URL_RE.findall(source):
        if url not in bundle.urls:
            bundle.urls.append(url)
    for route in _BASEURL_ROUTE_RE.findall(source):
        if route not in bundle.urls:
            bundle.urls.append(route)


# --- Playwright (hand-written specs) ------------------------------------------

_PW_LOCATOR_RES: list[tuple[str, re.Pattern[str]]] = [
    ("testid", re.compile(r"getByTestId\(\s*['\"][^'\"]+['\"]\s*\)")),
    ("role", re.compile(r"getByRole\(\s*['\"][^'\"]+['\"](?:\s*,\s*\{[^}]*\})?\s*\)")),
    ("label", re.compile(r"getByLabel\(\s*['\"][^'\"]+['\"](?:\s*,\s*\{[^}]*\})?\s*\)")),
    ("text", re.compile(r"getByText\(\s*['\"][^'\"]+['\"](?:\s*,\s*\{[^}]*\})?\s*\)")),
    ("xpath", re.compile(r"locator\(\s*['\"]xpath=[^'\"]+['\"][^)]*\)")),
    ("css", re.compile(r"locator\(\s*['\"](?!xpath=)[^'\"]+['\"][^)]*\)")),
]
_PW_URL_RE = re.compile(r"goto\(\s*['\"]([^'\"]+)['\"]")


def extract_playwright_specs(directory: Path, repo_root: Path | None = None) -> list[TestBundle]:
    """One bundle per ``*.spec.ts`` file (hand-written suite)."""
    base = repo_root or directory
    bundles: list[TestBundle] = []
    for path in sorted(directory.rglob("*.spec.ts")):
        text = path.read_text(errors="replace")
        rel = str(path.relative_to(base))
        annotation = XRAY_RE.search(text)  # the marker also matches inside a comment
        bundle = TestBundle(
            ref=rel,
            test_name=path.stem,
            class_name=path.stem,
            language="ts",
            xray_key=annotation.group(1) if annotation else None,
            code=f"// {rel}\n{text[:_TS_CODE_CAP]}",
        )
        for kind, pattern in _PW_LOCATOR_RES:
            for match in pattern.finditer(text):
                bundle.locators.append(
                    ExtractedLocator(kind=kind, value=match.group(0), declared_in=rel)
                )
        for url in _PW_URL_RE.findall(text):
            if url not in bundle.urls:
                bundle.urls.append(url)
        bundles.append(bundle)
    return bundles
