"""Static extraction for KB seeding: tests, locators, and helper resolution.

Everything here is deterministic code — the Distiller agent never searches the
repo (RETRIEVAL_MEMORY_PLAN.md §5). Discovery: any Java method annotated
``@Xray(testCase = "KEY")`` is a test; the rest of the walked tree becomes the
helper pool. Per test, **bounded static resolution** (default depth 2,
size-capped) follows the test's calls into helpers found in the tree — that is
where page objects keep their locators. Receivers are typed from local
declarations, class fields (the ``page = new LoginPage(driver)``-in-setUp
pattern), method parameters, ``extends`` chains, ``this``/``super`` and static
imports; fluent chains are followed through declared return types. Anything
unresolvable is recorded as ``unresolved:...`` so extraction completeness is
visible, never silent.

Locator values written as string constants (``By.id(LOGIN_ID)`` with
``String LOGIN_ID = "login"`` — the dominant real-suite shape) are resolved to
their literals; ``@FindBy`` PageFactory fields are read too. A locator whose
value cannot be statically resolved lands in ``unresolved_locators`` — flagged,
never guessed. All structure scanning runs on a comment-masked copy of the
source, so braces or apostrophes inside comments can never mis-slice a method.

Extracted locators are ground truth the Distiller must not contradict; their
``kind`` maps onto the selector resilience ladder used everywhere else.
Heuristic name-based parsing; no Java compiler.
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
_METHOD_SIG_RE = re.compile(
    r'^[ \t]*(?:(?:public|protected|private|static|final|synchronized|abstract|default)\s+)*'
    r'([\w<>\[\], ?.]+)\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w, .]+\s*)?\{',
    re.M,
)
_CLASS_RE = re.compile(
    r'\b(?:class|@interface|interface|enum)\s+(\w+)(?:\s+extends\s+([\w.]+))?'
)
_PACKAGE_RE = re.compile(r'^\s*package\s+([\w.]+)\s*;', re.M)
_STATIC_IMPORT_RE = re.compile(r'^\s*import\s+static\s+([\w.]+)\.(\w+|\*)\s*;', re.M)
_STRING_CONST_RE = re.compile(r'\bString\s+(\w+)\s*=\s*("(?:[^"\\]|\\.)*")\s*;')
_FIELD_DECL_RE = re.compile(r'^\s*[\w \t]*?\b([A-Z]\w*)(?:<[^>\n]*>)?\s+(\w+)\s*[;=]', re.M)
_ASSIGN_NEW_RE = re.compile(r'\b(\w+)\s*=\s*new\s+([A-Z]\w*)\s*\(')
_NEW_RE = re.compile(r'\bnew\s+([A-Z]\w*)\s*\(')
_RECEIVER_CALL_RE = re.compile(r'\b(\w+)\s*\.\s*(\w+)\s*\(')
_BARE_CALL_RE = re.compile(r'(?<![.\w])([a-z_]\w*)\s*\(')
_CHAIN_NEXT_RE = re.compile(r'\s*\.\s*(\w+)\s*\(')
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

DEFAULT_HELPER_DEPTH = 2
DEFAULT_HELPER_CHAR_CAP = 24_000
_TS_CODE_CAP = 20_000
_MAX_UNRESOLVED_FLAGS = 30
_CHAIN_HOP_LIMIT = 8


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
    # Locators that exist in the code but whose VALUE could not be statically
    # resolved (dynamic xpath, format call, unknown constant). Flagged so review
    # sees them; the Distiller is told to never guess these.
    unresolved_locators: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    @property
    def source_code(self) -> str:
        """Test + helper bundle, for the record's ``source_code`` payload."""
        return "\n\n".join([self.code, *self.helper_snippets])


