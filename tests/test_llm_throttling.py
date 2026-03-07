from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import discord
import pytest

from servant import llm_throttling
from servant.defs import GlobalContext
from servant.modules import background_indexer


class _FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class _FakeAuthor:
    def __init__(
        self,
        *,
        user_id: int,
        permissions: discord.Permissions | None = None,
        role_ids: list[int] | None = None,
    ) -> None:
        self.id = user_id
        self.guild_permissions = permissions or discord.Permissions.none()
        self.roles = [_FakeRole(role_id) for role_id in (role_ids or [])]


class _FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id


class _FakeChannel:
    def __init__(self, *, channel_id: int, guild: _FakeGuild | None) -> None:
        self.id = channel_id
        self.guild = guild


class _FakeMessage:
    def __init__(
        self,
        *,
        message_id: int,
        user_id: int,
        channel_id: int,
        guild_id: int | None,
        permissions: discord.Permissions | None = None,
        role_ids: list[int] | None = None,
    ) -> None:
        guild = _FakeGuild(guild_id) if guild_id is not None else None
        self.id = message_id
        self.author = _FakeAuthor(user_id=user_id, permissions=permissions, role_ids=role_ids)
        self.channel = _FakeChannel(channel_id=channel_id, guild=guild)
        self.guild = guild


def _seed_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        background_indexer._init_db(conn)
    finally:
        conn.close()


def _configure_policy(
    db_path: Path,
    *,
    enabled: bool,
    window_seconds: int,
    max_requests: int,
    downgraded_reasoning_effort: str,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            UPDATE llm_throttle_policy
            SET enabled = ?,
                window_seconds = ?,
                max_requests = ?,
                downgraded_reasoning_effort = ?,
                updated_at = 1
            WHERE policy_id = 1
            """,
            (
                int(enabled),
                window_seconds,
                max_requests,
                downgraded_reasoning_effort,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_exempt_role(db_path: Path, *, guild_id: str, role_id: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO llm_high_reasoning_exempt_roles (guild_id, role_id, created_at)
            VALUES (?, ?, ?)
            """,
            (guild_id, role_id, 1),
        )
        conn.commit()
    finally:
        conn.close()


