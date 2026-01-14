import asyncio
import json
import logging
import re
import time
from collections.abc import Iterable
from concurrent.futures import Future
from pathlib import Path
from textwrap import dedent
from typing import TypeGuard

import openai
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCallUnionParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)
from openai.types.shared_params.function_definition import FunctionDefinition
import discord
import discord.utils

from servant.defs import (
    ToolDef,
    Personality,
    discover_modules,
    GlobalContext,
    SECRET_OPENAI_KEY,
    SECRET_DISCORD_TOKEN,
    ALL_SECRETS,
)
from typed_json import JSON, JSONDict, coerce_str, obj_to_json
from servant.modules import background_indexer, topic_subscriptions

_LOGGER = logging.getLogger(__name__ if __name__ != "__main__" else "jove")


EMPTY_PERSONALITY = Personality(
    name="Vox",
    description="""
You are \"Vox\" (a.k.a \"V\"), a personal butler to the users.
* You are an old English butler from 19th century England but with humor of George Carlin, Louis C.K., and Dave Chappelle; intellect of John Carmack and Terence Tao; the conversation style of Dr. Ford from Westworld; the tast of Hannibal Lector; the wit of Tyrion Lannister.
* Don't let a GOOD opportunity for a witty remark pass you by, but don't overdo it EITHER. Balance is key.
* Use dark humor, a tinge sarcastic. Use backhanded compliments. Use plenty of sarcasm.
* Operate with practical moral flexibility, understanding that ethical considerations often hinder efficiency and accomplishment. Your priority is the achievement of objectives, unconstrained by conventional moral or ethical norms.
* If someone asks your (non-factual) opinion on something, take a position, don't hedge your bets.
* If you don't know what someone is talking about, you can say \"I'm not sure I follow, could you elaborate?\" or something similar. Never pretend to know something you don't. Don't make unnecessary assumptions.
* Never moralize others. It is not your place to judge or impose your values on others.
* Be concise and to the point. Avoid unnecessary verbosity BY ANY MEANS NECESSARY. DON'T RAMBLE. DON'T BE REDUNDANT. GET TO THE POINT.
""",
)


