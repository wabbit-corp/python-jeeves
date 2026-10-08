from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp import ClientResponse

import servant_app
from servant import llm_throttling, voice_transcriber
from servant.defs import CancelHandle, GlobalContext
from servant.modules import background_indexer, commitment, topic_subscriptions
from typed_json import JSON, JSONDict


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<@123> help", True),
        ("<@!123> help", True),
        ("<@456> help", False),
        ("<@1234> help", False),
        ("vox help", True),
        ("VOX, help", True),
        ("v help", True),
        ("V, help", True),
        ("convex", False),
        ("https://example.com?v=1", False),
        ("v.test", False),
        ("", False),
    ],
)
def test_addressing_uses_account_id_and_preserves_names(content: str, expected: bool) -> None:
    assert servant_app._message_addresses_bot(content, bot_user_id=123, personality_name="Vox") is expected


def test_vox_aliases_remain_available_with_a_different_personality() -> None:
    for content in ("vox help", "v help", "Jeeves help", "J help", "<@123> help"):
        assert servant_app._message_addresses_bot(content, bot_user_id=123, personality_name="Jeeves")
    assert not servant_app._message_addresses_bot("anything", bot_user_id=None, personality_name="")


def test_default_intents_preserve_name_triggers_without_member_or_presence_access() -> None:
    intents = servant_app._build_discord_intents({})
    assert intents.message_content
    assert not intents.members
    assert not intents.presences
    assert intents.guild_messages and intents.dm_messages and intents.voice_states


def test_privileged_intents_can_be_disabled_even_when_configured() -> None:
    config: JSONDict = {"discord.message_content_intent": True, "discord.members_intent": True}
    intents = servant_app._build_discord_intents(config, allow_privileged_intents=False)
    assert not (intents.message_content or intents.members or intents.presences)
    assert servant_app._build_discord_intents(config).members
    assert not servant_app._build_discord_intents({"discord.message_content_intent": False}).message_content


@pytest.mark.parametrize("value", ["false", 0, None])
def test_invalid_intent_flags_fail_instead_of_enabling_access(value: JSON) -> None:
    with pytest.raises(ValueError, match="must be a YAML boolean"):
        servant_app._build_discord_intents({"discord.members_intent": value})


def _prepare_main(
    monkeypatch: pytest.MonkeyPatch, *, message_content: bool = True
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    monkeypatch.setattr(
        servant_app,
        "load_yaml_config",
        lambda _path: {
            "discord": {"token": "test", "message_content_intent": message_content},
            "openai": {"key": "test"},
        },
    )
    monkeypatch.setattr(servant_app, "discover_modules", lambda: {})
    ctx = servant_app.GlobalContext()
    ctx.guild_retention.ready.set()
    ctx.channel_controls.loaded = True
    monkeypatch.setattr(servant_app, "GlobalContext", lambda: ctx)

    async def wait_until_ready(_client: discord.Client) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(discord.Client, "wait_until_ready", wait_until_ready)
    openai_client = AsyncMock()
    monkeypatch.setattr(servant_app, "AsyncOpenAI", lambda **_kwargs: openai_client)
    monkeypatch.setattr(background_indexer, "init_db", AsyncMock())
    record_message = AsyncMock()
    monkeypatch.setattr(background_indexer, "record_message_create", record_message)
    monkeypatch.setattr(topic_subscriptions, "topic_subscriptions_handle_message", AsyncMock())
    monkeypatch.setattr(voice_transcriber, "maybe_handle_voice_invite", AsyncMock())
    monkeypatch.setattr(
        llm_throttling,
        "resolve_reasoning_effort_for_message",
        AsyncMock(return_value=SimpleNamespace(reasoning_effort="low")),
    )
    handle_message = AsyncMock()
    monkeypatch.setattr(servant_app, "handle_incoming_message", handle_message)
    return openai_client, handle_message, record_message


@pytest.mark.parametrize(
    ("content", "is_dm", "message_content", "expected_calls"),
    [
        ("<@123> help", False, False, 1),
        ("<@!123> help", False, False, 1),
        ("<@123> help <@456>", False, False, 1),
        ("vox help", False, True, 1),
        ("v help", False, True, 1),
        ("help", True, False, 1),
        ("ordinary chatter", False, True, 0),
        ("", False, False, 0),
    ],
)
def test_main_message_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    is_dm: bool,
    message_content: bool,
    expected_calls: int,
) -> None:
    openai_client, handle_message, record_message = _prepare_main(monkeypatch, message_content=message_content)
    bot = SimpleNamespace(id=123, name="renamed_bot")
    monkeypatch.setattr(discord.Client, "user", property(lambda _self: bot))
    response = MagicMock(spec=ClientResponse, status=404, reason="Not Found")
    fetch_user = AsyncMock(side_effect=discord.HTTPException(response, "unknown user"))
    monkeypatch.setattr(discord.Client, "fetch_user", fetch_user)
    close = AsyncMock()
    monkeypatch.setattr(discord.Client, "close", close)
    message = SimpleNamespace(
        id=789,
        author=SimpleNamespace(id=456, name="alice"),
        channel=SimpleNamespace(id=999),
        guild=None if is_dm else SimpleNamespace(id=321),
        content=content,
        reference=None,
    )

    async def start(client: discord.Client, _token: str, *, reconnect: bool) -> None:
        assert reconnect
        await getattr(client, "on_message")(message)

    monkeypatch.setattr(discord.Client, "start", start)
    asyncio.run(servant_app.main())
    assert handle_message.await_count == expected_calls
    if "<@456>" in content:
        fetch_user.assert_awaited_once_with(456)
    else:
        fetch_user.assert_not_awaited()
    close.assert_awaited_once()
    openai_client.close.assert_awaited_once()
    if not content:
        record_message.assert_not_awaited()


