from __future__ import annotations

import builtins
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .channel import Channel


class Topic:
    """
    This class represents a topic of a channel.
    """

    def __init__(self):
        self._description: str | None = None
        self._channel: Channel | None = None
        self._keywords: list[str] = []

    def _get_keywords(self) -> list[str]:
        """
        :type: [str]
        """
        return self._keywords

    def _set_keywords(self, keywords: list[str]) -> None:
        """
        Set the keywords of the topic.

        :param keywords: The keywords of the topic
        """
        self._keywords = keywords

    keywords = builtins.property(_get_keywords, _set_keywords)

    def _get_description(self) -> str:
        """
        :type: str
        """
        assert self._description is not None
        return self._description

    def _set_description(self, description: str) -> None:
        """
        Set the description of the topic.

        :param description: The description of the topic
        """
        self._description = description

    description = builtins.property(_get_description, _set_description)

    def _get_channel(self) -> Channel:
        """
        :type: Channel
        """
        assert self._channel is not None
        return self._channel

    def _set_channel(self, channel: Channel) -> None:
        """
        Set the channel of the topic.

        :param channel: The channel of the topic
        """
        self._channel = channel

    channel = builtins.property(_get_channel, _set_channel)

    def deserialize(self, data: dict, channel: Channel) -> Topic:
        """
        Deserialize a topic into a Topic object.

        :param data: The JSON topic to deserialize
        :param channel: The channel of the topic
        :return: The deserialized Topic object
        """
        self._keywords = [keyword for keyword in data["keywords"]]
        self._description = data["description"]
        self._channel = channel

        return self
