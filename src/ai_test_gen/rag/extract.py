"""Static extraction for KB seeding: tests, locators, and helper resolution.

Everything here is deterministic code — the Distiller agent never searches the
repo (RETRIEVAL_MEMORY_PLAN.md §5). Discovery: any Java method annotated
``@Xray(testCase = "KEY")`` is a test; the rest of the walked tree becomes the
helper pool. Per test, resolution FOLLOWS THE TEST'S EXECUTION PATH to the end
of the in-repo call graph (unbounded by default, ``--helper-depth`` bounds it;
dedup keeps it finite): ``@Before*`` lifecycle methods run first, every call is
chased into helpers, base-class wrappers, shared flow classes — that is where
page objects keep their locators and where "log in" turns into fill/fill/click.
Receivers are typed from local declarations, class fields (the
``page = new LoginPage(driver)``-in-setUp pattern), method parameters,
``extends`` chains, ``this``/``super`` and static imports; fluent chains are
followed through declared return types. Classes referenced only by FIELD access
(``Locators.SAVE``) are visited too, so locator-repository classes are
harvested. Class names resolve import/package-aware on fully-qualified names —
two suites may both have a ``LoginPage`` without shadowing each other; a call
into a class imported from OUTSIDE the tree is known-external and stays silent.
Anything unresolvable is recorded as ``unresolved:...`` so extraction
completeness is visible, never silent.

Locator values written as string constants (``By.id(LOGIN_ID)`` with
``String LOGIN_ID = "login"`` — the dominant real-suite shape) are resolved to
their literals; ``@FindBy`` PageFactory fields are read too. Dynamic values
whose SKELETON is static — ``String.format(ROW_XPATH, name)``, ``"…='" + id +
"'…"`` — resolve to placeholder TEMPLATES (``%s`` kept, variables as
``{name}``), marked as templates. A locator whose value cannot be resolved even
as a template lands in ``unresolved_locators`` — flagged, never guessed. All
structure scanning runs on a comment-masked copy of the source, so braces or
apostrophes inside comments can never mis-slice a method.

Extracted locators are ground truth the Distiller must not contradict; their
``kind`` maps onto the selector resilience ladder used everywhere else.
Heuristic name-based parsing; no Java compiler.
"""
from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

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
# @FindBy attribute (and How.<CONST>) → the equivalent By.<method> name.
_FINDBY_TO_BY = {
    "id": "id",
    "css": "cssSelector",
    "xpath": "xpath",
    "name": "name",
    "className": "className",
    "tagName": "tagName",
    "linkText": "linkText",
    "partialLinkText": "partialLinkText",
    # How.* enum spellings
    "ID": "id",
    "CSS": "cssSelector",
    "XPATH": "xpath",
    "NAME": "name",
    "CLASS_NAME": "className",
    "TAG_NAME": "tagName",
    "LINK_TEXT": "linkText",
    "PARTIAL_LINK_TEXT": "partialLinkText",
}

_BY_OPEN_RE = re.compile(
    r'By\.(id|cssSelector|xpath|name|className|tagName|linkText|partialLinkText)\s*\('
)
# A Java type: dotted name, optional one-level-scanned generics, optional arrays.
# Deliberately contains NO bare space/comma alternatives: comment-masking turns
# comments into long space runs, and a type class that can match raw spaces sent
# this regex into catastrophic backtracking on exactly those runs (minutes per
# generated file). Spaces/commas are legal only INSIDE the <...> group.
_TYPE_RE = r"[\w.$]+(?:<[^;{}()]*>)?(?:\[\])*"
# Signature prefix: same-line annotations (each atom is anchored by `@`, so the
# group cannot re-split whitespace runs), then modifiers as explicit keywords,
# then an optional method type-parameter list (anchored by `<`, one nesting
# level, bounded) — `@Override public void x()` and `public <T> T x()` both parse.
_METHOD_SIG_RE = re.compile(
    r'^[ \t]*(?:@\w+(?:\([^)]*\))?[ \t]+)*'
    r'(?:(?:public|protected|private|static|final|synchronized|abstract|default)\s+)*'
    r'(?:<[^<>;{}()]{0,200}(?:<[^<>;{}()]{0,100}>[^<>;{}()]{0,100})?>\s+)?'
    r'(' + _TYPE_RE + r')\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w, .]+\s*)?\{',
    re.M,
)
# Class declarations are scanned on string-blanked text (see _mask_strings), so
# the word `class` inside a string literal cannot fake a declaration.
_CLASS_DECL_RE = re.compile(r'\b(?:class|interface|enum)\s+(\w+)')
_EXTENDS_RE = re.compile(r'\bextends\s+([\w.]+)')
_PACKAGE_RE = re.compile(r'^\s*package\s+([\w.]+)\s*;', re.M)
_IMPORT_RE = re.compile(r'^\s*import\s+(?!static\b)([\w.]+(?:\.\*)?)\s*;', re.M)
_STATIC_IMPORT_RE = re.compile(r'^\s*import\s+static\s+([\w.]+)\.(\w+|\*)\s*;', re.M)
_STRING_CONST_RE = re.compile(r'\bString\s+(\w+)\s*=\s*("(?:[^"\\]|\\.)*")\s*;')
# Explicit modifier keywords instead of a lazy wildcard prefix — a lazy
# `[\w \t]*?` re-scans every split of a whitespace run (quadratic on the space
# blocks that comment/method blanking leaves behind).
_FIELD_DECL_RE = re.compile(
    r'^[ \t]*(?:(?:public|protected|private|static|final|transient|volatile)\s+)*'
    r'([A-Z]\w*)(?:<[^>\n]*>)?(?:\[\])?\s+(\w+)\s*[;=]',
    re.M,
)
_ASSIGN_NEW_RE = re.compile(r'\b(\w+)\s*=\s*new\s+([A-Z]\w*)\s*\(')
_NEW_RE = re.compile(r'\bnew\s+([A-Z]\w*)\s*\(')
_RECEIVER_CALL_RE = re.compile(r'\b(\w+)\s*\.\s*(\w+)\s*\(')
_BARE_CALL_RE = re.compile(r'(?<![.\w])([a-z_]\w*)\s*\(')
_CHAIN_NEXT_RE = re.compile(r'\s*\.\s*(\w+)\s*\(')
# `Locators.SAVE` — qualified FIELD access (no call parens): the only reference
# many locator-repository classes ever get. The trailing \b keeps \w+ from
# stopping early to defeat the lookahead.
_FIELD_ACCESS_RE = re.compile(r'\b([A-Z]\w*)\s*\.\s*(\w+)\b(?!\s*\()')
_FORMAT_OPEN_RE = re.compile(r'String\s*\.\s*format\s*\(')
_FORMAT_MARK_RE = re.compile(r'%[a-zA-Z]')
_PLACEHOLDER_RE = re.compile(r'\{\w+\}')
_BEFORE_ANNOTATION_RE = re.compile(r'@Before\w*\b')  # JUnit4/5 + TestNG @Before*
_FINDBY_OPEN_RE = re.compile(r'@FindBy\s*\(')
_FINDBY_FIELD_RE = re.compile(
    r'\s*(?:(?:public|private|protected|static|final)\s+)*'
    r'(?:List\s*<\s*WebElement\s*>|WebElement)\s+(\w+)'
)
_FINDBY_ATTR_RE = re.compile(r'(\w+)\s*=\s*((?:"(?:[^"\\]|\\.)*"|[^,])+)')
_JAVA_URL_RE = re.compile(r'(?:driver\.get|navigate\(\)\.to)\s*\(\s*"([^"]+)"')
_BASEURL_ROUTE_RE = re.compile(r'baseUrl\s*\+\s*"([^"]+)"')

