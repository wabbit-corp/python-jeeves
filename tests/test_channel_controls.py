# SPDX-License-Identifier: LicenseRef-Wabbit-Public-Test-License-1.1

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import legacy_conversation_schema
import pytest
from discord import app_commands

from servant import channel_controls, database, discord_controls, guild_retention, voice_transcriber
from servant.defs import GlobalContext
from servant.modules import (
    background_indexer,
    commitment,
    event_channels,
    topic_subscriptions,
)


def context(tmp_path: Path) -> GlobalContext:
    ctx = GlobalContext(
        config={
            "indexer_db_path": str(tmp_path / "index.db"),
            "commitments_db_path": str(tmp_path / "commitments.db"),
            "topic_subscriptions_db_path": str(tmp_path / "topics.db"),
            "event_channels_db_path": str(tmp_path / "events.db"),
            "voice.transcript_dir": str(tmp_path / "transcripts"),
        }
    )
    ctx.guild_retention.ready.set()
    return ctx


def insert(conn: database.Connection, table: str, values: dict[str, str | int | None]) -> None:
    for column in conn.execute(f"PRAGMA table_info({table})"):
        name = str(column[1])
        if name not in values and column[3] and column[4] is None and not column[5]:
            values[name] = 0 if "INT" in str(column[2]) else "fixture"
    names = ",".join(values)
    marks = ",".join("?" for _ in values)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(values.values()))


def seed(ctx: GlobalContext) -> None:
    with database.connect(background_indexer._db_path(ctx)) as conn:
        background_indexer._init_db(conn)
        legacy_conversation_schema.init_tables(conn)
        for channel_id, guild_id in [("10", "1"), ("11", "1"), ("20", "2")]:
            insert(conn, "channels", {"channel_id": channel_id, "guild_id": guild_id})
            insert(
                conn,
                "messages",
                {
                    "message_id": channel_id,
                    "channel_id": channel_id,
                    "guild_id": guild_id,
                    "author_id": "42",
                    "content": "private",
                    "content_available": 1,
                },
            )
            insert(conn, "message_embeds", {"message_id": channel_id, "idx": 0, "embed_json": "private"})
            insert(
                conn,
                "codi_conversations",
                {"conversation_id": channel_id, "channel_id": channel_id, "guild_id": guild_id},
            )
            insert(
                conn,
                "codi_conversation_messages",
                {"conversation_id": channel_id, "channel_id": channel_id, "message_id": channel_id},
            )
        conn.execute("INSERT INTO privacy_opt_outs VALUES ('99',1)")
    for module, table in [
        (commitment, "commitments"),
        (topic_subscriptions, "topic_subscriptions"),
        (event_channels, "event_channel_subscriptions"),
    ]:
        with database.connect(module._db_path(ctx)) as conn:
            module._init_db(conn)
            for channel_id, guild_id in [("10", "1"), ("11", "1"), ("20", "2")]:
                insert(conn, table, {"channel_id": channel_id, "guild_id": guild_id})
            if module is event_channels:
                for row in conn.execute("SELECT id FROM event_channel_subscriptions"):
                    insert(conn, "event_channel_seen_items", {"subscription_id": row[0], "item_id": "test"})
            if module is topic_subscriptions:
                for channel_id in ("10", "11", "20"):
                    insert(conn, "topic_notification_cooldowns", {"channel_id": channel_id, "user_id": "42"})


