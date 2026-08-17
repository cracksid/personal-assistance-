"""
Tests for the internet tools.

No test here touches the real internet. httpx.MockTransport replaces the
network with a function that returns whatever page we want, and the search
backend is monkeypatched -- so these run offline, in milliseconds, and do
not fail because DuckDuckGo rate-limited the CI machine.

What is under test is our code: size limits, text extraction, untrusted
content labelling, and error translation.
"""

import httpx
import pytest

from app.config import settings
from app.tools.base import ToolContext, ToolError
from app.tools.web import UNTRUSTED_FOOTER, UNTRUSTED_HEADER, FetchUrl, WebSearch
from app.tools.web import FetchInput, SearchInput

ARTICLE = """
<html><head><title>Sounddevice vs PyAudio</title></head>
<body>
  <nav>Home | About | Contact | Subscribe now!</nav>
  <article>
    <h1>Choosing an audio library</h1>
    <p>On Windows, sounddevice installs cleanly while PyAudio often fails to
    build. For most projects sounddevice is the better default choice.</p>
  </article>
  <footer>Copyright 2026. Accept cookies?</footer>
</body></html>
"""


def context(db_session) -> ToolContext:
    return ToolContext(db=db_session)


def transport_returning(html: str, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, html=html)

    return httpx.MockTransport(handler)


# --- fetching --------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_extracts_the_article_and_drops_the_furniture(db_session):
    """
    trafilatura earns its place here: the model should receive the article,
    not the navigation, the cookie banner, or "Subscribe now!". Every one of
    those costs tokens and none of them answers anything.
    """
    tool = FetchUrl(transport=transport_returning(ARTICLE))

    result = await tool.run(FetchInput(url="https://example.com/audio"), context(db_session))

    assert result.ok
    assert "sounddevice installs cleanly" in result.output
    assert "Subscribe now!" not in result.output
    assert "Accept cookies?" not in result.output


@pytest.mark.anyio
async def test_fetched_content_is_labelled_as_untrusted(db_session):
    """
    The model is about to read text written by a stranger while holding
    tools that can delete files. The label is the nudge that it is data.

    Note what this test does NOT claim: that the label makes injection
    impossible. It does not -- the confirmation gate is the actual defence.
    This only asserts the labelling is present.
    """
    tool = FetchUrl(transport=transport_returning(ARTICLE))

    result = await tool.run(FetchInput(url="https://example.com/audio"), context(db_session))

    assert UNTRUSTED_HEADER in result.output
    assert UNTRUSTED_FOOTER in result.output
    assert "https://example.com/audio" in result.output


@pytest.mark.anyio
async def test_a_page_with_no_readable_text_says_so(db_session):
    tool = FetchUrl(transport=transport_returning("<html><body></body></html>"))

    result = await tool.run(FetchInput(url="https://example.com/empty"), context(db_session))

    assert result.ok is False
    assert "No readable text" in result.error


@pytest.mark.anyio
async def test_an_http_error_is_reported_not_raised(db_session):
    tool = FetchUrl(transport=transport_returning("nope", status=404))

    result = await tool.run(FetchInput(url="https://example.com/gone"), context(db_session))

    assert result.ok is False
    assert "Could not fetch" in result.error


@pytest.mark.anyio
async def test_extracted_text_is_capped(db_session, monkeypatch):
    """
    A long article can be tens of thousands of words, and the model reads
    every one of them at a cost.
    """
    monkeypatch.setattr(settings, "web_max_text_chars", 200)
    long_page = "<html><body><article>" + ("word " * 5000) + "</article></body></html>"
    tool = FetchUrl(transport=transport_returning(long_page))

    result = await tool.run(FetchInput(url="https://example.com/long"), context(db_session))

    assert "[truncated at 200 characters]" in result.output


@pytest.mark.anyio
async def test_download_stops_at_the_size_cap(db_session, monkeypatch):
    """
    The cap is enforced WHILE downloading, not by trusting Content-Length --
    that header can lie or be missing, so checking it afterwards would be no
    protection at all.
    """
    monkeypatch.setattr(settings, "web_max_download_bytes", 500)

    huge = "<html><body><article>" + ("x" * 100_000) + "</article></body></html>"
    tool = FetchUrl(transport=transport_returning(huge))

    result = await tool.run(FetchInput(url="https://example.com/huge"), context(db_session))

    # Either it truncated to something small, or extraction found nothing in
    # the fragment. Both are acceptable; downloading all 100KB is not.
    body = result.output or result.error or ""
    assert len(body) < 5_000


@pytest.mark.anyio
async def test_fetch_refuses_an_internal_url_before_any_request(db_session):
    """
    The URL guard runs first, so a blocked address never reaches the
    network layer at all.
    """

    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a request was made for a blocked URL")

    tool = FetchUrl(transport=httpx.MockTransport(explode))

    with pytest.raises(ToolError, match="inside this machine"):
        await tool.run(FetchInput(url="http://127.0.0.1:8000/tools"), context(db_session))


# --- redirects: the SSRF hole found in the Phase 19 review -----------------


