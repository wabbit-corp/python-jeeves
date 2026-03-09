from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from openai import AsyncOpenAI

import servant_app
from servant.defs import GlobalContext


class _NullTypingIndicator:
    def __init__(self, *args: object, **kwargs: object) -> None:
        return None

    async def __aenter__(self) -> _NullTypingIndicator:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> None:
        return None


class _FakeMember:
    def __init__(self, *, user_id: int, name: str, permissions: discord.Permissions) -> None:
        self.id = user_id
        self.name = name
        self.guild_permissions = permissions


class _FakeGuild:
    def __init__(self, members: dict[int, object] | None = None) -> None:
        self._members = members or {}

    def get_member(self, user_id: int) -> object | None:
        return self._members.get(user_id)


def test_build_author_payload_includes_staff_flags_for_member() -> None:
    ctx = GlobalContext()
    member = _FakeMember(
        user_id=456,
        name="alice",
        permissions=discord.Permissions(manage_messages=True),
    )

    payload = servant_app._build_author_payload(ctx, member, guild=None)

    assert payload == {
        "id": "456",
        "name": "alice",
        "mention": "<@456:alice>",
        "permissions": {
            "staff": True,
            "admin": False,
        },
    }


def test_build_author_payload_uses_cached_guild_member_permissions() -> None:
    ctx = GlobalContext()
    author = SimpleNamespace(id=789, name="bob")
    guild = _FakeGuild(
        members={
            789: _FakeMember(
                user_id=789,
                name="bob",
                permissions=discord.Permissions(administrator=True),
            )
        }
    )

    payload = servant_app._build_author_payload(ctx, author, guild=guild)

    assert payload == {
        "id": "789",
        "name": "bob",
        "mention": "<@789:bob>",
        "permissions": {
            "staff": True,
            "admin": True,
        },
    }


def test_build_author_payload_includes_global_admin_without_guild_permissions() -> None:
    ctx = GlobalContext(config={"admin_user_ids": ["123"]})
    author = SimpleNamespace(id=123, name="carol")

    payload = servant_app._build_author_payload(ctx, author, guild=None)

    assert payload == {
        "id": "123",
        "name": "carol",
        "mention": "<@123:carol>",
        "permissions": {
            "staff": True,
            "admin": True,
            "global_admin": True,
        },
    }


def test_flatten_config_values_preserves_top_level_and_dotted_keys() -> None:
    flattened = servant_app._flatten_config_values(
        {
            "web": {
                "user-agent": "vox",
                "fetch": {
                    "blocked_hosts": ["example.com"],
                },
            },
            "admin_user_ids": ["123"],
        }
    )

    assert flattened["web"] == {
        "user-agent": "vox",
        "fetch": {"blocked_hosts": ["example.com"]},
    }
    assert flattened["web.user-agent"] == "vox"
    assert flattened["web.fetch"] == {"blocked_hosts": ["example.com"]}
    assert flattened["web.fetch.blocked_hosts"] == ["example.com"]
    assert flattened["admin_user_ids"] == ["123"]


def test_handle_incoming_message_uses_supplied_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(servant_app, "TypingIndicator", _NullTypingIndicator)
    monkeypatch.setattr(servant_app, "reply", AsyncMock(return_value=None))

    ctx = GlobalContext()
    openai_client = AsyncOpenAI(api_key="test")
    create_mock = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="hello", tool_calls=None),
                )
            ]
        )
    )
    monkeypatch.setattr(openai_client.chat.completions, "create", create_mock)

    channel = Mock()
    channel.id = 789
    channel.guild = SimpleNamespace(id=321)
    author = Mock()
    author.id = 456
    author.name = "alice"
    message = servant_app._SyntheticMessage(
        channel=channel,
        author=author,
        message_id=123,
        content="hello",
    )

    client = discord.Client(intents=discord.Intents.none())
    try:
        asyncio.run(
            servant_app.handle_incoming_message(
                ctx=ctx,
                client=client,
                discord_message=message,
                openai_client=openai_client,
                reasoning_effort="minimal",
            )
        )
    finally:
        asyncio.run(openai_client.close())
        asyncio.run(client.close())

    assert create_mock.await_args is not None
    assert create_mock.await_args.kwargs["reasoning_effort"] == "minimal"
