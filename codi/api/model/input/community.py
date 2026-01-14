from __future__ import annotations

import builtins
import configparser
import datetime
import json
import os
import pickle
from collections.abc import MutableMapping
from typing import TYPE_CHECKING

from typed_json import JSON, JSONDict, coerce_str, obj_to_json, require_obj

from .channel import Channel
from .content import Code, Emoji, Link, Multimedia, Text
from .entity import Entity
from .member import Author, Member
from .mention import ChannelMention, MemberMention
from .message import Message
from .protocols import ChannelRef

if TYPE_CHECKING:
    from ...utils.compute_statistics import Statistics


class Community(Entity):
    """
    This class represents a community.
    """

    def __init__(self) -> None:
        super().__init__()
        config = configparser.ConfigParser()
        config.read(os.path.join(os.path.dirname(__file__), "../../../../config.ini"))

        self._platform: str | None = None
        self._name: str | None = None
        self._members: dict[str, Member] = {}
        self._channels: dict[str, Channel] = {}
        self._authors: dict[str, Author] = {}
        self._constants = config["constants"]

    def _get_platform(self) -> str:
        """
        :type: str
        """
        assert self._platform is not None
        return self._platform

    def _set_platform(self, platform: str) -> None:
        """
        Set the members of the community.

        :param platform: The name of the platform
        """
        self._platform = platform

    platform = builtins.property(_get_platform, _set_platform)

    def _get_name(self) -> str:
        """
        :type: str
        """
        assert self._name is not None
        return self._name

    def _set_name(self, name: str) -> None:
        """
        Set the name of the community.

        :param name: The name of the community
        """
        name = name.lower().replace(" ", "-")
        self._name = name

    name = builtins.property(_get_name, _set_name)

    def _get_members(self) -> dict[str, Member]:
        """
        :type: [Member]
        """
        return self._members

    def _set_members(self, members: dict[str, Member]) -> None:
        """
        Set the members of the community.

        :param members: The members of the community
        """
        self._members = members

    members = builtins.property(_get_members, _set_members)

    def _get_channels(self) -> dict[str, Channel]:
        """
        :type: [Channel]
        """
        return self._channels

    def _set_channels(self, channels: dict[str, Channel]) -> None:
        """
        Set the channels of the community.

        :param channels: The channels of the community
        """
        self._channels = channels

    channels = builtins.property(_get_channels, _set_channels)

    def _get_authors(self) -> dict[str, Author]:
        """
        :type: Dict[str, Author]
        """
        return self._authors

    def _set_authors(self, authors: dict[str, Author]) -> None:
        """
        Set the authors of the community.

        :param authors: The authors of the community
        """
        self._authors = authors

    @property
    def authors(self) -> dict[str, Author]:
        return self._get_authors()

    @authors.setter
    def authors(self, value: dict[str, Author]) -> None:
        self._set_authors(value)

    def _save(self) -> None:
        """
        Save the community to the data/ directory. If the community is new, it will be created. If the community
        already exists, it will be overwritten. A maximum of "_max_communities" different communities can be kept in
        the data/ directory at once.
        """
        path = os.path.join(os.path.dirname(__file__), "../../../../data")

        # Check if the `data/` directory exists
        if not os.path.exists(path):
            os.makedirs(path)

        _, _, files = next(os.walk(path))

        # Remove the old community file if it exists
        if f"{self._name}.pickle" in files:
            os.remove(os.path.join(path, f"{self._name}.pickle"))
        elif len(files) >= int(self._constants["max saved communities"]):
            raise Exception("Maximum number of pickled communities reached")

        # Save the newly created community
        with open(os.path.join(path, f"./{self._name}.pickle"), "wb") as f:
            pickle.dump(self, f)

    def serialize(self) -> JSONDict:
        members: list[JSON] = []
        authors: list[JSON] = []
        channels: list[JSON] = []

        out: JSONDict = {
            "platform": self.platform,
            "id": self.uuid,
            "name": self.name,
            "members": members,
            "authors": authors,
            "channels": channels,
        }

        _serialize_members(self, members)
        _serialize_authors(self, authors)
        _serialize_channels(self, channels)

        return out

    def save_json(
        self,
        op_type: int,
        statistics: Statistics | None = None,
        gold: JSON | None = None,
    ) -> JSONDict:
        """
        Save the community into a JSON file.

        :param op_type: Indicates the operation
                        0 for training
                        1 for validation
                        2 for prediction
        :param statistics: The statistics of the prediction or validation
        :param gold: It is the gold set of conversations used in validation
        """
        community_mod = self.serialize()
        path = os.path.join(os.path.dirname(__file__), "../../training/tmp/json")

        if gold:
            community_mod["gold"] = gold

        if op_type == 1 or op_type == 2:
            community_mod["statistics"] = obj_to_json(statistics) if statistics is not None else None

        match op_type:
            case 0:
                community_mod["type"] = "training"
            case 1:
                community_mod["type"] = "validation"
            case 2:
                community_mod["type"] = "prediction"

        # Check if the `tmp/` directory exists
        if not os.path.exists(path):
            os.makedirs(path)

        _, _, files = next(os.walk(path))

        # Remove the old community file if it exists
        if f'latest-{community_mod["type"]}.json' in files:
            os.remove(os.path.join(path, f'latest-{community_mod["type"]}.json'))

        # Save the newly created community
        with open(os.path.join(path, f'latest-{community_mod["type"]}.json'), "w") as f:
            json.dump(community_mod, f)

        return community_mod

    def get_messages(self) -> list[Message]:
        """
        Get all messages of the community.

        :return: The messages of the community
        """
        messages: list[Message] = []

        for channel in self._channels:
            for message in self._channels[channel].messages:
                messages.append(self._channels[channel].messages[message])

        return messages

    def deserialize(self, request: JSONDict) -> Community:
        """
        Deserialize a request into a Community object.

        :param request: The JSON community to be deserialized
        :return: The deserialized community
        """
        authors: dict[str, Author] = {}
        uninitialized_channels: MutableMapping[str, ChannelRef] = {}
        self._platform = coerce_str(request.get("platform"), field="platform").lower()
        self._uuid = coerce_str(request.get("id"), field="id")
        self._name = coerce_str(request.get("name"), field="name").lower().replace(" ", "-")

        # Deserialize Members
        members_value = request.get("members")
        if not isinstance(members_value, list):
            raise ValueError("members must be a list")
        for member in members_value:
            member_instance = Member().deserialize(require_obj(member), self)
            self._members[member_instance.uuid] = member_instance

        # Deserialize Channels
        channels_value = request.get("channels")
        if not isinstance(channels_value, list):
            raise ValueError("channels must be a list")
        channel_entries: list[JSONDict] = []
        channel_ids: list[str] = []
        for channel in channels_value:
            channel_obj = require_obj(channel)
            channel_entries.append(channel_obj)
            channel_instance = Channel().deserialize(channel_obj, self._members, self)
            self._channels[channel_instance.uuid] = channel_instance
            channel_ids.append(channel_instance.uuid)

        # Deserialize Messages
        for index, channel_id in enumerate(channel_ids):
            messages_value = channel_entries[index].get("messages")
            if not isinstance(messages_value, list):
                continue
            for message in messages_value:
                message_instance = Message()
                message_instance.channel = self._channels[channel_id]
                message_instance.deserialize(
                    require_obj(message), self._members, self._channels, uninitialized_channels, authors, self._platform
                )

                self._channels[channel_id].messages[message_instance.uuid] = message_instance

        # Merge authors and uninitialized channels into the community authors and channels respectively
        self._authors |= authors
        for channel_id, pending_channel in uninitialized_channels.items():
            if not isinstance(pending_channel, Channel):
                raise ValueError("Uninitialized channels must be Channel instances.")
            self._channels[channel_id] = pending_channel

        self._save()

        return self

    def from_json(self, filename: str) -> None:
        """Initialize this community with the content of the json file.

        :filename: name of the input json file."""
        with open(filename) as file:
            community = json.load(file)
            self.deserialize(community)