def _event_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT user_id, message_id, applied_reasoning_effort, throttled
            FROM llm_request_events
            ORDER BY id
            """
        ).fetchall()
        return list(rows)
    finally:
        conn.close()


def test_default_policy_seed_is_documented_shape(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite3"
    _seed_db(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT enabled, window_seconds, max_requests, downgraded_reasoning_effort
            FROM llm_throttle_policy
            WHERE policy_id = 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["enabled"] == 0
    assert row["window_seconds"] == 3600
    assert row["max_requests"] == 5
    assert row["downgraded_reasoning_effort"] == "low"


def test_init_db_migrates_legacy_default_policy_row(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            CREATE TABLE llm_throttle_policy (
                policy_id INTEGER PRIMARY KEY CHECK (policy_id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                window_seconds INTEGER NOT NULL DEFAULT 300,
                max_requests INTEGER NOT NULL DEFAULT 6,
                downgraded_reasoning_effort TEXT NOT NULL DEFAULT 'low',
                updated_at INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            INSERT INTO llm_throttle_policy
                (policy_id, enabled, window_seconds, max_requests, downgraded_reasoning_effort, updated_at)
            VALUES
                (1, 0, 300, 6, 'low', 0)
            """
        )

        background_indexer._init_db(conn)

        row = conn.execute(
            """
            SELECT enabled, window_seconds, max_requests, downgraded_reasoning_effort, updated_at
            FROM llm_throttle_policy
            WHERE policy_id = 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["enabled"] == 0
    assert row["window_seconds"] == 3600
    assert row["max_requests"] == 5
    assert row["downgraded_reasoning_effort"] == "low"
    assert row["updated_at"] == 0


def test_resolve_reasoning_effort_downgrades_after_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "index.sqlite3"
    _seed_db(db_path)
    _configure_policy(
        db_path,
        enabled=True,
        window_seconds=60,
        max_requests=2,
        downgraded_reasoning_effort="low",
    )
    ctx = GlobalContext(config={"indexer_db_path": str(db_path)})

    now = {"value": 100_000}
    monkeypatch.setattr(llm_throttling, "_now_ms", lambda: now["value"])

    first = asyncio.run(
        llm_throttling.resolve_reasoning_effort_for_message(
            ctx,
            _FakeMessage(message_id=1, user_id=100, channel_id=10, guild_id=20),
        )
    )
    assert first.reasoning_effort == "high"
    assert first.throttled is False
    assert first.exempt is False

    now["value"] += 1_000
    second = asyncio.run(
        llm_throttling.resolve_reasoning_effort_for_message(
            ctx,
            _FakeMessage(message_id=2, user_id=100, channel_id=10, guild_id=20),
        )
    )
    assert second.reasoning_effort == "high"
    assert second.throttled is False

    now["value"] += 1_000
    third = asyncio.run(
        llm_throttling.resolve_reasoning_effort_for_message(
            ctx,
            _FakeMessage(message_id=3, user_id=100, channel_id=10, guild_id=20),
        )
    )
    assert third.reasoning_effort == "low"
    assert third.throttled is True
    assert third.recent_requests == 2

    rows = _event_rows(db_path)
    assert [row["applied_reasoning_effort"] for row in rows] == ["high", "high", "low"]
    assert [row["throttled"] for row in rows] == [0, 0, 1]


def test_resolve_reasoning_effort_exempts_guild_administrators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "index.sqlite3"
    _seed_db(db_path)
    _configure_policy(
        db_path,
        enabled=True,
        window_seconds=60,
        max_requests=1,
        downgraded_reasoning_effort="minimal",
    )
    ctx = GlobalContext(config={"indexer_db_path": str(db_path)})

    now = {"value": 200_000}
    monkeypatch.setattr(llm_throttling, "_now_ms", lambda: now["value"])

    admin_perms = discord.Permissions(administrator=True)
    first = asyncio.run(
        llm_throttling.resolve_reasoning_effort_for_message(
            ctx,
            _FakeMessage(
                message_id=10,
                user_id=200,
                channel_id=10,
                guild_id=20,
                permissions=admin_perms,
            ),
        )
    )
    assert first.reasoning_effort == "high"
    assert first.exempt is True
    assert first.throttled is False

    now["value"] += 1_000
    second = asyncio.run(
        llm_throttling.resolve_reasoning_effort_for_message(
            ctx,
            _FakeMessage(
                message_id=11,
                user_id=200,
                channel_id=10,
                guild_id=20,
                permissions=admin_perms,
            ),
        )
    )
    assert second.reasoning_effort == "high"
    assert second.exempt is True
    assert second.throttled is False


def test_resolve_reasoning_effort_exempts_configured_roles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "index.sqlite3"
    _seed_db(db_path)
    _configure_policy(
        db_path,
        enabled=True,
        window_seconds=60,
        max_requests=1,
        downgraded_reasoning_effort="minimal",
    )
    _insert_exempt_role(db_path, guild_id="20", role_id="55")
    ctx = GlobalContext(config={"indexer_db_path": str(db_path)})

    now = {"value": 300_000}
    monkeypatch.setattr(llm_throttling, "_now_ms", lambda: now["value"])

    first = asyncio.run(
        llm_throttling.resolve_reasoning_effort_for_message(
            ctx,
            _FakeMessage(
                message_id=20,
                user_id=300,
                channel_id=10,
                guild_id=20,
                role_ids=[55],
            ),
        )
    )
    assert first.reasoning_effort == "high"
    assert first.exempt is True

    now["value"] += 1_000
    second = asyncio.run(
        llm_throttling.resolve_reasoning_effort_for_message(
            ctx,
            _FakeMessage(
                message_id=21,
                user_id=300,
                channel_id=10,
                guild_id=20,
                role_ids=[55],
            ),
        )
    )
    assert second.reasoning_effort == "high"
    assert second.exempt is True
    assert second.throttled is False
