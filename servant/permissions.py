from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

import discord

from servant import channel_controls
from servant import database as sqlite3
from servant.defs import GlobalContext, RequestContext
from typed_json import coerce_str

_LOGGER = logging.getLogger(__name__)

DEFAULT_GLOBAL_ADMIN_IDS: set[str] = set()


def _normalize_id(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_global_admin_ids(ctx: GlobalContext) -> set[str]:
    ids = set(DEFAULT_GLOBAL_ADMIN_IDS)
    raw = ctx.config.get("admin_user_ids")
    if raw is None:
        return ids
    if isinstance(raw, str):
        for part in raw.replace(",", " ").split():
            parsed = _normalize_id(part)
            if parsed is not None:
                ids.add(parsed)
        return ids
    if isinstance(raw, list):
        for item in raw:
            parsed = _normalize_id(item)
            if parsed is not None:
                ids.add(parsed)
        return ids
    parsed = _normalize_id(raw)
    if parsed is not None:
        ids.add(parsed)
    return ids


def is_global_admin(ctx: GlobalContext, user_id: str) -> bool:
    return user_id in _load_global_admin_ids(ctx)


def has_admin_permissions(perms: discord.Permissions | None) -> bool:
    return bool(getattr(perms, "administrator", False)) if perms is not None else False


def has_staff_permissions(perms: discord.Permissions | None) -> bool:
    if perms is None:
        return False
    return bool(
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_guild", False)
        or getattr(perms, "manage_channels", False)
        or getattr(perms, "manage_messages", False)
        or getattr(perms, "moderate_members", False)
        or getattr(perms, "kick_members", False)
        or getattr(perms, "ban_members", False)
    )


def require_request(ctx: GlobalContext) -> RequestContext:
    if ctx.request is not None and ctx.privacy.is_opted_out(ctx.request.user_id):
        raise PermissionError("Vox access is disabled for this account.")
    request = ctx.request
    if request is None:
        raise RuntimeError("Request context not set.")
    return request


def _indexer_db_path(ctx: GlobalContext) -> Path:
    raw = ctx.config.get("indexer_db_path")
    if raw:
        return Path(coerce_str(raw, field="indexer_db_path", allow_empty=False)).expanduser().resolve()
    return (Path.cwd() / "servant_index.sqlite3").resolve()


def _lookup_guild_id_from_indexer(ctx: GlobalContext, channel_id: str) -> str | None:
    dbfile = _indexer_db_path(ctx)
    if not dbfile.exists():
        return None
    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000;")
    try:
        row = conn.execute(
            "SELECT guild_id FROM channels WHERE channel_id = ? LIMIT 1",
            (channel_id,),
        ).fetchone()
        if row and row["guild_id"]:
            return str(row["guild_id"])
        row = conn.execute(
            "SELECT guild_id FROM channel_state WHERE channel_id = ? LIMIT 1",
            (channel_id,),
        ).fetchone()
        if row and row["guild_id"]:
            return str(row["guild_id"])
        return None
    finally:
        conn.close()


async def _lookup_guild_id_from_discord(ctx: GlobalContext, channel_id: str) -> str | None:
    client = ctx.discord_client
    if client is None:
        return None
    try:
        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        guild = getattr(channel, "guild", None)
        if guild is None:
            return None
        return str(guild.id)
    except Exception as exc:
        _LOGGER.debug("Failed to resolve guild id for channel %s: %s", channel_id, exc)
        return None


async def resolve_guild_id_for_channel(ctx: GlobalContext, channel_id: str) -> str | None:
    channel_id_norm = _normalize_id(channel_id)
    if channel_id_norm is None:
        return None
    request = ctx.request
    if request is not None and request.guild_id is not None and request.channel_id == channel_id_norm:
        return request.guild_id
    from_indexer = _lookup_guild_id_from_indexer(ctx, channel_id_norm)
    if from_indexer is not None:
        return from_indexer
    return await _lookup_guild_id_from_discord(ctx, channel_id_norm)


async def _fetch_member(
    ctx: GlobalContext,
    *,
    guild_id: str,
    user_id: str,
) -> object | None:
    client = ctx.discord_client
    if client is None:
        return None
    try:
        guild = client.get_guild(int(guild_id))
        if guild is None:
            guild = await client.fetch_guild(int(guild_id))
        member = guild.get_member(int(user_id)) if hasattr(guild, "get_member") else None
        if member is None and hasattr(guild, "fetch_member"):
            member = await guild.fetch_member(int(user_id))
        return member
    except Exception as exc:
        _LOGGER.debug(
            "Failed to fetch member %s in guild %s: %s",
            user_id,
            guild_id,
            exc,
        )
        return None


def member_permissions(member: object | None) -> discord.Permissions | None:
    perms = getattr(member, "guild_permissions", None)
    return perms if isinstance(perms, discord.Permissions) else None


def member_role_ids(member: object | None) -> set[str]:
    roles = getattr(member, "roles", None)
    if not isinstance(roles, list | tuple):
        return set()
    result: set[str] = set()
    for role in roles:
        role_id = getattr(role, "id", None)
        if role_id is not None:
            result.add(str(role_id))
    return result


async def is_admin_in_guild(ctx: GlobalContext, user_id: str, guild_id: str) -> bool:
    if is_global_admin(ctx, user_id):
        return True
    member = await _fetch_member(ctx, guild_id=guild_id, user_id=user_id)
    return has_admin_permissions(member_permissions(member))


async def is_staff_in_guild(ctx: GlobalContext, user_id: str, guild_id: str) -> bool:
    if is_global_admin(ctx, user_id):
        return True
    member = await _fetch_member(ctx, guild_id=guild_id, user_id=user_id)
    return has_staff_permissions(member_permissions(member))


async def is_admin_for_channel(ctx: GlobalContext, channel_id: str) -> bool:
    request = require_request(ctx)
    if is_global_admin(ctx, request.user_id):
        return True
    guild_id = await resolve_guild_id_for_channel(ctx, channel_id)
    if guild_id is None:
        return False
    return await is_admin_in_guild(ctx, request.user_id, guild_id)


async def is_staff_for_channel(ctx: GlobalContext, channel_id: str) -> bool:
    request = require_request(ctx)
    if is_global_admin(ctx, request.user_id):
        return True
    guild_id = await resolve_guild_id_for_channel(ctx, channel_id)
    if guild_id is None:
        return False
    return await is_staff_in_guild(ctx, request.user_id, guild_id)


async def require_admin_for_guilds(
    ctx: GlobalContext,
    *,
    guild_ids: Iterable[str] | None,
    action: str,
) -> None:
    request = require_request(ctx)
    if is_global_admin(ctx, request.user_id):
        return
    guild_set: set[str] = set()
    for gid in guild_ids or []:
        normalized = _normalize_id(gid)
        if normalized is not None:
            guild_set.add(normalized)
    if not guild_set:
        raise PermissionError(f"Admin privileges required to {action}.")
    for guild_id in guild_set:
        if not await is_admin_in_guild(ctx, request.user_id, guild_id):
            raise PermissionError(f"Admin privileges required to {action} in guild {guild_id}.")


async def require_admin_for_channel(ctx: GlobalContext, channel_id: str, action: str) -> None:
    request = require_request(ctx)
    if is_global_admin(ctx, request.user_id):
        return
    guild_id = await resolve_guild_id_for_channel(ctx, channel_id)
    if guild_id is None:
        raise PermissionError(f"Admin privileges required to {action}.")
    if not await is_admin_in_guild(ctx, request.user_id, guild_id):
        raise PermissionError(f"Admin privileges required to {action} in guild {guild_id}.")


async def require_staff_for_channel(ctx: GlobalContext, channel_id: str, action: str) -> None:
    request = require_request(ctx)
    if is_global_admin(ctx, request.user_id):
        return
    guild_id = await resolve_guild_id_for_channel(ctx, channel_id)
    if guild_id is None:
        raise PermissionError(f"Staff privileges required to {action}.")
    if not await is_staff_in_guild(ctx, request.user_id, guild_id):
        raise PermissionError(f"Staff privileges required to {action} in guild {guild_id}.")


async def can_read_channel(ctx: GlobalContext, channel: object, user_id: str) -> bool:
    """Recheck membership and effective channel permissions; failure always denies access."""
    if not isinstance(channel, discord.abc.GuildChannel | discord.Thread):
        return False
    guild = channel.guild
    if guild is None or ctx.privacy.is_opted_out(user_id) or not channel_controls.allows_channel(ctx, channel):
        return False
    try:
        member = await guild.fetch_member(int(user_id))
        if isinstance(channel, discord.Thread):
            if ctx.discord_client is None or channel.parent_id is None:
                return False
            parent = await ctx.discord_client.fetch_channel(channel.parent_id)
            if not isinstance(parent, discord.abc.GuildChannel) or parent.guild.id != guild.id:
                return False
            perms = parent.permissions_for(member)
        else:
            perms = channel.permissions_for(member)
        if not perms.view_channel or not perms.read_message_history:
            return False
        if isinstance(channel, discord.Thread) and channel.is_private() and not perms.manage_threads:
            await channel.fetch_member(int(user_id))
        return True
    except Exception:
        return False


async def require_search_scope(
    ctx: GlobalContext, *, channel_ids: Iterable[str] = (), guild_id: str | None = None
) -> discord.TextChannel | discord.Thread | discord.VoiceChannel | discord.StageChannel | discord.DMChannel:
    """Content remains in its originating channel, including for global administrators.

    A caller's private-channel access alone does not authorize sharing its contents
    with everyone who can see the reply's destination.
    """
    request = require_request(ctx)
    if request.channel_id is None or ctx.discord_client is None:
        raise PermissionError("A live Discord channel and requester are required to retrieve messages.")
    if any(cid != request.channel_id for cid in channel_ids) or (guild_id is not None and guild_id != request.guild_id):
        raise PermissionError("Message retrieval is limited to the channel where you asked Vox.")
    if request.is_dm != (request.guild_id is None):
        raise PermissionError("Invalid Discord request scope.")
    try:
        # Refresh channel overwrites instead of trusting an old index or a tool-supplied server ID.
        channel = await ctx.discord_client.fetch_channel(int(request.channel_id))
    except Exception as exc:
        raise PermissionError("Unable to verify current channel access.") from exc
    if str(channel.id) != request.channel_id or not isinstance(
        channel, discord.TextChannel | discord.Thread | discord.VoiceChannel | discord.StageChannel | discord.DMChannel
    ):
        raise PermissionError("Unable to verify current channel access.")
    if request.is_dm:
        if not isinstance(channel, discord.DMChannel) or channel.recipient is None:
            raise PermissionError("Only your direct conversation with Vox can be retrieved.")
        if str(channel.recipient.id) != request.user_id:
            raise PermissionError("Only your direct conversation with Vox can be retrieved.")
    else:
        guild = getattr(channel, "guild", None)
        if guild is None or str(guild.id) != request.guild_id:
            raise PermissionError("The requested channel belongs to another server.")
        if not await can_read_channel(ctx, channel, request.user_id):
            raise PermissionError("You cannot currently read this channel, or Vox processing is disabled here.")
        bot = guild.me
        if bot is None:
            raise PermissionError("Unable to verify Vox's channel access.")
        perms = channel.permissions_for(bot)
        if not perms.view_channel or not perms.read_message_history:
            raise PermissionError("Vox cannot currently read this channel.")
    return channel
