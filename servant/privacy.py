from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from servant import database as sqlite3

if TYPE_CHECKING:
    from servant.defs import GlobalContext

_LOGGER = logging.getLogger(__name__)
ConfirmOptOut = Callable[[str], Awaitable[None]]


@dataclass
class PrivacyState:
    opted_out_users: set[str] = field(default_factory=set)
    pending_opt_outs: set[str] = field(default_factory=set)
    generation: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    processing_tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def is_opted_out(self, user_id: str | int) -> bool:
        return str(user_id) in self.opted_out_users


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS privacy_opt_outs (user_id TEXT PRIMARY KEY, confirmed_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS privacy_excluded_messages (message_id TEXT PRIMARY KEY, user_id TEXT NOT NULL)"
    )
    for operation in ("INSERT", "UPDATE"):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS privacy_messages_{operation.lower()}
            BEFORE {operation} ON messages
            WHEN EXISTS (SELECT 1 FROM privacy_opt_outs WHERE user_id = NEW.author_id)
              OR EXISTS (SELECT 1 FROM privacy_excluded_messages WHERE message_id = NEW.message_id)
            BEGIN
                INSERT OR IGNORE INTO privacy_excluded_messages (message_id, user_id)
                    SELECT NEW.message_id, NEW.author_id WHERE NEW.author_id IS NOT NULL;
                SELECT RAISE(IGNORE);
            END
            """
        )
        for table in ("users", "guild_members", "guild_member_roles", "llm_request_events"):
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS privacy_{table}_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN EXISTS (SELECT 1 FROM privacy_opt_outs WHERE user_id = NEW.user_id)
                BEGIN SELECT RAISE(IGNORE); END
                """
            )
        for table in (
            "message_attachments",
            "message_embeds",
            "message_reactions",
            "message_user_mentions",
            "message_role_mentions",
            "message_channel_mentions",
        ):
            extra = (
                "OR EXISTS (SELECT 1 FROM privacy_opt_outs WHERE user_id = NEW.user_id)"
                if table == "message_user_mentions"
                else ""
            )
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS privacy_{table}_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN EXISTS (SELECT 1 FROM privacy_excluded_messages WHERE message_id = NEW.message_id) {extra}
                BEGIN SELECT RAISE(IGNORE); END
                """
            )


def confirm_in_database(conn: sqlite3.Connection, user_id: str, now_ms: int) -> None:
    conn.execute("INSERT OR IGNORE INTO privacy_opt_outs VALUES (?, ?)", (user_id, now_ms))
    conn.execute(
        "INSERT OR IGNORE INTO privacy_excluded_messages SELECT message_id, author_id FROM messages WHERE author_id = ?",
        (user_id,),
    )
    channels = conn.execute("SELECT DISTINCT channel_id FROM messages WHERE author_id = ?", (user_id,)).fetchall()
    for table in ("codi_conversation_messages", "codi_conversations", "codi_channel_state"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            conn.executemany(f"DELETE FROM {table} WHERE channel_id = ?", [(row[0],) for row in channels])
    for table in (
        "message_attachments",
        "message_embeds",
        "message_reactions",
        "message_user_mentions",
        "message_role_mentions",
        "message_channel_mentions",
    ):
        conn.execute(
            f"DELETE FROM {table} WHERE message_id IN (SELECT message_id FROM privacy_excluded_messages WHERE user_id = ?)",
            (user_id,),
        )
    conn.execute("DELETE FROM messages WHERE author_id = ?", (user_id,))
    for table in ("users", "guild_members", "guild_member_roles", "llm_request_events", "message_user_mentions"):
        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    conn.execute(
        """UPDATE messages SET reply_to_message_id = NULL, reply_to_channel_id = NULL, reply_to_guild_id = NULL
        WHERE reply_to_message_id IN (SELECT message_id FROM privacy_excluded_messages WHERE user_id = ?)""",
        (user_id,),
    )


def block_feature_records(conn: sqlite3.Connection, user_id: str, *, tables: tuple[str, ...]) -> None:
    """Purge feature records and reject writes from workers that started before opt-out."""
    conn.execute("CREATE TABLE IF NOT EXISTS privacy_blocked_users (user_id TEXT PRIMARY KEY)")
    conn.execute("INSERT OR IGNORE INTO privacy_blocked_users VALUES (?)", (user_id,))
    for table in tables:
        for operation in ("INSERT", "UPDATE"):
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS privacy_{table}_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN EXISTS (SELECT 1 FROM privacy_blocked_users WHERE user_id = NEW.user_id)
                BEGIN SELECT RAISE(IGNORE); END
                """
            )
        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))


