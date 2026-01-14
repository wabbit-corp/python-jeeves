from __future__ import annotations

from typed_json import JSONDict, coerce_str


class Entity:
    """
    This class represents a generic entity which has an id.
    """

    def __init__(self) -> None:
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

    @property
    def uuid(self) -> str:
        return self._get_uuid()

    @uuid.setter
    def uuid(self, value: str) -> None:
        self._set_uuid(value)

    def deserialize(self, data: JSONDict) -> Entity:
        """
        Deserialize an entity into an Entity object.

        :param data: The JSON data to deserialize
        """
        self.uuid = coerce_str(data.get("id"), field="id")
        return self
