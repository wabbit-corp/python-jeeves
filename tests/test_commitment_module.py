import asyncio
import datetime as dt
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from servant.defs import GlobalContext
from servant.modules import commitment
from typed_json import JSONDict, require_obj


def _ctx_with_db(tmp_path: Path) -> GlobalContext:
    return GlobalContext(secrets={"commitments_db_path": str(tmp_path / "commitments.sqlite3")})


@given(st.integers(min_value=-(10**12), max_value=10**12))
def test_parse_epoch_ms_accepts_int(value: int) -> None:
    assert commitment._parse_epoch_ms(value) == value


@given(st.integers(min_value=-(10**12), max_value=10**12))
def test_parse_epoch_ms_accepts_numeric_string(value: int) -> None:
    assert commitment._parse_epoch_ms(str(value)) == value


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
