from __future__ import annotations
from typing import List
from servant.defs import (
    ToolDef,
    GlobalContext,
    SECRET_USER_AGENT,
    SECRET_IMGFLIP_PASSWORD,
    SECRET_IMGFLIP_USERNAME,
)
from servant.json import JSONDict
import requests
import json
import time
import Levenshtein

all_memes = []
all_memes_last_updated = None


async def update_meme_templates(ctx: GlobalContext) -> None:
    global all_memes_last_updated, all_memes

    all_memes = requests.get(
        "https://api.imgflip.com/get_memes",
        headers={"User-Agent": ctx.secrets[SECRET_USER_AGENT]},
    ).json()
    # Save it to a file
    with open("all_memes.json", "wt") as f:
        json.dump(all_memes, f)

    all_memes_last_updated = time.time()
    all_memes = all_memes


async def list_meme_templates(ctx: GlobalContext) -> JSONDict:
    global all_memes, all_memes_last_updated
    if not all_memes or (
        all_memes_last_updated is None
        or (
            all_memes_last_updated
            - requests.utils.parse_http_date(requests.utils.http_date())
        )
        > 3600
    ):
        await update_meme_templates(ctx)

    if not all_memes:
        with open("all_memes.json", "rt") as f:
            all_memes = json.load(f)
    top_meme_names = [meme["name"] for meme in all_memes["data"]["memes"]]
    top_meme_names = ", ".join(
        f"'{meme['name']}' ({meme['box_count']} boxes)"
        for meme in all_memes["data"]["memes"]
    )
    return {
        "message": f"Total memes: {len(all_memes['data']['memes'])}. Top memes: {top_meme_names}",
        "data": {
            "memes": all_memes["data"]["memes"],
            "top_meme_names": top_meme_names,
        },
    }


list_meme_templates_tool: ToolDef = ToolDef(
    name="list_meme_templates",
    schema={
        "name": "list_meme_templates",
        "description": "List all available meme templates on Imgflip.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    function=lambda ctx, obj: list_meme_templates(ctx),
)


async def generate_meme(ctx: GlobalContext, name: str, box_text: List[str]) -> JSONDict:
    meme_id = None
    for meme in all_memes["data"]["memes"]:
        if meme["name"].lower() == name.lower():
            meme_id = meme["id"]
            break

    if meme_id is None:
        # Find the closest few matches
        matches = []

        for meme in all_memes["data"]["memes"]:
            matches.append(
                (meme["name"], Levenshtein.distance(name.lower(), meme["name"].lower()))
            )
        matches.sort(key=lambda x: x[1])
        return {
            "error": f'Meme template "{name}" not found. Closest matches: {[x[0] for x in matches[:10]]}'
        }

    data = {
        "template_id": meme_id,
        "username": ctx.secrets[SECRET_IMGFLIP_USERNAME],
        "password": ctx.secrets[SECRET_IMGFLIP_PASSWORD],
    }

    if len(box_text) > 0:
        data["text0"] = box_text[0]
    if len(box_text) > 1:
        data["text1"] = box_text[1]
    if len(box_text) > 2:
        data["text2"] = box_text[2]
    if len(box_text) > 3:
        data["text3"] = box_text[3]

    for i, text in enumerate(box_text):
        data[f"boxes[{i}][text]"] = text

    headers = {"User-Agent": ctx.secrets[SECRET_USER_AGENT]}

    print(data)

    r = requests.post(
        "https://api.imgflip.com/caption_image", data=data, headers=headers
    )

    rj = r.json()
    print(rj)
    return {"image": rj["data"]["url"]}


generate_meme_tool: ToolDef = ToolDef(
    name="generate_meme",
    schema={
        "name": "generate_meme",
        "description": "Generate a meme given a template and text",
        "parameters": {
            "type": "object",
            "properties": {
                "template_name": {
                    "type": "string",
                    "description": f"The name of the meme template on Imgflip.",
                },
                "box_text": {
                    "type": "array",
                    "description": "The text to put in each box of the meme.",
                    "items": {"type": "string"},
                },
            },
            "required": ["template_id", "text0", "text1"],
        },
    },
    function=lambda ctx, obj: generate_meme(ctx, obj["template_name"], obj["box_text"]),
)