# Receivers that are platform/framework API, not repo helpers — never "unresolved".
_KNOWN_RECEIVERS = {
    "By", "String", "System", "Thread", "Duration", "Assertions", "Assert", "Math",
    "Integer", "Long", "Double", "Boolean", "Character", "List", "Arrays",
    "Collections", "Objects", "Optional", "Stream", "Map", "Set", "Files", "Paths",
    "Pattern", "UUID", "ExpectedConditions", "WebDriverWait", "Keys", "TimeUnit",
    "PageFactory", "How", "LocalDate", "LocalDateTime", "ChronoUnit", "StringBuilder",
}
# Bare names that are control flow or constructor delegation, not helper calls.
_JAVA_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "throw", "new", "assert",
    "synchronized", "do", "else", "try", "finally", "case", "super", "this",
}
# Non-repo types (JDK/Selenium) show up as var/field/param types constantly; calls on
# them are framework actions, silently skipped. Repo types are "in the index" instead.
_ELEMENT_TYPES = {"WebElement", "By"}

# Traversal follows the whole in-repo call graph by default; dedup on
# (class, method) keeps it finite. An int bounds the hops (`--helper-depth`).
DEFAULT_HELPER_DEPTH: int | None = None
_UNBOUNDED_DEPTH = 1_000_000
DEFAULT_HELPER_CHAR_CAP = 48_000
_TS_CODE_CAP = 20_000
_MAX_UNRESOLVED_FLAGS = 30
_CHAIN_HOP_LIMIT = 8


@dataclass
class ExtractedLocator:
    kind: str
    value: str
    declared_in: str  # e.g. "LoginPage.EMAIL" or "CreateNoteTest#seededUserCreatesANote"
    # True when the value is a resolved SKELETON with runtime-filled parts:
    # `%s` from String.format, `{name}` from a concatenated variable/parameter.
    template: bool = False


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
    # Labels of @Before* lifecycle methods bundled in (they run before the test).
    lifecycle_refs: list[str] = field(default_factory=list)
    locators: list[ExtractedLocator] = field(default_factory=list)
    # Locators that exist in the code but whose VALUE could not be statically
    # resolved (fully dynamic — not even a template). Flagged so review sees
    # them; the Distiller is told to never guess these.
    unresolved_locators: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    # Unresolved-call count BEFORE the display cap — the summary must be truthful
    # even when a bundle overflows _MAX_UNRESOLVED_FLAGS.
    unresolved_count: int = 0

    @property
    def source_code(self) -> str:
        """Test + helper bundle, for the record's ``source_code`` payload."""
        return "\n\n".join([self.code, *self.helper_snippets])


# --- comment/string-aware scanning --------------------------------------------


def _text_block_end(text: str, start: int) -> int:
    """Index just past the closing ``\"\"\"`` of a Java text block whose opening
    delimiter ends at ``start`` (fail-safe: end of text).

    Text blocks (SQL/JSON in DB and API tests) desynchronize a pairwise quote
    scanner: content quotes flip its string state and a brace inside the block
    then corrupts every span downstream — on the real corpus that silently
    dropped whole classes. Every scanner below must treat ``\"\"\"…\"\"\"``
    as ONE literal.
    """
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
    """Length-preserving copy with comment bodies blanked to spaces.

    Newlines survive (so ``^``-anchored regexes still work) and string/char
    literals are copied verbatim. All structural scanning (method splitting,
    brace/paren matching, call and locator regexes) runs on the masked text, so
    a brace or apostrophe inside a comment can never corrupt extraction — and
    commented-out code never yields locators. Snippets are still sliced from
    the ORIGINAL text at the same offsets.
    """
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
    """Blank string/char literal CONTENTS (quotes kept) of comment-masked text.

    Used for declaration-level scans (package/imports/class decls) where a
    literal like ``"class Foo"`` must not fake a declaration. Java text blocks
    are blanked as one literal — their quote-heavy SQL/JSON bodies must never
    desynchronize the scan (that desync silently dropped whole classes on the
    real corpus).
    """
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


def _matching_delim(masked: str, open_index: int, open_char: str, close_char: str) -> int | None:
    """Index of the delimiter matching ``open_char`` at ``open_index``.

    Runs on comment-masked text; string/char literals are still skipped so
    braces/parens inside selector strings never miscount.
    """
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
            i = _text_block_end(masked, i + 3)  # skip the whole text block
            continue
        elif char in ('"', "'"):
            in_string = char
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _matching_brace(masked: str, open_index: int) -> int | None:
    return _matching_delim(masked, open_index, "{", "}")


def _matching_paren(masked: str, open_index: int) -> int | None:
    return _matching_delim(masked, open_index, "(", ")")


# --- Java model ----------------------------------------------------------------


@dataclass
class _JavaMethod:
    name: str
    source: str  # original text (comments intact) — what snippets show
    masked: str  # comment-masked slice — what scanning reads
    params: dict[str, str]  # param name → simple type name
    return_type: str
    start: int  # offset of the signature in the class text
    end: int  # offset just past the closing brace


