import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import bot
from test_bot import FakeAPI, message


class VerificationStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'state.sqlite3')
        self.store = bot.Store(self.path)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def verification(self):
        self.assertTrue(hasattr(bot, 'VerificationStore'), 'verification state is not implemented')
        return bot.VerificationStore(self.store.db)

    def grant(self, state, user_id=123, now=1000):
        raw = state.issue(user_id, now)
        self.assertIsNotNone(state.reserve(raw, now))
        self.assertTrue(state.complete(raw, now))
        return raw

    def test_capability_is_hashed_bound_and_consumed(self):
        state = self.verification()
        raw = state.issue(123, 1000)
        self.assertGreaterEqual(len(raw), 43)
        self.assertNotIn(raw, '\n'.join(self.store.db.iterdump()))
        self.assertEqual(state.reserve(raw, 1000), hashlib.sha256(raw.encode()).hexdigest())
        self.assertTrue(state.complete(raw, 1000))
        self.assertTrue(state.verified(123, 1001))
        self.assertFalse(state.verified(456, 1001))
        self.assertFalse(state.complete(raw, 1001))
        self.assertIsNone(state.reserve(raw, 1001))

    def test_expiry_boundaries_and_replacement(self):
        state = self.verification()
        raw = state.issue(123, 1000)
        self.assertIsNone(state.issue(123, 1059))
        replacement = state.issue(123, 1060)
        self.assertIsNone(state.challenge(raw, 1060))
        self.assertIsNotNone(state.challenge(replacement, 1659))
        self.assertIsNone(state.challenge(replacement, 1660))
        self.assertFalse(state.complete(replacement, 1660))

    def test_grant_lasts_30_days_and_survives_new_connection(self):
        state = self.verification()
        self.grant(state)
        other = sqlite3.connect(self.path)
        self.addCleanup(other.close)
        other_state = bot.VerificationStore(other)
        self.assertTrue(other_state.verified(123, 2592999))
        self.assertFalse(other_state.verified(123, 2593000))

    def test_attempt_budget_and_cooldown(self):
        state = self.verification()
        raw = state.issue(123, 1000)
        self.assertIsNotNone(state.reserve(raw, 1000))
        self.assertIsNone(state.reserve(raw, 1002))
        for now in (1003, 1006, 1009, 1012):
            self.assertIsNotNone(state.reserve(raw, now))
        self.assertIsNone(state.reserve(raw, 1015))

    def test_block_during_validation_and_revocation(self):
        state = self.verification()
        raw = state.issue(123, 1000)
        state.reserve(raw, 1000)
        self.store.block(123, 1001)
        self.assertFalse(state.complete(raw, 1002))
        self.assertFalse(state.verified(123, 1002))
        self.assertIsNone(state.issue(123, 1002))
        self.store.unblock(123)
        self.assertIsNone(state.challenge(raw, 1002))
        self.grant(state, now=2000)
        self.store.block(123, 2001)
        self.store.unblock(123)
        self.assertFalse(state.verified(123, 2002))

    def test_cleanup_removes_expired_state(self):
        state = self.verification()
        self.grant(state)
        state.issue(456, 1000)
        state.cleanup(2593000)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM verification_challenges').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM verified_users').fetchone()[0], 0)

    def test_malformed_capabilities_do_not_authorize(self):
        state = self.verification()
        for raw in ('', 'a' * 10000, '../', None, 123, 'a' * 43):
            self.assertIsNone(state.reserve(raw, 1000))
            self.assertFalse(state.complete(raw, 1000))


class VerificationGateTests(unittest.TestCase):
    setUp = VerificationStateTests.setUp
    verification = VerificationStateTests.verification
    grant = VerificationStateTests.grant

    def make_bot(self):
        self.assertIn('verification_url', bot.Config.__dataclass_fields__, 'bot verification gate is not implemented')
        self.api = FakeAPI()
        self.now = 1000
        config = bot.Config(999, self.path, verification_url='https://verify.example.com')
        self.relay = bot.RelayBot(self.api, self.store, config, clock=lambda: self.now)
        return self.verification()

    def test_unverified_sender_only_gets_bound_button(self):
        state = self.make_bot()
        self.relay.handle_update({'message': message(123)})
        self.assertEqual(len(self.api.calls), 1)
        method, payload = self.api.calls[0]
        self.assertEqual((method, payload['chat_id']), ('sendMessage', 123))
        link = payload['reply_markup']['inline_keyboard'][0][0]['url']
        parsed = urlsplit(link)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ('https', 'verify.example.com', '/verify'))
        self.assertIsNotNone(state.reserve(parsed.fragment, 1000))
        self.assertTrue(state.complete(parsed.fragment, 1000))
        self.assertTrue(state.verified(123, 1000))

    def test_start_and_media_cannot_bypass_gate_or_spam_prompts(self):
        self.make_bot()
        self.relay.handle_update({'message': message(123, text='/start')})
        media = message(123, 2)
        del media['text']
        media['photo'] = [{'file_id': 'fake'}]
        self.relay.handle_update({'message': media})
        self.assertEqual(len(self.api.calls), 1)
        self.assertIn('reply_markup', self.api.calls[0][1])
        self.now = 1060
        self.relay.handle_update({'message': media})
        self.assertEqual(len(self.api.calls), 2)

    def test_verified_sender_forwards_until_expiration(self):
        state = self.make_bot()
        self.grant(state)
        self.relay.handle_update({'message': message(123)})
        self.assertEqual([x[0] for x in self.api.calls], ['sendMessage', 'copyMessage', 'sendMessage'])
        self.api.calls.clear()
        self.now = 2593000
        self.relay.handle_update({'message': message(123, 2)})
        self.assertEqual(len(self.api.calls), 1)
        self.assertIn('reply_markup', self.api.calls[0][1])

    def test_owner_reply_and_recover_remain_available(self):
        self.make_bot()
        self.store.add_mapping([50], 123, 7, 1000)
        self.relay.handle_update({'message': message(999, 8, 'reply', reply_to=50)})
        self.assertEqual(self.api.calls[0][0], 'copyMessage')
        self.relay.bot_id = 888
        request = message(999, 9, '/recover')
        request['reply_to_message'] = {
            'message_id': 60, 'from': {'id': 888, 'is_bot': True},
            'text': self.relay.identity_text(message(456, 12)),
        }
        self.relay.handle_update({'message': request})
        self.assertEqual(self.store.target_for(60), (456, 12))

    def test_blocked_verified_sender_never_forwards(self):
        state = self.make_bot()
        self.grant(state)
        self.store.block(123, 1000)
        self.relay.handle_update({'message': message(123)})
        self.assertNotIn('copyMessage', [x[0] for x in self.api.calls])
        self.assertFalse(state.verified(123, 1000))


if __name__ == '__main__':
    unittest.main()
