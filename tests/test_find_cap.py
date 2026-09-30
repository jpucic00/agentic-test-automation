"""Unit tests for the browser_find result cap (browser/find_cap.py)."""
from __future__ import annotations

import asyncio
from typing import Any

from ai_test_gen.browser.find_cap import SNIPPET_SEPARATOR, FindResultCap, cap_find_result

PATH = "- generic [active] [ref=e1]:\n  - main [ref=e8]:\n"


def _find_output(
    snippets: list[str], query: str = "Note-taking", matches: int | None = None
) -> str:
    """A browser_find result in Playwright MCP 0.0.82's format."""
    count = matches if matches is not None else len(snippets)
    return f'### Result\nFound {count} matches for "{query}":\n\n' + SNIPPET_SEPARATOR.join(
        snippets
    )


def _paragraph(n: int) -> str:
    return (
        f"{PATH}    - paragraph [ref=p{n}]:\n"
        f"      - text: Paragraph {n} is about note-taking and how people record information\n"
        f'      - link "source {n}" [ref=l{n}] [cursor=pointer]:\n'
        f"        - /url: https://example.org/{n}"
    )


def _url_hit(n: int) -> str:
    return (
        f'{PATH}    - link "Log in {n}" [ref=u{n}] [cursor=pointer]:\n'
        f"      - /url: /w/index.php?title=Special:UserLogin&returnto=Note-taking{n}"
    )


HEADING = f'{PATH}    - heading "Note-taking" [level=1] [ref=h1]\n    - generic [ref=h2]: intro'
SAVE_BUTTON = f'{PATH}    - button "Save note-taking settings" [ref=b1] [cursor=pointer]'


def _big_result() -> str:
    # Page order: URL-only hits, 60 paragraphs, then the button and (last) the exact heading.
    snippets = [_url_hit(n) for n in range(5)] + [_paragraph(n) for n in range(60)]
    return _find_output(snippets + [SAVE_BUTTON, HEADING])


def test_small_results_no_matches_and_errors_pass_through():
    small = _find_output([HEADING])
    none = '### Result\nNo matches found for "zzz".'
    error = '### Error\nProvide either "text" or "regex" to search for.'
    for text in (small, none, error, "x" * 10_000):
        assert cap_find_result(text, {"text": "Note-taking"}, 6000) == text


def test_cap_off_with_zero():
    big = _big_result()
    assert cap_find_result(big, {"text": "Note-taking"}, 0) == big


def test_big_result_fits_and_keeps_the_exact_match_in_full():
    big = _big_result()
    out = cap_find_result(big, {"text": "Note-taking"}, 3000)
    assert len(big) > 10_000 and len(out) <= 3000
    assert out.startswith('### Result\nFound 67 matches for "Note-taking":')
    # The exact-name heading ranks first although it is last in page order, and is shown whole.
    assert HEADING in out


def test_target_role_outranks_page_text_and_url_hits_rank_last():
    out = cap_find_result(_big_result(), {"text": "Note-taking"}, 3000)
    full, listed_text = out.split("Other matches:\n", 1)
    # The button snippet ranks right after the exact heading, ahead of 60 earlier paragraphs.
    assert SAVE_BUTTON in full
    assert _url_hit(0) not in full
    listed = listed_text.split("\n")
    # Text nodes carry no ref of their own, so they are shown with their container's.
    assert listed[0].startswith("- text: Paragraph ")
    assert listed[0].endswith(")") and "(inside paragraph [ref=p" in listed[0]
    url_lines = [i for i, line in enumerate(listed) if line.startswith('- link "Log in')]
    text_lines = [i for i, line in enumerate(listed) if line.startswith("- text:")]
    assert not url_lines or min(url_lines) > max(text_lines)


def test_overflow_is_counted_with_narrowing_tips():
    out = cap_find_result(_big_result(), {"text": "Note-taking"}, 3000)
    assert "more matching element(s) not listed" in out
    assert '/heading "Note-taking"/' in out


def test_everything_listed_when_the_one_line_tier_fits():
    snippets = [_paragraph(n) for n in range(20)] + [HEADING]
    out = cap_find_result(_find_output(snippets), {"text": "Note-taking"}, 4000)
    assert "not listed" not in out
    assert all(f"[ref=p{n}]" in out for n in range(20))


def test_url_hits_stand_for_the_link_that_owns_them():
    snippets = [_url_hit(n) for n in range(80)]
    out = cap_find_result(_find_output(snippets), {"text": "Note-taking"}, 3000)
    assert '- link "Log in 79" [ref=u79] [cursor=pointer]:' in out or "not listed" in out
    assert "- /url:" not in out.split("Other matches:\n", 1)[1]


def test_regex_queries_use_the_same_test_as_mcp():
    out = cap_find_result(_big_result(), {"regex": "/HEADING \"note-taking\"/i"}, 3000)
    assert HEADING in out


def test_later_response_sections_are_kept():
    tail = "\n### Page\n- Page URL: https://en.wikipedia.org/wiki/Note-taking"
    out = cap_find_result(_big_result() + tail, {"text": "Note-taking"}, 3000)
    assert out.endswith(tail)


def test_hook_caps_only_browser_find_and_wraps_the_inner_hook():
    big = _big_result()
    calls: list[str] = []
    ctx: Any = None  # the hook never reads the run context

    async def call_tool(name: str, args: dict[str, Any], metadata: Any = None) -> Any:
        del name, args, metadata
        return big

    async def call_tool_list(name: str, args: dict[str, Any], metadata: Any = None) -> Any:
        del name, args, metadata
        return [big]

    async def inner(ctx: Any, call: Any, name: str, args: dict[str, Any]) -> Any:
        del ctx
        calls.append(name)
        return await call(name, args)

    cap = FindResultCap(3000, inner=inner)
    found = asyncio.run(cap(ctx, call_tool, "browser_find", {"text": "Note-taking"}))
    other = asyncio.run(cap(ctx, call_tool, "browser_snapshot", {}))
    listed = asyncio.run(
        FindResultCap(3000)(ctx, call_tool_list, "browser_find", {"text": "Note-taking"})
    )
    assert calls == ["browser_find", "browser_snapshot"]
    assert isinstance(found, str) and len(found) <= 3000
    assert other == big
    assert isinstance(listed, list) and isinstance(listed[0], str) and len(listed[0]) <= 3000
