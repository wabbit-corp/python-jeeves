from servant.defs import GlobalContext, ToolDef
from typed_json import JSON, JSONDict


async def generate_image(ctx: GlobalContext, prompt: str) -> JSONDict:
    import openai

    if ctx.openai_client is None:
        raise RuntimeError("OpenAI client not initialized.")

    try:
        r = await ctx.openai_client.images.generate(
            prompt=prompt,
            size="1024x1024",
            model="dall-e-3",
            response_format="url",
            n=1,
        )
    except openai.APIError as e:
        return {"error": str(e)}
    print(r.json)
    if not r.data:
        return {"error": "No image data returned."}
    image = r.data[0]
    return {"image": image.url, "revised_prompt": image.revised_prompt}


async def _generate_image_tool(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")
    prompt = obj.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string.")
    return await generate_image(ctx, prompt.strip())


generate_image_tool: ToolDef = ToolDef(
    name="generate_image",
    schema={
        "name": "generate_image",
        "description": "Generate an image given a prompt.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "A prompt to generate an image from.",
                },
            },
            "required": ["prompt"],
        },
    },
    function=_generate_image_tool,
)
