from __future__ import annotations

import os
from pathlib import Path

import yaml

from typed_json import JSONDict, obj_to_json

DEFAULT_CONFIG_PATH = ".private.yml"
CONFIG_PATH_ENV_VAR = "JEEVES_CONFIG_PATH"


def resolve_config_path(*, default_path: str = DEFAULT_CONFIG_PATH) -> Path:
    override = os.environ.get(CONFIG_PATH_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()
    return Path(default_path)


def load_yaml_config(path: Path) -> JSONDict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    loaded_json = obj_to_json(loaded)
    if isinstance(loaded_json, dict):
        return loaded_json
    return {}