# --- comment/string-aware scanning --------------------------------------------


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
    package: str
    path: Path
    rel: str
    text: str
    masked: str
    extends: str | None
    methods: dict[str, _JavaMethod]  # method name → parsed method (overloads merged)
    fields: dict[str, str]  # field name → simple type name
    consts: dict[str, str]  # String constant name → literal value (raw, as written)
    assigned_types: dict[str, str]  # name → class assigned via `x = new T(...)` anywhere
    static_member_imports: dict[str, str]  # imported member → simple class name
    static_star_imports: list[str]  # simple class names of `import static X.*`
    element_fields: set[str]  # WebElement/By-typed fields (incl. @FindBy) — leaf receivers


class JavaIndex:
    """All classes/methods under a repo root, keyed for heuristic resolution."""

    def __init__(self, classes: dict[str, _JavaClass]) -> None:
        self.classes = classes

    @classmethod
    def build(cls, root: Path) -> JavaIndex:
        classes: dict[str, _JavaClass] = {}
        for path in sorted(root.rglob("*.java")):
            text = path.read_text(errors="replace")
            masked = _mask_comments(text)
            class_match = _CLASS_RE.search(masked)
            if not class_match:
                continue
            name = class_match.group(1)
            extends_raw = class_match.group(2)
            package_match = _PACKAGE_RE.search(masked)
            methods = _split_methods(text, masked, name)
            fields = _class_fields(masked, methods)
            element_fields = {f for f, t in fields.items() if t in _ELEMENT_TYPES}
            element_fields.update(_findby_field_names(masked))
            member_imports: dict[str, str] = {}
            star_imports: list[str] = []
            for imp in _STATIC_IMPORT_RE.finditer(masked):
                owner_simple = imp.group(1).rsplit(".", 1)[-1]
                if imp.group(2) == "*":
                    star_imports.append(owner_simple)
                else:
                    member_imports[imp.group(2)] = owner_simple
            classes[name] = _JavaClass(
                name=name,
                package=package_match.group(1) if package_match else "",
                path=path,
                rel=str(path.relative_to(root)),
                text=text,
                masked=masked,
                extends=_simple_type(extends_raw) if extends_raw else None,
                methods=methods,
                fields=fields,
                consts={
                    m.group(1): m.group(2)[1:-1]
                    for m in _STRING_CONST_RE.finditer(masked)
                },
                assigned_types=_assigned_new_types(masked),
                static_member_imports=member_imports,
                static_star_imports=star_imports,
                element_fields=element_fields,
            )
        return cls(classes)

    def ancestors(self, class_name: str) -> list[_JavaClass]:
        """The class followed by its ``extends`` chain (in-repo classes only)."""
        chain: list[_JavaClass] = []
        seen: set[str] = set()
        current: str | None = class_name
        while current and current not in seen:
            seen.add(current)
            java_class = self.classes.get(current)
            if java_class is None:
                break
            chain.append(java_class)
            current = java_class.extends
        return chain

    def lookup_method(
        self, class_name: str, method_name: str
    ) -> tuple[_JavaClass, _JavaMethod] | None:
        """Resolve a method against a class or anything it extends."""
        chain = self.ancestors(class_name)
        if method_name == "<init>":  # constructors are not inherited
            chain = chain[:1]
        for java_class in chain:
            method = java_class.methods.get(method_name)
            if method is not None:
                return java_class, method
        return None


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


def _split_methods(text: str, masked: str, class_name: str) -> dict[str, _JavaMethod]:
    """Method name → parsed method (signature through matching close brace).

    Constructors are stored under ``<init>``. Overloads merge into one entry
    (sources concatenated) so every body is available to resolution.
    """
    methods: dict[str, _JavaMethod] = {}

    def add(name: str, params_src: str, return_type: str, start: int, brace_at: int) -> None:
        # `else if (...) {` / `new Thread() {` would otherwise parse as methods.
        if name in _JAVA_KEYWORDS or return_type.strip() in ("new", "else"):
            return
        body_end = _matching_brace(masked, brace_at)
        if body_end is None:
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
        r'^[ \t]*(?:(?:public|protected|private)\s+)*'
        + re.escape(class_name)
        + r'\s*\(([^)]*)\)\s*\{',
        re.M,
    )
    for match in ctor_re.finditer(masked):
        add("<init>", match.group(1), class_name, match.start(), match.end() - 1)
    return methods


