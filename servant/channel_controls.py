"""Moderator channel overrides and durable archive cleanup."""

from __future__ import annotations

import asyncio
import logging
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from servant import database

if TYPE_CHECKING:
    from servant.defs import GlobalContext

_LOGGER = logging.getLogger(__name__)
POLICY_URL = "https://github.com/wabbit-corp/python-jeeves/blob/master/PRIVACY.md"
PRIVACY_CONTACT = "wabbit@wabbit.one"


@dataclass
class ChannelControlsState:
    disabled: dict[str, str] = field(default_factory=dict)
    loaded: bool = False


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


def init_schema(conn: database.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS privacy_channel_controls ("
        "channel_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, allowed INTEGER NOT NULL, pending INTEGER NOT NULL)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS privacy_channel_mode (id INTEGER PRIMARY KEY CHECK(id=1))")
    # Replace the previous allowlist guards. Otherwise they continue denying new
    # channels even after the runtime changes to default-enabled behavior.
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'privacy_channel_*'"
    ).fetchall():
        name = str(row[0])
        if "_default_allow_v1_" not in name:
            quoted_name = '"' + name.replace('"', '""') + '"'
            conn.execute(f"DROP TRIGGER {quoted_name}")
    # Enable enforcement when the running application's startup loads its policy.
    # Existing migrations and offline schema inspection can still open a database.
    tables = [
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not str(row[0]).startswith(("sqlite_", "retention_", "privacy_"))
    ]
    for table in tables:
        quoted_table = '"' + table.replace('"', '""') + '"'
        columns = {str(col[1]) for col in conn.execute(f"PRAGMA table_info({quoted_table})")}
        conditions = []
        for column in ("channel_id", "parent_channel_id"):
            if column not in columns:
                continue
            conditions.append(
                f"EXISTS (SELECT 1 FROM privacy_channel_controls WHERE channel_id=NEW.{column} AND (allowed=0 OR pending=1))"
            )
            if "guild_id" in columns:
                conditions.append(
                    f"(NEW.guild_id IS NOT NULL AND EXISTS (SELECT 1 FROM privacy_channel_controls "
                    f"WHERE channel_id=NEW.{column} AND guild_id!=NEW.guild_id))"
                )
                conditions.append(
                    f"(NEW.guild_id IS NULL AND EXISTS(SELECT 1 FROM privacy_channel_controls "
                    f"WHERE channel_id=NEW.{column}))"
                )
                if "channels" in tables:
                    conditions.append(
                        f"(NEW.guild_id IS NULL AND EXISTS(SELECT 1 FROM channels "
                        f"WHERE channel_id=NEW.{column} AND guild_id IS NOT NULL))"
                    )
        if not conditions:
            continue
        for operation in ("INSERT", "UPDATE"):
            name = (
                '"'
                + f"privacy_channel_{table}_{operation.lower()}_default_allow_v1_{len(conditions)}".replace('"', '""')
                + '"'
            )
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {operation} ON {quoted_table} "
                f"WHEN EXISTS(SELECT 1 FROM privacy_channel_mode) AND ({' OR '.join(conditions)}) "
                "BEGIN SELECT RAISE(IGNORE); END"
            )


def _store(ctx: GlobalContext, channel_id: str, guild_id: str, *, allowed: bool, pending: bool) -> None:
    from servant import guild_retention

    paths = guild_retention._paths(ctx)
    # Disable journals first. Enabling becomes authoritative only after every mirror succeeds.
    ordered = (*paths[:-1], paths[-1]) if allowed else (paths[-1], *paths[:-1])
    for path in ordered:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(database.connect(path)) as conn, conn:
            init_schema(conn)
            conn.execute("INSERT OR IGNORE INTO privacy_channel_mode VALUES (1)")
            conn.execute(
                "INSERT OR REPLACE INTO privacy_channel_controls VALUES (?,?,?,?)",
                (channel_id, guild_id, int(allowed), int(pending)),
            )