class OptOutConfirmation(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        confirm: ConfirmOptOut,
        origin: discord.Interaction[discord.Client] | None = None,
        already_attempted: bool = False,
    ) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self._confirm = confirm
        self._decision_lock = asyncio.Lock()
        self._decided = False
        self._origin = origin
        self._attempted = already_attempted
        self.cancel_button.disabled = already_attempted

    async def on_timeout(self) -> None:
        async with self._decision_lock:
            if self._decided:
                return
            self._decided = True
            self.stop()
            if self._origin is not None:
                description = (
                    "Processing remains paused for your account. Run `/vox optout` again to finish saving your choice."
                    if self._attempted
                    else "No opt-out was confirmed. Run `/vox optout` again to review your choice."
                )
                try:
                    await self._origin.edit_original_response(
                        embed=discord.Embed(title="Confirmation expired", description=description),
                        view=None,
                    )
                except discord.HTTPException:
                    _LOGGER.warning("Could not update expired privacy confirmation", exc_info=True)

    async def interaction_check(self, interaction: discord.Interaction[discord.Client]) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who opened this confirmation can use it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm opt-out", style=discord.ButtonStyle.danger)
    async def confirm_button(
        self, interaction: discord.Interaction[discord.Client], _button: discord.ui.Button[OptOutConfirmation]
    ) -> None:
        async with self._decision_lock:
            if self._decided:
                await interaction.response.send_message("This choice has already been handled.", ephemeral=True)
                return
            await interaction.response.defer()
            self._attempted = True
            self.cancel_button.disabled = True
            try:
                await self._confirm(str(self.user_id))
            except Exception:
                _LOGGER.exception("Could not complete Vox privacy opt-out")
                await interaction.edit_original_response(
                    embed=discord.Embed(
                        title="Confirmation could not be completed",
                        description="Vox has paused processing for your account. Please retry confirmation to finish saving your choice.",
                    ),
                    view=self,
                )
                return
            self._decided = True
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="You have opted out",
                    description="Vox will ignore your messages and voice, and you can no longer use Vox in any server or DM. Your choice is saved across restarts.",
                    color=discord.Color.green(),
                ),
                view=None,
            )
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction[discord.Client], _button: discord.ui.Button[OptOutConfirmation]
    ) -> None:
        async with self._decision_lock:
            if self._decided:
                await interaction.response.send_message("This choice has already been handled.", ephemeral=True)
                return
            if self._attempted:
                await interaction.response.send_message(
                    "Processing is paused for your account. Retry confirmation to finish saving your choice.",
                    ephemeral=True,
                )
                return
            self._decided = True
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="Opt-out cancelled", description="Your Vox privacy settings have not changed."
                ),
                view=None,
            )
            self.stop()


async def show_confirmation(
    interaction: discord.Interaction[discord.Client], ctx: GlobalContext, confirm: ConfirmOptOut
) -> None:
    pending = str(interaction.user.id) in ctx.privacy.pending_opt_outs
    if ctx.privacy.is_opted_out(interaction.user.id) and not pending:
        await interaction.response.send_message(
            embed=discord.Embed(title="You have opted out", description="Vox access is disabled for your account."),
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=discord.Embed(
            title="Finish saving your opt-out?" if pending else "Stop Vox processing your data?",
            description=(
                "Vox will ignore your future messages and voice and remove your messages from its active index. "
                "Your reminders and topic subscriptions will stop.\n\n"
                "**You will lose access to all Vox features across every server and DM.** "
                "There is no command to undo this.\n\n"
                "Vox keeps your Discord user ID to remember this choice. This does not delete messages from Discord "
                "or erase historical logs, backups, or copies already sent to providers."
            ),
            color=discord.Color.orange(),
        ),
        view=OptOutConfirmation(
            user_id=interaction.user.id, confirm=confirm, origin=interaction, already_attempted=pending
        ),
        ephemeral=True,
    )
