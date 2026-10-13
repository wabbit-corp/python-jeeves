# SPDX-License-Identifier: LicenseRef-Wabbit-Public-Test-License-1.1
from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))


@pytest.fixture(autouse=True)
def sqlcipher_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key_file = tmp_path / "database.key"
    key_file.touch(mode=0o600)
    key_file.write_text(secrets.token_hex(32), encoding="ascii")
    monkeypatch.setenv("JEEVES_DB_KEY_FILE", str(key_file))
    return key_file
