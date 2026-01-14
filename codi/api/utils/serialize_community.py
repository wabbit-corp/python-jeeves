from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from typed_json import JSON, JSONDict

from ..model.input.channel import Channel
from ..model.input.content import Code, Emoji, Link, Multimedia, Text
from ..model.input.mention import ChannelMention, MemberMention
from ..model.input.message import Message

if TYPE_CHECKING:
    from ..model.input.community import Community


def serialize_community(community: Community) -> JSONDict:
    members: list[JSON] = []
    authors: list[JSON] = []
    channels: list[JSON] = []

    out: JSONDict = {
        "platform": community.platform,
        "id": community.uuid,
        "name": community.name,
        "members": members,
        "authors": authors,
        "channels": channels,
    }

    serialize_members(community, members)
    serialize_authors(community, authors)
    serialize_channels(community, channels)

    return out


def serialize_members(community: Community, members: list[JSON]) -> None:
    for member in community.members:
        members.append({"id": community.members[member].uuid, "name": community.members[member].username})


def serialize_authors(community: Community, authors: list[JSON]) -> None:
    for author in community.authors:
        authors.append(
            {
                "id": community.authors[author].uuid,
                "name": community.authors[author].username,
                "messagesIds": [message.uuid for message in community.members[author].messages],
            }
        )


def serialize_channels(community: Community, channels: list[JSON]) -> None:
    for channel in community.channels:
        channels.append(
            {
                "id": community.channels[channel].uuid,
                "path": community.channels[channel].path,
                "topics": serialize_topics(community.channels[channel]),
                "messages": serialize_messages(community.channels[channel]),
            }
        )


def serialize_topics(channel: Channel) -> list[JSON]:
    topics: list[JSON] = []

    for topic in channel.topics:
        topics.append({"description": topic.description, "keywords": topic.keywords})

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


def serialize_messages(channel: Channel) -> list[JSON]:
    messages: list[JSON] = []

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


def serialize_attachments(message: Message) -> list[JSON]:
    attachments: list[JSON] = []

    for attachment in message.attachments:
        attachments.append(
            {
                "urls": attachment.url,
            }
        )

    return attachments


def serialize_contents(message: Message) -> list[JSON]:
    contents: list[JSON] = []

    for content in message.contents:
        content_entry: JSONDict = {
            "type": str(content.__class__.__name__).lower(),
            "start_position": content.start_position,
            "end_position": content.end_position,
        }

        if isinstance(content, Text):
            content_entry["text"] = content.text
        elif isinstance(content, Link) or isinstance(content, Multimedia):
            content_entry["urls"] = content.url
        elif isinstance(content, Emoji):
            content_entry["unicode"] = content.unicode
        elif isinstance(content, Code):
            content_entry["code"] = content.code
        elif isinstance(content, MemberMention):
            content_entry["memberId"] = content.member.uuid
            message.text = message.text.replace("__MEMBER_MENTION__", f"{content.member.username}:", 1)
        elif isinstance(content, ChannelMention):
            content_entry["channelId"] = content.channel.uuid
            message.text = message.text.replace("__CHANNEL_MENTION__", f"{content.channel.path}:", 1)

        contents.append(content_entry)

    return contents
