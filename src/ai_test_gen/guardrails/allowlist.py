"""Navigation allow-list: the origins agents and generated tests may reach.

The allow-list is every ``STAGING_BASE_URL`` environment plus every ``STAGING_EXTRA_URLS``
host (an SSO login host, a mail-catcher UI), reduced to exact ``scheme://host[:port]``
origins. Two consumers enforce it:

- the MCP navigation guard (``guardrails/nav_guard.py``) refuses ``browser_navigate`` to an
  off-list origin and warns when a click/redirect lands the browser off-list;
- ``preflight_violations`` statically checks a spec's literal ``goto(...)`` targets and the
  plan's recorded URLs before Playwright starts (``test_runner.run_test``).

Only ``about:blank`` is allowed besides http(s) origins: it is the empty initial tab and makes
no request. ``data:``/``file:``/``javascript:`` and every other scheme are refused — no test
flow needs them, and a ``data:`` page can run arbitrary markup. Chromium's own error page
(``chrome-error://``, shown when an allowed host is unreachable) is not treated as leaving
the allow-list.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from ..core.models import TestPlan

BLANK_PAGE = "about:blank"
# Pages the browser shows on its own (no request to any host) — never an off-list landing.
_INERT_PAGE_PREFIXES = (BLANK_PAGE, "chrome-error:")
# A URL that starts with a scheme ("http:", "about:", "localhost:" …) — WHATWG-style.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
# The first argument of a goto(...) call when it is a string literal.
_GOTO_RE = re.compile(r"""\bgoto\s*\(\s*(?:'([^'\n]*)'|"([^"\n]*)"|`([^`]*)`)""")
# Characters that make a goto literal's source text differ from the URL the browser
# resolves: a backslash is a JS escape (\x68, \/) or a WHATWG path separator (/\host),
# and tab/CR/LF are silently removed by URL parsing. Either can turn a "relative" path
# into another host, so such a target is refused rather than guessed at.
_AMBIGUOUS_TARGET_CHARS = ("\\", "\t", "\r", "\n")


