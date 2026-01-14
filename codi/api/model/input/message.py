from __future__ import annotations

import builtins
import re
from collections.abc import Mapping, MutableMapping
from datetime import datetime
from webbrowser import open

from dateutil.parser import parse

from typed_json import JSONDict, coerce_optional_str, coerce_str, require_obj

from .attachment import Attachment
from .content import Code, Content, Emoji, Link, Multimedia, Text
from .entity import Entity
from .member import Author, Member
from .mention import ChannelMention, MemberMention, SlackChannelMention, SlackMemberMention
from .protocols import ChannelRef


class Message(Entity):
    """
    This class represents a message in a channel.
    """

    def __init__(self) -> None:
        super().__init__()
        self._text: str | None = None
        self._author: Author | None = None
        self._channel: ChannelRef | None = None
        self._timestamp: datetime | int | None = None
        self._original_text: str | None = None
        self._processable_text: str | None = None
        self._conversation: str | None = None
        self._attachments: list[Attachment] = []
        self._contents: list[Content] = []
        self._words: list[str] | None = None

    def __repr__(self) -> str:
        return self.__str__()

    def __str__(self) -> str:
        return f"Message: [{self._processable_text}]"

    @builtins.property
    def words(self) -> list[str]:
        """Return a list of words in lowercase of the content of this message.
        Removes special tokens (e.g., __MENTION__) based on double underscores."""
        if self._words is None:
            words = self.processable_text.lower().split()
            words = [
                word
                for word in words
                if (len(word) <= 4) or not (word[0] == "_" and word[1] == "_" and word[-1] == "_" and word[-2] == "_")
            ]
            self._words = words
        return self._words

    def _get_text(self) -> str:
        """
        :type: String
        """
        assert self._text is not None
        return self._text

    def _set_text(self, text: str) -> None:
        """
        Set the text of the message.

        :param text: The text of the message
        """
        self._text = text

    text = builtins.property(_get_text, _set_text)

    @builtins.property
    def original_text(self) -> str:
        """
        :type: String
        """
        assert self._original_text is not None
        return self._original_text

    def _get_timestamp(self) -> datetime | int:
        """
        :type: datetime
        """
        assert self._timestamp is not None
        return self._timestamp

    def _set_timestamp(self, timestamp: str) -> None:
        """
        Set the timestamp of the message.

        :param timestamp: The timestamp of the message
        """
        self._timestamp = parse(timestamp) if not timestamp.isnumeric() else int(timestamp)

    timestamp = builtins.property(_get_timestamp, _set_timestamp)

    def _get_author(self) -> Author:
        """
        :type: Author
        """
        assert self._author is not None
        return self._author

    def _set_author(self, author: Author) -> None:
        """
        Set the author of the message.

        :param author: The author of the message
        """
        self._author = author

    author = builtins.property(_get_author, _set_author)

    def _get_channel(self) -> ChannelRef:
        """
        :type: Channel
        """
        assert self._channel is not None
        return self._channel

    def _set_channel(self, channel: ChannelRef) -> None:
        """
        Set the channel of the message.

        :param channel: The channel of the message
        """
        self._channel = channel

    @property
    def channel(self) -> ChannelRef:
        return self._get_channel()

    @channel.setter
    def channel(self, value: ChannelRef) -> None:
        self._set_channel(value)

    def _get_contents(self) -> list[Content]:
        """
        :type: [Content]
        """
        return self._contents

    def _set_contents(self, contents: list[Content]) -> None:
        """
        Set the contents of the message.

        :param contents: The contents of the message
        """
        self._contents = contents

    contents = builtins.property(_get_contents, _set_contents)

    def _get_attachments(self) -> list[Attachment]:
        """
        :type: [Attachment]
        """
        return self._attachments

    def _set_attachments(self, attachments: list[Attachment]) -> None:
        """
        Set the attachments of the message.

        :param attachments: The attachments of the message
        """
        self._attachments = attachments

    attachments = builtins.property(_get_attachments, _set_attachments)

    def _get_processable_text(self) -> str:
        """
        :type: str
        """
        assert self._processable_text is not None
        return self._processable_text

    def _set_processable_text(self, processable_text: str) -> None:
        """
        Set the processable text of the message.

        :param processable_text: The processable text of the message
        """
        self._processable_text = processable_text
        self._words = None

    processable_text = builtins.property(_get_processable_text, _set_processable_text)

    def _get_conversation(self) -> str | None:
        """
        :type: Conversation
        """
        return self._conversation

    def _set_conversation(self, conversation: str | None) -> None:
        """
        Set the conversation of the message.

        :param conversation: The conversation of the message
        """
        self._conversation = conversation

    conversation = builtins.property(_get_conversation, _set_conversation)

    def has_code_blocks(self) -> bool:
        """Returns True if this message has at least one code block in its content."""
        for content in self.contents:
            if isinstance(content, Code):
                return True
        return False

    def has_links(self) -> bool:
        """Returns True if this message has at least one link in its content."""
        for content in self.contents:
            if isinstance(content, Link):
                return True
        return False

    def deserialize(
        self,
        data: JSONDict,
        members: dict[str, Member] | None = None,
        channels: Mapping[str, ChannelRef] | None = None,
        uninitialized_channels: MutableMapping[str, ChannelRef] | None = None,
        authors: dict[str, Author] | None = None,
        platform: str | None = None,
    ) -> Message:
        """
        Deserialize a message into a Message object.

        :param data: The JSON data to deserialize
        :param members: A dictionary of members of the community
        :param channels: The channels in the community
        :param uninitialized_channels: A dictionary of uninitialized channels in the community
        :param authors: A dictionary of authors of the community
        :param platform: The platform the community is from
        :return: The deserialized message
        """
        super().deserialize(data)

        if members is None or channels is None or uninitialized_channels is None or authors is None:
            raise ValueError("members, channels, uninitialized_channels, and authors are required.")

        member_mentions_type: type[MemberMention] = MemberMention
        channel_mentions_type: type[ChannelMention] = ChannelMention

        timestamp_value = data.get("timestamp")
        if isinstance(timestamp_value, int):
            self._timestamp = timestamp_value
        elif isinstance(timestamp_value, float):
            self._timestamp = int(timestamp_value)
        elif isinstance(timestamp_value, str):
            self._timestamp = parse(timestamp_value) if not timestamp_value.isnumeric() else int(timestamp_value)
        elif timestamp_value is None:
            raise ValueError("message timestamp is required.")
        else:
            timestamp_text = str(timestamp_value)
            self._timestamp = parse(timestamp_text) if not timestamp_text.isnumeric() else int(timestamp_text)

        self._conversation = coerce_optional_str(data.get("conversation"))

        author_id = coerce_str(data.get("authorId"), field="authorId")
        member = members[author_id]
        message_text = coerce_str(data.get("content"), field="content")
        self._original_text = message_text
        self._processable_text = message_text

        if platform == "slack":
            member_mentions_type = SlackMemberMention
            channel_mentions_type = SlackChannelMention

        attachments_list: list[JSONDict] = []
        attachments_value = data.get("attachments")
        if isinstance(attachments_value, list):
            for attachment in attachments_value:
                if isinstance(attachment, dict):
                    attachments_list.append(require_obj(attachment))

        if isinstance(member, Author):
            member.messages.append(self)
            author = member
        elif isinstance(member, Member):
            author = Author()
            author.uuid = member.uuid
            author.username = member.username
            author.messages = [self]
            author.community = member.community

            members[author_id] = author
        else:
            raise TypeError("Unexpected member type in members map.")

        self._author = author
        authors[author_id] = author

        # Retrieve attachments, code blocks, multimedia links, links, member mentions, and channel mentions
        self._attachments = Attachment.retrieve_attachments(attachments_list, self)

        code_blocks, self._processable_text = Code.retrieve(self._processable_text, self)
        multimedia_links, self._processable_text = Multimedia.retrieve(self._processable_text, self)
        links, self._processable_text = Link.retrieve(self._processable_text, self)
        emojis, self._processable_text = Emoji.retrieve(self._processable_text, self)
        member_mentions, self._processable_text = member_mentions_type.retrieve(members, self._processable_text, self)
        channel_mentions, self._processable_text = channel_mentions_type.retrieve(
            channels, self._processable_text, uninitialized_channels, self
        )

        # Add all contents to the list of contents and sort them by their start position
        self._contents.extend(code_blocks)
        self._contents.extend(member_mentions)
        self._contents.extend(channel_mentions)
        self._contents.extend(multimedia_links)
        self._contents.extend(links)
        self._contents.extend(emojis)
        self._contents.sort(key=lambda x: x.start_position)

        # Retrieve text blocks
        text_blocks = Text.retrieve(message_text, self._contents, self)

        # Add text blocks to the contents and sort them by start position
        self._contents.extend(text_blocks)
        self._contents.sort(key=lambda x: x.start_position)

        self._text = self._processable_text

        # Remove punctuation (except ' and - when used in words)
        self._processable_text = re.sub(r"\s?([^\w\s\'\-]+)\s?|[\W_]*([\-\'])[\W_]", " ", self._processable_text)

        return self

    def get_member_mentions(self) -> list[str]:
        return [mention.member.uuid for mention in self.contents if isinstance(mention, MemberMention)]

    def get_member_mentions_union(self, message2: Message, authors: bool = False) -> set[str]:
        """
        Get the union of member mentions in the message, or the union of the authors and mentions.

        :param message2: The second message to get the union of
        :param authors: Whether to get the union of the authors and mentions
        :return: The union of the member mentions in the message, or the union of the authors and mentions
        """
        message1_mentions = [mention.member.uuid for mention in self.contents if isinstance(mention, MemberMention)]
        message2_mentions = [mention.member.uuid for mention in message2.contents if isinstance(mention, MemberMention)]

        authors_set = set([self.author.uuid] + [message2.author.uuid])
        mentions = set(message1_mentions + message2_mentions)

        return authors_set & mentions if authors else set(message1_mentions) & set(message2_mentions)

    def open_in_browser(self) -> None:
        """
        Open the current message in the browser.
        """
        open(f"https://discordapp.com/channels/{self.channel.community.uuid}/{self.channel.uuid}/{self.uuid}", 2)
