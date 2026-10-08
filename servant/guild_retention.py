"""Remove a departed server's active records and fence delayed writes."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from servant import database

if TYPE_CHECKING:
    import discord

    from servant.defs import GlobalContext

_LOGGER = logging.getLogger(__name__)


@dataclass
class GuildRetentionState:
    removed_guilds: set[str] = field(default_factory=set)
    removed_channels: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    transcript_lock: threading.Lock = field(default_factory=threading.Lock)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _tables(conn: database.Connection) -> dict[str, set[str]]:
    return {
        str(row[0]): {str(col[1]) for col in conn.execute(f"PRAGMA table_info({_quote(str(row[0]))})")}
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not str(row[0]).startswith(("sqlite_", "retention_", "privacy_"))
    }


def init_schema(conn: database.Connection) -> None:
    from servant import channel_controls

    channel_controls.init_schema(conn)
    # These contain identifiers only, never content. They also journal unfinished cleanup.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS retention_removed_guilds (guild_id TEXT PRIMARY KEY, pending INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS retention_removed_channels (channel_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL)"
    )
    tables = _tables(conn)
    for table, columns in tables.items():
        conditions: list[str] = []
        signature = 0
        if "guild_id" in columns:
            signature += 1
            conditions.append("EXISTS (SELECT 1 FROM retention_removed_guilds WHERE guild_id=NEW.guild_id)")
        for column in ("channel_id", "parent_channel_id"):
            if column in columns:
                signature += 2 if column == "channel_id" else 4
                conditions.append(f"EXISTS (SELECT 1 FROM retention_removed_channels WHERE channel_id=NEW.{column})")
        if table.startswith("message_") and "message_id" in columns and "messages" in tables:
            signature += 8
            conditions.append("NOT EXISTS (SELECT 1 FROM messages WHERE message_id=NEW.message_id)")
        if not conditions:
            continue
        for operation in ("INSERT", "UPDATE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {_quote(f'retention_{table}_{operation.lower()}_{signature}')} "
                f"BEFORE {operation} ON {_quote(table)} WHEN {' OR '.join(conditions)} "
                "BEGIN SELECT RAISE(IGNORE); END"
            )


def guild_is_removed(conn: database.Connection, guild_id: str | None) -> bool:
    return (
        guild_id is not None
        and conn.execute("SELECT 1 FROM retention_removed_guilds WHERE guild_id=?", (guild_id,)).fetchone() is not None
    )


def _paths(ctx: GlobalContext) -> tuple[Path, ...]:
    from servant.modules import background_indexer, commitment, event_channels, topic_subscriptions

    # The index is last so references in other databases can be considered before deleting profiles.
    return (
        commitment._db_path(ctx),
        topic_subscriptions._db_path(ctx),
        event_channels._db_path(ctx),
        background_indexer._db_path(ctx),
    )


def _channel_ids(conn: database.Connection, guild_id: str) -> set[str]:
    result: set[str] = set()
    for table, columns in _tables(conn).items():
        if "guild_id" not in columns:
            continue
        for column in ("channel_id", "parent_channel_id"):
            if column in columns:
                result.update(
                    str(row[0])
                    for row in conn.execute(
                        f"SELECT DISTINCT {column} FROM {_quote(table)} WHERE guild_id=? AND {column} IS NOT NULL",
                        (guild_id,),
                    )
                )
    result.update(
        str(row[0])
        for row in conn.execute("SELECT channel_id FROM retention_removed_channels WHERE guild_id=?", (guild_id,))
    )
    return result


def _purge(
    conn: database.Connection,
    guild_id: str,
    channels: set[str],
    retained_users: set[str],
    *,
    remove_guild: bool = True,
) -> None:
    init_schema(conn)
    if remove_guild:
        conn.execute("INSERT OR REPLACE INTO retention_removed_guilds VALUES (?, 1)", (guild_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO retention_removed_channels VALUES (?, ?)", [(cid, guild_id) for cid in channels]
        )
        conn.execute("DELETE FROM privacy_channel_controls WHERE guild_id=?", (guild_id,))
    scope_guild_id = guild_id if remove_guild else None
    tables = _tables(conn)
    conn.execute("CREATE TEMP TABLE departing_channels (channel_id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO departing_channels VALUES (?)", [(cid,) for cid in channels])
    candidates: set[str] = set()
    if "messages" in tables:
        conn.execute("CREATE TEMP TABLE departing_messages (message_id TEXT PRIMARY KEY)")
        conn.execute(
            "INSERT INTO departing_messages SELECT message_id FROM messages WHERE guild_id=? OR channel_id IN (SELECT channel_id FROM departing_channels)",
            (scope_guild_id,),
        )
        candidates.update(
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT author_id FROM messages WHERE message_id IN (SELECT message_id FROM departing_messages) AND author_id IS NOT NULL"
            )
        )
        for table, columns in tables.items():
            if table != "messages" and "message_id" in columns:
                conn.execute(
                    f"DELETE FROM {_quote(table)} WHERE message_id IN (SELECT message_id FROM departing_messages)"
                )
        conn.execute(
            "UPDATE messages SET reply_to_message_id=NULL, reply_to_channel_id=NULL, reply_to_guild_id=NULL "
            "WHERE reply_to_guild_id=? OR reply_to_channel_id IN (SELECT channel_id FROM departing_channels) "
            "OR reply_to_message_id IN (SELECT message_id FROM departing_messages)",
            (scope_guild_id,),
        )
    if remove_guild and "guild_members" in tables:
        candidates.update(
            str(row[0]) for row in conn.execute("SELECT user_id FROM guild_members WHERE guild_id=?", (guild_id,))
        )
    # Explicit child deletion also works for historical databases without enabled foreign keys.
    if "event_channel_seen_items" in tables:
        conn.execute(
            "DELETE FROM event_channel_seen_items WHERE subscription_id IN (SELECT id FROM event_channel_subscriptions WHERE guild_id=? OR channel_id IN (SELECT channel_id FROM departing_channels))",
            (scope_guild_id,),
        )
    for table, columns in tables.items():
        clauses: list[str] = []
        params: tuple[str, ...] = ()
        if remove_guild and "guild_id" in columns:
            clauses.append("guild_id=?")
            params = (guild_id,)
        for column in ("channel_id", "parent_channel_id"):
            if column in columns:
                clauses.append(f"{column} IN (SELECT channel_id FROM departing_channels)")
        if clauses:
            conn.execute(f"DELETE FROM {_quote(table)} WHERE {' OR '.join(clauses)}", params)
    if "users" in tables:
        for user_id in candidates - retained_users:
            referenced = any(
                conn.execute(f"SELECT 1 FROM {_quote(table)} WHERE {column}=? LIMIT 1", (user_id,)).fetchone()
                for table, column in (
                    ("messages", "author_id"),
                    ("message_user_mentions", "user_id"),
                    ("guild_members", "user_id"),
                    ("llm_request_events", "user_id"),
                )
            )
            if not referenced:
                conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))


def _delete_records(ctx: GlobalContext, guild_id: str, channels: set[str]) -> set[str]:
    paths = _paths(ctx)
    # First persist the complete channel set in the index. A failure can be retried after restart.
    index = paths[-1]
    index.parent.mkdir(parents=True, exist_ok=True)
    with closing(database.connect(index)) as conn, conn:
        init_schema(conn)
        for path in paths:
            if path.exists() and path != index:
                other = database.connect(path)
                try:
                    init_schema(other)
                    channels.update(_channel_ids(other, guild_id))
                    other.commit()
                finally:
                    other.close()
        channels.update(_channel_ids(conn, guild_id))
        conn.execute("INSERT OR REPLACE INTO retention_removed_guilds VALUES (?, 1)", (guild_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO retention_removed_channels VALUES (?, ?)", [(cid, guild_id) for cid in channels]
        )
    retained_users: set[str] = set()
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = database.connect(path)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            _purge(conn, guild_id, channels, retained_users)
            for table, columns in _tables(conn).items():
                if "user_id" in columns and table != "users":
                    retained_users.update(
                        str(row[0])
                        for row in conn.execute(
                            f"SELECT DISTINCT user_id FROM {_quote(table)} WHERE user_id IS NOT NULL"
                        )
                    )
            conn.commit()
            # Remove deleted payloads from the live WAL; secure_delete is enabled centrally.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    return channels


def _mark_complete(ctx: GlobalContext, guild_id: str) -> None:
    conn = database.connect(_paths(ctx)[-1])
    try:
        conn.execute("UPDATE retention_removed_guilds SET pending=0 WHERE guild_id=?", (guild_id,))
        conn.commit()
    finally:
        conn.close()


async def remove_guild(ctx: GlobalContext, guild_id: str, channels: Iterable[str] = ()) -> None:
    from servant import voice_transcriber

    async with ctx.guild_retention.lock:
        ctx.guild_retention.removed_guilds.add(guild_id)
        ctx.channel_controls.disabled = {
            cid: gid for cid, gid in ctx.channel_controls.disabled.items() if gid != guild_id
        }
        known_channels = set(channels)
        ctx.guild_retention.removed_channels.update(known_channels)
        ctx.privacy.generation += 1
        current = asyncio.current_task()
        for task in list(ctx.privacy.processing_tasks):
            if task is not current:
                task.cancel()
        for handle in list(ctx.pending_cancels.values()):
            handle.cancel_event.set()
            if handle.task is not None and handle.task is not current:
                handle.task.cancel()
        # Conversation context can include retrieved excerpts from other channels.
        ctx.channel_messages.clear()
        ctx.module_state.pop("url_fetch", None)
        try:
            await voice_transcriber.discard_guild_audio(ctx, guild_id)
        except Exception:
            # Losing Discord access can prevent disconnect; local buffers are already dropped.
            _LOGGER.warning("Could not disconnect voice for departed guild %s", guild_id, exc_info=True)
        known_channels = await asyncio.to_thread(_delete_records, ctx, guild_id, known_channels)
        await asyncio.to_thread(voice_transcriber.purge_guild_transcripts, ctx, guild_id)
        ctx.guild_retention.removed_channels.update(known_channels)
        for channel_id in known_channels:
            ctx.channel_personality.pop(channel_id, None)
        # Only report success after both database and transcript cleanup finish.
        await asyncio.to_thread(_mark_complete, ctx, guild_id)
        _LOGGER.info("Removed active records for departed guild %s", guild_id)


def _stored_guilds(ctx: GlobalContext, ownership: dict[str, str]) -> tuple[set[str], set[str]]:
    from servant.modules import commitment

    stored: set[str] = set()
    pending: set[str] = set()
    paths = _paths(ctx)
    for path in paths:
        if not path.exists():
            continue
        conn = database.connect(path)
        try:
            init_schema(conn)
            for table, columns in _tables(conn).items():
                if "guild_id" in columns:
                    stored.update(
                        str(row[0])
                        for row in conn.execute(
                            f"SELECT DISTINCT guild_id FROM {_quote(table)} WHERE guild_id IS NOT NULL"
                        )
                    )
                    if "channel_id" in columns:
                        ownership.update(
                            (str(row[0]), str(row[1]))
                            for row in conn.execute(
                                f"SELECT DISTINCT channel_id, guild_id FROM {_quote(table)} WHERE channel_id IS NOT NULL AND guild_id IS NOT NULL"
                            )
                        )
            rows = conn.execute("SELECT guild_id, pending FROM retention_removed_guilds").fetchall()
            ctx.guild_retention.removed_guilds.update(str(row[0]) for row in rows)
            pending.update(str(row[0]) for row in rows if row[1] and path == paths[-1])
            ctx.guild_retention.removed_channels.update(
                str(row[0]) for row in conn.execute("SELECT channel_id FROM retention_removed_channels")
            )
            conn.commit()
        finally:
            conn.close()
    # Legacy commitments stored only channel IDs. Recover their ownership before
    # deciding which offline removals need cleanup.
    if paths[0].exists():
        conn = database.connect(paths[0])
        try:
            commitment._ensure_db_initialized(paths[0], conn)
            conn.executemany(
                "UPDATE commitments SET guild_id=? WHERE channel_id=? AND guild_id IS NULL",
                [(gid, cid) for cid, gid in ownership.items()],
            )
            stored.update(
                str(row[0])
                for row in conn.execute("SELECT DISTINCT guild_id FROM commitments WHERE guild_id IS NOT NULL")
            )
            conn.commit()
        finally:
            conn.close()
    return stored, pending


async def reconcile(ctx: GlobalContext, guilds: Callable[[], Iterable[discord.Guild]]) -> None:
    """Call only after READY: unavailable guilds remain members and must be kept."""
    ownership = {
        str(channel.id): str(guild.id)
        for guild in guilds()
        for channel in [*getattr(guild, "channels", ()), *getattr(guild, "threads", ())]
    }
    async with ctx.guild_retention.lock:
        stored, pending = await asyncio.to_thread(_stored_guilds, ctx, ownership)
    active = {str(guild.id) for guild in guilds()}
    for guild_id in (stored - active) | pending:
        await remove_guild(ctx, guild_id)
    active = {str(guild.id) for guild in guilds()}
    for guild_id in active & ctx.guild_retention.removed_guilds:

        def still_installed(gid: str = guild_id) -> bool:
            return gid in {str(g.id) for g in guilds()}

        await allow_guild(ctx, guild_id, still_installed=still_installed)
    from servant import channel_controls

    await channel_controls.load(ctx)
    ctx.guild_retention.ready.set()


async def allow_guild(ctx: GlobalContext, guild_id: str, *, still_installed: Callable[[], bool] | None = None) -> None:
    async with ctx.guild_retention.lock:
        if still_installed is not None and not still_installed():
            return

        def _allow() -> set[str]:
            channels: set[str] = set()
            for path in _paths(ctx):
                if not path.exists():
                    continue
                conn = database.connect(path)
                try:
                    init_schema(conn)
                    channels.update(_channel_ids(conn, guild_id))
                    conn.execute("DELETE FROM retention_removed_channels WHERE guild_id=?", (guild_id,))
                    conn.execute("DELETE FROM retention_removed_guilds WHERE guild_id=?", (guild_id,))
                    conn.commit()
                finally:
                    conn.close()
            return channels

        channels = await asyncio.to_thread(_allow)
        ctx.guild_retention.removed_guilds.discard(guild_id)
        ctx.guild_retention.removed_channels.difference_update(channels)
