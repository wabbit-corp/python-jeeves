#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typed_json import JSON, JSONDict

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MAX_SQL_PARAMS = 900


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _chunked(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(values), size):
        yield list(values[idx : idx + size])


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (name,),
    ).fetchone()
    return row is not None


def _parse_json_list(value: object | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item is not None]
    return []


def _coerce_int(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
    return None


def _timestamp_ms_from_row(created_at: object | None, message_id: str) -> int | None:
    parsed = _coerce_int(created_at)
    if parsed is not None:
        return parsed
    from servant.modules import background_indexer

    return background_indexer._snowflake_timestamp_ms(message_id)


def _timestamp_seconds(created_at: object | None, message_id: str) -> int | None:
    timestamp_ms = _timestamp_ms_from_row(created_at, message_id)
    if timestamp_ms is None:
        return None
    return int(timestamp_ms / 1000)


def _select_channel_ids(
    conn: sqlite3.Connection,
    channel_ids: Sequence[str] | None,
    guild_ids: Sequence[str] | None,
    *,
    include_empty: bool,
) -> list[str]:
    if channel_ids:
        return _dedupe(channel_ids)

    if guild_ids:
        resolved = _dedupe(guild_ids)
        results: list[str] = []
        if include_empty:
            query = "SELECT channel_id FROM channels WHERE guild_id IN ({})"
        else:
            query = """
                SELECT DISTINCT channel_id
                FROM messages
                WHERE guild_id IN ({})
                  AND content_available = 1
                  AND content IS NOT NULL
                  AND deleted_at IS NULL
            """
        for chunk in _chunked(resolved, MAX_SQL_PARAMS):
            placeholders = ", ".join(["?"] * len(chunk))
            rows = conn.execute(query.format(placeholders), chunk).fetchall()
            results.extend(str(row["channel_id"]) for row in rows if row["channel_id"] is not None)
        return _dedupe(results)

    if include_empty:
        rows = conn.execute("SELECT channel_id FROM channels").fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT channel_id
            FROM messages
            WHERE content_available = 1
              AND content IS NOT NULL
              AND deleted_at IS NULL
            """
        ).fetchall()
    return _dedupe([str(row["channel_id"]) for row in rows if row["channel_id"] is not None])


def _load_channel_row(conn: sqlite3.Connection, channel_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM channels WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    return row


def _load_guild_row(conn: sqlite3.Connection, guild_id: str | None) -> sqlite3.Row | None:
    if guild_id is None:
        return None
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM guilds WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    return row


def _load_channel_name_map(conn: sqlite3.Connection, guild_id: str | None) -> dict[str, str]:
    if guild_id is None:
        return {}
    rows = conn.execute(
        "SELECT channel_id, name FROM channels WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return {
        str(row["channel_id"]): str(row["name"] or row["channel_id"]) for row in rows if row["channel_id"] is not None
    }


def _build_channel_path(
    channel_id: str,
    channel_name: str | None,
    guild_name: str | None,
    parent_id: str | None,
    channel_name_map: dict[str, str],
) -> str:
    name = channel_name or channel_id
    parts: list[str] = []
    if guild_name:
        parts.append(guild_name)
    if parent_id:
        parts.append(channel_name_map.get(parent_id, parent_id))
    parts.append(name)
    return "/".join(parts)


def _load_conversation_data(
    conn: sqlite3.Connection,
    channel_id: str,
    *,
    require: bool,
) -> tuple[dict[str, int], dict[str, JSON], dict[str, str]]:
    if not _table_exists(conn, "codi_conversations") or not _table_exists(conn, "codi_conversation_messages"):
        if require:
            raise RuntimeError("CODI conversation tables not found. Run codi_conversation_indexer first.")
        return {}, {}, {}

    conv_rows = conn.execute(
        """
        SELECT
            conversation_id,
            name,
            name_hash,
            content_hash,
            message_count,
            first_message_id,
            last_message_id,
            created_at,
            updated_at,
            named_at
        FROM codi_conversations
        WHERE channel_id = ?
        ORDER BY created_at ASC, conversation_id ASC
        """,
        (channel_id,),
    ).fetchall()

    conv_index: dict[str, int] = {}
    conv_meta: dict[str, JSON] = {}
    for idx, row in enumerate(conv_rows, start=1):
        conv_id = str(row["conversation_id"])
        conv_index[conv_id] = idx
        conv_meta[conv_id] = {
            "conversation_id": conv_id,
            "label": f"T{idx}",
            "name": row["name"],
            "name_hash": row["name_hash"],
            "content_hash": row["content_hash"],
            "message_count": row["message_count"],
            "first_message_id": row["first_message_id"],
            "last_message_id": row["last_message_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "named_at": row["named_at"],
        }

    message_rows = conn.execute(
        """
        SELECT message_id, conversation_id
        FROM codi_conversation_messages
        WHERE channel_id = ?
        """,
        (channel_id,),
    ).fetchall()

    conv_by_message: dict[str, str] = {}
    for row in message_rows:
        message_id = row["message_id"]
        conv_id = row["conversation_id"]
        if message_id is None or conv_id is None:
            continue
        conv_by_message[str(message_id)] = str(conv_id)

    return conv_index, conv_meta, conv_by_message


def _load_attachment_map(
    conn: sqlite3.Connection,
    channel_id: str,
    message_ids: set[str],
) -> dict[str, list[JSON]]:
    if not message_ids:
        return {}
    rows = conn.execute(
        """
        SELECT
            attachment_id,
            message_id,
            filename,
            description,
            content_type,
            size,
            url,
            proxy_url,
            height,
            width,
            ephemeral,
            updated_at
        FROM message_attachments
        WHERE channel_id = ?
        ORDER BY message_id ASC, attachment_id ASC
        """,
        (channel_id,),
    ).fetchall()

    attachments: dict[str, list[JSON]] = {}
    for row in rows:
        message_id = row["message_id"]
        if message_id is None:
            continue
        message_id_str = str(message_id)
        if message_id_str not in message_ids:
            continue
        entry: JSONDict = {"url": str(row["url"])}
        attachment_id = row["attachment_id"]
        if attachment_id is not None:
            entry["id"] = str(attachment_id)
        filename = row["filename"]
        if filename is not None:
            entry["filename"] = str(filename)
        description = row["description"]
        if description is not None:
            entry["description"] = str(description)
        content_type = row["content_type"]
        if content_type is not None:
            entry["content_type"] = str(content_type)
        size = row["size"]
        if size is not None:
            entry["size"] = int(size)
        proxy_url = row["proxy_url"]
        if proxy_url is not None:
            entry["proxy_url"] = str(proxy_url)
        height = row["height"]
        if height is not None:
            entry["height"] = int(height)
        width = row["width"]
        if width is not None:
            entry["width"] = int(width)
        ephemeral = row["ephemeral"]
        if ephemeral is not None:
            entry["ephemeral"] = bool(ephemeral)
        updated_at = row["updated_at"]
        if updated_at is not None:
            entry["updated_at"] = int(updated_at)
        attachments.setdefault(message_id_str, []).append(entry)
    return attachments


def _load_members(
    conn: sqlite3.Connection,
    member_ids: set[str],
    guild_id: str | None,
) -> list[JSON]:
    if not member_ids:
        return []
    ids_list = sorted(member_ids)

    nick_map: dict[str, str | None] = {}
    if guild_id is not None:
        for chunk in _chunked(ids_list, MAX_SQL_PARAMS):
            placeholders = ", ".join(["?"] * len(chunk))
            rows = conn.execute(
                f"""
                SELECT user_id, nick
                FROM guild_members
                WHERE guild_id = ?
                  AND user_id IN ({placeholders})
                """,
                [guild_id, *chunk],
            ).fetchall()
            for row in rows:
                user_id = row["user_id"]
                if user_id is None:
                    continue
                nick_map[str(user_id)] = row["nick"]

    user_map: dict[str, tuple[str | None, str | None, str | None]] = {}
    for chunk in _chunked(ids_list, MAX_SQL_PARAMS):
        placeholders = ", ".join(["?"] * len(chunk))
        rows = conn.execute(
            f"""
            SELECT user_id, name, global_name, discriminator
            FROM users
            WHERE user_id IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        for row in rows:
            user_id = row["user_id"]
            if user_id is None:
                continue
            user_map[str(user_id)] = (
                row["name"],
                row["global_name"],
                row["discriminator"],
            )

    members: list[JSON] = []
    for user_id in ids_list:
        nick = nick_map.get(user_id)
        name, global_name, discriminator = user_map.get(user_id, (None, None, None))
        display_name = nick or global_name or name or user_id

        entry: JSONDict = {"id": user_id, "name": str(display_name)}
        if nick is not None:
            entry["nickname"] = str(nick)
        if name is not None:
            entry["username"] = str(name)
        if global_name is not None:
            entry["globalName"] = str(global_name)
        if name is not None and discriminator is not None:
            entry["uniqueName"] = f"{name}#{discriminator}"
        members.append(entry)
    return members


def _build_channel_payload(
    channel_id: str,
    channel_row: sqlite3.Row | None,
    guild_name: str | None,
    channel_name_map: dict[str, str],
    messages: list[JSON],
) -> JSONDict:
    channel_name = None
    parent_id = None
    topic = None
    payload: JSONDict = {
        "id": channel_id,
        "topics": [],
        "messages": messages,
    }
    if channel_row is not None:
        channel_name = channel_row["name"]
        parent_id = channel_row["parent_id"]
        topic = channel_row["topic"]
        if channel_name is not None:
            payload["name"] = str(channel_name)
        if channel_row["type"] is not None:
            payload["type"] = str(channel_row["type"])
        if channel_row["position"] is not None:
            payload["position"] = int(channel_row["position"])
        if channel_row["parent_id"] is not None:
            payload["parent_id"] = str(channel_row["parent_id"])
        if channel_row["nsfw"] is not None:
            payload["nsfw"] = bool(channel_row["nsfw"])
        if channel_row["slowmode_delay"] is not None:
            payload["slowmode_delay"] = int(channel_row["slowmode_delay"])
        if channel_row["created_at"] is not None:
            payload["created_at"] = int(channel_row["created_at"])
        if channel_row["updated_at"] is not None:
            payload["updated_at"] = int(channel_row["updated_at"])
        extra_json = channel_row["extra_json"]
        if extra_json:
            try:
                payload["extra"] = json.loads(str(extra_json))
            except json.JSONDecodeError:
                payload["extra"] = str(extra_json)

    path = _build_channel_path(
        channel_id,
        str(channel_name) if channel_name is not None else None,
        guild_name,
        str(parent_id) if parent_id is not None else None,
        channel_name_map,
    )
    payload["path"] = path
    if topic is not None:
        payload["topics"] = [{"description": str(topic), "keywords": []}]

    return payload


def _export_channel(
    conn: sqlite3.Connection,
    channel_id: str,
    *,
    require_conversations: bool,
) -> JSONDict:
    channel_row = _load_channel_row(conn, channel_id)
    guild_id = None
    channel_name = None
    if channel_row is not None:
        if channel_row["guild_id"] is not None:
            guild_id = str(channel_row["guild_id"])
        channel_name = channel_row["name"]

    if guild_id is None:
        row = conn.execute(
            "SELECT guild_id FROM messages WHERE channel_id = ? AND guild_id IS NOT NULL LIMIT 1",
            (channel_id,),
        ).fetchone()
        if row is not None and row["guild_id"] is not None:
            guild_id = str(row["guild_id"])

    guild_row = _load_guild_row(conn, guild_id)
    guild_name = str(guild_row["name"]) if guild_row and guild_row["name"] is not None else None

    conv_index, conv_meta, conv_by_message = _load_conversation_data(
        conn,
        channel_id,
        require=require_conversations,
    )

    message_rows = conn.execute(
        """
        SELECT
            m.message_id,
            m.author_id,
            m.content,
            m.created_at,
            m.edited_at,
            m.mention_ids,
            m.mention_everyone,
            m.attachments_count,
            m.embeds_count,
            m.pinned,
            COALESCE(gm.nick, u.global_name, u.name, m.author_id) AS author_name
        FROM messages m
        LEFT JOIN users u ON u.user_id = m.author_id
        LEFT JOIN guild_members gm
          ON gm.user_id = m.author_id AND gm.guild_id = m.guild_id
        WHERE m.channel_id = ?
          AND m.content IS NOT NULL
          AND m.content_available = 1
          AND m.deleted_at IS NULL
        ORDER BY m.created_at ASC, m.message_id ASC
        """,
        (channel_id,),
    ).fetchall()

    message_ids = {str(row["message_id"]) for row in message_rows if row["message_id"] is not None}
    attachment_map = _load_attachment_map(conn, channel_id, message_ids)

    author_ids: set[str] = set()
    mention_ids: set[str] = set()
    messages: list[JSON] = []
    for row in message_rows:
        message_id = row["message_id"]
        author_id = row["author_id"]
        if message_id is None or author_id is None:
            continue
        message_id_str = str(message_id)
        author_id_str = str(author_id)
        timestamp = _timestamp_seconds(row["created_at"], message_id_str)
        if timestamp is None:
            continue
        content = row["content"]
        content_text = "" if content is None else str(content)

        author_ids.add(author_id_str)
        parsed_mentions = _parse_json_list(row["mention_ids"])
        mention_ids.update(parsed_mentions)
        parsed_mentions_json: list[JSON] = []
        for mention in parsed_mentions:
            parsed_mentions_json.append(mention)

        message: JSONDict = {
            "id": message_id_str,
            "authorId": author_id_str,
            "content": content_text,
            "timestamp": timestamp,
        }
        author_name = row["author_name"]
        if author_name is not None:
            message["authorName"] = str(author_name)

        timestamp_ms = _timestamp_ms_from_row(row["created_at"], message_id_str)
        if timestamp_ms is not None:
            message["timestamp_ms"] = int(timestamp_ms)
        edited_at = row["edited_at"]
        if edited_at is not None:
            message["edited_at_ms"] = int(edited_at)
        mention_everyone = row["mention_everyone"]
        if mention_everyone is not None:
            message["mention_everyone"] = bool(mention_everyone)
        if parsed_mentions_json:
            message["mention_ids"] = parsed_mentions_json
        pinned = row["pinned"]
        if pinned is not None:
            message["pinned"] = bool(pinned)
        attachments_count = row["attachments_count"]
        if attachments_count is not None:
            message["attachments_count"] = int(attachments_count)
        embeds_count = row["embeds_count"]
        if embeds_count is not None:
            message["embeds_count"] = int(embeds_count)

        attachments = attachment_map.get(message_id_str)
        if attachments:
            message["attachments"] = attachments

        conv_id = conv_by_message.get(message_id_str)
        if conv_id is not None and conv_id in conv_index:
            message["conversation"] = f"T{conv_index[conv_id]}"
            message["conversation_id"] = conv_id

        messages.append(message)

    member_ids = author_ids | mention_ids
    members = _load_members(conn, member_ids, guild_id)

    channel_name_map = _load_channel_name_map(conn, guild_id)
    channel_payload = _build_channel_payload(channel_id, channel_row, guild_name, channel_name_map, messages)

    community_id = guild_id or channel_id
    community_name = guild_name or (str(channel_name) if channel_name is not None else channel_id)
    channels: list[JSON] = [channel_payload]
    community: JSONDict = {
        "platform": "discord",
        "id": str(community_id),
        "name": str(community_name),
        "members": members,
        "channels": channels,
    }

    metadata: JSONDict = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "channel_id": channel_id,
            "channel_name": str(channel_name) if channel_name is not None else None,
            "guild_id": guild_id,
            "guild_name": guild_name,
            "message_count": len(messages),
        },
    }
    if conv_meta:
        metadata["conversations"] = list(conv_meta.values())
    community["metadata"] = metadata

    return community


