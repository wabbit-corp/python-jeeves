"""Read the current channel policy without depending on cleanup controllers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from servant.defs import GlobalContext


def is_allowed(ctx: GlobalContext, channel_id: str, guild_id: str | None) -> bool:
    if channel_id in ctx.guild_retention.removed_channels or channel_id in ctx.channel_controls.disabled:
        return False
    if guild_id is None:
        return True  # DMs sent directly to Vox retain their existing behavior.
    return ctx.channel_controls.loaded and guild_id not in ctx.guild_retention.removed_guilds


def allows_channel(ctx: GlobalContext, channel: object) -> bool:
    guild = getattr(channel, "guild", None)
    channel_id = getattr(channel, "id", None)
    return channel_id is not None and is_allowed(ctx, str(channel_id), str(guild.id) if guild else None)


def allows_guild(ctx: GlobalContext, guild_id: str) -> bool:
    return ctx.channel_controls.loaded and guild_id not in ctx.guild_retention.removed_guilds
