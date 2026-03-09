import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from servant.defs import GlobalContext, RequestContext
from servant.modules import background_indexer, indexed_message_search
from typed_json import JSON, JSONDict


def _insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    channel_id: str,
    guild_id: str | None,
    author_id: str,
    content: str | None,
    created_at: int,
    deleted_at: int | None = None,
    content_available: int | None = None,
) -> None:
    if content_available is None:
        content_available = 0 if content is None else 1
    conn.execute(
        """
        INSERT INTO messages (
            message_id,
            channel_id,
            guild_id,
            author_id,
            content,
            content_available,
            created_at,
            edited_at,
            deleted_at,
            reply_to_message_id,
            reply_to_channel_id,
            reply_to_guild_id,
            mention_ids,
            mention_everyone,
            attachments_count,
            embeds_count,
            pinned
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            channel_id,
            guild_id,
            author_id,
            content,
            content_available,
            created_at,
            None,
            deleted_at,
            None,
            None,
            None,
            json.dumps([], ensure_ascii=True),
            0,
            0,
            0,
            0,
        ),
    )


def _seed_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    background_indexer._init_db(conn)

    conn.execute("INSERT INTO guilds (guild_id, name) VALUES (?, ?)", ("1", "Guild"))
    conn.execute(
        "INSERT INTO channels (channel_id, guild_id, name, type) VALUES (?, ?, ?, ?)",
        ("10", "1", "general", "text"),
    )
    conn.execute(
        "INSERT INTO channels (channel_id, guild_id, name, type) VALUES (?, ?, ?, ?)",
        ("11", "1", "random", "text"),
    )
    conn.execute(
        "INSERT INTO users (user_id, name, global_name, bot) VALUES (?, ?, ?, ?)",
        ("100", "alice", "Alice", 0),
    )
    conn.execute(
        "INSERT INTO users (user_id, name, global_name, bot) VALUES (?, ?, ?, ?)",
        ("101", "bob", "Bob", 0),
    )

    _insert_message(
        conn,
        message_id="200",
        channel_id="10",
        guild_id="1",
        author_id="100",
        content="Hello there",
        created_at=1_000,
    )
    _insert_message(
        conn,
        message_id="201",
        channel_id="10",
        guild_id="1",
        author_id="101",
        content="bye now",
        created_at=2_000,
    )
    _insert_message(
        conn,
        message_id="202",
        channel_id="11",
        guild_id="1",
        author_id="100",
        content="HELLO friend",
        created_at=3_000,
    )
    _insert_message(
        conn,
        message_id="203",
        channel_id="11",
        guild_id="1",
        author_id="100",
        content="hidden",
        created_at=4_000,
        deleted_at=4_500,
    )

    conn.commit()
    conn.close()


def _get_results(result: JSONDict) -> list[JSONDict]:
    results = result.get("results")
    if not isinstance(results, list):
        raise AssertionError("expected results list")
    output: list[JSONDict] = []
    for item in results:
        if not isinstance(item, dict):
            raise AssertionError("expected result item object")
        output.append(item)
    return output


def test_indexed_messages_search_substring_filters(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    _seed_db(db_path)
    admin_ids: list[JSON] = ["admin"]
    ctx = GlobalContext(config={"indexer_db_path": str(db_path), "admin_user_ids": admin_ids}).with_request(
        RequestContext(user_id="admin", channel_id="10", guild_id="1", is_dm=False)
    )

    result = asyncio.run(
        indexed_message_search.indexed_messages_search(
            ctx,
            {"query": "hello", "guild_id": "1"},
        )
    )

    assert result["ok"] is True
    assert result["messages_returned"] == 2
    results = _get_results(result)
    assert results[0]["message_id"] == "202"
    assert results[0]["channel_name"] == "random"
    assert results[1]["message_id"] == "200"
    assert results[1]["channel_name"] == "general"


def test_indexed_messages_search_case_sensitive_and_deleted(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    _seed_db(db_path)
    admin_ids: list[JSON] = ["admin"]
    ctx = GlobalContext(config={"indexer_db_path": str(db_path), "admin_user_ids": admin_ids}).with_request(
        RequestContext(user_id="admin", channel_id="10", guild_id="1", is_dm=False)
    )

    result = asyncio.run(
        indexed_message_search.indexed_messages_search(
            ctx,
            {"query": "Hello", "case_sensitive": True, "channel_id": "10"},
        )
    )
    assert result["messages_returned"] == 1
    results = _get_results(result)
    assert results[0]["message_id"] == "200"

    result = asyncio.run(
        indexed_message_search.indexed_messages_search(
            ctx,
            {"query": "hidden", "include_deleted": False},
        )
    )
    assert result["messages_returned"] == 0

    result = asyncio.run(
        indexed_message_search.indexed_messages_search(
            ctx,
            {"query": "hidden", "include_deleted": True},
        )
    )
    assert result["messages_returned"] == 1
    results = _get_results(result)
    assert results[0]["message_id"] == "203"


def test_indexed_messages_search_requires_admin(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    _seed_db(db_path)
    ctx = GlobalContext(config={"indexer_db_path": str(db_path)}).with_request(
        RequestContext(user_id="u1", channel_id="10", guild_id="1", is_dm=False)
    )
    with pytest.raises(PermissionError):
        asyncio.run(
            indexed_message_search.indexed_messages_search(
                ctx,
                {"query": "hello", "guild_id": "1"},
            )
        )


def test_indexed_messages_search_regex_scan(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    _seed_db(db_path)
    admin_ids: list[JSON] = ["admin"]
    ctx = GlobalContext(config={"indexer_db_path": str(db_path), "admin_user_ids": admin_ids}).with_request(
        RequestContext(user_id="admin", channel_id="10", guild_id="1", is_dm=False)
    )

    result = asyncio.run(
        indexed_message_search.indexed_messages_search(
            ctx,
            {"query": "h.llo", "match": "regex", "scan_limit": 10},
        )
    )

    assert result["messages_returned"] == 2
    rows_scanned = result.get("rows_scanned")
    assert isinstance(rows_scanned, int)
    assert rows_scanned >= 2
