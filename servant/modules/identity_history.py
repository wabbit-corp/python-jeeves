from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from servant.defs import GlobalContext, RoutineTask, ToolDef
from typed_json import JSON, JSONDict, coerce_bool, coerce_int, coerce_str

_LOGGER = logging.getLogger(__name__)

MODULE_PROMPT = (
    "Identity history module: installs SQLite triggers into the main index DB to track changes "
    "to Discord usernames/global names/avatars and guild nicknames/avatars."
)

DEFAULT_INDEX_DB_FILENAME = "servant_index.sqlite3"

# Schema object names (kept stable so users can query/backup reliably)
USER_HISTORY_TABLE = "user_profile_history"
MEMBER_HISTORY_TABLE = "guild_member_profile_history"
TRIGGER_USERS = "trg_users_identity_history"
TRIGGER_MEMBERS = "trg_guild_members_identity_history"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _db_path(ctx: GlobalContext) -> Path:
    raw = ctx.secrets.get("indexer_db_path")
    if raw:
        return Path(coerce_str(raw, field="indexer_db_path", allow_empty=False)).expanduser().resolve()
    return (Path.cwd() / DEFAULT_INDEX_DB_FILENAME).resolve()


def _connect(dbfile: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _is_installed(conn: sqlite3.Connection) -> bool:
    tables = {
        str(r["name"])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
            (USER_HISTORY_TABLE, MEMBER_HISTORY_TABLE),
        ).fetchall()
    }
    triggers = {
        str(r["name"])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?, ?)",
            (TRIGGER_USERS, TRIGGER_MEMBERS),
        ).fetchall()
    }
    return tables == {USER_HISTORY_TABLE, MEMBER_HISTORY_TABLE} and triggers == {TRIGGER_USERS, TRIGGER_MEMBERS}


def _install_sync(dbfile: Path) -> JSONDict:
    if not dbfile.exists():
        raise FileNotFoundError(
            f"Index DB not found at {dbfile}. Start the bot once (or run the indexer init) before installing."
        )

    conn = _connect(dbfile)
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {USER_HISTORY_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_at INTEGER NOT NULL,
                source TEXT
            );
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{USER_HISTORY_TABLE}_user_time ON {USER_HISTORY_TABLE}(user_id, changed_at);"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{USER_HISTORY_TABLE}_time ON {USER_HISTORY_TABLE}(changed_at);"
        )

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MEMBER_HISTORY_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_at INTEGER NOT NULL,
                source TEXT
            );
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{MEMBER_HISTORY_TABLE}_guild_user_time ON {MEMBER_HISTORY_TABLE}(guild_id, user_id, changed_at);"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{MEMBER_HISTORY_TABLE}_time ON {MEMBER_HISTORY_TABLE}(changed_at);"
        )

        # Trigger timestamps (ms). We avoid julianday math and just do epoch seconds * 1000.
        ts_expr = "CAST(strftime('%s','now') AS INTEGER) * 1000"

        # Users: track username/global_name/discriminator/avatar_url changes.
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {TRIGGER_USERS}
            AFTER UPDATE ON users
            BEGIN
                INSERT INTO {USER_HISTORY_TABLE}(user_id, field, old_value, new_value, changed_at, source)
                SELECT old.user_id, 'name', old.name, new.name, {ts_expr}, 'users'
                WHERE old.name IS NOT new.name;

                INSERT INTO {USER_HISTORY_TABLE}(user_id, field, old_value, new_value, changed_at, source)
                SELECT old.user_id, 'global_name', old.global_name, new.global_name, {ts_expr}, 'users'
                WHERE old.global_name IS NOT new.global_name;

                INSERT INTO {USER_HISTORY_TABLE}(user_id, field, old_value, new_value, changed_at, source)
                SELECT old.user_id, 'discriminator', old.discriminator, new.discriminator, {ts_expr}, 'users'
                WHERE old.discriminator IS NOT new.discriminator;

                INSERT INTO {USER_HISTORY_TABLE}(user_id, field, old_value, new_value, changed_at, source)
                SELECT old.user_id, 'avatar_url', old.avatar_url, new.avatar_url, {ts_expr}, 'users'
                WHERE old.avatar_url IS NOT new.avatar_url;
            END;
            """
        )

        # Guild members: track nick and guild-specific avatar.
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {TRIGGER_MEMBERS}
            AFTER UPDATE ON guild_members
            BEGIN
                INSERT INTO {MEMBER_HISTORY_TABLE}(guild_id, user_id, field, old_value, new_value, changed_at, source)
                SELECT old.guild_id, old.user_id, 'nick', old.nick, new.nick, {ts_expr}, 'guild_members'
                WHERE old.nick IS NOT new.nick;

                INSERT INTO {MEMBER_HISTORY_TABLE}(guild_id, user_id, field, old_value, new_value, changed_at, source)
                SELECT old.guild_id, old.user_id, 'avatar_url', old.avatar_url, new.avatar_url, {ts_expr}, 'guild_members'
                WHERE old.avatar_url IS NOT new.avatar_url;
            END;
            """
        )

        conn.commit()
        return {
            "ok": True,
            "db_path": str(dbfile),
            "installed": True,
            "already_installed": False,
            "tables": [USER_HISTORY_TABLE, MEMBER_HISTORY_TABLE],
            "triggers": [TRIGGER_USERS, TRIGGER_MEMBERS],
        }
    finally:
        conn.close()


