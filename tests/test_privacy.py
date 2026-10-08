from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import legacy_conversation_schema
import pytest

from servant import permissions, privacy, voice_transcriber
from servant.defs import GlobalContext, RequestContext
from servant.modules import (
    background_indexer,
    commitment,
    discord_search,
    topic_subscriptions,
)


def _interaction(user_id: int = 42) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = SimpleNamespace(id=user_id)
    interaction.response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock())
    interaction.edit_original_response = AsyncMock()
    return interaction


def _context(tmp_path: Path) -> GlobalContext:
    ctx = GlobalContext()
    ctx.config.update(
        {
            "indexer_db_path": str(tmp_path / "index.sqlite3"),
            "topic_subscriptions_db_path": str(tmp_path / "subscriptions.sqlite3"),
            "commitments_db_path": str(tmp_path / "commitments.sqlite3"),
        }
    )
    return ctx


def test_slash_confirmation_is_private_and_changes_nothing_until_confirmed() -> None:
    async def run() -> None:
        ctx = GlobalContext()
        interaction = _interaction()
        confirm = AsyncMock()
        await privacy.show_confirmation(interaction, ctx, confirm)
        kwargs = interaction.response.send_message.call_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "lose access to all Vox features" in kwargs["embed"].description
        assert isinstance(kwargs["view"], privacy.OptOutConfirmation)
        assert not ctx.privacy.opted_out_users
        confirm.assert_not_awaited()
        view = kwargs["view"]
        assert not await view.interaction_check(_interaction(99))
        assert await view.interaction_check(interaction)
        await view.confirm_button.callback(interaction)
        confirm.assert_awaited_once_with("42")
        assert interaction.edit_original_response.call_args.kwargs["view"] is None
        assert view.is_finished()
        await view.confirm_button.callback(_interaction())
        assert confirm.await_count == 1

    asyncio.run(run())


def test_cancel_and_timeout_do_not_opt_out() -> None:
    async def run() -> None:
        confirm = AsyncMock()
        view = privacy.OptOutConfirmation(user_id=42, confirm=confirm)
        interaction = _interaction()
        await view.cancel_button.callback(interaction)
        await view.confirm_button.callback(_interaction())
        confirm.assert_not_awaited()
        assert interaction.response.edit_message.call_args.kwargs["view"] is None
        origin = _interaction()
        expired = privacy.OptOutConfirmation(user_id=42, confirm=confirm, origin=origin)
        await expired.on_timeout()
        assert origin.edit_original_response.call_args.kwargs["view"] is None
        assert "No opt-out was confirmed" in origin.edit_original_response.call_args.kwargs["embed"].description
        await expired.confirm_button.callback(_interaction())
        confirm.assert_not_awaited()

    asyncio.run(run())


def test_failed_confirmation_does_not_report_success() -> None:
    async def run() -> None:
        confirm = AsyncMock(side_effect=OSError("database unavailable"))
        view = privacy.OptOutConfirmation(user_id=42, confirm=confirm)
        interaction = _interaction()
        await view.confirm_button.callback(interaction)
        assert (
            interaction.edit_original_response.call_args.kwargs["embed"].title == "Confirmation could not be completed"
        )
        assert not view.is_finished()
        assert view.cancel_button.disabled
        await view.cancel_button.callback(interaction)
        interaction.response.edit_message.assert_not_awaited()
        view.stop()

    asyncio.run(run())


def test_already_opted_out_user_gets_private_status_without_an_undo() -> None:
    async def run() -> None:
        ctx = GlobalContext()
        ctx.privacy.opted_out_users.add("42")
        interaction = _interaction()
        confirm = AsyncMock()
        await privacy.show_confirmation(interaction, ctx, confirm)
        kwargs = interaction.response.send_message.call_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "view" not in kwargs
        confirm.assert_not_awaited()

    asyncio.run(run())


