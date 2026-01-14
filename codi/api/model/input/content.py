from __future__ import annotations

import builtins
import re
import emoji

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .message import Message


class Content:
    """
    This class represents the content of a message
    """

    def __init__(self):
        self._start_position: int | None = None
        self._end_position: int | None = None
        self._message: Message | None = None

    def __repr__(self):
        return self.__str__()

    def __str__(self, content=None):
        return f"{self.__class__}: {content}"

    def _get_start_position(self) -> int:
        """
        :type: int
        """
        assert self._start_position is not None
        return self._start_position

    def _set_start_position(self, start_position: int) -> None:
        """
        Set the start position of the content in the message.

        :param start_position: The start position of the content in the message
        """
        self._start_position = start_position

    start_position = builtins.property(_get_start_position, _set_start_position)

    def _get_end_position(self) -> int:
        """
        :type: int
        """
        assert self._end_position is not None
        return self._end_position

    def _set_end_position(self, end_position: int) -> None:
        """
        Set the end position of the content in the message.

        :param end_position: The end position of the content in the message
        """
        self._end_position = end_position

    end_position = builtins.property(_get_end_position, _set_end_position)

    def _get_message(self) -> Message:
        """
        :type: str
        """
        assert self._message is not None
        return self._message

    def _set_message(self, message: Message) -> None:
        """
        Set the message.

        :param message: The message
        """
        self._message = message

    message = builtins.property(_get_message, _set_message)

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: Message | None = None,
        value: str | None = None,
    ) -> Content:
        self._start_position = start_position
        self._end_position = end_position
        self._message = message
        return self


class Text(Content):
    """
    This class represents a textual content in a message.
    """

    def __init__(self):
        super().__init__()
        self._text: str | None = None

    def __str__(self, content=None):
        return super.__str__(self._text)

    def _get_text(self) -> str:
        """
        :type: str
        """
        assert self._text is not None
        return self._text

    def _set_text(self, text: str) -> None:
        """
        Set the text of the content.

        :param text: The text of the content
        """
        self._text = text

    text = builtins.property(_get_text, _set_text)

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: Message | None = None,
        value: str | None = None,
    ) -> Text:
        """
        Deserialize the text contents of a message.

        :param start_position: The start position of the content in the message
        :param end_position: The end position of the content in the message
        :param message: The message object
        :param value: The text of the content
        :return:
        """
        super().deserialize(start_position, end_position, message, value)
        self._text = value

        return self

    @classmethod
    def retrieve(
        cls,
        message: str,
        contents: list[Content],
        message_obj: Message | None = None,
    ) -> list[Text]:
        """
        Retrieve a list of text blocks from a message.

        :param message: The message to retrieve the text contents from
        :param message_obj: The message object
        :param contents: The list of the retrieved contents
        :return: A list of text blocks
        """
        text = []

        if len(contents) == 0 and message != "":
            text.append(Text().deserialize(0, len(message), message_obj, message))
            return text
        else:
            for i, block in enumerate(contents):
                if i == 0 and 0 < block.start_position:
                    text.append(
                        Text().deserialize(0, block.start_position, message_obj, message[: block.start_position])
                    )

                if 0 < i < len(contents) and block.start_position > contents[i - 1].end_position:
                    previous_block = contents[i - 1]
                    text.append(
                        Text().deserialize(
                            previous_block.end_position,
                            block.start_position,
                            message_obj,
                            message[previous_block.end_position : block.start_position],
                        )
                    )

                if i == len(contents) - 1 and block.end_position < len(message):
                    text.append(
                        Text().deserialize(block.end_position, len(message), message_obj, message[block.end_position :])
                    )

        return text


