from __future__ import annotations

import builtins
import re
from collections.abc import Mapping, MutableMapping

from .content import Content
from .member import Member
from .protocols import ChannelRef, MessageRef


class Mention(Content):
    """
    This class represents a mention in a channel.
    """

    pass


class MemberMention(Mention):
    """
    This class represents a member mention in a channel.
    """

    def __init__(self) -> None:
        super().__init__()
        self._member: Member | None = None

    def __str__(self, content: str | None = None) -> str:
        return super().__str__(self.member.username)

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
        message: MessageRef | None = None,
        value: str | None = None,
        *,
        members: dict[str, Member] | None = None,
        member_id: str | None = None,
        member_name: str | None = None,
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

        if members is None or member_id is None:
            raise ValueError("Member mentions require members and member_id.")

        try:
            self._member = members[member_id]
        except KeyError as exc:
            if message is None:
                raise ValueError("Member mention requires message when member is missing.") from exc
            mention = Member()
            mention.uuid = member_id
            mention.username = member_name or member_id
            mention.community = message.channel.community

            members[member_id] = mention
            self._member = members[member_id]

        return self

    @classmethod
    def retrieve(
        cls,
        members: dict[str, Member],
        message: str,
        message_obj: MessageRef | None = None,
    ) -> tuple[list[MemberMention], str]:
        """
        Retrieve the list of member mentions in a message.

        :param members: The members of the community
        :param message: The message to retrieve the mentions from
        :param message_obj: The message object
        :return: The list of mentions of a member in a channel
        """
        mentions: list[MemberMention] = []
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
            message = message.replace(pattern, "__MEMBER_MENTION__", 1)

        return mentions, message


class SlackMemberMention(MemberMention):
    """
    This class represents a member mention in a Slack message.
    """

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: MessageRef | None = None,
        value: str | None = None,
        *,
        members: dict[str, Member] | None = None,
        member_id: str | None = None,
        member_name: str | None = None,
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
        super().deserialize(
            start_position,
            end_position,
            message,
            value,
            members=members,
            member_id=member_id,
            member_name=member_name,
        )
        return self

    @classmethod
    def retrieve(
        cls,
        members: dict[str, Member],
        message: str,
        message_obj: MessageRef | None = None,
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
            message = message.replace(pattern, "__MEMBER_MENTION__", 1)

        return mentions, message


class ChannelMention(Mention):
    """
    This class represents a channel mention in a message.
    """

    def __init__(self) -> None:
        super().__init__()
        self._channel: ChannelRef | None = None

    def __str__(self, content: str | None = None) -> str:
        return super().__str__(self.channel.path)

    def _get_channel(self) -> ChannelRef:
        """
        :type: Channel
        """
        assert self._channel is not None
        return self._channel

    def _set_channel(self, channel: ChannelRef) -> None:
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
        message: MessageRef | None = None,
        value: str | None = None,
        *,
        channels: Mapping[str, ChannelRef] | None = None,
        channel_id: str | None = None,
        channel_name: str | None = None,
        uninitialized_channels: MutableMapping[str, ChannelRef] | None = None,
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

        if channels is None or channel_id is None:
            raise ValueError("Channel mentions require channels and channel_id.")

        try:
            self._channel = channels[channel_id]
        except KeyError as exc:
            if uninitialized_channels is None:
                raise ValueError("Channel mention requires uninitialized_channels when channel is missing.") from exc
            if message is None:
                raise ValueError("Channel mention requires message when channel is missing.") from exc
            new_channel_type = type(message.channel)
            new_channel = new_channel_type()
            new_channel.uuid = channel_id
            new_channel.path = channel_name or channel_id
            new_channel.community = message.channel.community

            uninitialized_channels[channel_id] = new_channel
            self._channel = new_channel

        return self

    @classmethod
    def retrieve(
        cls,
        channels: Mapping[str, ChannelRef],
        message: str,
        uninitialized_channels: MutableMapping[str, ChannelRef],
        message_obj: MessageRef | None = None,
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
            message = message.replace(pattern, "__CHANNEL_MENTION__", 1)

        return mentions, message


class SlackChannelMention(ChannelMention):
    """
    This class represents a Slack channel mention.
    """

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: MessageRef | None = None,
        value: str | None = None,
        *,
        channels: Mapping[str, ChannelRef] | None = None,
        channel_id: str | None = None,
        channel_name: str | None = None,
        uninitialized_channels: MutableMapping[str, ChannelRef] | None = None,
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
        super().deserialize(
            start_position,
            end_position,
            message,
            value,
            channels=channels,
            channel_id=channel_id,
            channel_name=channel_name,
            uninitialized_channels=uninitialized_channels,
        )
        return self

    @classmethod
    def retrieve(
        cls,
        channels: Mapping[str, ChannelRef],
        message: str,
        uninitialized_channels: MutableMapping[str, ChannelRef],
        message_obj: MessageRef | None = None,
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
            message = message.replace(pattern, "__CHANNEL_MENTION__", 1)

        return mentions, message


# TODO: Add model for roles mentions
# TODO: Add model for special mentions
