import asyncio

from hypothesis import given
from hypothesis import strategies as st

from servant.defs import GlobalContext
from servant.modules import bravesearch
from typed_json import JSON, JSONDict


@given(st.integers(min_value=-100, max_value=100))
def test_clamp_num_results_respects_bounds(value: int) -> None:
    result = bravesearch._clamp_num_results(value)
    if value <= 0:
        assert result == bravesearch._DEFAULT_NUM_RESULTS
    elif value > bravesearch._MAX_NUM_RESULTS:
        assert result == bravesearch._MAX_NUM_RESULTS
    else:
        assert result == value


def test_clamp_num_results_defaults_for_none() -> None:
    assert bravesearch._clamp_num_results(None) == bravesearch._DEFAULT_NUM_RESULTS


@given(
    st.lists(st.integers(), max_size=20),
    st.lists(st.integers(), max_size=20),
    st.integers(min_value=0, max_value=20),
)
def test_extract_results_truncates_lists(web_items: list[int], news_items: list[int], limit: int) -> None:
    web_results: list[JSON] = [item for item in web_items]
    news_results: list[JSON] = [item for item in news_items]
    data: JSONDict = {"web": {"results": web_results}, "news": news_results}
    results = bravesearch._extract_results(data, limit)

    assert results["web"] == web_items[:limit]
    if news_items[:limit]:
        assert results["news"] == news_items[:limit]
    else:
        assert "news" not in results


def test_search_web_missing_key_returns_error() -> None:
    ctx = GlobalContext()
    result = asyncio.run(bravesearch.search_web(ctx, "hi", num_results=5))
    assert "error" in result
