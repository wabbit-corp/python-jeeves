from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import discord
from discord.http import Route

from servant.defs import RoutineTask, GlobalContext, ToolDef
from servant.json import JSON, JSONDict

_LOGGER = logging.getLogger(__name__)

DEFAULT_DB_FILENAME = "servant_index.sqlite3"
DEFAULT_MESSAGE_BATCH_SIZE = 50
DEFAULT_MEMBER_BATCH_SIZE = 100
DEFAULT_BACKFILL_CHANNELS_PER_TICK = 1
DEFAULT_TAIL_CHANNELS_PER_TICK = 1
DEFAULT_PINS_CHANNELS_PER_TICK = 1
DEFAULT_RUN_EVERY_SECONDS = 20
DEFAULT_SEARCH_EVERY_SECONDS = 600
DEFAULT_SEARCH_CHANNELS_PER_TICK = 1
DEFAULT_FETCH_FORBIDDEN_BACKOFF_SECONDS = 60
MAX_FETCH_FORBIDDEN_BACKOFF_SECONDS = 6 * 60 * 60
DEFAULT_CHANNEL_DISABLE_DAYS = 7
DEFAULT_GUILD_RESCAN_INTERVAL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_PINS_CHECK_EVERY_SECONDS = 6 * 60 * 60
DEFAULT_ARCHIVED_THREADS_FORBIDDEN_BACKOFF_DAYS = 5
DISCORD_EPOCH_MS = 1420070400000
DEFAULT_SEARCH_MAX_RESULTS = 200
MAX_SEARCH_MAX_RESULTS = 5000
DEFAULT_THREAD_DISCOVERY_RUN_EVERY_SECONDS = 300


def _db_path(ctx: GlobalContext) -> Path:
    raw = ctx.secrets.get("indexer_db_path")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / DEFAULT_DB_FILENAME).resolve()


def _connect(dbfile: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ensure_guild_members_left_at(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(guild_members)")
    }
    if "left_at" not in columns:
        conn.execute("ALTER TABLE guild_members ADD COLUMN left_at INTEGER;")


def _ensure_guild_members_last_seen_at(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(guild_members)")
    }
    if "last_seen_at" not in columns:
        conn.execute("ALTER TABLE guild_members ADD COLUMN last_seen_at INTEGER;")
        conn.execute(
            """
            UPDATE guild_members
            SET last_seen_at = updated_at
            WHERE last_seen_at IS NULL
            """
        )


def _ensure_messages_deleted_at(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "deleted_at" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN deleted_at INTEGER;")


def _ensure_messages_content_available(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "content_available" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN content_available INTEGER;")
        conn.execute(
            """
            UPDATE messages
            SET content_available = CASE WHEN content IS NULL THEN 0 ELSE 1 END
            WHERE content_available IS NULL
            """
        )


def _ensure_messages_mention_everyone(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "mention_everyone" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN mention_everyone INTEGER;")
        conn.execute(
            """
            UPDATE messages
            SET mention_everyone = 0
            WHERE mention_everyone IS NULL
            """
        )


def _ensure_channel_state_error_fields(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(channel_state)")}
    if "error_count" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN error_count INTEGER DEFAULT 0;"
        )
        conn.execute(
            "UPDATE channel_state SET error_count = 0 WHERE error_count IS NULL"
        )
    if "last_error" not in columns:
        conn.execute("ALTER TABLE channel_state ADD COLUMN last_error TEXT;")


def _ensure_channel_state_thread_backoff(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(channel_state)")}
    if "archived_threads_forbidden_until" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN archived_threads_forbidden_until INTEGER;"
        )
    if "archived_threads_error_count" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN archived_threads_error_count INTEGER DEFAULT 0;"
        )
        conn.execute(
            """
            UPDATE channel_state
            SET archived_threads_error_count = 0
            WHERE archived_threads_error_count IS NULL
            """
        )
    if "archived_threads_last_error" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN archived_threads_last_error TEXT;"
        )
    if "archived_threads_last_error_at" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN archived_threads_last_error_at INTEGER;"
        )


def _ensure_channel_state_disabled_fields(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(channel_state)")}
    if "disabled_until" not in columns:
        conn.execute("ALTER TABLE channel_state ADD COLUMN disabled_until INTEGER;")
    if "disabled_reason" not in columns:
        conn.execute("ALTER TABLE channel_state ADD COLUMN disabled_reason TEXT;")


def _ensure_channel_state_fetch_backoff(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(channel_state)")}
    if "fetch_forbidden_until" not in columns:
        conn.execute("ALTER TABLE channel_state ADD COLUMN fetch_forbidden_until INTEGER;")
    if "fetch_error_count" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN fetch_error_count INTEGER DEFAULT 0;"
        )
        conn.execute(
            "UPDATE channel_state SET fetch_error_count = 0 WHERE fetch_error_count IS NULL"
        )


def _ensure_channel_state_search_fields(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(channel_state)")}
    if "search_before_id" not in columns:
        conn.execute("ALTER TABLE channel_state ADD COLUMN search_before_id TEXT;")
    if "search_done" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN search_done INTEGER DEFAULT 0;"
        )
        conn.execute(
            "UPDATE channel_state SET search_done = 0 WHERE search_done IS NULL"
        )
    if "search_last_indexed_at" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN search_last_indexed_at INTEGER;"
        )
    if "search_error_count" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN search_error_count INTEGER DEFAULT 0;"
        )
        conn.execute(
            "UPDATE channel_state SET search_error_count = 0 WHERE search_error_count IS NULL"
        )
    if "search_last_error" not in columns:
        conn.execute("ALTER TABLE channel_state ADD COLUMN search_last_error TEXT;")
    if "search_forbidden_until" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN search_forbidden_until INTEGER;"
        )
    if "search_forbidden_count" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN search_forbidden_count INTEGER DEFAULT 0;"
        )
        conn.execute(
            "UPDATE channel_state SET search_forbidden_count = 0 WHERE search_forbidden_count IS NULL"
        )


