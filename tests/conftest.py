from __future__ import annotations

import secrets
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def sqlcipher_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key_file = tmp_path / "database.key"
    key_file.touch(mode=0o600)
    key_file.write_text(secrets.token_hex(32), encoding="ascii")
    monkeypatch.setenv("JEEVES_DB_KEY_FILE", str(key_file))
    return key_file
