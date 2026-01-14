from __future__ import annotations

import builtins


class Entity:
    """
    This class represents a generic entity which has an id.
    """

    def __init__(self):
        self._uuid: str | None = None

    def _get_uuid(self) -> str:
        assert self._uuid is not None
        return self._uuid

    def _set_uuid(self, value: str) -> None:
        """
        Set the UUID of the entity.

        :param value: The UUID of the entity
        """
        self._uuid = value

    uuid = builtins.property(_get_uuid, _set_uuid)

    def deserialize(self, data: dict):
        """
        Deserialize an entity into an Entity object.

        :param data: The JSON data to deserialize
        """
        self.uuid = data["id"]
