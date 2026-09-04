import logging
import tempfile
import unittest
from pathlib import Path

from bot import Config, RelayBot, Store, redact


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.next_id = 100

    def call(self, method, payload):
        self.calls.append((method, payload))
        self.next_id += 1
        return {"message_id": self.next_id}


def message(chat_id, message_id=1, text="hello", chat_type="private", reply_to=None):
    result = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": chat_id, "first_name": "测试", "username": "sender", "is_bot": False},
        "text": text,
    }
    if reply_to is not None:
        result["reply_to_message"] = {"message_id": reply_to}
    return result


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "test.sqlite3"))
        self.api = FakeAPI()
        self.config = Config(owner_id=999, db_path="unused", rate_count=2, rate_window=60)
        self.bot = RelayBot(self.api, self.store, self.config, clock=lambda: 1000)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_sender_message_is_relayed_and_mapped(self):
        self.bot.handle_update({"update_id": 1, "message": message(123, 7)})
        methods = [call[0] for call in self.api.calls]
        self.assertEqual(methods, ["sendMessage", "copyMessage", "sendMessage"])
        self.assertEqual(self.store.target_for(101), (123, 7))
        self.assertEqual(self.store.target_for(102), (123, 7))
        self.assertIn("用户 ID：123", self.api.calls[0][1]["text"])

    def test_owner_reply_is_copied_to_original_sender(self):
        self.store.add_mapping([50], 123, 7, 1000)
        self.bot.handle_update({"message": message(999, 8, "reply", reply_to=50)})
        self.assertEqual(self.api.calls[0][0], "copyMessage")
        self.assertEqual(self.api.calls[0][1]["chat_id"], 123)
        self.assertEqual(self.api.calls[0][1]["from_chat_id"], 999)

    def test_groups_are_ignored(self):
        self.bot.handle_update({"message": message(-100, chat_type="supergroup")})
        self.assertEqual(self.api.calls, [])

    def test_start_only_returns_help(self):
        self.bot.handle_update({"message": message(123, text="/start")})
        self.assertEqual([item[0] for item in self.api.calls], ["sendMessage"])

    def test_block_and_unblock_by_reply(self):
        self.store.add_mapping([50], 123, 7, 1000)
        self.bot.handle_update({"message": message(999, 8, "/block", reply_to=50)})
        self.assertTrue(self.store.is_blocked(123))
        self.bot.handle_update({"message": message(123, 9, "blocked")})
        self.assertNotIn("copyMessage", [item[0] for item in self.api.calls[1:]])
        self.bot.handle_update({"message": message(999, 10, "/unblock", reply_to=50)})
        self.assertFalse(self.store.is_blocked(123))

    def test_rate_limit(self):
        self.bot.handle_update({"message": message(123, 1)})
        self.bot.handle_update({"message": message(123, 2)})
        self.bot.handle_update({"message": message(123, 3)})
        copies = [item for item in self.api.calls if item[0] == "copyMessage"]
        self.assertEqual(len(copies), 2)
        self.assertIn("发送过于频繁", self.api.calls[-1][1]["text"])

    def test_cleanup_expires_old_mappings(self):
        self.store.add_mapping([50], 123, 7, 100)
        self.store.cleanup(31 * 86400, 30, 60)
        self.assertIsNone(self.store.target_for(50))

    def test_token_redaction(self):
        token = "123456:secret-value"
        self.assertNotIn(token, redact(f"url has {token}", token))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