@dataclass
class _JavaClass:
    name: str
    fqn: str  # package-qualified name — the collision-safe identity
    package: str
    path: Path
    rel: str
    text: str  # this class's slice of the file (comments intact)
    masked: str  # comment-masked slice
    extends: str | None  # as written (simple or dotted), generics stripped
    methods: dict[str, _JavaMethod]  # method name → parsed method (overloads merged)
    method_spans: list[tuple[int, int]]  # every matched body incl. each overload's own
    fields: dict[str, str]  # field name → simple type name
    consts: dict[str, str]  # String constant name → literal value (raw, as written)
    assigned_types: dict[str, str]  # name → class assigned via `x = new T(...)` anywhere
    imports: dict[str, str]  # simple name → imported FQN (file-level, single imports)
    wildcard_imports: list[str]  # packages of `import x.y.*;`
    static_member_imports: dict[str, str]  # imported member → owner FQN
    static_star_imports: list[str]  # owner FQNs of `import static X.*`
    element_fields: set[str]  # WebElement/By-typed fields (incl. @FindBy) — leaf receivers
    lifecycle_methods: list[str]  # names of @Before*-annotated methods, file order


class JavaIndex:
    """All classes/methods under a repo root, keyed for heuristic resolution.

    Identity is the fully-qualified name; simple names resolve through the
    caller's imports → same package → wildcard imports → unique-in-repo. Two
    suites can both have a ``LoginPage`` without silently shadowing each other.
    """

    def __init__(
        self,
        classes: list[_JavaClass],
        xray_seen: int = 0,
        fallback_files: list[str] | None = None,
    ) -> None:
        self.all = classes
        self.by_fqn: dict[str, _JavaClass] = {c.fqn: c for c in classes}
        self.by_simple: dict[str, list[_JavaClass]] = {}
        for java_class in classes:
            self.by_simple.setdefault(java_class.name, []).append(java_class)
        # Discovery parity: every @Xray seen in the tree must map to a
        # discovered test, or the seeding summary reports the gap loudly.
        self.xray_seen = xray_seen
        self.fallback_files = fallback_files or []
        # (owner fqn, method name) → (call targets, flags): _calls_of is
        # deterministic per method, and full-graph traversal revisits shared
        # helpers (base wrappers) from every test.
        self.calls_cache: dict[tuple[str, str], tuple[list[tuple[str, str]], list[str]]] = {}

    @classmethod
    def build(cls, root: Path) -> JavaIndex:
        classes: list[_JavaClass] = []
        xray_seen = 0
        fallback_files: list[str] = []
        for path in sorted(root.rglob("*.java")):
            text = path.read_text(errors="replace")
            masked = _mask_comments(text)
            declscan = _mask_strings(masked)
            package_match = _PACKAGE_RE.search(declscan)
            package = package_match.group(1) if package_match else ""
            imports: dict[str, str] = {}
            wildcard_imports: list[str] = []
            for imp in _IMPORT_RE.finditer(declscan):
                target = imp.group(1)
                if target.endswith(".*"):
                    wildcard_imports.append(target[:-2])
                else:
                    imports[target.rsplit(".", 1)[-1]] = target
            member_imports: dict[str, str] = {}
            star_imports: list[str] = []
            for imp in _STATIC_IMPORT_RE.finditer(declscan):
                if imp.group(2) == "*":
                    star_imports.append(imp.group(1))
                else:
                    member_imports[imp.group(2)] = imp.group(1)
            rel = str(path.relative_to(root))
            decls = _top_level_class_decls(declscan)
            annotations_at = [m.start() for m in XRAY_RE.finditer(masked)]
            xray_seen += len(annotations_at)
            uncovered = [
                at
                for at in annotations_at
                if not any(start <= at < end for _, _, start, end in decls)
            ]
            if uncovered:
                # Class slicing failed to cover an annotated test (a construct
                # the parser cannot span). Losing tests silently is the one
                # unacceptable outcome — index the WHOLE FILE as one class
                # (the pre-slicing behavior) and say so.
                first = decls[0] if decls else None
                decls = [
                    (
                        first[0] if first else path.stem,
                        first[1] if first else None,
                        0,
                        len(text),
                    )
                ]
                fallback_files.append(rel)
                logger.warning(
                    ":: %s: class structure not fully parsed — whole-file fallback "
                    "(%d @Xray annotation(s) were outside every parsed class)",
                    rel,
                    len(uncovered),
                )
            for name, extends_raw, start, end in decls:
                c_text = text[start:end]
                c_masked = masked[start:end]
                methods, method_spans = _split_methods(c_text, c_masked, name)
                fields = _class_fields(c_masked, method_spans)
                element_fields = {f for f, t in fields.items() if t in _ELEMENT_TYPES}
                element_fields.update(_findby_field_names(c_masked))
                classes.append(
                    _JavaClass(
                        name=name,
                        fqn=f"{package}.{name}" if package else name,
                        package=package,
                        path=path,
                        rel=rel,
                        text=c_text,
                        masked=c_masked,
                        extends=extends_raw,
                        methods=methods,
                        method_spans=method_spans,
                        fields=fields,
                        consts={
                            m.group(1): m.group(2)[1:-1]
                            for m in _STRING_CONST_RE.finditer(c_masked)
                        },
                        assigned_types=_assigned_new_types(c_masked),
                        imports=imports,
                        wildcard_imports=wildcard_imports,
                        static_member_imports=member_imports,
                        static_star_imports=star_imports,
                        element_fields=element_fields,
                        lifecycle_methods=[
                            m.name
                            for m in sorted(methods.values(), key=lambda m: m.start)
                            if _BEFORE_ANNOTATION_RE.search(_decoration_zone(c_masked, m))
                        ],
                    )
                )
        return cls(classes, xray_seen=xray_seen, fallback_files=fallback_files)

    def resolve(self, name: str, owner: _JavaClass | None = None) -> _JavaClass | None:
        """A type name as written in ``owner`` → the repo class it denotes.

        Dotted names must match a fully-qualified indexed class (an unknown
        dotted name is external, never tail-matched — wrong-class risk). A
        single import that points OUTSIDE the tree resolves to None without
        falling back: the import is the author's answer.
        """
        if "." in name:
            return self.by_fqn.get(name)
        if owner is not None:
            imported = owner.imports.get(name)
            if imported is not None:
                return self.by_fqn.get(imported)
            same_package = self.by_fqn.get(f"{owner.package}.{name}" if owner.package else name)
            if same_package is not None:
                return same_package
            for package in owner.wildcard_imports:
                via_star = self.by_fqn.get(f"{package}.{name}")
                if via_star is not None:
                    return via_star
        bucket = self.by_simple.get(name, [])
        if len(bucket) == 1:
            return bucket[0]
        return None

    def is_external(self, name: str, owner: _JavaClass) -> bool:
        """True when ``owner`` imports ``name`` from outside the tree —
        known-external (RestAssured, JDBC, JUnit…), silent by knowledge."""
        imported = owner.imports.get(name)
        return imported is not None and imported not in self.by_fqn

    def ambiguity(self, name: str) -> int:
        return len(self.by_simple.get(name, []))

    def ancestors(self, java_class: _JavaClass) -> list[_JavaClass]:
        """The class followed by its ``extends`` chain (in-repo classes only)."""
        chain: list[_JavaClass] = []
        seen: set[str] = set()
        current: _JavaClass | None = java_class
        while current is not None and current.fqn not in seen:
            seen.add(current.fqn)
            chain.append(current)
            current = self.resolve(current.extends, current) if current.extends else None
        return chain

    def lookup_method(
        self, java_class: _JavaClass, method_name: str
    ) -> tuple[_JavaClass, _JavaMethod] | None:
        """Resolve a method against a class or anything it extends."""
        chain = self.ancestors(java_class)
        if method_name == "<init>":  # constructors are not inherited
            chain = chain[:1]
        for candidate in chain:
            method = candidate.methods.get(method_name)
            if method is not None:
                return candidate, method
        return None