def _blank_method_spans(masked: str, methods: dict[str, _JavaMethod]) -> str:
    """The class body with method bodies blanked — the field/constant zone."""
    blanked = list(masked)
    for method in methods.values():
        for i in range(method.start, min(method.end, len(blanked))):
            if blanked[i] != "\n":
                blanked[i] = " "
    return "".join(blanked)


def _class_fields(masked: str, methods: dict[str, _JavaMethod]) -> dict[str, str]:
    """Field name → simple type, from the class body OUTSIDE method bodies."""
    zone = _blank_method_spans(masked, methods)
    fields: dict[str, str] = {}
    for match in _FIELD_DECL_RE.finditer(zone):
        type_name, name = match.group(1), match.group(2)
        if type_name not in ("Xray",) and name != type_name:
            fields[name] = type_name
    return fields


# --- locator extraction ----------------------------------------------------------


def _split_top_level_plus(expr: str) -> list[str]:
    """Split a Java expression on ``+`` outside strings/parens (concat folding)."""
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
        elif char == "+" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
        i += 1
    parts.append(current)
    return parts


def _resolve_string_expr(expr: str, owner: _JavaClass, index: JavaIndex) -> str | None:
    """Fold a locator argument to its literal value, or None if not static.

    Handles ``"literal"``, ``CONST`` (owner class + extends chain + static
    imports), ``Other.CONST`` and ``+`` concatenations of resolvable parts.
    """
    resolved: list[str] = []
    for part in _split_top_level_plus(expr):
        part = part.strip()
        literal = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', part)
        if literal:
            resolved.append(literal.group(1))
            continue
        if re.fullmatch(r"\w+", part):
            value = _lookup_const(part, owner, index)
            if value is None:
                return None
            resolved.append(value)
            continue
        qualified = re.fullmatch(r"(\w+)\.(\w+)", part)
        if qualified:
            other = index.classes.get(qualified.group(1))
            value = other.consts.get(qualified.group(2)) if other else None
            if value is None:
                return None
            resolved.append(value)
            continue
        return None
    return "".join(resolved)


