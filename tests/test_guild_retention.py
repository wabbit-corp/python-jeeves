# SPDX-License-Identifier: LicenseRef-Wabbit-Public-Test-License-1.1

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import legacy_conversation_schema
import pytest

from servant import database, guild_retention, voice_transcriber
from servant.defs import GlobalContext, Personality
from servant.modules import (
    background_indexer,
    commitment,
    event_channels,
    topic_subscriptions,
)


def _ctx(tmp_path: Path) -> GlobalContext:
    ctx = GlobalContext()
    ctx.config.update(
        {
            "indexer_db_path": str(tmp_path / "index.db"),
            "commitments_db_path": str(tmp_path / "commitments.db"),
            "topic_subscriptions_db_path": str(tmp_path / "topics.db"),
            "event_channels_db_path": str(tmp_path / "events.db"),
        }
    )
    return ctx


def _insert(conn: database.Connection, table: str, values: dict[str, str | int | None]) -> None:
    """Fill unrelated required schema fields while keeping real ownership keys explicit."""
    for col in conn.execute(f"PRAGMA table_info({table})"):
        name = str(col[1])
        if col[3] and col[4] is None and name not in values:
            values[name] = 1 if "INT" in str(col[2]).upper() else "fixture"
    cols = ",".join(values)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))


def _seed(ctx: GlobalContext) -> None:
    conn = background_indexer._connect(background_indexer._db_path(ctx))
    try:
        background_indexer._init_db(conn)
        legacy_conversation_schema.init_tables(conn)
        for uid in ("42", "43", "44"):
            _insert(conn, "users", {"user_id": uid, "name": f"user-{uid}"})
        for gid, cid, mid, uid in [
            ("1", "10", "100", "43"),
            ("2", "20", "101", "42"),
            (None, "30", "102", "44"),
            ("1", "10", "103", "42"),
        ]:
            if mid != "103":
                _insert(conn, "channels", {"guild_id": gid, "channel_id": cid})
            _insert(
                conn,
                "messages",
                {"guild_id": gid, "channel_id": cid, "message_id": mid, "author_id": uid, "content": "private content"},
            )
        for gid in ("1", "2"):
            cid, mid, role = ("10", "100", "700") if gid == "1" else ("20", "101", "800")
            for table, cols in guild_retention._tables(conn).items():
                if table in ("channels", "messages", "users") or not (
                    "guild_id" in cols or "channel_id" in cols or table.startswith("message_")
                ):
                    continue
                values: dict[str, str | int | None] = {}
                for key, val in [
                    ("guild_id", gid),
                    ("channel_id", cid),
                    ("parent_channel_id", cid),
                    ("message_id", mid),
                    ("user_id", "42"),
                    ("role_id", role),
                    ("emoji_id", role),
                    ("sticker_id", role),
                    ("attachment_id", role),
                    ("conversation_id", f"conv-{gid}"),
                ]:
                    if key in cols:
                        values[key] = val
                _insert(conn, table, values)
        _insert(conn, "guild_members", {"guild_id": "1", "user_id": "43"})
        conn.execute(
            "UPDATE messages SET reply_to_message_id='100', reply_to_channel_id='10', reply_to_guild_id='1' WHERE message_id='101'"
        )
        conn.execute("INSERT INTO privacy_opt_outs VALUES ('99', 1)")
        conn.execute("INSERT INTO privacy_excluded_messages VALUES ('999', '99')")
        conn.commit()
    finally:
        conn.close()
    for module in (commitment, topic_subscriptions, event_channels):
        conn = module._connect(module._db_path(ctx))
        try:
            module._init_db(conn)
            table = {
                commitment: "commitments",
                topic_subscriptions: "topic_subscriptions",
                event_channels: "event_channel_subscriptions",
            }[module]
            for gid, cid in [("1", "10"), ("1", "77"), ("2", "20"), (None, "30")]:
                if gid is None and module is not commitment:
                    continue
                values = {"channel_id": cid, "user_id": "42"} if module is not event_channels else {"channel_id": cid}
                values["guild_id"] = gid
                _insert(conn, table, values)
            if module is topic_subscriptions:
                for cid in ("10", "77", "20"):
                    _insert(conn, "topic_notification_cooldowns", {"channel_id": cid, "user_id": "42"})
                conn.execute("CREATE TABLE privacy_blocked_users (user_id TEXT PRIMARY KEY)")
                conn.execute("INSERT INTO privacy_blocked_users VALUES ('99')")
            if module is event_channels:
                for row in conn.execute("SELECT id FROM event_channel_subscriptions"):
                    _insert(conn, "event_channel_seen_items", {"subscription_id": row[0], "item_id": "item"})
            conn.commit()
        finally:
            conn.close()


