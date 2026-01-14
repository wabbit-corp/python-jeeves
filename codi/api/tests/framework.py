import json
from collections.abc import Callable, Sequence
from unittest import TestCase

from codi.api.model.disentanglement.feature import Feature
from codi.api.model.input.community import Community
from codi.api.model.input.member import Member
from codi.api.model.input.message import Message
from typed_json import JSONDict, require_obj


class Framework(TestCase):
    @staticmethod
    def _read_data_from_fixtures(file_path: str) -> JSONDict:
        with open(file_path) as f:
            return require_obj(json.load(f))

    @staticmethod
    def _get_community_object(data: JSONDict) -> Community:
        """
        Get the community object from the given data.

        :param data: The data to use
        :return: The community object
        """
        return Community().deserialize(data)

    @staticmethod
    def _get_community_members_and_messages(data: JSONDict) -> tuple[dict[str, Member], dict[str, Message]]:
        """
        Get the users and messages from the given data.

        :param data: The JSON data to use
        :return: The users and messages
        """
        community = Community().deserialize(data)

        return community.members, community.channels["575214823022002177"].messages

    @staticmethod
    def _get_blocks_and_messages(
        data: JSONDict,
        function: object,
        blocks_in: str,
        members: bool = False,
    ) -> tuple[list[Sequence[object]], list[str] | list[Sequence[object]]]:
        """
        Get the content blocks from the given message text.

        :param data: The data to use
        :param function: The function to call
        :param blocks_in: The field to read from. This can either be 'content' or 'attachments'.
        :param members: Whether we are testing member mentions or not
        :return: The blocks and modified messages
        """
        blocks: list[Sequence[object]] = []
        messages: list[str] = []

        messages_data = data["messages"]
        if not isinstance(messages_data, list):
            return blocks, messages

        if not callable(function):
            raise ValueError("Block extractor must be callable.")

        for message in messages_data:
            if not isinstance(message, dict):
                continue
            links: Sequence[object] = []

            if members:
                members_map: dict[str, Member] = {}
                member_data = data["members"]
                if not isinstance(member_data, list):
                    continue

                for member in member_data:
                    member_instance = Member().deserialize(require_obj(member))
                    members_map[member_instance.uuid] = member_instance

                message_value = message.get(blocks_in)
                if not isinstance(message_value, str):
                    continue
                tuple_result = function(members_map, message_value)
                if isinstance(tuple_result, tuple):
                    links, modified_message = tuple_result
                    messages.append(modified_message)
            elif blocks_in == "content":
                message_value = message.get(blocks_in)
                if not isinstance(message_value, str):
                    continue
                tuple_result = function(message_value)
                if isinstance(tuple_result, tuple):
                    links, modified_message = tuple_result
                    messages.append(modified_message)
            else:
                try:
                    attachments_value = message.get(blocks_in)
                    if isinstance(attachments_value, list):
                        attachments = [require_obj(item) for item in attachments_value if isinstance(item, dict)]
                        list_result = function(attachments, None)
                        if isinstance(list_result, list):
                            links = list_result
                except KeyError:
                    pass

            blocks.append(links)

        return blocks, messages if blocks_in == "content" else blocks

    @staticmethod
    def _get_feature(
        function: Callable[[Message, Message], Feature],
        message1: Message,
        message2: Message,
        unigram_probabilities: dict[str, float] | None = None,
    ) -> Feature:
        """
        Get the users and messages from the given data.

        :param function: The function to call
        :param message1: The first message
        :param message2: The second message
        :param community: The community to use
        :return: The users and messages
        """
        from codi.api.model.disentanglement.content import Repeat

        if function != Repeat.extract:
            return function(message1, message2)

        if unigram_probabilities is None:
            raise ValueError("unigram_probabilities is required for Repeat.extract")
        return Repeat.extract(message1, message2, unigram_probabilities)
