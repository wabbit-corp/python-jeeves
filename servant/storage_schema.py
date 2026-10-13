"""Database guards shared by controllers and feature storage without controller imports."""

from __future__ import annotations

from servant import database


def init_channel_schema(conn: database.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS privacy_channel_controls ("
        "channel_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, allowed INTEGER NOT NULL, pending INTEGER NOT NULL)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS privacy_channel_mode (id INTEGER PRIMARY KEY CHECK(id=1))")
    # Replace the previous allowlist guards. Otherwise they continue denying new
    # channels even after the runtime changes to default-enabled behavior.
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'privacy_channel_*'"
    ).fetchall():
        name = str(row[0])
        if "_default_allow_v1_" not in name:
            quoted_name = '"' + name.replace('"', '""') + '"'
            conn.execute(f"DROP TRIGGER {quoted_name}")
    # Enable enforcement when the running application's startup loads its policy.
    # Existing migrations and offline schema inspection can still open a database.
    tables = [
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not str(row[0]).startswith(("sqlite_", "retention_", "privacy_"))
    ]
    for table in tables:
        quoted_table = '"' + table.replace('"', '""') + '"'
        columns = {str(col[1]) for col in conn.execute(f"PRAGMA table_info({quoted_table})")}
        conditions = []
        for column in ("channel_id", "parent_channel_id"):
            if column not in columns:
                continue
            conditions.append(
                f"EXISTS (SELECT 1 FROM privacy_channel_controls WHERE channel_id=NEW.{column} AND (allowed=0 OR pending=1))"
            )
            if "guild_id" in columns:
                conditions.append(
                    f"(NEW.guild_id IS NOT NULL AND EXISTS (SELECT 1 FROM privacy_channel_controls "
                    f"WHERE channel_id=NEW.{column} AND guild_id!=NEW.guild_id))"
                )
                conditions.append(
                    f"(NEW.guild_id IS NULL AND EXISTS(SELECT 1 FROM privacy_channel_controls "
                    f"WHERE channel_id=NEW.{column}))"
                )
                if "channels" in tables:
                    conditions.append(
                        f"(NEW.guild_id IS NULL AND EXISTS(SELECT 1 FROM channels "
                        f"WHERE channel_id=NEW.{column} AND guild_id IS NOT NULL))"
                    )
        if not conditions:
            continue
        for operation in ("INSERT", "UPDATE"):
            name = (
                '"'
                + f"privacy_channel_{table}_{operation.lower()}_default_allow_v1_{len(conditions)}".replace('"', '""')
                + '"'
            )
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {operation} ON {quoted_table} "
                f"WHEN EXISTS(SELECT 1 FROM privacy_channel_mode) AND ({' OR '.join(conditions)}) "
                "BEGIN SELECT RAISE(IGNORE); END"
            )


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _tables(conn: database.Connection) -> dict[str, set[str]]:
    return {
        str(row[0]): {str(col[1]) for col in conn.execute(f"PRAGMA table_info({_quote(str(row[0]))})")}
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not str(row[0]).startswith(("sqlite_", "retention_", "privacy_"))
    }


def init_retention_schema(conn: database.Connection) -> None:
    init_channel_schema(conn)
    # These contain identifiers only, never content. They also journal unfinished cleanup.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS retention_removed_guilds (guild_id TEXT PRIMARY KEY, pending INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS retention_removed_channels (channel_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL)"
    )
    tables = _tables(conn)
    for table, columns in tables.items():
        conditions: list[str] = []
        signature = 0
        if "guild_id" in columns:
            signature += 1
            conditions.append("EXISTS (SELECT 1 FROM retention_removed_guilds WHERE guild_id=NEW.guild_id)")
        for column in ("channel_id", "parent_channel_id"):
            if column in columns:
                signature += 2 if column == "channel_id" else 4
                conditions.append(f"EXISTS (SELECT 1 FROM retention_removed_channels WHERE channel_id=NEW.{column})")
        if table.startswith("message_") and "message_id" in columns and "messages" in tables:
            signature += 8
            conditions.append("NOT EXISTS (SELECT 1 FROM messages WHERE message_id=NEW.message_id)")
        if not conditions:
            continue
        for operation in ("INSERT", "UPDATE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {_quote(f'retention_{table}_{operation.lower()}_{signature}')} "
                f"BEFORE {operation} ON {_quote(table)} WHEN {' OR '.join(conditions)} "
                "BEGIN SELECT RAISE(IGNORE); END"
            )


def guild_is_removed(conn: database.Connection, guild_id: str | None) -> bool:
    return (
        guild_id is not None
        and conn.execute("SELECT 1 FROM retention_removed_guilds WHERE guild_id=?", (guild_id,)).fetchone() is not None
    )
