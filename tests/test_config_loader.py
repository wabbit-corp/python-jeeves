from __future__ import annotations

from pathlib import Path

import pytest

from servant import config_loader


def test_resolve_config_path_uses_default_when_env_not_set() -> None:
    path = config_loader.resolve_config_path()
    assert path == Path(".private.yml")


def test_resolve_config_path_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "production.yml"
    monkeypatch.setenv(config_loader.CONFIG_PATH_ENV_VAR, str(override))

    path = config_loader.resolve_config_path()

    assert path == override.resolve()


def test_load_yaml_config_returns_flat_object_tree(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "discord:\n  token: secret\nadmin_user_ids:\n  - '123'\n",
        encoding="utf-8",
    )

    config = config_loader.load_yaml_config(config_path)

    assert config == {
        "discord": {"token": "secret"},
        "admin_user_ids": ["123"],
    }


def test_load_yaml_config_returns_empty_for_missing_file(tmp_path: Path) -> None:
    config = config_loader.load_yaml_config(tmp_path / "missing.yml")
    assert config == {}
