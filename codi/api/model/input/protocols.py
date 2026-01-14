from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class CommunityRef(Protocol):
    @property
    def uuid(self) -> str: ...

    @uuid.setter
    def uuid(self, value: str) -> None: ...

    @property
    def authors(self) -> Mapping[str, MemberRef]: ...


class MemberRef(Protocol):
    @property
    def uuid(self) -> str: ...

    @uuid.setter
    def uuid(self, value: str) -> None: ...

    @property
    def username(self) -> str: ...

    @username.setter
    def username(self, value: str) -> None: ...

    @property
    def cleaned_username(self) -> str: ...


class ChannelRef(Protocol):
    @property
    def uuid(self) -> str: ...

    @uuid.setter
    def uuid(self, value: str) -> None: ...

    @property
    def path(self) -> str: ...

    @path.setter
    def path(self, value: str) -> None: ...

    @property
    def community(self) -> CommunityRef: ...

    @community.setter
    def community(self, value: CommunityRef) -> None: ...


class MessageRef(Protocol):
    @property
    def uuid(self) -> str: ...

    @uuid.setter
    def uuid(self, value: str) -> None: ...

    @property
    def channel(self) -> ChannelRef: ...

    @channel.setter
    def channel(self, value: ChannelRef) -> None: ...