@pytest.mark.parametrize("reject_fallback", [False, True])
def test_intent_rejection_reconnects_once_and_cleans_up(monkeypatch: pytest.MonkeyPatch, reject_fallback: bool) -> None:
    openai_client, _, _ = _prepare_main(monkeypatch)
    attempts: list[discord.Intents] = []
    close = AsyncMock()
    monkeypatch.setattr(discord.Client, "close", close)

    async def start(client: discord.Client, _token: str, *, reconnect: bool) -> None:
        attempts.append(client.intents)
        if len(attempts) == 1 or reject_fallback:
            raise discord.PrivilegedIntentsRequired(None)

    monkeypatch.setattr(discord.Client, "start", start)
    if reject_fallback:
        with pytest.raises(discord.PrivilegedIntentsRequired):
            asyncio.run(servant_app.main())
    else:
        asyncio.run(servant_app.main())
    assert len(attempts) == 2
    assert attempts[0].message_content
    assert not (attempts[1].message_content or attempts[1].members or attempts[1].presences)
    assert close.await_count == 2
    assert openai_client.close.await_count == 2


def test_login_failure_does_not_trigger_intent_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    openai_client, _, _ = _prepare_main(monkeypatch)
    start = AsyncMock(side_effect=discord.LoginFailure("invalid test credentials"))
    monkeypatch.setattr(discord.Client, "start", start)
    monkeypatch.setattr(discord.Client, "close", AsyncMock())
    with pytest.raises(discord.LoginFailure):
        asyncio.run(servant_app.main())
    start.assert_awaited_once()
    openai_client.close.assert_awaited_once()


def test_roster_polling_skips_requests_without_member_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = discord.Client(intents=discord.Intents.none())
    database = AsyncMock(side_effect=AssertionError("No roster database work expected"))
    monkeypatch.setattr(background_indexer, "_with_db", database)
    assert asyncio.run(background_indexer._index_next_guild_members(GlobalContext(), client, 100)) == {
        "members_indexed": 0
    }
    database.assert_not_awaited()


def test_passive_indexing_skips_requests_without_message_content(monkeypatch: pytest.MonkeyPatch) -> None:
    client = discord.Client(intents=discord.Intents.none())
    monkeypatch.setattr(client, "wait_until_ready", AsyncMock())
    sync = AsyncMock(side_effect=AssertionError("No passive indexing work expected"))
    monkeypatch.setattr(background_indexer, "_sync_guilds_and_channels", sync)
    result = asyncio.run(background_indexer.background_indexer_routine(GlobalContext(discord_client=client), {}))
    assert result == {"ok": True, "messages_indexed": 0, "members_indexed": 0}
    sync.assert_not_awaited()