def redirecting_to(location: str, then_html: str = "<html><p>secret</p></html>"):
    """A server that answers the first request with a 302, then a page."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200, html=then_html)

    return httpx.MockTransport(handler), seen


@pytest.mark.anyio
async def test_a_redirect_to_an_internal_address_is_refused(db_session):
    """
    THE regression test for a real hole, found by trying it rather than by
    reading the code.

    safe_url checked the URL it was given, and httpx -- with
    follow_redirects=True -- then went wherever it was told. A public page
    JARVIS was asked to read could answer 302 -> http://127.0.0.1:8000 and
    have JARVIS fetch its own API, the router's admin page, or cloud
    instance metadata. A guard that runs once, on the first URL, is not a
    guard.
    """
    transport, seen = redirecting_to("http://127.0.0.1:8000/admin")
    tool = FetchUrl(transport=transport)

    result = await tool.run(
        FetchInput(url="https://example.com/page"), context(db_session)
    )

    assert result.ok is False
    assert "inside this machine" in result.error
    # And the internal host was never contacted at all.
    assert all("127.0.0.1" not in url for url in seen)


@pytest.mark.anyio
async def test_a_redirect_to_the_local_network_is_refused(db_session):
    """192.168.x is the router, the NAS, the printer -- none of it public."""
    transport, seen = redirecting_to("http://192.168.1.1/")
    tool = FetchUrl(transport=transport)

    result = await tool.run(
        FetchInput(url="https://example.com/page"), context(db_session)
    )

    assert result.ok is False
    assert all("192.168" not in url for url in seen)


@pytest.mark.anyio
async def test_a_relative_redirect_is_resolved_before_being_checked(db_session):
    """
    A bare "/admin" is a legal Location and must be resolved against the
    current URL first -- otherwise it does not parse as a host and the
    guard would be checking nothing at all.
    """
    transport, seen = redirecting_to("/somewhere-else")
    tool = FetchUrl(transport=transport)

    result = await tool.run(
        FetchInput(url="https://example.com/page"), context(db_session)
    )

    assert result.ok  # same public host, so this one is allowed
    assert seen[1] == "https://example.com/somewhere-else"


@pytest.mark.anyio
async def test_the_output_names_where_the_content_really_came_from(db_session):
    """
    The provenance line used to name the URL that was ASKED for, not the one
    that answered. After a redirect that was simply untrue, and it is the
    line the model uses to judge how much to trust what follows.
    """
    transport, _ = redirecting_to(
        "https://www.example.com/real", then_html=ARTICLE
    )
    tool = FetchUrl(transport=transport)

    result = await tool.run(
        FetchInput(url="https://example.com/page"), context(db_session)
    )

    assert result.ok
    assert "Fetched from https://www.example.com/real" in result.output


@pytest.mark.anyio
async def test_a_redirect_loop_gives_up(db_session):
    """Otherwise two servers pointing at each other hang the turn."""

    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://example.com/again"})

    tool = FetchUrl(transport=httpx.MockTransport(loop))

    result = await tool.run(
        FetchInput(url="https://example.com/page"), context(db_session)
    )

    assert result.ok is False
    assert "redirects" in result.error


# --- searching -------------------------------------------------------------


@pytest.mark.anyio
async def test_search_formats_results_for_the_model(db_session, monkeypatch):
    monkeypatch.setattr(
        WebSearch,
        "_search",
        staticmethod(
            lambda query, limit: [
                {
                    "title": "Faster Whisper",
                    "href": "https://github.com/SYSTRAN/faster-whisper",
                    "body": "Reimplementation of Whisper using CTranslate2.",
                }
            ]
        ),
    )

    result = await WebSearch().run(SearchInput(query="faster whisper"), context(db_session))

    assert result.ok
    assert "Faster Whisper" in result.output
    assert "github.com/SYSTRAN" in result.output
    assert UNTRUSTED_HEADER in result.output  # results are strangers' text too


@pytest.mark.anyio
async def test_search_with_no_results_is_success_not_failure(db_session, monkeypatch):
    monkeypatch.setattr(WebSearch, "_search", staticmethod(lambda query, limit: []))

    result = await WebSearch().run(SearchInput(query="asdkjhasd"), context(db_session))

    assert result.ok
    assert "No results" in result.output


@pytest.mark.anyio
async def test_search_failure_explains_rate_limiting(db_session, monkeypatch):
    """
    DuckDuckGo is scraped rather than a supported API, so it rate limits and
    occasionally changes shape. The user should get a sentence, not a
    library traceback.
    """

    def boom(query, limit):
        raise RuntimeError("202 Ratelimit")

    monkeypatch.setattr(WebSearch, "_search", staticmethod(boom))

    result = await WebSearch().run(SearchInput(query="anything"), context(db_session))

    assert result.ok is False
    assert "rate limiting" in result.error


@pytest.mark.anyio
async def test_an_empty_query_is_refused(db_session):
    result = await WebSearch().run(SearchInput(query="   "), context(db_session))

    assert result.ok is False


@pytest.mark.anyio
async def test_result_count_is_clamped_to_something_sensible(db_session, monkeypatch):
    """A model asking for 1000 results would be expensive and useless."""
    seen: list[int] = []

    monkeypatch.setattr(
        WebSearch,
        "_search",
        staticmethod(lambda query, limit: (seen.append(limit), [])[1]),
    )

    await WebSearch().run(SearchInput(query="x", max_results=1000), context(db_session))

    assert seen == [10]


# --- registration ----------------------------------------------------------


def test_the_web_tools_are_registered_and_read_only():
    from app.tools import registry

    for name in ("fetch_url", "web_search"):
        tool = registry.get_tool(name)
        assert tool is not None, f"{name} should be registered"
        # Reading is not destructive, so neither asks for confirmation.
        assert tool.requires_confirmation is False
