from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable, Iterable
from typing import Protocol, TYPE_CHECKING, TypeGuard, TypedDict
from urllib.parse import parse_qs, urlparse

from servant.defs import ToolDef
from typed_json import JSON, JSONDict, coerce_float, coerce_optional_str

if TYPE_CHECKING:
    from requests import Session
    from youtube_transcript_api.proxies import ProxyConfig

MODULE_PROMPT = """
## YouTube Transcripts
You can use `get_youtube_transcript` to fetch the English transcript of a YouTube video by URL or ID.
"""

ID_RE = re.compile(r"[0-9A-Za-z_-]{11}")


class _NeverMatch(Exception):
    pass


class TranscriptSegment(TypedDict):
    start: float
    duration: float
    text: str


class TranscriptMeta(TypedDict):
    translated: bool
    origin_lang: str
    origin_type: str | None


class _TranscriptFetchResult(Protocol):
    def to_raw_data(self) -> list[dict[str, object]]: ...


class _TranscriptItem(Protocol):
    is_generated: bool
    is_translatable: bool
    language_code: str

    def translate(self, language: str) -> "_TranscriptItem": ...

    def fetch(self, preserve_formatting: bool = False) -> _TranscriptFetchResult: ...


class _TranscriptApi(Protocol):
    def fetch(
        self,
        video_id: str,
        languages: Iterable[str] = ("en",),
        preserve_formatting: bool = False,
    ) -> _TranscriptFetchResult: ...

    def list(self, video_id: str) -> Iterable[object]: ...


class _TranscriptApiFactory(Protocol):
    def __call__(
        self,
        proxy_config: "ProxyConfig | None" = None,
        http_client: "Session | None" = None,
    ) -> _TranscriptApi: ...


class _WebshareProxyConfigFactory(Protocol):
    def __call__(
        self,
        proxy_username: str,
        proxy_password: str,
        filter_ip_locations: list[str] | None = None,
        retries_when_blocked: int = 10,
        domain_name: str = "p.webshare.io",
        proxy_port: int = 80,
    ) -> "ProxyConfig": ...


class _GenericProxyConfigFactory(Protocol):
    def __call__(
        self,
        http_url: str | None = None,
        https_url: str | None = None,
    ) -> "ProxyConfig": ...


def extract_video_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    if ID_RE.fullmatch(s):
        return s
    p = urlparse(s)
    host = (p.netloc or "").lower()
    if "youtu.be" in host:
        vid = p.path.lstrip("/").split("/")[0]
        if ID_RE.fullmatch(vid):
            return vid
    if "youtube.com" in host:
        if p.path == "/watch":
            v = parse_qs(p.query).get("v", [None])[0]
            if v and ID_RE.fullmatch(v):
                return v
        m = re.match(r"^/(shorts|embed|v)/([0-9A-Za-z_-]{11})", p.path)
        if m:
            return m.group(2)
        v = parse_qs(p.query).get("v", [None])[0]
        if v and ID_RE.fullmatch(v):
            return v
    m = re.search(r"(?<=v=)[0-9A-Za-z_-]{11}", s)
    if m:
        return m.group(0)
    raise ValueError(f"Could not extract a video ID from: {url_or_id!r}")


def _to_srt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1_000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _to_vtt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1_000)
    return f"{h:02}:{m:02}:{s:02}.{ms:03}"


def segments_to_txt(segments: list[TranscriptSegment], with_timestamps: bool) -> str:
    if with_timestamps:
        lines = []
        for seg in segments:
            ts = _to_vtt_timestamp(seg["start"])
            lines.append(f"[{ts}] {seg['text']}".strip())
        return "\n".join(lines) + "\n"
    pieces = [seg["text"].strip() for seg in segments if seg["text"].strip()]
    return " ".join(pieces) + "\n"


def segments_to_srt(segments: list[TranscriptSegment]) -> str:
    out = []
    for i, seg in enumerate(segments, 1):
        start = seg["start"]
        end = seg["start"] + seg.get("duration", 0.0)
        if end <= start:
            end = start + 0.001
        out.append(str(i))
        out.append(f"{_to_srt_timestamp(start)} --> {_to_srt_timestamp(end)}")
        out.append(seg["text"])
        out.append("")
    return "\n".join(out).strip() + "\n"


