from __future__ import annotations

import builtins
import re
from collections.abc import Mapping
from datetime import datetime

from typed_json import JSONDict, coerce_str, require_obj

from .entity import Entity
from .message import Message
from .protocols import CommunityRef, MemberRef
from .topic import Topic


class Channel(Entity):
    """
    This class represents a channel in a community.
    """

    def __init__(self) -> None:
        super().__init__()
        self._path: str | None = None
        self._community: CommunityRef | None = None
        self._topics: list[Topic] = []
        self._messages: dict[str, Message] = {}

    def _get_path(self) -> str:
        """
        :type: str
        """
        assert self._path is not None
        return self._path

    def _set_path(self, path: str) -> None:
        """
        Set the path of the channel.

        :param path: The path of the channel
        """
        self._path = path

    @property
    def path(self) -> str:
        return self._get_path()

    @path.setter
    def path(self, value: str) -> None:
        self._set_path(value)

    def _get_community(self) -> CommunityRef:
        """
        :type: Community
        """
        assert self._community is not None
        return self._community

    def _set_community(self, community: CommunityRef) -> None:
        """
        Set the community of the channel.

        :param community: The community of the channel
        """
        self._community = community

    @property
    def community(self) -> CommunityRef:
        return self._get_community()

    @community.setter
    def community(self, value: CommunityRef) -> None:
        self._set_community(value)

    def _get_topics(self) -> list[Topic]:
        """
        :type: [Topic]
        """
        return self._topics

    def _set_topics(self, topics: list[Topic]) -> None:
        """
        Set the topics of the channel.

        :param topics: The topics of the channel
        """
        self._topics = topics

    topics = builtins.property(_get_topics, _set_topics)

    def _get_messages(self) -> dict[str, Message]:
        """
        :type: [Message]
        """
        return self._messages

    def _set_messages(self, messages: dict[str, Message]) -> None:
        """
        Set the messages of the channel.

        :param messages: The messages of the channel
        """
        self._messages = messages

    messages = builtins.property(_get_messages, _set_messages)

    def deserialize(
        self,
        data: JSONDict,
        members: Mapping[str, MemberRef] | None = None,
        community: CommunityRef | None = None,
    ) -> Channel:
        """
        Deserialize a channel into a Channel object.

        :param data: The JSON channel to be deserialized
        :param members: A dictionary of the community members
        :param community: The community of the channel
        :return: The deserialized Channel object
        """
        super().deserialize(data)

        self._path = coerce_str(data.get("path"), field="path")
        self._community = community

        topics_value = data.get("topics")
        if isinstance(topics_value, list):
            self._topics = []
            for topic in topics_value:
                if isinstance(topic, dict):
                    self._topics.append(Topic().deserialize(require_obj(topic), self))
        else:
            self._topics = []

        return self

    def time_sorted_messages(self) -> list[Message]:
        return sorted(self.messages.values(), key=lambda message: message.timestamp)

    def as_annot(self, filename: str) -> None:
        """Export this channel messages in .annot format.
        Useful for compatibility with Elsner-Charniak modified algorithm and files.

        :filename: name of the file to save. Should include .annot extension."""

        def _to_epoch_seconds(timestamp: datetime | int) -> int:
            if isinstance(timestamp, datetime):
                return int(timestamp.timestamp())
            return int(timestamp)

        with open(filename, "w") as file:
            base_time: datetime | int | None = None
            for message in self.time_sorted_messages():
                # FIXME Marco fix for timestamps other than datetime.datetime
                if base_time is None:
                    base_time = message.timestamp

                clean_text = message.original_text.replace("\n", " ")
                clean_text = clean_text.replace("\r", " ")
                # FIXME use facilities provided by the Mention class
                author_mentions = re.findall(r"<@!?(\d*)>", clean_text)
                if author_mentions is not None:
                    for author in author_mentions:
                        try:
                            new_name = self.community.authors[author].cleaned_username
                        except KeyError:
                            new_name = "Author_" + author
                        clean_text = re.sub("<@!?" + author + ">", "" + new_name + ":", clean_text)
                clean_name = message.author.cleaned_username
                if base_time is None:
                    base_time = message.timestamp
                assert base_time is not None
                elapsed_seconds = _to_epoch_seconds(message.timestamp) - _to_epoch_seconds(base_time)
                # TODO add replies
                file.write(
                    f"{message.conversation if message.conversation is not None else 'T1234'} "
                    f"{elapsed_seconds} {clean_name} :  "
                    f"{clean_text}\n"
                )