def _serialize_members(community: Community, members: list[JSON]) -> None:
    for member in community.members:
        members.append({"id": community.members[member].uuid, "name": community.members[member].username})


def _serialize_authors(community: Community, authors: list[JSON]) -> None:
    for author in community.authors:
        authors.append(
            {
                "id": community.authors[author].uuid,
                "name": community.authors[author].username,
                "messagesIds": [message.uuid for message in community.members[author].messages],
            }
        )


def _serialize_channels(community: Community, channels: list[JSON]) -> None:
    for channel in community.channels:
        channels.append(
            {
                "id": community.channels[channel].uuid,
                "path": community.channels[channel].path,
                "topics": _serialize_topics(community.channels[channel]),
                "messages": _serialize_messages(community.channels[channel]),
            }
        )


def _serialize_topics(channel: Channel) -> list[JSON]:
    topics: list[JSON] = []

    for topic in channel.topics:
        topics.append({"description": topic.description, "keywords": topic.keywords})

    return topics


def _serialize_messages(channel: Channel) -> list[JSON]:
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
                "attachments": _serialize_attachments(message),
                "content": _serialize_contents(message),
            }
        )

    return messages


def _serialize_attachments(message: Message) -> list[JSON]:
    attachments: list[JSON] = []

    for attachment in message.attachments:
        attachments.append(
            {
                "urls": attachment.url,
            }
        )

    return attachments


def _serialize_contents(message: Message) -> list[JSON]:
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
