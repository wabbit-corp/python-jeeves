import asyncio
import datetime as dt
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import discord

from servant.defs import GlobalContext
from servant.modules import background_indexer
from typed_json import JSONDict


class _FakeReference:
    def __init__(self, message_id: str, channel_id: str, guild_id: str) -> None:
        self.message_id = message_id
        self.channel_id = channel_id
        self.guild_id = guild_id


class _FakeUser:
    def __init__(self, user_id: str) -> None:
        self.id = user_id


class _FakeChannel:
    def __init__(self, channel_id: str) -> None:
        self.id = channel_id


class _FakeGuild:
    def __init__(self, guild_id: str) -> None:
        self.id = guild_id


class _FakeMessage:
    def __init__(
        self,
        *,
        message_id: str,
        channel_id: str,
        guild_id: str,
        author_id: str,
        reply_id: str,
        reply_channel_id: str,
        reply_guild_id: str,
        mention_id: str,
    ) -> None:
        self.id = message_id
        self.channel = _FakeChannel(channel_id)
        self.guild = _FakeGuild(guild_id)
        self.author = _FakeUser(author_id)
        self.content = "hello"
        self.mentions = [_FakeUser(mention_id)]
        self.mention_everyone = False
        self.attachments = []
        self.embeds = []
        self.pinned = False
        self.created_at = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        self.edited_at = None
        self.reference = _FakeReference(reply_id, reply_channel_id, reply_guild_id)


def test_message_row_includes_reply_reference() -> None:
    message = _FakeMessage(
        message_id="100",
        channel_id="10",
        guild_id="20",
        author_id="30",
        reply_id="200",
        reply_channel_id="11",
        reply_guild_id="21",
        mention_id="40",
    )
    row = background_indexer._message_row(cast(discord.Message, message), now=1234)
    assert row[8] == "200"
    assert row[9] == "11"
    assert row[10] == "21"
    assert json.loads(row[11]) == ["40"]


def test_channel_row_forum_extra_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeForumTag:
        def __init__(self, tag_id: int, name: str, moderated: bool) -> None:
            self.id = tag_id
            self.name = name
            self.moderated = moderated

    class _FakeForumChannel:
        def __init__(self) -> None:
            self.id = 123
            self.name = "forum"
            self.type = "forum"
            self.position = 1
            self.parent_id = None
            self.topic = "topic"
            self.nsfw = False
            self.slowmode_delay = 5
            self.created_at = None
            self.default_auto_archive_duration = 1440
            self.flags = SimpleNamespace(value=4)
            self.available_tags = [_FakeForumTag(1, "tag", True)]
            self.default_reaction_emoji = "smile"
            self.default_sort_order = SimpleNamespace(value=1)
            self.default_forum_layout = SimpleNamespace(value=2)

    monkeypatch.setattr(background_indexer.discord, "ForumChannel", _FakeForumChannel, raising=False)

    row = background_indexer._channel_row(cast(discord.abc.GuildChannel, _FakeForumChannel()), guild_id=42, now=0)
    extra_json = row[-1]
    assert extra_json is not None
    extra = json.loads(extra_json)
    assert extra["default_auto_archive_duration"] == 1440
    assert extra["flags"] == 4
    assert extra["available_tags"] == [{"id": "1", "name": "tag", "moderated": True}]
    assert extra["default_reaction_emoji"] == "smile"
    assert extra["default_sort_order"] == 1
    assert extra["default_forum_layout"] == 2


def test_channel_row_voice_extra_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeVoiceChannel:
        def __init__(self) -> None:
            self.id = 456
            self.name = "voice"
            self.type = "voice"
            self.position = 2
            self.parent_id = None
            self.topic = None
            self.nsfw = None
            self.slowmode_delay = None
            self.created_at = None
            self.default_auto_archive_duration = 60
            self.flags = SimpleNamespace(value=8)
            self.bitrate = 64000
            self.user_limit = 10
            self.rtc_region = "us-west"
            self.video_quality_mode = SimpleNamespace(value=3)

    monkeypatch.setattr(background_indexer.discord, "VoiceChannel", _FakeVoiceChannel, raising=False)

    row = background_indexer._channel_row(cast(discord.abc.GuildChannel, _FakeVoiceChannel()), guild_id=99, now=0)
    extra_json = row[-1]
    assert extra_json is not None
    extra = json.loads(extra_json)
    assert extra["default_auto_archive_duration"] == 60
    assert extra["flags"] == 8
    assert extra["bitrate"] == 64000
    assert extra["user_limit"] == 10
    assert extra["rtc_region"] == "us-west"
    assert extra["video_quality_mode"] == 3


