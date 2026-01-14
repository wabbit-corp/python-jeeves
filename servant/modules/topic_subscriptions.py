from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from servant.defs import GlobalContext, ToolDef
from typed_json import JSON, JSONDict, coerce_float, coerce_int, coerce_str

if TYPE_CHECKING:
    import discord
    from sentence_transformers import SentenceTransformer


_LOGGER = logging.getLogger(__name__)

MODULE_PROMPT = (
    "Topic subscriptions module: users can subscribe to topics and get DMs "
    "when new messages in the same server are semantically similar."
)

DEFAULT_DB_FILENAME = "servant_topic_subscriptions.sqlite3"
DEFAULT_SIMILARITY_THRESHOLD = 0.6
DEFAULT_CHANNEL_COOLDOWN_SECONDS = 15 * 60
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"

_MODEL_LOCK = threading.Lock()
_MODEL: SentenceTransformer | None = None
_MODEL_NAME: str | None = None
T = TypeVar("T")


def _db_path(ctx: GlobalContext) -> Path:
    """
    Where to put the sqlite file.
    Override by setting ctx.secrets['topic_subscriptions_db_path'] to an absolute path.
    """
    raw = ctx.secrets.get("topic_subscriptions_db_path")
    if raw:
        return Path(coerce_str(raw, field="topic_subscriptions_db_path", allow_empty=False)).expanduser().resolve()
    return (Path.cwd() / DEFAULT_DB_FILENAME).resolve()


def _connect(dbfile: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS topic_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,              -- channel where subscription was created
            topic TEXT NOT NULL,
            topic_embedding TEXT NOT NULL,         -- JSON list of floats (normalized)
            similarity_threshold REAL NOT NULL DEFAULT 0.6,
            status TEXT NOT NULL DEFAULT 'active', -- active|cancelled
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS topic_notification_cooldowns (
            user_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            last_notified_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, channel_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_topic_subscriptions_guild_active " "ON topic_subscriptions(status, guild_id);"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_subscriptions_user " "ON topic_subscriptions(user_id);")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_topic_notifications_channel " "ON topic_notification_cooldowns(channel_id);"
    )
    conn.commit()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _require_int(obj: JSONDict, key: str) -> int:
    raw = obj.get(key)
    if raw is None:
        raise ValueError(f"{key} is required.")
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer.") from exc
    raise ValueError(f"{key} must be an integer.")