def segments_to_vtt(segments: list[TranscriptSegment]) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        start = seg["start"]
        end = seg["start"] + seg.get("duration", 0.0)
        if end <= start:
            end = start + 0.001
        lines.append(f"{_to_vtt_timestamp(start)} --> {_to_vtt_timestamp(end)}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def segments_to_csv(segments: list[TranscriptSegment]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["start", "end", "text"])
    for seg in segments:
        start = seg["start"]
        end = seg["start"] + seg.get("duration", 0.0)
        w.writerow([f"{start:.3f}", f"{end:.3f}", seg["text"]])
    return buf.getvalue()


def _coerce_segments(
    raw_segments: list[dict[str, object]],
) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for item in raw_segments:
        text_val = item.get("text")
        if text_val is None:
            continue
        segments.append(
            {
                "start": coerce_float(item.get("start"), 0.0),
                "duration": coerce_float(item.get("duration"), 0.0),
                "text": str(text_val),
            }
        )
    return segments


def _is_transcript_item(item: object) -> TypeGuard[_TranscriptItem]:
    return (
        hasattr(item, "is_generated")
        and hasattr(item, "is_translatable")
        and hasattr(item, "language_code")
        and hasattr(item, "translate")
        and hasattr(item, "fetch")
    )


def _load_yta() -> tuple[object, _TranscriptApiFactory]:
    try:
        import youtube_transcript_api as yta
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception as exc:
        raise RuntimeError(
            "youtube-transcript-api>=1.2.0 is required. " "Install with: pip install -U youtube-transcript-api"
        ) from exc

    def _factory(
        proxy_config: "ProxyConfig | None" = None,
        http_client: "Session | None" = None,
    ) -> _TranscriptApi:
        return YouTubeTranscriptApi(proxy_config=proxy_config, http_client=http_client)

    return yta, _factory


def _load_proxy_types() -> tuple[_WebshareProxyConfigFactory | None, _GenericProxyConfigFactory | None]:
    try:
        from youtube_transcript_api.proxies import (
            WebshareProxyConfig,
            GenericProxyConfig,
        )
    except Exception:
        return None, None
    return WebshareProxyConfig, GenericProxyConfig


def _build_api(YouTubeTranscriptApi: _TranscriptApiFactory, opts: JSONDict) -> _TranscriptApi:
    proxy_config: ProxyConfig | None = None
    WebshareProxyConfig, GenericProxyConfig = _load_proxy_types()
    webshare_user = coerce_optional_str(opts.get("webshare_user"))
    webshare_pass = coerce_optional_str(opts.get("webshare_pass"))
    webshare_locations_raw = opts.get("webshare_locations")
    webshare_locations: list[str] | None
    if isinstance(webshare_locations_raw, list):
        webshare_locations = [str(item) for item in webshare_locations_raw]
    else:
        webshare_locations = None
    if webshare_user and webshare_pass and WebshareProxyConfig:
        proxy_config = WebshareProxyConfig(
            proxy_username=webshare_user,
            proxy_password=webshare_pass,
            filter_ip_locations=webshare_locations or None,
        )
    elif (opts.get("http_proxy") or opts.get("https_proxy") or opts.get("socks_proxy")) and GenericProxyConfig:
        proxy_config = GenericProxyConfig(
            http_url=coerce_optional_str(opts.get("http_proxy")),
            https_url=coerce_optional_str(opts.get("https_proxy")),
        )
    return YouTubeTranscriptApi(proxy_config=proxy_config)


def fetch_english_transcript(
    api: _TranscriptApi,
    video_id: str,
    preserve_formatting: bool,
    no_transcript_exc: type[BaseException],
) -> tuple[list[TranscriptSegment], TranscriptMeta]:
    """
    Try English first; else translate any available transcript to English.
    Returns (segments_raw_list, meta_dict).
    """
    try:
        ft = api.fetch(video_id, languages=["en"], preserve_formatting=preserve_formatting)
        return _coerce_segments(ft.to_raw_data()), {
            "translated": False,
            "origin_lang": "en",
            "origin_type": None,
        }
    except no_transcript_exc:
        pass

    tlist = api.list(video_id)
    items = [t for t in tlist if _is_transcript_item(t)]
    manual = [t for t in items if not t.is_generated and t.is_translatable]
    auto = [t for t in items if t.is_generated and t.is_translatable]
    for t in manual + auto:
        tr = t.translate("en")
        ft = tr.fetch(preserve_formatting=preserve_formatting)
        return _coerce_segments(ft.to_raw_data()), {
            "translated": True,
            "origin_lang": t.language_code,
            "origin_type": "auto" if t.is_generated else "manual",
        }

    raise no_transcript_exc("No transcript translatable to English was found.")


def _expect_dict(value: JSON) -> JSONDict:
    if not isinstance(value, dict):
        raise ValueError("Input must be an object.")
    return value


def _exception_type(yta: object, name: str) -> type[BaseException]:
    exc = getattr(yta, name, _NeverMatch)
    if isinstance(exc, type) and issubclass(exc, BaseException):
        return exc
    return _NeverMatch


async def get_youtube_transcript(opts: JSON) -> JSONDict:
    opts_dict = _expect_dict(opts)
    url = str(opts_dict.get("url") or "")
    fmt = str(opts_dict.get("format") or "txt").lower()
    timestamps = bool(opts_dict.get("timestamps", False))
    preserve_formatting = bool(opts_dict.get("preserve_formatting", False))

    video_id = extract_video_id(url)
    yta, YouTubeTranscriptApi = _load_yta()

    TranscriptsDisabled = _exception_type(yta, "TranscriptsDisabled")
    NoTranscriptFound = _exception_type(yta, "NoTranscriptFound")
    RequestBlocked = _exception_type(yta, "RequestBlocked")
    IpBlocked = _exception_type(yta, "IpBlocked")
    AgeRestricted = _exception_type(yta, "AgeRestricted")
    VideoUnplayable = _exception_type(yta, "VideoUnplayable")
    PoTokenRequired = _exception_type(yta, "PoTokenRequired")

    api = _build_api(YouTubeTranscriptApi, opts_dict)

    try:
        segments, meta = fetch_english_transcript(api, video_id, preserve_formatting, NoTranscriptFound)
    except (TranscriptsDisabled, AgeRestricted) as exc:
        raise RuntimeError("Transcripts are unavailable for this video (disabled or age-restricted).") from exc
    except (RequestBlocked, IpBlocked) as exc:
        raise RuntimeError("YouTube is blocking your IP. Use rotating residential proxies.") from exc
    except (VideoUnplayable,) as exc:
        raise RuntimeError("The video is unplayable.") from exc
    except PoTokenRequired as exc:
        raise RuntimeError("A PO token is required for this transcript (library limitation).") from exc

    if fmt == "txt":
        content = segments_to_txt(segments, with_timestamps=timestamps)
    elif fmt == "srt":
        content = segments_to_srt(segments)
    elif fmt == "vtt":
        content = segments_to_vtt(segments)
    elif fmt == "csv":
        content = segments_to_csv(segments)
    elif fmt == "json":
        content = json.dumps(segments, ensure_ascii=False, indent=2)
    else:
        raise ValueError(f"Unknown format: {fmt}")

    return {
        "video_id": video_id,
        "format": fmt,
        "content": content,
        "translated": meta["translated"],
        "origin_lang": meta["origin_lang"],
        "origin_type": meta["origin_type"],
    }


get_youtube_transcript_tool = ToolDef(
    name="get_youtube_transcript",
    schema={
        "name": "get_youtube_transcript",
        "description": (
            "Fetch the English transcript for a YouTube video by URL or ID. "
            "If English isn't available, translate another language to English."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "YouTube URL or 11-character video ID.",
                },
                "format": {
                    "type": "string",
                    "description": "Output format for the transcript.",
                    "enum": ["txt", "srt", "vtt", "csv", "json"],
                },
                "timestamps": {
                    "type": "boolean",
                    "description": "Include timestamps in TXT output.",
                },
                "preserve_formatting": {
                    "type": "boolean",
                    "description": "Keep HTML tags like <i>/<b> if present.",
                },
                # "webshare_user": {
                #     "type": "string",
                #     "description": "Webshare rotating residential proxy username.",
                # },
                # "webshare_pass": {
                #     "type": "string",
                #     "description": "Webshare rotating residential proxy password.",
                # },
                # "webshare_locations": {
                #     "type": "array",
                #     "items": {"type": "string"},
                #     "description": "Limit Webshare IPs to ISO country codes, e.g. ['de','us'].",
                # },
                # "http_proxy": {
                #     "type": "string",
                #     "description": "Generic HTTP proxy URL.",
                # },
                # "https_proxy": {
                #     "type": "string",
                #     "description": "Generic HTTPS proxy URL.",
                # },
                # "socks_proxy": {
                #     "type": "string",
                #     "description": "Generic SOCKS proxy URL.",
                # },
            },
            "required": ["url"],
        },
    },
    function=lambda _ctx, obj: get_youtube_transcript(obj),
)
