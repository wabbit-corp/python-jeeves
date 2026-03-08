import asyncio
import datetime as dt
import sqlite3
import threading
import time
from pathlib import Path

import discord
import pytest
from hypothesis import given
from hypothesis import strategies as st

from servant.defs import GlobalContext, RequestContext
from servant.modules import commitment
from typed_json import JSON, JSONDict, require_obj


def _ctx_with_db(
    tmp_path: Path,
    *,
    user_id: str = "u1",
    channel_id: str = "c1",
    guild_id: str | None = None,
    admin_user_ids: list[str] | None = None,
) -> GlobalContext:
    config: JSONDict = {"commitments_db_path": str(tmp_path / "commitments.sqlite3")}
    if admin_user_ids is not None:
        admin_ids_json: list[JSON] = [str(item) for item in admin_user_ids]
        config["admin_user_ids"] = admin_ids_json
    ctx = GlobalContext(config=config)
    request = RequestContext(
        user_id=user_id,
        channel_id=channel_id,
        guild_id=guild_id,
        is_dm=guild_id is None,
    )
    return ctx.with_request(request)


def _row_from_dict(columns: dict[str, object]) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        column_defs = ", ".join(f"{name} TEXT" for name in columns)
        conn.execute(f"CREATE TABLE t ({column_defs});")
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO t ({', '.join(columns)}) VALUES ({placeholders});",
            tuple(columns.values()),
        )
        row: sqlite3.Row = conn.execute("SELECT * FROM t").fetchone()
        assert row is not None
        return row
    finally:
        conn.close()


class _FakeMember:
    def __init__(self, user_id: int, permissions: discord.Permissions) -> None:
        self.id = user_id
        self.guild_permissions = permissions
        self.roles: list[object] = []


class _FakeGuild:
    def __init__(self, guild_id: int, member: _FakeMember) -> None:
        self.id = guild_id
        self._member = member

    def get_member(self, user_id: int) -> _FakeMember | None:
        if user_id == self._member.id:
            return self._member
        return None

    async def fetch_member(self, user_id: int) -> _FakeMember | None:
        return self.get_member(user_id)


class _FakeChannel:
    def __init__(self, channel_id: int, guild: _FakeGuild) -> None:
        self.id = channel_id
        self.guild = guild


class _FakeDiscordClient:
    def __init__(self, guild: _FakeGuild, channel: _FakeChannel) -> None:
        self._guild = guild
        self._channel = channel

    def get_guild(self, guild_id: int) -> _FakeGuild | None:
        if guild_id == self._guild.id:
            return self._guild
        return None

    async def fetch_guild(self, guild_id: int) -> _FakeGuild | None:
        return self.get_guild(guild_id)

    def get_channel(self, channel_id: int) -> _FakeChannel | None:
        if channel_id == self._channel.id:
            return self._channel
        return None

    async def fetch_channel(self, channel_id: int) -> _FakeChannel | None:
        return self.get_channel(channel_id)


def _attach_staff_client(
    ctx: GlobalContext,
    *,
    guild_id: str,
    channel_id: str,
    user_id: str,
    permissions: discord.Permissions,
) -> None:
    member = _FakeMember(int(user_id), permissions)
    guild = _FakeGuild(int(guild_id), member)
    channel = _FakeChannel(int(channel_id), guild)
    ctx.__dict__["discord_client"] = _FakeDiscordClient(guild, channel)


@given(st.integers(min_value=-(10**12), max_value=10**12))
def test_parse_epoch_ms_accepts_int(value: int) -> None:
    assert commitment._parse_epoch_ms(value) == value


@given(st.integers(min_value=-(10**12), max_value=10**12))
def test_parse_epoch_ms_accepts_numeric_string(value: int) -> None:
    assert commitment._parse_epoch_ms(str(value)) == value


@given(st.floats(min_value=-(10**9), max_value=10**9, allow_nan=False, allow_infinity=False))
def test_parse_epoch_ms_accepts_float(value: float) -> None:
    assert commitment._parse_epoch_ms(value) == int(value)


@given(st.booleans())
def test_parse_epoch_ms_accepts_bool(value: bool) -> None:
    assert commitment._parse_epoch_ms(value) == value