def _ensure_channel_state_pins_fields(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(channel_state)")}
    if "pins_last_checked_at" not in columns:
        conn.execute(
            "ALTER TABLE channel_state ADD COLUMN pins_last_checked_at INTEGER;"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_channel_state_pins_check "
        "ON channel_state(pins_last_checked_at);"
    )


def _ensure_guild_state_scan_started_at(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(guild_state)")}
    if "scan_started_at" not in columns:
        conn.execute("ALTER TABLE guild_state ADD COLUMN scan_started_at INTEGER;")


def _ensure_guild_state_scan_completed_at(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(guild_state)")}
    if "scan_completed_at" not in columns:
        conn.execute("ALTER TABLE guild_state ADD COLUMN scan_completed_at INTEGER;")


def _ensure_guild_state_error_fields(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(guild_state)")}
    if "error_count" not in columns:
        conn.execute(
            "ALTER TABLE guild_state ADD COLUMN error_count INTEGER DEFAULT 0;"
        )
        conn.execute(
            "UPDATE guild_state SET error_count = 0 WHERE error_count IS NULL"
        )
    if "last_error" not in columns:
        conn.execute("ALTER TABLE guild_state ADD COLUMN last_error TEXT;")


def _ensure_channels_extra_json(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(channels)")}
    if "extra_json" not in columns:
        conn.execute("ALTER TABLE channels ADD COLUMN extra_json TEXT;")


def _migrate_timestamp_columns(conn: sqlite3.Connection) -> None:
    tables = {
        "guilds": ["created_at", "updated_at"],
        "channels": ["created_at", "updated_at"],
        "users": ["created_at", "updated_at"],
        "guild_members": [
            "joined_at",
            "premium_since",
            "left_at",
            "last_seen_at",
            "updated_at",
        ],
        "messages": ["created_at", "edited_at", "deleted_at"],
        "message_attachments": ["updated_at"],
        "message_embeds": ["updated_at"],
        "channel_state": [
            "last_indexed_at",
            "updated_at",
            "disabled_until",
            "fetch_forbidden_until",
            "search_forbidden_until",
            "search_last_indexed_at",
            "pins_last_checked_at",
            "archived_threads_forbidden_until",
            "archived_threads_last_error_at",
        ],
        "guild_state": [
            "scan_started_at",
            "scan_completed_at",
            "last_indexed_at",
            "updated_at",
        ],
        "roles": ["updated_at", "deleted_at"],
        "guild_emojis": ["updated_at"],
        "guild_stickers": ["updated_at"],
        "message_reactions": ["updated_at"],
        "thread_parent_state": ["public_before_ts", "private_before_ts", "updated_at"],
    }

    for table, columns in tables.items():
        for column in columns:
            rows = conn.execute(
                f"""
                SELECT rowid, {column}
                FROM {table}
                WHERE {column} IS NOT NULL
                  AND typeof({column}) IN ('text', 'real')
                """
            ).fetchall()
            for row in rows:
                ms = _parse_epoch_ms(row[column])
                if ms is None:
                    continue
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                    (ms, row["rowid"]),
                )


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guilds (
            guild_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            owner_id TEXT,
            member_count INTEGER,
            description TEXT,
            icon_url TEXT,
            created_at INTEGER,
            updated_at INTEGER
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            guild_id TEXT,
            name TEXT,
            type TEXT,
            position INTEGER,
            parent_id TEXT,
            topic TEXT,
            nsfw INTEGER,
            slowmode_delay INTEGER,
            created_at INTEGER,
            updated_at INTEGER,
            extra_json TEXT
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_channels_guild_id ON channels(guild_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            name TEXT,
            discriminator TEXT,
            global_name TEXT,
            bot INTEGER,
            system INTEGER,
            avatar_url TEXT,
            created_at INTEGER,
            updated_at INTEGER
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_members (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            nick TEXT,
            joined_at INTEGER,
            roles TEXT,
            pending INTEGER,
            premium_since INTEGER,
            mute INTEGER,
            deaf INTEGER,
            avatar_url TEXT,
            left_at INTEGER,
            last_seen_at INTEGER,
            updated_at INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guild_members_user_id "
        "ON guild_members(user_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            channel_id TEXT,
            guild_id TEXT,
            author_id TEXT,
            content TEXT,
            content_available INTEGER,
            created_at INTEGER,
            edited_at INTEGER,
            deleted_at INTEGER,
            mention_ids TEXT,
            mention_everyone INTEGER,
            attachments_count INTEGER,
            embeds_count INTEGER,
            pinned INTEGER
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_channel_id "
        "ON messages(channel_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_guild_id "
        "ON messages(guild_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_author_id "
        "ON messages(author_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_channel_created "
        "ON messages(channel_id, created_at);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_guild_created "
        "ON messages(guild_id, created_at);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_author_created "
        "ON messages(author_id, created_at);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_deleted_at "
        "ON messages(deleted_at);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_pinned "
        "ON messages(channel_id, pinned);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_user_mentions (
            message_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            PRIMARY KEY (message_id, user_id),
            FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_user_mentions_user_id "
        "ON message_user_mentions(user_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_role_mentions (
            message_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            PRIMARY KEY (message_id, role_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_role_mentions_role "
        "ON message_role_mentions(role_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_channel_mentions (
            message_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            PRIMARY KEY (message_id, channel_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_channel_mentions_channel "
        "ON message_channel_mentions(channel_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_attachments (
            attachment_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            guild_id TEXT,
            filename TEXT,
            description TEXT,
            content_type TEXT,
            size INTEGER,
            url TEXT NOT NULL,
            proxy_url TEXT,
            height INTEGER,
            width INTEGER,
            ephemeral INTEGER,
            updated_at INTEGER,
            FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_attachments_channel "
        "ON message_attachments(channel_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_attachments_guild "
        "ON message_attachments(guild_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_attachments_message_id "
        "ON message_attachments(message_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_embeds (
            message_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            embed_json TEXT NOT NULL,
            updated_at INTEGER,
            PRIMARY KEY (message_id, idx),
            FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_embeds_message_id "
        "ON message_embeds(message_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_state (
            channel_id TEXT PRIMARY KEY,
            guild_id TEXT,
            last_before_id TEXT,
            latest_seen_id TEXT,
            backfill_done INTEGER DEFAULT 0,
            last_indexed_at INTEGER,
            updated_at INTEGER,
            error_count INTEGER DEFAULT 0,
            last_error TEXT,
            disabled_until INTEGER,
            disabled_reason TEXT,
            fetch_forbidden_until INTEGER,
            fetch_error_count INTEGER DEFAULT 0,
            search_before_id TEXT,
            search_done INTEGER DEFAULT 0,
            search_last_indexed_at INTEGER,
            search_error_count INTEGER DEFAULT 0,
            search_last_error TEXT,
            search_forbidden_until INTEGER,
            search_forbidden_count INTEGER DEFAULT 0,
            pins_last_checked_at INTEGER,
            archived_threads_forbidden_until INTEGER,
            archived_threads_error_count INTEGER DEFAULT 0,
            archived_threads_last_error TEXT,
            archived_threads_last_error_at INTEGER
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_channel_state_backfill "
        "ON channel_state(backfill_done, last_indexed_at);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_state (
            guild_id TEXT PRIMARY KEY,
            member_after_id TEXT,
            member_latest_id TEXT,
            scan_started_at INTEGER,
            scan_completed_at INTEGER,
            backfill_done INTEGER DEFAULT 0,
            last_indexed_at INTEGER,
            updated_at INTEGER,
            error_count INTEGER DEFAULT 0,
            last_error TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS roles (
            role_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            name TEXT,
            color INTEGER,
            hoist INTEGER,
            position INTEGER,
            permissions TEXT,
            managed INTEGER,
            mentionable INTEGER,
            icon_url TEXT,
            unicode_emoji TEXT,
            updated_at INTEGER,
            deleted_at INTEGER
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_roles_guild_id ON roles(guild_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_member_roles (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, role_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guild_member_roles_role "
        "ON guild_member_roles(guild_id, role_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_emojis (
            emoji_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            name TEXT,
            animated INTEGER,
            available INTEGER,
            managed INTEGER,
            require_colons INTEGER,
            updated_at INTEGER
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guild_emojis_guild_id ON guild_emojis(guild_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_stickers (
            sticker_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            name TEXT,
            description TEXT,
            tags TEXT,
            format_type INTEGER,
            available INTEGER,
            updated_at INTEGER
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guild_stickers_guild_id ON guild_stickers(guild_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_reactions (
            message_id TEXT NOT NULL,
            emoji_key TEXT NOT NULL,
            count INTEGER NOT NULL,
            me INTEGER,
            updated_at INTEGER,
            PRIMARY KEY (message_id, emoji_key),
            FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_reactions_message_id "
        "ON message_reactions(message_id);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_parent_state (
            parent_channel_id TEXT PRIMARY KEY,
            guild_id TEXT,
            public_before_ts INTEGER,
            private_before_ts INTEGER,
            public_done INTEGER DEFAULT 0,
            private_done INTEGER DEFAULT 0,
            updated_at INTEGER,
            error_count INTEGER DEFAULT 0,
            last_error TEXT
        );
        """
    )
    _ensure_guild_members_left_at(conn)
    _ensure_guild_members_last_seen_at(conn)
    _ensure_messages_deleted_at(conn)
    _ensure_messages_content_available(conn)
    _ensure_messages_mention_everyone(conn)
    _ensure_channel_state_error_fields(conn)
    _ensure_channel_state_thread_backoff(conn)
    _ensure_channel_state_disabled_fields(conn)
    _ensure_channel_state_fetch_backoff(conn)
    _ensure_channel_state_search_fields(conn)
    _ensure_channel_state_pins_fields(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_channel_state_search "
        "ON channel_state(search_done, search_last_indexed_at);"
    )
    _ensure_guild_state_scan_started_at(conn)
    _ensure_guild_state_scan_completed_at(conn)
    _ensure_guild_state_error_fields(conn)
    _ensure_channels_extra_json(conn)
    _migrate_timestamp_columns(conn)
    conn.commit()


async def init_db(ctx: GlobalContext) -> None:
    if ctx._db_initialized:
        return

    dbfile = _db_path(ctx)

    def _run():
        dbfile.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(dbfile)
        try:
            _init_db(conn)
        finally:
            conn.close()

    await asyncio.to_thread(_run)
    ctx._db_initialized = True


async def _with_db(ctx: GlobalContext, fn):
    if not ctx._db_initialized:
        await init_db(ctx)

    dbfile = _db_path(ctx)

    def _run():
        dbfile.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(dbfile)
        try:
            result = fn(conn)
            if conn.in_transaction:
                conn.commit()
            return result
        finally:
            conn.close()

    return await asyncio.to_thread(_run)


def _now_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


def _to_epoch_ms(ts: Optional[dt.datetime]) -> Optional[int]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    else:
        ts = ts.astimezone(dt.timezone.utc)
    return int(ts.timestamp() * 1000)


def _parse_epoch_ms(value: Optional[Any]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            try:
                i = int(text)
            except ValueError:
                return None
            if -10_000_000_000 < i < 10_000_000_000:
                return i * 1000
            return i
        try:
            if text.endswith("Z"):
                text = text[:-1]
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        else:
            parsed = parsed.astimezone(dt.timezone.utc)
        return int(parsed.timestamp() * 1000)
    return None


def _coerce_snowflake(value: Optional[Any], field: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(int(str(value)))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a valid snowflake id.")


def _snowflake_timestamp_ms(value: Optional[Any]) -> Optional[int]:
    if value is None:
        return None
    try:
        snowflake = int(str(value))
    except (TypeError, ValueError):
        return None
    return (snowflake >> 22) + DISCORD_EPOCH_MS


def _snowflake_from_epoch_ms(value: int, *, high: bool = False) -> int:
    if value < DISCORD_EPOCH_MS:
        raise ValueError("timestamp must be after Discord epoch.")
    base = (value - DISCORD_EPOCH_MS) << 22
    if high:
        return base + (1 << 22) - 1
    return base


def _bool_int_or_none(value: Optional[bool]) -> Optional[int]:
    if value is None:
        return None
    return int(bool(value))


def _get_int_config(ctx: GlobalContext, key: str, default: int) -> int:
    raw = ctx.secrets.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _guild_row(
    guild: discord.Guild, now: int
) -> Tuple[
    str,
    str,
    Optional[str],
    Optional[int],
    Optional[str],
    Optional[str],
    Optional[int],
    int,
]:
    icon_url = None
    if guild.icon:
        icon_url = str(guild.icon.url)
    description = getattr(guild, "description", None)
    owner_id = str(guild.owner_id) if guild.owner_id is not None else None
    member_count = getattr(guild, "member_count", None)
    created_at = _to_epoch_ms(getattr(guild, "created_at", None))
    return (
        str(guild.id),
        guild.name,
        owner_id,
        member_count,
        description,
        icon_url,
        created_at,
        now,
    )


def _channel_row(
    channel: discord.abc.GuildChannel, guild_id: int, now: int
) -> Tuple[
    str,
    str,
    str,
    str,
    Optional[int],
    Optional[str],
    Optional[str],
    Optional[int],
    Optional[int],
    Optional[int],
    int,
    Optional[str],
]:
    type_name = getattr(getattr(channel, "type", None), "name", None)
    if not type_name:
        type_name = str(getattr(channel, "type", "unknown"))
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is None:
        parent_id = getattr(channel, "category_id", None)
    parent_id_str = str(parent_id) if parent_id is not None else None
    topic = getattr(channel, "topic", None)
    nsfw = getattr(channel, "nsfw", None)
    if nsfw is not None:
        nsfw = int(bool(nsfw))
    slowmode = getattr(channel, "slowmode_delay", None)
    position = getattr(channel, "position", None)
    created_at = _to_epoch_ms(getattr(channel, "created_at", None))
    extra: Dict[str, Any] = {}
    if isinstance(channel, discord.Thread):
        extra["archived"] = bool(getattr(channel, "archived", False))
        extra["locked"] = bool(getattr(channel, "locked", False))
        extra["auto_archive_duration"] = getattr(
            channel, "auto_archive_duration", None
        )
        extra["archive_timestamp"] = _to_epoch_ms(
            getattr(channel, "archive_timestamp", None)
        )
        owner_id = getattr(channel, "owner_id", None)
        if owner_id is not None:
            extra["owner_id"] = str(owner_id)
        extra["message_count"] = getattr(channel, "message_count", None)
        extra["member_count"] = getattr(channel, "member_count", None)
        invitable = getattr(channel, "invitable", None)
        if invitable is not None:
            extra["invitable"] = bool(invitable)
        applied_tags = getattr(channel, "applied_tags", None)
        if applied_tags:
            extra["applied_tags"] = [
                str(tag.id)
                for tag in applied_tags
                if getattr(tag, "id", None) is not None
            ]
    extra_json = json.dumps(extra, ensure_ascii=True) if extra else None
    return (
        str(channel.id),
        str(guild_id),
        channel.name,
        type_name,
        position,
        parent_id_str,
        topic,
        nsfw,
        slowmode,
        created_at,
        now,
        extra_json,
    )


def _user_row(
    user: discord.abc.User, now: int
) -> Tuple[
    str,
    str,
    str,
    Optional[str],
    int,
    int,
    Optional[str],
    Optional[int],
    int,
]:
    avatar_url = None
    try:
        avatar_url = str(user.display_avatar.url)
    except Exception:
        avatar_url = None
    created_at = _to_epoch_ms(getattr(user, "created_at", None))
    return (
        str(user.id),
        user.name,
        str(getattr(user, "discriminator", "")),
        getattr(user, "global_name", None),
        int(bool(getattr(user, "bot", False))),
        int(bool(getattr(user, "system", False))),
        avatar_url,
        created_at,
        now,
    )


def _user_row_from_payload(
    payload: Any, now: int
) -> Optional[
    Tuple[
        str,
        str,
        str,
        Optional[str],
        int,
        int,
        Optional[str],
        Optional[int],
        int,
    ]
]:
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("id")
    if user_id is None:
        return None
    user_id_str = str(user_id)
    name = payload.get("username") or payload.get("name") or ""
    discriminator = payload.get("discriminator")
    if discriminator is None:
        discriminator = ""
    global_name = payload.get("global_name")
    bot = int(bool(payload.get("bot", False)))
    system = int(bool(payload.get("system", False)))
    avatar = payload.get("avatar")
    avatar_url = None
    if avatar:
        ext = "gif" if str(avatar).startswith("a_") else "png"
        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{user_id_str}/{avatar}.{ext}"
        )
    created_at = _snowflake_timestamp_ms(user_id_str)
    return (
        user_id_str,
        name,
        str(discriminator),
        global_name,
        bot,
        system,
        avatar_url,
        created_at,
        now,
    )


def _role_row(
    role: discord.Role, now: int
) -> Tuple[
    str,
    str,
    str,
    int,
    int,
    int,
    Optional[str],
    int,
    int,
    Optional[str],
    Optional[str],
    int,
    Optional[int],
]:
    icon_url = None
    role_icon = getattr(role, "icon", None)
    if role_icon:
        try:
            icon_url = str(role_icon.url)
        except Exception:
            icon_url = None
    permissions = getattr(role, "permissions", None)
    permissions_value: Optional[str] = None
    if permissions is not None:
        permissions_value = str(getattr(permissions, "value", permissions))
    return (
        str(role.id),
        str(role.guild.id),
        role.name,
        int(role.color),
        int(bool(role.hoist)),
        int(role.position),
        permissions_value,
        int(bool(role.managed)),
        int(bool(role.mentionable)),
        icon_url,
        getattr(role, "unicode_emoji", None),
        now,
        None,
    )


def _emoji_row(
    emoji: discord.Emoji, guild_id: str, now: int
) -> Tuple[
    str,
    str,
    str,
    int,
    int,
    int,
    int,
    int,
]:
    return (
        str(emoji.id),
        guild_id,
        emoji.name,
        int(bool(getattr(emoji, "animated", False))),
        int(bool(getattr(emoji, "available", True))),
        int(bool(getattr(emoji, "managed", False))),
        int(bool(getattr(emoji, "require_colons", True))),
        now,
    )


def _sticker_row(
    sticker: discord.StickerItem, guild_id: str, now: int
) -> Tuple[
    str,
    str,
    str,
    Optional[str],
    Optional[str],
    Optional[int],
    int,
    int,
]:
    format_type = getattr(sticker, "format_type", None)
    format_value = None
    if format_type is not None:
        format_value = int(getattr(format_type, "value", format_type))
    return (
        str(sticker.id),
        guild_id,
        sticker.name,
        getattr(sticker, "description", None),
        getattr(sticker, "tags", None),
        format_value,
        int(bool(getattr(sticker, "available", True))),
        now,
    )


def _emoji_key_from_parts(name: Optional[str], emoji_id: Optional[Any]) -> Optional[str]:
    if emoji_id is not None:
        try:
            emoji_id = int(emoji_id)
        except (TypeError, ValueError):
            emoji_id = None
    if emoji_id is not None:
        if name:
            return f"{name}:{emoji_id}"
        return str(emoji_id)
    if name:
        return f":{name}:"
    return None


def _emoji_key(emoji: Any) -> Optional[str]:
    if isinstance(emoji, str):
        return f":{emoji}:"
    emoji_id = getattr(emoji, "id", None)
    name = getattr(emoji, "name", None)
    return _emoji_key_from_parts(name, emoji_id) or str(emoji)


def _reaction_rows(message: discord.Message, now: int) -> List[Tuple[str, str, int, int, int]]:
    rows: List[Tuple[str, str, int, int, int]] = []
    message_id = str(message.id)
    for reaction in getattr(message, "reactions", []) or []:
        emoji_key = _emoji_key(getattr(reaction, "emoji", None))
        if not emoji_key:
            continue
        count = int(getattr(reaction, "count", 0) or 0)
        me = int(bool(getattr(reaction, "me", False)))
        rows.append((message_id, emoji_key, count, me, now))
    return rows


def _reaction_rows_from_payload(
    *, message_id: str, reactions: Any, now: int
) -> List[Tuple[str, str, int, int, int]]:
    rows: List[Tuple[str, str, int, int, int]] = []
    if not isinstance(reactions, list):
        return rows
    for reaction in reactions:
        if not isinstance(reaction, dict):
            continue
        emoji = reaction.get("emoji") or {}
        if not isinstance(emoji, dict):
            emoji = {}
        emoji_key = _emoji_key_from_parts(
            emoji.get("name"), emoji.get("id")
        )
        if not emoji_key:
            continue
        count = reaction.get("count") or 0
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        me = int(bool(reaction.get("me", False)))
        rows.append((message_id, emoji_key, count, me, now))
    return rows


def _member_row(
    member: discord.Member, now: int, left_at: Optional[int] = None
) -> Tuple[
    str,
    str,
    Optional[str],
    Optional[int],
    str,
    int,
    Optional[int],
    int,
    int,
    Optional[str],
    Optional[int],
    Optional[int],
    int,
]:
    roles = [str(role.id) for role in member.roles]
    avatar = getattr(member, "avatar", None)
    avatar_url = str(avatar.url) if avatar else None
    return (
        str(member.guild.id),
        str(member.id),
        member.nick,
        _to_epoch_ms(member.joined_at),
        json.dumps(roles, ensure_ascii=True),
        int(bool(getattr(member, "pending", False))),
        _to_epoch_ms(getattr(member, "premium_since", None)),
        int(bool(getattr(member, "mute", False))),
        int(bool(getattr(member, "deaf", False))),
        avatar_url,
        left_at,
        now,
        now,
    )


def _message_row(
    message: discord.Message, now: int
) -> Tuple[
    str,
    str,
    Optional[str],
    str,
    Optional[str],
    int,
    Optional[int],
    Optional[int],
    str,
    int,
    int,
    int,
    int,
]:
    guild_id = str(message.guild.id) if message.guild else None
    mention_ids = _dedupe_ids([user.id for user in message.mentions])
    content = getattr(message, "content", None)
    content_available = 0 if content is None else 1
    return (
        str(message.id),
        str(message.channel.id),
        guild_id,
        str(message.author.id),
        content,
        content_available,
        _to_epoch_ms(message.created_at),
        _to_epoch_ms(message.edited_at),
        json.dumps(mention_ids, ensure_ascii=True),
        int(bool(getattr(message, "mention_everyone", False))),
        len(message.attachments),
        len(message.embeds),
        int(bool(message.pinned)),
    )


def _message_row_from_payload(
    payload: Any, *, channel_id: Optional[str], guild_id: Optional[str]
) -> Optional[
    Tuple[
        str,
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
        int,
        Optional[int],
        Optional[int],
        str,
        int,
        int,
        int,
        int,
    ]
]:
    if not isinstance(payload, dict):
        return None
    message_id = payload.get("id")
    if message_id is None:
        return None
    message_id_str = str(message_id)
    payload_channel_id = payload.get("channel_id")
    payload_guild_id = payload.get("guild_id")
    resolved_channel_id = (
        str(payload_channel_id)
        if payload_channel_id is not None
        else channel_id
    )
    resolved_guild_id = (
        str(payload_guild_id) if payload_guild_id is not None else guild_id
    )
    author = payload.get("author")
    author_id = None
    if isinstance(author, dict) and author.get("id") is not None:
        author_id = str(author.get("id"))
    content = payload.get("content")
    content_available = 0 if content is None else 1
    created_at = _parse_epoch_ms(payload.get("timestamp"))
    if created_at is None:
        created_at = _snowflake_timestamp_ms(message_id_str)
    edited_at = _parse_epoch_ms(payload.get("edited_timestamp"))
    mentions = payload.get("mentions") or []
    mention_ids = _extract_ids_from_payload(mentions)
    mention_everyone = int(bool(payload.get("mention_everyone", False)))
    attachments = payload.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []
    embeds = payload.get("embeds") or []
    if not isinstance(embeds, list):
        embeds = []
    pinned = int(bool(payload.get("pinned", False)))
    return (
        message_id_str,
        resolved_channel_id,
        resolved_guild_id,
        author_id,
        content,
        content_available,
        created_at,
        edited_at,
        json.dumps(mention_ids, ensure_ascii=True),
        mention_everyone,
        len(attachments),
        len(embeds),
        pinned,
    )


def _dedupe_ids(values: List[Any]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for value in values:
        if value is None:
            continue
        value_str = str(value)
        if value_str in seen:
            continue
        seen.add(value_str)
        result.append(value_str)
    return result


def _id_rows(message_id: str, values: List[Any]) -> List[Tuple[str, str]]:
    return [(message_id, value) for value in _dedupe_ids(values)]


def _extract_ids_from_payload(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    extracted: List[Any] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("id")
        if value is None:
            continue
        extracted.append(value)
    return _dedupe_ids(extracted)


def _user_mention_rows(message: discord.Message) -> List[Tuple[str, str]]:
    return _id_rows(str(message.id), [user.id for user in message.mentions])


def _role_mention_rows(message: discord.Message) -> List[Tuple[str, str]]:
    return _id_rows(str(message.id), [role.id for role in message.role_mentions])


def _channel_mention_rows(message: discord.Message) -> List[Tuple[str, str]]:
    return _id_rows(str(message.id), [channel.id for channel in message.channel_mentions])


def _embed_json(embed: Any) -> str:
    payload: Any
    if hasattr(embed, "to_dict"):
        payload = embed.to_dict()
    else:
        payload = embed
    return json.dumps(payload, ensure_ascii=True, default=str)


def _attachment_rows(message: discord.Message, now: int) -> List[Tuple]:
    guild_id = str(message.guild.id) if message.guild else None
    channel_id = str(message.channel.id)
    message_id = str(message.id)
    rows: List[Tuple] = []
    for attachment in message.attachments:
        attachment_id = getattr(attachment, "id", None)
        url = getattr(attachment, "url", None)
        if attachment_id is None or not url:
            continue
        proxy_url = getattr(attachment, "proxy_url", None)
        rows.append(
            (
                str(attachment_id),
                message_id,
                channel_id,
                guild_id,
                getattr(attachment, "filename", None),
                getattr(attachment, "description", None),
                getattr(attachment, "content_type", None),
                getattr(attachment, "size", None),
                str(url),
                str(proxy_url) if proxy_url else None,
                getattr(attachment, "height", None),
                getattr(attachment, "width", None),
                _bool_int_or_none(getattr(attachment, "ephemeral", None)),
                now,
            )
        )
    return rows


def _attachment_rows_from_payload(
    *,
    message_id: str,
    channel_id: Optional[str],
    guild_id: Optional[str],
    attachments: List[Any],
    now: int,
) -> List[Tuple]:
    if channel_id is None:
        return []
    rows: List[Tuple] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_id = attachment.get("id")
        url = attachment.get("url")
        if attachment_id is None or not url:
            continue
        size = attachment.get("size")
        if size is not None:
            try:
                size = int(size)
            except (TypeError, ValueError):
                size = None
        height = attachment.get("height")
        if height is not None:
            try:
                height = int(height)
            except (TypeError, ValueError):
                height = None
        width = attachment.get("width")
        if width is not None:
            try:
                width = int(width)
            except (TypeError, ValueError):
                width = None
        proxy_url = attachment.get("proxy_url")
        rows.append(
            (
                str(attachment_id),
                message_id,
                channel_id,
                guild_id,
                attachment.get("filename"),
                attachment.get("description"),
                attachment.get("content_type"),
                size,
                str(url),
                str(proxy_url) if proxy_url else None,
                height,
                width,
                _bool_int_or_none(attachment.get("ephemeral")),
                now,
            )
        )
    return rows


def _embed_rows(message: discord.Message, now: int) -> List[Tuple]:
    message_id = str(message.id)
    rows: List[Tuple] = []
    for idx, embed in enumerate(message.embeds):
        rows.append((message_id, idx, _embed_json(embed), now))
    return rows


def _embed_rows_from_payload(
    *,
    message_id: str,
    embeds: List[Any],
    now: int,
) -> List[Tuple]:
    rows: List[Tuple] = []
    for idx, embed in enumerate(embeds):
        rows.append((message_id, idx, _embed_json(embed), now))
    return rows


def _is_messageable(channel: Any) -> bool:
    return hasattr(channel, "history")


def _can_read_channel(channel: Any) -> bool:
    if not _is_messageable(channel):
        return False
    guild = getattr(channel, "guild", None)
    if guild is None:
        return True
    me = getattr(guild, "me", None)
    if me is None or not hasattr(channel, "permissions_for"):
        return True
    perms = channel.permissions_for(me)
    return bool(getattr(perms, "view_channel", True)) and bool(
        getattr(perms, "read_message_history", True)
    )


def _upsert_guilds(conn: sqlite3.Connection, rows: List[Tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO guilds
            (guild_id, name, owner_id, member_count, description, icon_url, created_at, updated_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            name = excluded.name,
            owner_id = excluded.owner_id,
            member_count = excluded.member_count,
            description = excluded.description,
            icon_url = excluded.icon_url,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at
        """,
        rows,
    )


def _upsert_channels(conn: sqlite3.Connection, rows: List[Tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO channels
            (channel_id, guild_id, name, type, position, parent_id, topic, nsfw, slowmode_delay, created_at, updated_at, extra_json)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            name = excluded.name,
            type = excluded.type,
            position = excluded.position,
            parent_id = excluded.parent_id,
            topic = excluded.topic,
            nsfw = excluded.nsfw,
            slowmode_delay = excluded.slowmode_delay,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            extra_json = excluded.extra_json
        """,
        rows,
    )


def _upsert_roles(conn: sqlite3.Connection, rows: List[Tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO roles
            (role_id, guild_id, name, color, hoist, position, permissions, managed, mentionable, icon_url, unicode_emoji, updated_at, deleted_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(role_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            name = excluded.name,
            color = excluded.color,
            hoist = excluded.hoist,
            position = excluded.position,
            permissions = excluded.permissions,
            managed = excluded.managed,
            mentionable = excluded.mentionable,
            icon_url = excluded.icon_url,
            unicode_emoji = excluded.unicode_emoji,
            updated_at = excluded.updated_at,
            deleted_at = excluded.deleted_at
        """,
        rows,
    )


def _mark_role_deleted(
    conn: sqlite3.Connection, *, role_id: str, guild_id: Optional[str], deleted_at: int
) -> None:
    conn.execute(
        """
        INSERT INTO roles
            (role_id, guild_id, deleted_at, updated_at)
        VALUES
            (?, ?, ?, ?)
        ON CONFLICT(role_id) DO UPDATE SET
            guild_id = COALESCE(excluded.guild_id, roles.guild_id),
            deleted_at = excluded.deleted_at,
            updated_at = excluded.updated_at
        """,
        (role_id, guild_id, deleted_at, deleted_at),
    )


def _upsert_guild_emojis(conn: sqlite3.Connection, rows: List[Tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO guild_emojis
            (emoji_id, guild_id, name, animated, available, managed, require_colons, updated_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(emoji_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            name = excluded.name,
            animated = excluded.animated,
            available = excluded.available,
            managed = excluded.managed,
            require_colons = excluded.require_colons,
            updated_at = excluded.updated_at
        """,
        rows,
    )


def _replace_guild_emojis(
    conn: sqlite3.Connection, *, guild_id: str, rows: List[Tuple], emoji_ids: List[str]
) -> None:
    _upsert_guild_emojis(conn, rows)
    if not emoji_ids:
        conn.execute(
            "DELETE FROM guild_emojis WHERE guild_id = ?",
            (guild_id,),
        )
        return
    placeholders = ", ".join("?" for _ in emoji_ids)
    conn.execute(
        f"""
        DELETE FROM guild_emojis
        WHERE guild_id = ?
          AND emoji_id NOT IN ({placeholders})
        """,
        [guild_id, *emoji_ids],
    )


def _upsert_guild_stickers(conn: sqlite3.Connection, rows: List[Tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO guild_stickers
            (sticker_id, guild_id, name, description, tags, format_type, available, updated_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sticker_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            name = excluded.name,
            description = excluded.description,
            tags = excluded.tags,
            format_type = excluded.format_type,
            available = excluded.available,
            updated_at = excluded.updated_at
        """,
        rows,
    )


def _replace_guild_stickers(
    conn: sqlite3.Connection,
    *,
    guild_id: str,
    rows: List[Tuple],
    sticker_ids: List[str],
) -> None:
    _upsert_guild_stickers(conn, rows)
    if not sticker_ids:
        conn.execute(
            "DELETE FROM guild_stickers WHERE guild_id = ?",
            (guild_id,),
        )
        return
    placeholders = ", ".join("?" for _ in sticker_ids)
    conn.execute(
        f"""
        DELETE FROM guild_stickers
        WHERE guild_id = ?
          AND sticker_id NOT IN ({placeholders})
        """,
        [guild_id, *sticker_ids],
    )


def _upsert_users(conn: sqlite3.Connection, rows: List[Tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO users
            (user_id, name, discriminator, global_name, bot, system, avatar_url, created_at, updated_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            name = excluded.name,
            discriminator = excluded.discriminator,
            global_name = excluded.global_name,
            bot = excluded.bot,
            system = excluded.system,
            avatar_url = excluded.avatar_url,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at
        """,
        rows,
    )


def _upsert_members(conn: sqlite3.Connection, rows: List[Tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO guild_members
            (guild_id, user_id, nick, joined_at, roles, pending, premium_since, mute, deaf, avatar_url, left_at, last_seen_at, updated_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            nick = excluded.nick,
            joined_at = excluded.joined_at,
            roles = excluded.roles,
            pending = excluded.pending,
            premium_since = excluded.premium_since,
            mute = excluded.mute,
            deaf = excluded.deaf,
            avatar_url = excluded.avatar_url,
            left_at = excluded.left_at,
            last_seen_at = excluded.last_seen_at,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    member_id_rows = [(row[0], row[1]) for row in rows]
    conn.executemany(
        "DELETE FROM guild_member_roles WHERE guild_id = ? AND user_id = ?",
        member_id_rows,
    )
    role_rows: List[Tuple[str, str, str]] = []
    for row in rows:
        guild_id = row[0]
        user_id = row[1]
        roles_raw = row[4]
        if not roles_raw:
            continue
        try:
            roles = json.loads(roles_raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(roles, list):
            continue
        seen_roles: Set[str] = set()
        for role_id in roles:
            if role_id is None:
                continue
            role_str = str(role_id)
            if role_str in seen_roles:
                continue
            seen_roles.add(role_str)
            role_rows.append((guild_id, user_id, role_str))
    if role_rows:
        conn.executemany(
            """
            INSERT INTO guild_member_roles
                (guild_id, user_id, role_id)
            VALUES
                (?, ?, ?)
            """,
            role_rows,
        )


def _upsert_messages(conn: sqlite3.Connection, rows: List[Tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO messages
            (message_id, channel_id, guild_id, author_id, content, content_available, created_at, edited_at, mention_ids, mention_everyone, attachments_count, embeds_count, pinned)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            channel_id = COALESCE(excluded.channel_id, messages.channel_id),
            guild_id = COALESCE(excluded.guild_id, messages.guild_id),
            author_id = COALESCE(excluded.author_id, messages.author_id),
            created_at = COALESCE(messages.created_at, excluded.created_at),
            content = COALESCE(excluded.content, messages.content),
            content_available =
                CASE
                    WHEN excluded.content IS NULL THEN messages.content_available
                    ELSE excluded.content_available
                END,
            edited_at = COALESCE(excluded.edited_at, messages.edited_at),
            mention_ids = COALESCE(excluded.mention_ids, messages.mention_ids),
            mention_everyone = COALESCE(excluded.mention_everyone, messages.mention_everyone),
            attachments_count = COALESCE(excluded.attachments_count, messages.attachments_count),
            embeds_count = COALESCE(excluded.embeds_count, messages.embeds_count),
            pinned = COALESCE(excluded.pinned, messages.pinned)
        """,
        rows,
    )


def _upsert_message_fields(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    channel_id: Optional[str],
    guild_id: Optional[str],
    fields: Dict[str, Any],
) -> None:
    if not fields:
        return
    field_names = list(fields.keys())
    columns = ["message_id", "channel_id", "guild_id", *field_names]
    placeholders = ", ".join("?" for _ in columns)
    update_parts: List[str] = []
    if channel_id is not None:
        update_parts.append("channel_id = excluded.channel_id")
    if guild_id is not None:
        update_parts.append("guild_id = excluded.guild_id")
    update_parts.extend([f"{name} = excluded.{name}" for name in field_names])
    update_stmt = ", ".join(update_parts)
    conn.execute(
        f"""
        INSERT INTO messages
            ({", ".join(columns)})
        VALUES
            ({placeholders})
        ON CONFLICT(message_id) DO UPDATE SET
            {update_stmt}
        """,
        [message_id, channel_id, guild_id, *fields.values()],
    )


def _mark_message_deleted(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    channel_id: Optional[str],
    guild_id: Optional[str],
    deleted_at: int,
) -> None:
    conn.execute(
        """
        INSERT INTO messages
            (message_id, channel_id, guild_id, deleted_at)
        VALUES
            (?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            channel_id = COALESCE(excluded.channel_id, messages.channel_id),
            guild_id = COALESCE(excluded.guild_id, messages.guild_id),
            deleted_at = excluded.deleted_at
        """,
        (message_id, channel_id, guild_id, deleted_at),
    )


def _ensure_message_row(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    channel_id: Optional[str],
    guild_id: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT INTO messages
            (message_id, channel_id, guild_id)
        VALUES
            (?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            channel_id = COALESCE(excluded.channel_id, messages.channel_id),
            guild_id = COALESCE(excluded.guild_id, messages.guild_id)
        """,
        (message_id, channel_id, guild_id),
    )


def _update_channel_pins(
    conn: sqlite3.Connection, *, channel_id: str, pinned_ids: List[str]
) -> None:
    if pinned_ids:
        placeholders = ", ".join("?" for _ in pinned_ids)
        conn.execute(
            f"""
            UPDATE messages
            SET pinned = 0
            WHERE channel_id = ?
              AND pinned = 1
              AND message_id NOT IN ({placeholders})
            """,
            [channel_id, *pinned_ids],
        )
    else:
        conn.execute(
            """
            UPDATE messages
            SET pinned = 0
            WHERE channel_id = ?
              AND pinned = 1
            """,
            (channel_id,),
        )


def _ensure_channel_state(
    conn: sqlite3.Connection, rows: List[Tuple[str, str, int]]
) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO channel_state
            (channel_id, guild_id, backfill_done, updated_at)
        VALUES
            (?, ?, 0, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            updated_at = excluded.updated_at
        """,
        rows,
    )


def _ensure_thread_parent_state(
    conn: sqlite3.Connection, rows: List[Tuple[str, str, int]]
) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO thread_parent_state
            (parent_channel_id, guild_id, public_done, private_done, updated_at)
        VALUES
            (?, ?, 0, 0, ?)
        ON CONFLICT(parent_channel_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            updated_at = excluded.updated_at
        """,
        rows,
    )


def _ensure_guild_state(conn: sqlite3.Connection, rows: List[Tuple[str, int]]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO guild_state
            (guild_id, backfill_done, updated_at)
        VALUES
            (?, 0, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            updated_at = excluded.updated_at
        """,
        rows,
    )


def _pick_channel_state_backfill(
    conn: sqlite3.Connection, now: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT channel_id, guild_id, last_before_id, latest_seen_id, backfill_done, fetch_error_count
        FROM channel_state
        WHERE backfill_done = 0
          AND (disabled_until IS NULL OR disabled_until <= ?)
          AND (fetch_forbidden_until IS NULL OR fetch_forbidden_until <= ?)
        ORDER BY last_indexed_at IS NOT NULL, last_indexed_at ASC
        LIMIT 1
        """
        ,
        (now, now),
    ).fetchone()


def _pick_channel_state_tail(
    conn: sqlite3.Connection, now: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT channel_id, guild_id, last_before_id, latest_seen_id, backfill_done, fetch_error_count
        FROM channel_state
        WHERE (disabled_until IS NULL OR disabled_until <= ?)
          AND (fetch_forbidden_until IS NULL OR fetch_forbidden_until <= ?)
          AND latest_seen_id IS NOT NULL
        ORDER BY last_indexed_at IS NOT NULL, last_indexed_at ASC
        LIMIT 1
        """
        ,
        (now, now),
    ).fetchone()


def _pick_channel_state_search(
    conn: sqlite3.Connection, now: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT channel_id, guild_id, last_before_id, latest_seen_id, backfill_done, search_before_id, search_done, fetch_error_count, search_forbidden_count
        FROM channel_state
        WHERE COALESCE(search_done, 0) = 0
          AND (disabled_until IS NULL OR disabled_until <= ?)
          AND (fetch_forbidden_until IS NULL OR fetch_forbidden_until <= ?)
          AND (search_forbidden_until IS NULL OR search_forbidden_until <= ?)
        ORDER BY search_last_indexed_at IS NOT NULL, search_last_indexed_at ASC
        LIMIT 1
        """
        ,
        (now, now, now),
    ).fetchone()


def _pick_channel_state_pins(
    conn: sqlite3.Connection, now: int, check_before: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT channel_id, guild_id
        FROM channel_state
        WHERE (disabled_until IS NULL OR disabled_until <= ?)
          AND (fetch_forbidden_until IS NULL OR fetch_forbidden_until <= ?)
          AND (pins_last_checked_at IS NULL OR pins_last_checked_at <= ?)
        ORDER BY pins_last_checked_at IS NOT NULL, pins_last_checked_at ASC
        LIMIT 1
        """
        ,
        (now, now, check_before),
    ).fetchone()


def _pick_guild_state(
    conn: sqlite3.Connection, rescan_cutoff: Optional[int]
) -> Optional[sqlite3.Row]:
    if rescan_cutoff is None:
        return conn.execute(
            """
            SELECT guild_id, member_after_id, member_latest_id, scan_started_at, backfill_done, scan_completed_at
            FROM guild_state
            WHERE backfill_done = 0
            ORDER BY last_indexed_at IS NOT NULL, last_indexed_at ASC
            LIMIT 1
            """
        ).fetchone()
    return conn.execute(
        """
        SELECT guild_id, member_after_id, member_latest_id, scan_started_at, backfill_done, scan_completed_at
        FROM guild_state
        WHERE backfill_done = 0
           OR scan_completed_at IS NULL
           OR scan_completed_at < ?
        ORDER BY last_indexed_at IS NOT NULL, last_indexed_at ASC
        LIMIT 1
        """
        ,
        (rescan_cutoff,),
    ).fetchone()


def _pick_thread_parent_state(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT parent_channel_id, guild_id,
               public_before_ts, private_before_ts,
               public_done, private_done
        FROM thread_parent_state
        WHERE COALESCE(public_done, 0) = 0
           OR COALESCE(private_done, 0) = 0
        ORDER BY updated_at IS NOT NULL, updated_at ASC
        LIMIT 1
        """
    ).fetchone()


def _update_channel_state(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    last_before_id: Optional[str],
    latest_seen_id: Optional[str],
    backfill_done: int,
    last_indexed_at: int,
    updated_at: int,
) -> None:
    conn.execute(
        """
        UPDATE channel_state
        SET last_before_id = ?,
            latest_seen_id = ?,
            backfill_done = ?,
            last_indexed_at = ?,
            updated_at = ?,
            fetch_error_count = 0,
            fetch_forbidden_until = NULL
        WHERE channel_id = ?
        """,
        (
            last_before_id,
            latest_seen_id,
            backfill_done,
            last_indexed_at,
            updated_at,
            channel_id,
        ),
    )


def _bump_channel_latest_seen(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    guild_id: Optional[str],
    message_id: str,
    now: int,
) -> None:
    conn.execute(
        """
        INSERT INTO channel_state (channel_id, guild_id, backfill_done, latest_seen_id, last_indexed_at, updated_at)
        VALUES (?, ?, 0, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            guild_id = COALESCE(excluded.guild_id, channel_state.guild_id),
            latest_seen_id = CASE
                WHEN channel_state.latest_seen_id IS NULL THEN excluded.latest_seen_id
                WHEN CAST(channel_state.latest_seen_id AS INTEGER) < CAST(excluded.latest_seen_id AS INTEGER)
                    THEN excluded.latest_seen_id
                ELSE channel_state.latest_seen_id
            END,
            last_indexed_at = excluded.last_indexed_at,
            updated_at = excluded.updated_at
        """,
        (channel_id, guild_id, message_id, now, now),
    )


def _update_channel_state_search(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    search_before_id: Optional[str],
    search_done: int,
    search_last_indexed_at: int,
    updated_at: int,
) -> None:
    conn.execute(
        """
        UPDATE channel_state
        SET search_before_id = ?,
            search_done = ?,
            search_last_indexed_at = ?,
            updated_at = ?,
            search_forbidden_count = 0,
            search_forbidden_until = NULL
        WHERE channel_id = ?
        """,
        (
            search_before_id,
            search_done,
            search_last_indexed_at,
            updated_at,
            channel_id,
        ),
    )


def _update_channel_state_pins_checked(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    pins_last_checked_at: int,
    updated_at: int,
) -> None:
    conn.execute(
        """
        UPDATE channel_state
        SET pins_last_checked_at = ?,
            updated_at = ?
        WHERE channel_id = ?
        """,
        (pins_last_checked_at, updated_at, channel_id),
    )


def _update_guild_state(
    conn: sqlite3.Connection,
    *,
    guild_id: str,
    member_after_id: Optional[str],
    member_latest_id: Optional[str],
    backfill_done: int,
    last_indexed_at: int,
    updated_at: int,
    scan_completed_at: Optional[int],
) -> None:
    conn.execute(
        """
        UPDATE guild_state
        SET member_after_id = ?,
            member_latest_id = ?,
            backfill_done = ?,
            last_indexed_at = ?,
            updated_at = ?,
            scan_completed_at = COALESCE(?, scan_completed_at)
        WHERE guild_id = ?
        """,
        (
            member_after_id,
            member_latest_id,
            backfill_done,
            last_indexed_at,
            updated_at,
            scan_completed_at,
            guild_id,
        ),
    )


def _set_guild_scan_started_at(
    conn: sqlite3.Connection, *, guild_id: str, scan_started_at: int, updated_at: int
) -> None:
    conn.execute(
        """
        UPDATE guild_state
        SET scan_started_at = COALESCE(scan_started_at, ?),
            updated_at = ?
        WHERE guild_id = ?
        """,
        (scan_started_at, updated_at, guild_id),
    )


def _reset_guild_scan(
    conn: sqlite3.Connection, *, guild_id: str, updated_at: int
) -> None:
    conn.execute(
        """
        UPDATE guild_state
        SET member_after_id = NULL,
            scan_started_at = NULL,
            scan_completed_at = NULL,
            backfill_done = 0,
            last_indexed_at = ?,
            updated_at = ?
        WHERE guild_id = ?
        """,
        (updated_at, updated_at, guild_id),
    )


def _update_thread_parent_state(
    conn: sqlite3.Connection,
    *,
    parent_channel_id: str,
    public_before_ts: Optional[int],
    private_before_ts: Optional[int],
    public_done: int,
    private_done: int,
    updated_at: int,
) -> None:
    conn.execute(
        """
        UPDATE thread_parent_state
        SET public_before_ts = ?,
            private_before_ts = ?,
            public_done = ?,
            private_done = ?,
            updated_at = ?
        WHERE parent_channel_id = ?
        """,
        (
            public_before_ts,
            private_before_ts,
            public_done,
            private_done,
            updated_at,
            parent_channel_id,
        ),
    )


def _record_thread_parent_error(
    conn: sqlite3.Connection,
    *,
    parent_channel_id: str,
    updated_at: int,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE thread_parent_state
        SET updated_at = ?,
            error_count = COALESCE(error_count, 0) + 1,
            last_error = ?
        WHERE parent_channel_id = ?
        """,
        (updated_at, error, parent_channel_id),
    )


def _record_channel_state_error(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    last_before_id: Optional[str],
    latest_seen_id: Optional[str],
    backfill_done: int,
    last_indexed_at: int,
    updated_at: int,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE channel_state
        SET last_before_id = ?,
            latest_seen_id = ?,
            backfill_done = ?,
            last_indexed_at = ?,
            updated_at = ?,
            error_count = COALESCE(error_count, 0) + 1,
            last_error = ?
        WHERE channel_id = ?
        """,
        (
            last_before_id,
            latest_seen_id,
            backfill_done,
            last_indexed_at,
            updated_at,
            error,
            channel_id,
        ),
    )


def _disable_channel_state(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    last_before_id: Optional[str],
    latest_seen_id: Optional[str],
    backfill_done: int,
    last_indexed_at: int,
    updated_at: int,
    disabled_until: int,
    disabled_reason: str,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE channel_state
        SET last_before_id = ?,
            latest_seen_id = ?,
            backfill_done = ?,
            last_indexed_at = ?,
            updated_at = ?,
            error_count = COALESCE(error_count, 0) + 1,
            last_error = ?,
            disabled_until = ?,
            disabled_reason = ?
        WHERE channel_id = ?
        """,
        (
            last_before_id,
            latest_seen_id,
            backfill_done,
            last_indexed_at,
            updated_at,
            error,
            disabled_until,
            disabled_reason,
            channel_id,
        ),
    )


def _record_channel_fetch_backoff(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    last_before_id: Optional[str],
    latest_seen_id: Optional[str],
    backfill_done: int,
    last_indexed_at: int,
    updated_at: int,
    fetch_error_count: int,
    fetch_forbidden_until: int,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE channel_state
        SET last_before_id = ?,
            latest_seen_id = ?,
            backfill_done = ?,
            last_indexed_at = ?,
            updated_at = ?,
            fetch_error_count = ?,
            fetch_forbidden_until = ?,
            error_count = COALESCE(error_count, 0) + 1,
            last_error = ?
        WHERE channel_id = ?
        """,
        (
            last_before_id,
            latest_seen_id,
            backfill_done,
            last_indexed_at,
            updated_at,
            fetch_error_count,
            fetch_forbidden_until,
            error,
            channel_id,
        ),
    )


def _record_channel_search_backoff(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    search_before_id: Optional[str],
    search_done: int,
    search_last_indexed_at: int,
    updated_at: int,
    search_forbidden_count: int,
    search_forbidden_until: int,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE channel_state
        SET search_before_id = ?,
            search_done = ?,
            search_last_indexed_at = ?,
            updated_at = ?,
            search_forbidden_count = ?,
            search_forbidden_until = ?,
            search_error_count = COALESCE(search_error_count, 0) + 1,
            search_last_error = ?
        WHERE channel_id = ?
        """,
        (
            search_before_id,
            search_done,
            search_last_indexed_at,
            updated_at,
            search_forbidden_count,
            search_forbidden_until,
            error,
            channel_id,
        ),
    )


def _record_channel_state_search_error(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    search_before_id: Optional[str],
    search_done: int,
    search_last_indexed_at: int,
    updated_at: int,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE channel_state
        SET search_before_id = ?,
            search_done = ?,
            search_last_indexed_at = ?,
            updated_at = ?,
            search_error_count = COALESCE(search_error_count, 0) + 1,
            search_last_error = ?
        WHERE channel_id = ?
        """,
        (
            search_before_id,
            search_done,
            search_last_indexed_at,
            updated_at,
            error,
            channel_id,
        ),
    )


def _record_channel_thread_fetch_error(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    error: str,
    error_at: int,
    forbidden_until: Optional[int],
) -> None:
    conn.execute(
        """
        UPDATE channel_state
        SET archived_threads_error_count = COALESCE(archived_threads_error_count, 0) + 1,
            archived_threads_last_error = ?,
            archived_threads_last_error_at = ?,
            archived_threads_forbidden_until = COALESCE(?, archived_threads_forbidden_until)
        WHERE channel_id = ?
        """,
        (error, error_at, forbidden_until, channel_id),
    )


def _record_guild_state_error(
    conn: sqlite3.Connection,
    *,
    guild_id: str,
    member_after_id: Optional[str],
    member_latest_id: Optional[str],
    backfill_done: int,
    last_indexed_at: int,
    updated_at: int,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE guild_state
        SET member_after_id = ?,
            member_latest_id = ?,
            backfill_done = ?,
            last_indexed_at = ?,
            updated_at = ?,
            error_count = COALESCE(error_count, 0) + 1,
            last_error = ?
        WHERE guild_id = ?
        """,
        (
            member_after_id,
            member_latest_id,
            backfill_done,
            last_indexed_at,
            updated_at,
            error,
            guild_id,
        ),
    )


async def _load_channel_thread_backoff(
    ctx: GlobalContext, channel_ids: List[str]
) -> Dict[str, Optional[int]]:
    if not channel_ids:
        return {}
    unique_ids = list(dict.fromkeys(channel_ids))

    def _read(conn: sqlite3.Connection) -> Dict[str, Optional[int]]:
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"""
            SELECT channel_id, archived_threads_forbidden_until
            FROM channel_state
            WHERE channel_id IN ({placeholders})
            """,
            unique_ids,
        ).fetchall()
        return {
            row["channel_id"]: row["archived_threads_forbidden_until"]
            for row in rows
        }

    return await _with_db(ctx, _read)


def _archived_threads_backoff_active(
    forbidden_until: Optional[int], now_ms: int
) -> bool:
    until = _parse_epoch_ms(forbidden_until)
    return until is not None and until > now_ms


def _fetch_backoff_ms(error_count: int) -> int:
    if error_count < 1:
        error_count = 1
    backoff_seconds = DEFAULT_FETCH_FORBIDDEN_BACKOFF_SECONDS * (
        2 ** (error_count - 1)
    )
    if backoff_seconds > MAX_FETCH_FORBIDDEN_BACKOFF_SECONDS:
        backoff_seconds = MAX_FETCH_FORBIDDEN_BACKOFF_SECONDS
    return int(backoff_seconds * 1000)


def _fetch_backoff_active(forbidden_until: Optional[int], now_ms: int) -> bool:
    until = _parse_epoch_ms(forbidden_until)
    return until is not None and until > now_ms


async def _sync_guilds_and_channels(
    ctx: GlobalContext, guilds: List[discord.Guild]
) -> None:
    now = _now_ms()
    guild_rows: List[Tuple] = []
    channel_rows: List[Tuple] = []
    channel_state_rows: List[Tuple[str, str, int]] = []
    guild_state_rows: List[Tuple[str, int]] = []
    role_rows: List[Tuple] = []
    emoji_rows_by_guild: Dict[str, List[Tuple]] = {}
    emoji_ids_by_guild: Dict[str, List[str]] = {}
    sticker_rows_by_guild: Dict[str, List[Tuple]] = {}
    sticker_ids_by_guild: Dict[str, List[str]] = {}
    thread_parent_rows: List[Tuple[str, str, int]] = []
    seen_channel_ids: Set[str] = set()

    async def _fetch_active_threads(guild: discord.Guild) -> List[Any]:
        for name in ("fetch_active_threads", "active_threads", "get_active_threads"):
            candidate = getattr(guild, name, None)
            if candidate is None:
                continue
            try:
                result = candidate() if callable(candidate) else candidate
                if asyncio.iscoroutine(result):
                    result = await result
            except Exception as e:
                _LOGGER.warning(
                    "Failed to fetch active threads for guild %s: %s",
                    guild.id,
                    e,
                )
                return []
            threads = None
            if isinstance(result, tuple) and result:
                threads = result[0]
            elif hasattr(result, "threads"):
                threads = getattr(result, "threads", None)
            if threads is None:
                threads = result
            if threads is None:
                return []
            try:
                return list(threads)
            except TypeError:
                return [threads]
        return []

    def _add_channel(channel: Any, guild_id: int) -> None:
        row = _channel_row(channel, guild_id, now)
        channel_id = row[0]
        if channel_id in seen_channel_ids:
            return
        seen_channel_ids.add(channel_id)
        channel_rows.append(row)
        if _is_messageable(channel):
            channel_state_rows.append((channel_id, str(guild_id), now))

    for guild in guilds:
        guild_rows.append(_guild_row(guild, now))
        guild_state_rows.append((str(guild.id), now))
        for role in getattr(guild, "roles", []) or []:
            role_rows.append(_role_row(role, now))
        emoji_rows: List[Tuple] = []
        emoji_ids: List[str] = []
        for emoji in getattr(guild, "emojis", []) or []:
            emoji_id = getattr(emoji, "id", None)
            if emoji_id is None:
                continue
            emoji_rows.append(_emoji_row(emoji, str(guild.id), now))
            emoji_ids.append(str(emoji_id))
        emoji_rows_by_guild[str(guild.id)] = emoji_rows
        emoji_ids_by_guild[str(guild.id)] = emoji_ids
        sticker_rows: List[Tuple] = []
        sticker_ids: List[str] = []
        for sticker in getattr(guild, "stickers", []) or []:
            sticker_id = getattr(sticker, "id", None)
            if sticker_id is None:
                continue
            sticker_rows.append(_sticker_row(sticker, str(guild.id), now))
            sticker_ids.append(str(sticker_id))
        sticker_rows_by_guild[str(guild.id)] = sticker_rows
        sticker_ids_by_guild[str(guild.id)] = sticker_ids

        for channel in guild.channels:
            _add_channel(channel, guild.id)
            if (
                hasattr(channel, "active_threads")
                or hasattr(channel, "archived_threads")
                or hasattr(channel, "private_archived_threads")
            ):
                thread_parent_rows.append((str(channel.id), str(guild.id), now))

        threads = getattr(guild, "threads", [])
        for thread in threads:
            _add_channel(thread, guild.id)

        active_threads = await _fetch_active_threads(guild)
        for thread in active_threads:
            _add_channel(thread, guild.id)

    def _write(conn: sqlite3.Connection) -> None:
        _upsert_guilds(conn, guild_rows)
        _upsert_channels(conn, channel_rows)
        _upsert_roles(conn, role_rows)
        for gid, rows in emoji_rows_by_guild.items():
            _replace_guild_emojis(
                conn,
                guild_id=gid,
                rows=rows,
                emoji_ids=emoji_ids_by_guild.get(gid, []),
            )
        for gid, rows in sticker_rows_by_guild.items():
            _replace_guild_stickers(
                conn,
                guild_id=gid,
                rows=rows,
                sticker_ids=sticker_ids_by_guild.get(gid, []),
            )
        _ensure_guild_state(conn, guild_state_rows)
        _ensure_channel_state(conn, channel_state_rows)
        _ensure_thread_parent_state(conn, thread_parent_rows)
        conn.commit()

    await _with_db(ctx, _write)


def _thread_before_dt(before_ts: Optional[int]) -> Optional[dt.datetime]:
    if before_ts is None:
        return None
    try:
        return dt.datetime.fromtimestamp(before_ts / 1000, tz=dt.timezone.utc)
    except (OSError, ValueError):
        return None


async def _collect_thread_page(result: Any) -> Tuple[List[Any], Optional[bool]]:
    if result is None:
        return [], None
    if asyncio.iscoroutine(result):
        result = await result
    has_more = None
    if isinstance(result, tuple):
        threads = result[0]
        has_more = result[1] if len(result) > 1 else None
    else:
        threads = result
        has_more = getattr(result, "has_more", None)
    if hasattr(threads, "__aiter__"):
        items: List[Any] = []
        async for thread in threads:
            items.append(thread)
        return items, has_more
    try:
        return list(threads), has_more
    except TypeError:
        return [threads], has_more


async def _index_next_thread_parent(
    ctx: GlobalContext, client: discord.Client, archived_limit: int
) -> JSONDict:
    state = await _with_db(ctx, _pick_thread_parent_state)
    if state is None:
        return {"threads_indexed": 0}

    parent_channel_id = str(state["parent_channel_id"])
    guild_id = state["guild_id"]
    public_done = int(state["public_done"] or 0)
    private_done = int(state["private_done"] or 0)
    public_before_ts = state["public_before_ts"]
    private_before_ts = state["private_before_ts"]

    if not guild_id:
        return {"threads_indexed": 0}

    backoff = await _load_channel_thread_backoff(ctx, [parent_channel_id])
    if _archived_threads_backoff_active(backoff.get(parent_channel_id), _now_ms()):
        await _with_db(
            ctx,
            lambda conn: _update_thread_parent_state(
                conn,
                parent_channel_id=parent_channel_id,
                public_before_ts=public_before_ts,
                private_before_ts=private_before_ts,
                public_done=public_done,
                private_done=private_done,
                updated_at=_now_ms(),
            ),
        )
        return {"threads_indexed": 0}

    channel = client.get_channel(int(parent_channel_id))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(parent_channel_id))
        except Exception as e:
            _LOGGER.warning(
                "Failed to fetch thread parent %s: %s",
                parent_channel_id,
                e,
            )
            await _with_db(
                ctx,
                lambda conn: _record_thread_parent_error(
                    conn,
                    parent_channel_id=parent_channel_id,
                    updated_at=_now_ms(),
                    error=str(e),
                ),
            )
            return {"threads_indexed": 0}

    if not (
        hasattr(channel, "archived_threads")
        or hasattr(channel, "private_archived_threads")
    ):
        await _with_db(
            ctx,
            lambda conn: _update_thread_parent_state(
                conn,
                parent_channel_id=parent_channel_id,
                public_before_ts=public_before_ts,
                private_before_ts=private_before_ts,
                public_done=1,
                private_done=1,
                updated_at=_now_ms(),
            ),
        )
        return {"threads_indexed": 0}

    scan_private = public_done != 0 and private_done == 0
    if scan_private and not hasattr(channel, "private_archived_threads"):
        private_done = 1
        await _with_db(
            ctx,
            lambda conn: _update_thread_parent_state(
                conn,
                parent_channel_id=parent_channel_id,
                public_before_ts=public_before_ts,
                private_before_ts=private_before_ts,
                public_done=public_done,
                private_done=private_done,
                updated_at=_now_ms(),
            ),
        )
        return {"threads_indexed": 0}
    if not scan_private and not hasattr(channel, "archived_threads"):
        public_done = 1
        await _with_db(
            ctx,
            lambda conn: _update_thread_parent_state(
                conn,
                parent_channel_id=parent_channel_id,
                public_before_ts=public_before_ts,
                private_before_ts=private_before_ts,
                public_done=public_done,
                private_done=private_done,
                updated_at=_now_ms(),
            ),
        )
        return {"threads_indexed": 0}
    before_ts = private_before_ts if scan_private else public_before_ts
    before_dt = _thread_before_dt(before_ts)

    try:
        if scan_private:
            result = channel.private_archived_threads(
                limit=archived_limit, before=before_dt
            )
        else:
            result = channel.archived_threads(
                limit=archived_limit, before=before_dt
            )
        threads, has_more = await _collect_thread_page(result)
    except discord.Forbidden as e:
        _LOGGER.warning(
            "Archived thread fetch forbidden for channel %s: %s",
            parent_channel_id,
            e,
        )
        now = _now_ms()
        forbidden_until = now + (
            DEFAULT_ARCHIVED_THREADS_FORBIDDEN_BACKOFF_DAYS
            * 24
            * 60
            * 60
            * 1000
        )
        def _write(conn: sqlite3.Connection) -> None:
            _record_channel_thread_fetch_error(
                conn,
                channel_id=parent_channel_id,
                error=str(e),
                error_at=now,
                forbidden_until=forbidden_until,
            )
            _record_thread_parent_error(
                conn,
                parent_channel_id=parent_channel_id,
                updated_at=now,
                error=str(e),
            )
            conn.commit()

        await _with_db(ctx, _write)
        return {"threads_indexed": 0}
    except Exception as e:
        _LOGGER.warning(
            "Archived thread fetch failed for channel %s: %s",
            parent_channel_id,
            e,
        )
        await _with_db(
            ctx,
            lambda conn: _record_thread_parent_error(
                conn,
                parent_channel_id=parent_channel_id,
                updated_at=_now_ms(),
                error=str(e),
            ),
        )
        return {"threads_indexed": 0}

    now = _now_ms()
    if not threads:
        if scan_private:
            private_done = 1
        else:
            public_done = 1
        await _with_db(
            ctx,
            lambda conn: _update_thread_parent_state(
                conn,
                parent_channel_id=parent_channel_id,
                public_before_ts=public_before_ts,
                private_before_ts=private_before_ts,
                public_done=public_done,
                private_done=private_done,
                updated_at=now,
            ),
        )
        return {"threads_indexed": 0}

    thread_rows: List[Tuple] = []
    channel_state_rows: List[Tuple[str, str, int]] = []
    oldest_ts: Optional[int] = None
    for thread in threads:
        thread_rows.append(_channel_row(thread, int(guild_id), now))
        if _is_messageable(thread):
            channel_state_rows.append((str(thread.id), str(guild_id), now))
        archive_ts = _to_epoch_ms(getattr(thread, "archive_timestamp", None))
        if archive_ts is not None:
            if oldest_ts is None or archive_ts < oldest_ts:
                oldest_ts = archive_ts

    if has_more is None:
        has_more = len(threads) >= archived_limit

    next_before_ts = before_ts
    if oldest_ts is not None:
        next_before_ts = max(oldest_ts - 1, 0)

    if scan_private:
        private_before_ts = next_before_ts
        if not has_more:
            private_done = 1
    else:
        public_before_ts = next_before_ts
        if not has_more:
            public_done = 1

    def _write(conn: sqlite3.Connection) -> None:
        _upsert_channels(conn, thread_rows)
        _ensure_channel_state(conn, channel_state_rows)
        _update_thread_parent_state(
            conn,
            parent_channel_id=parent_channel_id,
            public_before_ts=public_before_ts,
            private_before_ts=private_before_ts,
            public_done=public_done,
            private_done=private_done,
            updated_at=now,
        )
        conn.commit()

    await _with_db(ctx, _write)
    return {"threads_indexed": len(threads)}


async def _index_next_guild_members(
    ctx: GlobalContext, client: discord.Client, member_batch_size: int
) -> JSONDict:
    select_now = _now_ms()
    rescan_interval_seconds = _get_int_config(
        ctx,
        "indexer_guild_rescan_interval_seconds",
        DEFAULT_GUILD_RESCAN_INTERVAL_SECONDS,
    )
    rescan_cutoff: Optional[int] = None
    if rescan_interval_seconds > 0:
        rescan_cutoff = select_now - (rescan_interval_seconds * 1000)
    state = await _with_db(
        ctx, lambda conn: _pick_guild_state(conn, rescan_cutoff)
    )
    if state is None:
        return {"members_indexed": 0}

    guild_id = str(state["guild_id"])
    state_backfill_done = int(state["backfill_done"] or 0)
    scan_started_at = state["scan_started_at"]
    scan_completed_at = state["scan_completed_at"]
    member_after_id = state["member_after_id"]
    member_latest_id = state["member_latest_id"]

    async def _update_guild_state_for_error(mark_done: bool, error: Any) -> None:
        now = _now_ms()
        await _with_db(
            ctx,
            lambda conn: _record_guild_state_error(
                conn,
                guild_id=guild_id,
                member_after_id=member_after_id,
                member_latest_id=member_latest_id,
                backfill_done=1 if mark_done else state_backfill_done,
                last_indexed_at=now,
                updated_at=now,
                error=str(error),
            ),
        )

    if (
        rescan_cutoff is not None
        and state_backfill_done
        and (scan_completed_at is None or scan_completed_at < rescan_cutoff)
    ):
        await _with_db(
            ctx,
            lambda conn: _reset_guild_scan(
                conn, guild_id=guild_id, updated_at=select_now
            ),
        )
        state_backfill_done = 0
        scan_started_at = None
        scan_completed_at = None
        member_after_id = None

    guild = client.get_guild(int(guild_id))
    if guild is None:
        await _update_guild_state_for_error(mark_done=True, error="guild not available")
        return {"members_indexed": 0}

    if (
        scan_started_at is None
        and not state_backfill_done
        and not member_after_id
    ):
        scan_started_at = _now_ms()
        await _with_db(
            ctx,
            lambda conn: _set_guild_scan_started_at(
                conn,
                guild_id=guild_id,
                scan_started_at=scan_started_at,
                updated_at=scan_started_at,
            ),
        )

    after_id = member_after_id
    if after_id:
        after = discord.Object(id=int(after_id))
    else:
        after = None
    members: List[discord.Member] = []
    try:
        async for member in guild.fetch_members(limit=member_batch_size, after=after):
            members.append(member)
    except (discord.Forbidden, discord.NotFound) as e:
        _LOGGER.error(
            "Failed to fetch members for guild %s: %s", guild_id, e, exc_info=True
        )
        await _update_guild_state_for_error(mark_done=True, error=e)
        return {"members_indexed": 0}
    except (discord.HTTPException, asyncio.TimeoutError) as e:
        _LOGGER.error(
            "Failed to fetch members for guild %s: %s", guild_id, e, exc_info=True
        )
        await _update_guild_state_for_error(mark_done=False, error=e)
        return {"members_indexed": 0}
    except Exception as e:
        _LOGGER.error(
            "Failed to fetch members for guild %s: %s", guild_id, e, exc_info=True
        )
        await _update_guild_state_for_error(mark_done=False, error=e)
        return {"members_indexed": 0}

    now = _now_ms()
    if not members:
        scan_cutoff = scan_started_at or now
        def _write(conn: sqlite3.Connection) -> None:
            _update_guild_state(
                conn,
                guild_id=guild_id,
                member_after_id=member_after_id,
                member_latest_id=member_latest_id,
                backfill_done=1,
                last_indexed_at=now,
                updated_at=now,
                scan_completed_at=now,
            )
            conn.execute(
                """
                UPDATE guild_members
                SET left_at = ?, updated_at = ?
                WHERE guild_id = ?
                  AND left_at IS NULL
                  AND (last_seen_at IS NULL OR last_seen_at < ?)
                """,
                (now, now, guild_id, scan_cutoff),
            )
            conn.commit()

        await _with_db(ctx, _write)
        return {"members_indexed": 0}

    users_by_id: Dict[str, Tuple] = {}
    member_rows: List[Tuple] = []
    max_member_id = 0
    for member in members:
        user_row = _user_row(member, now)
        users_by_id[user_row[0]] = user_row
        member_rows.append(_member_row(member, now))
        if member.id > max_member_id:
            max_member_id = member.id

    member_after_id = str(max_member_id) if max_member_id else member_after_id
    if max_member_id and (not member_latest_id or max_member_id > int(member_latest_id)):
        member_latest_id = str(max_member_id)

    def _write(conn: sqlite3.Connection) -> None:
        new_backfill_done = int(state["backfill_done"] or 0)
        _upsert_users(conn, list(users_by_id.values()))
        _upsert_members(conn, member_rows)
        _update_guild_state(
            conn,
            guild_id=guild_id,
            member_after_id=member_after_id,
            member_latest_id=member_latest_id,
            backfill_done=new_backfill_done,
            last_indexed_at=now,
            updated_at=now,
            scan_completed_at=None,
        )
        conn.commit()

    await _with_db(ctx, _write)
    return {"members_indexed": len(members)}


async def _index_next_channel_messages_from_picker(
    ctx: GlobalContext,
    client: discord.Client,
    message_batch_size: int,
    pick_state: Callable[[sqlite3.Connection, int], Optional[sqlite3.Row]],
    *,
    fetch_mode: str,
) -> JSONDict:
    select_now = _now_ms()
    state = await _with_db(ctx, lambda conn: pick_state(conn, select_now))
    if state is None:
        return {"messages_indexed": 0}

    channel_id = str(state["channel_id"])
    state_backfill_done = int(state["backfill_done"] or 0)
    state_fetch_error_count = int(state["fetch_error_count"] or 0)
    fetch_tail = fetch_mode == "tail"

    async def _record_channel_error(mark_done: bool, error: Any) -> None:
        now = _now_ms()
        await _with_db(
            ctx,
            lambda conn: _record_channel_state_error(
                conn,
                channel_id=channel_id,
                last_before_id=state["last_before_id"],
                latest_seen_id=state["latest_seen_id"],
                backfill_done=1 if mark_done else state_backfill_done,
                last_indexed_at=now,
                updated_at=now,
                error=str(error),
            ),
        )

    async def _disable_channel(reason: str, error: Any) -> None:
        now = _now_ms()
        disabled_until = now + (
            DEFAULT_CHANNEL_DISABLE_DAYS * 24 * 60 * 60 * 1000
        )
        await _with_db(
            ctx,
            lambda conn: _disable_channel_state(
                conn,
                channel_id=channel_id,
                last_before_id=state["last_before_id"],
                latest_seen_id=state["latest_seen_id"],
                backfill_done=state_backfill_done,
                last_indexed_at=now,
                updated_at=now,
                disabled_until=disabled_until,
                disabled_reason=reason,
                error=str(error),
            ),
        )

    async def _record_fetch_backoff(error: Any) -> None:
        now = _now_ms()
        fetch_error_count = state_fetch_error_count + 1
        backoff_ms = _fetch_backoff_ms(fetch_error_count)
        fetch_forbidden_until = now + backoff_ms
        await _with_db(
            ctx,
            lambda conn: _record_channel_fetch_backoff(
                conn,
                channel_id=channel_id,
                last_before_id=state["last_before_id"],
                latest_seen_id=state["latest_seen_id"],
                backfill_done=state_backfill_done,
                last_indexed_at=now,
                updated_at=now,
                fetch_error_count=fetch_error_count,
                fetch_forbidden_until=fetch_forbidden_until,
                error=str(error),
            ),
        )

    channel = client.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(channel_id))
        except (discord.Forbidden, discord.NotFound) as e:
            _LOGGER.error(
                "Failed to fetch channel %s: %s", channel_id, e, exc_info=True
            )
            await _disable_channel("forbidden", e)
            return {"messages_indexed": 0}
        except Exception as e:
            _LOGGER.error(
                "Failed to fetch channel %s: %s", channel_id, e, exc_info=True
            )
            await _record_channel_error(mark_done=False, error=e)
            return {"messages_indexed": 0}

    if not _can_read_channel(channel):
        await _disable_channel("missing_read_permission", "missing read permission")
        return {"messages_indexed": 0}

    messages: List[discord.Message] = []

    try:
        _LOGGER.info("Fetching messages for channel %s", channel_id)
        if fetch_tail:
            after_id = state["latest_seen_id"]
            after = discord.Object(id=int(after_id)) if after_id else None
            async for message in channel.history(
                limit=message_batch_size, after=after, oldest_first=True
            ):
                messages.append(message)
        else:
            before_id = state["last_before_id"]
            before = discord.Object(id=int(before_id)) if before_id else None
            async for message in channel.history(
                limit=message_batch_size, before=before, oldest_first=False
            ):
                messages.append(message)
    except discord.Forbidden as e:
        _LOGGER.error(
            "Failed to fetch messages for channel %s: %s",
            channel_id,
            e,
            exc_info=True,
        )
        await _record_fetch_backoff(e)
        return {"messages_indexed": 0}
    except discord.NotFound as e:
        _LOGGER.error(
            "Channel not found while fetching messages for %s: %s",
            channel_id,
            e,
            exc_info=True,
        )
        await _disable_channel("not_found", e)
        return {"messages_indexed": 0}
    except Exception as e:
        _LOGGER.error(
            "Failed to fetch messages for channel %s: %s",
            channel_id,
            e,
            exc_info=True,
        )
        await _record_channel_error(mark_done=False, error=e)
        return {"messages_indexed": 0}

    now = _now_ms()
    if not messages:
        if fetch_tail:
            new_backfill_done = state_backfill_done
        else:
            new_backfill_done = 1 if not state_backfill_done else state_backfill_done
        await _with_db(
            ctx,
            lambda conn: _update_channel_state(
                conn,
                channel_id=channel_id,
                last_before_id=state["last_before_id"],
                latest_seen_id=state["latest_seen_id"],
                backfill_done=new_backfill_done,
                last_indexed_at=now,
                updated_at=now,
            ),
        )
        return {"messages_indexed": 0}

    message_rows: List[Tuple] = []
    attachment_rows: List[Tuple] = []
    embed_rows: List[Tuple] = []
    reaction_rows: List[Tuple] = []
    user_mention_rows: List[Tuple[str, str]] = []
    role_mention_rows: List[Tuple[str, str]] = []
    channel_mention_rows: List[Tuple[str, str]] = []
    users_by_id: Dict[str, Tuple] = {}
    message_ids: List[int] = []
    for message in messages:
        message_rows.append(_message_row(message, now))
        attachment_rows.extend(_attachment_rows(message, now))
        embed_rows.extend(_embed_rows(message, now))
        reaction_rows.extend(_reaction_rows(message, now))
        user_mention_rows.extend(_user_mention_rows(message))
        role_mention_rows.extend(_role_mention_rows(message))
        channel_mention_rows.extend(_channel_mention_rows(message))
        user_row = _user_row(message.author, now)
        users_by_id[user_row[0]] = user_row
        message_ids.append(message.id)

    min_id = min(message_ids)
    max_id = max(message_ids)

    latest_seen_id = state["latest_seen_id"]
    if max_id and (not latest_seen_id or max_id > int(latest_seen_id)):
        latest_seen_id = str(max_id)

    last_before_id = state["last_before_id"]
    if not fetch_tail and not state_backfill_done:
        last_before_id = str(min_id)

    message_id_rows = [(str(message_id),) for message_id in message_ids]

    def _write(conn: sqlite3.Connection) -> None:
        _upsert_users(conn, list(users_by_id.values()))
        _upsert_messages(conn, message_rows)
        if message_id_rows:
            conn.executemany(
                "DELETE FROM message_attachments WHERE message_id = ?",
                message_id_rows,
            )
            if attachment_rows:
                conn.executemany(
                    """
                    INSERT INTO message_attachments
                        (attachment_id, message_id, channel_id, guild_id, filename, description, content_type, size, url, proxy_url, height, width, ephemeral, updated_at)
                    VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    attachment_rows,
                )
            conn.executemany(
                "DELETE FROM message_embeds WHERE message_id = ?",
                message_id_rows,
            )
            if embed_rows:
                conn.executemany(
                    """
                    INSERT INTO message_embeds
                        (message_id, idx, embed_json, updated_at)
                    VALUES
                        (?, ?, ?, ?)
                    """,
                    embed_rows,
                )
            conn.executemany(
                "DELETE FROM message_reactions WHERE message_id = ?",
                message_id_rows,
            )
            if reaction_rows:
                conn.executemany(
                    """
                    INSERT INTO message_reactions
                        (message_id, emoji_key, count, me, updated_at)
                    VALUES
                        (?, ?, ?, ?, ?)
                    """,
                    reaction_rows,
                )
            conn.executemany(
                "DELETE FROM message_user_mentions WHERE message_id = ?",
                message_id_rows,
            )
            if user_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_user_mentions
                        (message_id, user_id)
                    VALUES
                        (?, ?)
                    """,
                    user_mention_rows,
                )
            conn.executemany(
                "DELETE FROM message_role_mentions WHERE message_id = ?",
                message_id_rows,
            )
            if role_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_role_mentions
                        (message_id, role_id)
                    VALUES
                        (?, ?)
                    """,
                    role_mention_rows,
                )
            conn.executemany(
                "DELETE FROM message_channel_mentions WHERE message_id = ?",
                message_id_rows,
            )
            if channel_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_channel_mentions
                        (message_id, channel_id)
                    VALUES
                        (?, ?)
                    """,
                    channel_mention_rows,
                )
        _update_channel_state(
            conn,
            channel_id=channel_id,
            last_before_id=last_before_id,
            latest_seen_id=latest_seen_id,
            backfill_done=state_backfill_done,
            last_indexed_at=now,
            updated_at=now,
        )
        conn.commit()

    await _with_db(ctx, _write)
    return {"messages_indexed": len(messages)}


async def _index_next_channel_messages_backfill(
    ctx: GlobalContext, client: discord.Client, message_batch_size: int
) -> JSONDict:
    return await _index_next_channel_messages_from_picker(
        ctx,
        client,
        message_batch_size,
        _pick_channel_state_backfill,
        fetch_mode="backfill",
    )


async def _index_next_channel_messages_tail(
    ctx: GlobalContext, client: discord.Client, message_batch_size: int
) -> JSONDict:
    return await _index_next_channel_messages_from_picker(
        ctx,
        client,
        message_batch_size,
        _pick_channel_state_tail,
        fetch_mode="tail",
    )


async def _index_next_channel_messages_search(
    ctx: GlobalContext, client: discord.Client
) -> JSONDict:
    select_now = _now_ms()
    state = await _with_db(ctx, lambda conn: _pick_channel_state_search(conn, select_now))
    if state is None:
        return {"messages_indexed": 0}

    channel_id = str(state["channel_id"])
    guild_id = state["guild_id"]
    state_last_before_id = state["last_before_id"]
    state_backfill_done = int(state["backfill_done"] or 0)
    search_before_id = state["search_before_id"] or state["latest_seen_id"]
    state_search_done = int(state["search_done"] or 0)
    state_search_forbidden_count = int(state["search_forbidden_count"] or 0)

    async def _update_channel_state_for_error(
        *, mark_done: bool, error: Any
    ) -> None:
        now = _now_ms()
        await _with_db(
            ctx,
            lambda conn: _record_channel_state_search_error(
                conn,
                channel_id=channel_id,
                search_before_id=search_before_id,
                search_done=1 if mark_done else state_search_done,
                search_last_indexed_at=now,
                updated_at=now,
                error=str(error),
            ),
        )

    async def _disable_channel(reason: str, error: Any) -> None:
        now = _now_ms()
        disabled_until = now + (
            DEFAULT_CHANNEL_DISABLE_DAYS * 24 * 60 * 60 * 1000
        )
        await _with_db(
            ctx,
            lambda conn: _disable_channel_state(
                conn,
                channel_id=channel_id,
                last_before_id=state_last_before_id,
                latest_seen_id=state["latest_seen_id"],
                backfill_done=state_backfill_done,
                last_indexed_at=now,
                updated_at=now,
                disabled_until=disabled_until,
                disabled_reason=reason,
                error=str(error),
            ),
        )

    async def _record_search_backoff(error: Any) -> None:
        now = _now_ms()
        search_forbidden_count = state_search_forbidden_count + 1
        backoff_ms = _fetch_backoff_ms(search_forbidden_count)
        search_forbidden_until = now + backoff_ms
        await _with_db(
            ctx,
            lambda conn: _record_channel_search_backoff(
                conn,
                channel_id=channel_id,
                search_before_id=search_before_id,
                search_done=state_search_done,
                search_last_indexed_at=now,
                updated_at=now,
                search_forbidden_count=search_forbidden_count,
                search_forbidden_until=search_forbidden_until,
                error=str(error),
            ),
        )

    channel = client.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(channel_id))
        except (discord.Forbidden, discord.NotFound) as e:
            _LOGGER.error(
                "Failed to fetch channel %s for search indexing: %s",
                channel_id,
                e,
                exc_info=True,
            )
            await _disable_channel("forbidden", e)
            return {"messages_indexed": 0}
        except Exception as e:
            _LOGGER.error(
                "Failed to fetch channel %s for search indexing: %s",
                channel_id,
                e,
                exc_info=True,
            )
            await _update_channel_state_for_error(mark_done=False, error=e)
            return {"messages_indexed": 0}

    if not _can_read_channel(channel):
        await _disable_channel("missing_read_permission", "missing read permission")
        return {"messages_indexed": 0}

    if guild_id is None:
        guild = getattr(channel, "guild", None)
        if guild is None:
            await _update_channel_state_for_error(
                mark_done=True, error="guild not available"
            )
            return {"messages_indexed": 0}
        guild_id = str(guild.id)
    else:
        guild_id = str(guild_id)

    try:
        data = await _search_guild_messages(
            client,
            guild_id=guild_id,
            channel_id=channel_id,
            query=None,
            author_id=None,
            min_id=None,
            max_id=search_before_id,
            offset=0,
            oldest_first=False,
            include_nsfw=None,
        )
    except discord.Forbidden as e:
        _LOGGER.error(
            "Search API forbidden for channel %s: %s",
            channel_id,
            e,
            exc_info=True,
        )
        await _record_search_backoff(e)
        return {"messages_indexed": 0}
    except discord.NotFound as e:
        _LOGGER.error(
            "Search API not found for channel %s: %s",
            channel_id,
            e,
            exc_info=True,
        )
        await _update_channel_state_for_error(mark_done=True, error=e)
        return {"messages_indexed": 0}
    except Exception as e:
        _LOGGER.error(
            "Search API failed for channel %s: %s",
            channel_id,
            e,
            exc_info=True,
        )
        await _update_channel_state_for_error(mark_done=False, error=e)
        return {"messages_indexed": 0}

    page_messages = _flatten_search_messages(data.get("messages"))
    now = _now_ms()
    if not page_messages:
        await _with_db(
            ctx,
            lambda conn: _update_channel_state_search(
                conn,
                channel_id=channel_id,
                search_before_id=search_before_id,
                search_done=1,
                search_last_indexed_at=now,
                updated_at=now,
            ),
        )
        return {"messages_indexed": 0}

    page_result = await _index_message_payloads(
        ctx,
        page_messages,
        channel_id=channel_id,
        guild_id=guild_id,
    )

    next_before_id = search_before_id
    min_id = page_result.get("min_id")
    if min_id is not None:
        try:
            min_value = int(min_id)
        except (TypeError, ValueError):
            min_value = None
        if min_value is not None:
            next_before_id = str(max(min_value - 1, 0))

    await _with_db(
        ctx,
        lambda conn: _update_channel_state_search(
            conn,
            channel_id=channel_id,
            search_before_id=next_before_id,
            search_done=0,
            search_last_indexed_at=now,
            updated_at=now,
        ),
    )

    return {"messages_indexed": page_result.get("messages_indexed", 0)}


async def _reconcile_next_channel_pins(
    ctx: GlobalContext, client: discord.Client, check_interval_ms: int
) -> JSONDict:
    if check_interval_ms <= 0:
        return {"channels_checked": 0}
    select_now = _now_ms()
    check_before = select_now - check_interval_ms
    state = await _with_db(
        ctx, lambda conn: _pick_channel_state_pins(conn, select_now, check_before)
    )
    if state is None:
        return {"channels_checked": 0}

    channel_id = str(state["channel_id"])
    channel = client.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(channel_id))
        except Exception as e:
            _LOGGER.error(
                "Failed to fetch channel %s for pin reconciliation: %s",
                channel_id,
                e,
                exc_info=True,
            )
            await _with_db(
                ctx,
                lambda conn: _update_channel_state_pins_checked(
                    conn,
                    channel_id=channel_id,
                    pins_last_checked_at=select_now,
                    updated_at=select_now,
                ),
            )
            return {"channels_checked": 0}

    await record_channel_pins_update(ctx, channel)
    return {"channels_checked": 1}


async def _record_member_event(
    ctx: GlobalContext, member: discord.Member, *, mark_left: bool
) -> None:
    now = _now_ms()
    left_at = now if mark_left else None
    user_row = _user_row(member, now)
    member_row = _member_row(member, now, left_at=left_at)

    def _write(conn: sqlite3.Connection) -> None:
        _upsert_users(conn, [user_row])
        _upsert_members(conn, [member_row])
        conn.commit()

    await _with_db(ctx, _write)


async def record_message_create(
    ctx: GlobalContext, message: discord.Message
) -> None:
    now = _now_ms()
    message_id = str(message.id)
    message_rows = [_message_row(message, now)]
    attachment_rows = _attachment_rows(message, now)
    embed_rows = _embed_rows(message, now)
    reaction_rows = _reaction_rows(message, now)
    user_mention_rows = _user_mention_rows(message)
    role_mention_rows = _role_mention_rows(message)
    channel_mention_rows = _channel_mention_rows(message)
    user_row = _user_row(message.author, now)
    message_id_rows = [(message_id,)]

    def _write(conn: sqlite3.Connection) -> None:
        _upsert_users(conn, [user_row])
        _upsert_messages(conn, message_rows)
        _bump_channel_latest_seen(
            conn,
            channel_id=str(message.channel.id),
            guild_id=str(message.guild.id) if message.guild else None,
            message_id=message_id,
            now=now,
        )
        conn.executemany(
            "DELETE FROM message_attachments WHERE message_id = ?",
            message_id_rows,
        )
        if attachment_rows:
            conn.executemany(
                """
                INSERT INTO message_attachments
                    (attachment_id, message_id, channel_id, guild_id, filename, description, content_type, size, url, proxy_url, height, width, ephemeral, updated_at)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                attachment_rows,
            )
        conn.executemany(
            "DELETE FROM message_embeds WHERE message_id = ?",
            message_id_rows,
        )
        if embed_rows:
            conn.executemany(
                """
                INSERT INTO message_embeds
                    (message_id, idx, embed_json, updated_at)
                VALUES
                    (?, ?, ?, ?)
                """,
                embed_rows,
            )
        conn.executemany(
            "DELETE FROM message_reactions WHERE message_id = ?",
            message_id_rows,
        )
        if reaction_rows:
            conn.executemany(
                """
                INSERT INTO message_reactions
                    (message_id, emoji_key, count, me, updated_at)
                VALUES
                    (?, ?, ?, ?, ?)
                """,
                reaction_rows,
            )
        conn.executemany(
            "DELETE FROM message_user_mentions WHERE message_id = ?",
            message_id_rows,
        )
        if user_mention_rows:
            conn.executemany(
                """
                INSERT INTO message_user_mentions
                    (message_id, user_id)
                VALUES
                    (?, ?)
                """,
                user_mention_rows,
            )
        conn.executemany(
            "DELETE FROM message_role_mentions WHERE message_id = ?",
            message_id_rows,
        )
        if role_mention_rows:
            conn.executemany(
                """
                INSERT INTO message_role_mentions
                    (message_id, role_id)
                VALUES
                    (?, ?)
                """,
                role_mention_rows,
            )
        conn.executemany(
            "DELETE FROM message_channel_mentions WHERE message_id = ?",
            message_id_rows,
        )
        if channel_mention_rows:
            conn.executemany(
                """
                INSERT INTO message_channel_mentions
                    (message_id, channel_id)
                VALUES
                    (?, ?)
                """,
                channel_mention_rows,
            )
        conn.commit()

    await _with_db(ctx, _write)


def _flatten_search_messages(values: Any) -> List[Dict[str, Any]]:
    flattened: List[Dict[str, Any]] = []
    if not isinstance(values, list):
        return flattened
    for item in values:
        if isinstance(item, list):
            for message in item:
                if isinstance(message, dict):
                    flattened.append(message)
        elif isinstance(item, dict):
            flattened.append(item)
    return flattened


async def _index_message_payloads(
    ctx: GlobalContext,
    message_payloads: List[Dict[str, Any]],
    *,
    channel_id: Optional[str],
    guild_id: Optional[str],
) -> Dict[str, Optional[str]]:
    if not message_payloads:
        return {"messages_indexed": 0, "min_id": None, "max_id": None}

    now = _now_ms()
    message_rows: List[Tuple] = []
    attachment_rows: List[Tuple] = []
    embed_rows: List[Tuple] = []
    reaction_rows: List[Tuple] = []
    user_mention_rows: List[Tuple[str, str]] = []
    role_mention_rows: List[Tuple[str, str]] = []
    channel_mention_rows: List[Tuple[str, str]] = []
    users_by_id: Dict[str, Tuple] = {}
    message_ids: List[str] = []

    for payload in message_payloads:
        row = _message_row_from_payload(
            payload, channel_id=channel_id, guild_id=guild_id
        )
        if row is None:
            continue
        message_rows.append(row)
        message_id = row[0]
        message_ids.append(message_id)

        attachments = payload.get("attachments") or []
        if not isinstance(attachments, list):
            attachments = []
        attachment_rows.extend(
            _attachment_rows_from_payload(
                message_id=message_id,
                channel_id=row[1],
                guild_id=row[2],
                attachments=attachments,
                now=now,
            )
        )

        embeds = payload.get("embeds") or []
        if not isinstance(embeds, list):
            embeds = []
        embed_rows.extend(
            _embed_rows_from_payload(
                message_id=message_id,
                embeds=embeds,
                now=now,
            )
        )
        reaction_rows.extend(
            _reaction_rows_from_payload(
                message_id=message_id,
                reactions=payload.get("reactions"),
                now=now,
            )
        )

        user_mention_rows.extend(
            _id_rows(message_id, _extract_ids_from_payload(payload.get("mentions") or []))
        )
        role_mention_rows.extend(
            _id_rows(
                message_id,
                _extract_ids_from_payload(payload.get("mention_roles") or []),
            )
        )
        channel_mention_rows.extend(
            _id_rows(
                message_id,
                _extract_ids_from_payload(payload.get("mention_channels") or []),
            )
        )

        user_row = _user_row_from_payload(payload.get("author"), now)
        if user_row is not None:
            users_by_id[user_row[0]] = user_row

    if not message_rows:
        return {"messages_indexed": 0, "min_id": None, "max_id": None}

    message_id_rows = [(message_id,) for message_id in message_ids]

    def _write(conn: sqlite3.Connection) -> None:
        _upsert_users(conn, list(users_by_id.values()))
        _upsert_messages(conn, message_rows)
        if message_id_rows:
            conn.executemany(
                "DELETE FROM message_attachments WHERE message_id = ?",
                message_id_rows,
            )
            if attachment_rows:
                conn.executemany(
                    """
                    INSERT INTO message_attachments
                        (attachment_id, message_id, channel_id, guild_id, filename, description, content_type, size, url, proxy_url, height, width, ephemeral, updated_at)
                    VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    attachment_rows,
                )
            conn.executemany(
                "DELETE FROM message_embeds WHERE message_id = ?",
                message_id_rows,
            )
            if embed_rows:
                conn.executemany(
                    """
                    INSERT INTO message_embeds
                        (message_id, idx, embed_json, updated_at)
                    VALUES
                        (?, ?, ?, ?)
                    """,
                    embed_rows,
                )
            conn.executemany(
                "DELETE FROM message_reactions WHERE message_id = ?",
                message_id_rows,
            )
            if reaction_rows:
                conn.executemany(
                    """
                    INSERT INTO message_reactions
                        (message_id, emoji_key, count, me, updated_at)
                    VALUES
                        (?, ?, ?, ?, ?)
                    """,
                    reaction_rows,
                )
            conn.executemany(
                "DELETE FROM message_user_mentions WHERE message_id = ?",
                message_id_rows,
            )
            if user_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_user_mentions
                        (message_id, user_id)
                    VALUES
                        (?, ?)
                    """,
                    user_mention_rows,
                )
            conn.executemany(
                "DELETE FROM message_role_mentions WHERE message_id = ?",
                message_id_rows,
            )
            if role_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_role_mentions
                        (message_id, role_id)
                    VALUES
                        (?, ?)
                    """,
                    role_mention_rows,
                )
            conn.executemany(
                "DELETE FROM message_channel_mentions WHERE message_id = ?",
                message_id_rows,
            )
            if channel_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_channel_mentions
                        (message_id, channel_id)
                    VALUES
                        (?, ?)
                    """,
                    channel_mention_rows,
                )
        conn.commit()

    await _with_db(ctx, _write)

    numeric_ids: List[int] = []
    for message_id in message_ids:
        try:
            numeric_ids.append(int(message_id))
        except (TypeError, ValueError):
            continue

    min_id = str(min(numeric_ids)) if numeric_ids else None
    max_id = str(max(numeric_ids)) if numeric_ids else None

    return {
        "messages_indexed": len(message_rows),
        "min_id": min_id,
        "max_id": max_id,
    }


async def _search_guild_messages(
    client: discord.Client,
    *,
    guild_id: str,
    channel_id: Optional[str],
    query: Optional[str],
    author_id: Optional[str],
    min_id: Optional[str],
    max_id: Optional[str],
    offset: int,
    oldest_first: bool,
    include_nsfw: Optional[bool],
) -> Dict[str, Any]:
    route = Route("GET", "/guilds/{guild_id}/messages/search", guild_id=guild_id)
    params: Dict[str, Any] = {
        "offset": offset,
        "sort_by": "timestamp",
        "sort_order": "asc" if oldest_first else "desc",
    }
    if channel_id is not None:
        params["channel_id"] = str(channel_id)
    if query:
        params["content"] = query
    if author_id is not None:
        params["author_id"] = str(author_id)
    if min_id is not None:
        params["min_id"] = str(min_id)
    if max_id is not None:
        params["max_id"] = str(max_id)
    if include_nsfw is not None:
        params["include_nsfw"] = "true" if include_nsfw else "false"

    return await client.http.request(route, params=params)


async def index_messages_search(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    client = getattr(ctx, "discord_client", None)
    if client is None:
        raise RuntimeError("discord client not available")

    await client.wait_until_ready()

    channel_id_raw = obj.get("channel_id")
    if channel_id_raw is None:
        raise ValueError("channel_id is required.")
    channel_id = str(channel_id_raw)

    guild_id = obj.get("guild_id")
    if guild_id is None:
        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        guild = getattr(channel, "guild", None)
        if guild is None:
            raise ValueError("channel does not belong to a guild.")
        guild_id = str(guild.id)
    else:
        guild_id = str(guild_id)

    query_raw = obj.get("query")
    query = str(query_raw).strip() if query_raw is not None else None
    if not query:
        query = None

    author_id = _coerce_snowflake(obj.get("author_id"), "author_id")
    after_message_id = _coerce_snowflake(
        obj.get("after_message_id"), "after_message_id"
    )
    before_message_id = _coerce_snowflake(
        obj.get("before_message_id"), "before_message_id"
    )
    after_timestamp = obj.get("after_timestamp")
    before_timestamp = obj.get("before_timestamp")

    if after_message_id is not None and after_timestamp is not None:
        raise ValueError(
            "after_message_id cannot be combined with after_timestamp."
        )
    if before_message_id is not None and before_timestamp is not None:
        raise ValueError(
            "before_message_id cannot be combined with before_timestamp."
        )

    if after_timestamp is not None:
        after_ms = _parse_epoch_ms(after_timestamp)
        if after_ms is None:
            raise ValueError("after_timestamp must be a valid timestamp.")
        after_message_id = str(_snowflake_from_epoch_ms(after_ms))
    if before_timestamp is not None:
        before_ms = _parse_epoch_ms(before_timestamp)
        if before_ms is None:
            raise ValueError("before_timestamp must be a valid timestamp.")
        before_message_id = str(_snowflake_from_epoch_ms(before_ms, high=True))

    try:
        offset = int(obj.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    if offset < 0:
        offset = 0

    try:
        max_results = int(obj.get("max_results", DEFAULT_SEARCH_MAX_RESULTS))
    except (TypeError, ValueError):
        max_results = DEFAULT_SEARCH_MAX_RESULTS
    if max_results <= 0:
        max_results = DEFAULT_SEARCH_MAX_RESULTS
    if max_results > MAX_SEARCH_MAX_RESULTS:
        max_results = MAX_SEARCH_MAX_RESULTS

    oldest_first = bool(obj.get("oldest_first", False))
    include_nsfw = obj.get("include_nsfw")
    if include_nsfw is not None:
        include_nsfw = bool(include_nsfw)

    cursor = offset
    messages_indexed = 0
    messages_fetched = 0
    total_results: Optional[int] = None
    min_indexed_id: Optional[int] = None
    max_indexed_id: Optional[int] = None
    done = False

    while messages_indexed < max_results:
        try:
            data = await _search_guild_messages(
                client,
                guild_id=guild_id,
                channel_id=channel_id,
                query=query,
                author_id=author_id,
                min_id=after_message_id,
                max_id=before_message_id,
                offset=cursor,
                oldest_first=oldest_first,
                include_nsfw=include_nsfw,
            )
        except Exception as e:
            _LOGGER.error(
                "Search API failed for guild %s channel %s: %s",
                guild_id,
                channel_id,
                e,
                exc_info=True,
            )
            return {"ok": False, "error": str(e)}

        if total_results is None:
            try:
                total_results = int(data.get("total_results"))
            except (TypeError, ValueError):
                total_results = None

        page_messages = _flatten_search_messages(data.get("messages"))
        if not page_messages:
            done = True
            break

        page_result = await _index_message_payloads(
            ctx,
            page_messages,
            channel_id=channel_id,
            guild_id=guild_id,
        )
        messages_indexed += int(page_result.get("messages_indexed") or 0)
        messages_fetched += len(page_messages)

        page_min_id = page_result.get("min_id")
        page_max_id = page_result.get("max_id")
        if page_min_id is not None:
            try:
                page_min_value = int(page_min_id)
            except (TypeError, ValueError):
                page_min_value = None
            if page_min_value is not None:
                if min_indexed_id is None or page_min_value < min_indexed_id:
                    min_indexed_id = page_min_value
        if page_max_id is not None:
            try:
                page_max_value = int(page_max_id)
            except (TypeError, ValueError):
                page_max_value = None
            if page_max_value is not None:
                if max_indexed_id is None or page_max_value > max_indexed_id:
                    max_indexed_id = page_max_value

        cursor += len(page_messages)
        if total_results is not None and cursor >= total_results:
            done = True
            break

    return {
        "ok": True,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "query": query,
        "author_id": author_id,
        "after_message_id": after_message_id,
        "before_message_id": before_message_id,
        "offset": offset,
        "next_offset": cursor,
        "max_results": max_results,
        "messages_fetched": messages_fetched,
        "messages_indexed": messages_indexed,
        "min_indexed_id": str(min_indexed_id)
        if min_indexed_id is not None
        else None,
        "max_indexed_id": str(max_indexed_id)
        if max_indexed_id is not None
        else None,
        "total_results": total_results,
        "done": done,
    }


async def record_message_delete(
    ctx: GlobalContext, payload: discord.RawMessageDeleteEvent
) -> None:
    now = _now_ms()
    message_id = str(payload.message_id)
    channel_id = (
        str(payload.channel_id) if payload.channel_id is not None else None
    )
    guild_id = str(payload.guild_id) if payload.guild_id is not None else None

    def _write(conn: sqlite3.Connection) -> None:
        _mark_message_deleted(
            conn,
            message_id=message_id,
            channel_id=channel_id,
            guild_id=guild_id,
            deleted_at=now,
        )
        conn.commit()

    await _with_db(ctx, _write)


async def record_message_bulk_delete(
    ctx: GlobalContext, payload: discord.RawBulkMessageDeleteEvent
) -> None:
    now = _now_ms()
    channel_id = (
        str(payload.channel_id) if payload.channel_id is not None else None
    )
    guild_id = str(payload.guild_id) if payload.guild_id is not None else None
    message_ids = [str(mid) for mid in payload.message_ids or []]
    if not message_ids:
        return

    rows = [(mid, channel_id, guild_id, now) for mid in message_ids]

    def _write(conn: sqlite3.Connection) -> None:
        conn.executemany(
            """
            INSERT INTO messages (message_id, channel_id, guild_id, deleted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                channel_id = COALESCE(excluded.channel_id, messages.channel_id),
                guild_id = COALESCE(excluded.guild_id, messages.guild_id),
                deleted_at = excluded.deleted_at
            """,
            rows,
        )
        conn.commit()

    await _with_db(ctx, _write)


async def record_message_edit(
    ctx: GlobalContext, payload: discord.RawMessageUpdateEvent
) -> None:
    data = payload.data or {}
    message_id = str(payload.message_id)
    channel_id = (
        str(payload.channel_id) if payload.channel_id is not None else None
    )
    guild_id = str(payload.guild_id) if payload.guild_id is not None else None

    updates: Dict[str, Any] = {}
    attachments_seen = False
    embeds_seen = False
    user_mentions_seen = False
    role_mentions_seen = False
    channel_mentions_seen = False
    attachment_rows: List[Tuple] = []
    embed_rows: List[Tuple] = []
    user_mention_rows: List[Tuple[str, str]] = []
    role_mention_rows: List[Tuple[str, str]] = []
    channel_mention_rows: List[Tuple[str, str]] = []
    now = _now_ms()

    if "content" in data:
        content = data.get("content")
        updates["content"] = content
        updates["content_available"] = 0 if content is None else 1

    edited_ts = _parse_epoch_ms(data.get("edited_timestamp"))
    if edited_ts is not None:
        updates["edited_at"] = edited_ts

    if "mentions" in data:
        user_mentions_seen = True
        mentions = data.get("mentions") or []
        mention_ids = _extract_ids_from_payload(mentions)
        updates["mention_ids"] = json.dumps(mention_ids, ensure_ascii=True)
        user_mention_rows = _id_rows(message_id, mention_ids)

    if "mention_roles" in data:
        role_mentions_seen = True
        role_ids = _extract_ids_from_payload(data.get("mention_roles") or [])
        role_mention_rows = _id_rows(message_id, role_ids)

    if "mention_channels" in data:
        channel_mentions_seen = True
        channel_ids = _extract_ids_from_payload(data.get("mention_channels") or [])
        channel_mention_rows = _id_rows(message_id, channel_ids)

    if "mention_everyone" in data:
        updates["mention_everyone"] = int(bool(data.get("mention_everyone")))

    if "attachments" in data:
        attachments_seen = True
        attachments = data.get("attachments") or []
        if not isinstance(attachments, list):
            attachments = []
        updates["attachments_count"] = len(attachments)
        attachment_rows = _attachment_rows_from_payload(
            message_id=message_id,
            channel_id=channel_id,
            guild_id=guild_id,
            attachments=attachments,
            now=now,
        )

    if "embeds" in data:
        embeds_seen = True
        embeds = data.get("embeds") or []
        if not isinstance(embeds, list):
            embeds = []
        updates["embeds_count"] = len(embeds)
        embed_rows = _embed_rows_from_payload(
            message_id=message_id,
            embeds=embeds,
            now=now,
        )

    if "pinned" in data:
        updates["pinned"] = int(bool(data.get("pinned")))

    author = data.get("author")
    if isinstance(author, dict) and author.get("id") is not None:
        updates["author_id"] = str(author["id"])

    if not updates and not (
        user_mentions_seen
        or role_mentions_seen
        or channel_mentions_seen
        or attachments_seen
        or embeds_seen
    ):
        return

    def _write(conn: sqlite3.Connection) -> None:
        _upsert_message_fields(
            conn,
            message_id=message_id,
            channel_id=channel_id,
            guild_id=guild_id,
            fields=updates,
        )
        if user_mentions_seen:
            conn.execute(
                "DELETE FROM message_user_mentions WHERE message_id = ?",
                (message_id,),
            )
            if user_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_user_mentions
                        (message_id, user_id)
                    VALUES
                        (?, ?)
                    """,
                    user_mention_rows,
                )
        if role_mentions_seen:
            conn.execute(
                "DELETE FROM message_role_mentions WHERE message_id = ?",
                (message_id,),
            )
            if role_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_role_mentions
                        (message_id, role_id)
                    VALUES
                        (?, ?)
                    """,
                    role_mention_rows,
                )
        if channel_mentions_seen:
            conn.execute(
                "DELETE FROM message_channel_mentions WHERE message_id = ?",
                (message_id,),
            )
            if channel_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_channel_mentions
                        (message_id, channel_id)
                    VALUES
                        (?, ?)
                    """,
                    channel_mention_rows,
                )
        if attachments_seen:
            conn.execute(
                "DELETE FROM message_attachments WHERE message_id = ?",
                (message_id,),
            )
            if attachment_rows:
                conn.executemany(
                    """
                    INSERT INTO message_attachments
                        (attachment_id, message_id, channel_id, guild_id, filename, description, content_type, size, url, proxy_url, height, width, ephemeral, updated_at)
                    VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    attachment_rows,
                )
        if embeds_seen:
            conn.execute(
                "DELETE FROM message_embeds WHERE message_id = ?",
                (message_id,),
            )
            if embed_rows:
                conn.executemany(
                    """
                    INSERT INTO message_embeds
                        (message_id, idx, embed_json, updated_at)
                    VALUES
                        (?, ?, ?, ?)
                    """,
                    embed_rows,
                )
        conn.commit()

    await _with_db(ctx, _write)


async def record_role_upsert(ctx: GlobalContext, role: discord.Role) -> None:
    now = _now_ms()
    row = _role_row(role, now)

    def _write(conn: sqlite3.Connection) -> None:
        _upsert_roles(conn, [row])
        conn.commit()

    await _with_db(ctx, _write)


async def record_role_delete(ctx: GlobalContext, role: discord.Role) -> None:
    now = _now_ms()
    role_id = str(role.id)
    guild_id = str(role.guild.id) if getattr(role, "guild", None) else None

    def _write(conn: sqlite3.Connection) -> None:
        _mark_role_deleted(
            conn, role_id=role_id, guild_id=guild_id, deleted_at=now
        )
        conn.commit()

    await _with_db(ctx, _write)


async def record_guild_emojis_update(
    ctx: GlobalContext, guild: discord.Guild, emojis: List[discord.Emoji]
) -> None:
    now = _now_ms()
    guild_id = str(guild.id)
    rows: List[Tuple] = []
    emoji_ids: List[str] = []
    for emoji in emojis or []:
        emoji_id = getattr(emoji, "id", None)
        if emoji_id is None:
            continue
        rows.append(_emoji_row(emoji, guild_id, now))
        emoji_ids.append(str(emoji_id))

    def _write(conn: sqlite3.Connection) -> None:
        _replace_guild_emojis(
            conn, guild_id=guild_id, rows=rows, emoji_ids=emoji_ids
        )
        conn.commit()

    await _with_db(ctx, _write)


async def record_guild_stickers_update(
    ctx: GlobalContext, guild: discord.Guild, stickers: List[discord.StickerItem]
) -> None:
    now = _now_ms()
    guild_id = str(guild.id)
    rows: List[Tuple] = []
    sticker_ids: List[str] = []
    for sticker in stickers or []:
        sticker_id = getattr(sticker, "id", None)
        if sticker_id is None:
            continue
        rows.append(_sticker_row(sticker, guild_id, now))
        sticker_ids.append(str(sticker_id))

    def _write(conn: sqlite3.Connection) -> None:
        _replace_guild_stickers(
            conn, guild_id=guild_id, rows=rows, sticker_ids=sticker_ids
        )
        conn.commit()

    await _with_db(ctx, _write)


async def record_channel_pins_update(
    ctx: GlobalContext, channel: discord.abc.GuildChannel
) -> None:
    now = _now_ms()
    if not _can_read_channel(channel):
        await _with_db(
            ctx,
            lambda conn: _update_channel_state_pins_checked(
                conn,
                channel_id=str(channel.id),
                pins_last_checked_at=now,
                updated_at=now,
            ),
        )
        return

    channel_id = str(channel.id)
    try:
        pinned_messages = await channel.pins()
    except Exception as e:
        _LOGGER.error(
            "Failed to fetch pinned messages for channel %s: %s",
            channel_id,
            e,
            exc_info=True,
        )
        await _with_db(
            ctx,
            lambda conn: _update_channel_state_pins_checked(
                conn,
                channel_id=channel_id,
                pins_last_checked_at=now,
                updated_at=now,
            ),
        )
        return

    message_rows: List[Tuple] = []
    attachment_rows: List[Tuple] = []
    embed_rows: List[Tuple] = []
    reaction_rows: List[Tuple] = []
    user_mention_rows: List[Tuple[str, str]] = []
    role_mention_rows: List[Tuple[str, str]] = []
    channel_mention_rows: List[Tuple[str, str]] = []
    users_by_id: Dict[str, Tuple] = {}
    pinned_ids: List[str] = []
    message_ids: List[str] = []

    for message in pinned_messages:
        message_rows.append(_message_row(message, now))
        attachment_rows.extend(_attachment_rows(message, now))
        embed_rows.extend(_embed_rows(message, now))
        reaction_rows.extend(_reaction_rows(message, now))
        user_mention_rows.extend(_user_mention_rows(message))
        role_mention_rows.extend(_role_mention_rows(message))
        channel_mention_rows.extend(_channel_mention_rows(message))
        user_row = _user_row(message.author, now)
        users_by_id[user_row[0]] = user_row
        pinned_ids.append(str(message.id))
        message_ids.append(str(message.id))

    message_id_rows = [(message_id,) for message_id in message_ids]
    max_pinned_id: Optional[str] = None
    if message_ids:
        try:
            max_pinned_id = str(max(int(mid) for mid in message_ids))
        except (TypeError, ValueError):
            max_pinned_id = None

    def _write(conn: sqlite3.Connection) -> None:
        _upsert_users(conn, list(users_by_id.values()))
        _upsert_messages(conn, message_rows)
        if max_pinned_id is not None:
            _bump_channel_latest_seen(
                conn,
                channel_id=channel_id,
                guild_id=str(channel.guild.id) if getattr(channel, "guild", None) else None,
                message_id=max_pinned_id,
                now=now,
            )
        if message_id_rows:
            conn.executemany(
                "DELETE FROM message_attachments WHERE message_id = ?",
                message_id_rows,
            )
            if attachment_rows:
                conn.executemany(
                    """
                    INSERT INTO message_attachments
                        (attachment_id, message_id, channel_id, guild_id, filename, description, content_type, size, url, proxy_url, height, width, ephemeral, updated_at)
                    VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    attachment_rows,
                )
            conn.executemany(
                "DELETE FROM message_embeds WHERE message_id = ?",
                message_id_rows,
            )
            if embed_rows:
                conn.executemany(
                    """
                    INSERT INTO message_embeds
                        (message_id, idx, embed_json, updated_at)
                    VALUES
                        (?, ?, ?, ?)
                    """,
                    embed_rows,
                )
            conn.executemany(
                "DELETE FROM message_reactions WHERE message_id = ?",
                message_id_rows,
            )
            if reaction_rows:
                conn.executemany(
                    """
                    INSERT INTO message_reactions
                        (message_id, emoji_key, count, me, updated_at)
                    VALUES
                        (?, ?, ?, ?, ?)
                    """,
                    reaction_rows,
                )
            conn.executemany(
                "DELETE FROM message_user_mentions WHERE message_id = ?",
                message_id_rows,
            )
            if user_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_user_mentions
                        (message_id, user_id)
                    VALUES
                        (?, ?)
                    """,
                    user_mention_rows,
                )
            conn.executemany(
                "DELETE FROM message_role_mentions WHERE message_id = ?",
                message_id_rows,
            )
            if role_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_role_mentions
                        (message_id, role_id)
                    VALUES
                        (?, ?)
                    """,
                    role_mention_rows,
                )
            conn.executemany(
                "DELETE FROM message_channel_mentions WHERE message_id = ?",
                message_id_rows,
            )
            if channel_mention_rows:
                conn.executemany(
                    """
                    INSERT INTO message_channel_mentions
                        (message_id, channel_id)
                    VALUES
                        (?, ?)
                    """,
                    channel_mention_rows,
                )
        _update_channel_pins(conn, channel_id=channel_id, pinned_ids=pinned_ids)
        _update_channel_state_pins_checked(
            conn,
            channel_id=channel_id,
            pins_last_checked_at=now,
            updated_at=now,
        )
        conn.commit()

    await _with_db(ctx, _write)


async def record_reaction_add(
    ctx: GlobalContext,
    payload: discord.RawReactionActionEvent,
    *,
    bot_user_id: Optional[int] = None,
) -> None:
    now = _now_ms()
    emoji_key = _emoji_key_from_parts(
        getattr(payload.emoji, "name", None),
        getattr(payload.emoji, "id", None),
    )
    if not emoji_key:
        return
    message_id = str(payload.message_id)
    channel_id = (
        str(payload.channel_id) if payload.channel_id is not None else None
    )
    guild_id = str(payload.guild_id) if payload.guild_id is not None else None
    me = int(bool(bot_user_id and payload.user_id == bot_user_id))

    def _write(conn: sqlite3.Connection) -> None:
        _ensure_message_row(
            conn,
            message_id=message_id,
            channel_id=channel_id,
            guild_id=guild_id,
        )
        conn.execute(
            """
            INSERT INTO message_reactions
                (message_id, emoji_key, count, me, updated_at)
            VALUES
                (?, ?, ?, ?, ?)
            ON CONFLICT(message_id, emoji_key) DO UPDATE SET
                count = message_reactions.count + 1,
                me = CASE
                    WHEN excluded.me = 1 THEN 1
                    ELSE message_reactions.me
                END,
                updated_at = excluded.updated_at
            """,
            (message_id, emoji_key, 1, me, now),
        )
        conn.commit()

    await _with_db(ctx, _write)


async def record_reaction_remove(
    ctx: GlobalContext,
    payload: discord.RawReactionActionEvent,
    *,
    bot_user_id: Optional[int] = None,
) -> None:
    now = _now_ms()
    emoji_key = _emoji_key_from_parts(
        getattr(payload.emoji, "name", None),
        getattr(payload.emoji, "id", None),
    )
    if not emoji_key:
        return
    message_id = str(payload.message_id)
    channel_id = (
        str(payload.channel_id) if payload.channel_id is not None else None
    )
    guild_id = str(payload.guild_id) if payload.guild_id is not None else None
    me = int(bool(bot_user_id and payload.user_id == bot_user_id))

    def _write(conn: sqlite3.Connection) -> None:
        _ensure_message_row(
            conn,
            message_id=message_id,
            channel_id=channel_id,
            guild_id=guild_id,
        )
        conn.execute(
            """
            INSERT INTO message_reactions
                (message_id, emoji_key, count, me, updated_at)
            VALUES
                (?, ?, ?, ?, ?)
            ON CONFLICT(message_id, emoji_key) DO UPDATE SET
                count = CASE
                    WHEN message_reactions.count > 0 THEN message_reactions.count - 1
                    ELSE 0
                END,
                me = CASE
                    WHEN excluded.me = 1 THEN 0
                    ELSE message_reactions.me
                END,
                updated_at = excluded.updated_at
            """,
            (message_id, emoji_key, 0, me, now),
        )
        conn.commit()

    await _with_db(ctx, _write)


async def record_reaction_clear(
    ctx: GlobalContext, payload: discord.RawReactionClearEvent
) -> None:
    message_id = str(payload.message_id)

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute(
            "DELETE FROM message_reactions WHERE message_id = ?",
            (message_id,),
        )
        conn.commit()

    await _with_db(ctx, _write)


async def record_reaction_clear_emoji(
    ctx: GlobalContext, payload: discord.RawReactionClearEmojiEvent
) -> None:
    message_id = str(payload.message_id)
    emoji_key = _emoji_key_from_parts(
        getattr(payload.emoji, "name", None),
        getattr(payload.emoji, "id", None),
    )
    if not emoji_key:
        return

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            DELETE FROM message_reactions
            WHERE message_id = ? AND emoji_key = ?
            """,
            (message_id, emoji_key),
        )
        conn.commit()

    await _with_db(ctx, _write)


async def record_member_join(ctx: GlobalContext, member: discord.Member) -> None:
    await _record_member_event(ctx, member, mark_left=False)


async def record_member_update(ctx: GlobalContext, member: discord.Member) -> None:
    await _record_member_event(ctx, member, mark_left=False)


async def record_member_remove(ctx: GlobalContext, member: discord.Member) -> None:
    await _record_member_event(ctx, member, mark_left=True)


async def thread_discovery_routine(ctx: GlobalContext, obj: JSON) -> JSONDict:
    client = getattr(ctx, "discord_client", None)
    if client is None:
        _LOGGER.error("Thread discovery has no discord client available.")
        return {"ok": False, "error": "discord client not available"}

    await client.wait_until_ready()

    scan_archived = bool(_get_int_config(ctx, "indexer_scan_archived_threads", 0))
    if not scan_archived:
        return {"ok": True, "threads_indexed": 0}

    archived_limit = _get_int_config(ctx, "indexer_archived_threads_limit", 100)
    if archived_limit < 1:
        archived_limit = 1
    if archived_limit > 100:
        archived_limit = 100

    result = await _index_next_thread_parent(ctx, client, archived_limit)
    return {"ok": True, "threads_indexed": result.get("threads_indexed", 0)}


index_messages_search_schema: ToolDef = ToolDef(
    name="index_messages_search",
    function=lambda ctx, obj: index_messages_search(ctx, obj),
    schema={
        "name": "index_messages_search",
        "description": (
            "Index historical messages into the local database using the Discord "
            "message search API. Provide channel_id and optional timestamp/id bounds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Channel ID to index."},
                "guild_id": {"type": "string", "description": "Guild ID (optional if channel_id is in a guild)."},
                "query": {"type": "string", "description": "Optional search text filter."},
                "author_id": {"type": "string", "description": "Only index messages from this author ID."},
                "after_message_id": {"type": "string", "description": "Only messages after this message ID."},
                "before_message_id": {"type": "string", "description": "Only messages before this message ID."},
                "after_timestamp": {
                    "type": "string",
                    "description": "Lower bound timestamp (ISO 8601 or epoch ms).",
                },
                "before_timestamp": {
                    "type": "string",
                    "description": "Upper bound timestamp (ISO 8601 or epoch ms).",
                },
                "offset": {"type": "integer", "description": "Search result offset."},
                "max_results": {"type": "integer", "description": "Maximum messages to index this call."},
                "oldest_first": {"type": "boolean", "description": "Sort oldest-first when paging."},
                "include_nsfw": {"type": "boolean", "description": "Include NSFW results if available."},
            },
            "required": ["channel_id"],
        },
    },
)


async def background_indexer_routine(ctx: GlobalContext, obj: JSON) -> JSONDict:
    client = getattr(ctx, "discord_client", None)
    if client is None:
        _LOGGER.error("Background indexer has no discord client available.")
        return {"ok": False, "error": "discord client not available"}

    await client.wait_until_ready()

    guilds = list(client.guilds)
    if not guilds:
        return {"ok": True, "messages_indexed": 0, "members_indexed": 0}

    now_ts = time.time()
    sync_every = _get_int_config(ctx, "indexer_sync_every_seconds", 3600)
    last_sync = getattr(ctx, "_indexer_last_sync_ts", 0.0)
    if now_ts - last_sync >= sync_every:
        await _sync_guilds_and_channels(ctx, guilds)
        ctx._indexer_last_sync_ts = now_ts

    message_batch_size = _get_int_config(
        ctx, "indexer_message_batch_size", DEFAULT_MESSAGE_BATCH_SIZE
    )
    member_batch_size = _get_int_config(
        ctx, "indexer_member_batch_size", DEFAULT_MEMBER_BATCH_SIZE
    )

    backfill_channels_per_tick = _get_int_config(
        ctx,
        "indexer_backfill_channels_per_tick",
        DEFAULT_BACKFILL_CHANNELS_PER_TICK,
    )
    tail_channels_per_tick = _get_int_config(
        ctx,
        "indexer_tail_channels_per_tick",
        DEFAULT_TAIL_CHANNELS_PER_TICK,
    )

    member_result = await _index_next_guild_members(ctx, client, member_batch_size)
    messages_indexed = 0
    search_messages_indexed = 0
    pins_checked = 0
    for _ in range(backfill_channels_per_tick):
        message_result = await _index_next_channel_messages_backfill(
            ctx, client, message_batch_size
        )
        messages_indexed += message_result.get("messages_indexed", 0)
    for _ in range(tail_channels_per_tick):
        message_result = await _index_next_channel_messages_tail(
            ctx, client, message_batch_size
        )
        messages_indexed += message_result.get("messages_indexed", 0)

    search_every = _get_int_config(
        ctx, "indexer_search_every_seconds", DEFAULT_SEARCH_EVERY_SECONDS
    )
    if search_every > 0:
        last_search = getattr(ctx, "_indexer_last_search_ts", 0.0)
        if now_ts - last_search >= search_every:
            search_channels_per_tick = _get_int_config(
                ctx,
                "indexer_search_channels_per_tick",
                DEFAULT_SEARCH_CHANNELS_PER_TICK,
            )
            for _ in range(search_channels_per_tick):
                search_result = await _index_next_channel_messages_search(ctx, client)
                search_messages_indexed += search_result.get(
                    "messages_indexed", 0
                )
            ctx._indexer_last_search_ts = now_ts

    pins_check_every = _get_int_config(
        ctx,
        "indexer_pins_check_every_seconds",
        DEFAULT_PINS_CHECK_EVERY_SECONDS,
    )
    pins_channels_per_tick = _get_int_config(
        ctx,
        "indexer_pins_channels_per_tick",
        DEFAULT_PINS_CHANNELS_PER_TICK,
    )
    if pins_check_every > 0 and pins_channels_per_tick > 0:
        for _ in range(pins_channels_per_tick):
            pins_result = await _reconcile_next_channel_pins(
                ctx, client, pins_check_every * 1000
            )
            pins_checked += pins_result.get("channels_checked", 0)

    return {
        "ok": True,
        "members_indexed": member_result.get("members_indexed", 0),
        "messages_indexed": messages_indexed,
        "search_messages_indexed": search_messages_indexed,
        "pins_checked": pins_checked,
    }


background_indexer_task: RoutineTask = RoutineTask(
    name="background_indexer",
    description="Slowly index guilds, channels, users, and messages over time.",
    run_every_seconds=DEFAULT_RUN_EVERY_SECONDS,
    function=background_indexer_routine,
)


thread_discovery_task: RoutineTask = RoutineTask(
    name="thread_discovery",
    description="Slowly index archived threads with pagination and cursors.",
    run_every_seconds=DEFAULT_THREAD_DISCOVERY_RUN_EVERY_SECONDS,
    function=thread_discovery_routine,
)