def test_slash_opt_out_clears_context_cancels_work_and_blocks_all_future_invocations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai_client, handle_message, record_message = _prepare_main(monkeypatch, message_content=False)
    ctx = GlobalContext()
    ctx.guild_retention.ready.set()
    monkeypatch.setattr(servant_app, "GlobalContext", lambda: ctx)
    persist = AsyncMock()
    purge_topics = AsyncMock()
    purge_commitments = AsyncMock()
    monkeypatch.setattr(background_indexer, "confirm_privacy_opt_out", persist)
    monkeypatch.setattr(topic_subscriptions, "purge_user_data", purge_topics)
    monkeypatch.setattr(commitment, "purge_user_data", purge_commitments)
    monkeypatch.setattr(discord.Client, "user", property(lambda _self: SimpleNamespace(id=123, name="Vox")))
    monkeypatch.setattr(discord.Client, "close", AsyncMock())

    async def start(client: discord.Client, _token: str, *, reconnect: bool) -> None:
        assert reconnect
        group = getattr(client, "tree").get_command("vox")
        command = group.get_command("optout")
        assert command is not None
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = SimpleNamespace(id=42)
        interaction.response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
        interaction.edit_original_response = AsyncMock()
        ctx.channel_messages["10"].append({"content": "private previous context"})

        async def wait_for_cancel() -> None:
            await asyncio.Event().wait()

        pending = asyncio.create_task(wait_for_cancel())
        handle = CancelHandle("100", "42", asyncio.Event(), pending)
        ctx.pending_cancels["100"] = handle
        await command.callback(interaction)
        assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
        assert not ctx.privacy.is_opted_out(42)
        view = interaction.response.send_message.call_args.kwargs["view"]
        await view.confirm_button.callback(interaction)
        assert ctx.privacy.is_opted_out(42)
        assert not ctx.channel_messages
        assert handle.cancel_event.is_set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        persist.assert_awaited_once_with(ctx, "42")
        purge_topics.assert_awaited_once_with(ctx, "42")
        purge_commitments.assert_awaited_once_with(ctx, "42")
        for content, guild in (
            ("<@123> help", SimpleNamespace(id=1)),
            ("vox help", SimpleNamespace(id=1)),
            ("help", None),
        ):
            message = SimpleNamespace(id=200, author=SimpleNamespace(id=42), content=content, guild=guild)
            await getattr(client, "on_message")(message)
        record_message.assert_not_awaited()
        handle_message.assert_not_awaited()
        # Replies to a withdrawn user's messages must not include their content.
        referenced = SimpleNamespace(id=100, author=SimpleNamespace(id=42), guild=None, content="withdrawn content")
        monkeypatch.setattr(client, "_get_referenced_message", AsyncMock(return_value=referenced))
        other_message = SimpleNamespace(
            id=201,
            author=SimpleNamespace(id=99, name="Bob"),
            guild=None,
            reference=SimpleNamespace(message_id=100),
            channel=SimpleNamespace(id=10),
        )
        payload = await getattr(client, "_build_user_message_content")(other_message, "hello")
        assert "withdrawn content" not in payload

    monkeypatch.setattr(discord.Client, "start", start)
    asyncio.run(servant_app.main())
    openai_client.close.assert_awaited_once()