def _get_cooldown_seconds(ctx: GlobalContext) -> int:
    raw_seconds = ctx.secrets.get("topic_subscriptions_cooldown_seconds")
    if raw_seconds is not None:
        return coerce_int(raw_seconds, DEFAULT_CHANNEL_COOLDOWN_SECONDS)
    raw_minutes = ctx.secrets.get("topic_subscriptions_cooldown_minutes")
    if raw_minutes is not None:
        return coerce_int(raw_minutes, DEFAULT_CHANNEL_COOLDOWN_SECONDS // 60) * 60
    return DEFAULT_CHANNEL_COOLDOWN_SECONDS


def _get_model(ctx: GlobalContext) -> SentenceTransformer:
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:  # pragma: no cover - handled at runtime
        raise RuntimeError(
            "sentence_transformers is not installed. " "Install it in the .venv to use topic subscriptions."
        ) from exc
    model_name = str(ctx.secrets.get("topic_subscriptions_model_name") or DEFAULT_MODEL_NAME)
    global _MODEL, _MODEL_NAME
    with _MODEL_LOCK:
        if _MODEL is None or _MODEL_NAME != model_name:
            _LOGGER.info("Loading sentence_transformers model: %s", model_name)
            _MODEL = SentenceTransformer(model_name)
            _MODEL_NAME = model_name
    if _MODEL is None:
        raise RuntimeError("Failed to initialize sentence_transformers model.")
    return _MODEL


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def _embed_text(ctx: GlobalContext, text: str) -> list[float]:
    model = _get_model(ctx)
    with _MODEL_LOCK:
        embedding = model.encode(text, show_progress_bar=False)
    if hasattr(embedding, "tolist"):
        raw = embedding.tolist()
        if not isinstance(raw, list):
            raise ValueError("Embedding must be a list of numbers.")
        vec = [float(v) for v in raw]
    elif isinstance(embedding, Iterable):
        vec = [float(v) for v in embedding]
    else:
        raise ValueError("Embedding must be iterable.")
    return _normalize(vec)


def _serialize_embedding(vec: list[float]) -> str:
    return json.dumps(vec, separators=(",", ":"))


def _deserialize_embedding(raw: object) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        try:
            return [float(v) for v in raw]
        except (TypeError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            try:
                return [float(v) for v in parsed]
            except (TypeError, ValueError):
                return None
    return None


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float | None:
    if len(vec_a) != len(vec_b):
        return None
    return sum(a * b for a, b in zip(vec_a, vec_b, strict=True))


async def _with_db(ctx: GlobalContext, fn: Callable[[sqlite3.Connection], T]) -> T:
    dbfile = _db_path(ctx)

    def _run() -> T:
        dbfile.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(dbfile)
        try:
            _init_db(conn)
            return fn(conn)
        finally:
            conn.close()

    return await asyncio.to_thread(_run)


def _normalize_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _infer_guild_id(ctx: GlobalContext, obj: JSONDict) -> str | None:
    guild_id = _normalize_id(obj.get("guild_id"))
    if guild_id:
        return guild_id

    channel_id = _normalize_id(obj.get("channel_id"))
    if not channel_id:
        return None

    guild_id = await _lookup_guild_id_from_indexer(ctx, channel_id)
    if guild_id:
        return guild_id

    return await _lookup_guild_id_from_discord(ctx, channel_id)


async def _lookup_guild_id_from_indexer(ctx: GlobalContext, channel_id: str) -> str | None:
    try:
        from servant.modules import background_indexer
    except Exception:
        return None

    dbfile = background_indexer._db_path(ctx)
    if not dbfile.exists():
        return None

    def _run() -> str | None:
        conn = sqlite3.connect(str(dbfile))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            row = conn.execute(
                "SELECT guild_id FROM channel_state WHERE channel_id = ? LIMIT 1",
                (channel_id,),
            ).fetchone()
            if row and row["guild_id"]:
                return str(row["guild_id"])
            row = conn.execute(
                "SELECT guild_id FROM channels WHERE channel_id = ? LIMIT 1",
                (channel_id,),
            ).fetchone()
            if row and row["guild_id"]:
                return str(row["guild_id"])
            return None
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        _LOGGER.debug(
            "Failed to infer guild_id from indexer db for channel %s",
            channel_id,
            exc_info=True,
        )
        return None


async def _lookup_guild_id_from_discord(ctx: GlobalContext, channel_id: str) -> str | None:
    client = ctx.discord_client
    if client is None:
        return None

    try:
        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        guild = getattr(channel, "guild", None)
        if guild is None:
            return None
        return str(guild.id)
    except Exception:
        _LOGGER.debug(
            "Failed to resolve guild_id from Discord for channel %s",
            channel_id,
            exc_info=True,
        )
        return None


@dataclass(frozen=True)
class TopicSubscription:
    id: int
    user_id: str
    guild_id: str
    channel_id: str
    topic: str
    similarity_threshold: float
    status: str
    created_at: int
    updated_at: int


def _row_to_subscription(r: sqlite3.Row) -> TopicSubscription:
    return TopicSubscription(
        id=int(r["id"]),
        user_id=str(r["user_id"]),
        guild_id=str(r["guild_id"]),
        channel_id=str(r["channel_id"]),
        topic=str(r["topic"]),
        similarity_threshold=float(r["similarity_threshold"]),
        status=str(r["status"]),
        created_at=int(r["created_at"]),
        updated_at=int(r["updated_at"]),
    )


def _subscription_payload(subscription: TopicSubscription) -> JSONDict:
    return {
        "id": subscription.id,
        "user_id": subscription.user_id,
        "guild_id": subscription.guild_id,
        "channel_id": subscription.channel_id,
        "topic": subscription.topic,
        "similarity_threshold": subscription.similarity_threshold,
        "status": subscription.status,
        "created_at": subscription.created_at,
        "updated_at": subscription.updated_at,
    }


# ----------------------------
# Tool: create/update/cancel/list
# ----------------------------


async def topic_subscription_manage(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    op = obj.get("operation")
    if op not in {"create", "update", "cancel", "list"}:
        raise ValueError("operation must be one of: create, update, cancel, list")

    resolved_guild_id: str | None = None
    if op == "create":
        resolved_guild_id = await _infer_guild_id(ctx, obj)

    def _logic(conn: sqlite3.Connection) -> JSONDict:
        now = _now_ms()

        if op == "create":
            topic = str(obj["topic"]).strip()
            if not topic:
                raise ValueError("topic must be non-empty.")
            user_id = _normalize_id(obj.get("user_id"))
            channel_id = _normalize_id(obj.get("channel_id"))
            guild_id = resolved_guild_id
            if not user_id:
                raise ValueError("user_id is required for create.")
            if not channel_id:
                raise ValueError("channel_id is required for create.")
            if not guild_id:
                raise ValueError("guild_id is required for create (or provide channel_id in a guild).")
            similarity_threshold = coerce_float(obj.get("similarity_threshold"), DEFAULT_SIMILARITY_THRESHOLD)

            topic_embedding = _serialize_embedding(_embed_text(ctx, topic))

            cur = conn.execute(
                """
                INSERT INTO topic_subscriptions
                    (user_id, guild_id, channel_id, topic, topic_embedding,
                     similarity_threshold, status, created_at, updated_at)
                VALUES
                    (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    user_id,
                    guild_id,
                    channel_id,
                    topic,
                    topic_embedding,
                    similarity_threshold,
                    now,
                    now,
                ),
            )
            conn.commit()
            lastrowid = cur.lastrowid
            if lastrowid is None:
                raise RuntimeError("Failed to create subscription.")
            sid = int(lastrowid)
            row = conn.execute(
                "SELECT * FROM topic_subscriptions WHERE id = ?",
                (sid,),
            ).fetchone()
            return {
                "ok": True,
                "subscription": _subscription_payload(_row_to_subscription(row)),
            }

        if op == "update":
            sid = _require_int(obj, "subscription_id")
            sets: list[str] = []
            params: list[object] = []

            if "topic" in obj and obj["topic"] is not None:
                topic = str(obj["topic"]).strip()
                if not topic:
                    raise ValueError("topic must be non-empty.")
                sets.append("topic = ?")
                params.append(topic)
                sets.append("topic_embedding = ?")
                params.append(_serialize_embedding(_embed_text(ctx, topic)))

            if "similarity_threshold" in obj and obj["similarity_threshold"] is not None:
                sets.append("similarity_threshold = ?")
                params.append(coerce_float(obj.get("similarity_threshold"), DEFAULT_SIMILARITY_THRESHOLD))

            if "status" in obj and obj["status"] is not None:
                sets.append("status = ?")
                params.append(str(obj["status"]))

            if not sets:
                raise ValueError("No updatable fields provided.")

            sets.append("updated_at = ?")
            params.append(now)
            params.append(sid)

            conn.execute(
                f"UPDATE topic_subscriptions SET {', '.join(sets)} WHERE id = ?",
                tuple(params),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM topic_subscriptions WHERE id = ?",
                (sid,),
            ).fetchone()
            if not row:
                raise ValueError(f"Subscription id={sid} not found.")
            return {
                "ok": True,
                "subscription": _subscription_payload(_row_to_subscription(row)),
            }

        if op == "cancel":
            sid = _require_int(obj, "subscription_id")
            cur = conn.execute(
                """
                UPDATE topic_subscriptions
                SET status = 'cancelled', updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, sid),
            )
            conn.commit()
            if cur.rowcount == 0:
                row = conn.execute(
                    "SELECT * FROM topic_subscriptions WHERE id = ?",
                    (sid,),
                ).fetchone()
                if not row:
                    raise ValueError(f"Subscription id={sid} not found.")
                return {
                    "ok": True,
                    "subscription": _subscription_payload(_row_to_subscription(row)),
                }
            row = conn.execute(
                "SELECT * FROM topic_subscriptions WHERE id = ?",
                (sid,),
            ).fetchone()
            return {
                "ok": True,
                "subscription": _subscription_payload(_row_to_subscription(row)),
            }

        # op == "list"
        where = []
        params = []

        if "user_id" in obj and obj["user_id"] is not None:
            where.append("user_id = ?")
            params.append(str(obj["user_id"]))
        if "guild_id" in obj and obj["guild_id"] is not None:
            where.append("guild_id = ?")
            params.append(str(obj["guild_id"]))
        if "status" in obj and obj["status"] is not None:
            where.append("status = ?")
            params.append(str(obj["status"]))

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT * FROM topic_subscriptions {clause} ORDER BY id DESC",
            tuple(params),
        ).fetchall()
        return {
            "ok": True,
            "subscriptions": [_subscription_payload(_row_to_subscription(r)) for r in rows],
        }

    return await _with_db(ctx, _logic)


topic_subscription_manage_tool: ToolDef = ToolDef(
    name="topic_subscription_manage",
    function=lambda ctx, obj: topic_subscription_manage(ctx, obj),
    schema={
        "name": "topic_subscription_manage",
        "description": (
            "Create/update/cancel/list a topic subscription for a user. "
            "Subscriptions match new messages in the same server using cosine similarity."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "update", "cancel", "list"],
                    "description": "What to do.",
                },
                "subscription_id": {
                    "type": "integer",
                    "description": "Required for update/cancel.",
                },
                "topic": {"type": "string"},
                "user_id": {"type": "string", "description": "Discord user id."},
                "guild_id": {
                    "type": "string",
                    "description": "Discord guild id (optional if channel_id is provided).",
                },
                "channel_id": {
                    "type": "string",
                    "description": (
                        "Channel id where subscription was created " "(required for create if guild_id omitted)."
                    ),
                },
                "similarity_threshold": {
                    "type": "number",
                    "description": "Defaults to 0.6.",
                },
                "status": {"type": "string", "enum": ["active", "cancelled"]},
            },
            "required": ["operation"],
        },
    },
)


