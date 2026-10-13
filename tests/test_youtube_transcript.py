# SPDX-License-Identifier: LicenseRef-Wabbit-Public-Test-License-1.1
from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable
from types import SimpleNamespace

import pytest
from youtube_transcript_api import NoTranscriptFound, YouTubeTranscriptApi

from servant.modules import youtube_transcript


def test_missing_transcript_preserves_the_library_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    api = YouTubeTranscriptApi()
    original = NoTranscriptFound("abcdefghijk", ["en"], [])

    def fetch(*_args: object, **_kwargs: object) -> None:
        raise original

    monkeypatch.setattr(api, "fetch", fetch)
    monkeypatch.setattr(api, "list", lambda _video: [])
    with pytest.raises(NoTranscriptFound) as caught:
        youtube_transcript.fetch_english_transcript(api, "abcdefghijk", False, NoTranscriptFound)
    assert caught.value is original


def test_missing_transcript_returns_a_useful_error_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    api = YouTubeTranscriptApi()
    main_thread = threading.get_ident()

    def fetch(_video: str, languages: Iterable[str], preserve_formatting: bool) -> None:
        assert threading.get_ident() != main_thread
        raise NoTranscriptFound("abcdefghijk", languages, [])

    monkeypatch.setattr(api, "fetch", fetch)
    monkeypatch.setattr(api, "list", lambda _video: [])
    monkeypatch.setattr(youtube_transcript, "_build_api", lambda *_args: api)
    result = asyncio.run(youtube_transcript.get_youtube_transcript({"url": "abcdefghijk"}))
    assert result == {"video_id": "abcdefghijk", "error": "No English or English-translatable transcript is available."}


@pytest.mark.parametrize("english", [True, False])
def test_english_and_translated_transcripts_remain_available(monkeypatch: pytest.MonkeyPatch, english: bool) -> None:
    api = YouTubeTranscriptApi()
    fetched = SimpleNamespace(to_raw_data=lambda: [{"start": 1.0, "duration": 2.0, "text": "Hello"}])

    def fetch(*_args: object, **_kwargs: object) -> object:
        if english:
            return fetched
        raise NoTranscriptFound("abcdefghijk", ["en"], [])

    translated = SimpleNamespace(fetch=lambda **_kwargs: fetched)
    transcript = SimpleNamespace(
        is_generated=False,
        is_translatable=True,
        language_code="fr",
        translate=lambda _language: translated,
        fetch=lambda **_kwargs: fetched,
    )
    monkeypatch.setattr(api, "fetch", fetch)
    monkeypatch.setattr(api, "list", lambda _video: [transcript])
    segments, meta = youtube_transcript.fetch_english_transcript(api, "abcdefghijk", False, NoTranscriptFound)
    assert segments == [{"start": 1.0, "duration": 2.0, "text": "Hello"}]
    assert meta["translated"] is not english
