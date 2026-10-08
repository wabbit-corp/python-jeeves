"""Export a plaintext SQLite snapshot to a new verified SQLCipher database."""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path

from sqlcipher3 import dbapi2 as driver

from servant import database

_LOGGER = logging.getLogger(__name__)


def encrypt_database(
    source: Path, destination: Path, *, key_file: Path | None = None, encrypted_source: bool = False
) -> None:
    """Leave the source intact; refuse to replace any existing destination."""
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(destination)
    key = database.read_key(key_file)
    fd, name = tempfile.mkstemp(prefix=".sqlcipher-", suffix=".db", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    conn: database.Connection | None = None
    try:
        conn = driver.connect(source.as_uri() + "?mode=ro", uri=True)
        database.require_sqlcipher(conn)
        if encrypted_source:
            conn.execute(f"PRAGMA key = \"x'{key}'\"")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cipher_memory_security=ON")
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("Source database failed integrity verification.")
        conn.execute(f"ATTACH DATABASE ? AS encrypted KEY \"x'{key}'\"", (str(temporary),))
        # Keep data, schema, and metadata within one consistent source snapshot.
        conn.execute("BEGIN")
        user_version = int(conn.execute("PRAGMA main.user_version").fetchone()[0])
        application_id = int(conn.execute("PRAGMA main.application_id").fetchone()[0])
        conn.execute("SELECT sqlcipher_export('encrypted')").fetchone()
        conn.execute(f"PRAGMA encrypted.user_version={user_version}")
        conn.execute(f"PRAGMA encrypted.application_id={application_id}")
        conn.commit()
        conn.execute("DETACH DATABASE encrypted")
        conn.close()
        conn = None
        encrypted = database.connect(temporary, key_file=key_file)
        try:
            if encrypted.execute("PRAGMA cipher_integrity_check").fetchall():
                raise RuntimeError("Encrypted database failed authentication verification.")
            if [row[0] for row in encrypted.execute("PRAGMA integrity_check")] != ["ok"]:
                raise RuntimeError("Encrypted database failed integrity verification.")
        finally:
            encrypted.close()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        # A hard link publishes atomically and fails if a destination appeared meanwhile.
        os.link(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if conn is not None:
            conn.close()
        temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--key-file", type=Path, help="Defaults to JEEVES_DB_KEY_FILE; never pass a key on the command line."
    )
    parser.add_argument(
        "--encrypted-source", action="store_true", help="Create an encrypted snapshot of a keyed database."
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    encrypt_database(args.source, args.destination, key_file=args.key_file, encrypted_source=args.encrypted_source)
    _LOGGER.info("Verified encrypted database created at %s; source retained for controlled cutover.", args.destination)


if __name__ == "__main__":
    main()
