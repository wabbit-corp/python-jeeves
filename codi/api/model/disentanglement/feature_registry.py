from __future__ import annotations

from typing import TYPE_CHECKING

from .chat import CrossAuthorMention, HasMention, MentionOther, MentionSame, Speaker, Time
from .content import ContainsCode, ContainsLink, Repeat, Tech
from .discourse import CueWords, Greet, Long, Question, Thanks
from .feature import Feature

if TYPE_CHECKING:
    from ..input.message import Message


def get_default_features() -> list[type[Feature]]:
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


def get_features(
    message_1: Message,
    message_2: Message,
    features_type_list: list[type[Feature]],
    hyper_params: dict[str, str],
    unigram_probabilities: dict[str, float],
) -> list[Feature]:
    features: list[Feature] = []

    for feature_type in features_type_list:
        if feature_type is Time:
            features.append(Time.extract(message_1, message_2, int(hyper_params["chat bins"])))
        elif feature_type is Long:
            features.append(Long.extract(message_1, message_2, int(hyper_params["discourse max words"])))
        elif feature_type is Repeat:
            features.append(Repeat.extract(message_1, message_2, unigram_probabilities))
        else:
            features.append(feature_type.extract(message_1, message_2))

    return features