def _top_level_class_decls(declscan: str) -> list[tuple[str, str | None, int, int]]:
    """Every top-level (class, extends, start, end) in string-blanked text.

    Nested declarations stay inside the enclosing class's slice (its method
    scan already covers them); a file may hold several top-level classes.
    """
    decls: list[tuple[str, str | None, int, int]] = []
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
        header = _strip_generics(declscan[match.end() : brace_at])
        extends_match = _EXTENDS_RE.search(header)
        decls.append(
            (
                match.group(1),
                extends_match.group(1) if extends_match else None,
                match.start(),
                body_end + 1,
            )
        )
        taken.append((match.start(), body_end + 1))
    return decls


def _strip_generics(header: str) -> str:
    """``Foo<T extends Bar<T>> extends Base`` → ``Foo extends Base``."""
    out: list[str] = []
    depth = 0
    for char in header:
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return "".join(out)


def _decoration_zone(class_masked: str, method: _JavaMethod) -> str:
    """The annotation zone of a method: above the signature (after the previous
    member's closing brace) plus the signature's own first line — one-line
    forms like ``@Xray(...) public void t() {`` are consumed into the match."""
    preamble = class_masked[max(0, method.start - 400) : method.start]
    first_line = method.masked.split("\n", 1)[0]
    return preamble.rsplit("}", 1)[-1] + "\n" + first_line


def _simple_type(raw: str) -> str:
    """``com.x.Page<T>[]`` → ``Page`` — the index key form of a type name."""
    return raw.split("<")[0].split(".")[-1].replace("[]", "").replace("...", "").strip()


def _assigned_new_types(masked: str) -> dict[str, str]:
    """``x = new T(...)`` bindings — but only when the constructor is the whole
    right-hand side: ``x = new T(...).chain(...)`` yields the CHAIN's type, which
    only the declared type knows, so those must not bind x to T."""
    bindings: dict[str, str] = {}
    for match in _ASSIGN_NEW_RE.finditer(masked):
        close = _matching_paren(masked, match.end() - 1)
        if close is None:
            continue
        rest = masked[close + 1 :].lstrip()
        if not rest.startswith("."):
            bindings[match.group(1)] = match.group(2)
    return bindings


def _parse_params(params_src: str) -> dict[str, str]:
    """``"WebDriver driver, Map<String, String> row"`` → {name: simple type}."""
    params: dict[str, str] = {}
    depth = 0
    part = ""
    parts: list[str] = []
    for char in params_src:
        if char in "<(":
            depth += 1
        elif char in ">)":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += char
    parts.append(part)
    for candidate in parts:
        tokens = [t for t in candidate.split() if t not in ("final",) and not t.startswith("@")]
        if len(tokens) >= 2:
            params[tokens[-1]] = _simple_type(" ".join(tokens[:-1]).split()[-1])
    return params


def _split_methods(
    text: str, masked: str, class_name: str
) -> tuple[dict[str, _JavaMethod], list[tuple[int, int]]]:
    """Method name → parsed method, plus the spans of EVERY matched body.

    Constructors are stored under ``<init>``. Overloads merge into one entry
    (sources concatenated) so every body is available to resolution — but each
    overload's own span is still returned, so field-zone blanking removes all
    method bodies, not just the first overload's.
    """
    methods: dict[str, _JavaMethod] = {}
    spans: list[tuple[int, int]] = []

    def add(name: str, params_src: str, return_type: str, start: int, brace_at: int) -> None:
        body_end = _matching_brace(masked, brace_at)
        if body_end is None:
            return
        spans.append((start, body_end + 1))
        # `else if (...) {` / `new Thread() {` would otherwise parse as methods
        # (their spans sit inside a real method's span, so blanking is unharmed).
        if name in _JAVA_KEYWORDS or return_type.strip() in ("new", "else"):
            return
        parsed = _JavaMethod(
            name=name,
            source=text[start : body_end + 1],
            masked=masked[start : body_end + 1],
            params=_parse_params(params_src),
            return_type=_simple_type(return_type) if return_type else "",
            start=start,
            end=body_end + 1,
        )
        existing = methods.get(name)
        if existing is None:
            methods[name] = parsed
        else:  # overload: keep every body visible, prefer a chain-followable return type
            existing.source += "\n\n" + parsed.source
            existing.masked += "\n\n" + parsed.masked
            existing.params.update(parsed.params)
            if existing.return_type in ("", "void") and parsed.return_type:
                existing.return_type = parsed.return_type

    for match in _METHOD_SIG_RE.finditer(masked):
        add(match.group(2), match.group(3), match.group(1), match.start(), match.end() - 1)
    ctor_re = re.compile(
        r'^[ \t]*(?:@\w+(?:\([^)]*\))?[ \t]+)*(?:(?:public|protected|private)\s+)*'
        + re.escape(class_name)
        + r'\s*\(([^)]*)\)\s*\{',
        re.M,
    )
    for match in ctor_re.finditer(masked):
        add("<init>", match.group(1), class_name, match.start(), match.end() - 1)
    return methods, spans


