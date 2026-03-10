from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from servant.defs import (
    SECRET_GOOGLE_DRIVE_CLIENT_ID,
    SECRET_GOOGLE_DRIVE_CLIENT_SECRET,
    SECRET_GOOGLE_DRIVE_REDIRECT_URI,
    GlobalContext,
)
from servant.modules import video_downloads
from typed_json import JSON


def _ctx(tmp_path: Path) -> GlobalContext:
    return GlobalContext(
        secrets={
            "video_downloads_db_path": str(tmp_path / "video_downloads.sqlite3"),
            "video_downloads_dir": str(tmp_path / "downloads"),
            SECRET_GOOGLE_DRIVE_CLIENT_ID: "client-id",
            SECRET_GOOGLE_DRIVE_CLIENT_SECRET: "client-secret",
            SECRET_GOOGLE_DRIVE_REDIRECT_URI: "https://example.com/oauth/callback",
        },
        current_user_id="42",
        current_channel_id="77",
    )


def test_build_google_auth_url_persists_state(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = video_downloads._build_google_auth_url(ctx, "42")

    assert result["authorization_required"] is True
    assert result["recipient_user_id"] == "42"
    assert "authorization_url" in result
    token_state = result["state"]
    assert isinstance(token_state, str)

    db_path = Path(str(ctx.secrets["video_downloads_db_path"]))
    assert db_path.exists()

    # The stored state should be accepted once.
    video_downloads._consume_oauth_state(ctx, "42", token_state)
    with pytest.raises(ValueError):
        video_downloads._consume_oauth_state(ctx, "42", token_state)


def test_complete_google_drive_auth_stores_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    auth_info = video_downloads._build_google_auth_url(ctx, "42")
    state = str(auth_info["state"])

    async def fake_post_form_json(url: str, form_data: dict[str, str]) -> dict[str, object]:
        assert url == video_downloads._DRIVE_TOKEN_URL
        assert form_data["code"] == "abc123"
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": video_downloads._DRIVE_SCOPE,
        }

    monkeypatch.setattr(video_downloads, "_post_form_json", fake_post_form_json)
    result = asyncio.run(
        video_downloads.complete_google_drive_auth(
            ctx,
            {
                "recipient_user_id": "42",
                "authorization_response_url": f"https://example.com/oauth/callback?code=abc123&state={state}",
            },
        )
    )

    assert result["ok"] is True
    token = video_downloads._load_token(ctx, "42")
    assert token is not None
    assert token.access_token == "access-token"
    assert token.refresh_token == "refresh-token"


def test_download_video_to_drive_requests_auth_when_missing_token(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = asyncio.run(video_downloads.download_video_to_drive(ctx, {"url": "https://example.com/video"}))

    assert result["authorization_required"] is True
    assert result["recipient_user_id"] == "42"
    assert "authorization_url" in result


def test_process_video_transfer_uploads_and_cleans_up(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    file_path = tmp_path / "downloads" / "clip.mp4"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"video-bytes")

    video_downloads._save_token(
        ctx,
        video_downloads.DriveToken(
            recipient_user_id="42",
            access_token="token",
            refresh_token="refresh",
            expires_at=None,
            token_type="Bearer",
            scope=video_downloads._DRIVE_SCOPE,
            folder_id="folder-1",
        ),
    )

    async def fake_downloader(url: str, download_dir: Path) -> video_downloads.VideoDownloadResult:
        assert url == "https://example.com/video"
        assert download_dir == Path(str(ctx.secrets["video_downloads_dir"]))
        return video_downloads.VideoDownloadResult(
            file_path=file_path,
            title="Example Video",
            source_url=url,
            extractor="generic",
            duration_seconds=12,
        )

    async def fake_drive_uploader(
        inner_ctx: GlobalContext,
        token: video_downloads.DriveToken,
        inner_file_path: Path,
    ) -> dict[str, JSON]:
        assert inner_ctx is ctx
        assert token.recipient_user_id == "42"
        assert inner_file_path == file_path
        return {"id": "drive-file", "webViewLink": "https://drive.example/file"}

    async def fake_discord_sender(inner_ctx: GlobalContext, channel_id: str, inner_file_path: Path) -> bool:
        assert inner_ctx is ctx
        assert channel_id == "77"
        assert inner_file_path == file_path
        return True

    result = asyncio.run(
        video_downloads._process_video_transfer(
            ctx,
            url="https://example.com/video",
            recipient_user_id="42",
            channel_id="77",
            send_to_discord=True,
            downloader=fake_downloader,
            drive_uploader=fake_drive_uploader,
            discord_sender=fake_discord_sender,
        )
    )

    assert result["ok"] is True
    assert result["discord_uploaded"] is True
    assert result["drive_file"] == {"id": "drive-file", "webViewLink": "https://drive.example/file"}
    assert not file_path.exists()