def test_opt_out_persists_and_prevents_history_or_partial_edits_restoring_messages(tmp_path: Path) -> None:
    async def run() -> None:
        ctx = _context(tmp_path)
        await background_indexer.init_db(ctx)
        conn = background_indexer._connect(background_indexer._db_path(ctx))
        try:
            conn.execute("INSERT INTO users (user_id, name) VALUES ('42', 'Alice')")
            conn.execute(
                "INSERT INTO messages (message_id, channel_id, author_id, content) VALUES ('100', '10', '42', 'secret')"
            )
            conn.execute(
                "INSERT INTO messages (message_id, channel_id, author_id, content) VALUES ('101', '10', '99', 'keep')"
            )
            conn.execute(
                "INSERT INTO message_attachments (attachment_id, message_id, channel_id, url) VALUES ('1', '100', '10', 'https://example.com/a')"
            )
            conn.execute("INSERT INTO message_embeds (message_id, idx, embed_json) VALUES ('100', 0, '{}')")
            conn.commit()
        finally:
            conn.close()
        ctx.privacy.opted_out_users.add("42")
        await background_indexer.confirm_privacy_opt_out(ctx, "42")
        restored = _context(tmp_path)
        await background_indexer.init_db(restored)
        assert restored.privacy.is_opted_out(42)
        assert restored.with_request(RequestContext("42", "10", None, True)).privacy is restored.privacy
        conn = background_indexer._connect(background_indexer._db_path(ctx))
        try:
            assert conn.execute("SELECT content FROM messages ORDER BY message_id").fetchall()[0][0] == "keep"
            assert conn.execute("SELECT COUNT(*) FROM users WHERE user_id = '42'").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM message_embeds").fetchone()[0] == 0
            # Even a worker with an old snapshot cannot reinsert a withdrawn author's data.
            conn.execute("INSERT INTO messages (message_id, author_id, content) VALUES ('200', '42', 'new secret')")
            conn.execute(
                "INSERT INTO message_attachments (attachment_id, message_id, channel_id, url) VALUES ('2', '200', '10', 'https://example.com/b')"
            )
            conn.execute("INSERT INTO messages (message_id, content) VALUES ('100', 'edited secret')")
            conn.execute("INSERT INTO users (user_id, name) VALUES ('42', 'Alice again')")
            conn.commit()
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        finally:
            conn.close()
        payload = MagicMock(spec=discord.RawMessageUpdateEvent)
        payload.message_id = 300
        payload.channel_id = 10
        payload.guild_id = None
        payload.data = {"content": "unknown author"}
        await background_indexer.record_message_edit(restored, payload)
        conn = background_indexer._connect(background_indexer._db_path(ctx))
        try:
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE message_id = '300'").fetchone()[0] == 0
        finally:
            conn.close()

    asyncio.run(run())


def test_opted_out_messages_never_reach_indexing_or_topic_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    ctx.privacy.opted_out_users.add("42")
    message = MagicMock(spec=discord.Message)
    message.author.id = 42
    database = AsyncMock(side_effect=AssertionError("No database or content processing expected"))
    monkeypatch.setattr(background_indexer, "_with_db", database)
    asyncio.run(background_indexer.record_message_create(ctx, message))
    assert asyncio.run(topic_subscriptions.topic_subscriptions_handle_message(ctx, message)) == {
        "ok": True,
        "notified": 0,
    }
    assert (
        topic_subscriptions._match_subscriptions(
            ctx, guild_id="1", channel_id="10", author_id="42", content="secret", now_ms=1
        )
        == []
    )
    database.assert_not_awaited()
    assert not Path(str(ctx.config["topic_subscriptions_db_path"])).exists()


