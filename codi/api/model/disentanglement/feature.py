from __future__ import annotations

import builtins
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..input.message import Message

FeatureValue = float | int | list[float | int]


class Feature:
    """
    This class represents a feature of a pair of messages.
    """

    def __init__(self) -> None:
        self._val: FeatureValue | None = None
        self._message_1: Message | None = None
        self._message_2: Message | None = None

    def __repr__(self) -> str:
        return self.__str__()

    def __str__(self) -> str:
        return f"{self.__class__.__name__}: {self._val}"

    def _get_message_1(self) -> Message:
        """
        :type: Message
        """
        assert self._message_1 is not None
        return self._message_1

    def _set_message_1(self, message_1: Message) -> None:
        """
        Set the first message of the pair.

        :param message_1: The first message of the pair
        """
        self._message_1 = message_1

    message_1 = builtins.property(_get_message_1, _set_message_1)

    def _get_message_2(self) -> Message:
        """
        :type: Message
        """
        assert self._message_2 is not None
        return self._message_2

    def _set_message_2(self, message_2: Message) -> None:
        """
        Set the second message of the pair.

        :param message_2: The second message of the pair
        """
        self._message_2 = message_2

    message_2 = builtins.property(_get_message_2, _set_message_2)

    def _get_val(self) -> FeatureValue | None:
        """
        :type: Any
        """
        return self._val

    def _set_val(self, val: FeatureValue) -> None:
        """
        Set the value of the feature.

        :param val: The value of the feature
        """
        self._val = val

    val = builtins.property(_get_val, _set_val)

    @staticmethod
    def _get_collection_from_file(file_name: str) -> list[str]:
        """
        Get a collection of words or phrases from the input file.

        :param file_name: The name of the file
        :return: The collection of words
        """
        file_path = os.path.join(os.path.dirname(__file__), f"../../collections/{file_name}")

        with open(file_path) as file:
            collection = [line.strip() for line in file.readlines()]

        return collection

    @staticmethod
    def get_group_features() -> list[type[Feature]]:
        raise NotImplementedError

    @classmethod
    def extract(cls, message_1: Message, message_2: Message) -> Feature:
        raise NotImplementedError

    @classmethod
    def get_features(
        cls,
        message_1: Message,
        message_2: Message,
        features_type_list: list[type[Feature]],
        hyper_params: dict[str, str],
        unigram_probabilities: dict[str, float],
    ) -> list[Feature]:
        raise NotImplementedError
