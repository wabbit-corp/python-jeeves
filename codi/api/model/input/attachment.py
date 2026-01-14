from __future__ import annotations

import builtins
from collections.abc import Sequence

from typed_json import JSONDict, coerce_str

from .protocols import MessageRef


class Attachment:
    """
    This class represents an attachment in a message.
    """

    def __init__(self) -> None:
        self._url: str | None = None
        self._message: MessageRef | None = None

    def __repr__(self) -> str:
        return self.__str__()

    def __str__(self) -> str:
        return f"{self.__class__}: {self._url}"

    def _get_url(self) -> str:
        """
        :type: str
        """
        assert self._url is not None
        return self._url

    def _set_url(self, url: str) -> None:
        """
        Set the URL of the attachment.

        :param url: The URL of the attachment
        """
        self._url = url

    url = builtins.property(_get_url, _set_url)

    def _get_message(self) -> MessageRef:
        """
        :type: Message
        """
        assert self._message is not None
        return self._message

    def _set_message(self, message: MessageRef) -> None:
        """
        Set the message of the attachment.

        :param message: The message of the attachment
        """
        self._message = message

    message = builtins.property(_get_message, _set_message)

    def deserialize(
        self,
        url: str | None = None,
        message: MessageRef | None = None,
    ) -> Attachment:
        """
        Deserialize an attachment into an Attachment object.

        :param url: The URL of the attachment
        :param message: The message of the attachment
        :return: The deserialized attachment
        """

        self._url = url
        self._message = message

        return self

    @classmethod
    def retrieve_attachments(
        cls,
        attachments: Sequence[JSONDict],
        message: MessageRef | None,
    ) -> list[Attachment]:
        """
        Retrieve a list of attachments from a dictionary.

        :param attachments: The dictionary containing the attachments
        :param message: The message of the attachments
        :return: The list of attachments
        """
        retrieved: list[Attachment] = []
        for attachment in attachments:
            url_value = attachment.get("url")
            if url_value is None:
                url_value = attachment.get("urls")
            url = coerce_str(url_value, field="url")
            retrieved.append(Attachment().deserialize(url, message))
        return retrieved
