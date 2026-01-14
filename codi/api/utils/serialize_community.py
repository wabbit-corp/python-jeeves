from __future__ import annotations

import datetime
from typing import Any, TYPE_CHECKING

from ..model.input.content import *
from ..model.input.mention import *
from ..model.input.channel import Channel
from ..model.input.message import Message

if TYPE_CHECKING:
    from ..model.input.community import Community


def serialize_community(community: Community) -> dict[str, Any]:
    out = {
        "platform": community.platform,
        "id": community.uuid,
        "name": community.name,
        "members": [],
        "authors": [],
        "channels": [],
    }

    serialize_members(community, out)
    serialize_authors(community, out)
    serialize_channels(community, out)

    return out


def serialize_members(community: Community, out: dict[str, Any]) -> None:
    for member in community.members:
        out["members"].append({"id": community.members[member].uuid, "name": community.members[member].username})


def serialize_authors(community: Community, out: dict[str, Any]) -> None:
    for author in community.authors:
        out["authors"].append(
            {
                "id": community.authors[author].uuid,
                "name": community.authors[author].username,
                "messagesIds": [message.uuid for message in community.members[author].messages],
            }
        )


def serialize_channels(community: Community, out: dict[str, Any]) -> None:
    for channel in community.channels:
        out["channels"].append(
            {
                "id": community.channels[channel].uuid,
                "path": community.channels[channel].path,
                "topics": serialize_topics(community.channels[channel]),
                "messages": serialize_messages(community.channels[channel]),
            }
        )


def serialize_topics(channel: Channel) -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []

    for i, topic in enumerate(channel.topics):
        topics.append({"description": channel.topics[i].description, "keywords": channel.topics[i].keywords})

    return topics


# def serialize_conversations(channel: Channel):
#     conversations = []
#
#     for conversation in channel.messages:
#         conversations.append({
#             'id': channel.messages[conversation].uuid,
#             'messages': serialize_messages(channel.messages[conversation])
#         })
#
#     return conversations


def serialize_messages(channel: Channel) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    for message in channel.messages.values():
        if not isinstance(message.timestamp, int):
            timestamp = datetime.datetime.timestamp(message.timestamp)
        else:
            timestamp = message.timestamp

        messages.append(
            {
                "id": message.uuid,
                "authorId": message.author.uuid,
                "author_username": message.author.username,
                "timestamp": timestamp,
                "conversationId": message.conversation,
                "processable_text": message.processable_text,
                "text": message.text,
                "attachments": serialize_attachments(message),
                "content": serialize_contents(message),
            }
        )

    return messages


def serialize_attachments(message: Message) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []

    for i, attachment in enumerate(message.attachments):
        attachments.append(
            {
                "urls": message.attachments[i].url,
            }
        )

    return attachments


def serialize_contents(message: Message) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []

    for i, content in enumerate(message.contents):
        contents.append(
            {
                "type": str(message.contents[i].__class__.__name__).lower(),
                "start_position": message.contents[i].start_position,
                "end_position": message.contents[i].end_position,
            }
        )

        if isinstance(message.contents[i], Text):
            contents[i]["text"] = message.contents[i].text
        elif isinstance(message.contents[i], Link) or isinstance(content, Multimedia):
            contents[i]["urls"] = message.contents[i].url
        elif isinstance(message.contents[i], Emoji):
            contents[i]["unicode"] = message.contents[i].unicode
        elif isinstance(message.contents[i], Code):
            contents[i]["code"] = message.contents[i].code
        elif isinstance(message.contents[i], MemberMention):
            contents[i]["memberId"] = message.contents[i].member.uuid
            message.text = message.text.replace("__MEMBER_MENTION__", f"{message.contents[i].member.username}:", 1)
        elif isinstance(message.contents[i], ChannelMention):
            contents[i]["channelId"] = message.contents[i].channel.uuid
            message.text = message.text.replace("__CHANNEL_MENTION__", f"{message.contents[i].channel.path}:", 1)

    return contents