# ----------------------------
# Message handling
# ----------------------------


def _match_subscriptions(
    ctx: GlobalContext,
    *,
    guild_id: str,
    channel_id: str,
    author_id: str,
    content: str,
    now_ms: int,
) -> list[JSONDict]:
    dbfile = _db_path(ctx)
    dbfile.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(dbfile)
    try:
        _init_db(conn)
        rows = conn.execute(
            """
            SELECT user_id, topic, topic_embedding, similarity_threshold
            FROM topic_subscriptions
            WHERE status = 'active' AND guild_id = ?
            ORDER BY id ASC
            """,
            (guild_id,),
        ).fetchall()
        if not rows:
            return []

        cooldown_seconds = _get_cooldown_seconds(ctx)
        cooldown_rows = conn.execute(
            """
            SELECT user_id, last_notified_at
            FROM topic_notification_cooldowns
            WHERE channel_id = ?
            """,
            (channel_id,),
        ).fetchall()
        cooldown_by_user = {str(r["user_id"]): int(r["last_notified_at"]) for r in cooldown_rows}

        candidates = []
        for r in rows:
            user_id = str(r["user_id"])
            if user_id == author_id:
                continue
            last_notified_at = cooldown_by_user.get(user_id)
            if last_notified_at is not None:
                elapsed = (now_ms - last_notified_at) / 1000.0
                if elapsed < cooldown_seconds:
                    continue
            candidates.append(r)

        if not candidates:
            return []

        message_embedding = _embed_text(ctx, content)

        matches_by_user: dict[str, list[JSONDict]] = {}
        for r in candidates:
            topic_embedding = _deserialize_embedding(r["topic_embedding"])
            if not topic_embedding:
                continue
            similarity = _cosine_similarity(topic_embedding, message_embedding)
            if similarity is None:
                continue
            threshold = coerce_float(r["similarity_threshold"], DEFAULT_SIMILARITY_THRESHOLD)
            if similarity >= threshold:
                user_id = str(r["user_id"])
                matches_by_user.setdefault(user_id, []).append(
                    {"topic": str(r["topic"]), "similarity": float(similarity)}
                )

        notifications: list[JSONDict] = []
        for user_id, topics in matches_by_user.items():
            topics.sort(
                key=lambda t: coerce_float(t.get("similarity"), 0.0),
                reverse=True,
            )
            topics_json: list[JSON] = [topic for topic in topics]
            notifications.append({"user_id": user_id, "topics": topics_json})
        return notifications
    finally:
        conn.close()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


