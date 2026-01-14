from __future__ import annotations

from typing import TYPE_CHECKING

from ..input.entity import Entity

if TYPE_CHECKING:
    from ..input.message import Message


class Conversation(Entity):
    """
    This class represents a conversation -- i.e. a set of correlated messages.
    """

    def __init__(self) -> None:
        super().__init__()
        self._messages: list[Message] = []

    def __repr__(self) -> str:
        return self.__str__()

    def __str__(self) -> str:
        return f"Conversation: [{len(self.messages)} messages]"

    @property
    def messages(self) -> list[Message]:
        return self._messages

    @messages.setter
    def messages(self, messages: list[Message]) -> None:
        self._messages = messages
