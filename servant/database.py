"""Keyed SQLCipher connections for all persistent Vox databases."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from sqlcipher3 import dbapi2 as driver

Connection = driver.Connection
Row = driver.Row
DatabaseError = driver.DatabaseError
OperationalError = driver.OperationalError
KEY_FILE_ENV = "JEEVES_DB_KEY_FILE"
_PROCESSING_VALID: ContextVar[Callable[[], bool] | None] = ContextVar("vox_processing_valid", default=None)


@contextmanager
def processing_scope(generation: Callable[[], int]) -> Iterator[None]:
    """Carry cancellation into worker threads, which asyncio task cancellation cannot stop."""
    if _PROCESSING_VALID.get() is not None:
        yield
        return
    started = generation()
    token = _PROCESSING_VALID.set(lambda: generation() == started)
    try:
        yield
    finally:
        _PROCESSING_VALID.reset(token)


def processing_is_valid() -> bool:
    check = _PROCESSING_VALID.get()
    return check is None or check()


def read_key(key_file: Path | None = None) -> str:
    if key_file is None:
        configured = os.environ.get(KEY_FILE_ENV)
        if not configured:
            raise RuntimeError(f"Set {KEY_FILE_ENV} to the protected SQLCipher key file before starting Vox.")
        key_file = Path(configured).expanduser()
    with key_file.open("r", encoding="ascii") as handle:
        mode = os.fstat(handle.fileno()).st_mode
        if not stat.S_ISREG(mode) or mode & 0o027:
            raise PermissionError("The SQLCipher key must be a regular file with mode 0600 or 0640.")
        key = handle.read(66).strip()
        if handle.read(1) or re.fullmatch(r"[0-9a-fA-F]{64}", key) is None:
            raise ValueError("The SQLCipher key file must contain exactly 64 hexadecimal characters.")
    return key


def require_sqlcipher(conn: Connection) -> None:
    version = conn.execute("PRAGMA cipher_version").fetchone()
    if not version or int(str(version[0]).split(".", 1)[0]) != 4:
        raise RuntimeError("Vox requires SQLCipher 4; plaintext SQLite is not supported.")


def connect(database: str | Path, timeout: float = 5.0, *, key_file: Path | None = None) -> Connection:
    key = read_key(key_file)
    path = str(database)
    if path != ":memory:":
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    conn = driver.connect(path, timeout=timeout)
    try:
        require_sqlcipher(conn)
        # PRAGMA parameters cannot be bound. The key is validated hex, never user SQL.
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
        conn.execute("PRAGMA cipher_memory_security=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.row_factory = Row
        check = _PROCESSING_VALID.get()
        if check is not None:
            # Check every instruction, including cached statements and COMMIT.
            conn.set_progress_handler(lambda: int(not check()), 1)
        return conn
    except BaseException:
        conn.close()
        raise