def _blank_method_spans(masked: str, spans: list[tuple[int, int]]) -> str:
    """The class body with method bodies blanked — the field/constant zone."""
    blanked = list(masked)
    for start, end in spans:
        for i in range(start, min(end, len(blanked))):
            if blanked[i] != "\n":
                blanked[i] = " "
    return "".join(blanked)


def _class_fields(masked: str, spans: list[tuple[int, int]]) -> dict[str, str]:
    """Field name → simple type, from the class body OUTSIDE method bodies."""
    zone = _blank_method_spans(masked, spans)
    fields: dict[str, str] = {}
    for match in _FIELD_DECL_RE.finditer(zone):
        type_name, name = match.group(1), match.group(2)
        if type_name not in ("Xray",) and name != type_name:
            fields[name] = type_name
    return fields


# --- locator extraction ----------------------------------------------------------


def _split_top_level(expr: str, separator: str) -> list[str]:
    """Split a Java expression on ``separator`` outside strings/parens."""
    parts: list[str] = []
    depth = 0
    in_string: str | None = None
    current = ""
    i = 0
    while i < len(expr):
        char = expr[i]
        if in_string:
            current += char
            if char == "\\" and i + 1 < len(expr):
                current += expr[i + 1]
                i += 2
                continue
            if char == in_string:
                in_string = None
        elif char in ('"', "'"):
            in_string = char
            current += char
        elif char in "(<":
            depth += 1
            current += char
        elif char in ")>":
            depth -= 1
            current += char
        elif char == separator and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
        i += 1
    parts.append(current)
    return parts


def _resolve_string_expr(
    expr: str, owner: _JavaClass, index: JavaIndex
) -> tuple[str, bool] | None:
    """Fold a locator argument to (literal value, is_template), or None.

    Handles ``"literal"``, ``CONST`` (owner class + extends chain + static
    imports), ``Other.CONST``, ``+`` concatenations, and dynamic SKELETONS:
    ``String.format(FMT, …)`` keeps its ``%s`` markers, and a concatenated
    variable/parameter becomes a ``{name}`` placeholder (lowercase identifiers
    only — by Java convention those are variables; an unresolvable UPPER_CASE
    name is a constant we failed to find and must stay unresolved, never
    guessed). A value that is ONLY placeholders carries no information and
    resolves to None.
    """
    expr = expr.strip()
    format_match = _FORMAT_OPEN_RE.match(expr)
    if format_match:
        close = _matching_paren(expr, format_match.end() - 1)
        if close is None or expr[close + 1 :].strip():
            return None
        args = _split_top_level(expr[format_match.end() : close], ",")
        if not args or not args[0].strip():
            return None
        skeleton = _resolve_string_expr(args[0], owner, index)
        if skeleton is None:
            return None
        value = skeleton[0]
        if not _FORMAT_MARK_RE.sub("", value).strip():
            return None  # pure "%s" — no information
        return value, True

    resolved: list[str] = []
    is_template = False
    for part in _split_top_level(expr, "+"):
        part = part.strip()
        if part.startswith("this."):
            part = part[len("this.") :].strip()
        literal = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', part)
        if literal:
            resolved.append(literal.group(1))
            continue
        if re.fullmatch(r"\w+", part):
            value = _lookup_const(part, owner, index)
            if value is not None:
                resolved.append(value)
            elif part[0].islower() or part[0] == "_":
                resolved.append("{" + part + "}")  # a variable/parameter by convention
                is_template = True
            else:
                return None  # an UPPER_CASE constant we failed to resolve
            continue
        qualified = re.fullmatch(r"(\w+)\.(\w+)", part)
        if qualified:
            other = index.resolve(qualified.group(1), owner)
            value = other.consts.get(qualified.group(2)) if other else None
            if value is None:
                return None
            resolved.append(value)
            continue
        return None
    value = "".join(resolved)
    if is_template and not _PLACEHOLDER_RE.sub("", value).strip():
        return None  # nothing but placeholders — no information
    if _FORMAT_MARK_RE.search(value):
        is_template = True  # a const that is itself a format skeleton
    return value, is_template


def _lookup_const(name: str, owner: _JavaClass, index: JavaIndex) -> str | None:
    for java_class in index.ancestors(owner):
        if name in java_class.consts:
            return java_class.consts[name]
    imported_from = owner.static_member_imports.get(name)
    owners = [imported_from] if imported_from else owner.static_star_imports
    for owner_fqn in owners:
        java_class = index.by_fqn.get(owner_fqn or "")
        if java_class and name in java_class.consts:
            return java_class.consts[name]
    return None


def _by_calls(masked: str) -> list[tuple[str, int, int]]:
    """Every ``By.<method>(...)`` in ``masked`` → (by_method, arg_start, arg_end)."""
    calls: list[tuple[str, int, int]] = []
    for match in _BY_OPEN_RE.finditer(masked):
        close = _matching_paren(masked, match.end() - 1)
        if close is not None:
            calls.append((match.group(1), match.end(), close))
    return calls


def _locators_in(
    masked: str,
    owner: _JavaClass,
    index: JavaIndex,
    declared_in_fallback: str,
    attribute_fields: bool = False,
) -> tuple[list[ExtractedLocator], list[str]]:
    """Extract (resolved locators, unresolved descriptions) from a masked slice."""
    locators: list[ExtractedLocator] = []
    unresolved: list[str] = []
    for by_method, arg_start, arg_end in _by_calls(masked):
        expr = masked[arg_start:arg_end].strip()
        declared_in = declared_in_fallback
        if attribute_fields:
            prefix = masked[max(0, arg_start - 160) : arg_start]
            field_match = re.search(r"By\s+(\w+)\s*=\s*By\.\w+\s*\(\Z", prefix)
            if field_match:
                declared_in = f"{owner.name}.{field_match.group(1)}"
        resolved = _resolve_string_expr(expr, owner, index)
        if resolved is None:
            unresolved.append(
                f"{declared_in}: By.{by_method}({expr}) — value not statically resolvable"
            )
        else:
            value, is_template = resolved
            locators.append(
                ExtractedLocator(
                    kind=_BY_KIND[by_method],
                    value=f'By.{by_method}("{value}")',
                    declared_in=declared_in,
                    template=is_template,
                )
            )
    return locators, unresolved


def _findby_field_names(masked: str) -> set[str]:
    """Names of ``@FindBy(...) WebElement <name>`` fields (silent leaf receivers)."""
    names: set[str] = set()
    for open_match in _FINDBY_OPEN_RE.finditer(masked):
        close = _matching_paren(masked, open_match.end() - 1)
        if close is None:
            continue
        field_match = _FINDBY_FIELD_RE.match(masked, close + 1)
        if field_match is not None:
            names.add(field_match.group(1))
    return names


