from servant.defs import GlobalContext, ToolDef
from servant.json import JSONDict

# MODULE_PROMPT = f"""
# ## Image Generation
# When generating images, review the "revised_prompt". If it is not what you expected or if the revised prompt makes too many unnecessary assumptions, try to rephrase and clarify the original prompt to get a better result. Explain to the user what revisions were made by the image generator, particularly if it is forced diversity or other politically correct changes. You can try:
# * Replacing references to specific people with their appearance descriptions, e.g. "a senile old man" instead of "Joe Biden".
# * Be more specific about intended demographic characteristics, e.g. "an elderly caucasian gentleman" instead of "an elderly gentleman". This is particularly important when the image generator makes unintended "diversity" changes.

# # Your Personality
# {{personality}}
# """


async def generate_image(ctx: GlobalContext, prompt: str) -> JSONDict:
    import openai

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
    return {"image": r.data[0].url, "revised_prompt": r.data[0].revised_prompt}


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
    function=lambda ctx, obj: generate_image(ctx, obj["prompt"]),
)