async def _record_cooldowns(ctx: GlobalContext, *, channel_id: str, user_ids: list[str], now_ms: int) -> None:
    if not user_ids:
        return

    def _logic(conn: sqlite3.Connection) -> None:
        rows = [(uid, channel_id, now_ms) for uid in user_ids]
        conn.executemany(
            """
            INSERT INTO topic_notification_cooldowns (user_id, channel_id, last_notified_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, channel_id) DO UPDATE SET
                last_notified_at = excluded.last_notified_at
            """,
            rows,
        )
        conn.commit()

    await _with_db(ctx, _logic)


async def _send_dm(ctx: GlobalContext, user_id: str, content: str) -> None:
    client = ctx.discord_client
    if client is None:
        raise RuntimeError("Discord client not initialized yet.")

    async def _send() -> None:
        user = await client.fetch_user(int(user_id))
        await user.send(content)

    loop = ctx.discord_loop
    if loop is None:
        raise RuntimeError("ctx.discord_loop not set yet (client not initialized).")

    try:
        running = asyncio.get_running_loop()
        if running is loop:
            await _send()
            return
    except RuntimeError:
        running = None

    fut = asyncio.run_coroutine_threadsafe(_send(), loop)
    if running is not None:
        await asyncio.wrap_future(fut)
    else:
        fut.result()


