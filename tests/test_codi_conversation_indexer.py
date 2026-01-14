import sqlite3
import unittest

from servant.defs import GlobalContext
from servant.modules import codi_conversation_indexer as indexer


class TestCodiConversationIndexer(unittest.TestCase):
    def test_assign_conversation_ids_with_overlap(self) -> None:
        predicted = {
            "T1": ["m1", "m2", "m5"],
            "T2": ["m3", "m4"],
        }
        existing = {
            "c1": {"m1", "m2"},
            "c2": {"m3", "m4"},
        }
        assignments, new_ids = indexer._assign_conversation_ids(
            predicted, existing, min_overlap_ratio=0.5, id_factory=lambda: "new"
        )

        self.assertEqual(assignments["T1"], "c1")
        self.assertEqual(assignments["T2"], "c2")
        self.assertEqual(new_ids, [])

    def test_assign_conversation_ids_creates_new(self) -> None:
        predicted = {"T1": ["m9"]}
        existing = {"c1": {"m1", "m2"}}
        assignments, new_ids = indexer._assign_conversation_ids(
            predicted, existing, min_overlap_ratio=0.5, id_factory=lambda: "new"
        )

        self.assertEqual(assignments["T1"], "new")
        self.assertEqual(new_ids, ["new"])

    def test_hash_conversation_changes_on_content(self) -> None:
        messages_a: list[indexer.MessagePayload] = [
            {
                "id": "m1",
                "author_id": "u1",
                "author_name": "Alice",
                "content": "hello",
                "timestamp": "1",
                "created_at": None,
            },
            {
                "id": "m2",
                "author_id": "u2",
                "author_name": "Bob",
                "content": "world",
                "timestamp": "2",
                "created_at": None,
            },
        ]
        messages_b: list[indexer.MessagePayload] = [
            {
                "id": "m1",
                "author_id": "u1",
                "author_name": "Alice",
                "content": "hello",
                "timestamp": "1",
                "created_at": None,
            },
            {
                "id": "m2",
                "author_id": "u2",
                "author_name": "Bob",
                "content": "changed",
                "timestamp": "2",
                "created_at": None,
            },
        ]
        hash_a = indexer._hash_conversation(messages_a)
        hash_b = indexer._hash_conversation(messages_b)

        self.assertNotEqual(hash_a, hash_b)

    def test_sample_messages_for_naming_keeps_edges(self) -> None:
        messages: list[indexer.MessagePayload] = [
            {
                "id": str(i),
                "author_id": "u1",
                "author_name": "User",
                "content": "",
                "timestamp": str(i),
                "created_at": None,
            }
            for i in range(6)
        ]
        sampled = indexer._sample_messages_for_naming(messages, max_messages=4)

        self.assertEqual([msg["id"] for msg in sampled], ["0", "1", "4", "5"])

    def test_format_messages_for_prompt_truncates(self) -> None:
        messages: list[indexer.MessagePayload] = [
            {
                "id": "m1",
                "author_id": "u1",
                "author_name": "Alice",
                "content": "hello    there\\nfriend",
                "timestamp": "1",
                "created_at": None,
            }
        ]
        text = indexer._format_messages_for_prompt(messages, max_chars=10)

        self.assertEqual(text, "Alice: hello t...")

    def test_select_channels_to_analyze_filters_up_to_date(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE messages (
                message_id TEXT,
                channel_id TEXT,
                guild_id TEXT,
                content TEXT,
                content_available INTEGER,
                deleted_at INTEGER,
                created_at INTEGER
            );
            CREATE TABLE channels (
                channel_id TEXT PRIMARY KEY,
                name TEXT
            );
            CREATE TABLE guilds (
                guild_id TEXT PRIMARY KEY,
                name TEXT
            );
            CREATE TABLE codi_channel_state (
                channel_id TEXT PRIMARY KEY,
                guild_id TEXT,
                last_analyzed_at INTEGER,
                last_message_id TEXT,
                last_message_ts INTEGER,
                last_message_count INTEGER
            );
            """
        )
        conn.execute("INSERT INTO channels (channel_id, name) VALUES ('c1', 'one'), ('c2', 'two')")
        conn.execute("INSERT INTO guilds (guild_id, name) VALUES ('g1', 'guild')")
        conn.executemany(
            """
            INSERT INTO messages (message_id, channel_id, guild_id, content, content_available, deleted_at, created_at)
            VALUES (?, ?, ?, ?, 1, NULL, ?)
            """,
            [
                ("m1", "c1", "g1", "hi", 100),
                ("m2", "c1", "g1", "yo", 200),
                ("m3", "c2", "g1", "hey", 100),
                ("m4", "c2", "g1", "there", 200),
                ("m5", "c2", "g1", "again", 300),
            ],
        )
        conn.execute(
            """
            INSERT INTO codi_channel_state
                (channel_id, guild_id, last_analyzed_at, last_message_id, last_message_ts, last_message_count)
            VALUES ('c1', 'g1', 999, 'm2', 200, 2)
            """
        )

        batches = indexer._select_channels_to_analyze(conn, limit=10)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].channel_id, "c2")

    def test_module_state_is_cached(self) -> None:
        ctx = GlobalContext()
        state1 = indexer._module_state(ctx)
        state2 = indexer._module_state(ctx)

        self.assertIs(state1, state2)


if __name__ == "__main__":
    unittest.main()
