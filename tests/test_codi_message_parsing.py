from __future__ import annotations

import string

import emoji
import pytest
from hypothesis import given
from hypothesis import strategies as st

from codi.api.model.input.channel import Channel
from codi.api.model.input.community import Community
from codi.api.model.input.content import Code, Emoji, Text
from codi.api.model.input.member import Author, Member
from codi.api.model.input.mention import ChannelMention, MemberMention, SlackChannelMention, SlackMemberMention
from codi.api.model.input.message import Message
from codi.api.model.input.protocols import ChannelRef
from typed_json import JSONDict


def _build_message_context() -> tuple[Community, Channel, Message]:
    community = Community()
    community.uuid = "community-1"
    community.platform = "discord"
    community.name = "Test Community"

    channel = Channel()
    channel.uuid = "channel-1"
    channel.path = "#general"
    channel.community = community

    message = Message()
    message.channel = channel
    return community, channel, message


@given(st.integers(min_value=1, max_value=1_000_000), st.booleans())
def test_member_mention_retrieve_creates_missing_member(member_id: int, use_bang: bool) -> None:
    community, _, message = _build_message_context()
    members: dict[str, Member] = {}
    token = f"<@!{member_id}>" if use_bang else f"<@{member_id}>"
    text = f"hi {token} there"

    mentions, cleaned = MemberMention.retrieve(members, text, message_obj=message)

    member_key = str(member_id)
    assert cleaned.count("__MEMBER_MENTION__") == 1
    assert len(mentions) == 1
    assert mentions[0].member.uuid == member_key
    assert member_key in members
    assert members[member_key].community == community


def test_member_mention_retrieve_uses_existing_member() -> None:
    community, _, message = _build_message_context()
    existing = Member()
    existing.uuid = "123"
    existing.username = "Ada"
    existing.community = community
    members: dict[str, Member] = {"123": existing}

    mentions, cleaned = MemberMention.retrieve(members, "hi <@123> there", message_obj=message)

    assert cleaned.count("__MEMBER_MENTION__") == 1
    assert len(mentions) == 1
    assert mentions[0].member is existing
    assert members["123"] is existing


@given(st.integers(min_value=1, max_value=1_000_000))
def test_slack_member_mention_creates_missing_member(member_id: int) -> None:
    community, _, message = _build_message_context()
    members: dict[str, Member] = {}
    token = f"<@U{member_id}|user{member_id}>"

    mentions, cleaned = SlackMemberMention.retrieve(members, f"hey {token}", message_obj=message)

    member_key = str(member_id)
    assert cleaned.count("__MEMBER_MENTION__") == 1
    assert len(mentions) == 1
    assert mentions[0].member.uuid == member_key
    assert members[member_key].community == community


@given(st.integers(min_value=1, max_value=1_000_000))
def test_channel_mention_retrieve_creates_missing_channel(channel_id: int) -> None:
    community, channel, message = _build_message_context()
    channels: dict[str, ChannelRef] = {channel.uuid: channel}
    uninitialized: dict[str, ChannelRef] = {}
    token = f"<#{channel_id}>"

    mentions, cleaned = ChannelMention.retrieve(channels, f"ping {token}", uninitialized, message_obj=message)

    channel_key = str(channel_id)
    assert cleaned.count("__CHANNEL_MENTION__") == 1
    assert len(mentions) == 1
    assert channel_key in uninitialized
    created = uninitialized[channel_key]
    assert mentions[0].channel is created
    assert created.uuid == channel_key
    assert created.path == channel_key
    assert created.community == community


@given(st.integers(min_value=1, max_value=1_000_000))
def test_channel_mention_retrieve_uses_existing_channel(channel_id: int) -> None:
    community, channel, message = _build_message_context()
    existing = Channel()
    existing.uuid = str(channel_id)
    existing.path = "existing"
    existing.community = community
    channels: dict[str, ChannelRef] = {channel.uuid: channel, existing.uuid: existing}
    uninitialized: dict[str, ChannelRef] = {}
    token = f"<#{channel_id}>"

    mentions, cleaned = ChannelMention.retrieve(channels, f"ping {token}", uninitialized, message_obj=message)

    assert cleaned.count("__CHANNEL_MENTION__") == 1
    assert len(mentions) == 1
    assert mentions[0].channel is existing
    assert not uninitialized


@given(
    st.integers(min_value=1, max_value=1_000_000),
    st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=1, max_size=12),
)
def test_slack_channel_mention_uses_channel_name(channel_id: int, channel_name: str) -> None:
    community, channel, message = _build_message_context()
    channels: dict[str, ChannelRef] = {channel.uuid: channel}
    uninitialized: dict[str, ChannelRef] = {}
    token = f"<#C{channel_id}|{channel_name}>"

    mentions, cleaned = SlackChannelMention.retrieve(channels, f"ping {token}", uninitialized, message_obj=message)

    channel_key = str(channel_id)
    assert cleaned.count("__CHANNEL_MENTION__") == 1
    assert len(mentions) == 1
    assert channel_key in uninitialized
    created = uninitialized[channel_key]
    assert mentions[0].channel is created
    assert created.uuid == channel_key
    assert created.path == channel_name
    assert created.community == community


def test_emoji_retrieve_handles_unicode_and_colon_emoji() -> None:
    unicode_emoji = emoji.emojize(":grinning_face:")
    message = f"hello {unicode_emoji} and :wave:"

    emojis, cleaned = Emoji.retrieve(message)

    assert len(emojis) == 2
    assert cleaned.count("__EMOJI__") == 2
    assert any(entry.unicode == ":wave:" for entry in emojis)


def test_text_retrieve_splits_around_content() -> None:
    community, _, message = _build_message_context()
    code = Code().deserialize(4, 8, message, "code")

    text_blocks = Text.retrieve("abc code xyz", [code], message)

    assert [block.text for block in text_blocks] == ["abc ", " xyz"]


def test_message_deserialize_handles_mentions_and_attachments() -> None:
    community, channel, message = _build_message_context()
    member = Member()
    member.uuid = "author-1"
    member.username = "Ada"
    member.community = community

    members: dict[str, Member] = {"author-1": member}
    channels: dict[str, ChannelRef] = {channel.uuid: channel}
    uninitialized: dict[str, ChannelRef] = {}
    authors: dict[str, Author] = {}
    payload: JSONDict = {
        "id": "msg-1",
        "authorId": "author-1",
        "content": "Hello <@999> <#777> https://example.com ```code``` :wave:",
        "timestamp": 123.4,
        "conversation": "c-1",
        "attachments": [{"url": "https://example.com/file.png"}],
    }

    message.deserialize(payload, members, channels, uninitialized, authors, platform="discord")

    assert message.timestamp == 123
    assert isinstance(message.author, Author)
    assert len(message.attachments) == 1
    assert message.attachments[0].url == "https://example.com/file.png"
    assert any(isinstance(content, MemberMention) for content in message.contents)
    assert any(isinstance(content, ChannelMention) for content in message.contents)
    assert any(isinstance(content, Code) for content in message.contents)
    assert any(isinstance(content, Emoji) for content in message.contents)
    assert "999" in members
    assert "777" in uninitialized
    assert authors["author-1"] is message.author


def test_member_mention_requires_members() -> None:
    with pytest.raises(ValueError):
        MemberMention().deserialize(0, 4, None, member_id="1")
