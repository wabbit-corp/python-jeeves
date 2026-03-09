from __future__ import annotations

from xml.etree.ElementTree import Element


class ParseError(Exception): ...


def fromstring(text: str | bytes) -> Element: ...
