from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from typing import TypeVar

from servant import permissions
from servant.defs import GlobalContext, ToolDef
from servant.modules import background_indexer
from typed_json import (
    JSON,
    JSONDict,
    coerce_bool,
    coerce_int,
    coerce_optional_str,
    coerce_snowflake,
    obj_to_json,
)

_LOGGER = logging.getLogger(__name__)

MODULE_PROMPT = "Indexed message search: query the local Discord message index database."

DEFAULT_MAX_RESULTS = 25
MAX_RESULTS = 200
DEFAULT_MAX_CONTENT_CHARS = 400
MAX_CONTENT_CHARS = 2000
DEFAULT_OFFSET = 0
DEFAULT_SCAN_LIMIT = 1000
MAX_SCAN_LIMIT = 20_000
MAX_CHANNELS = 25

T = TypeVar("T")


def _clamp(value: int, min_value: int, max_value: int) -> int:
    return max(min_value, min(value, max_value))


def _normalize_query(value: object | None) -> str:
    return str(value).strip() if value is not None else ""


def _ms_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    ts = dt.datetime.fromtimestamp(value / 1000.0, tz=dt.timezone.utc)
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object | None, field: str) -> int | None:
    if value is None:
        return None
    parsed = background_indexer._parse_epoch_ms(value)
    if parsed is None:
        raise ValueError(f"{field} must be a valid timestamp.")
    return parsed


def _parse_snowflake_timestamp(value: str, field: str) -> int:
    parsed = background_indexer._snowflake_timestamp_ms(value)
    if parsed is None:
        raise ValueError(f"{field} must be a valid snowflake id.")
    return parsed


def _parse_id(value: object | None, field: str) -> str | None:
    if value is None:
        return None
    parsed = coerce_snowflake(value, field)
    if parsed is None:
        raise ValueError(f"{field} must be a valid snowflake id.")
    return parsed


