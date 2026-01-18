from __future__ import annotations

import dataclasses
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from typed_json import (
    JSON,
    coerce_bool,
    coerce_float,
    coerce_float_strict,
    coerce_int,
    coerce_optional_str,
    coerce_optional_str_list,
    coerce_snowflake,
    coerce_str,
    json_hash,
    obj_to_json,
    require_obj,
)


@given(st.integers())
def test_coerce_int_round_trip_from_str(value: int) -> None:
    assert coerce_int(str(value), default=999) == value


def test_coerce_int_invalid_str_uses_default() -> None:
    assert coerce_int("not-a-number", default=7) == 7


def test_coerce_int_bool_coerces_to_int() -> None:
    assert coerce_int(True, default=0) == 1
    assert coerce_int(False, default=1) == 0


@given(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False, width=32))
def test_coerce_float_round_trip_from_str(value: float) -> None:
    result = coerce_float(str(value), default=0.0)
    assert math.isclose(result, value, rel_tol=1e-12, abs_tol=0.0)


def test_coerce_float_invalid_str_uses_default() -> None:
    assert coerce_float("invalid", default=1.25) == 1.25


def test_coerce_float_strict_rejects_bool() -> None:
    with pytest.raises(ValueError):
        coerce_float_strict(True, field="value")


def test_coerce_float_strict_rejects_invalid_str() -> None:
    with pytest.raises(ValueError):
        coerce_float_strict("nope", field="value")


def test_coerce_bool_rejects_unexpected_types() -> None:
    with pytest.raises(ValueError):
        coerce_bool([])


def test_coerce_bool_handles_strings() -> None:
    assert coerce_bool("yes") is True
    assert coerce_bool("0") is False


@given(st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1))
def test_coerce_str_strips_when_disallow_empty(value: str) -> None:
    padded = f"  {value}  "
    assert coerce_str(padded, allow_empty=False) == value


def test_coerce_str_rejects_empty_when_disallowed() -> None:
    with pytest.raises(ValueError):
        coerce_str("   ", allow_empty=False)


@pytest.mark.parametrize("value", [[], {}, ["x"], {"k": "v"}])
def test_coerce_str_rejects_sequences(value: object) -> None:
    with pytest.raises(ValueError):
        coerce_str(value)


def test_coerce_str_rejects_non_str_when_disallowed() -> None:
    with pytest.raises(ValueError):
        coerce_str(123, allow_non_str=False)


@given(st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1))
def test_coerce_optional_str_trims(value: str) -> None:
    padded = f" {value} "
    assert coerce_optional_str(padded) == value


def test_coerce_optional_str_rejects_non_str_when_disallowed() -> None:
    with pytest.raises(ValueError):
        coerce_optional_str(123, allow_non_str=False)


def test_coerce_optional_str_returns_none_for_blank() -> None:
    assert coerce_optional_str("   ") is None


@given(st.lists(st.integers(), max_size=10))
def test_coerce_optional_str_list_from_list(values: list[int]) -> None:
    assert coerce_optional_str_list(values) == [str(value) for value in values]


def test_coerce_optional_str_list_non_list_returns_none() -> None:
    assert coerce_optional_str_list("not-a-list") is None


@given(st.integers(min_value=0, max_value=10**12))
def test_coerce_snowflake_round_trip(value: int) -> None:
    assert coerce_snowflake(value, field="id") == str(value)


def test_coerce_snowflake_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        coerce_snowflake("12.5", field="id")


def test_require_obj_enforces_dict() -> None:
    assert require_obj({"ok": True}) == {"ok": True}
    with pytest.raises(ValueError):
        require_obj(["nope"])


@dataclasses.dataclass
class _SampleData:
    name: str
    count: int


class _SampleObject:
    def __init__(self) -> None:
        self.value = 3


def test_obj_to_json_handles_dataclasses() -> None:
    payload = _SampleData(name="alpha", count=2)
    assert obj_to_json(payload) == {"name": "alpha", "count": 2}


def test_obj_to_json_falls_back_to_dict() -> None:
    payload = _SampleObject()
    assert obj_to_json(payload) == {"value": 3}


def test_json_hash_is_order_independent() -> None:
    first: JSON = {"b": 2, "a": 1}
    second: JSON = {"a": 1, "b": 2}
    assert json_hash(first) == json_hash(second)
