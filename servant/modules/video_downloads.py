from __future__ import annotations

import asyncio
import importlib
import json
import logging
import mimetypes
import secrets
import sqlite3
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeGuard

import aiohttp
import discord

from servant.defs import (
    SECRET_GOOGLE_DRIVE_CLIENT_ID,
    SECRET_GOOGLE_DRIVE_CLIENT_SECRET,
    SECRET_GOOGLE_DRIVE_REDIRECT_URI,
    GlobalContext,
    ToolDef,
)
from typed_json import JSON, JSONDict, coerce_bool, coerce_optional_str, coerce_str, obj_to_json, require_obj

_LOGGER = logging.getLogger(__name__)

MODULE_PROMPT = """
## Video Downloads
Use `download_video_to_drive` to download a video URL with yt-dlp, upload it to the authorized recipient's Google Drive,
optionally attach it in Discord if it fits, and remove the local file after a successful Drive upload.
If Google Drive authorization is missing, start the flow and then use `complete_google_drive_auth` with the returned state
and the full redirect URL after the recipient authorizes access.
"""

_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_DRIVE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_DRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
_VOX_DOWNLOADS_FOLDER = "VoxDownloads"
_DEFAULT_DISCORD_UPLOAD_LIMIT_BYTES = 8 * 1024 * 1024
_AUTH_STATE_TTL_SECONDS = 3600


@dataclass(frozen=True)
class VideoDownloadResult:
    file_path: Path
    title: str
    source_url: str
    extractor: str | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True)
class DriveToken:
    recipient_user_id: str
    access_token: str
    refresh_token: str | None
    expires_at: int | None
    token_type: str
    scope: str
    folder_id: str | None = None


class _YtDlpDownloader(Protocol):
    def __enter__(self) -> "_YtDlpDownloader": ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> object: ...

    def extract_info(self, url: str, download: bool = True) -> object: ...


class _YtDlpModule(Protocol):
    YoutubeDL: Callable[[dict[str, object]], _YtDlpDownloader]