def test_existing_and_new_channels_and_threads_are_enabled_without_overrides(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    seed(ctx)
    asyncio.run(channel_controls.load(ctx))
    for channel_id, guild_id in (("10", "1"), ("21", "2"), ("31", "3")):
        assert channel_controls.is_allowed(ctx, channel_id, guild_id)
    thread = MagicMock(spec=discord.Thread)
    thread.id = 32
    thread.guild = SimpleNamespace(id=3)
    assert channel_controls.allows_channel(ctx, thread)
    assert channel_controls.allows_guild(ctx, "3")


def test_database_accepts_new_channel_records_without_enable_commands(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    seed(ctx)
    asyncio.run(channel_controls.load(ctx))
    with database.connect(background_indexer._db_path(ctx)) as conn:
        insert(conn, "messages", {"message_id": "new", "channel_id": "21", "guild_id": "1", "content": "new"})
        assert conn.execute("SELECT 1 FROM messages WHERE message_id='new'").fetchone()


def test_old_allowlist_guards_are_replaced_without_losing_disabled_choices(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    seed(ctx)
    with database.connect(background_indexer._db_path(ctx)) as conn:
        conn.execute("INSERT INTO privacy_channel_mode VALUES (1)")
        conn.execute("INSERT INTO privacy_channel_controls VALUES ('10','1',0,0)")
        conn.execute(
            "CREATE TRIGGER privacy_channel_messages_insert_4 BEFORE INSERT ON messages "
            "WHEN NEW.guild_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM privacy_channel_controls "
            "WHERE channel_id=NEW.channel_id AND guild_id=NEW.guild_id AND allowed=1) "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
    asyncio.run(channel_controls.load(ctx))
    assert not channel_controls.is_allowed(ctx, "10", "1")
    assert channel_controls.is_allowed(ctx, "21", "1")
    with database.connect(background_indexer._db_path(ctx)) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='privacy_channel_messages_insert_4'").fetchone()
        for cid in ("10", "21"):
            insert(conn, "messages", {"message_id": "new-" + cid, "channel_id": cid, "guild_id": "1", "content": "new"})
        assert not conn.execute("SELECT 1 FROM messages WHERE message_id='new-10'").fetchone()
        assert conn.execute("SELECT 1 FROM messages WHERE message_id='new-21'").fetchone()


def test_default_enabled_durable_disable_and_complete_channel_cleanup(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    seed(ctx)

    async def run() -> None:
        await channel_controls.load(ctx)
        assert channel_controls.is_allowed(ctx, "10", "1")
        assert channel_controls.is_allowed(ctx, "30", None)
        ctx.guild_retention.removed_guilds.add("2")
        assert not channel_controls.is_allowed(ctx, "10", "2")
        fresh = context(tmp_path)
        await channel_controls.load(fresh)
        assert channel_controls.is_allowed(fresh, "10", "1")
        ctx.channel_messages["20"].append({"content": "retrieved private text"})
        await channel_controls.disable(ctx, "10", "1")
        assert not ctx.channel_messages
        assert not channel_controls.is_allowed(ctx, "10", "1")
        assert channel_controls.is_allowed(ctx, "11", "1")
        fresh = context(tmp_path)
        await channel_controls.load(fresh)
        assert not channel_controls.is_allowed(fresh, "10", "1")
        assert channel_controls.is_allowed(fresh, "11", "1")

    asyncio.run(run())
    for path in guild_retention._paths(ctx):
        with database.connect(path) as conn:
            for table, cols in guild_retention._tables(conn).items():
                if "channel_id" in cols and table != "message_channel_mentions":
                    assert not conn.execute(f"SELECT 1 FROM {table} WHERE channel_id='10'").fetchone(), table
            assert tuple(
                conn.execute("SELECT allowed,pending FROM privacy_channel_controls WHERE channel_id='10'").fetchone()
            ) == (0, 0)
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with database.connect(background_indexer._db_path(ctx)) as conn:
        assert {row[0] for row in conn.execute("SELECT message_id FROM messages")} == {"11", "20"}
        assert {row[0] for row in conn.execute("SELECT message_id FROM message_embeds")} == {"11", "20"}
        assert conn.execute("SELECT user_id FROM privacy_opt_outs").fetchone()[0] == "99"
        # Late writes stay blocked in the disabled channel; new channels work by default.
        for cid in ("10", "21"):
            insert(
                conn, "messages", {"message_id": "late-" + cid, "channel_id": cid, "guild_id": "1", "content": "late"}
            )
        assert {row[0] for row in conn.execute("SELECT message_id FROM messages")} == {"11", "20", "late-21"}


def test_interrupted_channel_cleanup_is_retried_on_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = context(tmp_path)
    seed(ctx)
    original = channel_controls._delete_records

    async def run() -> None:
        await channel_controls.load(ctx)
        await channel_controls.enable(ctx, "10", "1")
        monkeypatch.setattr(channel_controls, "_delete_records", MagicMock(side_effect=OSError("disk unavailable")))
        with pytest.raises(OSError):
            await channel_controls.disable(ctx, "10", "1")
        assert not channel_controls.is_allowed(ctx, "10", "1")
        with pytest.raises(RuntimeError, match="unfinished"):
            await channel_controls.enable(ctx, "10", "1")
        monkeypatch.setattr(channel_controls, "_delete_records", original)
        fresh = context(tmp_path)
        await channel_controls.load(fresh)
        assert not channel_controls.is_allowed(fresh, "10", "1")

    asyncio.run(run())
    with database.connect(background_indexer._db_path(ctx)) as conn:
        assert not conn.execute("SELECT 1 FROM messages WHERE channel_id='10'").fetchone()


def test_old_worker_cannot_restore_data_even_after_channel_is_enabled_again(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    ready = threading.Event()
    proceed = threading.Event()

    def delayed_write() -> None:
        with database.connect(background_indexer._db_path(ctx)) as conn:
            ready.set()
            assert proceed.wait(5)
            insert(
                conn, "messages", {"message_id": "late", "channel_id": "10", "guild_id": "1", "content": "withdrawn"}
            )

    async def run() -> None:
        seed(ctx)
        await channel_controls.load(ctx)
        await channel_controls.enable(ctx, "10", "1")
        with database.processing_scope(lambda: ctx.privacy.generation):
            worker = asyncio.create_task(asyncio.to_thread(delayed_write))
        assert await asyncio.to_thread(ready.wait, 5)
        await channel_controls.disable(ctx, "10", "1")
        await channel_controls.enable(ctx, "10", "1")
        proceed.set()
        with pytest.raises(database.DatabaseError):
            await worker

    asyncio.run(run())
    with database.connect(background_indexer._db_path(ctx)) as conn:
        assert not conn.execute("SELECT 1 FROM messages WHERE channel_id='10'").fetchone()


def test_background_pickers_skip_disabled_channels_and_admit_default_enabled_channels(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    with database.connect(background_indexer._db_path(ctx)) as conn:
        background_indexer._init_db(conn)
        for cid, gid in [("10", "1"), ("20", "2")]:
            insert(
                conn, "channel_state", {"channel_id": cid, "guild_id": gid, "latest_seen_id": "100", "backfill_done": 0}
            )
            insert(conn, "guild_state", {"guild_id": gid, "backfill_done": 0})
            insert(conn, "thread_parent_state", {"parent_channel_id": cid, "guild_id": gid})

    async def run() -> None:
        await asyncio.to_thread(channel_controls._store, ctx, "10", "1", allowed=False, pending=False)
        await channel_controls.load(ctx)

    asyncio.run(run())
    with database.connect(background_indexer._db_path(ctx)) as conn:
        for picker in (
            background_indexer._pick_channel_state_backfill,
            background_indexer._pick_channel_state_tail,
            background_indexer._pick_channel_state_search,
        ):
            assert picker(conn, 1)["channel_id"] == "20"
        assert background_indexer._pick_channel_state_pins(conn, 1, 1)["channel_id"] == "20"
        assert background_indexer._pick_guild_state(conn, 1)["guild_id"] == "1"
        assert background_indexer._pick_guild_state(conn, None)["guild_id"] == "1"
        assert background_indexer._pick_thread_parent_state(conn)["parent_channel_id"] == "20"


def test_disabled_create_edit_and_search_payloads_do_not_enter_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = context(tmp_path)
    item = interaction(ctx)
    ctx.channel_controls.disabled["10"] = "1"
    message = MagicMock(spec=discord.Message)
    message.channel = item.channel
    message.author.id = 42
    convert = MagicMock(side_effect=AssertionError("disabled channel content must not be read"))
    monkeypatch.setattr(background_indexer, "_message_row", convert)

    async def run() -> None:
        await background_indexer.record_message_create(ctx, message)
        await background_indexer.record_message_edit(ctx, SimpleNamespace(channel_id=10, guild_id=1))
        result = await background_indexer._index_message_payloads(
            ctx, [{"channel_id": "10", "guild_id": "1", "content": "disabled"}], channel_id="10", guild_id="1"
        )
        assert result["messages_indexed"] == 0

    asyncio.run(run())
    convert.assert_not_called()
    assert not background_indexer._db_path(ctx).exists()


def interaction(ctx: GlobalContext, *, moderator: bool = True) -> MagicMock:
    target = MagicMock(spec=discord.TextChannel)
    target.id = 10
    target.guild = MagicMock(spec=discord.Guild)
    target.guild.id = 1
    target.guild.fetch_member.return_value = SimpleNamespace(id=42)
    target.permissions_for.return_value = discord.Permissions(view_channel=True, manage_messages=moderator)
    result = MagicMock(spec=discord.Interaction)
    result.user = SimpleNamespace(id=42)
    result.guild_id = 1
    result.channel_id = 10
    result.channel = target
    result.client = MagicMock(spec=discord.Client)
    result.client.fetch_channel.return_value = target
    result.response = AsyncMock()
    result.followup = AsyncMock()
    result.edit_original_response = AsyncMock()
    ctx.channel_controls.loaded = True
    return result


def commands(ctx: GlobalContext) -> app_commands.Group:
    group = app_commands.Group(name="vox", description="Vox")
    discord_controls.register(group, ctx)
    return group


@pytest.mark.parametrize("operation", ["enable", "disable", "status"])
def test_channel_commands_deny_members_without_moderation_permissions(tmp_path: Path, operation: str) -> None:
    ctx = context(tmp_path)
    item = interaction(ctx, moderator=False)
    group = commands(ctx).get_command("channel")
    command = group.get_command(operation)
    asyncio.run(command.callback(item))
    item.channel.send.assert_not_called()
    assert "permission is required" in item.followup.send.call_args.args[0]
    assert not ctx.channel_controls.disabled


def test_enable_resumes_privately_without_posting_a_public_notice(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    item = interaction(ctx)
    ctx.channel_controls.disabled["10"] = "1"
    command = commands(ctx).get_command("channel").get_command("enable")
    item.channel.send.side_effect = RuntimeError("cannot post public messages")
    asyncio.run(command.callback(item))
    assert channel_controls.is_allowed(ctx, "10", "1")
    item.channel.send.assert_not_called()
    assert item.followup.send.call_args.kwargs["ephemeral"] is True


def test_private_disable_confirmation_rechecks_permissions_and_owner(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    item = interaction(ctx)

    async def run() -> None:
        await commands(ctx).get_command("channel").get_command("disable").callback(item)
        assert item.followup.send.call_args.kwargs["ephemeral"] is True
        view = item.followup.send.call_args.kwargs["view"]
        stranger = interaction(ctx)
        stranger.user = SimpleNamespace(id=43)
        assert not await view.interaction_check(stranger)
        item.channel.permissions_for.return_value = discord.Permissions(view_channel=True)
        await view.confirm.callback(item)
        assert channel_controls.is_allowed(ctx, "10", "1")

    asyncio.run(run())


def test_privacy_command_is_ephemeral_and_available_to_opted_out_accounts(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    item = interaction(ctx, moderator=False)
    ctx.privacy.opted_out_users.add("42")
    asyncio.run(commands(ctx).get_command("privacy").callback(item))
    assert item.response.send_message.call_args.kwargs["ephemeral"] is True
    embed = item.response.send_message.call_args.kwargs["embed"].to_dict()
    text = str(embed)
    for required in (channel_controls.POLICY_URL, "wabbit@wabbit.one", "/vox optout", "user ID", "logs", "one month"):
        assert required in text


def test_confirming_disable_completes_cleanup_and_edits_private_confirmation(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    seed(ctx)
    item = interaction(ctx)

    async def run() -> None:
        await channel_controls.load(ctx)
        await channel_controls.enable(ctx, "10", "1")
        await commands(ctx).get_command("channel").get_command("disable").callback(item)
        view = item.followup.send.call_args.kwargs["view"]
        await view.confirm.callback(item)
        assert not channel_controls.is_allowed(ctx, "10", "1")
        assert "active archive has been cleared" in item.edit_original_response.call_args.kwargs["content"]
        assert view.confirm.disabled and view.cancel.disabled

    asyncio.run(run())


def test_channel_transcript_cleanup_preserves_other_channels_and_multiline_records(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    # Use the real configured key, including partitioned and legacy mixed files.
    ctx.config[voice_transcriber.CONFIG_TRANSCRIPT_DIR] = str(tmp_path / "transcripts")
    for name in ("guilds/1/2026-10-07", "2026-10-07"):
        path = tmp_path / "transcripts" / name / "voice_transcripts.log"
        path.parent.mkdir(parents=True)
        path.write_text(
            "Voice transcript guild=1 channel=10 user=42: secret\ncontinuation\nVoice transcript guild=1 channel=11 user=42: keep\nkeep more\n"
        )
    voice_transcriber.purge_channel_transcripts(ctx, "10")
    for path in (tmp_path / "transcripts").rglob("voice_transcripts.log"):
        assert path.read_text() == "Voice transcript guild=1 channel=11 user=42: keep\nkeep more\n"