def _lookup_const(name: str, owner: _JavaClass, index: JavaIndex) -> str | None:
    for java_class in index.ancestors(owner.name):
        if name in java_class.consts:
            return java_class.consts[name]
    imported_from = owner.static_member_imports.get(name)
    search = [imported_from] if imported_from else owner.static_star_imports
    for class_name in search:
        java_class = index.classes.get(class_name or "")
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
        value = _resolve_string_expr(expr, owner, index)
        if value is None:
            unresolved.append(
                f"{declared_in}: By.{by_method}({expr}) — value not statically resolvable"
            )
        else:
            locators.append(
                ExtractedLocator(
                    kind=_BY_KIND[by_method],
                    value=f'By.{by_method}("{value}")',
                    declared_in=declared_in,
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
        value = _resolve_string_expr(value_expr, java_class, index)
        if value is None:
            unresolved.append(
                f"{declared_in}: @FindBy {by_method}({value_expr}) — "
                "value not statically resolvable"
            )
        else:
            locators.append(
                ExtractedLocator(
                    kind=_BY_KIND[by_method],
                    value=f'By.{by_method}("{value}")',
                    declared_in=declared_in,
                )
            )
    return locators, unresolved


# --- test discovery + helper resolution ----------------------------------------


def extract_java_tests(
    root: Path,
    index: JavaIndex | None = None,
    *,
    helper_depth: int = DEFAULT_HELPER_DEPTH,
    helper_char_cap: int = DEFAULT_HELPER_CHAR_CAP,
) -> list[TestBundle]:
    """One bundle per ``@Xray``-annotated method found under ``root``."""
    index = index or JavaIndex.build(root)
    bundles: list[TestBundle] = []
    for java_class in index.classes.values():
        for method_name, method in java_class.methods.items():
            # The annotation sits in the decoration zone directly above the
            # signature — after the previous member's closing brace — so a key
            # mentioned inside another method's body can never be picked up.
            preamble = java_class.masked[max(0, method.start - 400) : method.start]
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
                code=f"// {java_class.rel}\n{method.source}",
            )
            _resolve_helpers(
                bundle, method, java_class, index, depth=helper_depth, char_cap=helper_char_cap
            )
            _collect_java_urls(bundle, method.masked)
            bundles.append(bundle)
    return bundles


class _Resolution:
    """Mutable state of one bundle's helper walk (dedup, budget, flags)."""

    def __init__(self, char_cap: int) -> None:
        self.included: set[tuple[str, str]] = set()
        self.visited_classes: set[str] = set()
        self.flags: list[str] = []
        self.flagged: set[str] = set()
        self.char_cap = char_cap
        self.used = 0

    def flag(self, text: str) -> None:
        if text not in self.flagged:
            self.flagged.add(text)
            self.flags.append(text)

    def fits(self, snippet: str) -> bool:
        if self.used + len(snippet) > self.char_cap:
            return False
        self.used += len(snippet)
        return True


def _resolve_helpers(
    bundle: TestBundle,
    test_method: _JavaMethod,
    test_class: _JavaClass,
    index: JavaIndex,
    depth: int = DEFAULT_HELPER_DEPTH,
    char_cap: int = DEFAULT_HELPER_CHAR_CAP,
) -> None:
    """Bounded resolution of the test's calls into repo helpers (plan §5.2).

    The snippet budget only limits helper TEXT handed to the Distiller —
    traversal, locator extraction and unresolved-flagging always run to the
    end, so a fat early helper can no longer starve later classes.
    """
    state = _Resolution(char_cap)
    queue: list[tuple[str, str, int]] = [(test_class.name, "*", depth)]
    queue.extend(_calls_of(test_method, test_class, index, depth, state))
    _method_locators(
        bundle, test_method, test_class, index, f"{test_class.name}#{bundle.test_name}"
    )

    while queue:
        class_name, method_name, remaining = queue.pop(0)
        java_class = index.classes.get(class_name)
        if java_class is None:
            state.flag(f"unresolved:{class_name}.{method_name}")
            continue
        if class_name not in state.visited_classes:
            state.visited_classes.add(class_name)
            _visit_class(bundle, java_class, index, state)
        if method_name == "*":
            continue
        resolved = index.lookup_method(class_name, method_name)
        if resolved is None:
            state.flag(f"unresolved:{class_name}.{method_name}")
            continue
        defining, method = resolved
        key = (defining.name, method_name)
        if key in state.included:
            continue
        state.included.add(key)
        if defining.name not in state.visited_classes:
            state.visited_classes.add(defining.name)
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
            queue.extend(_calls_of(method, defining, index, remaining - 1, state))

    flags = state.flags
    if len(flags) > _MAX_UNRESOLVED_FLAGS:
        overflow = len(flags) - _MAX_UNRESOLVED_FLAGS
        flags = flags[:_MAX_UNRESOLVED_FLAGS] + [f"unresolved:… and {overflow} more"]
    bundle.helper_refs.extend(flags)


def _visit_class(
    bundle: TestBundle, java_class: _JavaClass, index: JavaIndex, state: _Resolution
) -> None:
    """First visit of a class: harvest its locator fields (+ @FindBy)."""
    field_zone = _blank_method_spans(java_class.masked, java_class.methods)
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
        rendered = "\n".join(f"By {loc.declared_in.split('.')[-1]} = {loc.value};" for loc in added)
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
    for java_class in reversed(index.ancestors(owner.name)):
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
    depth: int,
    state: _Resolution,
) -> list[tuple[str, str, int]]:
    """(class, method, depth) targets this method calls, heuristically resolved.

    Every receiver we cannot type and every call we cannot place is FLAGGED via
    ``state`` — resolution gaps must be visible in the review output, never
    silent (the page-object layer lives behind exactly these calls).
    """
    masked = method.masked
    var_types = _var_types_for(method, owner, index)
    targets: list[tuple[str, str, int]] = []

    def element_field(name: str) -> bool:
        return any(name in c.element_fields for c in index.ancestors(owner.name))

    def push(class_name: str, method_name: str) -> str | None:
        """Queue a call target; returns its return type for chain-following."""
        targets.append((class_name, method_name, depth))
        resolved = index.lookup_method(class_name, method_name)
        return resolved[1].return_type if resolved else None

    def follow_chain(close_at: int, current_type: str | None) -> None:
        hops = 0
        position = close_at + 1
        while current_type and current_type in index.classes and hops < _CHAIN_HOP_LIMIT:
            nxt = _CHAIN_NEXT_RE.match(masked, position)
            if not nxt:
                return
            current_type = push(current_type, nxt.group(1))
            close = _matching_paren(masked, nxt.end() - 1)
            if close is None:
                return
            position = close + 1
            hops += 1

    # new X(...) — visit the class (locator fields), include its constructor,
    # and follow any fluent chain hanging off the expression.
    for match in _NEW_RE.finditer(masked):
        class_name = match.group(1)
        if class_name not in index.classes:
            continue
        targets.append((class_name, "*", depth))
        if "<init>" in index.classes[class_name].methods:
            targets.append((class_name, "<init>", depth))
        close = _matching_paren(masked, match.end() - 1)
        if close is not None:
            follow_chain(close, class_name)

    for match in _RECEIVER_CALL_RE.finditer(masked):
        receiver, method_name = match.group(1), match.group(2)
        close = _matching_paren(masked, match.end() - 1)
        if receiver in ("this", "super"):
            start_class = owner.extends if receiver == "super" else owner.name
            if start_class and index.lookup_method(start_class, method_name):
                return_type = push(start_class, method_name)
                if close is not None:
                    follow_chain(close, return_type)
            else:
                state.flag(f"unresolved:{owner.name}.{method_name}")
            continue
        if receiver[0].isupper():
            if receiver in index.classes:
                return_type = push(receiver, method_name)
                if close is not None:
                    follow_chain(close, return_type)
            elif receiver not in _KNOWN_RECEIVERS and receiver not in var_types:
                state.flag(f"unresolved:{receiver}.{method_name}")
            continue
        receiver_type = var_types.get(receiver)
        if receiver_type is not None:
            if receiver_type in index.classes:
                return_type = push(receiver_type, method_name)
                if close is not None:
                    follow_chain(close, return_type)
            # else: JDK/Selenium-typed receiver — framework action, silently fine
            continue
        if element_field(receiver):
            continue
        state.flag(f"unresolved:{receiver}.{method_name} (untyped receiver)")

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
        own = index.lookup_method(owner.name, name)
        if own is not None:
            return_type = push(own[0].name, name)
            close = _matching_paren(masked, match.end() - 1)
            if close is not None:
                follow_chain(close, return_type)
            continue
        imported_from = owner.static_member_imports.get(name)
        if imported_from is not None:
            if imported_from in index.classes:
                push(imported_from, name)
            # else: static import from outside the repo (JUnit asserts, …) — known-external
            continue
        star_owner = next(
            (c for c in owner.static_star_imports if index.lookup_method(c, name)), None
        )
        if star_owner is not None:
            push(star_owner, name)
            continue
        if owner.static_star_imports:
            continue  # plausibly an external static-star import (assertions, matchers)
        if name in var_types or element_field(name):
            continue  # a lambda/functional field, not a helper
        state.flag(f"unresolved:{name}() (no matching method found)")

    return targets


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
