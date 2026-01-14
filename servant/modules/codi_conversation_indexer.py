from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import importlib
import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, TypedDict
from uuid import uuid4

from servant.defs import GlobalContext, RoutineTask, ToolDef
from typed_json import (
    JSON,
    JSONDict,
    coerce_float,
    coerce_int,
    coerce_optional_str,
    coerce_optional_str_list,
)
from servant.modules import background_indexer

from codi import disentangle as codi_disentangle

_LOGGER = logging.getLogger(__name__)

MODULE_PROMPT = (
    "CODI conversation indexer: batch disentangle channel history, persist stable "
    "conversation ids, hash content, and name conversations."
)

DEFAULT_RUN_EVERY_SECONDS = 60 * 60
DEFAULT_CHANNELS_PER_TICK = 1
DEFAULT_MIN_OVERLAP_RATIO = 0.5
DEFAULT_MAX_NAMED_PER_TICK = 5
DEFAULT_MAX_NAME_MESSAGES = 40
DEFAULT_MESSAGE_PREVIEW_CHARS = 200


@dataclass
class ChannelBatch:
    channel_id: str
    guild_id: str | None
    channel_name: str
    guild_name: str | None
    max_created_at: int | None
    message_count: int


@dataclass
class ModuleState:
    db_initialized: bool = False


T = TypeVar("T")


class MessagePayload(TypedDict):
    id: str
    author_id: str
    author_name: str
    content: str
    timestamp: str
    created_at: int | None


class ExistingConversationMeta(TypedDict):
    name: str | None
    name_hash: str | None
    content_hash: str | None


class ConversationMeta(TypedDict):
    content_hash: str
    messages: list[MessagePayload]
    name: str | None
    name_hash: str | None


def _module_state(ctx: GlobalContext) -> ModuleState:
    state = ctx.module_state.get("codi_conversation_indexer")
    if isinstance(state, ModuleState):
        return state
    state = ModuleState()
    ctx.module_state["codi_conversation_indexer"] = state
    return state


def _get_int_config(ctx: GlobalContext, key: str, default: int) -> int:
    raw = ctx.secrets.get(key)
    if raw is None:
        return default
    return coerce_int(raw, default)


def _get_float_config(ctx: GlobalContext, key: str, default: float) -> float:
    raw = ctx.secrets.get(key)
    if raw is None:
        return default
    return coerce_float(raw, default)


def _db_path(ctx: GlobalContext) -> Path:
    return background_indexer._db_path(ctx)


def _connect(dbfile: Path) -> sqlite3.Connection:
    return background_indexer._connect(dbfile)


