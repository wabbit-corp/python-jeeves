import codecs
from collections.abc import Mapping
from typing import IO

from django.conf import settings
from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser


class PlainParser(BaseParser):
    media_type = "text/plain"

    def parse(
        self,
        stream: IO[bytes],
        media_type: str | None = None,
        parser_context: Mapping[str, object] | None = None,
    ) -> dict[str, str]:
        """
        Parses the incoming bytestream as Plain Text and returns the resulting data.
        """
        parser_context = parser_context or {}
        encoding_value = parser_context.get("encoding", settings.DEFAULT_CHARSET)
        encoding = encoding_value if isinstance(encoding_value, str) else settings.DEFAULT_CHARSET

        try:
            decoded_stream = codecs.getreader(encoding)(stream)
            text_content = decoded_stream.read()
            return {"text": text_content}
        except ValueError as exc:
            raise ParseError(f"Plain text parse error - {exc}") from exc
