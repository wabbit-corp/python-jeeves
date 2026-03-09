import asyncio

import pytest

from servant.defs import SECRET_USER_AGENT, GlobalContext
from servant.modules import url_fetch


def test_host_matches_pattern_supports_exact_and_wildcard() -> None:
    assert url_fetch._host_matches_pattern("api.coingecko.com", "*.coingecko.com")
    assert url_fetch._host_matches_pattern("coingecko.com", "*.coingecko.com")
    assert url_fetch._host_matches_pattern("api.coingecko.com", "api.coingecko.com")
    assert not url_fetch._host_matches_pattern("example.com", "*.coingecko.com")


def test_iter_blocklist_patterns_reads_ctx_config_list() -> None:
    ctx = GlobalContext(config={"url_fetch_blocked_hosts": ["api.example.com", "*.example.org"]})

    assert url_fetch._iter_blocklist_patterns(ctx) == ["api.example.com", "*.example.org"]


def test_validate_url_rejects_blocklisted_host() -> None:
    with pytest.raises(ValueError, match="blacklist"):
        asyncio.run(url_fetch._validate_url("https://example.com/data.json", blocklist=["example.com"]))


def test_get_user_agent_prefers_fetch_specific_config_and_ignores_legacy_web_user_agent() -> None:
    ctx = GlobalContext(
        config={
            SECRET_USER_AGENT: "legacy-bot-agent",
            "fetch_user_agent": "fetch-agent",
        }
    )
    fallback_ctx = GlobalContext(config={SECRET_USER_AGENT: "legacy-bot-agent"})

    assert url_fetch._get_user_agent(ctx) == "fetch-agent"
    assert url_fetch._get_user_agent(fallback_ctx) == url_fetch._DEFAULT_FETCH_USER_AGENT


def test_fetch_url_returns_inline_json(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = GlobalContext()

    async def _fake_resolve(*_args: object, **_kwargs: object) -> url_fetch._ResolvedUrl:
        return url_fetch._ResolvedUrl(
            url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            host="api.coingecko.com",
            status_code=200,
            content_type="application/json; charset=utf-8",
        )

    async def _fake_fetch(*_args: object, **_kwargs: object) -> url_fetch._FetchedContent:
        return url_fetch._FetchedContent(
            url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            content_kind="json",
            fetcher="aiohttp",
            text='{\n  "bitcoin": {\n    "usd": 123.45\n  }\n}',
            status_code=200,
            content_type="application/json; charset=utf-8",
            json_value={"bitcoin": {"usd": 123.45}},
        )

    monkeypatch.setattr(url_fetch, "_resolve_final_url", _fake_resolve)
    monkeypatch.setattr(url_fetch, "_fetch_via_aiohttp", _fake_fetch)

    result = asyncio.run(
        url_fetch.fetch_url(
            ctx,
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
        )
    )

    assert result["inline"] is True
    assert result["content_kind"] == "json"
    assert result["fetcher"] == "aiohttp"
    assert result["json"] == {"bitcoin": {"usd": 123.45}}


def test_fetch_url_uses_crawl4ai_for_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = GlobalContext()
    called = {"crawl": False}

    async def _fake_resolve(*_args: object, **_kwargs: object) -> url_fetch._ResolvedUrl:
        return url_fetch._ResolvedUrl(
            url="https://www.coingecko.com/en/coins/bitcoin",
            host="www.coingecko.com",
            status_code=200,
            content_type="text/html; charset=utf-8",
        )

    async def _fake_fetch(*_args: object, **_kwargs: object) -> url_fetch._FetchedContent:
        called["crawl"] = True
        return url_fetch._FetchedContent(
            url="https://www.coingecko.com/en/coins/bitcoin",
            content_kind="page",
            fetcher="crawl4ai",
            text="Bitcoin price page",
            status_code=200,
            content_type="text/html; charset=utf-8",
        )

    async def _fake_validate(url: str, *, blocklist: list[str]) -> tuple[str, str]:
        return url, "www.coingecko.com"

    monkeypatch.setattr(url_fetch, "_resolve_final_url", _fake_resolve)
    monkeypatch.setattr(url_fetch, "_fetch_via_crawl4ai", _fake_fetch)
    monkeypatch.setattr(url_fetch, "_validate_url", _fake_validate)

    result = asyncio.run(url_fetch.fetch_url(ctx, "https://www.coingecko.com/en/coins/bitcoin"))

    assert called["crawl"] is True
    assert result["inline"] is True
    assert result["fetcher"] == "crawl4ai"
    assert result["content_kind"] == "page"


def test_fetch_url_large_response_returns_handle_and_supports_read_and_grep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = GlobalContext()
    large_text = ("bitcoin usd 123\n" * 400).strip()

    async def _fake_resolve(*_args: object, **_kwargs: object) -> url_fetch._ResolvedUrl:
        return url_fetch._ResolvedUrl(
            url="https://api.coingecko.com/api/v3/large",
            host="api.coingecko.com",
            status_code=200,
            content_type="text/plain; charset=utf-8",
        )

    async def _fake_fetch(*_args: object, **_kwargs: object) -> url_fetch._FetchedContent:
        return url_fetch._FetchedContent(
            url="https://api.coingecko.com/api/v3/large",
            content_kind="text",
            fetcher="aiohttp",
            text=large_text,
            status_code=200,
            content_type="text/plain; charset=utf-8",
        )

    monkeypatch.setattr(url_fetch, "_resolve_final_url", _fake_resolve)
    monkeypatch.setattr(url_fetch, "_fetch_via_aiohttp", _fake_fetch)

    fetch_result = asyncio.run(url_fetch.fetch_url(ctx, "https://api.coingecko.com/api/v3/large"))

    assert fetch_result["inline"] is False
    handle = str(fetch_result["handle"])

    read_result = asyncio.run(url_fetch.fetch_url_handle_read(ctx, handle, start_line=1, end_line=3))
    grep_result = asyncio.run(url_fetch.fetch_url_handle_grep(ctx, handle, "bitcoin", max_matches=3))

    assert read_result["mode"] == "lines"
    assert read_result["lines"] == [
        {"no": 1, "text": "bitcoin usd 123"},
        {"no": 2, "text": "bitcoin usd 123"},
        {"no": 3, "text": "bitcoin usd 123"},
    ]
    assert grep_result["match_count"] == 3
    assert grep_result["matches"] == [
        {"line": 1, "start_char": 0, "end_char": 7, "text": "bitcoin usd 123"},
        {"line": 2, "start_char": 0, "end_char": 7, "text": "bitcoin usd 123"},
        {"line": 3, "start_char": 0, "end_char": 7, "text": "bitcoin usd 123"},
    ]


def test_fetch_url_handle_read_rejects_mixed_modes() -> None:
    ctx = GlobalContext()
    stored = url_fetch._store_handle(
        ctx,
        url_fetch._FetchedContent(
            url="https://api.coingecko.com/api/v3/simple/price",
            content_kind="text",
            fetcher="aiohttp",
            text="alpha\nbeta\n",
            status_code=200,
            content_type="text/plain",
        ),
    )

    with pytest.raises(ValueError, match="either line bounds or character bounds"):
        asyncio.run(url_fetch.fetch_url_handle_read(ctx, stored.handle, start_line=1, start_char=0))
