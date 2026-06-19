from __future__ import annotations

import json
from typing import Any, Dict, List

import aiohttp

from servant.defs import ToolDef, GlobalContext, SECRET_BRAVE_KEY, SECRET_USER_AGENT
from servant.json import JSONDict
from servant.rate_limiting import SimpleRateLimiter


MODULE_PROMPT = """
## Brave Search
Use `brave_search` to search the web via Brave's Search API.
"""


_RATE_LIMIT = SimpleRateLimiter(1.0)  # 1 request / second (be polite, unlike humans)


def _clamp_int(v: Any, *, lo: int, hi: int, default: int) -> int:
    try:
        v = int(v)
    except Exception:
        return default
    return max(lo, min(hi, v))


async def brave_search(
    ctx: GlobalContext,
    query: str,
    *,
    count: int = 5,
    offset: int = 0,
    safesearch: str = "moderate",
) -> JSONDict:
    api_key = ctx.secrets.get(SECRET_BRAVE_KEY)
    if not api_key:
        return {
            "error": "Brave API key not configured. Set .private.yml -> brave.key",
            "query": query,
        }

    query = (query or "").strip()
    if not query:
        return {"error": "Empty query.", "query": query}

    count = _clamp_int(count, lo=1, hi=20, default=5)
    offset = _clamp_int(offset, lo=0, hi=10_000, default=0)

    safesearch = (safesearch or "moderate").strip().lower()
    if safesearch not in {"off", "moderate", "strict"}:
        safesearch = "moderate"

    await _RATE_LIMIT()

    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
        "User-Agent": str(ctx.secrets.get(SECRET_USER_AGENT, "python-jeeves")),
    }
    params = {
        "q": query,
        "count": count,
        "offset": offset,
        "safesearch": safesearch,
        # keep responses compact
        "text_decorations": "false",
        "spellcheck": "true",
    }

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, params=params) as resp:
            # Brave sometimes returns useful JSON on errors, so read text first.
            body_text = await resp.text()
            if resp.status != 200:
                return {
                    "error": f"Brave API error (HTTP {resp.status})",
                    "query": query,
                    "details": body_text[:2000],
                }
            try:
                data = json.loads(body_text)
            except Exception:
                return {
                    "error": "Failed to parse Brave API response as JSON.",
                    "query": query,
                    "details": body_text[:2000],
                }

    web = data.get("web") or {}
    raw_results = web.get("results") or []

    results: List[Dict[str, Any]] = []
    for r in raw_results[:count]:
        results.append(
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "description": r.get("description") or r.get("snippet"),
            }
        )

    return {
        "query": query,
        "count": count,
        "offset": offset,
        "safesearch": safesearch,
        "results": results,
    }


brave_search_tool: ToolDef = ToolDef(
    name="brave_search",
    function=lambda ctx, o: brave_search(
        ctx,
        o.get("query", ""),
        count=o.get("count", 5),
        offset=o.get("offset", 0),
        safesearch=o.get("safesearch", "moderate"),
    ),
    schema={
        "name": "brave_search",
        "description": "Search the web using Brave Search.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "count": {
                    "type": "integer",
                    "description": "Number of results to return (1..20).",
                },
                "offset": {
                    "type": "integer",
                    "description": "Result offset for pagination (0+).",
                },
                "safesearch": {
                    "type": "string",
                    "description": "SafeSearch level.",
                    "enum": ["off", "moderate", "strict"],
                },
            },
            "required": ["query"],
        },
    },
)