def test_slash_registration_upserts_only_vox(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_main(monkeypatch, message_content=False)
    monkeypatch.setattr(discord.Client, "application_id", property(lambda _self: 123))
    monkeypatch.setattr(discord.Client, "close", AsyncMock())
    request = AsyncMock(return_value={"id": "1"})
    monkeypatch.setattr(discord.http.HTTPClient, "request", request)

    async def start(client: discord.Client, _token: str, *, reconnect: bool) -> None:
        await client.setup_hook()

    monkeypatch.setattr(discord.Client, "start", start)
    asyncio.run(servant_app.main())
    request.assert_awaited_once()
    route = request.call_args.args[0]
    assert route.method == "POST"
    assert route.path == "/applications/{application_id}/commands"
    payload = request.call_args.kwargs["json"]
    assert payload["name"] == "vox"
    assert payload["options"][0]["name"] == "optout"


@pytest.mark.parametrize("content", ["vox I would like to opt-out of processing of my data", "<@123> opt out"])
def test_text_opt_out_redirects_before_indexing_or_model_processing(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    _, handle_message, record_message = _prepare_main(monkeypatch)
    monkeypatch.setattr(discord.Client, "user", property(lambda _self: SimpleNamespace(id=123, name="Vox")))
    monkeypatch.setattr(discord.Client, "close", AsyncMock())
    channel = SimpleNamespace(id=10, send=AsyncMock())
    message = SimpleNamespace(author=SimpleNamespace(id=42), content=content, channel=channel, guild=None)

    async def start(client: discord.Client, _token: str, *, reconnect: bool) -> None:
        await getattr(client, "on_message")(message)

    monkeypatch.setattr(discord.Client, "start", start)
    asyncio.run(servant_app.main())
    assert "/vox optout" in channel.send.call_args.args[0]
    record_message.assert_not_awaited()
    handle_message.assert_not_awaited()


def test_failed_save_can_be_retried_through_a_new_slash_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_main(monkeypatch)
    ctx = GlobalContext()
    ctx.guild_retention.ready.set()
    monkeypatch.setattr(servant_app, "GlobalContext", lambda: ctx)
    persist = AsyncMock(side_effect=[OSError("database unavailable"), None])
    monkeypatch.setattr(background_indexer, "confirm_privacy_opt_out", persist)
    monkeypatch.setattr(topic_subscriptions, "purge_user_data", AsyncMock())
    monkeypatch.setattr(commitment, "purge_user_data", AsyncMock())
    monkeypatch.setattr(discord.Client, "close", AsyncMock())

    async def start(client: discord.Client, _token: str, *, reconnect: bool) -> None:
        command = getattr(client, "tree").get_command("vox").get_command("optout")
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = SimpleNamespace(id=42)
        interaction.response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
        interaction.edit_original_response = AsyncMock()
        await command.callback(interaction)
        first = interaction.response.send_message.call_args.kwargs["view"]
        await first.confirm_button.callback(interaction)
        assert ctx.privacy.is_opted_out(42)
        assert ctx.privacy.pending_opt_outs == {"42"}
        first.stop()
        await command.callback(interaction)
        retry = interaction.response.send_message.call_args.kwargs["view"]
        assert retry.cancel_button.disabled
        await retry.confirm_button.callback(interaction)
        assert not ctx.privacy.pending_opt_outs
        assert persist.await_count == 2
        assert interaction.edit_original_response.call_args.kwargs["embed"].title == "You have opted out"

    monkeypatch.setattr(discord.Client, "start", start)
    asyncio.run(servant_app.main())


@pytest.mark.parametrize("opt_out_during", ["fetch", "first_chunk"])
def test_queued_background_send_stops_when_privacy_changes(
    monkeypatch: pytest.MonkeyPatch, opt_out_during: str
) -> None:
    _prepare_main(monkeypatch)
    ctx = GlobalContext()
    ctx.guild_retention.ready.set()
    monkeypatch.setattr(servant_app, "GlobalContext", lambda: ctx)
    monkeypatch.setattr(discord.Client, "close", AsyncMock())
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 10
    channel.guild = SimpleNamespace(id=1)
    ctx.channel_controls.loaded = True

    async def fetch(_channel_id: int) -> MagicMock:
        if opt_out_during == "fetch":
            ctx.privacy.generation += 1
        return channel

    async def send(_content: str) -> None:
        ctx.privacy.generation += 1

    channel.send = AsyncMock(side_effect=send)

    async def start(client: discord.Client, _token: str, *, reconnect: bool) -> None:
        monkeypatch.setattr(client, "wait_until_ready", AsyncMock())
        monkeypatch.setattr(client, "fetch_channel", AsyncMock(side_effect=fetch))
        assert ctx.send_discord_message is not None
        await ctx.send_discord_message("10", "x" * 4001)
        assert not ctx.channel_messages

    monkeypatch.setattr(discord.Client, "start", start)
    asyncio.run(servant_app.main())
    assert channel.send.await_count == (0 if opt_out_during == "fetch" else 1)


def test_server_removal_and_ready_reconciliation_are_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_main(monkeypatch)
    remove = AsyncMock()
    reconcile = AsyncMock()
    monkeypatch.setattr(servant_app.guild_retention, "remove_guild", remove)
    monkeypatch.setattr(servant_app.guild_retention, "reconcile", reconcile)
    monkeypatch.setattr(discord.Client, "close", AsyncMock())
    guild = SimpleNamespace(id=321, channels=[SimpleNamespace(id=10)], threads=[SimpleNamespace(id=11)])

    async def start(client: discord.Client, _token: str, *, reconnect: bool) -> None:
        await getattr(client, "on_ready")()
        assert callable(reconcile.call_args.args[1])
        # No handler turns a temporary guild outage into deletion.
        assert not hasattr(client, "on_guild_unavailable")
        assert not client.cached_messages
        await getattr(client, "on_guild_remove")(guild)

    monkeypatch.setattr(discord.Client, "start", start)
    asyncio.run(servant_app.main())
    assert remove.call_args.args[1] == "321"
    assert set(remove.call_args.args[2]) == {"10", "11"}
    reconcile.assert_awaited_once()