class Link(Content):
    """
    This class represents a link in a message.
    """

    def __init__(self):
        super().__init__()
        self._url: str | None = None

    def __str__(self, content=None):
        return super.__str__(self._url)

    def _get_url(self) -> str:
        """
        :type: str
        """
        assert self._url is not None
        return self._url

    def _set_url(self, url: str) -> None:
        """
        Set the URL of the link.

        :param url: The URL of the link
        """
        self._url = url

    url = builtins.property(_get_url, _set_url)

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: Message | None = None,
        value: str | None = None,
    ) -> Link:
        """
        Deserialize a link into a Link object.

        :param start_position: The start position of the link in the message
        :param end_position: The end position of the link in the message
        :param message: The message object
        :param value: The URL of the link
        :return: The deserialized link
        """
        super().deserialize(start_position, end_position, message, value)
        self._url = value

        return self

    @classmethod
    def retrieve(
        cls,
        message: str,
        message_obj: Message | None = None,
    ) -> tuple[list[Link], str]:
        """
        Retrieve the list of links from a message text.

        :param message: The message text
        :param message_obj: The message object
        :return: The list of links
        """
        links = []
        link_regex = re.compile(
            r"<?(https?://)(www\.)?([-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b)(["
            r"-a-zA-Z0-9()@:%_+.~#?&/=]*)>?"
        )

        for link in link_regex.finditer(message):
            link_text = "".join([i for i in link.groups() if i])
            links.append(Link().deserialize(link.start(), link.end(), message_obj, link_text))

            pattern = f"<{link_text}>" if (f"<{link_text}>" in message) else f"{link_text}"
            message = message.replace(pattern, f"__LINK__", 1)

        return links, message


class Code(Content):
    """
    This class represents a code block in a message.
    """

    def __init__(self):
        super().__init__()
        self._code: str | None = None

    def __str__(self, content=None):
        return super.__str__(self._code)

    def _get_code(self) -> str:
        """
        :type: str
        """
        assert self._code is not None
        return self._code

    def _set_code(self, code: str) -> None:
        """
        Set the code of the content.

        :param code: The code of the content
        """
        self._code = code

    code = builtins.property(_get_code, _set_code)

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: Message | None = None,
        value: str | None = None,
    ) -> Code:
        """
        Deserialize a code block into a Code object.

        :param start_position: The start position of the code block in the message
        :param end_position: The end position of the code block in the message
        :param message: The message object
        :param value: The code of the code block
        :return: The deserialized code block
        """
        super().deserialize(start_position, end_position, message, value)
        self._code = value

        return self

    @classmethod
    def retrieve(
        cls,
        message: str,
        message_obj: Message | None = None,
    ) -> tuple[list[Code], str]:
        """
        Retrieve the list of code blocks in a message.

        :param message: The message to retrieve the code blocks from
        :param message_obj: The message object
        :return: The list of code blocks in the message
        """
        code_block_list = []
        code_regex = re.compile(r"(`{3}[\s\S]*?`{3})|(`[\s\S]*?`)")

        for code in code_regex.finditer(message):
            code_text = "".join([i for i in code.groups() if i])
            code_block_list.append(Code().deserialize(code.start(), code.end(), message_obj, code_text))
            message = message.replace(code_text, f"__CODEBLOCK__", 1)

        return code_block_list, message


