from __future__ import annotations

from servant.api.bravesearch import BraveSearchApi
from servant.defs import SECRET_BRAVE_KEY, GlobalContext, ToolDef
from typed_json import JSON, JSONDict, coerce_int, coerce_str, require_obj

MODULE_PROMPT = """
## Web Search
Use `search_web` to search the web via Brave Search.
"""

_DEFAULT_NUM_RESULTS = 10
_MAX_NUM_RESULTS = 20


def _clamp_num_results(value: int | None) -> int:
    try:
        count = int(value) if value is not None else _DEFAULT_NUM_RESULTS
    except (TypeError, ValueError):
        count = _DEFAULT_NUM_RESULTS
    if count <= 0:
        return _DEFAULT_NUM_RESULTS
    return min(count, _MAX_NUM_RESULTS)


def _extract_results(data: JSONDict, limit: int) -> JSONDict:
    web = data.get("web", {})
    web_results: list[JSON] = []
    if isinstance(web, dict):
        raw_results = web.get("results", [])
        if isinstance(raw_results, list):
            web_results = raw_results
    web_results = web_results[:limit]

    news_results: list[JSON] = []
    raw_news = data.get("news", [])
    if isinstance(raw_news, list):
        news_results = raw_news[:limit]

    results: JSONDict = {"web": web_results}
    if news_results:
        results["news"] = news_results
    return results


async def search_web(
    ctx: GlobalContext,
    query: str,
    num_results: int = _DEFAULT_NUM_RESULTS,
) -> JSONDict:
    api_key_raw = ctx.secrets.get(SECRET_BRAVE_KEY)
    if not api_key_raw:
        return {"error": "Missing Brave Search API key. Set secret brave.key."}
    api_key = coerce_str(api_key_raw, field="brave.key", allow_empty=False)

    limit = _clamp_num_results(num_results)
    async with BraveSearchApi(api_key=api_key) as api:
        data = await api.search(query, num_results=limit)

    return {"query": query, "results": _extract_results(data, limit)}


async def _search_web_tool(ctx: GlobalContext, obj: JSON) -> JSONDict:
    data = require_obj(obj)
    query = coerce_str(data.get("query"), field="query", allow_empty=False)
    num_results = coerce_int(data.get("num_results"), _DEFAULT_NUM_RESULTS)
    return await search_web(ctx, query, num_results)


search_web_tool: ToolDef = ToolDef(
    name="search_web",
    schema={
        "name": "search_web",
        "description": "Search the web using Brave Search.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The query to search for.",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "default": _DEFAULT_NUM_RESULTS,
                },
            },
            "required": ["query"],
        },
    },
    function=_search_web_tool,
)
