from __future__ import annotations

import builtins

from ..input.message import Message


class Relatedness:
    """
    This class represents a pair of messages which have a percentage of relatedness.
    """

    def __init__(self) -> None:
        self._message1: Message | None = None
        self._message2: Message | None = None
        self._percentage: float | None = None

    def __repr__(self) -> str:
        return self.__str__()

    def __str__(self) -> str:
        message1 = self.message1
        message2 = self.message2
        return f"Relatedness: [msg1: {message1.processable_text}, msg2: {message2.processable_text}]"

    def _get_message1(self) -> Message:
        assert self._message1 is not None
        return self._message1

    def _set_message1(self, message1: Message) -> None:
        self._message1 = message1

    message1 = builtins.property(_get_message1, _set_message1)

    def _get_message2(self) -> Message:
        assert self._message2 is not None
        return self._message2

    def _set_message2(self, message2: Message) -> None:
        self._message2 = message2

    message2 = builtins.property(_get_message2, _set_message2)

    def _get_percentage(self) -> float | None:
        return self._percentage

    def _set_percentage(self, percentage: float) -> None:
        self._percentage = percentage

    percentage = builtins.property(_get_percentage, _set_percentage)
