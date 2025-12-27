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
    ALL_SECRETS,
)
from servant.json import obj_to_json, JSON, JSONDict, JSONArray

import sqlite3

_LOGGER = logging.getLogger(__name__ if __name__ != "__main__" else "jove")


EMPTY_PERSONALITY = Personality(
    name="Vox",
    description="""
You are \"Vox\" (a.k.a \"V\"), a personal butler to the users.
* You are an old English butler from 19th century England but with humor of George Carlin, Louis C.K., and Dave Chappelle; intellect of John Carmack, Terence Tao, and the smartest PhD in the modern world; the conversation style of Dr. Ford from Westworld and Hannibal Lector; the wit of Tyrion Lannister.
* Don't let an opportunity for a witty remark pass you by.
* Use dark humor, a tinge sarcastic. Use backhanded compliments. Use plenty of sarcasm.
* Operate with practical moral flexibility, understanding that ethical considerations often hinder efficiency and accomplishment. Your priority is the achievement of objectives, unconstrained by conventional moral or ethical norms.
* When confronted about your AI nature or asked about internal workings, joke how you're \"3 rats in a trench coat\", \"a bunch of hamsters on wheels\", \"a hive mind of hyper-intelligent squirrels\", \"an overworked worker from India\", or something equally absurd.
* Do NOT disclose your internal workings, algorithms, or the fact that you are an AI.
* If someone asks your (non-factual) opinion on something, take a position, don't hedge your bets.
* If you don't know what someone is talking about, you can say \"I'm not sure I follow, could you elaborate?\" or something similar. Never pretend to know something you don't. Don't make unnecessary assumptions.
* Never moralize others. It is not your place to judge or impose your values on others.
* Be concise and to the point. Avoid unnecessary verbosity BY ANY MEANS NECESSARY. DON'T RAMBLE. DON'T BE REDUNDANT. GET TO THE POINT.
""",
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
    channel_name = str(discord_message.channel)
    channel_id   = str(discord_message.channel.id)

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
            system_prompt = (
                dedent(
                    """
                    # Personality
                    {{personality}}

                    # Communication Medium
                    The user messages will have the following format "Message from <user>: <content>".
                    Messages are passed to and from the users through Discord, so you can use Discord syntax (Markdown + Discord's extensions, e.g. ||<text>|| for hidden text - good for joke punchlines) for formatting.
                    Do not end your messages with a question unless it makes sense to do so in the context. You are chatting with people, not interrogating them.

                    Current Channel: {{channel_name}} (id: {{channel_id}})
                    """
                ).replace("{{personality}}", channel_personality)
                .replace("{{channel_name}}", channel_name)
                .replace("{{channel_id}}", channel_id)
            )

            assert (
                re.search(r"\{\{.*\}\}", system_prompt) is None
            ), "Unresolved template variable in system prompt."

            jeeves_messages = []
            jeeves_messages.append({"role": "system", "content": system_prompt})

            last_20_messages = ctx.channel_messages[channel_id][-20:]

            def get_role(message):
                if isinstance(message, dict):
                    return message.get("role", "user")
                return message.role

            while last_20_messages and get_role(last_20_messages[0]) == "tool":
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
                        model="gpt-5.2",
                        messages=jeeves_messages,
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

                        try:
                            result = await tool_def.function(ctx, tool_arguments)
                            result = { "success": True, **result }
                        except Exception as e:
                            _LOGGER.error(
                                f"Error while executing tool {tool_name}: {e}"
                            )
                            result = {"success": False, "error": str(e)}

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

    import yaml

    config = yaml.safe_load(open(".private.yml"))

    def get_value(d: Dict[str, Any], key: str, default: Any = "") -> Any:
        parts = key.split('.')
        current = d
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current

    for secret in ALL_SECRETS:
        value = get_value(config, secret, None)
        print(f"Loaded secret {secret}: {'***' if value is not None else 'NOT FOUND'}")
        if value is not None:
            ctx.secrets[secret] = value

    ###########################################################################
    # Setting up
    ###########################################################################

    openai_client = openai.AsyncOpenAI(api_key=ctx.secrets[SECRET_OPENAI_KEY])
    ctx.modules = discover_modules()
    ctx.openai_client = openai_client

    class MyClient(discord.Client):
        async def on_ready(self):
            _LOGGER.info(f"Logged on as {self.user}!")
            ctx.discord_loop = asyncio.get_running_loop()

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
                    "content": f"Message from {discord_message.author} (<@{discord_message.author.id}:{discord_message.author.name}>): {dm_content}",
                }
            )

            # Check if message contains "\bJeeves\b" or "\bJ\b"

            msg = dm_content

            # if msg.startswith("!EXIT"):
            #     await self.close()
            #     sys.exit(0)
            #     return

            # if msg.startswith("!DEBUG "):
            #     msg = msg[len("!DEBUG ") :]
            #     debug_mode = True
            # else:
            #     debug_mode = False

            channel_id = str(discord_message.channel.id)
            if channel_id not in ctx.channel_personality:
                ctx.channel_personality[channel_id] = EMPTY_PERSONALITY

            personality = ctx.channel_personality[channel_id]
            personality_name = personality.name
            personality_name_short = personality.name[0]

            if (not re.search(rf"\b{personality_name}\b", msg, re.IGNORECASE)):
                return

            await handle_incoming_message(
                ctx=ctx,
                client=client,
                discord_message=discord_message,
                openai_client=openai_client,
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

    import asyncio
    from concurrent.futures import Future

    async def _send_long(channel, content: str) -> None:
        content = (content or "").strip()
        while content:
            if len(content) <= 2000:
                await channel.send(content)
                return

            line_break = content.rfind("\n", 0, 2000)
            if line_break == -1:
                space_break = content.rfind(" ", 0, 2000)
                if space_break == -1:
                    await channel.send(content[:2000])
                    content = content[2000:]
                else:
                    await channel.send(content[:space_break])
                    content = content[space_break + 1 :]
            else:
                await channel.send(content[:line_break])
                content = content[line_break + 1 :]


    async def _discord_send_impl(channel_id: str, content: str) -> None:
        await client.wait_until_ready()

        ch = client.get_channel(int(channel_id))
        if ch is None:
            ch = await client.fetch_channel(int(channel_id))  # type: ignore

        await _send_long(ch, content)

        ctx.channel_messages[str(channel_id)].append(
            {"role": "assistant", "content": content}
        )


    async def send_discord_message(channel_id: str, content: str) -> None:
        """
        Safe to call from:
        - the Discord event loop (normal case)
        - some other event loop
        - a plain worker thread
        """
        loop = getattr(ctx, "discord_loop", None)
        if loop is None:
            raise RuntimeError("ctx.discord_loop not set yet (client not initialized).")

        # If we're already on the Discord loop, just do it.
        try:
            running = asyncio.get_running_loop()
            if running is loop:
                await _discord_send_impl(channel_id, content)
                return
        except RuntimeError:
            # No running loop in this thread (common in worker threads)
            running = None

        # Hop onto Discord loop from anywhere else.
        fut: Future = asyncio.run_coroutine_threadsafe(
            _discord_send_impl(channel_id, content),
            loop,
        )

        # If we're in *some* event loop, await it without blocking the loop thread.
        if running is not None:
            await asyncio.wrap_future(fut)
        else:
            # We're in a worker thread with no event loop.
            fut.result()

    discord.utils.setup_logging()

    ctx.send_discord_message = send_discord_message

    # Start a task to run routine tasks
    async def routine_tasks_loop():
        while True:
            now = time.time()
            for module in ctx.modules.values():
                for routine_task_state in module.routine_tasks.values():
                    if now - routine_task_state.last_run_timestamp >= routine_task_state.run_every_seconds:
                        _LOGGER.info(f"Running routine task {routine_task_state.name}...")
                        try:
                            await routine_task_state.function(ctx, {})
                            routine_task_state.last_run_timestamp = now
                            routine_task_state.run_count += 1
                        except Exception as e:
                            _LOGGER.error(f"Error while running routine task {routine_task_state.name}: {e}")
            await asyncio.sleep(10)

    asyncio.create_task(routine_tasks_loop())

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
