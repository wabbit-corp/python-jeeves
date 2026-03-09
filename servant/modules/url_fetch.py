from __future__ import annotations

import asyncio
import importlib
import ipaddress
import json
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import aiohttp

from servant.defs import GlobalContext, ToolDef
from typed_json import (
    JSON,
    JSONDict,
    coerce_bool,
    coerce_int,
    coerce_str,
    obj_to_json,
    require_obj,
)

_LOGGER = logging.getLogger(__name__)

MODULE_PROMPT = """
## URL Fetch
Use `fetch_url` to fetch live JSON/text from a public http(s) URL.
Before calling it, make sure the link itself does not look malicious or suspicious.
Use `kind="json"` for simple API GETs and `kind="page"` for ordinary web pages.
Short content is returned inline; larger content returns a handle.
Use `fetch_url_handle_read` or `fetch_url_handle_grep` to inspect handle content.
"""

_CONFIG_BLOCKLIST_KEYS = (
    "url_fetch_blocked_hosts",
    "url_fetch.blacklist",
    "web.fetch.blocked_hosts",
)
_CONFIG_FETCH_USER_AGENT_KEYS = (
    "fetch_user_agent",
    "url_fetch.user_agent",
    "web.fetch.user_agent",
)
_DEFAULT_BLOCKED_HOST_PATTERNS: tuple[str, ...] = ()
_DEFAULT_FETCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_DEFAULT_TIMEOUT_SECONDS = 15
_MAX_REDIRECTS = 3
_MAX_DOWNLOAD_BYTES = 1_000_000
_INLINE_CHAR_LIMIT = 4_000
_MAX_STORED_CHARS = 250_000
_MAX_STORED_HANDLES = 32
_DEFAULT_READ_LINE_WINDOW = 80
_MAX_READ_LINES = 250
_DEFAULT_READ_CHAR_WINDOW = 4_000
_MAX_READ_CHARS = 8_000
_DEFAULT_GREP_MAX_MATCHES = 20
_MAX_GREP_MATCHES = 100
_MAX_RETURNED_LINE_LENGTH = 20_000


class _CrawlerInstance(Protocol):
    async def arun(self, *, url: str, config: object) -> object: ...