def test_upsert_channels_preserves_extra_json() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    background_indexer._init_db(conn)

    background_indexer._upsert_channels(
        conn,
        [
            (
                "1",
                "2",
                "chan",
                "text",
                0,
                None,
                None,
                None,
                None,
                None,
                1,
                json.dumps({"foo": "bar"}),
            )
        ],
    )
    background_indexer._upsert_channels(
        conn,
        [
            (
                "1",
                "2",
                "chan-updated",
                "text",
                1,
                None,
                None,
                None,
                None,
                None,
                2,
                None,
            )
        ],
    )

    row = conn.execute("SELECT name, extra_json FROM channels WHERE channel_id = ?", ("1",)).fetchone()
    assert row is not None
    assert row["name"] == "chan-updated"
    assert row["extra_json"] == json.dumps({"foo": "bar"})


def test_index_message_payloads_preserves_embeds_on_partial_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite3"
    ctx = GlobalContext(secrets={"indexer_db_path": str(db_path)})

    payload: JSONDict = {
        "id": "123",
        "channel_id": "10",
        "guild_id": "20",
        "author": {"id": "30", "username": "user"},
        "content": "hello",
        "timestamp": "2024-01-01T00:00:00Z",
        "mentions": [{"id": "40"}],
        "mention_everyone": True,
        "attachments": [
            {
                "id": "att-1",
                "filename": "file.txt",
                "size": 1,
                "url": "https://example.test/file.txt",
                "proxy_url": "https://example.test/proxy.txt",
            }
        ],
        "embeds": [{"title": "embed"}],
        "reactions": [{"emoji": {"name": "smile"}, "count": 1, "me": True}],
        "mention_roles": ["50"],
        "mention_channels": [{"id": "60"}],
        "message_reference": {"message_id": "222", "channel_id": "10", "guild_id": "20"},
        "referenced_message": {
            "content": "parent",
            "author": {"id": "31", "username": "parent"},
            "timestamp": "2024-01-01T00:00:01Z",
        },
    }

    result = asyncio.run(
        background_indexer._index_message_payloads(
            ctx,
            [payload],
            channel_id="10",
            guild_id="20",
        )
    )
    assert result["messages_indexed"] == 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT reply_to_message_id, reply_to_channel_id, reply_to_guild_id FROM messages WHERE message_id = ?",
        ("123",),
    ).fetchone()
    assert row is not None
    assert row["reply_to_message_id"] == "222"
    assert row["reply_to_channel_id"] == "10"
    assert row["reply_to_guild_id"] == "20"

    embed_row = conn.execute(
        "SELECT COUNT(*) AS total FROM message_embeds WHERE message_id = ?",
        ("123",),
    ).fetchone()
    assert embed_row is not None
    embed_count = embed_row["total"]
    assert embed_count == 1

    referenced = conn.execute(
        "SELECT message_id FROM messages WHERE message_id = ?",
        ("222",),
    ).fetchone()
    assert referenced is not None
    conn.close()

    payload_partial: JSONDict = {
        "id": "123",
        "channel_id": "10",
        "guild_id": "20",
        "author": {"id": "30", "username": "user"},
        "content": "updated",
    }

    asyncio.run(
        background_indexer._index_message_payloads(
            ctx,
            [payload_partial],
            channel_id="10",
            guild_id="20",
        )
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    embed_row_after = conn.execute(
        "SELECT COUNT(*) AS total FROM message_embeds WHERE message_id = ?",
        ("123",),
    ).fetchone()
    assert embed_row_after is not None
    embed_count_after = embed_row_after["total"]
    assert embed_count_after == 1
    conn.close()
