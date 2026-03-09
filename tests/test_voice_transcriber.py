from servant import voice_transcriber


def test_extract_invite_code_matches_variants() -> None:
    assert voice_transcriber.extract_invite_code("https://discord.gg/C6WHG3bR") == "C6WHG3bR"
    assert voice_transcriber.extract_invite_code("http://discord.gg/abcDEF") == "abcDEF"
    assert voice_transcriber.extract_invite_code("discord.gg/xyz-123") == "xyz-123"
    assert voice_transcriber.extract_invite_code("https://discordapp.com/invite/Alpha-42") == "Alpha-42"
    assert voice_transcriber.extract_invite_code("https://discord.com/invite/InviteCode") == "InviteCode"


def test_extract_invite_code_returns_none_when_absent() -> None:
    assert voice_transcriber.extract_invite_code("hello there") is None
    assert voice_transcriber.extract_invite_code("") is None
