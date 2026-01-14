from __future__ import annotations

from urllib.parse import quote

import requests

from servant.defs import GlobalContext, ToolDef
from typed_json import JSON, JSONDict


async def get_wiki_summary(topic: str, sentences: int = 2) -> JSONDict:
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(topic)
    data: JSONDict = requests.get(url, timeout=5).json()
    extract = str(data.get("extract") or "")
    return {"summary": " ".join(extract.split(". ")[:sentences]).strip() + "."}


async def _get_wiki_summary_tool(_ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")
    topic = obj.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("topic must be a non-empty string.")
    return await get_wiki_summary(topic.strip())


get_wiki_summary_tool = ToolDef(
    name="get_wiki_summary",
    function=_get_wiki_summary_tool,
    schema={
        "name": "get_wiki_summary",
        "description": "Return a short Wikipedia summary of a topic.",
        "parameters": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
)