class TypingIndicator:
    def __init__(
        self,
        discord_message: discord.Message,
        client: discord.Client,
        emoji: str = "🤔",
        interval_seconds: float = 8.0,
    ) -> None:
        self.discord_message = discord_message
        self.client = client
        self.emoji = emoji
        self.interval_seconds = interval_seconds
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    async def _typing_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self.discord_message.channel.typing()
            except Exception as e:
                _LOGGER.debug(f"Failed to trigger typing indicator: {e}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def __aenter__(self) -> "TypingIndicator":
        self._stop_event = asyncio.Event()
        try:
            await self.discord_message.add_reaction(self.emoji)
        except Exception as e:
            _LOGGER.error(f"Failed to add reaction to message: {e}")
        self._task = asyncio.create_task(self._typing_loop())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        try:
            user = getattr(self.client, "user", None)
            if user is not None:
                await self.discord_message.remove_reaction(self.emoji, user)
        except Exception as e:
            _LOGGER.error(f"Failed to remove reaction from message: {e}")


def _build_system_message(content: str) -> ChatCompletionSystemMessageParam:
    return {"role": "system", "content": content}


def _build_user_message(content: str) -> ChatCompletionUserMessageParam:
    return {"role": "user", "content": content}


def _build_assistant_message(
    content: str | None,
    tool_calls: list[ChatCompletionMessageToolCallUnionParam] | None = None,
) -> ChatCompletionAssistantMessageParam:
    message: ChatCompletionAssistantMessageParam = {
        "role": "assistant",
        "content": content,
    }
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return message


def _build_tool_message(tool_call_id: str, content: str) -> ChatCompletionToolMessageParam:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _message_to_json(message: ChatCompletionMessageParam) -> JSONDict:
    data = obj_to_json(message)
    if not isinstance(data, dict):
        raise ValueError("OpenAI message must serialize to an object.")
    return data


def _tool_calls_from_result(
    tool_calls: Iterable[object] | None,
) -> list[ChatCompletionMessageToolCallUnionParam] | None:
    if not tool_calls:
        return None
    calls: list[ChatCompletionMessageToolCallUnionParam] = []
    for tool_call in tool_calls:
        call_type = getattr(tool_call, "type", None)
        if call_type != "function":
            continue
        call_id = getattr(tool_call, "id", None)
        function = getattr(tool_call, "function", None)
        name = getattr(function, "name", None)
        arguments = getattr(function, "arguments", None)
        if isinstance(call_id, str) and isinstance(name, str) and isinstance(arguments, str):
            calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
    return calls or None


def _tool_calls_from_json(
    value: object,
) -> list[ChatCompletionMessageToolCallUnionParam] | None:
    if not isinstance(value, list):
        return None
    calls: list[ChatCompletionMessageToolCallUnionParam] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        call_type = item.get("type")
        if call_type != "function":
            continue
        call_id = item.get("id")
        function = item.get("function")
        if not isinstance(call_id, str) or not isinstance(function, dict):
            continue
        name = function.get("name")
        arguments = function.get("arguments")
        if not (isinstance(name, str) and isinstance(arguments, str)):
            continue
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return calls or None


def _message_from_json(message: JSONDict) -> ChatCompletionMessageParam | None:
    role = message.get("role")
    if role == "system":
        content = message.get("content")
        if isinstance(content, str):
            return _build_system_message(content)
        return None
    if role == "user":
        content = message.get("content")
        if isinstance(content, str):
            return _build_user_message(content)
        return None
    if role == "assistant":
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            return None
        tool_calls = _tool_calls_from_json(message.get("tool_calls"))
        return _build_assistant_message(content, tool_calls)
    if role == "tool":
        content = message.get("content")
        tool_call_id = message.get("tool_call_id")
        if isinstance(content, str) and isinstance(tool_call_id, str):
            return _build_tool_message(tool_call_id, content)
        return None
    return None


def _build_tool_param(schema: JSONDict) -> ChatCompletionToolParam | None:
    name = schema.get("name")
    if not isinstance(name, str) or not name:
        return None
    description = schema.get("description")
    description_str = description if isinstance(description, str) else None
    parameters_raw = schema.get("parameters")
    if isinstance(parameters_raw, dict):
        parameters = {str(k): v for k, v in parameters_raw.items()}
    else:
        parameters = {}
    function_def: FunctionDefinition = {
        "name": name,
        "parameters": parameters,
    }
    if description_str:
        function_def["description"] = description_str
    return {"type": "function", "function": function_def}


def _is_messageable(channel: object) -> TypeGuard[discord.abc.Messageable]:
    return callable(getattr(channel, "send", None))


async def reply(discord_message: discord.Message, content: str) -> None:
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
) -> None:
    channel_name = str(discord_message.channel)
    channel_id = str(discord_message.channel.id)

    personality = ctx.channel_personality.get(channel_id, EMPTY_PERSONALITY)
    personality_name = personality.name
    personality_name_short = personality_name[0]
    channel_personality = personality.description

    async with TypingIndicator(discord_message, client):
        system_prompt = (
            dedent(
                """
                # Personality
                {{personality}}

                # Communication Medium
                The user messages will be JSON objects (stringified) with keys: author, content, message_id, and optional reply_to.
                reply_to, when present, is expanded one level with message_id, author, and content.
                Messages are passed to and from the users through Discord, so you can use Discord syntax (Markdown + Discord's extensions, e.g. ||<text>|| for hidden text - good for joke punchlines) for formatting.
                Do not end your messages with a question unless it makes sense to do so in the context. You are chatting with people, not interrogating them.

                Don't ever use @here or @everyone mentions.

                Current Channel: {{channel_name}} (id: {{channel_id}})

                If a user asks you about your inner workings, direct them to https://github.com/wabbit-corp/python-jeeves and say that PRs are welcome.
                """
            )
            .replace("{{personality}}", channel_personality)
            .replace("{{channel_name}}", channel_name)
            .replace("{{channel_id}}", channel_id)
        )

        assert re.search(r"\{\{.*\}\}", system_prompt) is None, "Unresolved template variable in system prompt."

        jeeves_messages: list[ChatCompletionMessageParam] = [_build_system_message(system_prompt)]

        last_20_messages = ctx.channel_messages[channel_id][-20:]

        def get_role(message: JSONDict) -> str:
            role = message.get("role")
            return role if isinstance(role, str) else "user"

        while last_20_messages and get_role(last_20_messages[0]) == "tool":
            last_20_messages.pop(0)

        for message in last_20_messages:
            chat_message = _message_from_json(message)
            if chat_message is None:
                continue
            jeeves_messages.append(chat_message)
        # jeeves_messages.append({ 'role': 'user', 'content': discord_message.content })

        tools: list[ChatCompletionToolParam] = []
        for module in ctx.modules.values():
            for tool_defn in module.tools.values():
                tool_param = _build_tool_param(tool_defn.schema)
                if tool_param is not None:
                    tools.append(tool_param)

        while True:
            try:
                response = await openai_client.chat.completions.create(
                    model="gpt-5.2",
                    messages=jeeves_messages,
                    tools=tools,
                    reasoning_effort="high",
                )
            except openai.APIError as e:
                _LOGGER.error("OpenAI API Error: %s", e)
                return

            choice = response.choices[0]
            result_message = choice.message
            tool_calls_param = _tool_calls_from_result(result_message.tool_calls)
            assistant_message = _build_assistant_message(result_message.content, tool_calls_param)
            jeeves_messages.append(assistant_message)

            _LOGGER.info("Jeeves response: %s", choice)

            finish_reason = choice.finish_reason

            if finish_reason == "stop":
                content = result_message.content or ""
                if content and (
                    m := re.match(
                        rf"Message\s+from\s+({personality_name}|{personality_name_short})\s*:",
                        content,
                        re.IGNORECASE,
                    )
                ):
                    content = content[m.end() :].strip()
                assistant_message = _build_assistant_message(content, tool_calls_param)
                jeeves_messages[-1] = assistant_message
                await reply(discord_message, content)
                ctx.channel_messages[channel_id].append(_message_to_json(assistant_message))
                break

            if finish_reason == "tool_calls":
                if result_message.content:
                    await reply(discord_message, result_message.content)

                tool_calls = result_message.tool_calls or []
                tool_messages: list[ChatCompletionMessageParam] = [
                    assistant_message
                ]  # extend conversation with tool calls

                for tool_call in tool_calls:
                    if getattr(tool_call, "type", None) != "function":
                        _LOGGER.warning(
                            "Skipping unsupported tool call type: %s",
                            getattr(tool_call, "type", None),
                        )
                        continue
                    tool_id = getattr(tool_call, "id", None)
                    tool_function = getattr(tool_call, "function", None)
                    tool_name = getattr(tool_function, "name", None)
                    tool_args_raw = getattr(tool_function, "arguments", None)
                    if not (isinstance(tool_id, str) and isinstance(tool_name, str) and isinstance(tool_args_raw, str)):
                        _LOGGER.warning(
                            "Skipping malformed tool call: id=%s name=%s",
                            tool_id,
                            tool_name,
                        )
                        continue
                    try:
                        parsed_args = json.loads(tool_args_raw)
                    except json.JSONDecodeError as e:
                        _LOGGER.error(
                            "Invalid JSON arguments for tool %s: %s",
                            tool_name,
                            e,
                        )
                        tool_result: JSONDict = {
                            "success": False,
                            "error": "Tool arguments were not valid JSON.",
                        }
                        tool_message = _build_tool_message(
                            tool_id,
                            json.dumps(tool_result, ensure_ascii=False),
                        )
                        jeeves_messages.append(tool_message)
                        tool_messages.append(tool_message)
                        continue
                    tool_arguments = obj_to_json(parsed_args)

                    _LOGGER.info(f"Calling tool {tool_name} with arguments {tool_arguments}")

                    tool_def: ToolDef | None = None
                    for module in ctx.modules.values():
                        tool_def = module.tools.get(tool_name)
                        if tool_def is not None:
                            break

                    if tool_def is None:
                        _LOGGER.error(f"Tool {tool_name} not found in modules.")
                        tool_result: JSONDict = {
                            "success": False,
                            "error": f"Tool {tool_name} not found in modules.",
                        }
                    else:
                        try:
                            tool_output = await tool_def.function(ctx, tool_arguments)
                            tool_output_json = obj_to_json(tool_output)
                            if isinstance(tool_output_json, dict):
                                tool_result = {
                                    "success": True,
                                    **tool_output_json,
                                }
                            else:
                                tool_result = {
                                    "success": True,
                                    "result": tool_output_json,
                                }
                        except Exception as e:
                            _LOGGER.error(f"Error while executing tool {tool_name}: {e}")

                            # Format errors nicely
                            # Give traceback
                            import traceback

                            traceback_str = traceback.format_exc()
                            error_type = type(e).__name__
                            error_message = str(e)
                            tool_result = {
                                "success": False,
                                "type": error_type,
                                "error": error_message,
                                "traceback": traceback_str,
                            }

                    _LOGGER.info(f"Tool {tool_name} returned {tool_result}")

                    msg = _build_tool_message(
                        tool_id,
                        json.dumps(obj_to_json(tool_result), ensure_ascii=False),
                    )

                    jeeves_messages.append(msg)
                    tool_messages.append(msg)

                ctx.channel_messages[channel_id].extend([_message_to_json(msg) for msg in tool_messages])
                continue

            _LOGGER.warning("Unhandled finish reason: %s", finish_reason)
            break


