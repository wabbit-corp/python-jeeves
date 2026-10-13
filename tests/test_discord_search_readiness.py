# SPDX-License-Identifier: LicenseRef-Wabbit-Public-Test-License-1.1
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from servant.defs import GlobalContext, RequestContext
from servant.modules import background_indexer


def _client() -> MagicMock:
    client = MagicMock(spec=discord.Client)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.me = SimpleNamespace(id=123)
    guild.fetch_member.return_value = SimpleNamespace(id=42)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 10
    channel.guild = guild
    channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
    client.get_channel.return_value = channel
    client.fetch_channel.return_value = channel
    client.http = SimpleNamespace(request=AsyncMock())
    return client


def _ctx(tmp_path: Path, client: discord.Client) -> GlobalContext:
    ctx = GlobalContext(config={"indexer_db_path": str(tmp_path / "index.sqlite3")}, discord_client=client)
    ctx.channel_controls.loaded = True
    conn = background_indexer._connect(background_indexer._db_path(ctx))
    try:
        background_indexer._init_db(conn)
        conn.execute("INSERT INTO channel_state (channel_id, guild_id, search_done) VALUES ('10', '1', 0)")
        conn.commit()
    finally:
        conn.close()
    return ctx.with_request(RequestContext(user_id="42", channel_id="10", guild_id="1", is_dm=False))


def test_search_retries_preparing_index_then_accepts_ready_result(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    client.http.request.side_effect = [
        {"code": 110000, "retry_after": 2, "documents_indexed": 0},
        {"messages": [], "total_results": 0},
    ]
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    result = asyncio.run(
        background_indexer._search_guild_messages(
            client,
            guild_id="1",
            channel_id="10",
            query=None,
            author_id=None,
            min_id=None,
            max_id=None,
            offset=0,
            oldest_first=False,
            include_nsfw=None,
        )
    )
    assert result == {"messages": [], "total_results": 0}
    sleep.assert_awaited_once_with(2.0)
    assert client.http.request.await_count == 2


@pytest.mark.parametrize("retry_after", [2, 120])
def test_background_search_preserves_pending_state_and_retry_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_after: int
) -> None:
    client = _client()
    client.http.request.return_value = {"code": 110000, "retry_after": retry_after}
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    ctx = _ctx(tmp_path, client)
    result = asyncio.run(background_indexer._index_next_channel_messages_search(ctx, client))
    assert result["pending"] is True
    conn = background_indexer._connect(background_indexer._db_path(ctx))
    try:
        state = conn.execute("SELECT * FROM channel_state WHERE channel_id = '10'").fetchone()
        assert state["search_done"] == 0
        assert state["search_forbidden_count"] == 0
        assert state["search_retry_after"] >= state["search_last_indexed_at"] + retry_after * 1000
        assert background_indexer._pick_channel_state_search(conn, state["search_retry_after"] - 1) is None
        assert background_indexer._pick_channel_state_search(conn, state["search_retry_after"]) is not None
    finally:
        conn.close()
    assert client.http.request.await_count == (1 if retry_after == 120 else 3)
    monkeypatch.setattr(background_indexer, "_now_ms", lambda: state["search_retry_after"])
    client.http.request.return_value = {"messages": [], "total_results": 0}
    assert asyncio.run(background_indexer._index_next_channel_messages_search(ctx, client)) == {"messages_indexed": 0}
    conn = background_indexer._connect(background_indexer._db_path(ctx))
    try:
        recovered = conn.execute("SELECT * FROM channel_state WHERE channel_id = '10'").fetchone()
        assert recovered["search_done"] == 1
        assert recovered["search_retry_after"] is None
        assert recovered["search_last_error"] is None
    finally:
        conn.close()


def test_manual_search_returns_pending_and_cursor(tmp_path: Path) -> None:
    client = _client()
    client.http.request.return_value = {"code": 110000, "retry_after": 120}
    ctx = _ctx(tmp_path, client)
    result = asyncio.run(
        background_indexer.index_messages_search(ctx, {"channel_id": "10", "guild_id": "1", "offset": 25})
    )
    assert result["ok"] is False
    assert result["pending"] is True
    assert result["next_offset"] == 25
    assert result["messages_indexed"] == 0


def test_completed_empty_search_marks_channel_done(tmp_path: Path) -> None:
    client = _client()
    client.http.request.return_value = {"messages": [], "total_results": 0}
    ctx = _ctx(tmp_path, client)
    result = asyncio.run(background_indexer._index_next_channel_messages_search(ctx, client))
    assert result == {"messages_indexed": 0}
    conn = background_indexer._connect(background_indexer._db_path(ctx))
    try:
        assert conn.execute("SELECT search_done FROM channel_state").fetchone()[0] == 1
    finally:
        conn.close()
