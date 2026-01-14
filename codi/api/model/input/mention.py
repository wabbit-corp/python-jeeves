from __future__ import annotations

import builtins
import re
from typing import TYPE_CHECKING, Any

from .member import Member
from .content import Content

if TYPE_CHECKING:
    from .message import Message
    from .channel import Channel


class Mention(Content):
    """
    This class represents a mention in a channel.
    """

    pass


class MemberMention(Mention):
    """
    This class represents a member mention in a channel.
    """

    def __init__(self):
        super().__init__()
        self._member: Member | None = None

    def __str__(self, content=None):
        return super.__str__(self._member.username)

    def _get_member(self) -> Member:
        """
        :type: Member
        """
        assert self._member is not None
        return self._member

    def _set_member(self, member: Member) -> None:
        """
        Set the member of this mention.

        :param member: The member of this mention
        """
        self._member = member

    member = builtins.property(_get_member, _set_member)

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: Message | None = None,
        value: str | None = None,
        **kwargs: Any,
    ) -> MemberMention:
        """
        Deserialize a mention into a MemberMention object.

        :param start_position: The start index of the mention
        :param end_position: The end index of the mention
        :param message: The message object
        :param value: Unused payload slot for Content compatibility
        :param kwargs: Mention metadata like members, member_id, member_name
        :return: The MemberMention object
        """
        super().deserialize(start_position, end_position, message, value)

        members = kwargs.get("members")
        member_id = kwargs.get("member_id")
        member_name = kwargs.get("member_name")

        try:
            assert members is not None
            assert member_id is not None
            self._member = members[member_id]
        except KeyError:
            assert members is not None
            assert member_id is not None
            assert message is not None
            mention = Member()
            mention.uuid = member_id
            mention.username = member_name
            mention.community = message.channel.community

            members[member_id] = mention
            self._member = members[member_id]

        return self

    @classmethod
    def retrieve(
        cls,
        members: dict[str, Member],
        message: str,
        message_obj: Message | None = None,
    ) -> tuple[list[MemberMention], str]:
        """
        Retrieve the list of member mentions in a message.

        :param members: The members of the community
        :param message: The message to retrieve the mentions from
        :param message_obj: The message object
        :return: The list of mentions of a member in a channel
        """
        mentions = []
        user_mention_regex = re.compile(r"<@!?(\d*)>")

        for user_mention in user_mention_regex.finditer(message):
            user_mention_id = user_mention.group(1)
            mentions.append(
                MemberMention().deserialize(
                    user_mention.start(),
                    user_mention.end(),
                    message_obj,
                    members=members,
                    member_id=user_mention_id,
                )
            )

            pattern = f"<@!{user_mention_id}>" if (f"<@!{user_mention_id}>" in message) else f"<@{user_mention_id}>"
            message = message.replace(pattern, f"__MEMBER_MENTION__", 1)

        return mentions, message


class SlackMemberMention(MemberMention):
    """
    This class represents a member mention in a Slack message.
    """

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: Message | None = None,
        value: str | None = None,
        **kwargs: Any,
    ) -> SlackMemberMention:
        """
        Deserialize a mention into a SlackMemberMention object.

        :param start_position: The start index of the mention
        :param end_position: The end index of the mention
        :param message: The message object
        :param members: The members of the community
        :param member_id: The id of the member of this mention
        :param member_name: The name of the member of this mention
        :return: The SlackMemberMention object
        """
        super().deserialize(start_position, end_position, message, value, **kwargs)
        return self

    @classmethod
    def retrieve(
        cls,
        members: dict[str, Member],
        message: str,
        message_obj: Message | None = None,
    ) -> tuple[list[MemberMention], str]:
        """
        Retrieve the list of Slack member mentions in a message.

        :param members: The members of the community
        :param message: The message to retrieve the mentions from
        :param message_obj: The message object
        :return: The list of Slack member mentions
        """
        mentions: list[MemberMention] = []
        slack_user_mention_regex = re.compile(r"<@U([^\s]*)\|([^\s]*)>")

        for slack_user_mention in slack_user_mention_regex.finditer(message):
            slack_user_mention_id = slack_user_mention.group(1)
            slack_user_mention_name = slack_user_mention.group(2)
            mentions.append(
                SlackMemberMention().deserialize(
                    slack_user_mention.start(),
                    slack_user_mention.end(),
                    message_obj,
                    members=members,
                    member_id=slack_user_mention_id,
                    member_name=slack_user_mention_name,
                )
            )

            pattern = f"<@U{slack_user_mention_id}|{slack_user_mention_name}>"
            message = message.replace(pattern, f"__MEMBER_MENTION__", 1)

        return mentions, message


