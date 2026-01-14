from __future__ import annotations

import builtins
import os

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ..input.message import Message
    from ..input.community import Community


class Feature:
    """
    This class represents a feature of a pair of messages.
    """

    def __init__(self) -> None:
        self._val: float | int | None = None
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

    def _get_val(self) -> float | int | None:
        """
        :type: Any
        """
        return self._val

    def _set_val(self, val: float | int) -> None:
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

        with open(file_path, "r") as file:
            collection = [line.strip() for line in file.readlines()]

        return collection

    @staticmethod
    def get_default_features() -> list[type[Feature]]:
        """
        Get the default features to be extracted from the message.

        :return: The default features list
        """
        from ..disentanglement.content import Repeat, Tech, ContainsCode, ContainsLink
        from ..disentanglement.discourse import CueWords, Question, Long, Greet, Thanks
        from ..disentanglement.chat import Time, Speaker, CrossAuthorMention, MentionSame, MentionOther, HasMention

        return [
            Repeat,
            Tech,
            ContainsCode,
            ContainsLink,
            CueWords,
            Question,
            Long,
            Greet,
            Thanks,
            Time,
            Speaker,
            CrossAuthorMention,
            MentionSame,
            MentionOther,
            HasMention,
        ]

    @classmethod
    def get_features(
        cls,
        message_1: Message,
        message_2: Message,
        features_type_list: list[type[Feature]],
        hyper_params: dict[str, str],
        unigram_probabilities: dict[str, float],
    ) -> list[Feature]:
        """
        Extract the features -- given by feature_type_list -- of a pair of messages.

        :param message_1: The first message of the pair
        :param message_2: The second message of the pair
        :param features_type_list: The list of types of features to extract
        :param hyper_params: The hyperparameters' dictionary
        :param unigram_probabilities: The unigram probabilities of the words in the community
        :return: The feature vector of the pair of messages
        """
        from .chat import Time
        from .content import Repeat
        from .discourse import Long

        features: list[Feature] = []

        for feature_type in features_type_list:
            feature_cls = cast(Any, feature_type)
            if feature_type == Time:
                features.append(feature_cls.extract(message_1, message_2, int(hyper_params["chat bins"])))
            elif feature_type == Long:
                features.append(feature_cls.extract(message_1, message_2, int(hyper_params["discourse max words"])))
            elif feature_type == Repeat:
                features.append(feature_cls.extract(message_1, message_2, unigram_probabilities))
            else:
                features.append(feature_cls.extract(message_1, message_2))

        return features