async def enable(ctx: GlobalContext, channel_id: str, guild_id: str) -> None:
    async with ctx.guild_retention.lock:
        if not ctx.channel_controls.loaded or guild_id in ctx.guild_retention.removed_guilds:
            raise RuntimeError("Vox is still connecting or no longer belongs to this server. Try again later.")
        # Finish any interrupted deletion before a moderator may resume collection.
        from servant import guild_retention

        with closing(database.connect(guild_retention._paths(ctx)[-1])) as conn:
            init_schema(conn)
            row = conn.execute(
                "SELECT pending FROM privacy_channel_controls WHERE channel_id=?", (channel_id,)
            ).fetchone()
        if row and row[0]:
            raise RuntimeError("Archive cleanup is unfinished. Run /vox channel disable again before enabling.")
        await asyncio.to_thread(_store, ctx, channel_id, guild_id, allowed=True, pending=False)
        ctx.channel_controls.disabled.pop(channel_id, None)


def _delete_records(ctx: GlobalContext, channel_id: str, guild_id: str) -> None:
    from servant import guild_retention

    retained_users: set[str] = set()
    for path in guild_retention._paths(ctx):
        with closing(database.connect(path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            guild_retention._purge(conn, guild_id, {channel_id}, retained_users, remove_guild=False)
            for table, columns in guild_retention._tables(conn).items():
                if "user_id" in columns and table != "users":
                    retained_users.update(
                        str(row[0])
                        for row in conn.execute(
                            f"SELECT DISTINCT user_id FROM {guild_retention._quote(table)} WHERE user_id IS NOT NULL"
                        )
                    )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


async def disable(ctx: GlobalContext, channel_id: str, guild_id: str) -> None:
    from servant import voice_transcriber

    async with ctx.guild_retention.lock:
        ctx.channel_controls.disabled[channel_id] = guild_id
        ctx.privacy.generation += 1
        current = asyncio.current_task()
        for task in list(ctx.privacy.processing_tasks):
            if task is not current:
                task.cancel()
        for handle in list(ctx.pending_cancels.values()):
            handle.cancel_event.set()
            if handle.task is not None and handle.task is not current:
                handle.task.cancel()
        ctx.channel_messages.clear()
        ctx.channel_personality.pop(channel_id, None)
        ctx.module_state.pop("url_fetch", None)
        await asyncio.to_thread(_store, ctx, channel_id, guild_id, allowed=False, pending=True)
        await voice_transcriber.discard_channel_audio(ctx, channel_id)
        await asyncio.to_thread(_delete_records, ctx, channel_id, guild_id)
        await asyncio.to_thread(voice_transcriber.purge_channel_transcripts, ctx, channel_id)
        await asyncio.to_thread(_store, ctx, channel_id, guild_id, allowed=False, pending=False)


async def load(ctx: GlobalContext) -> None:
    """Restore explicit overrides and retry deletion before admitting work."""
    from servant import guild_retention

    def restore() -> list[tuple[str, str, int, int]]:
        paths = guild_retention._paths(ctx)
        with closing(database.connect(paths[-1])) as conn:
            init_schema(conn)
            rows = [
                (str(row[0]), str(row[1]), int(row[2]), int(row[3]))
                for row in conn.execute("SELECT channel_id,guild_id,allowed,pending FROM privacy_channel_controls")
            ]
            conn.commit()
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(database.connect(path)) as conn, conn:
                init_schema(conn)
                conn.execute("INSERT OR IGNORE INTO privacy_channel_mode VALUES (1)")
                conn.execute("DELETE FROM privacy_channel_controls")
                conn.executemany("INSERT INTO privacy_channel_controls VALUES (?,?,?,?)", rows)
        return rows

    ctx.channel_controls.loaded = False
    async with ctx.guild_retention.lock:
        rows = await asyncio.to_thread(restore)
        ctx.channel_controls.disabled = {cid: gid for cid, gid, allowed, pending in rows if not allowed or pending}
    for channel_id, guild_id, _allowed, pending in rows:
        if pending:
            await disable(ctx, channel_id, guild_id)
    ctx.channel_controls.loaded = True
    _LOGGER.info(
        "Loaded %s disabled Vox channels; other channels are enabled by default", len(ctx.channel_controls.disabled)
    )
