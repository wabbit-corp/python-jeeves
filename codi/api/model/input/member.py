from __future__ import annotations

import builtins
import re

from typed_json import JSONDict, coerce_str

from .entity import Entity
from .protocols import CommunityRef, MessageRef


class Member(Entity):
    """
    This class represents a member of a community.
    """

    def __init__(self) -> None:
        super().__init__()
        self._username: str | None = None
        self._community: CommunityRef | None = None

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

    @property
    def username(self) -> str:
        return self._get_username()

    @username.setter
    def username(self, value: str) -> None:
        self._set_username(value)

    @builtins.property
    def cleaned_username(self) -> str:
        name = self.username
        name = re.sub(r"[^A-z]", "", name)
        return name.capitalize().encode("ascii", "ignore").decode("ascii")

    def _get_community(self) -> CommunityRef:
        """
        :type: Community
        """
        assert self._community is not None
        return self._community

    def _set_community(self, community: CommunityRef) -> None:
        """
        Set the community of the member.

        :param community: The community of the member
        """
        self._community = community

    @property
    def community(self) -> CommunityRef:
        return self._get_community()

    @community.setter
    def community(self, value: CommunityRef) -> None:
        self._set_community(value)

    def deserialize(self, data: JSONDict, community: CommunityRef | None = None) -> Member:
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
        self._messages: list[MessageRef] = []

    def _get_messages(self) -> list[MessageRef]:
        """
        :type: [Message]
        """
        return self._messages

    def _set_messages(self, messages: list[MessageRef]) -> None:
        """
        Set the messages of the author.

        :param messages: The messages of the author
        """
        self._messages = messages

    messages = builtins.property(_get_messages, _set_messages)
