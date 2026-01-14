from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import is_dataclass
from typing import TypeAlias

_logger = logging.getLogger(__name__)

JSONPrimitive: TypeAlias = str | int | float | bool | None
JSON: TypeAlias = dict[str, "JSON"] | list["JSON"] | JSONPrimitive
JSONDict = dict[str, JSON]
JSONArray = list[JSON]


def _is_dataclass_instance(obj: object) -> bool:
    return is_dataclass(obj) and not isinstance(obj, type)


def obj_to_json(obj: object, emit_null: bool = True) -> JSON:
    if isinstance(obj, list):
        return [obj_to_json(o) for o in obj]
    elif isinstance(obj, dict):
        return {str(k): obj_to_json(v) for k, v in obj.items() if emit_null or v is not None}
    elif isinstance(obj, int) or isinstance(obj, float) or isinstance(obj, str) or isinstance(obj, bool) or obj is None:
        return obj
    elif _is_dataclass_instance(obj):
        return obj_to_json(obj.__dict__)
    else:
        _logger.warning(f"Unknown type {type(obj)} in {obj_to_json.__name__}")
        return obj_to_json(obj.__dict__)


def json_hash(obj: JSON) -> str:
    request = json.dumps(obj, sort_keys=True)
    return hashlib.sha256(request.encode("utf-8")).hexdigest()


def coerce_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def coerce_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def coerce_float_strict(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be a number.") from exc
    raise ValueError(f"{field} must be a number.")


def coerce_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    raise ValueError("Invalid boolean value.")


def coerce_str(
    value: object,
    *,
    field: str | None = None,
    default: str | None = None,
    allow_empty: bool = True,
    allow_non_str: bool = True,
) -> str:
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"{field or 'value'} is required.")
    if isinstance(value, (dict, list, tuple, set)):
        raise ValueError(f"{field or 'value'} must be a string.")
    if not isinstance(value, str):
        if allow_non_str:
            value = str(value)
        else:
            raise ValueError(f"{field or 'value'} must be a string.")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field or 'value'} must be a non-empty string.")
    return value.strip() if not allow_empty else value


def coerce_optional_str(value: object, *, allow_non_str: bool = True) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        raise ValueError("Expected a string value.")
    if not isinstance(value, str):
        if allow_non_str:
            value = str(value)
        else:
            raise ValueError("Expected a string value.")
    text = value.strip()
    return text or None


def coerce_optional_str_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    return [str(item) for item in value]


def coerce_snowflake(value: object, field: str) -> str | None:
    if value is None:
        return None
    try:
        return str(int(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid snowflake id.") from exc


def require_obj(value: JSON) -> JSONDict:
    if not isinstance(value, dict):
        raise ValueError("Input must be an object.")
    return value