def _assert_deleted(ctx: GlobalContext) -> None:
    for path in guild_retention._paths(ctx):
        conn = database.connect(path)
        try:
            for table, cols in guild_retention._tables(conn).items():
                if "guild_id" in cols:
                    assert not conn.execute(f'SELECT 1 FROM {table} WHERE guild_id="1"').fetchall(), table
                if "channel_id" in cols:
                    assert not conn.execute(
                        f'SELECT 1 FROM {table} WHERE channel_id IN ("10", "77", "88")'
                    ).fetchall(), table
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    conn = database.connect(background_indexer._db_path(ctx))
    try:
        assert {row[0] for row in conn.execute("SELECT message_id FROM messages")} == {"101", "102"}
        assert {row[0] for row in conn.execute("SELECT user_id FROM users")} == {"42", "44"}
        assert tuple(
            conn.execute(
                'SELECT reply_to_message_id,reply_to_channel_id,reply_to_guild_id FROM messages WHERE message_id="101"'
            ).fetchone()
        ) == (None, None, None)
        assert conn.execute("SELECT user_id FROM privacy_opt_outs").fetchone()[0] == "99"
        assert conn.execute("SELECT message_id FROM privacy_excluded_messages").fetchone()[0] == "999"
        assert conn.execute('SELECT pending FROM retention_removed_guilds WHERE guild_id="1"').fetchone()[0] == 0
    finally:
        conn.close()