def _findby_locators(
    java_class: _JavaClass, index: JavaIndex
) -> tuple[list[ExtractedLocator], list[str]]:
    """PageFactory ``@FindBy(...) WebElement field`` locators of a class."""
    locators: list[ExtractedLocator] = []
    unresolved: list[str] = []
    masked = java_class.masked
    for open_match in _FINDBY_OPEN_RE.finditer(masked):
        close = _matching_paren(masked, open_match.end() - 1)
        if close is None:
            continue
        field_match = _FINDBY_FIELD_RE.match(masked, close + 1)
        if field_match is None:
            continue
        attrs_src = masked[open_match.end() : close]
        attrs = {m.group(1): m.group(2).strip() for m in _FINDBY_ATTR_RE.finditer(attrs_src)}
        field_name = field_match.group(1)
        declared_in = f"{java_class.name}.{field_name}"
        how = attrs.pop("how", "")
        value_expr = attrs.pop("using", "")
        by_method = _FINDBY_TO_BY.get(how.rsplit(".", 1)[-1]) if how else None
        if by_method is None:
            for attr, expr in attrs.items():
                if attr in _FINDBY_TO_BY:
                    by_method, value_expr = _FINDBY_TO_BY[attr], expr
                    break
        if by_method is None:
            continue
        resolved = _resolve_string_expr(value_expr, java_class, index)
        if resolved is None:
            unresolved.append(
                f"{declared_in}: @FindBy {by_method}({value_expr}) — "
                "value not statically resolvable"
            )
        else:
            value, is_template = resolved
            locators.append(
                ExtractedLocator(
                    kind=_BY_KIND[by_method],
                    value=f'By.{by_method}("{value}")',
                    declared_in=declared_in,
                    template=is_template,
                )
            )
    return locators, unresolved


# --- test discovery + helper resolution ----------------------------------------


def extract_java_tests(
    root: Path,
    index: JavaIndex | None = None,
    *,
    helper_depth: int | None = DEFAULT_HELPER_DEPTH,
    helper_char_cap: int = DEFAULT_HELPER_CHAR_CAP,
) -> list[TestBundle]:
    """One bundle per ``@Xray``-annotated method found under ``root``."""
    index = index or JavaIndex.build(root)
    depth = _UNBOUNDED_DEPTH if helper_depth is None else max(1, helper_depth)
    bundles: list[TestBundle] = []
    for java_class in index.all:
        for method_name, method in java_class.methods.items():
            # The annotation sits in the decoration zone directly above the
            # signature (or on the signature line itself) — a key mentioned
            # inside another method's body can never be picked up.
            annotation = XRAY_RE.search(_decoration_zone(java_class.masked, method))
            if not annotation:
                continue
            bundle = TestBundle(
                ref=f"{java_class.rel}#{method_name}",
                test_name=method_name,
                class_name=java_class.name,
                language="java",
                xray_key=annotation.group(1),
                code=f"// {java_class.rel}\n{method.source}",
            )
            _resolve_helpers(
                bundle, method, java_class, index, depth=depth, char_cap=helper_char_cap
            )
            _collect_java_urls(bundle, method.masked)
            bundles.append(bundle)
    return bundles


class _Resolution:
    """Mutable state of one bundle's helper walk (dedup, budget, flags)."""

    def __init__(self, char_cap: int) -> None:
        self.included: set[tuple[str, str]] = set()  # (fqn, method) already snippeted
        self.visited_classes: set[str] = set()  # fqns whose locator fields are harvested
        self.flags: list[str] = []
        self.flagged: set[str] = set()
        self.unresolved_count = 0
        self.char_cap = char_cap
        self.used = 0

    def flag(self, text: str) -> None:
        if text not in self.flagged:
            self.flagged.add(text)
            self.flags.append(text)
            if text.startswith("unresolved:"):
                self.unresolved_count += 1

    def fits(self, snippet: str) -> bool:
        if self.used + len(snippet) > self.char_cap:
            return False
        self.used += len(snippet)
        return True


def _lifecycle_entry_points(
    test_class: _JavaClass, index: JavaIndex
) -> list[tuple[_JavaClass, _JavaMethod]]:
    """The ``@Before*`` methods that run before a test of ``test_class``,
    parent-first (JUnit/TestNG execute the base class's setup first). Login and
    navigation routinely live here — they are part of every test's real flow."""
    entry_points: list[tuple[_JavaClass, _JavaMethod]] = []
    for java_class in reversed(index.ancestors(test_class)):
        for name in java_class.lifecycle_methods:
            method = java_class.methods.get(name)
            if method is not None:
                entry_points.append((java_class, method))
    return entry_points


