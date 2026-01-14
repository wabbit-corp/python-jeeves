from __future__ import annotations

import json
import time

import Levenshtein
import requests

from servant.defs import (
    SECRET_IMGFLIP_PASSWORD,
    SECRET_IMGFLIP_USERNAME,
    SECRET_USER_AGENT,
    GlobalContext,
    ToolDef,
)
from typed_json import JSON, JSONDict, coerce_str, obj_to_json

all_memes: JSON = {}
all_memes_last_updated: float | None = None


def _memes_from_data(data: JSON) -> list[JSONDict]:
    if not isinstance(data, dict):
        return []
    payload = data.get("data")
    if not isinstance(payload, dict):
        return []
    memes = payload.get("memes")
    if not isinstance(memes, list):
        return []
    return [m for m in memes if isinstance(m, dict)]


def _require_secret(ctx: GlobalContext, key: str) -> str:
    return coerce_str(ctx.secrets.get(key), field=key, allow_empty=False)


def _load_memes_from_file() -> None:
    global all_memes
    try:
        with open("all_memes.json", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except Exception:
        return
    if isinstance(loaded, dict):
        all_memes = loaded


async def update_meme_templates(ctx: GlobalContext) -> None:
    global all_memes_last_updated, all_memes

    response = requests.get(
        "https://api.imgflip.com/get_memes",
        headers={"User-Agent": _require_secret(ctx, SECRET_USER_AGENT)},
    ).json()
    if not isinstance(response, dict):
        all_memes = {}
        return
    all_memes = response
    # Save it to a file
    try:
        with open("all_memes.json", "w", encoding="utf-8") as f:
            json.dump(all_memes, f)
    except Exception:
        pass

    all_memes_last_updated = time.time()


async def list_meme_templates(ctx: GlobalContext) -> JSONDict:
    global all_memes, all_memes_last_updated
    if not _memes_from_data(all_memes) or (
        all_memes_last_updated is None or (time.time() - all_memes_last_updated) > 3600
    ):
        await update_meme_templates(ctx)

    memes = _memes_from_data(all_memes)
    if not memes:
        _load_memes_from_file()
        memes = _memes_from_data(all_memes)
    top_meme_names = ", ".join(f"'{meme.get('name', '')}' ({meme.get('box_count', '')} boxes)" for meme in memes)
    return {
        "message": f"Total memes: {len(memes)}. Top memes: {top_meme_names}",
        "data": {
            "memes": obj_to_json(memes),
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


async def generate_meme(ctx: GlobalContext, name: str, box_text: list[str]) -> JSONDict:
    meme_id = None
    memes = _memes_from_data(all_memes)
    if not memes:
        await update_meme_templates(ctx)
        memes = _memes_from_data(all_memes)
    if not memes:
        _load_memes_from_file()
        memes = _memes_from_data(all_memes)
    if not memes:
        return {"error": "No meme templates available."}
    for meme in memes:
        meme_name = str(meme.get("name") or "")
        if meme_name.lower() == name.lower():
            raw_id = meme.get("id")
            meme_id = str(raw_id) if raw_id is not None else None
            break

    if meme_id is None:
        # Find the closest few matches
        matches = []

        for meme in memes:
            meme_name = str(meme.get("name") or "")
            matches.append((meme_name, Levenshtein.distance(name.lower(), meme_name.lower())))
        matches.sort(key=lambda x: x[1])
        return {"error": f'Meme template "{name}" not found. Closest matches: {[x[0] for x in matches[:10]]}'}

    data = {
        "template_id": meme_id,
        "username": _require_secret(ctx, SECRET_IMGFLIP_USERNAME),
        "password": _require_secret(ctx, SECRET_IMGFLIP_PASSWORD),
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

    headers = {"User-Agent": _require_secret(ctx, SECRET_USER_AGENT)}

    print(data)

    r = requests.post("https://api.imgflip.com/caption_image", data=data, headers=headers)

    rj = r.json()
    print(rj)
    return {"image": rj["data"]["url"]}


async def _generate_meme_tool(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")
    template_name = obj.get("template_name")
    box_text = obj.get("box_text", [])
    if not isinstance(template_name, str) or not template_name.strip():
        raise ValueError("template_name must be a non-empty string.")
    if not isinstance(box_text, list):
        raise ValueError("box_text must be a list of strings.")
    return await generate_meme(
        ctx,
        template_name.strip(),
        [str(item) for item in box_text],
    )


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
                    "description": "The name of the meme template on Imgflip.",
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
    function=_generate_meme_tool,
)
