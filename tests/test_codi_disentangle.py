import unittest

from codi import disentangle as codi


class TestCodiDisentangleHelpers(unittest.TestCase):
    def test_normalize_message_with_explicit_fields(self):
        message = {
            "id": "m1",
            "author_id": "u1",
            "author_name": "Alice",
            "content": "hello",
            "timestamp": 123,
        }
        normalized = codi._normalize_message(message, 0)

        self.assertEqual(normalized["id"], "m1")
        self.assertEqual(normalized["authorId"], "u1")
        self.assertEqual(normalized["authorName"], "Alice")
        self.assertEqual(normalized["content"], "hello")
        self.assertEqual(normalized["timestamp"], "123")

    def test_normalize_message_with_author_object(self):
        message = {
            "message_id": "m2",
            "author": {"id": 42, "name": "Bob"},
            "text": "hi",
        }
        normalized = codi._normalize_message(message, 5)

        self.assertEqual(normalized["id"], "m2")
        self.assertEqual(normalized["authorId"], "42")
        self.assertEqual(normalized["authorName"], "Bob")
        self.assertEqual(normalized["content"], "hi")
        self.assertEqual(normalized["timestamp"], "5")

    def test_normalize_message_requires_author(self):
        with self.assertRaises(ValueError):
            codi._normalize_message({"content": "missing author"}, 0)

    def test_build_community_minimal(self):
        messages = [
            {
                "id": "1",
                "author_id": "u1",
                "author_name": "Alice",
                "content": "hey",
                "timestamp": 1,
            },
            {
                "id": "2",
                "author_id": "u1",
                "author_name": "Alice",
                "content": "again",
                "timestamp": 2,
            },
            {
                "id": "3",
                "author_id": "u2",
                "author_name": "Bob",
                "content": "yo",
                "timestamp": 3,
            },
        ]
        community = codi._build_community(
            messages,
            platform="discord",
            community_id="guild-1",
            community_name="guild-one",
            channel_id="chan-1",
            channel_name="#general",
        )

        self.assertEqual(community["platform"], "discord")
        self.assertEqual(community["id"], "guild-1")
        self.assertEqual(community["name"], "guild-one")

        members = {member["id"]: member["name"] for member in community["members"]}
        self.assertEqual(members, {"u1": "Alice", "u2": "Bob"})

        self.assertEqual(len(community["channels"]), 1)
        channel = community["channels"][0]
        self.assertEqual(channel["id"], "chan-1")
        self.assertEqual(channel["path"], "#general")
        self.assertEqual(len(channel["messages"]), 3)
        self.assertEqual(channel["messages"][0]["authorId"], "u1")
        self.assertEqual(channel["messages"][2]["authorId"], "u2")


if __name__ == "__main__":
    unittest.main()