class ChannelMention(Mention):
    """
    This class represents a channel mention in a message.
    """

    def __init__(self):
        super().__init__()
        self._channel: Channel | None = None

    def __str__(self, content=None):
        return super.__str__(self._channel.path)

    def _get_channel(self) -> Channel:
        """
        :type: Channel
        """
        assert self._channel is not None
        return self._channel

    def _set_channel(self, channel: Channel) -> None:
        """
        Set the channel of this mention.

        :param channel: The channel of this mention
        """
        self._channel = channel

    channel = builtins.property(_get_channel, _set_channel)

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: Message | None = None,
        value: str | None = None,
        **kwargs: Any,
    ) -> ChannelMention:
        """
        Deserialize a mention into a ChannelMention object.

        :param start_position: The start index of the mention
        :param end_position: The end index of the mention
        :param message: The message object
        :param value: Unused payload slot for Content compatibility
        :param kwargs: Mention metadata like channels, channel_id, channel_name
        :return: The ChannelMention object
        """
        super().deserialize(start_position, end_position, message, value)

        channels = kwargs.get("channels")
        channel_id = kwargs.get("channel_id")
        channel_name = kwargs.get("channel_name")
        uninitialized_channels = kwargs.get("uninitialized_channels")

        try:
            assert channels is not None
            assert channel_id is not None
            self._channel = channels[channel_id]
        except KeyError:
            from .channel import Channel

            assert uninitialized_channels is not None
            assert channel_id is not None
            assert message is not None
            new_channel = Channel()
            new_channel.uuid = channel_id
            new_channel.path = channel_name
            new_channel.community = message.channel.community

            uninitialized_channels[channel_id] = new_channel
            self._channel = new_channel

        return self

    @classmethod
    def retrieve(
        cls,
        channels: dict[str, Channel],
        message: str,
        uninitialized_channels: dict[str, Channel],
        message_obj: Message | None = None,
    ) -> tuple[list[ChannelMention], str]:
        """
        Retrieve the list of channel mentions from a message.

        :param channels: The channels of the community
        :param message: The message to retrieve the mentions from
        :param uninitialized_channels: The uninitialized channels of the community
        :param message_obj: The message object
        :return: The list of mentions of a member in a channel
        """
        mentions = []
        channel_mention_regex = re.compile(r"<#(\d*)>")

        for channel_mention in channel_mention_regex.finditer(message):
            channel_mention_id = channel_mention.group(1)
            mentions.append(
                ChannelMention().deserialize(
                    channel_mention.start(),
                    channel_mention.end(),
                    message_obj,
                    channels=channels,
                    channel_id=channel_mention_id,
                    uninitialized_channels=uninitialized_channels,
                )
            )

            pattern = f"<#{channel_mention_id}>"
            message = message.replace(pattern, f"__CHANNEL_MENTION__", 1)

        return mentions, message


class SlackChannelMention(ChannelMention):
    """
    This class represents a Slack channel mention.
    """

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: Message | None = None,
        value: str | None = None,
        **kwargs: Any,
    ) -> SlackChannelMention:
        """
        Deserialize a mention into a ChannelMention object.

        :param start_position: The start index of the mention
        :param end_position: The end index of the mention
        :param message: The message object
        :param channels: The channels of the community
        :param channel_id: The channel of this mention
        :param channel_name: The name of the channel of this mention
        :param uninitialized_channels: The uninitialized channels of the community
        :return: The ChannelMention object
        """
        super().deserialize(start_position, end_position, message, value, **kwargs)
        return self

    @classmethod
    def retrieve(
        cls,
        channels: dict[str, Channel],
        message: str,
        uninitialized_channels: dict[str, Channel],
        message_obj: Message | None = None,
    ) -> tuple[list[ChannelMention], str]:
        """
        Retrieve the list of channel mentions from a message.

        :param channels: The channels of the community
        :param message: The message to retrieve the mentions from
        :param uninitialized_channels: The uninitialized channels of the community
        :param message_obj: The message object
        :return: The list of mentions of a member in a channel
        """
        mentions: list[ChannelMention] = []
        slack_channel_mention_regex = re.compile(r"<#C([^\s]*)\|([^\s]*)>")

        for slack_channel_mention in slack_channel_mention_regex.finditer(message):
            slack_channel_id = slack_channel_mention.group(1)
            slack_channel_name = slack_channel_mention.group(2)
            mentions.append(
                SlackChannelMention().deserialize(
                    slack_channel_mention.start(),
                    slack_channel_mention.end(),
                    message_obj,
                    channels=channels,
                    channel_id=slack_channel_id,
                    channel_name=slack_channel_name,
                    uninitialized_channels=uninitialized_channels,
                )
            )

            pattern = f"<#C{slack_channel_id}|{slack_channel_name}>"
            message = message.replace(pattern, f"__CHANNEL_MENTION__", 1)

        return mentions, message


# TODO: Add model for roles mentions
# TODO: Add model for special mentions