class Multimedia(Content):
    """
    This class represents a multimedia element in a message.
    """

    def __init__(self):
        super().__init__()
        self._url: str | None = None

    def __str__(self, content=None):
        return super.__str__(self._url)

    def _get_url(self) -> str:
        """
        :type: str
        """
        assert self._url is not None
        return self._url

    def _set_url(self, url: str) -> None:
        """
        Set the urls of the multimedia element.

        :param url: The urls of the multimedia element
        """
        self._url = url

    url = builtins.property(_get_url, _set_url)

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: Message | None = None,
        value: str | None = None,
    ) -> Multimedia:
        """
        Deserialize a multimedia urls into a Multimedia object.

        :param start_position: The start position of the multimedia urls in the message
        :param end_position: The end position of the multimedia urls in the message
        :param message: The message object
        :param value: The urls of the multimedia element
        :return: The Multimedia object
        """
        super().deserialize(start_position, end_position, message, value)
        self._url = value

        return self

    @classmethod
    def retrieve(
        cls,
        message: str,
        message_obj: Message | None = None,
    ) -> tuple[list[Multimedia], str]:
        """
        Retrieve the list of multimedia elements in a message.

        :param message: The message to retrieve the multimedia elements from
        :param message_obj: The message object
        :return: The list of multimedia elements in the message
        """
        multimedia_list = []
        video_regex = re.compile(
            r"<?(https?://)(player.|www.)?(vimeo\.com|youtu("
            r"?:be\.com|\.be|be\.googleapis\.com))(/)(video/|embed/|watch\?v=|v/)?(["
            r"A-Za-z0-9._%-]+)(&\S+)?>?"
        )

        for video in video_regex.finditer(message):
            video_url = "".join([i for i in video.groups() if i])
            multimedia_list.append(Multimedia().deserialize(video.start(), video.end(), message_obj, video_url))

            pattern = f"<{video_url}>" if (f"<{video_url}>" in message) else f"{video_url}"
            message = message.replace(pattern, f"__VIDEO__", 1)

        multimedia_regex = re.compile(
            r"<?(https?://)([-a-zA-Z0-9()@:%_+.~#?&/=]*)(\.)("
            r"jpg|jpeg|JPG|JPEG|gif|gifv|png|PNG|webm|mp4|mp3|wav|ogg)>?"
        )

        for multimedia in multimedia_regex.finditer(message):
            multimedia_url = "".join([i for i in multimedia.groups() if i])
            multimedia_list.append(
                Multimedia().deserialize(multimedia.start(), multimedia.end(), message_obj, multimedia_url)
            )

            pattern = f"<{multimedia_url}>" if (f"<{multimedia_url}>" in message) else f"{multimedia_url}"
            message = message.replace(pattern, f"__MULTIMEDIA__", 1)

        return multimedia_list, message


class Emoji(Content):
    """
    This class represents an emoji in a message.
    """

    def __init__(self):
        super().__init__()
        self._unicode: str | None = None

    def __str__(self, content=None):
        return super.__str__(self._unicode)

    def _get_unicode(self) -> str:
        """
        :type: str
        """
        assert self._unicode is not None
        return self._unicode

    def _set_unicode(self, unicode: str) -> None:
        """
        Set the unicode of the emoji.

        :param unicode: The unicode of the emoji
        """
        self._unicode = unicode

    unicode = builtins.property(_get_unicode, _set_unicode)

    def deserialize(
        self,
        start_position: int,
        end_position: int,
        message: Message | None = None,
        value: str | None = None,
    ) -> Emoji:
        """
        Deserialize a unicode string into an Emoji object.

        :param start_position: The start position of the unicode in the message
        :param end_position: The end position of the unicode in the message
        :param message: The message object
        :param value: The unicode of the emoji
        :return: The Emoji object
        """
        super().deserialize(start_position, end_position, message, value)
        self._unicode = value

        return self

    @classmethod
    def retrieve(
        cls,
        message: str,
        message_obj: Message | None = None,
    ) -> tuple[list[Emoji], str]:
        """
        Retrieve the list of unicode strings in a message.

        :param message: The message to retrieve the unicode strings from
        :param message_obj: The message object
        :return: The list of Emoji objects in the message
        """
        emoji_list = []
        emoji_regex = emoji.get_emoji_regexp()

        for emoji_char in emoji_regex.finditer(message):
            emoji_unicode = "".join([i for i in emoji_char.groups() if i])
            emoji_list.append(Emoji().deserialize(emoji_char.start(), emoji_char.end(), message_obj, emoji_unicode))

            message = message.replace(emoji_unicode, f"__EMOJI__", 1)

        for emoji_char in re.finditer(r":([^\s:]+):", message):
            emoji_unicode = emoji_char.group(0)
            emoji_list.append(Emoji().deserialize(emoji_char.start(), emoji_char.end(), message_obj, emoji_unicode))

            message = message.replace(emoji_char.group(0), f"__EMOJI__", 1)

        return emoji_list, message
