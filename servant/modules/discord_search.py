from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import AsyncIterator
from typing import TypeAlias, TypeGuard

import discord

from servant.defs import GlobalContext, ToolDef
from typed_json import JSON, JSONDict, coerce_int, coerce_optional_str, obj_to_json

_LOGGER = logging.getLogger(__name__)

DEFAULT_MESSAGE_SCAN_LIMIT = 200
MAX_MESSAGE_SCAN_LIMIT = 1000
DEFAULT_MESSAGE_MAX_RESULTS = 20
MAX_MESSAGE_RESULTS = 100
DEFAULT_MAX_CONTENT_CHARS = 400
MAX_CONTENT_CHARS = 2000
DEFAULT_MAX_CHANNELS = 5
MAX_CHANNELS = 25

DEFAULT_MEMBER_SCAN_LIMIT = 200
MAX_MEMBER_SCAN_LIMIT = 2000
DEFAULT_MEMBER_MAX_RESULTS = 20
MAX_MEMBER_RESULTS = 200

DEFAULT_CHANNEL_MAX_RESULTS = 50
MAX_CHANNEL_RESULTS = 200


def _clamp(value: int, min_value: int, max_value: int) -> int:
    return max(min_value, min(value, max_value))


def _to_iso(ts: dt.datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return ts.replace(microsecond=0).isoformat() + "Z"


def _get_client(ctx: GlobalContext) -> discord.Client:
    client = ctx.discord_client
    if client is None:
        raise RuntimeError("discord client not available")
    return client


def _snowflake(value: str | None, field: str) -> discord.Object | None:
    if value is None:
        return None
    try:
        return discord.Object(id=int(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid snowflake id.") from exc


ResolvedChannel: TypeAlias = discord.abc.GuildChannel | discord.Thread | discord.abc.PrivateChannel


async def _resolve_channel(client: discord.Client, channel_id: str) -> ResolvedChannel:
    cid = int(str(channel_id))
    channel = client.get_channel(cid)
    if channel is None:
        channel = await client.fetch_channel(cid)
    return channel


async def _resolve_guild(
    client: discord.Client,
    *,
    guild_id: str | None,
    channel_id: str | None,
) -> discord.Guild:
    if guild_id:
        gid = int(str(guild_id))
        guild = client.get_guild(gid)
        if guild is None:
            guild = await client.fetch_guild(gid)
        return guild
    if channel_id:
        channel = await _resolve_channel(client, channel_id)
        if isinstance(channel, (discord.abc.GuildChannel, discord.Thread)):
            guild = channel.guild
            if guild is None:
                raise ValueError("channel does not belong to a guild.")
            return guild
        raise ValueError("channel does not belong to a guild.")
    raise ValueError("guild_id or channel_id is required.")


def _is_messageable(channel: ResolvedChannel) -> TypeGuard[discord.abc.Messageable]:
    return isinstance(channel, discord.abc.Messageable)


def _channel_type_name(channel: ResolvedChannel) -> str:
    type_obj = getattr(channel, "type", None)
    if type_obj is None:
        return "unknown"
    name = getattr(type_obj, "name", None)
    return name or str(type_obj)


def _normalize_query(query: object | None) -> str:
    return str(query).strip() if query is not None else ""


def _compile_query(query: str, *, regex: bool, case_sensitive: bool) -> re.Pattern[str] | None:
    if not query or not regex:
        return None
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(query, flags)
    except re.error as e:
        raise ValueError(f"Invalid regex: {e}") from e


def _text_matches(
    text: str,
    query: str,
    *,
    regex: bool,
    case_sensitive: bool,
    rx: re.Pattern[str] | None,
    match: str = "substring",
) -> bool:
    text = text or ""
    if not query:
        return True

    if match not in {"substring", "exact", "regex"}:
        raise ValueError("match must be one of: substring, exact, regex")

    if match == "regex" or regex:
        if rx is None:
            rx = _compile_query(query, regex=True, case_sensitive=case_sensitive)
        return bool(rx.search(text)) if rx else False

    if not case_sensitive:
        text = text.lower()
        query = query.lower()

    if match == "exact":
        return text == query
    return query in text


def _author_payload(author: discord.abc.User | None) -> JSONDict | None:
    if author is None:
        return None
    return {
        "id": str(author.id),
        "name": author.name,
        "display_name": getattr(author, "display_name", None),
        "bot": bool(getattr(author, "bot", False)),
        "mention": getattr(author, "mention", None),
    }


def _message_payload(message: discord.Message, max_content_chars: int) -> JSONDict:
    content = message.content or ""
    if max_content_chars == 0:
        content = ""
    elif max_content_chars > 0 and len(content) > max_content_chars:
        content = content[:max_content_chars] + "..."

    channel = getattr(message, "channel", None)
    channel_name = getattr(channel, "name", None) if channel else None

    return {
        "message_id": str(message.id),
        "channel_id": str(channel.id) if channel else None,
        "channel_name": channel_name,
        "channel_type": _channel_type_name(channel) if channel else None,
        "guild_id": str(message.guild.id) if message.guild else None,
        "author": _author_payload(message.author),
        "content": content,
        "created_at": _to_iso(message.created_at),
        "edited_at": _to_iso(message.edited_at),
        "jump_url": getattr(message, "jump_url", None),
        "attachments_count": len(message.attachments),
        "embeds_count": len(message.embeds),
        "pinned": bool(getattr(message, "pinned", False)),
    }


def _member_payload(member: discord.Member, include_roles: bool) -> JSONDict:
    payload: JSONDict = {
        "member_id": str(member.id),
        "name": member.name,
        "display_name": member.display_name,
        "nick": member.nick,
        "global_name": getattr(member, "global_name", None),
        "bot": bool(getattr(member, "bot", False)),
        "mention": member.mention,
        "joined_at": _to_iso(getattr(member, "joined_at", None)),
        "created_at": _to_iso(getattr(member, "created_at", None)),
        "avatar_url": (str(member.display_avatar.url) if getattr(member, "display_avatar", None) else None),
    }
    if include_roles:
        payload["roles"] = [{"id": str(role.id), "name": role.name} for role in getattr(member, "roles", [])]
    return payload


def _channel_payload(channel: discord.abc.GuildChannel | discord.Thread) -> JSONDict:
    return {
        "channel_id": str(channel.id),
        "name": getattr(channel, "name", None),
        "type": _channel_type_name(channel),
        "guild_id": str(channel.guild.id) if getattr(channel, "guild", None) else None,
        "parent_id": (str(getattr(channel, "parent_id", None)) if getattr(channel, "parent_id", None) else None),
        "topic": getattr(channel, "topic", None),
        "nsfw": getattr(channel, "nsfw", None),
        "position": getattr(channel, "position", None),
        "mention": getattr(channel, "mention", None),
    }


async def discord_search_messages(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    client = _get_client(ctx)
    await client.wait_until_ready()

    channel_ids: list[str] = []
    if obj.get("channel_ids") is not None:
        if not isinstance(obj["channel_ids"], list):
            raise ValueError("channel_ids must be a list of strings.")
        channel_ids.extend([str(cid) for cid in obj["channel_ids"]])
    if obj.get("channel_id") is not None:
        channel_ids.append(str(obj["channel_id"]))

    if not channel_ids:
        raise ValueError("channel_id or channel_ids is required.")

    seen: set[str] = set()
    unique_channel_ids: list[str] = []
    for cid in channel_ids:
        if cid in seen:
            continue
        seen.add(cid)
        unique_channel_ids.append(cid)
    channel_ids = unique_channel_ids

    max_channels = _clamp(
        coerce_int(obj.get("max_channels"), DEFAULT_MAX_CHANNELS),
        1,
        MAX_CHANNELS,
    )
    channels_truncated = len(channel_ids) > max_channels
    channel_ids = channel_ids[:max_channels]

    max_results = _clamp(
        coerce_int(obj.get("max_results"), DEFAULT_MESSAGE_MAX_RESULTS),
        1,
        MAX_MESSAGE_RESULTS,
    )
    scan_limit = _clamp(
        coerce_int(obj.get("scan_limit"), DEFAULT_MESSAGE_SCAN_LIMIT),
        1,
        MAX_MESSAGE_SCAN_LIMIT,
    )
    max_content_chars = coerce_int(obj.get("max_content_chars"), DEFAULT_MAX_CONTENT_CHARS)
    if max_content_chars < 0:
        max_content_chars = MAX_CONTENT_CHARS
    max_content_chars = _clamp(max_content_chars, 0, MAX_CONTENT_CHARS)

    query = _normalize_query(obj.get("query"))
    regex = bool(obj.get("regex", False))
    case_sensitive = bool(obj.get("case_sensitive", False))
    author_id = coerce_optional_str(obj.get("author_id"))
    include_bots = bool(obj.get("include_bots", True))
    oldest_first = bool(obj.get("oldest_first", False))

    before_id = coerce_optional_str(obj.get("before_message_id"))
    after_id = coerce_optional_str(obj.get("after_message_id"))
    around_id = coerce_optional_str(obj.get("around_message_id"))
    if around_id and (before_id or after_id):
        raise ValueError("around_message_id cannot be combined with before/after.")

    before = _snowflake(before_id, "before_message_id")
    after = _snowflake(after_id, "after_message_id")
    around = _snowflake(around_id, "around_message_id")

    rx = _compile_query(query, regex=regex, case_sensitive=case_sensitive)

    results: list[JSONDict] = []
    errors: list[JSONDict] = []
    messages_scanned = 0
    channels_scanned = 0
    truncated = False

    for channel_id in channel_ids:
        try:
            channel = await _resolve_channel(client, channel_id)
        except Exception as e:
            errors.append({"channel_id": str(channel_id), "error": str(e)})
            continue

        if not _is_messageable(channel):
            errors.append({"channel_id": str(channel_id), "error": "channel is not messageable"})
            continue

        channels_scanned += 1
        try:
            async for message in channel.history(
                limit=scan_limit,
                before=before,
                after=after,
                around=around,
                oldest_first=oldest_first,
            ):
                messages_scanned += 1
                if author_id and str(message.author.id) != str(author_id):
                    continue
                if not include_bots and bool(getattr(message.author, "bot", False)):
                    continue
                if not _text_matches(
                    message.content or "",
                    query,
                    regex=regex,
                    case_sensitive=case_sensitive,
                    rx=rx,
                ):
                    continue

                results.append(_message_payload(message, max_content_chars))
                if len(results) >= max_results:
                    truncated = True
                    break
        except Exception as e:
            errors.append({"channel_id": str(channel_id), "error": str(e)})

        if truncated:
            break

    return {
        "ok": True,
        "query": query,
        "regex": regex,
        "case_sensitive": case_sensitive,
        "author_id": str(author_id) if author_id is not None else None,
        "max_results": max_results,
        "scan_limit": scan_limit,
        "max_content_chars": max_content_chars,
        "channels_scanned": channels_scanned,
        "messages_scanned": messages_scanned,
        "channels_truncated": channels_truncated,
        "messages_returned": len(results),
        "truncated": truncated,
        "results": obj_to_json(results),
        "errors": obj_to_json(errors),
    }


async def discord_search_members(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    client = _get_client(ctx)
    await client.wait_until_ready()

    guild = await _resolve_guild(
        client,
        guild_id=coerce_optional_str(obj.get("guild_id")),
        channel_id=coerce_optional_str(obj.get("channel_id")),
    )

    query = _normalize_query(obj.get("query"))
    match = coerce_optional_str(obj.get("match")) or "substring"
    case_sensitive = bool(obj.get("case_sensitive", False))
    include_bots = bool(obj.get("include_bots", True))
    include_roles = bool(obj.get("include_roles", False))
    role_id = coerce_optional_str(obj.get("role_id"))

    max_results = _clamp(
        coerce_int(obj.get("max_results"), DEFAULT_MEMBER_MAX_RESULTS),
        1,
        MAX_MEMBER_RESULTS,
    )
    scan_limit = _clamp(
        coerce_int(obj.get("scan_limit"), DEFAULT_MEMBER_SCAN_LIMIT),
        1,
        MAX_MEMBER_SCAN_LIMIT,
    )

    rx = _compile_query(query, regex=(match == "regex"), case_sensitive=case_sensitive)

    results: list[JSONDict] = []
    members_scanned = 0
    truncated = False

    user_id = obj.get("user_id")
    if user_id is not None:
        try:
            mid = int(str(user_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("user_id must be a valid snowflake id.") from exc

        member = guild.get_member(mid)
        if member is None:
            try:
                member = await guild.fetch_member(mid)
            except Exception as e:
                return {"ok": False, "error": str(e)}

        if member:
            results.append(_member_payload(member, include_roles))
        return {
            "ok": True,
            "guild_id": str(guild.id),
            "query": query,
            "match": match,
            "case_sensitive": case_sensitive,
            "role_id": str(role_id) if role_id is not None else None,
            "max_results": max_results,
            "scan_limit": scan_limit,
            "members_scanned": 1,
            "members_returned": len(results),
            "truncated": False,
            "results": obj_to_json(results),
        }

    use_cache = bool(obj.get("use_cache", True))

    async def _iter_members() -> AsyncIterator[discord.Member]:
        if use_cache and getattr(guild, "members", None):
            for member in guild.members:
                yield member
            return
        async for member in guild.fetch_members(limit=scan_limit):
            yield member

    async for member in _iter_members():
        members_scanned += 1
        if members_scanned > scan_limit:
            break
        if not include_bots and bool(getattr(member, "bot", False)):
            continue
        if role_id is not None:
            if not any(str(r.id) == str(role_id) for r in getattr(member, "roles", [])):
                continue
        if query:
            fields = [
                member.name,
                member.display_name,
                member.nick or "",
                getattr(member, "global_name", "") or "",
                str(member.id),
            ]
            if not any(
                _text_matches(
                    field,
                    query,
                    regex=(match == "regex"),
                    case_sensitive=case_sensitive,
                    rx=rx,
                    match=match,
                )
                for field in fields
                if field is not None
            ):
                continue

        results.append(_member_payload(member, include_roles))
        if len(results) >= max_results:
            truncated = True
            break

    return {
        "ok": True,
        "guild_id": str(guild.id),
        "query": query,
        "match": match,
        "case_sensitive": case_sensitive,
        "role_id": str(role_id) if role_id is not None else None,
        "max_results": max_results,
        "scan_limit": scan_limit,
        "members_scanned": members_scanned,
        "members_returned": len(results),
        "truncated": truncated,
        "results": obj_to_json(results),
    }


async def discord_search_channels(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    client = _get_client(ctx)
    await client.wait_until_ready()

    guild = await _resolve_guild(
        client,
        guild_id=coerce_optional_str(obj.get("guild_id")),
        channel_id=coerce_optional_str(obj.get("channel_id")),
    )

    query = _normalize_query(obj.get("query"))
    match = coerce_optional_str(obj.get("match")) or "substring"
    case_sensitive = bool(obj.get("case_sensitive", False))
    types = obj.get("types") or []
    type_filters = {str(t).lower() for t in types} if isinstance(types, list) else set()

    max_results = _clamp(
        coerce_int(obj.get("max_results"), DEFAULT_CHANNEL_MAX_RESULTS),
        1,
        MAX_CHANNEL_RESULTS,
    )

    rx = _compile_query(query, regex=(match == "regex"), case_sensitive=case_sensitive)

    try:
        channels = await guild.fetch_channels()
    except Exception as e:
        _LOGGER.debug("fetch_channels failed: %s", e)
        channels = list(getattr(guild, "channels", []))

    results: list[JSONDict] = []
    for channel in channels:
        type_name = _channel_type_name(channel).lower()
        if type_filters and type_name not in type_filters:
            continue

        if query:
            if not _text_matches(
                getattr(channel, "name", "") or "",
                query,
                regex=(match == "regex"),
                case_sensitive=case_sensitive,
                rx=rx,
                match=match,
            ) and not _text_matches(
                getattr(channel, "topic", "") or "",
                query,
                regex=(match == "regex"),
                case_sensitive=case_sensitive,
                rx=rx,
                match=match,
            ):
                continue

        results.append(_channel_payload(channel))
        if len(results) >= max_results:
            break

    return {
        "ok": True,
        "guild_id": str(guild.id),
        "query": query,
        "match": match,
        "case_sensitive": case_sensitive,
        "types": obj_to_json(sorted(type_filters)),
        "max_results": max_results,
        "channels_returned": len(results),
        "results": obj_to_json(results),
    }


discord_search_messages_schema: ToolDef = ToolDef(
    name="discord_search_messages",
    function=lambda ctx, obj: discord_search_messages(ctx, obj),
    schema={
        "name": "discord_search_messages",
        "description": (
            "Search messages in one or more Discord channels using the live Discord API "
            "(not the local index). Requires channel_id or channel_ids."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring or regex to match in message content.",
                },
                "channel_id": {
                    "type": "string",
                    "description": "Channel ID to search.",
                },
                "channel_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of channel IDs to search.",
                },
                "author_id": {
                    "type": "string",
                    "description": "Only return messages from this author ID.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matching messages to return.",
                },
                "scan_limit": {
                    "type": "integer",
                    "description": "Maximum messages to scan per channel.",
                },
                "max_channels": {
                    "type": "integer",
                    "description": "Cap number of channels to scan.",
                },
                "before_message_id": {
                    "type": "string",
                    "description": "Only messages before this message ID.",
                },
                "after_message_id": {
                    "type": "string",
                    "description": "Only messages after this message ID.",
                },
                "around_message_id": {
                    "type": "string",
                    "description": "Messages around this message ID (exclusive with before/after).",
                },
                "oldest_first": {
                    "type": "boolean",
                    "description": "Search oldest-first instead of newest-first.",
                },
                "regex": {"type": "boolean", "description": "Treat query as regex."},
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case-sensitive matching.",
                },
                "include_bots": {
                    "type": "boolean",
                    "description": "Include bot-authored messages.",
                },
                "max_content_chars": {
                    "type": "integer",
                    "description": "Truncate message content in results (0 for empty).",
                },
            },
        },
    },
)


discord_search_members_schema: ToolDef = ToolDef(
    name="discord_search_members",
    function=lambda ctx, obj: discord_search_members(ctx, obj),
    schema={
        "name": "discord_search_members",
        "description": (
            "Search guild members using the live Discord API (not the local index). "
            "Provide guild_id or channel_id (to infer the guild)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "guild_id": {"type": "string", "description": "Guild ID to search."},
                "channel_id": {
                    "type": "string",
                    "description": "Channel ID to infer guild.",
                },
                "query": {
                    "type": "string",
                    "description": "Search string to match against member names.",
                },
                "match": {
                    "type": "string",
                    "description": "Match mode: substring, exact, or regex.",
                    "enum": ["substring", "exact", "regex"],
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case-sensitive matching.",
                },
                "user_id": {
                    "type": "string",
                    "description": "Fetch a specific member by user ID.",
                },
                "role_id": {"type": "string", "description": "Filter by role ID."},
                "include_bots": {
                    "type": "boolean",
                    "description": "Include bots in results.",
                },
                "include_roles": {
                    "type": "boolean",
                    "description": "Include role details in results.",
                },
                "use_cache": {
                    "type": "boolean",
                    "description": "Prefer cached members if available.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum matching members to return.",
                },
                "scan_limit": {
                    "type": "integer",
                    "description": "Maximum members to scan.",
                },
            },
        },
    },
)


discord_search_channels_schema: ToolDef = ToolDef(
    name="discord_search_channels",
    function=lambda ctx, obj: discord_search_channels(ctx, obj),
    schema={
        "name": "discord_search_channels",
        "description": (
            "Search guild channels using the live Discord API (not the local index). "
            "Provide guild_id or channel_id (to infer the guild)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "guild_id": {"type": "string", "description": "Guild ID to search."},
                "channel_id": {
                    "type": "string",
                    "description": "Channel ID to infer guild.",
                },
                "query": {
                    "type": "string",
                    "description": "Search string to match in channel names/topics.",
                },
                "match": {
                    "type": "string",
                    "description": "Match mode: substring, exact, or regex.",
                    "enum": ["substring", "exact", "regex"],
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case-sensitive matching.",
                },
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional channel type filters (e.g., text, voice, category).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum matching channels to return.",
                },
            },
        },
    },
)
