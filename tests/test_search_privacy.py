# SPDX-License-Identifier: LicenseRef-Wabbit-Public-Test-License-1.1

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from servant import database, permissions
from servant.defs import GlobalContext, RequestContext
from servant.modules import background_indexer, discord_search, indexed_message_search, topic_subscriptions


def search_context(tmp_path: Path) -> GlobalContext:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 10
    channel.name = "general"
    channel.type = discord.ChannelType.text
    member = SimpleNamespace(id=42)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.me = member
    guild.get_member.return_value = member
    guild.fetch_member.return_value = member
    channel.guild = guild
    channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
    client = MagicMock(spec=discord.Client)
    client.get_channel.return_value = channel
    client.fetch_channel.return_value = channel
    client.get_guild.return_value = guild
    ctx = GlobalContext(config={"indexer_db_path": str(tmp_path / "index.db"), "admin_user_ids": ["42"]})
    ctx.discord_client = client
    ctx.channel_controls.loaded = True
    return ctx.with_request(RequestContext(user_id="42", channel_id="10", guild_id="1", is_dm=False))


def test_unfiltered_index_search_never_reads_other_channels_or_servers(tmp_path: Path) -> None:
    ctx = search_context(tmp_path)
    with database.connect(tmp_path / "index.db") as conn:
        background_indexer._init_db(conn)
        conn.executemany(
            "INSERT INTO messages(message_id,channel_id,guild_id,author_id,content,content_available,created_at) "
            "VALUES(?,?,?,'99',?,1,1)",
            [("100", "10", "1", "local"), ("101", "11", "1", "staff secret"), ("102", "20", "2", "other server")],
        )
    result = asyncio.run(indexed_message_search.indexed_messages_search(ctx, {}))
    assert [item["message_id"] for item in result["results"]] == ["100"]


@pytest.mark.parametrize(
    "tool",
    [
        indexed_message_search.indexed_messages_search,
        discord_search.discord_search_messages,
        background_indexer.index_messages_search,
    ],
)
@pytest.mark.parametrize("arguments", [{"channel_id": "11"}, {"channel_id": "20", "guild_id": "2"}])
def test_message_tools_reject_other_channels_before_fetching_or_querying(tmp_path: Path, tool, arguments) -> None:
    ctx = search_context(tmp_path)
    with pytest.raises(PermissionError):
        asyncio.run(tool(ctx, arguments))
    ctx.discord_client.fetch_channel.assert_not_called()
    assert not (tmp_path / "index.db").exists()


@pytest.mark.parametrize(
    "tool",
    [
        indexed_message_search.indexed_messages_search,
        discord_search.discord_search_messages,
        background_indexer.index_messages_search,
    ],
)
@pytest.mark.parametrize("failure", ["read_denied", "membership_lost", "disabled", "wrong_guild", "lookup_failed"])
def test_message_search_fails_closed_with_current_permissions(tmp_path: Path, tool, failure: str) -> None:
    ctx = search_context(tmp_path)
    channel = ctx.discord_client.fetch_channel.return_value
    if failure == "read_denied":
        channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=False)
    elif failure == "membership_lost":
        channel.guild.fetch_member.side_effect = RuntimeError("member left")
    elif failure == "disabled":
        ctx.channel_controls.disabled["10"] = "1"
    elif failure == "wrong_guild":
        channel.guild.id = 2
    else:
        ctx.discord_client.fetch_channel.side_effect = RuntimeError("Discord unavailable")
    with pytest.raises(PermissionError):
        asyncio.run(tool(ctx, {"channel_id": "10"}))
    channel.history.assert_not_called()
    assert not (tmp_path / "index.db").exists()


def test_live_history_search_returns_only_allowed_authors(tmp_path: Path) -> None:
    ctx = search_context(tmp_path)
    channel = ctx.discord_client.fetch_channel.return_value
    ctx.privacy.opted_out_users.add("99")

    async def history(**_kwargs):
        for uid in (42, 99):
            message = MagicMock(spec=discord.Message)
            message.id = uid
            message.channel = channel
            message.guild = channel.guild
            message.author = SimpleNamespace(id=uid, name="member", bot=False)
            message.content = "history"
            message.created_at = dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc)
            message.edited_at = None
            message.attachments = []
            message.jump_url = "https://discord.com/channels/1/10/42"
            yield message

    channel.history.side_effect = history
    result = asyncio.run(discord_search.discord_search_messages(ctx, {"channel_id": "10"}))
    assert [row["message_id"] for row in result["results"]] == ["42"]


def test_private_thread_requires_current_membership_and_parent_permissions(tmp_path: Path) -> None:
    ctx = search_context(tmp_path)
    parent = ctx.discord_client.fetch_channel.return_value
    thread = MagicMock(spec=discord.Thread)
    thread.id = 10
    thread.parent_id = 9
    thread.guild = parent.guild
    thread.is_private.return_value = True
    thread.permissions_for.return_value = parent.permissions_for.return_value
    thread.fetch_member.side_effect = RuntimeError("not a thread member")
    ctx.discord_client.fetch_channel.side_effect = lambda cid: thread if cid == 10 else parent
    with pytest.raises(PermissionError):
        asyncio.run(permissions.require_search_scope(ctx))
    thread.fetch_member.side_effect = None
    assert asyncio.run(permissions.require_search_scope(ctx)) is thread
    parent.permissions_for.return_value = discord.Permissions(view_channel=False, read_message_history=True)
    with pytest.raises(PermissionError):
        asyncio.run(permissions.require_search_scope(ctx))


@pytest.mark.parametrize("recipient_id", [42, 99])
def test_dm_search_is_limited_to_requesters_own_conversation(tmp_path: Path, recipient_id: int) -> None:
    ctx = search_context(tmp_path).with_request(RequestContext("42", "10", None, True))
    dm = MagicMock(spec=discord.DMChannel)
    dm.id = 10
    dm.recipient = SimpleNamespace(id=recipient_id)
    ctx.discord_client.fetch_channel.return_value = dm
    if recipient_id == 42:
        assert asyncio.run(permissions.require_search_scope(ctx)) is dm
    else:
        with pytest.raises(PermissionError):
            asyncio.run(permissions.require_search_scope(ctx))


@pytest.mark.parametrize("allowed", [False, True])
def test_topic_notifications_check_subscribers_current_channel_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
    from unittest.mock import AsyncMock

    ctx = search_context(tmp_path)
    channel = ctx.discord_client.fetch_channel.return_value
    channel.guild.name = "community"
    channel.permissions_for.return_value = discord.Permissions(view_channel=allowed, read_message_history=allowed)
    match = MagicMock(return_value=[{"user_id": "43", "topics": []}])
    monkeypatch.setattr(topic_subscriptions, "_match_subscriptions", match)
    send = AsyncMock()
    monkeypatch.setattr(topic_subscriptions, "_send_dm", send)
    monkeypatch.setattr(topic_subscriptions, "_record_cooldowns", AsyncMock())
    message = SimpleNamespace(
        id=101,
        channel=channel,
        guild=channel.guild,
        author=SimpleNamespace(id=42, name="member"),
        content="topic discussion",
        jump_url="https://discord.com/channels/1/10/101",
    )
    result = asyncio.run(topic_subscriptions.topic_subscriptions_handle_message(ctx, message))
    assert result["notified"] == int(allowed)
    assert send.await_count == int(allowed)
    channel.guild.fetch_member.assert_awaited_with(43)
