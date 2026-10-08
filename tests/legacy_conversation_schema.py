# SPDX-License-Identifier: LicenseRef-Wabbit-Public-Test-License-1.1

"""Old database tables retained in fixtures to verify privacy cleanup."""

from servant import database


def init_tables(conn: database.Connection) -> None:
    conn.execute(
        """CREATE TABLE codi_channel_state (
            channel_id TEXT PRIMARY KEY, guild_id TEXT,
            last_analyzed_at INTEGER, last_message_id TEXT,
            last_message_ts INTEGER, last_message_count INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE codi_conversations (
            conversation_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
            guild_id TEXT, content_hash TEXT NOT NULL, name TEXT, name_hash TEXT,
            message_count INTEGER, first_message_id TEXT, last_message_id TEXT,
            created_at INTEGER, updated_at INTEGER, named_at INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE codi_conversation_messages (
            conversation_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL, created_at INTEGER,
            PRIMARY KEY (conversation_id, message_id), UNIQUE (channel_id, message_id),
            FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
        )"""
    )
    from servant import guild_retention

    guild_retention.init_schema(conn)
