from __future__ import annotations

import sqlite3 as plaintext_sqlite
from pathlib import Path

import pytest

from servant import database
from servant.scripts.encrypt_databases import encrypt_database


def test_database_and_wal_are_encrypted_and_require_correct_key(tmp_path: Path, sqlcipher_key: Path) -> None:
    path = tmp_path / "encrypted.db"
    marker = "unique secret content never stored as plaintext"
    conn = database.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE messages (content TEXT)")
    conn.execute("INSERT INTO messages VALUES (?)", (marker,))
    conn.commit()
    try:
        assert not path.read_bytes().startswith(b"SQLite format 3")
        assert marker.encode() not in path.read_bytes()
        assert marker.encode() not in Path(str(path) + "-wal").read_bytes()
        assert path.stat().st_mode & 0o777 == 0o600
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        with plaintext_sqlite.connect(path) as plaintext:
            with pytest.raises(plaintext_sqlite.DatabaseError):
                plaintext.execute("SELECT * FROM messages").fetchall()
        wrong_key = tmp_path / "wrong.key"
        wrong_key.touch(mode=0o600)
        wrong_key.write_text("34" * 32)
        with pytest.raises(database.DatabaseError):
            database.connect(path, key_file=wrong_key)
        reopened = database.connect(path, key_file=sqlcipher_key)
        try:
            assert reopened.execute("SELECT content FROM messages").fetchone()[0] == marker
        finally:
            reopened.close()
    finally:
        conn.close()


def test_no_key_no_plaintext_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(database.KEY_FILE_ENV)
    path = tmp_path / "should-not-exist.db"
    with pytest.raises(RuntimeError, match=database.KEY_FILE_ENV):
        database.connect(path)
    assert not path.exists()


@pytest.mark.parametrize("content", ["", "abc", "z" * 64, "12" * 33, "12" * 32 + "\nmore"])
def test_invalid_key_rejected(sqlcipher_key: Path, content: str) -> None:
    sqlcipher_key.write_text(content)
    with pytest.raises(ValueError, match="64 hexadecimal"):
        database.read_key(sqlcipher_key)


def test_insecure_key_permissions_rejected(sqlcipher_key: Path) -> None:
    sqlcipher_key.chmod(0o644)
    with pytest.raises(PermissionError):
        database.read_key(sqlcipher_key)


def test_plaintext_driver_is_rejected() -> None:
    conn = plaintext_sqlite.connect(":memory:")
    try:
        with pytest.raises(RuntimeError, match="SQLCipher 4"):
            database.require_sqlcipher(conn)
    finally:
        conn.close()


def test_plaintext_database_refused_without_automatic_migration(tmp_path: Path) -> None:
    path = tmp_path / "plain.db"
    with plaintext_sqlite.connect(path) as conn:
        conn.execute("CREATE TABLE messages (content TEXT)")
        conn.execute("INSERT INTO messages VALUES ('keep intact')")
    before = path.read_bytes()
    with pytest.raises(database.DatabaseError):
        database.connect(path)
    assert path.read_bytes() == before


def test_migration_preserves_schema_data_metadata_and_wal(tmp_path: Path) -> None:
    source = tmp_path / "plain.db"
    destination = tmp_path / "encrypted.db"
    source_conn = plaintext_sqlite.connect(source)
    source_conn.execute("PRAGMA journal_mode=WAL")
    source_conn.executescript(
        """
        PRAGMA user_version=17;
        PRAGMA application_id=42;
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT);
        CREATE INDEX messages_content ON messages(content);
        CREATE VIEW message_view AS SELECT content FROM messages;
        CREATE TABLE audit (content TEXT);
        CREATE TRIGGER on_message AFTER INSERT ON messages
            BEGIN INSERT INTO audit VALUES (NEW.content); END;
        CREATE VIRTUAL TABLE search USING fts5(content);
        INSERT INTO messages(content) VALUES ('retain my message');
        INSERT INTO search VALUES ('retain my message');
        """
    )
    try:
        encrypt_database(source, destination)
        assert source_conn.execute("SELECT content FROM messages").fetchone()[0] == "retain my message"
        assert source.read_bytes().startswith(b"SQLite format 3")
        assert not destination.read_bytes().startswith(b"SQLite format 3")
        encrypted = database.connect(destination)
        try:
            assert encrypted.execute("PRAGMA user_version").fetchone()[0] == 17
            assert encrypted.execute("PRAGMA application_id").fetchone()[0] == 42
            assert encrypted.execute("SELECT content FROM message_view").fetchone()[0] == "retain my message"
            assert (
                encrypted.execute("SELECT content FROM search WHERE search MATCH 'retain'").fetchone()[0]
                == "retain my message"
            )
            encrypted.execute("INSERT INTO messages(content) VALUES ('second')")
            encrypted.commit()
            assert encrypted.execute("SELECT content FROM audit ORDER BY rowid").fetchall()[-1][0] == "second"
            assert encrypted.execute("SELECT MAX(id) FROM messages").fetchone()[0] == 2
        finally:
            encrypted.close()
        backup = tmp_path / "backup.db"
        encrypt_database(destination, backup, encrypted_source=True)
        restored = database.connect(backup)
        try:
            assert restored.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        finally:
            restored.close()
        assert not list(tmp_path.glob(".sqlcipher-*"))
    finally:
        source_conn.close()


def test_migration_never_overwrites_destination(tmp_path: Path) -> None:
    source = tmp_path / "plain.db"
    with plaintext_sqlite.connect(source) as conn:
        conn.execute("CREATE TABLE messages (content TEXT)")
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"do not replace")
    with pytest.raises(FileExistsError):
        encrypt_database(source, destination)
    assert destination.read_bytes() == b"do not replace"


def test_failed_migration_keeps_source_and_removes_temporary_output(tmp_path: Path) -> None:
    source = tmp_path / "corrupt.db"
    source.write_bytes(b"not a database")
    destination = tmp_path / "new.db"
    with pytest.raises(database.DatabaseError):
        encrypt_database(source, destination)
    assert source.read_bytes() == b"not a database"
    assert not destination.exists()
    assert not list(tmp_path.glob(".sqlcipher-*"))
