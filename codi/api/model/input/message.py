from __future__ import annotations

import builtins
from datetime import datetime
from typing import TYPE_CHECKING, List
from webbrowser import open
from dateutil.parser import parse

from .member import *
from .content import *
from .mention import *
from .attachment import *

if TYPE_CHECKING:
    from .channel import Channel


class Message(Entity):
    """
    This class represents a message in a channel.
    """

    def __init__(self):
        super().__init__()
        self._text: str | None = None
        self._author: Author | None = None
        self._channel: Channel | None = None
        self._timestamp: datetime | int | None = None
        self._original_text: str | None = None
        self._processable_text: str | None = None
        self._conversation: str | None = None
        self._attachments: list[Attachment] = []
        self._contents: list[Content] = []
        self._words: list[str] | None = None

    def __repr__(self):
        self.__str__()

    def __str__(self):
        return f"Message: [{self._processable_text}]"

    @builtins.property
    def words(self) -> List[str]:
        """Return a list of words in lowercase of the content of this message.
        Removes special tokens (e.g., __MENTION__) based on double underscores."""
        if self._words is None:
            self._words = self.processable_text.lower().split()
            self._words = list(
                filter(
                    lambda x: (len(x) <= 4) or not (x[0] == "_" and x[1] == "_" and x[-1] == "_" and x[-2] == "_"),
                    self._words,
                )
            )
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

    def _get_channel(self) -> Channel:
        """
        :type: Channel
        """
        assert self._channel is not None
        return self._channel

    def _set_channel(self, channel: Channel) -> None:
        """
        Set the channel of the message.

        :param channel: The channel of the message
        """
        self._channel = channel

    channel = builtins.property(_get_channel, _set_channel)

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

    def has_code_blocks(self):
        """Returns True if this message has at least one code block in its content."""
        for content in self.contents:
            if isinstance(content, Code):
                return True
        return False

    def has_links(self):
        """Returns True if this message has at least one link in its content."""
        for content in self.contents:
            if isinstance(content, Link):
                return True
        return False

    def deserialize(
        self,
        data: dict,
        members: dict[str, Member] | None = None,
        channels: dict[str, Channel] | None = None,
        uninitialized_channels: dict[str, Channel] | None = None,
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

        assert members is not None
        assert channels is not None
        assert uninitialized_channels is not None
        assert authors is not None

        member_mentions_type: type[MemberMention] = MemberMention
        channel_mentions_type: type[ChannelMention] = ChannelMention

        timestamp = data["timestamp"]
        if isinstance(timestamp, int):
            self._timestamp = timestamp
        else:
            self._timestamp = parse(timestamp) if not timestamp.isnumeric() else int(timestamp)

        try:
            self._conversation = data["conversation"]
        except KeyError:
            self._conversation = None

        member = members[data["authorId"]]
        self._original_text = data["content"]
        message_text = data["content"]
        self._processable_text = message_text

        if platform == "slack":
            member_mentions_type = SlackMemberMention
            channel_mentions_type = SlackChannelMention

        try:
            attachments = data["attachments"]
        except KeyError:
            attachments = []

        if isinstance(member, Author):
            member.messages.append(self)
            author = member
        elif isinstance(member, Member):
            author = Author()
            author.uuid = member.uuid
            author.username = member.username
            author.messages = [self]
            author.community = member.community

            members[data["authorId"]] = author

        self._author = author
        authors[data["authorId"]] = author

        # Retrieve attachments, code blocks, multimedia links, links, member mentions, and channel mentions
        self._attachments = Attachment.retrieve_attachments(attachments, self)

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

    def get_member_mentions(self):
        return [mention.member.uuid for mention in self.contents if isinstance(mention, MemberMention)]

    def get_member_mentions_union(self, message2, authors: bool = False):
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

    def open_in_browser(self):
        """
        Open the current message in the browser.
        """
        open(f"https://discordapp.com/channels/{self.channel.community.uuid}/{self.channel.uuid}/{self.uuid}", 2)
