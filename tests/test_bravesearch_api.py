import asyncio

import aiohttp
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from servant.api import bravesearch
from typed_json import JSON, JSONDict

_ASCII_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789_-"

_JSON_KEYS = st.one_of(
    st.sampled_from(sorted(bravesearch._DROP_KEYS)),
    st.text(alphabet=_ASCII_ALPHABET, min_size=1, max_size=12),
)
_JSON_VALUES = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(alphabet=_ASCII_ALPHABET, max_size=20),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(_JSON_KEYS, children, max_size=5),
    ),
    max_leaves=10,
)


class _BadInt(int):
    def __int__(self) -> int:
        raise ValueError("bad int")


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.raise_for_status_calls = 0

    def raise_for_status(self) -> None:
        self.raise_for_status_calls += 1

    async def json(self) -> object:
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.base_url: str | None = None
        self.closed = False
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.last_params: dict[str, str | int] | None = None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str | int],
    ) -> _FakeResponse:
        self.last_url = url
        self.last_headers = dict(headers)
        self.last_params = dict(params)
        return self._response

    async def close(self) -> None:
        self.closed = True


def _patch_client_session(patch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    def _client_session(*, base_url: str) -> _FakeSession:
        session.base_url = base_url
        return session

    patch.setattr(aiohttp, "ClientSession", _client_session)


def _assert_stripped_matches(original: JSON, stripped: JSON) -> None:
    if isinstance(original, dict):
        assert isinstance(stripped, dict)
        for key, value in original.items():
            if key in bravesearch._DROP_KEYS:
                assert key not in stripped
            else:
                assert key in stripped
                _assert_stripped_matches(value, stripped[key])
        for key in stripped:
            assert key in original
            assert key not in bravesearch._DROP_KEYS
        return
    if isinstance(original, list):
        assert isinstance(stripped, list)
        assert len(stripped) == len(original)
        for index, value in enumerate(original):
            _assert_stripped_matches(value, stripped[index])
        return
    assert stripped == original


async def _run_search(
    api: bravesearch.BraveSearchApi,
    query: str,
    num_results: int,
) -> JSONDict:
    async with api:
        return await api.search(query, num_results=num_results)


def _setup_api(
    payload: object,
    *,
    api_key: str = "token",
    base_url: str = "https://example.test",
    qps: float | None = None,
) -> tuple[pytest.MonkeyPatch, _FakeSession, bravesearch.BraveSearchApi]:
    response = _FakeResponse(payload)
    session = _FakeSession(response)
    patch = pytest.MonkeyPatch()
    _patch_client_session(patch, session)
    api = bravesearch.BraveSearchApi(api_key=api_key, base_url=base_url, qps=qps)
    return patch, session, api


@given(_JSON_VALUES)
@example({"img": "drop", "keep": "ok"})
@example([{"favicon": "drop"}, 2])
@example("plain")
def test_strip_keys_removes_drop_keys(value: JSON) -> None:
    stripped = bravesearch._strip_keys(value)
    _assert_stripped_matches(value, stripped)


def test_search_requires_api_key() -> None:
    api = bravesearch.BraveSearchApi(api_key=None)
    with pytest.raises(ValueError):
        asyncio.run(api.search("query"))


def test_search_requires_session() -> None:
    api = bravesearch.BraveSearchApi(api_key="token")
    with pytest.raises(RuntimeError):
        asyncio.run(api.search("query"))


def test_aexit_without_session_is_noop() -> None:
    api = bravesearch.BraveSearchApi(api_key="token")
    asyncio.run(api.__aexit__(None, None, None))


def test_search_strips_response_and_sets_count() -> None:
    payload: JSONDict = {
        "query": "drop",
        "mixed": {"unused": True},
        "web": {"results": [{"title": "Web", "img": "drop", "url": "https://example.com"}]},
        "news": {"results": [{"title": "News", "thumbnail": "drop", "url": "https://news.example.com"}]},
        "other": {"favicon": "drop", "ok": "yes"},
    }
    patch, session, api = _setup_api(payload, qps=5.0)
    try:
        result = asyncio.run(_run_search(api, "query", num_results=2))
    finally:
        patch.undo()

    assert session.base_url == "https://example.test"
    assert session.last_url == "/res/v1/web/search"
    assert session.last_headers == {"X-Subscription-Token": "token", "Accept": "application/json"}
    assert session.last_params == {"q": "query", "count": 2}
    assert session.closed is True
    assert session._response.raise_for_status_calls == 1
    assert "query" not in result
    assert "mixed" not in result
    assert result["web"] == {"results": [{"title": "Web", "url": "https://example.com"}]}
    assert result["news"] == [{"title": "News", "url": "https://news.example.com"}]
    assert result["other"] == {"ok": "yes"}


def test_search_keeps_news_list_and_omits_count_for_zero() -> None:
    payload: JSONDict = {"web": {"results": []}, "news": [{"title": "Existing"}]}
    patch, session, api = _setup_api(payload)
    try:
        result = asyncio.run(_run_search(api, "query", num_results=0))
    finally:
        patch.undo()

    assert session.last_params == {"q": "query"}
    assert result["news"] == [{"title": "Existing"}]


def test_search_omits_count_for_bad_int() -> None:
    payload: JSONDict = {"web": {"results": []}}
    patch, session, api = _setup_api(payload)
    bad_value: int = _BadInt(5)
    try:
        result = asyncio.run(_run_search(api, "query", num_results=bad_value))
    finally:
        patch.undo()

    assert session.last_params == {"q": "query"}
    assert result["web"] == {"results": []}


def test_search_raises_for_non_object_response() -> None:
    patch, session, api = _setup_api(["not", "an", "object"])
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(_run_search(api, "query", num_results=1))
    finally:
        patch.undo()

    assert session.closed is True
