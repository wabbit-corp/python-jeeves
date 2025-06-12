from __future__ import annotations
from servant.defs import ToolDef, JSONDict
import requests


async def get_wiki_summary(topic: str, sentences: int = 2) -> JSONDict:
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + requests.utils.quote(
        topic
    )
    data = requests.get(url, timeout=5).json()
    return {"summary": " ".join(data["extract"].split(". ")[:sentences]) + "."}


get_wiki_summary_tool = ToolDef(
    name="get_wiki_summary",
    function=lambda _ctx, o: get_wiki_summary(o["topic"]),
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
