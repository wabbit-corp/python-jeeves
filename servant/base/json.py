from typing import TypeAlias
from dataclasses import is_dataclass, dataclass

import hashlib
import json
import logging

_logger = logging.getLogger(__name__)


JSON: TypeAlias = dict[str, "JSON"] | list["JSON"] | str | int | float | bool | None
JSONDict = dict[str, JSON]
JSONArray = list[JSON]


def _is_dataclass_instance(obj):
    return is_dataclass(obj) and not isinstance(obj, type)


def obj_to_json(obj, emit_null=True) -> JSON:
    if isinstance(obj, list):
        return [obj_to_json(o) for o in obj]
    elif isinstance(obj, dict):
        return {k: obj_to_json(v) for k, v in obj.items() if emit_null or v is not None}
    elif isinstance(obj, int) or isinstance(obj, float) or isinstance(obj, str) or isinstance(obj, bool) or obj is None:
        return obj
    elif _is_dataclass_instance(obj):
        return obj_to_json(obj.__dict__)
    else:
        _logger.warning(f'Unknown type {type(obj)} in {obj_to_json.__name__}')
        return obj_to_json(obj.__dict__)


def json_hash(obj: JSON) -> str:
    request = json.dumps(obj, sort_keys=True)
    return hashlib.sha256(request.encode('utf-8')).hexdigest()
