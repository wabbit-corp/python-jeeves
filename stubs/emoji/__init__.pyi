from re import Pattern

def get_emoji_regexp() -> Pattern[str]: ...
def emojize(
    string: str,
    *,
    delimiters: tuple[str, str] = ...,
    language: str | None = ...,
    variant: str | None = ...,
    version: str | None = ...,
    handle_version: str | None = ...,
) -> str: ...