def _init_codi_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codi_channel_state (
            channel_id TEXT PRIMARY KEY,
            guild_id TEXT,
            last_analyzed_at INTEGER,
            last_message_id TEXT,
            last_message_ts INTEGER,
            last_message_count INTEGER
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codi_conversations (
            conversation_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            guild_id TEXT,
            content_hash TEXT NOT NULL,
            name TEXT,
            name_hash TEXT,
            message_count INTEGER,
            first_message_id TEXT,
            last_message_id TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            named_at INTEGER
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_codi_conversations_channel " "ON codi_conversations(channel_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_codi_conversations_guild " "ON codi_conversations(guild_id);")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codi_conversation_messages (
            conversation_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            created_at INTEGER,
            PRIMARY KEY (conversation_id, message_id),
            UNIQUE (channel_id, message_id),
            FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_codi_conv_messages_channel " "ON codi_conversation_messages(channel_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_codi_conv_messages_message " "ON codi_conversation_messages(message_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_codi_conv_messages_conversation "
        "ON codi_conversation_messages(conversation_id);"
    )
    conn.commit()


async def init_db(ctx: GlobalContext, *, mctx: ModuleState | None = None) -> None:
    state = mctx or _module_state(ctx)
    if state.db_initialized:
        return
    await background_indexer.init_db(ctx)
    dbfile = _db_path(ctx)

    def _run() -> None:
        dbfile.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(dbfile)
        try:
            _init_codi_tables(conn)
        finally:
            conn.close()

    await asyncio.to_thread(_run)
    state.db_initialized = True


async def _with_db(
    ctx: GlobalContext,
    fn: Callable[[sqlite3.Connection], T],
    *,
    mctx: ModuleState | None = None,
) -> T:
    await init_db(ctx, mctx=mctx)
    dbfile = _db_path(ctx)

    def _run() -> T:
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


def _select_channels_to_analyze(conn: sqlite3.Connection, limit: int) -> list[ChannelBatch]:
    rows = conn.execute(
        """
        WITH channel_stats AS (
            SELECT
                channel_id,
                guild_id,
                MAX(created_at) AS max_created_at,
                COUNT(*) AS message_count
            FROM messages
            WHERE content IS NOT NULL
              AND content_available = 1
              AND deleted_at IS NULL
            GROUP BY channel_id
        )
        SELECT
            cs.channel_id,
            cs.guild_id,
            c.name AS channel_name,
            g.name AS guild_name,
            cs.max_created_at,
            cs.message_count,
            s.last_message_ts,
            s.last_message_count
        FROM channel_stats cs
        LEFT JOIN codi_channel_state s ON s.channel_id = cs.channel_id
        LEFT JOIN channels c ON c.channel_id = cs.channel_id
        LEFT JOIN guilds g ON g.guild_id = cs.guild_id
        WHERE s.last_message_ts IS NULL
           OR s.last_message_ts < cs.max_created_at
           OR s.last_message_count IS NULL
           OR s.last_message_count != cs.message_count
        ORDER BY
            s.last_analyzed_at IS NOT NULL,
            s.last_analyzed_at ASC,
            cs.max_created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    batches = []
    for row in rows:
        batches.append(
            ChannelBatch(
                channel_id=row["channel_id"],
                guild_id=row["guild_id"],
                channel_name=row["channel_name"] or row["channel_id"],
                guild_name=row["guild_name"],
                max_created_at=row["max_created_at"],
                message_count=row["message_count"],
            )
        )
    return batches


def _load_channel_messages(
    conn: sqlite3.Connection, channel_id: str
) -> tuple[list[MessagePayload], str | None, str | None, str | None]:
    rows = conn.execute(
        """
        SELECT
            m.message_id,
            m.author_id,
            m.content,
            m.created_at,
            m.guild_id,
            COALESCE(gm.nick, u.global_name, u.name, m.author_id) AS author_name,
            c.name AS channel_name,
            g.name AS guild_name
        FROM messages m
        LEFT JOIN users u ON u.user_id = m.author_id
        LEFT JOIN guild_members gm
          ON gm.user_id = m.author_id AND gm.guild_id = m.guild_id
        LEFT JOIN channels c ON c.channel_id = m.channel_id
        LEFT JOIN guilds g ON g.guild_id = m.guild_id
        WHERE m.channel_id = ?
          AND m.content IS NOT NULL
          AND m.content_available = 1
          AND m.deleted_at IS NULL
        ORDER BY m.created_at ASC, m.message_id ASC
        """,
        (channel_id,),
    ).fetchall()

    messages: list[MessagePayload] = []
    guild_id: str | None = None
    channel_name: str | None = None
    guild_name: str | None = None

    for row in rows:
        if guild_id is None and row["guild_id"] is not None:
            guild_id = str(row["guild_id"])
        if channel_name is None and row["channel_name"] is not None:
            channel_name = str(row["channel_name"])
        if guild_name is None and row["guild_name"] is not None:
            guild_name = str(row["guild_name"])
        messages.append(
            {
                "id": str(row["message_id"]),
                "author_id": str(row["author_id"]),
                "author_name": str(row["author_name"] or row["author_id"]),
                "content": str(row["content"]),
                "timestamp": str(row["created_at"] or 0),
                "created_at": (int(row["created_at"]) if row["created_at"] is not None else None),
            }
        )

    return messages, guild_id, channel_name, guild_name


def _load_existing_conversations(
    conn: sqlite3.Connection, channel_id: str
) -> tuple[dict[str, set[str]], dict[str, ExistingConversationMeta]]:
    rows = conn.execute(
        """
        SELECT conversation_id, message_id
        FROM codi_conversation_messages
        WHERE channel_id = ?
        """,
        (channel_id,),
    ).fetchall()

    conv_to_messages: dict[str, set[str]] = {}
    for row in rows:
        conv_id = row["conversation_id"]
        conv_to_messages.setdefault(conv_id, set()).add(row["message_id"])

    meta_rows = conn.execute(
        """
        SELECT conversation_id, name, name_hash, content_hash
        FROM codi_conversations
        WHERE channel_id = ?
        """,
        (channel_id,),
    ).fetchall()

    metadata: dict[str, ExistingConversationMeta] = {}
    for row in meta_rows:
        metadata[row["conversation_id"]] = {
            "name": str(row["name"]) if row["name"] is not None else None,
            "name_hash": (str(row["name_hash"]) if row["name_hash"] is not None else None),
            "content_hash": (str(row["content_hash"]) if row["content_hash"] is not None else None),
        }

    return conv_to_messages, metadata


def _assign_conversation_ids(
    predicted_groups: dict[str, list[str]],
    existing_groups: dict[str, set[str]],
    min_overlap_ratio: float,
    id_factory: Callable[[], str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    if id_factory is None:
        id_factory = lambda: f"codi-{uuid4().hex}"

    assignments: dict[str, str] = {}
    used_existing: set[str] = set()
    candidates: list[tuple[int, float, str, str]] = []

    for temp_id, message_ids in predicted_groups.items():
        message_set = set(message_ids)
        for conv_id, existing_ids in existing_groups.items():
            if not existing_ids:
                continue
            intersection = len(message_set & existing_ids)
            if intersection == 0:
                continue
            overlap_ratio = intersection / len(existing_ids)
            candidates.append((intersection, overlap_ratio, temp_id, conv_id))

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)

    for intersection, overlap_ratio, temp_id, conv_id in candidates:
        if temp_id in assignments or conv_id in used_existing:
            continue
        if overlap_ratio < min_overlap_ratio:
            continue
        assignments[temp_id] = conv_id
        used_existing.add(conv_id)

    new_ids: list[str] = []
    for temp_id in predicted_groups.keys():
        if temp_id not in assignments:
            new_id = id_factory()
            assignments[temp_id] = new_id
            new_ids.append(new_id)

    return assignments, new_ids


def _hash_conversation(messages: Iterable[MessagePayload]) -> str:
    hasher = hashlib.sha256()
    for msg in messages:
        hasher.update(str(msg["id"]).encode("utf-8"))
        hasher.update(b"\n")
        hasher.update(str(msg.get("author_id") or "").encode("utf-8"))
        hasher.update(b"\n")
        hasher.update(str(msg.get("content") or "").encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _sample_messages_for_naming(
    messages: list[MessagePayload],
    max_messages: int,
) -> list[MessagePayload]:
    if len(messages) <= max_messages:
        return messages
    half = max_messages // 2
    return messages[:half] + messages[-(max_messages - half) :]


def _format_messages_for_prompt(
    messages: list[MessagePayload],
    max_chars: int,
) -> str:
    lines = []
    for msg in messages:
        author = msg.get("author_name") or msg.get("author_id") or "unknown"
        content = msg.get("content") or ""
        content = " ".join(content.split())
        if len(content) > max_chars:
            content = content[: max_chars - 3] + "..."
        lines.append(f"{author}: {content}")
    return "\n".join(lines)


async def _name_conversation(
    ctx: GlobalContext,
    *,
    channel_name: str,
    guild_name: str | None,
    messages: list[MessagePayload],
    max_messages: int,
    max_chars: int,
) -> str | None:
    if ctx.openai_client is None:
        return None

    sample = _sample_messages_for_naming(messages, max_messages)
    prompt = _format_messages_for_prompt(sample, max_chars)
    context_bits = [f"Channel: {channel_name}"]
    if guild_name:
        context_bits.append(f"Guild: {guild_name}")
    context = " | ".join(context_bits)

    system_prompt = (
        "You are naming a chat conversation. "
        "Return a concise, descriptive title (3-8 words). "
        "Avoid usernames. Output plain text only."
    )
    user_prompt = f"{context}\nMessages:\n{prompt}\nTitle:"

    try:
        response = await ctx.openai_client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            reasoning_effort="low",
            max_completion_tokens=32,
        )
    except Exception as exc:
        _LOGGER.warning("Conversation naming failed: %s", exc)
        return None

    title = response.choices[0].message.content if response.choices else None
    if not title:
        return None
    title = " ".join(title.strip().strip('"').split())
    if not title:
        return None
    return title[:120]


async def _analyze_channel(
    ctx: GlobalContext,
    *,
    channel_id: str,
    model_dir: str | None,
    features: list[str] | None,
    min_overlap_ratio: float,
    max_named: int,
    max_name_messages: int,
    max_preview_chars: int,
    force: bool = False,
    mctx: ModuleState | None = None,
) -> JSONDict:
    messages, guild_id, channel_name, guild_name = await _with_db(
        ctx,
        lambda conn: _load_channel_messages(conn, channel_id),
        mctx=mctx,
    )

    if not messages:
        await _with_db(
            ctx,
            lambda conn: _update_channel_state(
                conn,
                channel_id=channel_id,
                guild_id=guild_id,
                last_message_id=None,
                last_message_ts=None,
                last_message_count=0,
            ),
            mctx=mctx,
        )
        return {"channel_id": channel_id, "messages": 0, "conversations": 0}

    model_path = Path(model_dir).expanduser().resolve() if model_dir else Path(codi_disentangle._default_model_dir())
    model_file = model_path / "model.pickle"
    if not model_file.exists():
        raise RuntimeError(f"Missing model.pickle in {model_path}")

    codi_disentangle._ensure_codi_imported()
    resolved_features = codi_disentangle._resolve_features(features)

    message_dicts = [dict(msg) for msg in messages]
    community_obj = codi_disentangle._build_community(
        message_dicts,
        platform="discord",
        community_id=str(guild_id or "community"),
        community_name=str(guild_name or "community"),
        channel_id=str(channel_id),
        channel_name=str(channel_name or channel_id),
    )

    community_module = importlib.import_module("api.model.input.community")
    community_cls = getattr(community_module, "Community")
    community = community_cls().deserialize(community_obj)
    model = codi_disentangle._get_model(model_path)
    result = model.predict(community, features=resolved_features, retrain=False)

    predicted_groups: dict[str, list[str]] = {}
    for channel in result.get("channels", []):
        for message in channel.get("messages", []):
            temp_id = message.get("conversationId")
            msg_id = message.get("id")
            if not temp_id or not msg_id:
                continue
            predicted_groups.setdefault(temp_id, []).append(msg_id)

    existing_groups, existing_meta = await _with_db(
        ctx,
        lambda conn: _load_existing_conversations(conn, channel_id),
        mctx=mctx,
    )

    assignments, _ = _assign_conversation_ids(predicted_groups, existing_groups, min_overlap_ratio)

    message_map: dict[str, MessagePayload] = {msg["id"]: msg for msg in messages}
    conversation_rows: list[JSONDict] = []
    conversation_messages_rows: list[tuple[str, str, str, int | None]] = []
    conversation_metadata: dict[str, ConversationMeta] = {}

    for temp_id, message_ids in predicted_groups.items():
        conv_id = assignments[temp_id]
        convo_messages = [message_map[mid] for mid in message_ids if mid in message_map]
        if not convo_messages:
            continue
        convo_messages.sort(key=lambda m: (m.get("created_at") or 0, m["id"]))
        content_hash = _hash_conversation(convo_messages)
        first_message = convo_messages[0]
        last_message = convo_messages[-1]

        conversation_rows.append(
            {
                "conversation_id": conv_id,
                "channel_id": channel_id,
                "guild_id": guild_id,
                "content_hash": content_hash,
                "message_count": len(convo_messages),
                "first_message_id": first_message["id"],
                "last_message_id": last_message["id"],
                "created_at": first_message.get("created_at"),
                "updated_at": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
            }
        )

        existing = existing_meta.get(conv_id)
        conversation_metadata[conv_id] = {
            "content_hash": content_hash,
            "messages": convo_messages,
            "name": existing["name"] if existing else None,
            "name_hash": existing["name_hash"] if existing else None,
        }

        for msg in convo_messages:
            conversation_messages_rows.append(
                (
                    conv_id,
                    channel_id,
                    msg["id"],
                    msg.get("created_at"),
                )
            )

    await _with_db(
        ctx,
        lambda conn: _persist_conversations(
            conn,
            channel_id=channel_id,
            guild_id=guild_id,
            conversation_rows=conversation_rows,
            conversation_messages_rows=conversation_messages_rows,
            last_message=messages[-1],
            last_message_count=len(messages),
        ),
        mctx=mctx,
    )

    if not force and max_named <= 0:
        return {
            "channel_id": channel_id,
            "messages": len(messages),
            "conversations": len(conversation_rows),
        }

    named = 0
    for conv_id, meta in conversation_metadata.items():
        if named >= max_named:
            break
        if meta.get("content_hash") is None:
            continue
        if meta.get("name") and meta.get("name_hash") == meta.get("content_hash"):
            continue
        title = await _name_conversation(
            ctx,
            channel_name=channel_name or channel_id,
            guild_name=guild_name,
            messages=meta["messages"],
            max_messages=max_name_messages,
            max_chars=max_preview_chars,
        )
        if title is None:
            continue
        if not title:
            continue
        title_str = title

        def _write_name(
            conn: sqlite3.Connection,
            conv_id: str = conv_id,
            title: str = title_str,
            content_hash: str = meta["content_hash"],
        ) -> None:
            _update_conversation_name(conn, conv_id, title, content_hash)

        await _with_db(ctx, _write_name, mctx=mctx)
        named += 1

    return {
        "channel_id": channel_id,
        "messages": len(messages),
        "conversations": len(conversation_rows),
        "named": named,
    }


def _persist_conversations(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    guild_id: str | None,
    conversation_rows: list[JSONDict],
    conversation_messages_rows: list[tuple[str, str, str, int | None]],
    last_message: MessagePayload,
    last_message_count: int,
) -> None:
    conn.execute(
        "DELETE FROM codi_conversation_messages WHERE channel_id = ?",
        (channel_id,),
    )
    if conversation_messages_rows:
        conn.executemany(
            """
            INSERT INTO codi_conversation_messages (
                conversation_id, channel_id, message_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            conversation_messages_rows,
        )

    for convo in conversation_rows:
        conn.execute(
            """
            INSERT INTO codi_conversations (
                conversation_id,
                channel_id,
                guild_id,
                content_hash,
                message_count,
                first_message_id,
                last_message_id,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                guild_id = excluded.guild_id,
                content_hash = excluded.content_hash,
                message_count = excluded.message_count,
                first_message_id = excluded.first_message_id,
                last_message_id = excluded.last_message_id,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                convo["conversation_id"],
                convo["channel_id"],
                convo["guild_id"],
                convo["content_hash"],
                convo["message_count"],
                convo["first_message_id"],
                convo["last_message_id"],
                convo["created_at"],
                convo["updated_at"],
            ),
        )

    _update_channel_state(
        conn,
        channel_id=channel_id,
        guild_id=guild_id,
        last_message_id=last_message["id"],
        last_message_ts=last_message["created_at"],
        last_message_count=last_message_count,
    )


def _update_channel_state(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    guild_id: str | None,
    last_message_id: str | None,
    last_message_ts: int | None,
    last_message_count: int,
) -> None:
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    conn.execute(
        """
        INSERT INTO codi_channel_state (
            channel_id,
            guild_id,
            last_analyzed_at,
            last_message_id,
            last_message_ts,
            last_message_count
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            last_analyzed_at = excluded.last_analyzed_at,
            last_message_id = excluded.last_message_id,
            last_message_ts = excluded.last_message_ts,
            last_message_count = excluded.last_message_count
        """,
        (
            channel_id,
            guild_id,
            now,
            last_message_id,
            last_message_ts,
            last_message_count,
        ),
    )


def _update_conversation_name(
    conn: sqlite3.Connection,
    conversation_id: str,
    name: str,
    content_hash: str,
) -> None:
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    conn.execute(
        """
        UPDATE codi_conversations
        SET name = ?, name_hash = ?, named_at = ?
        WHERE conversation_id = ?
        """,
        (name, content_hash, now, conversation_id),
    )


async def analyze_conversations(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    state = _module_state(ctx)

    channel_id = obj.get("channel_id")
    guild_id = obj.get("guild_id")
    model_dir = coerce_optional_str(obj.get("model_dir")) or coerce_optional_str(ctx.secrets.get("codi_model_dir"))
    features = coerce_optional_str_list(obj.get("features"))

    min_overlap_ratio = _get_float_config(ctx, "codi_min_overlap_ratio", DEFAULT_MIN_OVERLAP_RATIO)
    channels_per_tick = _get_int_config(ctx, "codi_channels_per_tick", DEFAULT_CHANNELS_PER_TICK)
    max_named = _get_int_config(ctx, "codi_max_named_per_tick", DEFAULT_MAX_NAMED_PER_TICK)
    max_name_messages = _get_int_config(ctx, "codi_max_name_messages", DEFAULT_MAX_NAME_MESSAGES)
    max_preview_chars = _get_int_config(ctx, "codi_max_name_preview_chars", DEFAULT_MESSAGE_PREVIEW_CHARS)
    force = bool(obj.get("force", False))

    if channel_id:
        result: JSON = await _analyze_channel(
            ctx,
            channel_id=str(channel_id),
            model_dir=model_dir,
            features=features,
            min_overlap_ratio=min_overlap_ratio,
            max_named=max_named,
            max_name_messages=max_name_messages,
            max_preview_chars=max_preview_chars,
            force=force,
            mctx=state,
        )
        return {"channels": [result]}

    def _pick_channels(conn: sqlite3.Connection) -> list[ChannelBatch]:
        if guild_id:
            rows = conn.execute(
                """
                WITH channel_stats AS (
                    SELECT
                        channel_id,
                        guild_id,
                        MAX(created_at) AS max_created_at,
                        COUNT(*) AS message_count
                    FROM messages
                    WHERE content IS NOT NULL
                      AND content_available = 1
                      AND deleted_at IS NULL
                      AND guild_id = ?
                    GROUP BY channel_id
                )
                SELECT
                    cs.channel_id,
                    cs.guild_id,
                    c.name AS channel_name,
                    g.name AS guild_name,
                    cs.max_created_at,
                    cs.message_count,
                    s.last_message_ts,
                    s.last_message_count
                FROM channel_stats cs
                LEFT JOIN codi_channel_state s ON s.channel_id = cs.channel_id
                LEFT JOIN channels c ON c.channel_id = cs.channel_id
                LEFT JOIN guilds g ON g.guild_id = cs.guild_id
                WHERE s.last_message_ts IS NULL
                   OR s.last_message_ts < cs.max_created_at
                   OR s.last_message_count IS NULL
                   OR s.last_message_count != cs.message_count
                ORDER BY
                    s.last_analyzed_at IS NOT NULL,
                    s.last_analyzed_at ASC,
                    cs.max_created_at DESC
                LIMIT ?
                """,
                (str(guild_id), channels_per_tick),
            ).fetchall()
            return [
                ChannelBatch(
                    channel_id=row["channel_id"],
                    guild_id=row["guild_id"],
                    channel_name=row["channel_name"] or row["channel_id"],
                    guild_name=row["guild_name"],
                    max_created_at=row["max_created_at"],
                    message_count=row["message_count"],
                )
                for row in rows
            ]
        return _select_channels_to_analyze(conn, channels_per_tick)

    batches = await _with_db(ctx, _pick_channels, mctx=state)
    results: list[JSON] = []
    for batch in batches:
        results.append(
            await _analyze_channel(
                ctx,
                channel_id=batch.channel_id,
                model_dir=model_dir,
                features=features,
                min_overlap_ratio=min_overlap_ratio,
                max_named=max_named,
                max_name_messages=max_name_messages,
                max_preview_chars=max_preview_chars,
                force=force,
                mctx=state,
            )
        )
    return {"channels": results}


async def codi_conversation_routine(ctx: GlobalContext, obj: JSON) -> JSONDict:
    channels_per_tick = _get_int_config(ctx, "codi_channels_per_tick", DEFAULT_CHANNELS_PER_TICK)
    if channels_per_tick <= 0:
        return {"ok": True, "channels": 0}
    result = await analyze_conversations(ctx, {})
    channels = result.get("channels")
    count = len(channels) if isinstance(channels, list) else 0
    return {"ok": True, "channels": count}


codi_conversation_task: RoutineTask = RoutineTask(
    name="codi_conversation_indexer",
    description="Disentangle channels with CODI, persist conversation IDs, hash and name them.",
    run_every_seconds=DEFAULT_RUN_EVERY_SECONDS,
    function=codi_conversation_routine,
)


analyze_conversations_tool: ToolDef = ToolDef(
    name="codi_analyze_conversations",
    schema={
        "name": "codi_analyze_conversations",
        "description": "Batch analyze channel history with CODI, persist stable conversation ids, hash and name them.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "guild_id": {"type": "string"},
                "model_dir": {"type": "string"},
                "features": {"type": "array", "items": {"type": "string"}},
                "force": {"type": "boolean"},
            },
            "required": [],
        },
    },
    function=analyze_conversations,
)
