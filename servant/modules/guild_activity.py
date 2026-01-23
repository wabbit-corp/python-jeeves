from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from typing import TypeVar

from servant.defs import GlobalContext, ToolDef
from servant.modules import background_indexer
from typed_json import JSON, JSONDict, coerce_bool, coerce_int, coerce_snowflake, obj_to_json, require_obj

MODULE_PROMPT = "Guild activity: query the local Discord message index for message counts per user."

T = TypeVar("T")

DEFAULT_MAX_MESSAGES = 5
DEFAULT_MAX_RESULTS = 200
MAX_RESULTS = 2000


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


async def _with_db(ctx: GlobalContext, fn: Callable[[sqlite3.Connection], T]) -> T:
    await background_indexer.init_db(ctx)
    dbfile = background_indexer._db_path(ctx)

    def _run() -> T:
        dbfile.parent.mkdir(parents=True, exist_ok=True)
        conn = background_indexer._connect(dbfile)
        try:
            return fn(conn)
        finally:
            conn.close()

    return await asyncio.to_thread(_run)


async def guild_users_with_message_count_at_most(ctx: GlobalContext, obj: JSON) -> JSONDict:
    data = require_obj(obj)

    guild_id = coerce_snowflake(data.get("guild_id"), "guild_id")
    if guild_id is None:
        raise ValueError("guild_id is required (snowflake).")

    max_messages = _clamp(coerce_int(data.get("max_messages"), DEFAULT_MAX_MESSAGES), 0, 1_000_000)
    max_results = _clamp(coerce_int(data.get("max_results"), DEFAULT_MAX_RESULTS), 1, MAX_RESULTS)
    offset = max(0, coerce_int(data.get("offset"), 0))

    include_bots = coerce_bool(data.get("include_bots"), default=True)
    include_deleted = coerce_bool(data.get("include_deleted"), default=False)

    def _query(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        conn.row_factory = sqlite3.Row

        where = ["m.guild_id = ?", "m.author_id IS NOT NULL"]
        params: list[object] = [str(guild_id)]

        if not include_deleted:
            where.append("m.deleted_at IS NULL")

        if not include_bots:
            # If we don't know (u.bot is NULL), include them.
            where.append("(u.bot IS NULL OR u.bot = 0)")

        where_sql = " AND ".join(where)

        sql = f"""
            SELECT
                m.author_id AS user_id,
                u.name AS name,
                u.global_name AS global_name,
                u.bot AS bot,
                COUNT(*) AS message_count
            FROM messages m
            LEFT JOIN users u ON u.user_id = m.author_id
            WHERE {where_sql}
            GROUP BY m.author_id
            HAVING message_count <= ?
            ORDER BY message_count ASC, COALESCE(u.name, '') ASC, m.author_id ASC
            LIMIT ? OFFSET ?
        """

        return conn.execute(sql, params + [max_messages, max_results, offset]).fetchall()

    rows = await _with_db(ctx, _query)

    results: list[JSONDict] = []
    for row in rows:
        bot_val = row["bot"]
        results.append(
            {
                "id": str(row["user_id"]) if row["user_id"] is not None else None,
                "name": row["name"],
                "global_name": row["global_name"],
                "bot": (bool(bot_val) if bot_val is not None else None),
                "message_count": int(row["message_count"] or 0),
            }
        )

    return {
        "ok": True,
        "guild_id": str(guild_id),
        "max_messages": max_messages,
        "include_bots": include_bots,
        "include_deleted": include_deleted,
        "max_results": max_results,
        "offset": offset,
        "users_returned": len(results),
        "results": obj_to_json(results),
        "note": (
            "Counts are based on Vox's local indexed messages database. "
            "If the guild/channel history is not fully indexed, results will be incomplete."
        ),
    }


guild_users_with_message_count_at_most_tool: ToolDef = ToolDef(
    name="guild_users_with_message_count_at_most",
    function=lambda ctx, obj: guild_users_with_message_count_at_most(ctx, obj),
    schema={
        "name": "guild_users_with_message_count_at_most",
        "description": (
            "List guild users whose message count (in the local indexed DB) is <= max_messages. "
            "Useful for finding low-activity accounts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "guild_id": {"type": "string", "description": "Discord guild ID (snowflake)."},
                "max_messages": {
                    "type": "integer",
                    "description": "Return users with message_count <= this value.",
                    "default": DEFAULT_MAX_MESSAGES,
                },
                "include_bots": {"type": "boolean", "description": "Include bots.", "default": True},
                "include_deleted": {"type": "boolean", "description": "Include deleted messages in counts.", "default": False},
                "max_results": {"type": "integer", "description": "Max users to return.", "default": DEFAULT_MAX_RESULTS},
                "offset": {"type": "integer", "description": "Offset for paging.", "default": 0},
            },
            "required": ["guild_id"],
        },
    },
)