def url_origin(url: str) -> str | None:
    """Normalized ``scheme://host[:port]`` of an absolute http(s) URL, else ``None``.

    Default ports are dropped and the host is lowercased, so equal origins compare equal.
    URLs with a backslash, whitespace, or control characters are rejected outright: the
    browser and ``urllib`` disagree on where such an authority ends.
    """
    if not url or any(ch == "\\" or ord(ch) <= 0x20 for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in ("http", "https") or not host:
        return None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    default_port = 80 if scheme == "http" else 443
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{host}{suffix}"


def origins(urls: Iterable[str]) -> tuple[str, ...]:
    """The distinct origins of ``urls``, in order; entries without one are skipped."""
    seen: dict[str, None] = {}
    for url in urls:
        origin = url_origin(url)
        if origin is not None:
            seen.setdefault(origin, None)
    return tuple(seen)


def relative_to(url: str, app_origins: Sequence[str]) -> str:
    """``url`` as a baseURL-relative path when it is on one of ``app_origins``, else unchanged.

    ``https://staging.example.com/notes?x=1`` → ``/notes?x=1``: the Generator then emits
    ``page.goto('/notes?x=1')``, which follows whichever environment the run's baseURL names.
    """
    if url_origin(url) not in app_origins:
        return url
    parts = urlsplit(url)
    path = parts.path or "/"
    return path + (f"?{parts.query}" if parts.query else "") + (
        f"#{parts.fragment}" if parts.fragment else ""
    )


def _as_browser_url(raw: str) -> str:
    """Mirror Playwright MCP's ``checkUrlAndNavigate``: a scheme-less URL gets one prepended."""
    url = raw.strip()
    if _SCHEME_RE.match(url):
        return url
    return ("http://" if url.startswith("localhost") else "https://") + url


def navigation_refusal(raw_url: str, allowed: Sequence[str]) -> str | None:
    """Why navigating to ``raw_url`` is refused, or ``None`` when it is allowed."""
    url = _as_browser_url(raw_url)
    if url == BLANK_PAGE:
        return None
    origin = url_origin(url)
    if origin is None:
        return f"{raw_url!r} is not an absolute http(s) URL"
    if origin not in allowed:
        return f"{origin} is not an allowed host"
    return None


_PAGE_URL_LINE_RE = re.compile(r"^- Page URL: (\S+)", re.MULTILINE)


def offlist_page_url(tool_text: str, allowed: Sequence[str]) -> str | None:
    """The current page URL a Playwright MCP result reports, when it is off the allow-list."""
    match = _PAGE_URL_LINE_RE.search(tool_text)
    if match is None:
        return None
    url = match.group(1)
    if url.startswith(_INERT_PAGE_PREFIXES):
        return None
    return None if url_origin(url) in allowed else url


def spec_goto_targets(code: str) -> list[str]:
    """Literal first arguments of every ``goto(...)`` call in ``code``.

    A template literal contributes only its static prefix (up to the first ``${``); a goto
    whose target is a variable cannot be resolved statically and is not returned.
    """
    targets: list[str] = []
    for match in _GOTO_RE.finditer(code):
        single, double, template = match.groups()
        value = single if single is not None else double
        if value is None:
            value = (template or "").split("${", 1)[0]
        if value.strip():
            targets.append(value.strip())
    return targets


def preflight_violations(
    code: str,
    plan: TestPlan | None,
    *,
    base_url: str,
    env_origins: Sequence[str],
    extra_origins: Sequence[str],
) -> list[str]:
    """Absolute navigation targets that must stop a run before Playwright starts.

    Spec ``goto`` literals are checked against THIS run's origin plus the extra hosts: a spec
    that hard-codes another configured environment would silently test that environment
    instead of ``base_url``. Relative targets resolve against ``base_url`` and always pass.
    The plan's ``target_url`` / ``page_url`` values were recorded on the primary environment,
    so they are checked against the full allow-list (every environment + extra hosts).
    """
    base_origin = url_origin(base_url)
    base_scheme = urlsplit(base_url).scheme or "https"
    run_allowed = {*extra_origins, *([base_origin] if base_origin else [])}
    violations: list[str] = []
    for target in spec_goto_targets(code):
        if any(ch in target for ch in _AMBIGUOUS_TARGET_CHARS):
            violations.append(
                f"spec goto({target!r}) contains a backslash or control character, so the "
                "browser may resolve it to a different host — write a plain URL or path"
            )
            continue
        url = f"{base_scheme}:{target}" if target.startswith("//") else target
        if not _SCHEME_RE.match(url) or url == BLANK_PAGE:
            continue  # baseURL-relative
        origin = url_origin(url)
        if origin is None:
            violations.append(f"spec goto({target!r}) is not an http(s) URL")
        elif origin in env_origins and origin not in run_allowed:
            violations.append(
                f"spec goto({target!r}) hard-codes {origin}, another configured environment — "
                "use a baseURL-relative path (e.g. page.goto('/')) so the spec follows BASE_URL"
            )
        elif origin not in run_allowed:
            violations.append(
                f"spec goto({target!r}) targets {origin}, which is not an allowed host"
            )
    if plan is not None:
        all_allowed = {*env_origins, *extra_origins}
        recorded = [("target_url", plan.target_url)] + [
            (f"steps[{i}].page_url", step.page_url)
            for i, step in enumerate(plan.steps)
            if step.page_url
        ]
        for field, value in recorded:
            url = f"{base_scheme}:{value}" if value.startswith("//") else value
            origin = url_origin(url)
            if origin is not None and origin not in all_allowed:
                violations.append(
                    f"plan {field} {value!r} is on {origin}, which is not an allowed host"
                )
    return violations