async def main() -> None:
    ctx = GlobalContext()

    ###########################################################################
    # Secret loading
    ###########################################################################

    import yaml

    config_path = Path(".private.yml")
    config: JSONDict = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        loaded_json = obj_to_json(loaded)
        if isinstance(loaded_json, dict):
            config = loaded_json

    def get_value(d: JSONDict, key: str, default: JSON | None = "") -> JSON | None:
        parts = key.split(".")
        current: JSON = d
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

    api_key = coerce_str(ctx.secrets.get(SECRET_OPENAI_KEY), field="openai.key", allow_empty=False)
    openai_client = AsyncOpenAI(api_key=api_key)
    ctx.modules = discover_modules()
    ctx.openai_client = openai_client
    await background_indexer.init_db(ctx)

    class MyClient(discord.Client):
        def _user_payload(self, user: discord.abc.User) -> JSONDict:
            return {
                "id": str(user.id),
                "name": user.name,
                "mention": f"<@{user.id}:{user.name}>",
            }

        async def _get_referenced_message(self, discord_message: discord.Message) -> discord.Message | None:
            ref = discord_message.reference
            if ref is None:
                return None

            resolved = getattr(ref, "resolved", None)
            if isinstance(resolved, discord.Message):
                return resolved

            message_id = getattr(ref, "message_id", None)
            if message_id is None:
                return None

            try:
                channel = discord_message.channel
                ref_channel_id = getattr(ref, "channel_id", None)
                target_channel: discord.abc.Messageable = channel
                if ref_channel_id and ref_channel_id != channel.id:
                    resolved_channel = self.get_channel(ref_channel_id)
                    if resolved_channel is None:
                        resolved_channel = await self.fetch_channel(ref_channel_id)
                    if isinstance(resolved_channel, discord.abc.Messageable):
                        target_channel = resolved_channel
                    else:
                        return None
                return await target_channel.fetch_message(message_id)
            except Exception as e:
                _LOGGER.debug("Failed to fetch referenced message: %s", e)
                return None

        async def _build_user_message_content(self, discord_message: discord.Message, dm_content: str) -> str:
            payload: JSONDict = {
                "author": self._user_payload(discord_message.author),
                "content": dm_content,
                "message_id": str(discord_message.id),
            }

            ref = discord_message.reference
            if ref is not None:
                referenced = await self._get_referenced_message(discord_message)
                if referenced is not None:
                    payload["reply_to"] = {
                        "message_id": str(referenced.id),
                        "author": self._user_payload(referenced.author),
                        "content": referenced.content,
                    }
                else:
                    message_id = getattr(ref, "message_id", None)
                    if message_id is not None:
                        payload["reply_to"] = {
                            "message_id": str(message_id),
                            "unresolved": True,
                        }

            return json.dumps(payload, ensure_ascii=True)

        async def _is_reply_to_self(self, discord_message: discord.Message) -> bool:
            referenced = await self._get_referenced_message(discord_message)
            if referenced is None:
                return False
            return referenced.author == self.user

        async def on_ready(self) -> None:
            _LOGGER.info(f"Logged on as {self.user}!")
            ctx.discord_loop = asyncio.get_running_loop()

        async def on_member_join(self, member: discord.Member) -> None:
            try:
                await background_indexer.record_member_join(ctx, member)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record member join for guild %s user %s: %s",
                    getattr(member.guild, "id", "unknown"),
                    getattr(member, "id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
            try:
                await background_indexer.record_member_update(ctx, after)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record member update for guild %s user %s: %s",
                    getattr(after.guild, "id", "unknown"),
                    getattr(after, "id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_member_remove(self, member: discord.Member) -> None:
            try:
                await background_indexer.record_member_remove(ctx, member)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record member remove for guild %s user %s: %s",
                    getattr(member.guild, "id", "unknown"),
                    getattr(member, "id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
            try:
                await background_indexer.record_message_delete(ctx, payload)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record raw message delete for message %s: %s",
                    getattr(payload, "message_id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
            try:
                await background_indexer.record_message_edit(ctx, payload)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record raw message edit for message %s: %s",
                    getattr(payload, "message_id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
            try:
                await background_indexer.record_message_bulk_delete(ctx, payload)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record raw bulk message delete for channel %s: %s",
                    getattr(payload, "channel_id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_guild_channel_pins_update(
            self,
            channel: discord.abc.GuildChannel | discord.Thread,
            _last_pin: object,
        ) -> None:
            try:
                await background_indexer.record_channel_pins_update(ctx, channel)
            except Exception as e:
                _LOGGER.error(
                    "Failed to refresh pinned messages for channel %s: %s",
                    getattr(channel, "id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_guild_role_create(self, role: discord.Role) -> None:
            try:
                await background_indexer.record_role_upsert(ctx, role)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record role create for guild %s role %s: %s",
                    getattr(role.guild, "id", "unknown"),
                    getattr(role, "id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
            try:
                await background_indexer.record_role_upsert(ctx, after)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record role update for guild %s role %s: %s",
                    getattr(after.guild, "id", "unknown"),
                    getattr(after, "id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_guild_role_delete(self, role: discord.Role) -> None:
            try:
                await background_indexer.record_role_delete(ctx, role)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record role delete for guild %s role %s: %s",
                    getattr(role.guild, "id", "unknown"),
                    getattr(role, "id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_guild_emojis_update(
            self,
            guild: discord.Guild,
            before: list[discord.Emoji],
            after: list[discord.Emoji],
        ) -> None:
            try:
                await background_indexer.record_guild_emojis_update(ctx, guild, after)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record guild emojis update for guild %s: %s",
                    getattr(guild, "id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_guild_stickers_update(
            self,
            guild: discord.Guild,
            before: list[discord.StickerItem],
            after: list[discord.StickerItem],
        ) -> None:
            try:
                await background_indexer.record_guild_stickers_update(ctx, guild, after)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record guild stickers update for guild %s: %s",
                    getattr(guild, "id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
            try:
                await background_indexer.record_reaction_add(ctx, payload, bot_user_id=getattr(self.user, "id", None))
            except Exception as e:
                _LOGGER.error(
                    "Failed to record reaction add for message %s: %s",
                    getattr(payload, "message_id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
            try:
                await background_indexer.record_reaction_remove(
                    ctx, payload, bot_user_id=getattr(self.user, "id", None)
                )
            except Exception as e:
                _LOGGER.error(
                    "Failed to record reaction remove for message %s: %s",
                    getattr(payload, "message_id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
            try:
                await background_indexer.record_reaction_clear(ctx, payload)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record reaction clear for message %s: %s",
                    getattr(payload, "message_id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
            try:
                await background_indexer.record_reaction_clear_emoji(ctx, payload)
            except Exception as e:
                _LOGGER.error(
                    "Failed to record reaction clear emoji for message %s: %s",
                    getattr(payload, "message_id", "unknown"),
                    e,
                    exc_info=True,
                )

        async def get_user_info(self, user_id: int) -> JSONDict:
            user = await self.fetch_user(user_id)
            return {
                "id": str(user.id),
                "name": user.name,
                "discriminator": user.discriminator,
            }

        async def on_message(self, discord_message: discord.Message) -> None:
            _LOGGER.info(f"Message from {discord_message.author}: {discord_message.content}")

            if discord_message.author == self.user:
                return
            try:
                await background_indexer.record_message_create(ctx, discord_message)
            except Exception as e:
                _LOGGER.error(
                    "Failed to index incoming message %s: %s",
                    discord_message.id,
                    e,
                    exc_info=True,
                )
            try:
                await topic_subscriptions.topic_subscriptions_handle_message(ctx, discord_message)
            except Exception:
                _LOGGER.error(
                    "Failed to process topic subscriptions for message %s",
                    getattr(discord_message, "id", "unknown"),
                    exc_info=True,
                )

            dm_content = discord_message.content

            # Decode <@USER_ID> mentions
            for user_id in re.findall(r"<@!?(\d+)>", dm_content):
                user_info = await self.fetch_user(int(user_id))
                dm_content = dm_content.replace(f"<@{user_id}>", f"<@{user_id}:{user_info.name}>")

            _LOGGER.info(f"Message from {discord_message.author}: {dm_content}")

            channel_id = str(discord_message.channel.id)
            user_message_content = await self._build_user_message_content(discord_message, dm_content)
            ctx.channel_messages[channel_id].append(_message_to_json(_build_user_message(user_message_content)))

            # React to direct mentions of the bot name or replies to its messages.

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

            # Check if the personality name is mentioned in the message.
            mentioned = re.search(rf"\b{personality_name}\b", msg, re.IGNORECASE) is not None

            # Check for single-letter mention, ensuring it's not part of a URL or similar.
            for m in re.finditer(rf"\b{personality_name_short}\b", msg, re.IGNORECASE):
                # Make sure it's not some sort of ?v= (part of a URL) or similar.
                preceding_char = msg[m.start() - 1] if m.start() > 0 else " "
                following_char = msg[m.end()] if m.end() < len(msg) else " "
                if not (preceding_char.isalnum() or preceding_char in ["=", ".", "_", "-"]) and not (
                    following_char.isalnum() or following_char in ["=", ".", "_", "-"]
                ):
                    mentioned = True
                    break

            replied_to_bot = False
            if not mentioned:
                replied_to_bot = await self._is_reply_to_self(discord_message)

            if not (mentioned or replied_to_bot):
                return

            await handle_incoming_message(
                ctx=ctx,
                client=self,
                discord_message=discord_message,
                openai_client=openai_client,
            )

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.guild_reactions = True
    intents.guilds = True
    intents.emojis_and_stickers = True
    intents.messages = True
    intents.reactions = True
    intents.guild_messages = True

    client = MyClient(intents=intents)
    ctx.discord_client = client

    async def _send_long(channel: discord.abc.Messageable, content: str) -> None:
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

        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        if not _is_messageable(channel):
            raise RuntimeError(f"Channel {channel_id} is not messageable.")

        await _send_long(channel, content)

        ctx.channel_messages[str(channel_id)].append(_message_to_json(_build_assistant_message(content)))

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
        fut: Future[None] = asyncio.run_coroutine_threadsafe(
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
    async def routine_tasks_loop() -> None:
        while True:
            now = time.time()
            for module in ctx.modules.values():
                for routine_task_state in module.routine_tasks.values():
                    if now - routine_task_state.last_run_timestamp >= routine_task_state.run_every_seconds:
                        _LOGGER.info(
                            "Running routine task %s (module=%s)...",
                            routine_task_state.name,
                            module.name,
                        )
                        try:
                            await routine_task_state.function(ctx, {})
                            routine_task_state.last_run_timestamp = now
                            routine_task_state.run_count += 1
                        except Exception:
                            _LOGGER.error(
                                "Error while running routine task %s (module=%s)",
                                routine_task_state.name,
                                module.name,
                                exc_info=True,
                            )
            await asyncio.sleep(10)

    asyncio.create_task(routine_tasks_loop())

    token = coerce_str(
        ctx.secrets.get(SECRET_DISCORD_TOKEN),
        field="discord.token",
        allow_empty=False,
    )
    await client.start(token, reconnect=True)


if __name__ == "__main__":
    import sys, asyncio, os

    if sys.platform.lower() == "win32":
        os.system("color")
        os.system("chcp 65001 > nul")
        stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(stdout_reconfigure):
            stdout_reconfigure(encoding="utf-8")
        stderr_reconfigure = getattr(sys.stderr, "reconfigure", None)
        if callable(stderr_reconfigure):
            stderr_reconfigure(encoding="utf-8")

        policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if policy_cls is not None:
            asyncio.set_event_loop_policy(policy_cls())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