def _db_path(ctx: GlobalContext) -> Path:
    raw = ctx.secrets.get("video_downloads_db_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return Path(".data/video_downloads.sqlite3")


def _download_dir(ctx: GlobalContext) -> Path:
    raw = ctx.secrets.get("video_downloads_dir")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return Path(".data/video_downloads")


def _ensure_db(ctx: GlobalContext) -> None:
    db_path = _db_path(ctx)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS google_drive_tokens (
                recipient_user_id TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                expires_at INTEGER,
                token_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                folder_id TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS google_drive_oauth_states (
                state TEXT PRIMARY KEY,
                recipient_user_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_google_drive_oauth_states_created ON google_drive_oauth_states(created_at)"
        )
        conn.commit()
    finally:
        conn.close()


def _cleanup_expired_states(conn: sqlite3.Connection, now: int) -> None:
    conn.execute(
        "DELETE FROM google_drive_oauth_states WHERE created_at < ?",
        (now - _AUTH_STATE_TTL_SECONDS,),
    )


def _save_token(ctx: GlobalContext, token: DriveToken) -> None:
    _ensure_db(ctx)
    now = int(time.time())
    conn = sqlite3.connect(_db_path(ctx))
    try:
        conn.execute(
            """
            INSERT INTO google_drive_tokens (
                recipient_user_id,
                access_token,
                refresh_token,
                expires_at,
                token_type,
                scope,
                folder_id,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(recipient_user_id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = COALESCE(excluded.refresh_token, google_drive_tokens.refresh_token),
                expires_at = excluded.expires_at,
                token_type = excluded.token_type,
                scope = excluded.scope,
                folder_id = COALESCE(excluded.folder_id, google_drive_tokens.folder_id),
                updated_at = excluded.updated_at
            """,
            (
                token.recipient_user_id,
                token.access_token,
                token.refresh_token,
                token.expires_at,
                token.token_type,
                token.scope,
                token.folder_id,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _load_token(ctx: GlobalContext, recipient_user_id: str) -> DriveToken | None:
    _ensure_db(ctx)
    conn = sqlite3.connect(_db_path(ctx))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT recipient_user_id, access_token, refresh_token, expires_at, token_type, scope, folder_id
            FROM google_drive_tokens
            WHERE recipient_user_id = ?
            """,
            (recipient_user_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return DriveToken(
        recipient_user_id=str(row["recipient_user_id"]),
        access_token=str(row["access_token"]),
        refresh_token=row["refresh_token"] if row["refresh_token"] is None else str(row["refresh_token"]),
        expires_at=row["expires_at"] if row["expires_at"] is None else int(row["expires_at"]),
        token_type=str(row["token_type"]),
        scope=str(row["scope"]),
        folder_id=row["folder_id"] if row["folder_id"] is None else str(row["folder_id"]),
    )


def _save_oauth_state(ctx: GlobalContext, recipient_user_id: str, state: str) -> None:
    _ensure_db(ctx)
    now = int(time.time())
    conn = sqlite3.connect(_db_path(ctx))
    try:
        _cleanup_expired_states(conn, now)
        conn.execute(
            """
            INSERT OR REPLACE INTO google_drive_oauth_states (state, recipient_user_id, created_at)
            VALUES (?, ?, ?)
            """,
            (state, recipient_user_id, now),
        )
        conn.commit()
    finally:
        conn.close()


def _consume_oauth_state(ctx: GlobalContext, recipient_user_id: str, state: str) -> None:
    _ensure_db(ctx)
    now = int(time.time())
    conn = sqlite3.connect(_db_path(ctx))
    try:
        _cleanup_expired_states(conn, now)
        row = conn.execute(
            """
            SELECT recipient_user_id
            FROM google_drive_oauth_states
            WHERE state = ?
            """,
            (state,),
        ).fetchone()
        if row is None:
            raise ValueError("OAuth state is missing or expired.")
        saved_user_id = str(row[0])
        if saved_user_id != recipient_user_id:
            raise ValueError("OAuth state does not belong to that recipient.")
        conn.execute("DELETE FROM google_drive_oauth_states WHERE state = ?", (state,))
        conn.commit()
    finally:
        conn.close()


def _require_google_oauth_config(ctx: GlobalContext) -> tuple[str, str, str]:
    client_id = coerce_str(ctx.secrets.get(SECRET_GOOGLE_DRIVE_CLIENT_ID), field=SECRET_GOOGLE_DRIVE_CLIENT_ID)
    client_secret = coerce_str(
        ctx.secrets.get(SECRET_GOOGLE_DRIVE_CLIENT_SECRET),
        field=SECRET_GOOGLE_DRIVE_CLIENT_SECRET,
    )
    redirect_uri = coerce_str(
        ctx.secrets.get(SECRET_GOOGLE_DRIVE_REDIRECT_URI),
        field=SECRET_GOOGLE_DRIVE_REDIRECT_URI,
    )
    return client_id, client_secret, redirect_uri


def _build_google_auth_url(ctx: GlobalContext, recipient_user_id: str) -> JSONDict:
    client_id, _, redirect_uri = _require_google_oauth_config(ctx)
    state = secrets.token_urlsafe(24)
    _save_oauth_state(ctx, recipient_user_id, state)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _DRIVE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return {
        "authorization_required": True,
        "recipient_user_id": recipient_user_id,
        "authorization_url": f"{_DRIVE_AUTH_URL}?{query}",
        "state": state,
        "instructions": (
            "Open the authorization URL, approve Google Drive access, then pass the full redirected URL "
            "to complete_google_drive_auth with the same state."
        ),
    }


def _extract_code_and_state(authorization_response_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(authorization_response_url)
    params = urllib.parse.parse_qs(parsed.query)
    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]
    if not isinstance(code, str) or not code:
        raise ValueError("The authorization response URL did not contain a code.")
    if not isinstance(state, str) or not state:
        raise ValueError("The authorization response URL did not contain a state.")
    return code, state


async def _post_form_json(url: str, form_data: dict[str, str]) -> JSONDict:
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form_data) as response:
            payload = obj_to_json(await response.json())
            if not isinstance(payload, dict):
                raise RuntimeError("Expected an object response.")
            if response.status >= 400:
                raise RuntimeError(f"Google OAuth request failed: {payload}")
            return payload


async def complete_google_drive_auth(ctx: GlobalContext, obj: JSON) -> JSONDict:
    data = require_obj(obj)
    recipient_user_id = coerce_optional_str(data.get("recipient_user_id")) or ctx.current_user_id
    if recipient_user_id is None:
        raise ValueError("recipient_user_id is required.")

    response_url = coerce_optional_str(data.get("authorization_response_url"))
    code = coerce_optional_str(data.get("code"))
    state = coerce_optional_str(data.get("state"))

    if response_url:
        code_from_url, state_from_url = _extract_code_and_state(response_url)
        code = code or code_from_url
        state = state or state_from_url

    if code is None or state is None:
        raise ValueError("Provide authorization_response_url or both code and state.")

    _consume_oauth_state(ctx, recipient_user_id, state)
    client_id, client_secret, redirect_uri = _require_google_oauth_config(ctx)
    token_payload = await _post_form_json(
        _DRIVE_TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )

    access_token = coerce_str(token_payload.get("access_token"), field="access_token", allow_empty=False)
    refresh_token = coerce_optional_str(token_payload.get("refresh_token"))
    expires_in_raw = token_payload.get("expires_in")
    expires_at: int | None = None
    if isinstance(expires_in_raw, (int, float)):
        expires_at = int(time.time()) + int(expires_in_raw) - 30
    token = DriveToken(
        recipient_user_id=recipient_user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        token_type=coerce_str(token_payload.get("token_type"), field="token_type", default="Bearer"),
        scope=coerce_str(token_payload.get("scope"), field="scope", default=_DRIVE_SCOPE),
        folder_id=None,
    )
    _save_token(ctx, token)
    return {
        "ok": True,
        "recipient_user_id": recipient_user_id,
        "authorized": True,
        "scope": token.scope,
        "has_refresh_token": refresh_token is not None,
    }


async def _authorized_token(ctx: GlobalContext, recipient_user_id: str) -> DriveToken | None:
    token = _load_token(ctx, recipient_user_id)
    if token is None:
        return None
    if token.expires_at is None or token.expires_at > int(time.time()):
        return token
    if not token.refresh_token:
        return None

    client_id, client_secret, _ = _require_google_oauth_config(ctx)
    payload = await _post_form_json(
        _DRIVE_TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": token.refresh_token,
            "grant_type": "refresh_token",
        },
    )
    expires_in_raw = payload.get("expires_in")
    expires_in = int(expires_in_raw) if isinstance(expires_in_raw, (int, float)) else 3600
    refreshed = DriveToken(
        recipient_user_id=recipient_user_id,
        access_token=coerce_str(payload.get("access_token"), field="access_token", allow_empty=False),
        refresh_token=token.refresh_token,
        expires_at=int(time.time()) + expires_in - 30,
        token_type=coerce_str(payload.get("token_type"), field="token_type", default=token.token_type),
        scope=coerce_str(payload.get("scope"), field="scope", default=token.scope),
        folder_id=token.folder_id,
    )
    _save_token(ctx, refreshed)
    return refreshed


async def _drive_request_json(
    method: str,
    url: str,
    *,
    token: DriveToken,
    params: dict[str, str] | None = None,
    json_body: JSONDict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONDict:
    request_headers = {"Authorization": f"Bearer {token.access_token}"}
    if headers:
        request_headers.update(headers)
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, params=params, json=json_body, headers=request_headers) as response:
            payload = obj_to_json(await response.json())
            if not isinstance(payload, dict):
                raise RuntimeError("Expected an object response from Google Drive.")
            if response.status >= 400:
                raise RuntimeError(f"Google Drive request failed: {payload}")
            return payload


async def _ensure_drive_folder(ctx: GlobalContext, token: DriveToken, folder_name: str = _VOX_DOWNLOADS_FOLDER) -> str:
    if token.folder_id:
        return token.folder_id

    escaped_name = folder_name.replace("'", "\\'")
    query = "mimeType='application/vnd.google-apps.folder' " f"and trashed=false and name='{escaped_name}'"
    listing = await _drive_request_json(
        "GET",
        _DRIVE_FILES_URL,
        token=token,
        params={"q": query, "pageSize": "1", "fields": "files(id,name)"},
    )
    files = listing.get("files")
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, dict):
            folder_id = coerce_str(first.get("id"), field="folder_id", allow_empty=False)
            _save_token(ctx, DriveToken(**{**token.__dict__, "folder_id": folder_id}))
            return folder_id

    created = await _drive_request_json(
        "POST",
        _DRIVE_FILES_URL,
        token=token,
        params={"fields": "id,name"},
        json_body={
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        },
    )
    folder_id = coerce_str(created.get("id"), field="folder_id", allow_empty=False)
    _save_token(ctx, DriveToken(**{**token.__dict__, "folder_id": folder_id}))
    return folder_id


def _build_multipart_body(metadata: JSONDict, file_path: Path) -> tuple[bytes, str]:
    boundary = f"vox-upload-{secrets.token_hex(12)}"
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    metadata_json = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    file_bytes = file_path.read_bytes()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("ascii"),
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
            metadata_json,
            b"\r\n",
            f"--{boundary}\r\n".encode("ascii"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return body, f"multipart/related; boundary={boundary}"


async def _upload_file_to_drive(ctx: GlobalContext, token: DriveToken, file_path: Path) -> JSONDict:
    folder_id = await _ensure_drive_folder(ctx, token)
    metadata: JSONDict = {"name": file_path.name, "parents": [folder_id]}
    body, content_type = await asyncio.to_thread(_build_multipart_body, metadata, file_path)
    headers = {
        "Authorization": f"Bearer {token.access_token}",
        "Content-Type": content_type,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            _DRIVE_UPLOAD_URL,
            data=body,
            headers=headers,
            params={"fields": "id,name,webViewLink,webContentLink,size,mimeType"},
        ) as response:
            payload = obj_to_json(await response.json())
            if not isinstance(payload, dict):
                raise RuntimeError("Expected an object response from Google Drive upload.")
            if response.status >= 400:
                raise RuntimeError(f"Google Drive upload failed: {payload}")
            return payload


def _discord_upload_limit(channel: object) -> int:
    guild = getattr(channel, "guild", None)
    limit = getattr(guild, "filesize_limit", None)
    if isinstance(limit, int) and limit > 0:
        return limit
    return _DEFAULT_DISCORD_UPLOAD_LIMIT_BYTES


def _is_messageable_channel(channel: object) -> TypeGuard[discord.abc.Messageable]:
    return callable(getattr(channel, "send", None))


async def _send_file_to_discord(ctx: GlobalContext, channel_id: str, file_path: Path) -> bool:
    client = ctx.discord_client
    loop = ctx.discord_loop
    if client is None or loop is None:
        raise RuntimeError("Discord client is not initialized.")

    async def _send() -> bool:
        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        if channel is None or not _is_messageable_channel(channel):
            raise RuntimeError(f"Channel {channel_id} is not messageable.")
        if file_path.stat().st_size > _discord_upload_limit(channel):
            return False
        await channel.send(file=discord.File(str(file_path), filename=file_path.name))
        return True

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is loop:
        return await _send()

    fut = asyncio.run_coroutine_threadsafe(_send(), loop)
    if running is not None:
        return await asyncio.wrap_future(fut)
    return fut.result()


def _is_yt_dlp_module(module: object) -> TypeGuard[_YtDlpModule]:
    return callable(getattr(module, "YoutubeDL", None))


def _load_yt_dlp_module() -> _YtDlpModule:
    try:
        module = importlib.import_module("yt_dlp")
    except Exception as exc:
        raise RuntimeError("yt-dlp is required. Install it into the project environment.") from exc
    if not _is_yt_dlp_module(module):
        raise RuntimeError("yt-dlp module does not expose YoutubeDL.")
    return module


def _resolve_downloaded_file(info: dict[str, object], download_dir: Path) -> Path:
    requested_downloads = info.get("requested_downloads")
    if isinstance(requested_downloads, list):
        for item in requested_downloads:
            if isinstance(item, dict):
                filepath = item.get("filepath")
                if isinstance(filepath, str) and filepath:
                    candidate = Path(filepath)
                    if candidate.exists():
                        return candidate
    filepath = info.get("filepath")
    if isinstance(filepath, str) and filepath:
        candidate = Path(filepath)
        if candidate.exists():
            return candidate

    name = info.get("_filename")
    if isinstance(name, str) and name:
        candidate = Path(name)
        if candidate.exists():
            return candidate

    video_id = info.get("id")
    if isinstance(video_id, str) and video_id:
        matches = sorted(download_dir.glob(f"*{video_id}*"))
        if matches:
            return matches[0]
    raise RuntimeError("yt-dlp did not produce a readable output file path.")


def _download_video_sync(url: str, download_dir: Path) -> VideoDownloadResult:
    yt_dlp = _load_yt_dlp_module()
    download_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(download_dir / "%(title).160B [%(id)s].%(ext)s")
    options = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "restrictfilenames": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned an unexpected result.")
    file_path = _resolve_downloaded_file(info, download_dir)
    title = coerce_str(info.get("title"), field="title", default=file_path.stem)
    source_url = coerce_str(info.get("webpage_url"), field="webpage_url", default=url)
    extractor = coerce_optional_str(info.get("extractor"))
    duration_raw = info.get("duration")
    duration_seconds = int(duration_raw) if isinstance(duration_raw, (int, float)) else None
    return VideoDownloadResult(
        file_path=file_path,
        title=title,
        source_url=source_url,
        extractor=extractor,
        duration_seconds=duration_seconds,
    )


async def _download_video(url: str, download_dir: Path) -> VideoDownloadResult:
    return await asyncio.to_thread(_download_video_sync, url, download_dir)


async def _process_video_transfer(
    ctx: GlobalContext,
    *,
    url: str,
    recipient_user_id: str,
    channel_id: str | None,
    send_to_discord: bool,
    downloader: Callable[[str, Path], Awaitable[VideoDownloadResult]] = _download_video,
    drive_uploader: Callable[[GlobalContext, DriveToken, Path], Awaitable[JSONDict]] = _upload_file_to_drive,
    discord_sender: Callable[[GlobalContext, str, Path], Awaitable[bool]] = _send_file_to_discord,
) -> JSONDict:
    token = await _authorized_token(ctx, recipient_user_id)
    if token is None:
        return _build_google_auth_url(ctx, recipient_user_id)

    download_result = await downloader(url, _download_dir(ctx))
    file_path = download_result.file_path
    file_size = file_path.stat().st_size
    discord_uploaded = False
    drive_payload = await drive_uploader(ctx, token, file_path)

    if send_to_discord and channel_id:
        try:
            discord_uploaded = await discord_sender(ctx, channel_id, file_path)
        except Exception:
            _LOGGER.exception("Discord file upload failed for %s", file_path)

    file_path.unlink(missing_ok=True)
    return {
        "ok": True,
        "recipient_user_id": recipient_user_id,
        "channel_id": channel_id,
        "title": download_result.title,
        "source_url": download_result.source_url,
        "extractor": download_result.extractor,
        "duration_seconds": download_result.duration_seconds,
        "filename": file_path.name,
        "size_bytes": file_size,
        "discord_uploaded": discord_uploaded,
        "drive_file": drive_payload,
    }


async def download_video_to_drive(ctx: GlobalContext, obj: JSON) -> JSONDict:
    data = require_obj(obj)
    url = coerce_str(data.get("url"), field="url", allow_empty=False)
    recipient_user_id = coerce_optional_str(data.get("recipient_user_id")) or ctx.current_user_id
    if recipient_user_id is None:
        raise ValueError("recipient_user_id is required.")
    channel_id = coerce_optional_str(data.get("channel_id")) or ctx.current_channel_id
    send_to_discord = coerce_bool(data.get("send_to_discord"), default=True)
    return await _process_video_transfer(
        ctx,
        url=url,
        recipient_user_id=recipient_user_id,
        channel_id=channel_id,
        send_to_discord=send_to_discord,
    )


download_video_to_drive_tool: ToolDef = ToolDef(
    name="download_video_to_drive",
    function=lambda ctx, obj: download_video_to_drive(ctx, obj),
    schema={
        "name": "download_video_to_drive",
        "description": (
            "Download a video URL with yt-dlp, upload it to the recipient's Google Drive under VoxDownloads, "
            "optionally attach it in the current Discord channel if it fits, and delete the local file after "
            "a successful Drive upload."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The video URL to download."},
                "recipient_user_id": {
                    "type": "string",
                    "description": "Discord user id whose Google Drive should receive the upload. Defaults to the requesting user.",
                },
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel id where the file should be attached if it fits. Defaults to the current channel.",
                },
                "send_to_discord": {
                    "type": "boolean",
                    "description": "Whether to upload the downloaded file to Discord when it fits the file size limit.",
                    "default": True,
                },
            },
            "required": ["url"],
        },
    },
)


complete_google_drive_auth_tool: ToolDef = ToolDef(
    name="complete_google_drive_auth",
    function=lambda ctx, obj: complete_google_drive_auth(ctx, obj),
    schema={
        "name": "complete_google_drive_auth",
        "description": (
            "Finish the Google Drive OAuth flow for a Discord user by exchanging the returned authorization code "
            "or full redirect URL for Drive tokens."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_user_id": {
                    "type": "string",
                    "description": "Discord user id being authorized. Defaults to the requesting user.",
                },
                "authorization_response_url": {
                    "type": "string",
                    "description": "The full redirected URL after Google authorization, including code and state.",
                },
                "code": {"type": "string", "description": "OAuth code, if passing it directly."},
                "state": {"type": "string", "description": "OAuth state value returned by download_video_to_drive."},
            },
        },
    },
)