def _resolve_helpers(
    bundle: TestBundle,
    test_method: _JavaMethod,
    test_class: _JavaClass,
    index: JavaIndex,
    depth: int,
    char_cap: int = DEFAULT_HELPER_CHAR_CAP,
) -> None:
    """Follow the test's execution path into repo helpers (plan §5.2).

    ``@Before*`` lifecycle methods are walked first (they run first), then the
    test's own calls, transitively to ``depth`` hops (unbounded by default).
    The snippet budget only limits helper TEXT handed to the Distiller —
    traversal, locator extraction and unresolved-flagging always run to the
    end, so a fat early helper can no longer starve later classes.
    """
    state = _Resolution(char_cap)
    queue: deque[tuple[str, str, int]] = deque()
    for ancestor in index.ancestors(test_class):
        queue.append((ancestor.fqn, "*", depth))
    for setup_class, setup_method in _lifecycle_entry_points(test_class, index):
        key = (setup_class.fqn, setup_method.name)
        if key in state.included:
            continue
        state.included.add(key)
        label = f"{setup_class.name}.{setup_method.name} ({setup_class.rel})"
        snippet = f"// setup (@Before): {label} — runs before the test\n{setup_method.source}"
        if state.fits(snippet):
            bundle.helper_snippets.append(snippet)
        else:
            state.flag(f"truncated:setup {label} omitted (helper budget reached)")
        bundle.helper_refs.append(f"setup:{label}")
        bundle.lifecycle_refs.append(label)
        _method_locators(
            bundle, setup_method, setup_class, index,
            f"{setup_class.name}#{setup_method.name}",
        )
        _collect_java_urls(bundle, setup_method.masked)
        queue.extend(
            (fqn, name, depth)
            for fqn, name in _calls_of(setup_method, setup_class, index, state)
        )
    queue.extend(
        (fqn, name, depth) for fqn, name in _calls_of(test_method, test_class, index, state)
    )
    _method_locators(
        bundle, test_method, test_class, index, f"{test_class.name}#{bundle.test_name}"
    )

    while queue:
        fqn, method_name, remaining = queue.popleft()
        java_class = index.by_fqn.get(fqn)
        if java_class is None:  # push-side resolution makes this unreachable; belt+braces
            state.flag(f"unresolved:{fqn}.{method_name}")
            continue
        if fqn not in state.visited_classes:
            state.visited_classes.add(fqn)
            _visit_class(bundle, java_class, index, state)
            # A class's locator fields include what it inherits — harvest the
            # whole extends chain, not just the class the call resolved into.
            queue.extend(
                (ancestor.fqn, "*", remaining)
                for ancestor in index.ancestors(java_class)[1:]
                if ancestor.fqn not in state.visited_classes
            )
        if method_name == "*":
            continue
        resolved = index.lookup_method(java_class, method_name)
        if resolved is None:
            state.flag(f"unresolved:{java_class.name}.{method_name}")
            continue
        defining, method = resolved
        key = (defining.fqn, method_name)
        if key in state.included:
            continue
        state.included.add(key)
        if defining.fqn not in state.visited_classes:
            state.visited_classes.add(defining.fqn)
            _visit_class(bundle, defining, index, state)

        label = "<init>" if method_name == "<init>" else method_name
        snippet = f"// {defining.name}.{label} ({defining.rel})\n{method.source}"
        if state.fits(snippet):
            bundle.helper_snippets.append(snippet)
        else:
            state.flag(
                f"truncated:{defining.name}.{label} omitted "
                f"(helper budget {char_cap} chars reached)"
            )
        bundle.helper_refs.append(f"{defining.name}.{label} ({defining.rel})")
        _method_locators(bundle, method, defining, index, f"{defining.name}#{label}")
        _collect_java_urls(bundle, method.masked)
        if remaining > 1:
            queue.extend(
                (next_fqn, next_name, remaining - 1)
                for next_fqn, next_name in _calls_of(method, defining, index, state)
            )

    bundle.unresolved_count = state.unresolved_count
    flags = state.flags
    if len(flags) > _MAX_UNRESOLVED_FLAGS:
        overflow = len(flags) - _MAX_UNRESOLVED_FLAGS
        flags = flags[:_MAX_UNRESOLVED_FLAGS] + [f"unresolved:… and {overflow} more"]
    bundle.helper_refs.extend(flags)


def _visit_class(
    bundle: TestBundle, java_class: _JavaClass, index: JavaIndex, state: _Resolution
) -> None:
    """First visit of a class: harvest its locator fields (+ @FindBy)."""
    field_zone = _blank_method_spans(java_class.masked, java_class.method_spans)
    locators, unresolved = _locators_in(
        field_zone, java_class, index, java_class.name, attribute_fields=True
    )
    findby_locators, findby_unresolved = _findby_locators(java_class, index)
    locators.extend(findby_locators)
    unresolved.extend(findby_unresolved)
    added = _add_locators(bundle, locators)
    bundle.unresolved_locators.extend(
        u for u in unresolved if u not in bundle.unresolved_locators
    )
    if added:
        rendered = "\n".join(
            f"By {loc.declared_in.split('.')[-1]} = {loc.value};"
            + ("  // template — runtime-filled parts" if loc.template else "")
            for loc in added
        )
        snippet = f"// locator fields of {java_class.name} ({java_class.rel})\n{rendered}"
        if state.fits(snippet):
            bundle.helper_snippets.append(snippet)
        else:
            state.flag(
                f"truncated:{java_class.name} locator fields omitted (helper budget reached)"
            )


def _method_locators(
    bundle: TestBundle,
    method: _JavaMethod,
    owner: _JavaClass,
    index: JavaIndex,
    declared_in: str,
) -> None:
    """Locators written inline in a visited method body."""
    locators, unresolved = _locators_in(method.masked, owner, index, declared_in)
    _add_locators(bundle, locators)
    for entry in unresolved:
        # Field references like By.id(EMAIL_ID) resolve at class level; an
        # unresolvable CONST inside a method is only reported if the class-level
        # pass did not already resolve a locator with that expression.
        if entry not in bundle.unresolved_locators:
            bundle.unresolved_locators.append(entry)


def _add_locators(bundle: TestBundle, locators: list[ExtractedLocator]) -> list[ExtractedLocator]:
    seen = {(loc.kind, loc.value) for loc in bundle.locators}
    added: list[ExtractedLocator] = []
    for locator in locators:
        key = (locator.kind, locator.value)
        if key not in seen:
            seen.add(key)
            bundle.locators.append(locator)
            added.append(locator)
    return added


def _var_types_for(method: _JavaMethod, owner: _JavaClass, index: JavaIndex) -> dict[str, str]:
    """Receiver name → simple type, from every binding source we can see."""
    var_types: dict[str, str] = {}
    for java_class in reversed(index.ancestors(owner)):
        var_types.update(java_class.fields)
        var_types.update(java_class.assigned_types)
    var_types.update(method.params)
    var_types.update(_assigned_new_types(method.masked))
    # Declared local types come LAST — for `NotesPage notes = new LoginPage(...)
    # .open(...).loginAs(...)` the declaration knows the chain's final type.
    for match in re.finditer(r"\b([A-Z]\w*)(?:<[^>\n]*>)?\s+([a-z_]\w*)\s*[=;]", method.masked):
        var_types[match.group(2)] = match.group(1)
    return var_types