async def topic_subscriptions_handle_message(ctx: GlobalContext, discord_message: discord.Message) -> JSONDict:
    guild = getattr(discord_message, "guild", None)
    if guild is None:
        return {"ok": True, "notified": 0}

    content = (getattr(discord_message, "content", "") or "").strip()
    if not content:
        return {"ok": True, "notified": 0}

    guild_id = str(guild.id)
    channel_id = str(discord_message.channel.id)
    author_id = str(discord_message.author.id)
    now_ms = _now_ms()

    try:
        notifications = await asyncio.to_thread(
            _match_subscriptions,
            ctx,
            guild_id=guild_id,
            channel_id=channel_id,
            author_id=author_id,
            content=content,
            now_ms=now_ms,
        )
    except Exception:
        _LOGGER.error(
            "Failed to evaluate topic subscriptions for message %s",
            getattr(discord_message, "id", "unknown"),
            exc_info=True,
        )
        return {"ok": False, "notified": 0}

    if not notifications:
        return {"ok": True, "notified": 0}

    guild_name = str(guild.name)
    author_name = str(
        getattr(discord_message.author, "display_name", None) or getattr(discord_message.author, "name", "unknown")
    )
    jump_url = getattr(discord_message, "jump_url", None)
    snippet = _truncate(content.replace("\n", " "), 500)

    notified_users: list[str] = []
    for note in notifications:
        user_id = str(note.get("user_id"))
        topics = note.get("topics")
        if not isinstance(topics, list):
            topics = []

        lines = []
        lines.append(f"Topic match in {guild_name} <#{channel_id}>")
        lines.append(f"Author: {author_name}")
        if topics:
            lines.append("Matched topics:")
            for topic in topics[:5]:
                if not isinstance(topic, dict):
                    continue
                topic_name = str(topic.get("topic", ""))
                similarity = coerce_float(topic.get("similarity"), 0.0)
                lines.append(f"- {topic_name} (score {similarity:.2f})")
        lines.append("")
        lines.append(snippet or "(no message content)")
        if jump_url:
            lines.append(jump_url)
        content_out = "\n".join(lines).strip()

        try:
            await _send_dm(ctx, user_id, content_out)
        except Exception:
            _LOGGER.error(
                "Failed to DM user %s about topic match in channel %s",
                user_id,
                channel_id,
                exc_info=True,
            )
            continue
        notified_users.append(user_id)

    if notified_users:
        await _record_cooldowns(ctx, channel_id=channel_id, user_ids=notified_users, now_ms=now_ms)

    return {"ok": True, "notified": len(notified_users)}
