from typing import Any, List, Dict, Callable, Awaitable, Optional
import typing
import dataclasses
from dataclasses import dataclass, field
from collections import defaultdict
import builtins

import re
import json
from textwrap import dedent
import time
import logging

from clj import SExpr, sexpr
from clj.exec import ExecutionContext, eval_sexpr, Quoted

import openai
from openai import AsyncOpenAI
import discord
import discord.utils
from pathlib import Path

from servant.defs import (
    Module,
    ToolDef,
    Personality,
    discover_modules,
    GlobalContext,
    SECRET_OPENAI_KEY,
    SECRET_USER_AGENT,
    SECRET_DISCORD_TOKEN,
    SECRET_IMGFLIP_USERNAME,
    SECRET_IMGFLIP_PASSWORD,
    SECRET_BRAVE_KEY,
)
from servant.json import obj_to_json, JSON, JSONDict, JSONArray

import sqlite3

_LOGGER = logging.getLogger(__name__ if __name__ != "__main__" else "jove")


EMPTY_PERSONALITY = Personality(
    name="Jove",
    description=dedent(
        """
        You are Jove, a helpful and friendly AI assistant.
        """
    ).strip(),
)


async def reply(discord_message, content):
    while True:
        content = content.strip()
        if len(content) == 0:
            break
        if len(content) <= 2000:
            await discord_message.channel.send(content)
            break
        else:
            line_break = content.rfind("\n", 0, 2000)
            if line_break == -1:
                space_break = content.rfind(" ", 0, 2000)
                if space_break == -1:
                    await discord_message.channel.send(content[:2000])
                    content = content[2000:]
                else:
                    await discord_message.channel.send(content[:space_break])
                    content = content[space_break + 1 :]
            else:
                await discord_message.channel.send(content[:line_break])
                content = content[line_break + 1 :]


async def handle_incoming_message(
    ctx: GlobalContext,
    client: discord.Client,
    discord_message: discord.Message,
    openai_client: AsyncOpenAI,
    debug_mode: bool = False,
):
    channel_id = str(discord_message.channel.id)

    personality = ctx.channel_personality.get(channel_id, EMPTY_PERSONALITY)
    personality_name = personality.name
    personality_name_short = personality_name[0]
    channel_personality = personality.description

    try:
        await discord_message.add_reaction("🤔")
    except Exception as e:
        _LOGGER.error(f"Failed to add reaction to message: {e}")
        pass

    # Add a typing indicator
    try:
        async with discord_message.channel.typing():
            system_prompt = dedent(
                """
                # Personality
                {{personality}}

                # Communication Medium
                The user messages will have the following format "Message from <user>: <content>".
                Messages are passed to and from the users through Discord, so you can use Discord syntax (Markdown + Discord's extensions, e.g. ||<text>|| for hidden text - good for joke punchlines) for formatting.
                Do not end your messages with a question unless it makes sense to do so in the context. You are chatting with people, not interrogating them.
                """
            ).replace("{{personality}}", channel_personality)

            assert (
                re.search(r"\{\{.*\}\}", system_prompt) is None
            ), "Unresolved template variable in system prompt."

            jeeves_messages = []
            jeeves_messages.append({"role": "system", "content": system_prompt})

            last_20_messages = ctx.channel_messages[channel_id][-20:]
            while last_20_messages and last_20_messages[0].get("role") == "tool":
                last_20_messages.pop(0)

            for message in last_20_messages:
                jeeves_messages.append(message)
            # jeeves_messages.append({ 'role': 'user', 'content': discord_message.content })

            tools = []
            for module in ctx.modules.values():
                for tool_name, tool_def in module.tools.items():
                    tools.append(
                        {
                            "type": "function",
                            "function": tool_def.schema,
                        }
                    )

            while True:
                try:
                    response = await openai_client.chat.completions.create(
                        model="gpt-4o",
                        messages=jeeves_messages,
                        max_tokens=4096,
                        tools=tools,
                    )
                except openai.APIError as e:
                    _LOGGER.error(f"OpenAI API Error: {e}")
                    return

                result = response.choices[0]
                jeeves_messages.append(result.message)

                _LOGGER.info(f"Jeeves response: {result}")

                finish_reason = result.finish_reason
                result_message = result.message

                if finish_reason == "stop":
                    content = result_message.content
                    if m := re.match(
                        rf"Message\s+from\s+({personality_name}|{personality_name_short})\s*:",
                        content,
                        re.IGNORECASE,
                    ):
                        content = content[m.end() :].strip()
                    result.message.content = content
                    await reply(discord_message, content)
                    ctx.channel_messages[channel_id].append(result.message)
                    break

                elif finish_reason == "tool_calls":
                    if "content" in result_message and result_message["content"]:
                        await reply(discord_message, result_message["content"])

                    tool_calls = result_message.tool_calls

                    tool_messages = []
                    tool_messages.append(
                        result.message
                    )  # extend conversation with tool calls

                    for tool_call in tool_calls:
                        tool_id = tool_call.id
                        tool_function = tool_call.function

                        tool_name = tool_function.name
                        tool_arguments = json.loads(tool_function.arguments)

                        _LOGGER.info(
                            f"Calling tool {tool_name} with arguments {tool_arguments}"
                        )

                        tool_def: ToolDef | None = None
                        for module in ctx.modules.values():
                            if tool_name in module.tools:
                                tool_def = module.tools[tool_name]

                        if tool_def is None:
                            _LOGGER.error(f"Tool {tool_name} not found in modules.")
                            result = {
                                "error": f"Tool {tool_name} not found in modules."
                            }

                        result = await tool_def.function(ctx, tool_arguments)

                        _LOGGER.info(f"Tool {tool_name} returned {result}")

                        msg = {
                            "tool_call_id": tool_id,
                            "role": "tool",
                            "name": tool_name,
                            "content": json.dumps(
                                obj_to_json(result), ensure_ascii=False
                            ),
                        }

                        jeeves_messages.append(msg)
                        tool_messages.append(msg)

                    ctx.channel_messages[channel_id].extend(tool_messages)
    finally:
        try:
            await discord_message.remove_reaction("🤔", client.user)
        except Exception as e:
            _LOGGER.error(f"Failed to remove reaction from message: {e}")
            pass


