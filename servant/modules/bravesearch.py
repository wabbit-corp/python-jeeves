from __future__ import annotations

from servant.api.bravesearch import BraveSearchApi
from servant.defs import GlobalContext, ToolDef, SECRET_BRAVE_KEY
from servant.json import JSONDict

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
    web_results = []
    if isinstance(web, dict):
        web_results = web.get("results", [])
    if isinstance(web_results, list):
        web_results = web_results[:limit]
    else:
        web_results = []

    news_results = data.get("news", [])
    if isinstance(news_results, list):
        news_results = news_results[:limit]
    else:
        news_results = []

    results: JSONDict = {"web": web_results}
    if news_results:
        results["news"] = news_results
    return results


async def search_web(
    ctx: GlobalContext,
    query: str,
    num_results: int = _DEFAULT_NUM_RESULTS,
) -> JSONDict:
    api_key = ctx.secrets.get(SECRET_BRAVE_KEY)
    if not api_key:
        return {"error": "Missing Brave Search API key. Set secret brave.key."}

    limit = _clamp_num_results(num_results)
    async with BraveSearchApi(api_key=api_key) as api:
        data = await api.search(query, num_results=limit)

    return {"query": query, "results": _extract_results(data, limit)}


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
    function=lambda ctx, obj: search_web(
        ctx,
        obj["query"],
        obj.get("num_results", _DEFAULT_NUM_RESULTS),
    ),
)