class _CrawlerContextManager(Protocol):
    async def __aenter__(self) -> _CrawlerInstance: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class _AsyncCrawlerFactory(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> _CrawlerContextManager: ...


class _ConfigFactory(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


class _CacheModeLike(Protocol):
    BYPASS: object


@dataclass(frozen=True)
class _ResolvedUrl:
    url: str
    host: str
    status_code: int
    content_type: str | None


@dataclass(frozen=True)
class _FetchedContent:
    url: str
    content_kind: str
    fetcher: str
    text: str
    status_code: int | None
    content_type: str | None
    json_value: JSON | None = None
    truncated: bool = False


@dataclass
class _StoredHandle:
    handle: str
    url: str
    content_kind: str
    fetcher: str
    text: str
    status_code: int | None
    content_type: str | None
    truncated: bool
    created_at: float = field(default_factory=time.time)


@dataclass
class ModuleState:
    handles: dict[str, _StoredHandle] = field(default_factory=dict)


def _module_state(ctx: GlobalContext) -> ModuleState:
    state = ctx.module_state.get("url_fetch")
    if isinstance(state, ModuleState):
        return state
    state = ModuleState()
    ctx.module_state["url_fetch"] = state
    return state


def _get_user_agent(ctx: GlobalContext) -> str:
    for key in _CONFIG_FETCH_USER_AGENT_KEYS:
        raw = ctx.config.get(key)
        if raw is None:
            continue
        try:
            return coerce_str(raw, field=key, allow_empty=False)
        except Exception:
            continue
    return _DEFAULT_FETCH_USER_AGENT


def _normalize_host(value: str) -> str:
    return value.strip().rstrip(".").lower()


def _iter_blocklist_patterns(ctx: GlobalContext) -> list[str]:
    for key in _CONFIG_BLOCKLIST_KEYS:
        raw = ctx.config.get(key)
        if isinstance(raw, list):
            patterns = [_normalize_host(str(item)) for item in raw if str(item).strip()]
            if patterns:
                return patterns
        if isinstance(raw, str):
            parts = [_normalize_host(part) for part in re.split(r"[\s,]+", raw) if part.strip()]
            if parts:
                return parts
    return list(_DEFAULT_BLOCKED_HOST_PATTERNS)


def _host_matches_pattern(host: str, pattern: str) -> bool:
    normalized_host = _normalize_host(host)
    normalized_pattern = _normalize_host(pattern)
    if not normalized_pattern:
        return False
    if normalized_pattern.startswith("*."):
        suffix = normalized_pattern[2:]
        return normalized_host == suffix or normalized_host.endswith("." + suffix)
    return normalized_host == normalized_pattern


def _is_blocklisted_host(host: str, patterns: list[str]) -> bool:
    return any(_host_matches_pattern(host, pattern) for pattern in patterns)


def _is_forbidden_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def _ensure_host_resolves_public(host: str) -> None:
    try:
        direct_ip = ipaddress.ip_address(host)
    except ValueError:
        direct_ip = None

    if direct_ip is not None:
        if _is_forbidden_ip(direct_ip):
            raise ValueError(f"Host {host!r} resolves to a forbidden address.")
        return

    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    if not infos:
        raise ValueError(f"Host {host!r} did not resolve to any address.")
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        addr_text = sockaddr[0]
        address = ipaddress.ip_address(addr_text)
        if _is_forbidden_ip(address):
            raise ValueError(f"Host {host!r} resolves to a forbidden address.")


async def _validate_url(url: str, *, blocklist: list[str]) -> tuple[str, str]:
    parsed = urlparse(coerce_str(url, field="url", allow_empty=False))
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url must use http or https.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url has an invalid port.") from exc
    if port is not None and (port <= 0 or port > 65535):
        raise ValueError("url has an invalid port.")
    host = parsed.hostname
    if not host:
        raise ValueError("url must include a hostname.")
    normalized_host = _normalize_host(host)
    if _is_blocklisted_host(normalized_host, blocklist):
        raise ValueError(f"Host {normalized_host!r} is blocked by the fetch blacklist.")
    await _ensure_host_resolves_public(normalized_host)
    sanitized = parsed._replace(fragment="").geturl()
    return sanitized, normalized_host


def _make_client_session(*, timeout_seconds: int, headers: dict[str, str]) -> aiohttp.ClientSession:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    return aiohttp.ClientSession(timeout=timeout, headers=headers)


async def _resolve_final_url(
    url: str,
    *,
    blocklist: list[str],
    headers: dict[str, str],
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> _ResolvedUrl:
    current_url = url
    async with _make_client_session(timeout_seconds=timeout_seconds, headers=headers) as session:
        for _ in range(_MAX_REDIRECTS + 1):
            validated_url, host = await _validate_url(current_url, blocklist=blocklist)
            async with session.get(validated_url, allow_redirects=False) as response:
                response_url = str(response.url)
                if 300 <= response.status < 400:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError(f"Redirect from {response_url} did not include a Location header.")
                    current_url = urljoin(response_url, location)
                    continue
                return _ResolvedUrl(
                    url=response_url,
                    host=host,
                    status_code=response.status,
                    content_type=response.headers.get("Content-Type"),
                )
    raise ValueError(f"Too many redirects while resolving {url!r}.")


def _coerce_kind(value: object | None) -> str:
    kind = coerce_str(value or "auto", field="kind", allow_empty=False).lower()
    if kind not in {"auto", "json", "text", "page"}:
        raise ValueError("kind must be one of: auto, json, text, page.")
    return kind


def _looks_like_json_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    lowered = content_type.lower()
    return "application/json" in lowered or lowered.endswith("+json") or "+json;" in lowered


def _looks_like_plain_text_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    lowered = content_type.lower()
    if "text/html" in lowered or "application/xhtml+xml" in lowered:
        return False
    if lowered.startswith("text/"):
        return True
    return any(
        marker in lowered
        for marker in (
            "application/xml",
            "text/xml",
            "application/yaml",
            "application/x-yaml",
            "application/javascript",
            "application/x-ndjson",
            "application/csv",
        )
    )


def _looks_like_api_url(url: str) -> bool:
    parsed = urlparse(url)
    host = _normalize_host(parsed.hostname or "")
    path = parsed.path.lower()
    query = parsed.query.lower()
    return host.startswith("api.") or "/api/" in path or path.endswith(".json") or "format=json" in query


def _choose_fetch_kind(requested_kind: str, resolved: _ResolvedUrl) -> str:
    if requested_kind != "auto":
        return requested_kind
    if _looks_like_json_content_type(resolved.content_type):
        return "json"
    if _looks_like_plain_text_content_type(resolved.content_type):
        return "text"
    if _looks_like_api_url(resolved.url):
        return "json"
    return "page"


async def _read_response_text(response: aiohttp.ClientResponse) -> str:
    total_bytes = 0
    chunks: list[bytes] = []
    async for chunk in response.content.iter_chunked(8192):
        total_bytes += len(chunk)
        if total_bytes > _MAX_DOWNLOAD_BYTES:
            raise ValueError(f"Response exceeded {_MAX_DOWNLOAD_BYTES} bytes.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    encoding = response.charset
    if encoding is None:
        try:
            encoding = response.get_encoding()
        except Exception:
            encoding = "utf-8"
    return raw.decode(encoding or "utf-8", errors="replace")


async def _fetch_via_aiohttp(
    url: str,
    *,
    content_kind: str,
    headers: dict[str, str],
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> _FetchedContent:
    async with _make_client_session(timeout_seconds=timeout_seconds, headers=headers) as session:
        async with session.get(url, allow_redirects=False) as response:
            if 300 <= response.status < 400:
                raise ValueError("Unexpected redirect after preflight validation.")
            if response.status >= 400:
                raise ValueError(f"HTTP {response.status} while fetching {url}.")
            status_code = response.status
            content_type = response.headers.get("Content-Type")
            body = await _read_response_text(response)

    if content_kind == "json":
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError("Response body was not valid JSON.") from exc
        json_value = obj_to_json(parsed)
        if not isinstance(json_value, (dict, list, str, int, float, bool)) and json_value is not None:
            raise ValueError("Parsed JSON could not be normalized.")
        pretty = json.dumps(json_value, ensure_ascii=False, indent=2, sort_keys=True)
        return _FetchedContent(
            url=url,
            content_kind="json",
            fetcher="aiohttp",
            text=pretty,
            status_code=status_code,
            content_type=content_type,
            json_value=json_value,
        )

    if not _looks_like_plain_text_content_type(content_type):
        raise ValueError("Response content type is not allowed for text mode.")
    return _FetchedContent(
        url=url,
        content_kind="text",
        fetcher="aiohttp",
        text=body,
        status_code=status_code,
        content_type=content_type,
    )


def _load_crawl4ai() -> tuple[_AsyncCrawlerFactory, _ConfigFactory, _ConfigFactory, _CacheModeLike]:
    try:
        module = importlib.import_module("crawl4ai")
    except Exception as exc:
        raise RuntimeError("crawl4ai is required for page fetching.") from exc
    return (
        module.AsyncWebCrawler,
        module.BrowserConfig,
        module.CrawlerRunConfig,
        module.CacheMode,
    )


def _header_lookup(headers: object, name: str) -> str | None:
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return str(value)
    return None


def _choose_page_text(result: object) -> str:
    markdown = getattr(result, "markdown", None)
    if markdown is not None:
        fit_markdown = getattr(markdown, "fit_markdown", None)
        if isinstance(fit_markdown, str) and fit_markdown.strip():
            return fit_markdown
        raw_markdown = getattr(markdown, "raw_markdown", None)
        if isinstance(raw_markdown, str) and raw_markdown.strip():
            return raw_markdown
        rendered_markdown = str(markdown)
        if rendered_markdown.strip():
            return rendered_markdown
    extracted = getattr(result, "extracted_content", None)
    if isinstance(extracted, str) and extracted.strip():
        return extracted
    cleaned_html = getattr(result, "cleaned_html", None)
    if isinstance(cleaned_html, str) and cleaned_html.strip():
        return cleaned_html
    html = getattr(result, "html", None)
    if isinstance(html, str) and html.strip():
        return html
    raise ValueError("crawl4ai returned no readable page content.")


async def _fetch_via_crawl4ai(
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> _FetchedContent:
    AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode = _load_crawl4ai()
    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
        text_mode=True,
        light_mode=True,
        user_agent=headers.get("User-Agent", ""),
        headers=headers,
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=timeout_seconds * 1000,
        wait_until="domcontentloaded",
        verbose=False,
        log_console=False,
    )
    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)

    if not bool(getattr(result, "success", False)):
        error_message = getattr(result, "error_message", None)
        raise ValueError(f"crawl4ai failed to fetch page: {error_message or 'unknown error'}")

    redirected_url = getattr(result, "redirected_url", None)
    final_url = redirected_url if isinstance(redirected_url, str) and redirected_url else url
    status_code = getattr(result, "status_code", None)
    response_headers = getattr(result, "response_headers", None)
    content_type = _header_lookup(response_headers, "Content-Type")
    return _FetchedContent(
        url=final_url,
        content_kind="page",
        fetcher="crawl4ai",
        text=_choose_page_text(result),
        status_code=int(status_code) if isinstance(status_code, int) else None,
        content_type=content_type,
    )


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _preview_text(text: str, *, limit: int = 300) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "..."


def _normalize_for_storage(fetched: _FetchedContent) -> _FetchedContent:
    if len(fetched.text) <= _MAX_STORED_CHARS:
        return fetched
    return _FetchedContent(
        url=fetched.url,
        content_kind=fetched.content_kind,
        fetcher=fetched.fetcher,
        text=fetched.text[:_MAX_STORED_CHARS],
        status_code=fetched.status_code,
        content_type=fetched.content_type,
        json_value=fetched.json_value if fetched.content_kind == "json" else None,
        truncated=True,
    )


def _store_handle(ctx: GlobalContext, fetched: _FetchedContent) -> _StoredHandle:
    state = _module_state(ctx)
    handle = _StoredHandle(
        handle=f"urlfetch_{uuid4().hex[:12]}",
        url=fetched.url,
        content_kind=fetched.content_kind,
        fetcher=fetched.fetcher,
        text=fetched.text,
        status_code=fetched.status_code,
        content_type=fetched.content_type,
        truncated=fetched.truncated,
    )
    state.handles[handle.handle] = handle
    while len(state.handles) > _MAX_STORED_HANDLES:
        oldest_handle = min(state.handles.values(), key=lambda item: item.created_at).handle
        del state.handles[oldest_handle]
    return handle


def _require_handle(ctx: GlobalContext, handle: str) -> _StoredHandle:
    state = _module_state(ctx)
    stored = state.handles.get(handle)
    if stored is None:
        raise ValueError(f"Unknown fetch handle: {handle}")
    return stored


def _serialize_fetch_result(fetched: _FetchedContent, *, inline: bool, handle: str | None = None) -> JSONDict:
    result: JSONDict = {
        "url": fetched.url,
        "content_kind": fetched.content_kind,
        "fetcher": fetched.fetcher,
        "status_code": fetched.status_code,
        "content_type": fetched.content_type,
        "inline": inline,
        "chars": len(fetched.text),
        "lines": _line_count(fetched.text),
        "truncated": fetched.truncated,
    }
    if inline:
        if fetched.content_kind == "json" and fetched.json_value is not None:
            result["json"] = fetched.json_value
        result["text"] = fetched.text
        return result
    assert handle is not None
    result["handle"] = handle
    result["preview"] = _preview_text(fetched.text)
    result["hint"] = "Use fetch_url_handle_read or fetch_url_handle_grep with this handle."
    return result


async def fetch_url(
    ctx: GlobalContext,
    url: str,
    *,
    kind: str = "auto",
) -> JSONDict:
    requested_kind = _coerce_kind(kind)
    blocklist = _iter_blocklist_patterns(ctx)
    headers = {"User-Agent": _get_user_agent(ctx)}
    resolved = await _resolve_final_url(url, blocklist=blocklist, headers=headers)
    fetch_kind = _choose_fetch_kind(requested_kind, resolved)

    if fetch_kind in {"json", "text"}:
        fetched = await _fetch_via_aiohttp(resolved.url, content_kind=fetch_kind, headers=headers)
    else:
        fetched = await _fetch_via_crawl4ai(resolved.url, headers=headers)
        await _validate_url(fetched.url, blocklist=blocklist)

    normalized = _normalize_for_storage(fetched)
    if len(normalized.text) <= _INLINE_CHAR_LIMIT:
        return _serialize_fetch_result(normalized, inline=True)
    stored = _store_handle(ctx, normalized)
    return _serialize_fetch_result(normalized, inline=False, handle=stored.handle)


def _clip_line(line: str) -> str:
    if len(line) <= _MAX_RETURNED_LINE_LENGTH:
        return line
    return line[:_MAX_RETURNED_LINE_LENGTH] + "..."


async def fetch_url_handle_read(
    ctx: GlobalContext,
    handle: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    start_char: int | None = None,
    end_char: int | None = None,
) -> JSONDict:
    stored = _require_handle(ctx, handle)
    if (start_line is not None or end_line is not None) and (start_char is not None or end_char is not None):
        raise ValueError("Specify either line bounds or character bounds, not both.")

    result: JSONDict = {
        "handle": stored.handle,
        "url": stored.url,
        "content_kind": stored.content_kind,
        "fetcher": stored.fetcher,
        "status_code": stored.status_code,
        "content_type": stored.content_type,
        "total_chars": len(stored.text),
        "total_lines": _line_count(stored.text),
        "truncated": stored.truncated,
    }

    if start_char is not None or end_char is not None:
        first_char = max(0, coerce_int(start_char, 0))
        last_char = coerce_int(end_char, first_char + _DEFAULT_READ_CHAR_WINDOW)
        if last_char < first_char:
            raise ValueError("end_char must be >= start_char.")
        if last_char - first_char > _MAX_READ_CHARS:
            raise ValueError(f"Requested too many characters (max {_MAX_READ_CHARS}).")
        result["mode"] = "chars"
        result["start_char"] = first_char
        result["end_char"] = last_char
        result["text"] = stored.text[first_char:last_char]
        return result

    first_line = max(1, coerce_int(start_line, 1))
    last_line = coerce_int(end_line, first_line + _DEFAULT_READ_LINE_WINDOW - 1)
    if last_line < first_line:
        raise ValueError("end_line must be >= start_line.")
    if last_line - first_line + 1 > _MAX_READ_LINES:
        raise ValueError(f"Requested too many lines (max {_MAX_READ_LINES}).")
    lines = stored.text.splitlines()
    line_objects: list[JSONDict] = []
    for number in range(first_line, min(last_line, len(lines)) + 1):
        line_objects.append({"no": number, "text": _clip_line(lines[number - 1])})
    result["mode"] = "lines"
    result["start_line"] = first_line
    result["end_line"] = last_line
    result["lines"] = obj_to_json(line_objects)
    return result


async def fetch_url_handle_grep(
    ctx: GlobalContext,
    handle: str,
    pattern: str,
    *,
    regex: bool = False,
    ignore_case: bool = True,
    whole_word: bool = False,
    max_matches: int = _DEFAULT_GREP_MAX_MATCHES,
) -> JSONDict:
    stored = _require_handle(ctx, handle)
    query = coerce_str(pattern, field="pattern", allow_empty=False)
    requested_matches = max(1, min(coerce_int(max_matches, _DEFAULT_GREP_MAX_MATCHES), _MAX_GREP_MATCHES))

    flags = re.MULTILINE
    if ignore_case:
        flags |= re.IGNORECASE
    pattern_source = query if regex else re.escape(query)
    if whole_word:
        pattern_source = r"\b" + pattern_source + r"\b"
    try:
        compiled = re.compile(pattern_source, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc

    matches: list[JSONDict] = []
    for line_number, line in enumerate(stored.text.splitlines(), start=1):
        for match in compiled.finditer(line):
            matches.append(
                {
                    "line": line_number,
                    "start_char": match.start(),
                    "end_char": match.end(),
                    "text": _clip_line(line),
                }
            )
            if len(matches) >= requested_matches:
                break
        if len(matches) >= requested_matches:
            break

    return {
        "handle": stored.handle,
        "url": stored.url,
        "pattern": query,
        "regex": bool(regex),
        "ignore_case": bool(ignore_case),
        "whole_word": bool(whole_word),
        "max_matches": requested_matches,
        "match_count": len(matches),
        "matches": obj_to_json(matches),
        "truncated": stored.truncated,
    }


async def _fetch_url_tool(ctx: GlobalContext, obj: JSON) -> JSONDict:
    data = require_obj(obj)
    url = coerce_str(data.get("url"), field="url", allow_empty=False)
    kind = _coerce_kind(data.get("kind"))
    return await fetch_url(ctx, url, kind=kind)


async def _fetch_url_handle_read_tool(ctx: GlobalContext, obj: JSON) -> JSONDict:
    data = require_obj(obj)
    handle = coerce_str(data.get("handle"), field="handle", allow_empty=False)
    return await fetch_url_handle_read(
        ctx,
        handle,
        start_line=coerce_int(data.get("start_line"), 1) if data.get("start_line") is not None else None,
        end_line=coerce_int(data.get("end_line"), 0) if data.get("end_line") is not None else None,
        start_char=coerce_int(data.get("start_char"), 0) if data.get("start_char") is not None else None,
        end_char=coerce_int(data.get("end_char"), 0) if data.get("end_char") is not None else None,
    )


async def _fetch_url_handle_grep_tool(ctx: GlobalContext, obj: JSON) -> JSONDict:
    data = require_obj(obj)
    handle = coerce_str(data.get("handle"), field="handle", allow_empty=False)
    pattern = coerce_str(data.get("pattern"), field="pattern", allow_empty=False)
    return await fetch_url_handle_grep(
        ctx,
        handle,
        pattern,
        regex=coerce_bool(data.get("regex"), default=False),
        ignore_case=coerce_bool(data.get("ignore_case"), default=True),
        whole_word=coerce_bool(data.get("whole_word"), default=False),
        max_matches=coerce_int(data.get("max_matches"), _DEFAULT_GREP_MAX_MATCHES),
    )


fetch_url_tool: ToolDef = ToolDef(
    name="fetch_url",
    schema={
        "name": "fetch_url",
        "description": (
            "Fetch JSON/text from a public URL. Before fetching, make sure the link itself does not look malicious "
            "or suspicious. Some hosts may be blocked locally. Use kind='json' for simple API GETs and "
            "kind='page' for ordinary pages."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The http(s) URL to fetch.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["auto", "json", "text", "page"],
                    "default": "auto",
                    "description": "Fetch mode: auto, json, text, or page.",
                },
            },
            "required": ["url"],
        },
    },
    function=_fetch_url_tool,
)


fetch_url_handle_read_tool: ToolDef = ToolDef(
    name="fetch_url_handle_read",
    schema={
        "name": "fetch_url_handle_read",
        "description": "Read specific line ranges or character ranges from a fetch handle returned by fetch_url.",
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Handle returned by fetch_url.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-indexed start line (inclusive). Use with end_line.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-indexed end line (inclusive).",
                },
                "start_char": {
                    "type": "integer",
                    "description": "0-indexed start character (inclusive). Use with end_char instead of line bounds.",
                },
                "end_char": {
                    "type": "integer",
                    "description": "0-indexed end character (exclusive).",
                },
            },
            "required": ["handle"],
        },
    },
    function=_fetch_url_handle_read_tool,
)


fetch_url_handle_grep_tool: ToolDef = ToolDef(
    name="fetch_url_handle_grep",
    schema={
        "name": "fetch_url_handle_grep",
        "description": "Search line-by-line within a fetch handle returned by fetch_url.",
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Handle returned by fetch_url.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Search pattern. Literal unless regex=true.",
                },
                "regex": {
                    "type": "boolean",
                    "default": False,
                    "description": "Treat pattern as a regex.",
                },
                "ignore_case": {
                    "type": "boolean",
                    "default": True,
                    "description": "Case-insensitive matching.",
                },
                "whole_word": {
                    "type": "boolean",
                    "default": False,
                    "description": "Match whole words only.",
                },
                "max_matches": {
                    "type": "integer",
                    "default": _DEFAULT_GREP_MAX_MATCHES,
                    "description": "Maximum matches to return.",
                },
            },
            "required": ["handle", "pattern"],
        },
    },
    function=_fetch_url_handle_grep_tool,
)