async def identity_history_install(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if obj is None:
        obj = {}
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    force = coerce_bool(obj.get("force"), False)
    dbfile = _db_path(ctx)

    def _work() -> JSONDict:
        conn = _connect(dbfile) if dbfile.exists() else None
        try:
            already = _is_installed(conn) if conn is not None else False
        finally:
            if conn is not None:
                conn.close()

        if already and not force:
            return {
                "ok": True,
                "db_path": str(dbfile),
                "installed": True,
                "already_installed": True,
                "tables": [USER_HISTORY_TABLE, MEMBER_HISTORY_TABLE],
                "triggers": [TRIGGER_USERS, TRIGGER_MEMBERS],
            }

        return _install_sync(dbfile)

    return await asyncio.to_thread(_work)


async def identity_history_get(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    user_id = coerce_str(obj.get("user_id"), field="user_id", allow_empty=False)
    guild_id = obj.get("guild_id")
    guild_id_s = str(guild_id) if guild_id is not None and str(guild_id).strip() else None
    limit = coerce_int(obj.get("limit"), 50)
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    dbfile = _db_path(ctx)

    def _work() -> JSONDict:
        conn = _connect(dbfile)
        try:
            if not _is_installed(conn):
                return {"ok": False, "error": "identity history not installed (run identity_history_install first)"}

            user_rows = conn.execute(
                f"""
                SELECT user_id, field, old_value, new_value, changed_at, source
                FROM {USER_HISTORY_TABLE}
                WHERE user_id = ?
                ORDER BY changed_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()

            if guild_id_s is None:
                member_rows = conn.execute(
                    f"""
                    SELECT guild_id, user_id, field, old_value, new_value, changed_at, source
                    FROM {MEMBER_HISTORY_TABLE}
                    WHERE user_id = ?
                    ORDER BY changed_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            else:
                member_rows = conn.execute(
                    f"""
                    SELECT guild_id, user_id, field, old_value, new_value, changed_at, source
                    FROM {MEMBER_HISTORY_TABLE}
                    WHERE user_id = ? AND guild_id = ?
                    ORDER BY changed_at DESC
                    LIMIT ?
                    """,
                    (user_id, guild_id_s, limit),
                ).fetchall()

            return {
                "ok": True,
                "user_id": user_id,
                "guild_id": guild_id_s,
                "user_changes": [dict(r) for r in user_rows],
                "member_changes": [dict(r) for r in member_rows],
            }
        finally:
            conn.close()

    return await asyncio.to_thread(_work)


async def identity_history_recent(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if obj is None:
        obj = {}
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    limit = coerce_int(obj.get("limit"), 50)
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    guild_id = obj.get("guild_id")
    guild_id_s = str(guild_id) if guild_id is not None and str(guild_id).strip() else None

    dbfile = _db_path(ctx)

    def _work() -> JSONDict:
        conn = _connect(dbfile)
        try:
            if not _is_installed(conn):
                return {"ok": False, "error": "identity history not installed (run identity_history_install first)"}

            user_rows = conn.execute(
                f"""
                SELECT 'user' AS scope, NULL AS guild_id, user_id, field, old_value, new_value, changed_at, source
                FROM {USER_HISTORY_TABLE}
                ORDER BY changed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            if guild_id_s is None:
                member_rows = conn.execute(
                    f"""
                    SELECT 'member' AS scope, guild_id, user_id, field, old_value, new_value, changed_at, source
                    FROM {MEMBER_HISTORY_TABLE}
                    ORDER BY changed_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                member_rows = conn.execute(
                    f"""
                    SELECT 'member' AS scope, guild_id, user_id, field, old_value, new_value, changed_at, source
                    FROM {MEMBER_HISTORY_TABLE}
                    WHERE guild_id = ?
                    ORDER BY changed_at DESC
                    LIMIT ?
                    """,
                    (guild_id_s, limit),
                ).fetchall()

            # Merge and sort in Python.
            merged = [dict(r) for r in user_rows] + [dict(r) for r in member_rows]
            merged.sort(key=lambda r: int(r.get("changed_at") or 0), reverse=True)
            merged = merged[:limit]

            return {"ok": True, "guild_id": guild_id_s, "limit": limit, "changes": merged}
        finally:
            conn.close()

    return await asyncio.to_thread(_work)


identity_history_install_tool: ToolDef = ToolDef(
    name="identity_history_install",
    function=lambda ctx, obj: identity_history_install(ctx, obj),
    schema={
        "name": "identity_history_install",
        "description": "Install identity-history tables+triggers into the main index DB to track username/nick/avatar changes.",
        "parameters": {
            "type": "object",
            "properties": {
                "force": {"type": "boolean", "description": "Recreate objects if already present."}
            },
        },
    },
)


identity_history_get_tool: ToolDef = ToolDef(
    name="identity_history_get",
    function=lambda ctx, obj: identity_history_get(ctx, obj),
    schema={
        "name": "identity_history_get",
        "description": "Fetch recorded identity changes for a user (and optional guild).",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "guild_id": {"type": "string"},
                "limit": {"type": "integer", "description": "1..500 (default 50)."},
            },
            "required": ["user_id"],
        },
    },
)


identity_history_recent_tool: ToolDef = ToolDef(
    name="identity_history_recent",
    function=lambda ctx, obj: identity_history_recent(ctx, obj),
    schema={
        "name": "identity_history_recent",
        "description": "List most recent identity changes (global and/or per guild).",
        "parameters": {
            "type": "object",
            "properties": {
                "guild_id": {"type": "string"},
                "limit": {"type": "integer", "description": "1..500 (default 50)."},
            },
        },
    },
)


identity_history_bootstrap_task: RoutineTask = RoutineTask(
    name="identity_history_bootstrap",
    description="Installs identity-history triggers into the index DB (idempotent).",
    run_every_seconds=60,
    function=lambda ctx, obj: identity_history_install(ctx, obj or {}),
)
