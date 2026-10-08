"""Public privacy information and privately confirmed moderator channel controls."""

from __future__ import annotations

import logging
from typing import TypeAlias

import discord
from discord import app_commands

from servant import channel_controls
from servant.defs import GlobalContext

_LOGGER = logging.getLogger(__name__)
ProcessingChannel: TypeAlias = discord.TextChannel | discord.Thread | discord.VoiceChannel | discord.StageChannel


def privacy_embed(ctx: GlobalContext, channel: object | None) -> discord.Embed:
    embed = discord.Embed(title="Vox privacy", url=channel_controls.POLICY_URL, colour=discord.Colour.blue())
    embed.description = (
        f"[Read the privacy policy]({channel_controls.POLICY_URL})\nContact: {channel_controls.PRIVACY_CONTACT}"
    )
    embed.add_field(
        name="Your choice",
        value=(
            "Use `/vox optout` for a private confirmation. Confirming stops processing your authored messages "
            "and voice, removes active indexed messages and personal feature records, and disables your Vox "
            "access across servers and DMs. The preference persists across restarts."
        ),
        inline=False,
    )
    embed.add_field(
        name="Access or delete your data",
        value=(
            f"Email {channel_controls.PRIVACY_CONTACT} privately with your Discord user ID and request scope. "
            "Ask about logs, transcripts, exports, recovery copies, and provider copies too. We respond within "
            "one month. Minimal opt-out identifiers remain to honor your choice. Do not send passwords or tokens."
        ),
        inline=False,
    )
    enabled = channel is not None and channel_controls.allows_channel(ctx, channel)
    if channel is not None and getattr(channel, "guild", None) is None:
        channel_details = (
            "This is your direct conversation with Vox. Only your own DM history can be retrieved here. "
            "Use `/vox optout` to stop processing your authored messages and voice and disable your Vox access."
        )
    else:
        channel_details = (
            "Moderators can use `/vox channel disable` to stop processing and clear its active archive. "
            + ("Vox processing is enabled here. " if enabled else "Vox processing is disabled here. ")
            + "Vox's access follows Discord permissions. "
            "Message retrieval stays in the channel where it was requested and checks current permissions. "
        )
    embed.add_field(
        name="This channel",
        value=channel_details,
        inline=False,
    )
    return embed


async def _authorize(
    interaction: discord.Interaction[discord.Client], ctx: GlobalContext, channel_id: int
) -> ProcessingChannel:
    if ctx.privacy.is_opted_out(interaction.user.id):
        raise PermissionError("Vox access is disabled for this account. /vox privacy remains available.")
    if interaction.guild_id is None:
        raise PermissionError("Channel controls are available only in a server.")
    if not ctx.guild_retention.ready.is_set():
        raise RuntimeError("Vox is still connecting. Try again shortly.")
    try:
        channel = await interaction.client.fetch_channel(channel_id)
        if not isinstance(channel, ProcessingChannel) or channel.guild.id != interaction.guild_id:
            raise PermissionError("Choose a message or voice channel in this server.")
        member = await channel.guild.fetch_member(interaction.user.id)
        if isinstance(channel, discord.Thread):
            if channel.parent_id is None:
                raise PermissionError("Unable to verify thread permissions.")
            parent = await interaction.client.fetch_channel(channel.parent_id)
            if not isinstance(parent, discord.abc.GuildChannel) or parent.guild.id != interaction.guild_id:
                raise PermissionError("Unable to verify thread permissions.")
            perms = parent.permissions_for(member)
            if channel.is_private() and not perms.manage_threads:
                await channel.fetch_member(interaction.user.id)
        else:
            perms = channel.permissions_for(member)
        if not perms.view_channel or not (
            perms.administrator or perms.manage_guild or perms.manage_channels or perms.manage_messages
        ):
            raise PermissionError("Manage Channels, Manage Server, or Manage Messages permission is required here.")
        return channel
    except discord.HTTPException as exc:
        raise PermissionError("Unable to verify your current channel permissions.") from exc