def test_opt_out_purges_feature_records_and_blocks_tools(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    for module, table, statement in (
        (
            topic_subscriptions,
            "topic_subscriptions",
            "INSERT INTO topic_subscriptions (user_id,guild_id,channel_id,topic,topic_embedding,created_at,updated_at) VALUES ('42','1','10','topic','[]',1,1)",
        ),
        (
            commitment,
            "commitments",
            "INSERT INTO commitments (name,description,user_id,channel_id,start_date,end_date,created_at,updated_at) VALUES ('reminder','secret','42','10','2026-10-01','2026-10-30',1,1)",
        ),
    ):
        path = module._db_path(ctx)
        conn = module._connect(path)
        try:
            module._init_db(conn)
            conn.execute(statement)
            conn.commit()
        finally:
            conn.close()
        asyncio.run(module.purge_user_data(ctx, "42"))
        conn = module._connect(path)
        try:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            # A stale worker must not recreate feature data after the purge commits.
            conn.execute(statement)
            conn.commit()
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        finally:
            conn.close()
    ctx.privacy.opted_out_users.add("42")
    ctx.request = RequestContext("42", "10", "1", False)
    with pytest.raises(PermissionError):
        permissions.require_request(ctx)
    with pytest.raises(PermissionError):
        asyncio.run(commitment.commitment_manage(ctx, {"operation": "create", "user_id": "42"}))
    with pytest.raises(PermissionError):
        asyncio.run(topic_subscriptions.topic_subscription_manage(ctx, {"operation": "create", "user_id": "42"}))


def test_opted_out_audio_is_dropped_before_buffering_and_transcription() -> None:
    async def run() -> None:
        ctx = GlobalContext()
        ctx.privacy.opted_out_users.add("42")
        api = AsyncMock()
        transcriber = voice_transcriber.VoiceTranscriber(
            ctx=ctx,
            openai_client=api,
            loop=asyncio.get_running_loop(),
            transcript_dir=None,
            silence_seconds=1,
            max_segment_seconds=8,
            flush_interval_seconds=1,
            model="whisper-1",
        )
        user = MagicMock(spec=discord.User)
        user.id = 42
        transcriber.enqueue_audio(user, b"audio")
        assert not transcriber._buffers
        buffer = voice_transcriber._UserBuffer(user_id=42, user_name="Alice", last_audio_ts=0)
        await transcriber._transcribe_and_log(buffer, b"wav")
        api.audio.transcriptions.create.assert_not_awaited()

    asyncio.run(run())


def test_optout_removes_legacy_conversation_records(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    asyncio.run(background_indexer.init_db(ctx))
    conn = background_indexer._connect(background_indexer._db_path(ctx))
    try:
        legacy_conversation_schema.init_tables(conn)
        conn.execute(
            "INSERT INTO messages (message_id, channel_id, author_id, content) VALUES ('100', '10', '42', 'withdrawn')"
        )
        conn.execute(
            "INSERT INTO codi_conversations (conversation_id, channel_id, content_hash, name) "
            "VALUES ('conv', '10', 'hash', 'withdrawn summary')"
        )
        conn.execute("INSERT INTO codi_conversation_messages VALUES ('conv', '10', '100', 1)")
        conn.execute("INSERT INTO codi_channel_state (channel_id) VALUES ('10')")
        privacy.confirm_in_database(conn, "42", 1)
        assert conn.execute("SELECT COUNT(*) FROM codi_conversations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM codi_conversation_messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM codi_channel_state").fetchone()[0] == 0
    finally:
        conn.close()


def test_rest_search_discards_opted_out_messages_before_reading_content(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        ctx = GlobalContext()
        ctx.privacy.opted_out_users.add("42")
        client = MagicMock(spec=discord.Client)
        client.wait_until_ready = AsyncMock()
        ctx.discord_client = client
        blocked = MagicMock(spec=discord.Message)
        blocked.author.id = 42
        allowed = MagicMock(spec=discord.Message)
        allowed.author.id = 99
        allowed.author.bot = False
        allowed.content = "hello"

        async def history(**_kwargs: object) -> AsyncIterator[MagicMock]:
            for message in (blocked, allowed):
                yield message

        ctx.request = RequestContext("99", "10", None, True)
        channel = MagicMock(spec=discord.DMChannel)
        channel.id = 10
        channel.recipient = SimpleNamespace(id=99)
        channel.history.side_effect = history
        client.fetch_channel.return_value = channel
        payload = MagicMock(return_value={"content": "hello"})
        monkeypatch.setattr(discord_search, "_message_payload", payload)
        result = await discord_search.discord_search_messages(ctx, {"channel_id": "10", "max_content_chars": 16})
        assert result["results"] == [{"content": "hello"}]
        payload.assert_called_once_with(allowed, 16)

    asyncio.run(run())


def test_topic_dm_does_not_send_withdrawn_content_after_fetch() -> None:
    async def run() -> None:
        ctx = GlobalContext()
        client = MagicMock(spec=discord.Client)
        ctx.discord_client = client
        ctx.discord_loop = asyncio.get_running_loop()
        user = MagicMock(spec=discord.User)
        user.send = AsyncMock()

        async def fetch(_user_id: int) -> MagicMock:
            # The source author withdraws their data while the recipient is fetched.
            ctx.privacy.opted_out_users.add("99")
            ctx.privacy.generation += 1
            return user

        client.fetch_user = AsyncMock(side_effect=fetch)
        await topic_subscriptions._send_dm(ctx, "42", "message from user 99")
        user.send.assert_not_awaited()

    asyncio.run(run())