def test_removal_deletes_all_scoped_tables_preserves_other_guild_dm_and_optout(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    ctx.channel_messages["20"].append({"content": "retrieved departed-server text"})
    ctx.channel_personality["77"] = Personality(name="Vox", description="server preference")
    asyncio.run(guild_retention.remove_guild(ctx, "1", ["88"]))
    _assert_deleted(ctx)
    assert not ctx.channel_messages
    assert not ctx.channel_personality
    assert {"10", "77", "88"} <= ctx.guild_retention.removed_channels
    # Idempotence matters for retries and repeated remove events.
    asyncio.run(guild_retention.remove_guild(ctx, "1"))
    _assert_deleted(ctx)


def test_stale_writes_and_child_records_cannot_restore_deleted_guild(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    conn = background_indexer._connect(background_indexer._db_path(ctx))
    try:
        asyncio.run(guild_retention.remove_guild(ctx, "1"))
        _insert(conn, "messages", {"message_id": "100", "guild_id": "1", "channel_id": "10", "author_id": "43"})
        _insert(conn, "message_role_mentions", {"message_id": "100", "role_id": "700"})
        background_indexer._upsert_users(conn, [("43", "restored", None, None, 0, 0, None, 1, 1)], guild_id="1")
        conn.commit()
        assert not conn.execute('SELECT 1 FROM messages WHERE message_id="100"').fetchone()
        assert not conn.execute('SELECT 1 FROM message_role_mentions WHERE message_id="100"').fetchone()
        assert not conn.execute('SELECT 1 FROM users WHERE user_id="43"').fetchone()
    finally:
        conn.close()
    conn = commitment._connect(commitment._db_path(ctx))
    try:
        _insert(conn, "commitments", {"channel_id": "77", "user_id": "43"})
        conn.commit()
        assert not conn.execute('SELECT 1 FROM commitments WHERE channel_id="77"').fetchone()
    finally:
        conn.close()


def test_offline_removal_and_unavailable_member_guild(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    asyncio.run(guild_retention.reconcile(ctx, lambda: [SimpleNamespace(id=2, unavailable=True)]))
    _assert_deleted(ctx)
    assert ctx.guild_retention.ready.is_set()
    # A temporarily unavailable guild remains installed, so retain all its rows.
    assert "2" not in ctx.guild_retention.removed_guilds


def test_partial_failure_is_journaled_and_retried_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    purge = guild_retention._purge

    def fail(conn: database.Connection, gid: str, channels: set[str], users: set[str]) -> None:
        if "topic_subscriptions" in guild_retention._tables(conn):
            raise OSError("injected storage failure")
        purge(conn, gid, channels, users)

    monkeypatch.setattr(guild_retention, "_purge", fail)
    with pytest.raises(OSError, match="storage failure"):
        asyncio.run(guild_retention.remove_guild(ctx, "1"))
    conn = database.connect(background_indexer._db_path(ctx))
    try:
        assert conn.execute('SELECT pending FROM retention_removed_guilds WHERE guild_id="1"').fetchone()[0] == 1
        assert conn.execute('SELECT 1 FROM retention_removed_channels WHERE channel_id="77"').fetchone()
    finally:
        conn.close()
    monkeypatch.setattr(guild_retention, "_purge", purge)
    restarted = _ctx(tmp_path)
    asyncio.run(guild_retention.reconcile(restarted, lambda: [SimpleNamespace(id=2)]))
    _assert_deleted(restarted)


def test_processing_scope_interrupts_delayed_worker_even_after_reinstallation(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    with database.processing_scope(lambda: ctx.privacy.generation):
        old = database.connect(background_indexer._db_path(ctx))
    try:
        asyncio.run(guild_retention.remove_guild(ctx, "1"))
        asyncio.run(guild_retention.allow_guild(ctx, "1"))
        with pytest.raises(database.OperationalError, match="interrupted"):
            old.execute('INSERT INTO users (user_id,name) VALUES ("43","stale profile")')
        with database.processing_scope(lambda: ctx.privacy.generation):
            fresh = database.connect(background_indexer._db_path(ctx))
        try:
            _insert(fresh, "messages", {"message_id": "104", "guild_id": "1", "channel_id": "10", "author_id": "42"})
            fresh.commit()
            assert fresh.execute('SELECT 1 FROM messages WHERE message_id="104"').fetchone()
        finally:
            fresh.close()
    finally:
        old.close()


def test_remove_cancels_processing_and_voice_buffers(tmp_path: Path) -> None:
    async def run() -> None:
        ctx = _ctx(tmp_path)
        _seed(ctx)
        manager = voice_transcriber.VoiceTranscriberManager(ctx)
        transcriber = SimpleNamespace(_buffers={42: b"voice"}, stop=MagicMock())
        manager._transcribers[1] = transcriber
        ctx.module_state["voice_transcriber"] = manager
        work = asyncio.create_task(asyncio.Event().wait())
        ctx.privacy.processing_tasks.add(work)
        await guild_retention.remove_guild(ctx, "1")
        assert work.cancelled()
        assert not transcriber._buffers
        assert 1 not in manager._transcribers

    asyncio.run(run())


def test_transcripts_are_purged_including_legacy_multiline_records(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    directory = tmp_path / "transcripts"
    ctx.config["voice_transcript_dir"] = str(directory)
    legacy = directory / "2026-10-01" / "voice_transcripts.log"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "Voice transcript guild=1 channel=10 text=remove\ncontinued private text\nVoice transcript guild=2 channel=20 text=keep\nkeep continuation\n"
    )
    partition = directory / "guilds" / "1" / "2026-10-01" / "voice_transcripts.log"
    partition.parent.mkdir(parents=True)
    partition.write_text("private transcript")
    asyncio.run(guild_retention.remove_guild(ctx, "1"))
    assert legacy.read_text() == "Voice transcript guild=2 channel=20 text=keep\nkeep continuation\n"
    assert not (directory / "guilds" / "1").exists()


def test_failed_transcript_cleanup_is_retried_even_after_database_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    original = voice_transcriber.purge_guild_transcripts

    def fail(_ctx: GlobalContext, _guild_id: str) -> None:
        raise OSError("transcripts locked")

    monkeypatch.setattr(voice_transcriber, "purge_guild_transcripts", fail)
    with pytest.raises(OSError, match="transcripts locked"):
        asyncio.run(guild_retention.remove_guild(ctx, "1"))
    conn = database.connect(background_indexer._db_path(ctx))
    try:
        assert not conn.execute('SELECT 1 FROM messages WHERE guild_id="1"').fetchone()
        assert conn.execute('SELECT pending FROM retention_removed_guilds WHERE guild_id="1"').fetchone()[0] == 1
    finally:
        conn.close()
    monkeypatch.setattr(voice_transcriber, "purge_guild_transcripts", original)
    restarted = _ctx(tmp_path)
    asyncio.run(guild_retention.reconcile(restarted, lambda: [SimpleNamespace(id=2)]))
    _assert_deleted(restarted)


def test_worker_thread_cannot_write_after_its_async_task_is_cancelled(tmp_path: Path) -> None:
    import threading

    async def run() -> None:
        ctx = _ctx(tmp_path)
        _seed(ctx)
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        errors: list[str] = []

        def worker() -> None:
            conn = database.connect(background_indexer._db_path(ctx))
            try:
                started.set()
                assert release.wait(5)
                conn.execute('INSERT INTO users (user_id,name) VALUES ("43","delayed")')
                conn.commit()
            except database.OperationalError as error:
                errors.append(str(error))
            finally:
                conn.close()
                done.set()

        async def processing() -> None:
            with database.processing_scope(lambda: ctx.privacy.generation):
                await asyncio.to_thread(worker)

        work = asyncio.create_task(processing())
        ctx.privacy.processing_tasks.add(work)
        assert await asyncio.to_thread(started.wait, 5)
        try:
            await guild_retention.remove_guild(ctx, "1")
            await guild_retention.allow_guild(ctx, "1")
            assert work.cancelled()
        finally:
            release.set()
        assert await asyncio.to_thread(done.wait, 5)
        assert errors == ["interrupted"]
        conn = database.connect(background_indexer._db_path(ctx))
        try:
            assert not conn.execute('SELECT 1 FROM users WHERE user_id="43"').fetchone()
        finally:
            conn.close()

    asyncio.run(run())


def test_legacy_reminder_ownership_is_recovered_before_offline_cleanup(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    conn = database.connect(commitment._db_path(ctx))
    try:
        conn.execute("UPDATE commitments SET guild_id=NULL")
        conn.commit()
    finally:
        conn.close()
    asyncio.run(guild_retention.reconcile(ctx, lambda: [SimpleNamespace(id=2)]))
    _assert_deleted(ctx)
    conn = database.connect(commitment._db_path(ctx))
    try:
        assert tuple(conn.execute('SELECT guild_id,channel_id FROM commitments WHERE channel_id="20"').fetchone()) == (
            "2",
            "20",
        )
        assert conn.execute('SELECT guild_id FROM commitments WHERE channel_id="30"').fetchone()[0] is None
    finally:
        conn.close()


def test_voice_append_cannot_restore_a_file_after_removal_and_rejoin(tmp_path: Path) -> None:
    async def run() -> None:
        ctx = _ctx(tmp_path)
        _seed(ctx)
        transcriber = voice_transcriber.VoiceTranscriber(
            ctx=ctx,
            openai_client=MagicMock(),
            loop=asyncio.get_running_loop(),
            transcript_dir=tmp_path / "transcripts",
            silence_seconds=1.0,
            max_segment_seconds=8.0,
            flush_interval_seconds=0.5,
            model="test",
        )
        transcriber._guild_id = 1
        generation = ctx.privacy.generation
        path = tmp_path / "transcripts" / "guilds" / "1" / "2026-10-07" / "voice_transcripts.log"
        await guild_retention.remove_guild(ctx, "1")
        await guild_retention.allow_guild(ctx, "1")
        transcriber._append_line_sync(path, "stale text", generation)
        assert not path.exists()
        transcriber.stop()
        transcriber.enqueue_audio(SimpleNamespace(id=42), b"old audio")
        assert not transcriber._buffers

    asyncio.run(run())
