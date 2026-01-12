from __future__ import annotations

import csv
import io
import json
import re
from urllib.parse import parse_qs, urlparse

from servant.defs import ToolDef
from servant.json import JSONDict

MODULE_PROMPT = """
## YouTube Transcripts
You can use `get_youtube_transcript` to fetch the English transcript of a YouTube video by URL or ID.
"""

ID_RE = re.compile(r"[0-9A-Za-z_-]{11}")


class _NeverMatch(Exception):
    pass


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


def segments_to_txt(segments, with_timestamps: bool) -> str:
    if with_timestamps:
        lines = []
        for seg in segments:
            ts = _to_vtt_timestamp(seg["start"])
            lines.append(f"[{ts}] {seg['text']}".strip())
        return "\n".join(lines) + "\n"
    pieces = [seg["text"].strip() for seg in segments if seg["text"].strip()]
    return " ".join(pieces) + "\n"


def segments_to_srt(segments) -> str:
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


def segments_to_vtt(segments) -> str:
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


def segments_to_csv(segments) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["start", "end", "text"])
    for seg in segments:
        start = seg["start"]
        end = seg["start"] + seg.get("duration", 0.0)
        w.writerow([f"{start:.3f}", f"{end:.3f}", seg["text"]])
    return buf.getvalue()


def _load_yta():
    try:
        import youtube_transcript_api as yta
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception as exc:
        raise RuntimeError(
            "youtube-transcript-api>=1.2.0 is required. "
            "Install with: pip install -U youtube-transcript-api"
        ) from exc
    return yta, YouTubeTranscriptApi


def _load_proxy_types():
    try:
        from youtube_transcript_api.proxies import WebshareProxyConfig, GenericProxyConfig
    except Exception:
        WebshareProxyConfig = GenericProxyConfig = None
    return WebshareProxyConfig, GenericProxyConfig


def _build_api(YouTubeTranscriptApi, opts: dict) -> object:
    proxy_config = None
    WebshareProxyConfig, GenericProxyConfig = _load_proxy_types()
    webshare_user = opts.get("webshare_user")
    webshare_pass = opts.get("webshare_pass")
    webshare_locations = opts.get("webshare_locations")
    if webshare_user and webshare_pass and WebshareProxyConfig:
        proxy_config = WebshareProxyConfig(
            proxy_username=webshare_user,
            proxy_password=webshare_pass,
            filter_ip_locations=webshare_locations or None,
        )
    elif (
        opts.get("http_proxy") or opts.get("https_proxy") or opts.get("socks_proxy")
    ) and GenericProxyConfig:
        proxy_config = GenericProxyConfig(
            http_url=opts.get("http_proxy") or None,
            https_url=opts.get("https_proxy") or None,
            socks_url=opts.get("socks_proxy") or None,
        )
    return YouTubeTranscriptApi(proxy_config=proxy_config)


def fetch_english_transcript(
    api, video_id: str, preserve_formatting: bool, NoTranscriptFound
):
    """
    Try English first; else translate any available transcript to English.
    Returns (segments_raw_list, meta_dict).
    """
    try:
        ft = api.fetch(
            video_id, languages=["en"], preserve_formatting=preserve_formatting
        )
        return ft.to_raw_data(), {
            "translated": False,
            "origin_lang": "en",
            "origin_type": None,
        }
    except NoTranscriptFound:
        pass

    tlist = api.list(video_id)
    manual = [t for t in tlist if not t.is_generated and t.is_translatable]
    auto = [t for t in tlist if t.is_generated and t.is_translatable]
    for t in manual + auto:
        tr = t.translate("en")
        ft = tr.fetch(preserve_formatting=preserve_formatting)
        return ft.to_raw_data(), {
            "translated": True,
            "origin_lang": t.language_code,
            "origin_type": "auto" if t.is_generated else "manual",
        }

    raise NoTranscriptFound("No transcript translatable to English was found.")


async def get_youtube_transcript(opts: dict) -> JSONDict:
    url = opts.get("url") or ""
    fmt = (opts.get("format") or "txt").lower()
    timestamps = bool(opts.get("timestamps", False))
    preserve_formatting = bool(opts.get("preserve_formatting", False))

    video_id = extract_video_id(url)
    yta, YouTubeTranscriptApi = _load_yta()

    TranscriptsDisabled = getattr(yta, "TranscriptsDisabled", _NeverMatch)
    NoTranscriptFound = getattr(yta, "NoTranscriptFound", _NeverMatch)
    RequestBlocked = getattr(yta, "RequestBlocked", _NeverMatch)
    IpBlocked = getattr(yta, "IpBlocked", _NeverMatch)
    AgeRestricted = getattr(yta, "AgeRestricted", _NeverMatch)
    VideoUnplayable = getattr(yta, "VideoUnplayable", _NeverMatch)
    PoTokenRequired = getattr(yta, "PoTokenRequired", _NeverMatch)

    api = _build_api(YouTubeTranscriptApi, opts)

    try:
        segments, meta = fetch_english_transcript(
            api, video_id, preserve_formatting, NoTranscriptFound
        )
    except (TranscriptsDisabled, AgeRestricted) as exc:
        raise RuntimeError(
            "Transcripts are unavailable for this video (disabled or age-restricted)."
        ) from exc
    except (RequestBlocked, IpBlocked) as exc:
        raise RuntimeError(
            "YouTube is blocking your IP. Use rotating residential proxies."
        ) from exc
    except (VideoUnplayable,) as exc:
        raise RuntimeError("The video is unplayable.") from exc
    except PoTokenRequired as exc:
        raise RuntimeError(
            "A PO token is required for this transcript (library limitation)."
        ) from exc

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
        **meta,
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