def test_parse_epoch_ms_parses_iso_z() -> None:
    expected = int(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert commitment._parse_epoch_ms("2024-01-01T00:00:00Z") == expected


def test_parse_epoch_ms_parses_iso_offset() -> None:
    expected = int(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert commitment._parse_epoch_ms("2024-01-01T03:00:00+03:00") == expected


def test_parse_epoch_ms_rejects_blank_and_invalid() -> None:
    assert commitment._parse_epoch_ms("   ") is None
    assert commitment._parse_epoch_ms("nope") is None


def test_require_int_validation() -> None:
    assert commitment._require_int({"value": "42"}, "value") == 42
    with pytest.raises(ValueError):
        commitment._require_int({}, "missing")
    with pytest.raises(ValueError):
        commitment._require_int({"value": "nope"}, "value")


@given(st.floats(min_value=-(10**9), max_value=10**9, allow_nan=False, allow_infinity=False))
def test_require_int_accepts_float(value: float) -> None:
    assert commitment._require_int({"value": value}, "value") == int(value)


@given(st.booleans())
def test_require_int_accepts_bool(value: bool) -> None:
    assert commitment._require_int({"value": value}, "value") == int(value)


@given(st.integers(min_value=1, max_value=30), st.integers(min_value=1, max_value=30))
def test_should_checkin_interval(days_ago: int, interval_days: int) -> None:
    today = dt.date(2024, 1, 31)
    last_checkin = (today - dt.timedelta(days=days_ago)).isoformat()
    assert commitment._should_checkin(today, last_checkin, interval_days) is (days_ago >= interval_days)


def test_should_checkin_logic() -> None:
    today = dt.date(2024, 1, 10)
    assert commitment._should_checkin(today, None, 5)
    assert commitment._should_checkin(today, "nope", 5)
    assert not commitment._should_checkin(today, "2024-01-08", 5)
    assert commitment._should_checkin(today, "2024-01-04", 5)


def test_commitment_manage_crud(tmp_path: Path) -> None:
    ctx = _ctx_with_db(tmp_path)
    create_payload: JSONDict = {
        "operation": "create",
        "name": "Run",
        "description": "Daily",
        "user_id": "u1",
        "channel_id": "c1",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
        "interval_days": 3,
    }
    result = asyncio.run(commitment.commitment_manage(ctx, create_payload))
    assert result["ok"] is True
    commitment_payload = require_obj(result["commitment"])
    commitment_id = commitment_payload["id"]
    assert isinstance(commitment_id, int)
    assert commitment_payload["status"] == "active"

    update_payload: JSONDict = {
        "operation": "update",
        "commitment_id": commitment_id,
        "description": "Daily 5k",
        "end_date": "2024-02-01",
    }
    result = asyncio.run(commitment.commitment_manage(ctx, update_payload))
    updated_payload = require_obj(result["commitment"])
    assert updated_payload["description"] == "Daily 5k"
    assert updated_payload["end_date"] == "2024-02-01"

    cancel_payload: JSONDict = {"operation": "cancel", "commitment_id": commitment_id}
    result = asyncio.run(commitment.commitment_manage(ctx, cancel_payload))
    cancelled_payload = require_obj(result["commitment"])
    assert cancelled_payload["status"] == "cancelled"

    list_payload: JSONDict = {"operation": "list", "user_id": "u1"}
    result = asyncio.run(commitment.commitment_manage(ctx, list_payload))
    commitments_value = result["commitments"]
    assert isinstance(commitments_value, list)
    assert len(commitments_value) == 1
    first = commitments_value[0]
    assert isinstance(first, dict)
    assert first["id"] == commitment_id


def test_commitment_manage_defaults_end_date(tmp_path: Path) -> None:
    ctx = _ctx_with_db(tmp_path)
    create_payload: JSONDict = {
        "operation": "create",
        "name": "Stretch",
        "description": "Morning routine",
        "user_id": "u1",
        "channel_id": "c1",
        "start_date": "2024-01-01",
    }
    result = asyncio.run(commitment.commitment_manage(ctx, create_payload))
    commitment_payload = require_obj(result["commitment"])
    assert commitment_payload["end_date"] == "2024-01-31"


def test_commitment_notes_manage_set_append_clear(tmp_path: Path) -> None:
    ctx = _ctx_with_db(tmp_path)
    create_payload: JSONDict = {
        "operation": "create",
        "name": "Journal",
        "description": "Daily reflection",
        "user_id": "u1",
        "channel_id": "c1",
        "start_date": "2024-01-01",
        "end_date": "2024-01-15",
    }
    create_result = asyncio.run(commitment.commitment_manage(ctx, create_payload))
    commitment_payload = require_obj(create_result["commitment"])
    commitment_id = commitment_payload["id"]

    set_payload: JSONDict = {
        "operation": "set",
        "commitment_id": commitment_id,
        "notes": "Week 1: kept a 3-day streak.",
    }
    result = asyncio.run(commitment.commitment_notes_manage(ctx, set_payload))
    updated_payload = require_obj(result["commitment"])
    assert updated_payload["notes"] == "Week 1: kept a 3-day streak."

    append_payload: JSONDict = {
        "operation": "append",
        "commitment_id": commitment_id,
        "notes": "Week 2: missed one day.",
    }
    result = asyncio.run(commitment.commitment_notes_manage(ctx, append_payload))
    updated_payload = require_obj(result["commitment"])
    assert updated_payload["notes"] == "Week 1: kept a 3-day streak.\nWeek 2: missed one day."

    clear_payload: JSONDict = {"operation": "clear", "commitment_id": commitment_id}
    result = asyncio.run(commitment.commitment_notes_manage(ctx, clear_payload))
    updated_payload = require_obj(result["commitment"])
    assert updated_payload["notes"] is None


def test_commitment_manage_update_rejects_bad_dates(tmp_path: Path) -> None:
    ctx = _ctx_with_db(tmp_path)
    create_payload: JSONDict = {
        "operation": "create",
        "name": "Read",
        "description": "Chapter 1",
        "user_id": "u1",
        "channel_id": "c1",
        "start_date": "2024-01-10",
        "end_date": "2024-01-20",
    }
    create_result = asyncio.run(commitment.commitment_manage(ctx, create_payload))
    commitment_payload = require_obj(create_result["commitment"])
    commitment_id = commitment_payload["id"]
    assert isinstance(commitment_id, int)

    bad_update: JSONDict = {"operation": "update", "commitment_id": commitment_id, "end_date": "2024-01-05"}
    with pytest.raises(ValueError):
        asyncio.run(commitment.commitment_manage(ctx, bad_update))


def test_commitment_manage_requires_admin_for_other_user_create(tmp_path: Path) -> None:
    ctx = _ctx_with_db(tmp_path, user_id="u1")
    create_payload: JSONDict = {
        "operation": "create",
        "name": "Run",
        "description": "Daily",
        "user_id": "u2",
        "channel_id": "c1",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
    }
    with pytest.raises(PermissionError):
        asyncio.run(commitment.commitment_manage(ctx, create_payload))


def test_commitment_manage_requires_admin_for_other_user_update(tmp_path: Path) -> None:
    admin_ctx = _ctx_with_db(tmp_path, user_id="admin", admin_user_ids=["admin"])
    create_payload: JSONDict = {
        "operation": "create",
        "name": "Run",
        "description": "Daily",
        "user_id": "u2",
        "channel_id": "c1",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
    }
    create_result = asyncio.run(commitment.commitment_manage(admin_ctx, create_payload))
    commitment_payload = require_obj(create_result["commitment"])
    commitment_id = commitment_payload["id"]

    ctx = _ctx_with_db(tmp_path, user_id="u1")
    update_payload: JSONDict = {
        "operation": "update",
        "commitment_id": commitment_id,
        "description": "Updated",
    }
    with pytest.raises(PermissionError):
        asyncio.run(commitment.commitment_manage(ctx, update_payload))


def test_commitment_manage_requires_admin_for_other_user_list(tmp_path: Path) -> None:
    ctx = _ctx_with_db(tmp_path, user_id="u1")
    list_payload: JSONDict = {"operation": "list", "user_id": "u2"}
    with pytest.raises(PermissionError):
        asyncio.run(commitment.commitment_manage(ctx, list_payload))


def test_commitment_manage_staff_can_update_other_user_commitment(tmp_path: Path) -> None:
    admin_ctx = _ctx_with_db(
        tmp_path,
        user_id="999",
        channel_id="100",
        guild_id="1",
        admin_user_ids=["999"],
    )
    create_payload: JSONDict = {
        "operation": "create",
        "name": "Run",
        "description": "Daily",
        "user_id": "200",
        "channel_id": "100",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
    }
    create_result = asyncio.run(commitment.commitment_manage(admin_ctx, create_payload))
    commitment_payload = require_obj(create_result["commitment"])
    commitment_id = commitment_payload["id"]

    ctx = _ctx_with_db(tmp_path, user_id="500", channel_id="100", guild_id="1")
    _attach_staff_client(
        ctx,
        guild_id="1",
        channel_id="100",
        user_id="500",
        permissions=discord.Permissions(manage_messages=True),
    )

    update_payload: JSONDict = {
        "operation": "update",
        "commitment_id": commitment_id,
        "description": "Updated by mod",
    }
    result = asyncio.run(commitment.commitment_manage(ctx, update_payload))
    updated_payload = require_obj(result["commitment"])
    assert updated_payload["description"] == "Updated by mod"


def test_commitment_manage_staff_can_list_other_user_commitments(tmp_path: Path) -> None:
    admin_ctx = _ctx_with_db(
        tmp_path,
        user_id="999",
        channel_id="100",
        guild_id="1",
        admin_user_ids=["999"],
    )
    create_payload: JSONDict = {
        "operation": "create",
        "name": "Read",
        "description": "Daily chapter",
        "user_id": "200",
        "channel_id": "100",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
    }
    asyncio.run(commitment.commitment_manage(admin_ctx, create_payload))

    ctx = _ctx_with_db(tmp_path, user_id="500", channel_id="100", guild_id="1")
    _attach_staff_client(
        ctx,
        guild_id="1",
        channel_id="100",
        user_id="500",
        permissions=discord.Permissions(moderate_members=True),
    )

    result = asyncio.run(
        commitment.commitment_manage(
            ctx,
            {"operation": "list", "user_id": "200", "channel_id": "100"},
        )
    )
    assert result["ok"] is True
    commitments_value = result["commitments"]
    assert isinstance(commitments_value, list)
    assert len(commitments_value) == 1


def test_row_to_commitment_defaults_missing_optional_columns() -> None:
    original = set(commitment._WARNED_MISSING_COMMITMENT_COLUMNS)
    try:
        row = _row_from_dict(
            {
                "id": 1,
                "name": "Run",
                "description": "Daily",
                "user_id": "u1",
                "channel_id": "c1",
                "start_date": "2024-01-01",
                "end_date": "2024-01-05",
            }
        )
        parsed = commitment._row_to_commitment(row)
        assert parsed.interval_days == commitment.DEFAULT_INTERVAL_DAYS
        assert parsed.status == "active"
        assert parsed.last_checkin_date is None
    finally:
        commitment._WARNED_MISSING_COMMITMENT_COLUMNS.clear()
        commitment._WARNED_MISSING_COMMITMENT_COLUMNS.update(original)


def test_row_to_commitment_invalid_interval_uses_default() -> None:
    row = _row_from_dict(
        {
            "id": 2,
            "name": "Read",
            "description": "Chapter",
            "user_id": "u2",
            "channel_id": "c2",
            "start_date": "2024-02-01",
            "end_date": "2024-02-10",
            "interval_days": "nope",
            "last_checkin_date": None,
            "status": "active",
        }
    )
    parsed = commitment._row_to_commitment(row)
    assert parsed.interval_days == commitment.DEFAULT_INTERVAL_DAYS


def test_commitment_checkin_task_enforces_date_window(
    tmp_path: Path,
) -> None:
    today = dt.date.today()
    today_s = today.isoformat()
    future_start = (today + dt.timedelta(days=1)).isoformat()
    future_end = (today + dt.timedelta(days=5)).isoformat()
    expired_start = (today - dt.timedelta(days=5)).isoformat()
    expired_end = (today - dt.timedelta(days=1)).isoformat()

    ctx = _ctx_with_db(tmp_path)
    sent_messages: list[tuple[str, str, str]] = []

    async def _fake_request_reply(channel_id: str, system_message: str, user_message: str) -> None:
        sent_messages.append((channel_id, system_message, user_message))

    ctx.request_vox_reply = _fake_request_reply

    payloads: list[JSONDict] = [
        {
            "operation": "create",
            "name": "Current",
            "description": "Due today",
            "user_id": "u1",
            "channel_id": "c1",
            "start_date": today_s,
            "end_date": today_s,
        },
        {
            "operation": "create",
            "name": "Future",
            "description": "Starts tomorrow",
            "user_id": "u1",
            "channel_id": "c1",
            "start_date": future_start,
            "end_date": future_end,
        },
        {
            "operation": "create",
            "name": "Expired",
            "description": "Already ended",
            "user_id": "u1",
            "channel_id": "c1",
            "start_date": expired_start,
            "end_date": expired_end,
        },
    ]
    for payload in payloads:
        asyncio.run(commitment.commitment_manage(ctx, payload))

    result = asyncio.run(commitment.commitment_checkin_task(ctx, {}))

    assert result == {
        "ok": True,
        "channels_pinged": 1,
        "commitments_pinged": 1,
        "channels_failed": 0,
    }
    assert len(sent_messages) == 1
    channel_id, system_message, user_message = sent_messages[0]
    assert channel_id == "c1"
    assert "Current" in system_message
    assert "Future" not in system_message
    assert "Expired" not in system_message
    assert user_message == "Send the commitment check-in reminder now."

    db_path = Path(str(ctx.config["commitments_db_path"]))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT name, status, last_checkin_date
            FROM commitments
            ORDER BY name
            """
        ).fetchall()
    finally:
        conn.close()

    by_name = {str(row["name"]): row for row in rows}
    assert by_name["Current"]["status"] == "active"
    assert by_name["Current"]["last_checkin_date"] == today_s
    assert by_name["Future"]["status"] == "active"
    assert by_name["Future"]["last_checkin_date"] is None
    assert by_name["Expired"]["status"] == "ended"
    assert by_name["Expired"]["last_checkin_date"] is None


def test_commitment_db_init_runs_once_per_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx_with_db(tmp_path)
    original_init = commitment._init_db
    init_calls = 0

    def _counting_init(conn: sqlite3.Connection) -> None:
        nonlocal init_calls
        init_calls += 1
        original_init(conn)

    monkeypatch.setattr(commitment, "_init_db", _counting_init)

    create_result = asyncio.run(
        commitment.commitment_manage(
            ctx,
            {
                "operation": "create",
                "name": "Daily Summary",
                "description": "Write the summary",
                "user_id": "u1",
                "channel_id": "c1",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
        )
    )
    commitment_payload = require_obj(create_result["commitment"])
    commitment_id = commitment_payload["id"]
    assert isinstance(commitment_id, int)

    asyncio.run(
        commitment.commitment_notes_manage(
            ctx,
            {
                "operation": "append",
                "commitment_id": commitment_id,
                "notes": "Completed for 2026-01-29 and 2026-01-30.",
            },
        )
    )
    asyncio.run(commitment.commitment_manage(ctx, {"operation": "list"}))

    assert init_calls == 1


def test_commitment_notes_append_waits_for_locked_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = sqlite3.connect

    def _connect_zero_timeout(
        database: str,
        timeout: float = 5.0,
    ) -> sqlite3.Connection:
        del timeout
        return original_connect(database, timeout=0.0)

    monkeypatch.setattr(sqlite3, "connect", _connect_zero_timeout)

    ctx = _ctx_with_db(tmp_path)
    create_result = asyncio.run(
        commitment.commitment_manage(
            ctx,
            {
                "operation": "create",
                "name": "Daily Summary",
                "description": "Write the summary",
                "user_id": "u1",
                "channel_id": "c1",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
        )
    )
    commitment_payload = require_obj(create_result["commitment"])
    commitment_id = commitment_payload["id"]
    assert isinstance(commitment_id, int)

    db_path = Path(str(ctx.config["commitments_db_path"]))
    hold_conn = original_connect(str(db_path), timeout=0.0)
    hold_conn.row_factory = sqlite3.Row
    hold_conn.execute("PRAGMA journal_mode=WAL;")
    hold_conn.execute("PRAGMA busy_timeout=0;")
    hold_conn.execute("BEGIN IMMEDIATE")
    hold_conn.execute(
        "UPDATE commitments SET updated_at = updated_at WHERE id = ?",
        (commitment_id,),
    )

    append_started = threading.Event()
    original_now_ms = commitment._now_ms

    def _signal_now_ms() -> int:
        append_started.set()
        return original_now_ms()

    monkeypatch.setattr(commitment, "_now_ms", _signal_now_ms)

    result_holder: list[JSONDict] = []
    error_holder: list[Exception] = []

    def _append_notes() -> None:
        try:
            result_holder.append(
                asyncio.run(
                    commitment.commitment_notes_manage(
                        ctx,
                        {
                            "operation": "append",
                            "commitment_id": commitment_id,
                            "notes": "Completed for 2026-01-29 and 2026-01-30.",
                        },
                    )
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            error_holder.append(exc)

    thread = threading.Thread(target=_append_notes)
    thread.start()

    assert append_started.wait(timeout=1.0)
    time.sleep(0.1)
    hold_conn.commit()
    hold_conn.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert not error_holder
    assert len(result_holder) == 1

    updated_commitment = require_obj(result_holder[0]["commitment"])
    assert updated_commitment["notes"] == "Completed for 2026-01-29 and 2026-01-30."