def _resolve_db_path(db_path: str | None) -> Path:
    from servant.defs import GlobalContext
    from servant.modules import background_indexer

    ctx = GlobalContext()
    if db_path:
        ctx.secrets["indexer_db_path"] = str(Path(db_path).expanduser())
    return background_indexer._db_path(ctx)


def _connect(db_path: Path) -> sqlite3.Connection:
    from servant.modules import background_indexer

    conn = background_indexer._connect(db_path)
    conn.execute("PRAGMA query_only=1;")
    return conn


def _write_json(path: Path, payload: JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export discord indexer data to CODI training JSON.")
    parser.add_argument(
        "--db-path",
        help="Path to servant_index.sqlite3 (defaults to current config).",
    )
    parser.add_argument(
        "--out-dir",
        default="exports/codi",
        help="Directory to write per-channel JSON exports.",
    )
    parser.add_argument(
        "--channel-id",
        action="append",
        dest="channel_ids",
        help="Discord channel id to export (repeatable).",
    )
    parser.add_argument(
        "--guild-id",
        action="append",
        dest="guild_ids",
        help="Discord guild id to export (repeatable).",
    )
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Include channels even if they have no messages.",
    )
    parser.add_argument(
        "--require-conversations",
        action="store_true",
        help="Fail if CODI conversation tables are missing.",
    )

    args = parser.parse_args()

    db_path = _resolve_db_path(args.db_path)
    out_dir = Path(args.out_dir).expanduser().resolve()

    conn = _connect(db_path)
    try:
        channel_ids = _select_channel_ids(
            conn,
            args.channel_ids,
            args.guild_ids,
            include_empty=args.include_empty,
        )
        if not channel_ids:
            print("No channels found to export.")
            return 0

        exported = 0
        for channel_id in channel_ids:
            payload = _export_channel(conn, channel_id, require_conversations=args.require_conversations)
            out_path = out_dir / f"channel_{channel_id}.json"
            _write_json(out_path, payload)
            exported += 1
            print(f"Wrote {out_path}")

        print(f"Exported {exported} channel(s).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