def _parse_id_list(value: object | None, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of snowflake ids.")
    ids: list[str] = []
    for item in value:
        if item is None:
            raise ValueError(f"{field} must be a list of snowflake ids.")
        parsed = coerce_snowflake(item, field)
        if parsed is None:
            raise ValueError(f"{field} must be a list of snowflake ids.")
        ids.append(parsed)
    return ids


def _unique_ids(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def _parse_match(value: object | None) -> str:
    match = coerce_optional_str(value) or "substring"
    match = match.lower()
    if match not in {"substring", "exact", "regex"}:
        raise ValueError("match must be one of: substring, exact, regex")
    return match


def _parse_mention_ids(value: object | None) -> list[JSON]:
    if value is None:
        return []
    mentions: list[JSON] = []
    if isinstance(value, list):
        for item in value:
            if item is not None:
                mentions.append(str(item))
        return mentions
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        _LOGGER.debug("Failed to parse mention_ids payload: %s", value)
        return []
    if isinstance(parsed, list):
        for item in parsed:
            if item is not None:
                mentions.append(str(item))
    return mentions


def _truncate_content(content: str, max_content_chars: int) -> str:
    if max_content_chars == 0:
        return ""
    if max_content_chars > 0 and len(content) > max_content_chars:
        return content[:max_content_chars] + "..."
    return content


def _author_payload(row: sqlite3.Row) -> JSONDict:
    author_id = row["author_id"]
    author_bot = row["author_bot"]
    return {
        "id": str(author_id) if author_id is not None else None,
        "name": row["author_name"],
        "global_name": row["author_global_name"],
        "bot": (bool(author_bot) if author_bot is not None else None),
    }


def _message_payload(row: sqlite3.Row, max_content_chars: int) -> JSONDict:
    message_id = row["message_id"]
    channel_id = row["channel_id"]
    guild_id = row["guild_id"]

    content_raw = row["content"]
    content_text = "" if content_raw is None else str(content_raw)
    content_text = _truncate_content(content_text, max_content_chars)

    jump_url = None
    if message_id and channel_id:
        jump_guild = guild_id if guild_id else "@me"
        jump_url = f"https://discord.com/channels/{jump_guild}/{channel_id}/{message_id}"

    return {
        "message_id": str(message_id) if message_id is not None else None,
        "channel_id": str(channel_id) if channel_id is not None else None,
        "channel_name": row["channel_name"],
        "channel_type": row["channel_type"],
        "guild_id": str(guild_id) if guild_id is not None else None,
        "guild_name": row["guild_name"],
        "author": _author_payload(row),
        "content": content_text,
        "content_available": bool(row["content_available"] or 0),
        "created_at": _ms_to_iso(row["created_at"]),
        "edited_at": _ms_to_iso(row["edited_at"]),
        "deleted_at": _ms_to_iso(row["deleted_at"]),
        "reply_to_message_id": row["reply_to_message_id"],
        "reply_to_channel_id": row["reply_to_channel_id"],
        "reply_to_guild_id": row["reply_to_guild_id"],
        "mention_ids": _parse_mention_ids(row["mention_ids"]),
        "mention_everyone": bool(row["mention_everyone"] or 0),
        "attachments_count": int(row["attachments_count"] or 0),
        "embeds_count": int(row["embeds_count"] or 0),
        "pinned": bool(row["pinned"] or 0),
        "jump_url": jump_url,
    }


def _build_where_clause(
    *,
    channel_ids: list[str],
    guild_id: str | None,
    author_id: str | None,
    include_deleted: bool,
    include_unavailable: bool,
    query: str,
    match: str,
    case_sensitive: bool,
    after_ms: int | None,
    before_ms: int | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []

    if channel_ids:
        placeholders = ", ".join(["?"] * len(channel_ids))
        clauses.append(f"m.channel_id IN ({placeholders})")
        params.extend(channel_ids)

    if guild_id is not None:
        clauses.append("m.guild_id = ?")
        params.append(guild_id)

    if author_id is not None:
        clauses.append("m.author_id = ?")
        params.append(author_id)

    if not include_deleted:
        clauses.append("m.deleted_at IS NULL")

    if query:
        clauses.append("m.content IS NOT NULL")
        clauses.append("m.content_available = 1")
    elif not include_unavailable:
        clauses.append("m.content_available = 1")

    if after_ms is not None:
        clauses.append("m.created_at > ?")
        params.append(after_ms)

    if before_ms is not None:
        clauses.append("m.created_at < ?")
        params.append(before_ms)

    if query and match != "regex":
        if match == "substring":
            if case_sensitive:
                clauses.append("instr(m.content, ?) > 0")
                params.append(query)
            else:
                clauses.append("instr(lower(m.content), lower(?)) > 0")
                params.append(query)
        elif match == "exact":
            if case_sensitive:
                clauses.append("m.content = ?")
                params.append(query)
            else:
                clauses.append("lower(m.content) = lower(?)")
                params.append(query)

    where_sql = ""
    if clauses:
        where_sql = " WHERE " + " AND ".join(clauses)
    return where_sql, params


def _scan_regex_rows(
    conn: sqlite3.Connection,
    *,
    base_sql: str,
    where_sql: str,
    params: list[object],
    rx: re.Pattern[str],
    max_results: int,
    offset: int,
    scan_limit: int,
    order_sql: str,
) -> tuple[list[sqlite3.Row], int]:
    results: list[sqlite3.Row] = []
    matched = 0
    scanned = 0
    batch_size = min(200, scan_limit)

    while scanned < scan_limit:
        limit = min(batch_size, scan_limit - scanned)
        sql = f"{base_sql}{where_sql}{order_sql} LIMIT ? OFFSET ?"
        rows = conn.execute(sql, params + [limit, scanned]).fetchall()
        if not rows:
            break
        scanned += len(rows)

        for row in rows:
            content_raw = row["content"]
            if content_raw is None:
                continue
            content_text = str(content_raw)
            if not rx.search(content_text):
                continue
            if matched < offset:
                matched += 1
                continue
            results.append(row)
            if len(results) >= max_results:
                return results, scanned

        if len(rows) < limit:
            break

    return results, scanned


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


async def indexed_messages_search(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    query = _normalize_query(obj.get("query"))
    match = _parse_match(obj.get("match"))
    case_sensitive = coerce_bool(obj.get("case_sensitive"), default=False)

    channel_id = _parse_id(obj.get("channel_id"), "channel_id")
    channel_ids = _parse_id_list(obj.get("channel_ids"), "channel_ids")
    if channel_id is not None:
        channel_ids.append(channel_id)
    channel_ids = _unique_ids(channel_ids)
    if len(channel_ids) > MAX_CHANNELS:
        raise ValueError(f"channel_ids may not exceed {MAX_CHANNELS} entries.")

    guild_id = _parse_id(obj.get("guild_id"), "guild_id")

    guild_ids: set[str] = set()
    if guild_id is not None:
        guild_ids.add(guild_id)
    if channel_ids:
        for cid in channel_ids:
            resolved = await permissions.resolve_guild_id_for_channel(ctx, cid)
            if resolved is None:
                if guild_id is None:
                    raise PermissionError(f"Unable to resolve guild for channel {cid}.")
                continue
            guild_ids.add(resolved)

    if not guild_ids:
        request = permissions.require_request(ctx)
        if request.guild_id is not None:
            guild_ids.add(request.guild_id)

    await permissions.require_admin_for_guilds(ctx, guild_ids=guild_ids, action="search indexed messages")
    author_id = _parse_id(obj.get("author_id"), "author_id")

    after_message_id = _parse_id(obj.get("after_message_id"), "after_message_id")
    before_message_id = _parse_id(obj.get("before_message_id"), "before_message_id")
    after_timestamp = obj.get("after_timestamp")
    before_timestamp = obj.get("before_timestamp")

    if after_message_id is not None and after_timestamp is not None:
        raise ValueError("after_message_id cannot be combined with after_timestamp.")
    if before_message_id is not None and before_timestamp is not None:
        raise ValueError("before_message_id cannot be combined with before_timestamp.")

    after_ms = _parse_timestamp(after_timestamp, "after_timestamp")
    if after_ms is None and after_message_id is not None:
        after_ms = _parse_snowflake_timestamp(after_message_id, "after_message_id")

    before_ms = _parse_timestamp(before_timestamp, "before_timestamp")
    if before_ms is None and before_message_id is not None:
        before_ms = _parse_snowflake_timestamp(before_message_id, "before_message_id")

    include_deleted = coerce_bool(obj.get("include_deleted"), default=False)
    include_unavailable = coerce_bool(obj.get("include_unavailable"), default=False)

    max_results = _clamp(coerce_int(obj.get("max_results"), DEFAULT_MAX_RESULTS), 1, MAX_RESULTS)
    max_content_chars = _clamp(
        coerce_int(obj.get("max_content_chars"), DEFAULT_MAX_CONTENT_CHARS),
        0,
        MAX_CONTENT_CHARS,
    )
    offset = max(0, coerce_int(obj.get("offset"), DEFAULT_OFFSET))
    oldest_first = coerce_bool(obj.get("oldest_first"), default=False)

    scan_limit = _clamp(coerce_int(obj.get("scan_limit"), DEFAULT_SCAN_LIMIT), 1, MAX_SCAN_LIMIT)

    base_sql = """
        SELECT
            m.message_id,
            m.channel_id,
            m.guild_id,
            m.author_id,
            m.content,
            m.content_available,
            m.created_at,
            m.edited_at,
            m.deleted_at,
            m.reply_to_message_id,
            m.reply_to_channel_id,
            m.reply_to_guild_id,
            m.mention_ids,
            m.mention_everyone,
            m.attachments_count,
            m.embeds_count,
            m.pinned,
            c.name AS channel_name,
            c.type AS channel_type,
            g.name AS guild_name,
            u.name AS author_name,
            u.global_name AS author_global_name,
            u.bot AS author_bot
        FROM messages m
        LEFT JOIN channels c ON c.channel_id = m.channel_id
        LEFT JOIN guilds g ON g.guild_id = m.guild_id
        LEFT JOIN users u ON u.user_id = m.author_id
        """

    where_sql, params = _build_where_clause(
        channel_ids=channel_ids,
        guild_id=guild_id,
        author_id=author_id,
        include_deleted=include_deleted,
        include_unavailable=include_unavailable,
        query=query,
        match=match,
        case_sensitive=case_sensitive,
        after_ms=after_ms,
        before_ms=before_ms,
    )

    order_dir = "ASC" if oldest_first else "DESC"
    order_sql = f" ORDER BY m.created_at {order_dir}, m.message_id {order_dir}"

    scanned = 0
    if match == "regex" and query:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            rx = re.compile(query, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex: {exc}") from exc

        rows, scanned = await _with_db(
            ctx,
            lambda conn: _scan_regex_rows(
                conn,
                base_sql=base_sql,
                where_sql=where_sql,
                params=params,
                rx=rx,
                max_results=max_results,
                offset=offset,
                scan_limit=scan_limit,
                order_sql=order_sql,
            ),
        )
    else:
        sql = f"{base_sql}{where_sql}{order_sql} LIMIT ? OFFSET ?"
        rows = await _with_db(ctx, lambda conn: conn.execute(sql, params + [max_results, offset]).fetchall())

    results = [_message_payload(row, max_content_chars) for row in rows]

    payload: JSONDict = {
        "ok": True,
        "query": query,
        "match": match,
        "case_sensitive": case_sensitive,
        "guild_id": guild_id,
        "channel_ids": obj_to_json(channel_ids),
        "author_id": author_id,
        "after_timestamp": _ms_to_iso(after_ms),
        "before_timestamp": _ms_to_iso(before_ms),
        "max_results": max_results,
        "offset": offset,
        "oldest_first": oldest_first,
        "include_deleted": include_deleted,
        "include_unavailable": include_unavailable,
        "messages_returned": len(results),
        "results": obj_to_json(results),
    }
    if match == "regex" and query:
        payload["rows_scanned"] = scanned
        payload["scan_limit"] = scan_limit
    return payload


indexed_messages_search_schema: ToolDef = ToolDef(
    name="indexed_messages_search",
    function=lambda ctx, obj: indexed_messages_search(ctx, obj),
    schema={
        "name": "indexed_messages_search",
        "description": (
            "Search the local indexed Discord messages database. Supports filtering by "
            "guild, channel, author, and timestamp bounds with substring/exact/regex matching."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to match in message content."},
                "match": {
                    "type": "string",
                    "description": "Match mode: substring, exact, or regex.",
                    "enum": ["substring", "exact", "regex"],
                },
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive matching."},
                "channel_id": {"type": "string", "description": "Channel ID to search."},
                "channel_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of channel IDs to search.",
                },
                "guild_id": {"type": "string", "description": "Guild ID to search."},
                "author_id": {"type": "string", "description": "Only return messages from this author ID."},
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
                "offset": {"type": "integer", "description": "Offset into matching results."},
                "max_results": {"type": "integer", "description": "Maximum number of messages to return."},
                "scan_limit": {
                    "type": "integer",
                    "description": "Maximum rows to scan when using regex matching.",
                },
                "oldest_first": {"type": "boolean", "description": "Return oldest messages first."},
                "include_deleted": {"type": "boolean", "description": "Include deleted messages."},
                "include_unavailable": {
                    "type": "boolean",
                    "description": "Include messages without stored content when no query is provided.",
                },
                "max_content_chars": {
                    "type": "integer",
                    "description": "Truncate message content in results (0 for empty).",
                },
            },
        },
    },
)