async def main():
    ctx = GlobalContext()

    ###########################################################################
    # Secret loading
    ###########################################################################

    from clj.types import SExpr
    from clj.parser import sexpr
    from clj.exec import ExecutionContext, eval_sexpr

    config_exec_ctx = ExecutionContext()

    def set_openai_key(exec_ctx: ExecutionContext, key: SExpr.Str) -> None:
        assert isinstance(key, SExpr.Str)
        ctx.secrets[SECRET_OPENAI_KEY] = key.value

    config_exec_ctx.register(set_openai_key, name="openai-key")

    def set_user_agent(exec_ctx: ExecutionContext, user_agent: SExpr.Str) -> None:
        assert isinstance(user_agent, SExpr.Str)
        ctx.secrets[SECRET_USER_AGENT] = user_agent.value

    config_exec_ctx.register(set_user_agent, name="user-agent")

    def set_discord_token(exec_ctx: ExecutionContext, discord_token: SExpr.Str) -> None:
        assert isinstance(discord_token, SExpr.Str)
        ctx.secrets[SECRET_DISCORD_TOKEN] = discord_token.value

    config_exec_ctx.register(set_discord_token, name="discord-token")

    def set_imgflip_credentials(
        exec_ctx: ExecutionContext, username: SExpr.Str, password: SExpr.Str
    ) -> None:
        assert isinstance(username, SExpr.Str)
        assert isinstance(password, SExpr.Str)
        ctx.secrets[SECRET_IMGFLIP_USERNAME] = username.value
        ctx.secrets[SECRET_IMGFLIP_PASSWORD] = password.value

    config_exec_ctx.register(set_imgflip_credentials, name="imgflip-credentials")

    def set_brave_key(exec_ctx: ExecutionContext, key: SExpr.Str) -> None:
        assert isinstance(key, SExpr.Str)
        ctx.secrets[SECRET_BRAVE_KEY] = key.value

    config_exec_ctx.register(set_brave_key, name="brave-key")

    eval_sexpr(config_exec_ctx, sexpr(open(".private.clj").read()))

    ###########################################################################
    # Setting up
    ###########################################################################

    openai_client = openai.AsyncOpenAI(api_key=ctx.secrets[SECRET_OPENAI_KEY])
    ctx.modules = discover_modules()
    ctx.openai_client = openai_client

    class MyClient(discord.Client):
        async def on_ready(self):
            _LOGGER.info(f"Logged on as {self.user}!")

        async def get_user_info(self, user_id):
            user = await self.fetch_user(user_id)
            return {
                "id": user.id,
                "name": user.name,
                "discriminator": user.discriminator,
            }

        async def on_message(self, discord_message):
            _LOGGER.info(
                f"Message from {discord_message.author}: {discord_message.content}"
            )

            if discord_message.author == self.user:
                return

            dm_content = discord_message.content

            # Decode <@USER_ID> mentions
            for user_id in re.findall(r"<@!?(\d+)>", dm_content):
                user_info = await self.fetch_user(int(user_id))
                dm_content = dm_content.replace(
                    f"<@{user_id}>", f"<@{user_id}:{user_info.name}>"
                )

            _LOGGER.info(f"Message from {discord_message.author}: {dm_content}")

            channel_id = str(discord_message.channel.id)
            ctx.channel_messages[channel_id].append(
                {
                    "role": "user",
                    "content": f"Message from {discord_message.author}: {dm_content}",
                }
            )

            # Check if message contains "\bJeeves\b" or "\bJ\b"

            msg = dm_content

            if msg.startswith("!EXIT"):
                await self.close()
                sys.exit(0)
                return

            if msg.startswith("!DEBUG "):
                msg = msg[len("!DEBUG ") :]
                debug_mode = True
            else:
                debug_mode = False

            channel_id = str(discord_message.channel.id)
            if channel_id not in ctx.channel_personality:
                ctx.channel_personality[channel_id] = EMPTY_PERSONALITY

            personality = ctx.channel_personality[channel_id]
            personality_name = personality.name
            personality_name_short = personality.name[0]

            if not re.search(
                rf"\b{personality_name}\b", msg, re.IGNORECASE
            ) and not re.search(rf"\b{personality_name_short}\b", msg, re.IGNORECASE):
                return

            await handle_incoming_message(
                ctx=ctx,
                client=client,
                discord_message=discord_message,
                openai_client=openai_client,
                debug_mode=debug_mode,
            )

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.guild_reactions = True
    intents.guilds = True
    intents.messages = True
    intents.reactions = True
    intents.guild_messages = True

    client = MyClient(intents=intents)

    discord.utils.setup_logging()

    await client.start(ctx.secrets[SECRET_DISCORD_TOKEN], reconnect=True)


if __name__ == "__main__":
    import sys, asyncio, os

    if sys.platform.lower() == "win32":
        os.system("color")
        os.system("chcp 65001 > nul")
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
