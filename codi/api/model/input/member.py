from __future__ import annotations

import builtins
import re
from typing import TYPE_CHECKING

from typed_json import JSONDict, coerce_str

from .entity import Entity

if TYPE_CHECKING:
    from .community import Community
    from .message import Message


class Member(Entity):
    """
    This class represents a member of a community.
    """

    def __init__(self) -> None:
        super().__init__()
        self._username: str | None = None
        self._community: Community | None = None

    def _get_username(self) -> str:
        """
        :type: str
        """
        assert self._username is not None
        return self._username

    def _set_username(self, username: str) -> None:
        """
        Set the username of the member.

        :param username: The username of the member
        """
        self._username = username

    username = builtins.property(_get_username, _set_username)

    @builtins.property
    def cleaned_username(self) -> str:
        name = self.username
        name = re.sub(r"[^A-z]", "", name)
        return name.capitalize().encode("ascii", "ignore").decode("ascii")

    def _get_community(self) -> Community:
        """
        :type: Community
        """
        assert self._community is not None
        return self._community

    def _set_community(self, community: Community) -> None:
        """
        Set the community of the member.

        :param community: The community of the member
        """
        self._community = community

    community = builtins.property(_get_community, _set_community)

    def deserialize(self, data: JSONDict, community: Community | None = None) -> Member:
        """
        Deserialize the data into a Member object.

        :param data: The data to deserialize
        :param community: The community object
        """
        super().deserialize(data)
        self._username = coerce_str(data.get("name"), field="name")
        self._community = community

        return self


class Author(Member):
    """
    This class represents a member of a community who has written at least one message.
    """

    def __init__(self) -> None:
        super().__init__()
        self._messages: list[Message] = []

    def _get_messages(self) -> list[Message]:
        """
        :type: [Message]
        """
        return self._messages

    def _set_messages(self, messages: list[Message]) -> None:
        """
        Set the messages of the author.

        :param messages: The messages of the author
        """
        self._messages = messages

    messages = builtins.property(_get_messages, _set_messages)
