from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

from servant import permissions
from servant.defs import GlobalContext
from servant.modules import background_indexer

_LOGGER = logging.getLogger(__name__)

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]

DEFAULT_REASONING_EFFORT: ReasoningEffort = "high"
_DEFAULT_DOWNGRADED_REASONING_EFFORT: ReasoningEffort = "low"
_POLICY_ID = 1
_EVENT_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
_REASONING_EFFORTS: dict[str, ReasoningEffort] = {
    "none": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
}


@dataclass(frozen=True)
class ThrottlePolicy:
    enabled: bool
    window_seconds: int
    max_requests: int
    downgraded_reasoning_effort: ReasoningEffort


@dataclass(frozen=True)
class ThrottleDecision:
    reasoning_effort: ReasoningEffort
    throttled: bool
    exempt: bool
    recent_requests: int
    policy: ThrottlePolicy


def _now_ms() -> int:
    return background_indexer._now_ms()


def _normalize_reasoning_effort(
    value: object | None,
    *,
    default: ReasoningEffort = DEFAULT_REASONING_EFFORT,
) -> ReasoningEffort:
    if isinstance(value, str):
        normalized = _REASONING_EFFORTS.get(value.strip().lower())
        if normalized is not None:
            return normalized
    return default


def _load_policy(conn: sqlite3.Connection) -> ThrottlePolicy:
    row = conn.execute(
        """
        SELECT enabled, window_seconds, max_requests, downgraded_reasoning_effort
        FROM llm_throttle_policy
        WHERE policy_id = ?
        """,
        (_POLICY_ID,),
    ).fetchone()
    if row is None:
        return ThrottlePolicy(
            enabled=False,
            window_seconds=0,
            max_requests=0,
            downgraded_reasoning_effort=_DEFAULT_DOWNGRADED_REASONING_EFFORT,
        )

    enabled = bool(row["enabled"])
    window_seconds_raw = row["window_seconds"]
    max_requests_raw = row["max_requests"]
    window_seconds = int(window_seconds_raw) if isinstance(window_seconds_raw, int | float) else 0
    max_requests = int(max_requests_raw) if isinstance(max_requests_raw, int | float) else 0
    downgraded = _normalize_reasoning_effort(
        row["downgraded_reasoning_effort"],
        default=_DEFAULT_DOWNGRADED_REASONING_EFFORT,
    )

    if window_seconds <= 0 or max_requests <= 0:
        enabled = False

    return ThrottlePolicy(
        enabled=enabled,
        window_seconds=max(0, window_seconds),
        max_requests=max(0, max_requests),
        downgraded_reasoning_effort=downgraded,
    )


def _load_exempt_role_ids(conn: sqlite3.Connection, *, guild_id: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT role_id
        FROM llm_high_reasoning_exempt_roles
        WHERE guild_id = ?
        """,
        (guild_id,),
    ).fetchall()
    return {str(row["role_id"]) for row in rows if row["role_id"] is not None}


def _recent_request_count(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    since_ms: int,
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM llm_request_events
        WHERE user_id = ?
          AND created_at >= ?
        """,
        (user_id, since_ms),
    ).fetchone()
    if row is None:
        return 0
    count = row["count"]
    return int(count) if isinstance(count, int | float) else 0


def _prune_old_events(conn: sqlite3.Connection, *, now_ms: int) -> None:
    cutoff = now_ms - _EVENT_RETENTION_MS
    conn.execute("DELETE FROM llm_request_events WHERE created_at < ?", (cutoff,))


def _insert_event(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    guild_id: str | None,
    channel_id: str | None,
    message_id: str | None,
    created_at: int,
    applied_reasoning_effort: ReasoningEffort,
    throttled: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO llm_request_events
            (user_id, guild_id, channel_id, message_id, created_at, applied_reasoning_effort, throttled)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            guild_id,
            channel_id,
            message_id,
            created_at,
            applied_reasoning_effort,
            int(throttled),
        ),
    )


async def resolve_reasoning_effort_for_message(
    ctx: GlobalContext,
    discord_message: object,
) -> ThrottleDecision:
    author = getattr(discord_message, "author", None)
    author_id = getattr(author, "id", None)
    if author_id is None:
        raise ValueError("discord_message.author.id is required for throttling.")

    message_id_value = getattr(discord_message, "id", None)
    if message_id_value is None:
        raise ValueError("discord_message.id is required for throttling.")

    channel = getattr(discord_message, "channel", None)
    channel_id_value = getattr(channel, "id", None)
    channel_id = str(channel_id_value) if channel_id_value is not None else None
    guild = getattr(discord_message, "guild", None)
    guild_id = str(guild.id) if guild is not None else None
    message_id = str(message_id_value)
    user_id = str(author_id)
    author_permissions = permissions.member_permissions(author)
    author_role_ids = permissions.member_role_ids(author)

    def _logic(conn: sqlite3.Connection) -> ThrottleDecision:
        conn.execute("BEGIN IMMEDIATE")
        now_ms = _now_ms()
        _prune_old_events(conn, now_ms=now_ms)

        policy = _load_policy(conn)
        exempt = permissions.is_global_admin(ctx, user_id) or permissions.has_admin_permissions(author_permissions)
        if not exempt and guild_id is not None and author_role_ids:
            exempt_role_ids = _load_exempt_role_ids(conn, guild_id=guild_id)
            exempt = bool(author_role_ids & exempt_role_ids)

        recent_requests = 0
        reasoning_effort = DEFAULT_REASONING_EFFORT
        throttled = False

        if policy.enabled and not exempt:
            since_ms = now_ms - (policy.window_seconds * 1000)
            recent_requests = _recent_request_count(conn, user_id=user_id, since_ms=since_ms)
            if recent_requests >= policy.max_requests:
                reasoning_effort = policy.downgraded_reasoning_effort
                throttled = True

        _insert_event(
            conn,
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            created_at=now_ms,
            applied_reasoning_effort=reasoning_effort,
            throttled=throttled,
        )

        return ThrottleDecision(
            reasoning_effort=reasoning_effort,
            throttled=throttled,
            exempt=exempt,
            recent_requests=recent_requests,
            policy=policy,
        )

    decision = await background_indexer._with_db(ctx, _logic)
    if decision.throttled:
        _LOGGER.info(
            "Applied downgraded reasoning effort for user %s: recent_requests=%s window_seconds=%s max_requests=%s effort=%s",
            user_id,
            decision.recent_requests,
            decision.policy.window_seconds,
            decision.policy.max_requests,
            decision.reasoning_effort,
        )
    return decision