class DisableConfirmation(discord.ui.View):
    def __init__(self, ctx: GlobalContext, *, owner_id: int, channel_id: int) -> None:
        super().__init__(timeout=180)
        self.ctx = ctx
        self.owner_id = owner_id
        self.channel_id = channel_id
        self.busy = False
        self.started = False

    async def interaction_check(self, interaction: discord.Interaction[discord.Client]) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Only the moderator who requested this can confirm it.", ephemeral=True)
        return False

    @discord.ui.button(label="Stop processing and clear archive", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction[discord.Client], button: discord.ui.Button[DisableConfirmation]
    ) -> None:
        if self.busy:
            await interaction.response.send_message("Cleanup is already running.", ephemeral=True)
            return
        self.busy = True
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await _authorize(interaction, self.ctx, self.channel_id)
        except (PermissionError, RuntimeError) as exc:
            self.busy = False
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        self.started = True
        try:
            await channel_controls.disable(self.ctx, str(channel.id), str(channel.guild.id))
        except Exception:
            self.busy = False
            _LOGGER.exception("Channel archive cleanup failed for %s", self.channel_id)
            await interaction.followup.send(
                "Processing is paused, but archive cleanup is unfinished. Confirm again to retry; "
                "Vox also retries after reconnecting. Contact wabbit@wabbit.one if it keeps failing.",
                ephemeral=True,
            )
            return
        button.disabled = True
        self.cancel.disabled = True
        await interaction.edit_original_response(
            content=(
                f"Vox processing is disabled in <#{self.channel_id}> and its active archive has been cleared. "
                "Historical logs, exports, recovery copies, and provider copies need separate deletion: wabbit@wabbit.one."
            ),
            embed=None,
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction[discord.Client], button: discord.ui.Button[DisableConfirmation]
    ) -> None:
        if self.busy:
            await interaction.response.send_message("Cleanup is already running.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=(
                "Dismissed. Processing remains paused and archive cleanup is unfinished. Run `/vox channel disable` to retry."
                if self.started
                else "Cancelled. Channel settings are unchanged."
            ),
            embed=None,
            view=None,
        )
        self.stop()


def register(group: app_commands.Group, ctx: GlobalContext) -> None:
    @group.command(name="privacy", description="Read Vox's privacy policy and your opt-out and deletion options")
    async def show_privacy(interaction: discord.Interaction[discord.Client]) -> None:
        await interaction.response.send_message(embed=privacy_embed(ctx, interaction.channel), ephemeral=True)

    controls = app_commands.Group(name="channel", description="Moderator controls for Vox processing and archives")

    @controls.command(name="enable", description="Moderator: resume Vox processing in a disabled channel")
    async def enable(
        interaction: discord.Interaction[discord.Client], channel: ProcessingChannel | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            target_id = channel.id if channel is not None else interaction.channel_id
            if target_id is None:
                raise PermissionError("Choose a server channel.")
            target = await _authorize(interaction, ctx, target_id)
            await channel_controls.enable(ctx, str(target.id), str(target.guild.id))
            await interaction.followup.send(
                f"Vox processing is enabled in <#{target.id}>, including accessible history.", ephemeral=True
            )
        except (PermissionError, RuntimeError, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            _LOGGER.exception("Could not enable channel processing")
            await interaction.followup.send(
                "Could not save channel settings. Try again or contact wabbit@wabbit.one.", ephemeral=True
            )

    @controls.command(name="disable", description="Moderator: stop Vox processing and clear a channel's active archive")
    async def disable(
        interaction: discord.Interaction[discord.Client], channel: ProcessingChannel | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            target_id = channel.id if channel is not None else interaction.channel_id
            if target_id is None:
                raise PermissionError("Choose a server channel.")
            target = await _authorize(interaction, ctx, target_id)
        except (PermissionError, RuntimeError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            content=(
                f"Stop Vox processing in <#{target.id}> and delete its active message archive, conversation records, "
                "reminders, subscriptions, notification history, and configured voice transcripts? "
                "Other channels and personal opt-out preferences remain. Historical copies require operator cleanup."
            ),
            view=DisableConfirmation(ctx, owner_id=interaction.user.id, channel_id=target.id),
            ephemeral=True,
        )

    @controls.command(name="status", description="Moderator: check whether Vox processing is enabled in a channel")
    async def status(
        interaction: discord.Interaction[discord.Client], channel: ProcessingChannel | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            target_id = channel.id if channel is not None else interaction.channel_id
            if target_id is None:
                raise PermissionError("Choose a server channel.")
            target = await _authorize(interaction, ctx, target_id)
            state = "enabled" if channel_controls.allows_channel(ctx, target) else "disabled"
            await interaction.followup.send(f"Vox processing is {state} in <#{target.id}>.", ephemeral=True)
        except (PermissionError, RuntimeError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    group.add_command(controls)