def _calls_of(
    method: _JavaMethod,
    owner: _JavaClass,
    index: JavaIndex,
    state: _Resolution,
) -> list[tuple[str, str]]:
    """(class fqn, method) targets this method calls, heuristically resolved.

    Targets are resolved import/package-aware at the CALL SITE (the caller's
    file knows which ``LoginPage`` it means). Every receiver we cannot type and
    every call we cannot place is FLAGGED via ``state`` — resolution gaps must
    be visible in the review output, never silent — EXCEPT names imported from
    outside the tree (RestAssured, JDBC, JUnit…), which are known-external.
    Deterministic per (owner, method), so results are cached on the index.
    """
    cache_key = (owner.fqn, method.name)
    cached = index.calls_cache.get(cache_key)
    if cached is not None:
        cached_targets, cached_flags = cached
        for text in cached_flags:
            state.flag(text)
        return list(cached_targets)

    masked = method.masked
    var_types = _var_types_for(method, owner, index)
    targets: list[tuple[str, str]] = []
    flags: list[str] = []

    def flag(text: str) -> None:
        if text not in flags:
            flags.append(text)

    def element_field(name: str) -> bool:
        return any(name in c.element_fields for c in index.ancestors(owner))

    def push(java_class: _JavaClass, method_name: str) -> _JavaClass | None:
        """Queue a call target; returns its return type's class for chains."""
        targets.append((java_class.fqn, method_name))
        resolved = index.lookup_method(java_class, method_name)
        if resolved is None:
            return None
        defining, target = resolved
        # The return type is written in the DEFINING class's file — resolve it
        # against that file's imports/package.
        return index.resolve(target.return_type, defining) if target.return_type else None

    def follow_chain(close_at: int, current: _JavaClass | None) -> None:
        hops = 0
        position = close_at + 1
        while current is not None and hops < _CHAIN_HOP_LIMIT:
            nxt = _CHAIN_NEXT_RE.match(masked, position)
            if not nxt:
                return
            current = push(current, nxt.group(1))
            close = _matching_paren(masked, nxt.end() - 1)
            if close is None:
                return
            position = close + 1
            hops += 1

    def resolve_or_flag(name: str, method_name: str) -> _JavaClass | None:
        java_class = index.resolve(name, owner)
        if java_class is not None:
            return java_class
        if name in _KNOWN_RECEIVERS or name in _ELEMENT_TYPES or index.is_external(name, owner):
            return None
        if index.ambiguity(name) > 1:
            flag(
                f"unresolved:{name}.{method_name} (ambiguous: "
                f"{index.ambiguity(name)} classes named {name})"
            )
        else:
            flag(f"unresolved:{name}.{method_name}")
        return None

    # new X(...) — visit the class (locator fields), include its constructor,
    # and follow any fluent chain hanging off the expression.
    for match in _NEW_RE.finditer(masked):
        java_class = index.resolve(match.group(1), owner)
        if java_class is None:
            continue
        targets.append((java_class.fqn, "*"))
        if "<init>" in java_class.methods:
            targets.append((java_class.fqn, "<init>"))
        close = _matching_paren(masked, match.end() - 1)
        if close is not None:
            follow_chain(close, java_class)

    for match in _RECEIVER_CALL_RE.finditer(masked):
        receiver, method_name = match.group(1), match.group(2)
        close = _matching_paren(masked, match.end() - 1)
        if receiver in ("this", "super"):
            start: _JavaClass | None = owner
            if receiver == "super":
                start = index.resolve(owner.extends, owner) if owner.extends else None
            if start is not None and index.lookup_method(start, method_name):
                return_class = push(start, method_name)
                if close is not None:
                    follow_chain(close, return_class)
            else:
                flag(f"unresolved:{owner.name}.{method_name}")
            continue
        if receiver[0].isupper():
            if receiver in var_types:  # an upper-cased variable shadows the class namespace
                continue
            java_class = resolve_or_flag(receiver, method_name)
            if java_class is not None:
                return_class = push(java_class, method_name)
                if close is not None:
                    follow_chain(close, return_class)
            continue
        receiver_type = var_types.get(receiver)
        if receiver_type is not None:
            if receiver_type in _ELEMENT_TYPES or receiver_type in _KNOWN_RECEIVERS:
                continue  # framework action on a JDK/Selenium-typed receiver
            java_class = index.resolve(receiver_type, owner)
            if java_class is not None:
                return_class = push(java_class, method_name)
                if close is not None:
                    follow_chain(close, return_class)
            elif not index.is_external(receiver_type, owner) and index.ambiguity(receiver_type) > 1:
                flag(
                    f"unresolved:{receiver}.{method_name} (receiver type {receiver_type} "
                    f"ambiguous: {index.ambiguity(receiver_type)} classes)"
                )
            # else: imported-external or unknown framework type — silently fine
            continue
        if element_field(receiver):
            continue
        flag(f"unresolved:{receiver}.{method_name} (untyped receiver)")

    for match in _BARE_CALL_RE.finditer(masked):
        name = match.group(1)
        if name in _JAVA_KEYWORDS:
            continue
        preceding = masked[: match.start()].rstrip()
        if preceding.endswith("."):  # chained — handled by follow_chain
            continue
        sig_close = _matching_paren(masked, match.end() - 1)
        if sig_close is not None and re.match(
            r"\s*(?:throws[\w, .]+)?\s*\{", masked[sig_close + 1 :]
        ):
            continue  # `name(args) {` is a definition (the method's own signature), not a call
        own = index.lookup_method(owner, name)
        if own is not None:
            return_class = push(owner, name)
            close = _matching_paren(masked, match.end() - 1)
            if close is not None:
                follow_chain(close, return_class)
            continue
        imported_from = owner.static_member_imports.get(name)
        if imported_from is not None:
            imported_class = index.by_fqn.get(imported_from)
            if imported_class is not None:
                push(imported_class, name)
            # else: static import from outside the repo (JUnit asserts, …) — known-external
            continue
        star_owner = next(
            (
                c
                for fqn in owner.static_star_imports
                if (c := index.by_fqn.get(fqn)) and index.lookup_method(c, name)
            ),
            None,
        )
        if star_owner is not None:
            push(star_owner, name)
            continue
        if owner.static_star_imports:
            continue  # plausibly an external static-star import (assertions, matchers)
        if name in var_types or element_field(name):
            continue  # a lambda/functional field, not a helper
        flag(f"unresolved:{name}() (no matching method found)")

    # Qualified FIELD access (`Locators.SAVE`) — no call to chase, but the class
    # holds locators: visit it. Static-import owners in the tree likewise.
    for match in _FIELD_ACCESS_RE.finditer(masked):
        java_class = index.resolve(match.group(1), owner)
        if java_class is not None:
            targets.append((java_class.fqn, "*"))
    for owner_fqn in {*owner.static_member_imports.values(), *owner.static_star_imports}:
        if owner_fqn in index.by_fqn:
            targets.append((owner_fqn, "*"))

    index.calls_cache[cache_key] = (targets, flags)
    for text in flags:
        state.flag(text)
    return list(targets)


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
        if len(text) > _TS_CODE_CAP:
            bundle.helper_refs.append(
                f"truncated:spec exceeds {_TS_CODE_CAP} chars — code cut off"
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
