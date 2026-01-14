#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from servant.defs import GlobalContext
    from typed_json import JSON, JSONDict

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_yaml_config(path: Path) -> dict[str, JSON]:
    if not path.exists():
        return {}
    try:
        import yaml
    except Exception:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return loaded if isinstance(loaded, dict) else {}


def _get_value(d: dict[str, JSON], key: str, default: JSON = "") -> JSON:
    parts = key.split(".")
    current: JSON = d
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def _load_secrets(ctx: GlobalContext, config: dict[str, JSON]) -> None:
    from servant.defs import ALL_SECRETS, SECRET_OPENAI_KEY

    for secret in ALL_SECRETS:
        value = _get_value(config, secret, None)
        if value is not None:
            ctx.secrets[secret] = value

    if SECRET_OPENAI_KEY not in ctx.secrets:
        env_key = os.environ.get("OPENAI_API_KEY")
        if env_key:
            ctx.secrets[SECRET_OPENAI_KEY] = env_key


def _parse_time(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    try:
        from dateutil.parser import parse

        dt = parse(text)
    except Exception:
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Invalid time value: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def _truncate(text: str, max_len: int) -> str:
    if max_len <= 0:
        return text
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _query_output(
    ctx: GlobalContext,
    *,
    channel_id: str,
    start_ms: int | None,
    end_ms: int | None,
    include_messages: bool,
    max_messages_per_conversation: int,
    max_message_length: int,
) -> JSONDict:
    from servant.modules import background_indexer

    dbfile = background_indexer._db_path(ctx)
    conn = background_indexer._connect(dbfile)
    try:
        channel_row = conn.execute(
            "SELECT channel_id, name, guild_id FROM channels WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()

        guild_name = None
        if channel_row and channel_row["guild_id"]:
            guild_row = conn.execute(
                "SELECT guild_id, name FROM guilds WHERE guild_id = ?",
                (channel_row["guild_id"],),
            ).fetchone()
            if guild_row:
                guild_name = guild_row["name"]

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

        range_counts: dict[str, int] = {}
        range_messages: dict[str, list[JSONDict]] = {}

        if include_messages or start_ms is not None or end_ms is not None:
            conditions = [
                "cm.channel_id = ?",
                "m.deleted_at IS NULL",
                "m.content_available = 1",
                "m.content IS NOT NULL",
            ]
            params: list[int | str] = [channel_id]
            if start_ms is not None:
                conditions.append("m.created_at >= ?")
                params.append(start_ms)
            if end_ms is not None:
                conditions.append("m.created_at <= ?")
                params.append(end_ms)

            where_clause = " AND ".join(conditions)
            query = f"""
                SELECT
                    cm.conversation_id,
                    m.message_id,
                    m.author_id,
                    m.content,
                    m.created_at,
                    COALESCE(gm.nick, u.global_name, u.name, m.author_id) AS author_name
                FROM codi_conversation_messages cm
                JOIN messages m ON m.message_id = cm.message_id
                LEFT JOIN users u ON u.user_id = m.author_id
                LEFT JOIN guild_members gm
                  ON gm.user_id = m.author_id AND gm.guild_id = m.guild_id
                WHERE {where_clause}
                ORDER BY m.created_at ASC, m.message_id ASC
            """
            rows = conn.execute(query, params).fetchall()
            for row in rows:
                conv_id = row["conversation_id"]
                range_counts[conv_id] = range_counts.get(conv_id, 0) + 1
                if include_messages:
                    if (
                        max_messages_per_conversation > 0
                        and len(range_messages.get(conv_id, [])) >= max_messages_per_conversation
                    ):
                        continue
                    range_messages.setdefault(conv_id, []).append(
                        {
                            "message_id": row["message_id"],
                            "author_id": row["author_id"],
                            "author_name": row["author_name"],
                            "created_at": row["created_at"],
                            "content": _truncate(row["content"] or "", max_message_length),
                        }
                    )

        conversations: list[JSON] = []
        for row in conv_rows:
            conv_id = row["conversation_id"]
            if start_ms is not None or end_ms is not None:
                if range_counts.get(conv_id, 0) == 0:
                    continue
            convo = {
                "conversation_id": conv_id,
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
            if start_ms is not None or end_ms is not None:
                convo["messages_in_range"] = range_counts.get(conv_id, 0)
            if include_messages:
                convo["messages"] = range_messages.get(conv_id, [])
            conversations.append(convo)

        return {
            "channel_id": channel_id,
            "channel_name": channel_row["name"] if channel_row else None,
            "guild_id": channel_row["guild_id"] if channel_row else None,
            "guild_name": guild_name,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "conversations": conversations,
        }
    finally:
        conn.close()


async def _run(args: argparse.Namespace) -> int:
    from servant.defs import SECRET_OPENAI_KEY, GlobalContext
    from servant.modules import codi_conversation_indexer
    from typed_json import coerce_str

    ctx = GlobalContext()

    config = _load_yaml_config(Path(".private.yml"))
    _load_secrets(ctx, config)

    if args.db_path:
        ctx.secrets["indexer_db_path"] = str(Path(args.db_path).expanduser())
    if args.model_dir:
        ctx.secrets["codi_model_dir"] = str(Path(args.model_dir).expanduser())
    if args.max_named is not None:
        ctx.secrets["codi_max_named_per_tick"] = int(args.max_named)
    if args.max_name_messages is not None:
        ctx.secrets["codi_max_name_messages"] = int(args.max_name_messages)
    if args.max_name_preview_chars is not None:
        ctx.secrets["codi_max_name_preview_chars"] = int(args.max_name_preview_chars)

    if SECRET_OPENAI_KEY in ctx.secrets:
        try:
            import openai

            api_key = coerce_str(
                ctx.secrets.get(SECRET_OPENAI_KEY),
                field="openai.key",
                allow_empty=False,
            )
            ctx.openai_client = openai.AsyncOpenAI(api_key=api_key)
        except Exception as exc:
            print(f"Warning: failed to init OpenAI client: {exc}")
    else:
        print("Warning: no OpenAI key found; naming will be skipped.")

    start_ms = _parse_time(args.start)
    end_ms = _parse_time(args.end)

    features = args.features if args.features else None

    await codi_conversation_indexer.analyze_conversations(
        ctx,
        {
            "channel_id": args.channel_id,
            "features": features,
            "force": bool(args.force),
        },
    )

    output = _query_output(
        ctx,
        channel_id=args.channel_id,
        start_ms=start_ms,
        end_ms=end_ms,
        include_messages=bool(args.include_messages),
        max_messages_per_conversation=int(args.max_messages_per_conversation),
        max_message_length=int(args.max_message_length),
    )

    out_text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        out_path = Path(args.output).expanduser()
        out_path.write_text(out_text, encoding="utf-8")
    else:
        print(out_text)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run CODI on a single channel and output conversation results.")
    parser.add_argument("--channel-id", required=True, help="Discord channel id.")
    parser.add_argument(
        "--db-path",
        help="Path to servant_index.sqlite3 (defaults to current config).",
    )
    parser.add_argument(
        "--model-dir",
        help="Directory containing model.pickle (overrides codi_model_dir).",
    )
    parser.add_argument(
        "--features",
        nargs="*",
        help="Feature groups to use: chat, discourse, content, or all.",
    )
    parser.add_argument("--start", help="Start time (epoch ms or ISO string).")
    parser.add_argument("--end", help="End time (epoch ms or ISO string).")
    parser.add_argument("--output", help="Write JSON output to this path instead of stdout.")
    parser.add_argument(
        "--include-messages",
        action="store_true",
        help="Include message previews in output.",
    )
    parser.add_argument(
        "--max-messages-per-conversation",
        type=int,
        default=200,
        help="Limit included messages per conversation (0 = no limit).",
    )
    parser.add_argument(
        "--max-message-length",
        type=int,
        default=200,
        help="Max characters per message preview (0 = no limit).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force naming attempts even if no naming budget remains.",
    )
    parser.add_argument(
        "--max-named",
        type=int,
        help="Override codi_max_named_per_tick for this run.",
    )
    parser.add_argument(
        "--max-name-messages",
        type=int,
        help="Override codi_max_name_messages for this run.",
    )
    parser.add_argument(
        "--max-name-preview-chars",
        type=int,
        help="Override codi_max_name_preview_chars for this run.",
    )

    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
