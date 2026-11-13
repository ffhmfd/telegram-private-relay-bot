import concurrent.futures
import hashlib
import http.client
import io
import json
import sqlite3
import socket
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

import verification
import test_verification as state_tests


class TurnstileClientTests(unittest.TestCase):
    def client(self, response=None, failure=None):
        self.assertTrue(hasattr(verification, 'TurnstileClient'), 'server-side validation is not implemented')
        self.requests = []
        def opener(request, timeout):
            self.requests.append((request, timeout))
            if failure:
                raise failure
            return io.BytesIO(json.dumps(response).encode())
        return verification.TurnstileClient('secret-value', 'verify.example.com', opener=opener)

    def test_only_full_success_authorizes(self):
        valid = {'success': True, 'hostname': 'verify.example.com', 'action': 'private_relay', 'cdata': 'a' * 64}
        client = self.client(valid)
        self.assertTrue(client.verify('real-browser-token', 'a' * 64))
        request, timeout = self.requests[0]
        self.assertEqual(request.full_url, 'https://challenges.cloudflare.com/turnstile/v0/siteverify')
        self.assertEqual(json.loads(request.data), {'secret': 'secret-value', 'response': 'real-browser-token'})
        self.assertLessEqual(timeout, 10)
        for field, value in [('success', False), ('success', 'true'), ('hostname', 'evil.example'), ('action', 'other'), ('cdata', 'b' * 64)]:
            self.assertFalse(self.client({**valid, field: value}).verify('token', 'a' * 64), field)
        for response in ([], None, {}, {'success': True}):
            self.assertFalse(self.client(response).verify('token', 'a' * 64))

    def test_network_failure_fails_closed(self):
        for failure in (TimeoutError(), urllib.error.URLError('private connection details')):
            self.assertFalse(self.client(failure=failure).verify('token', 'a' * 64))

    def test_empty_or_oversized_tokens_never_reach_cloudflare(self):
        client = self.client({})
        for token in ('', None, 123, 'x' * 2049):
            self.assertFalse(client.verify(token, 'a' * 64))
        self.assertEqual(self.requests, [])


class HTTPVerificationTests(unittest.TestCase):
    setUpState = state_tests.VerificationStateTests.setUp
    verification = state_tests.VerificationStateTests.verification

    def setUp(self):
        self.setUpState()
        self.assertTrue(hasattr(verification, 'make_server'), 'verification HTTP service is not implemented')
        self.now = 1000
        self.state = self.verification()
        self.raw = self.state.issue(123, self.now)
        self.cloud_success = True
        self.cloud_requests = []
        self.cloud_hook = None
        def opener(request, timeout):
            self.cloud_requests.append(json.loads(request.data))
            if self.cloud_hook:
                self.cloud_hook()
            return io.BytesIO(json.dumps({
                'success': self.cloud_success, 'hostname': 'verify.example.com',
                'action': 'private_relay', 'cdata': hashlib.sha256(self.raw.encode()).hexdigest(),
            }).encode())
        client = verification.TurnstileClient('test-secret', 'verify.example.com', opener=opener)
        self.server = verification.make_server(('127.0.0.1', 0), self.path, 'test-sitekey', client, clock=lambda: self.now)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def close_server():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
        self.addCleanup(close_server)

    def request(self, path, data=None, headers=None, method=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        base_headers = {'Origin': 'https://verify.example.com', 'Content-Type': 'application/json'}
        base_headers.update(headers or {})
        body = json.dumps(data).encode() if isinstance(data, dict) else data
        try:
            connection.request(method or ('POST' if data is not None else 'GET'), path, body, base_headers)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_http_grants_once_without_relaying_message(self):
        status, body, _ = self.request('/api/challenge', {'challenge': self.raw})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['cdata'], hashlib.sha256(self.raw.encode()).hexdigest())
        payload = {'challenge': self.raw, 'token': 'cloud-token'}
        self.assertEqual(self.request('/api/verify', payload)[0], 200)
        self.assertTrue(self.state.verified(123, self.now))
        self.assertEqual(self.request('/api/verify', payload)[0], 429)
        self.assertEqual(len(self.cloud_requests), 1)

    def test_invalid_and_expired_links_cannot_reach_cloudflare(self):
        for raw in ('', 'x' * 43, '../../', self.raw):
            self.now = 1600
            self.assertEqual(self.request('/api/challenge', {'challenge': raw})[0], 410)
            self.assertEqual(self.request('/api/verify', {'challenge': raw, 'token': 'token'})[0], 429)
        self.assertEqual(self.cloud_requests, [])

    def test_cloudflare_rejection_and_attempt_budget(self):
        self.cloud_success = False
        payload = {'challenge': self.raw, 'token': 'invalid-token'}
        for index in range(5):
            self.now = 1000 + index * 3
            self.assertEqual(self.request('/api/verify', payload)[0], 403)
        self.now = 1015
        self.assertEqual(self.request('/api/verify', payload)[0], 429)
        self.assertFalse(self.state.verified(123, self.now))
        self.assertEqual(len(self.cloud_requests), 5)

    def test_block_or_expiry_during_siteverify_fails_closed(self):
        def block():
            db = sqlite3.connect(self.path)
            with db:
                db.execute('INSERT INTO blocked_users VALUES (123, 1001)')
            db.close()
        self.cloud_hook = block
        self.assertEqual(self.request('/api/verify', {'challenge': self.raw, 'token': 'token'})[0], 410)
        self.assertFalse(self.state.verified(123, 1001))
        self.store.unblock(123)
        self.cloud_hook = lambda: setattr(self, 'now', 1600)
        self.now = 1003
        self.assertEqual(self.request('/api/verify', {'challenge': self.raw, 'token': 'token'})[0], 410)

    def test_concurrent_submission_grants_only_once(self):
        payload = {'challenge': self.raw, 'token': 'token'}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: self.request('/api/verify', payload)[0], range(2)))
        self.assertEqual(sorted(statuses), [200, 429])
        self.assertEqual(len(self.cloud_requests), 1)

    def test_input_validation_and_origin(self):
        for data in (b'not json', b'[]', b'null', b'{"challenge":"a","challenge":"b"}'):
            self.assertEqual(self.request('/api/verify', data)[0], 400)
        self.assertEqual(self.request('/api/verify', b'x' * 8193)[0], 413)
        self.assertEqual(self.request('/api/verify', {}, {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/verify', {}, {'Origin': ''})[0], 403)
        self.assertEqual(self.request('/api/verify', {}, {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('/api/verify', {'challenge': self.raw, 'token': 'x' * 2049})[0], 400)
        self.assertEqual(self.cloud_requests, [])

    def test_static_page_and_security_headers(self):
        status, body, headers = self.request('/verify')
        self.assertEqual(status, 200)
        self.assertIn(b'test-sitekey', body)
        self.assertNotIn(b'test-secret', body)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(self.request('/verify.js')[0], 200)
        self.assertEqual(self.request('/verify.css')[0], 200)
        self.assertEqual(self.request('/../verification.py')[0], 404)
        self.assertEqual(self.request('/api/verify', method='GET')[0], 404)

    def test_completion_is_atomic_across_connections(self):
        self.state.reserve(self.raw, self.now)
        def complete(_):
            db = sqlite3.connect(self.path, timeout=10)
            try:
                return verification.VerificationStore(db).complete(self.raw, self.now)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(complete, range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_trickled_requests_have_absolute_deadline_and_release_workers(self):
        self.server.request_deadline = 0.25
        def trickle(_):
            connection = socket.create_connection(self.server.server_address, timeout=2)
            try:
                connection.sendall(b'POST /api/challenge HTTP/1.1\r\nHost: verify.example.com\r\nOrigin: https://verify.example.com\r\nContent-Type: application/json\r\nContent-Length: 2000\r\n\r\n')
                end = time.monotonic() + 1.2
                while time.monotonic() < end:
                    try:
                        connection.sendall(b' ')
                    except OSError:
                        return True
                    threading.Event().wait(0.03)
                return False
            finally:
                connection.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            closed = list(pool.map(trickle, range(8)))
        self.assertTrue(all(closed), 'slow upload clients retained HTTP workers past the absolute deadline')
        self.assertEqual(self.request('/verify')[0], 200)

    def test_readiness_accepts_expected_service_page(self):
        self.assertTrue(hasattr(verification, 'check_readiness'), 'HTTP readiness check is missing')
        verification.check_readiness({'address': self.server.server_address, 'site_key': 'test-sitekey'}, attempts=1)


class ConfigTests(unittest.TestCase):
    def test_verification_origin_validation(self):
        for bad in ('http://example.com', 'https://u:p@example.com', 'https://example.com/path', 'https://example.com#x', 'https://example.com?x', 'https://example.com:8443'):
            with self.assertRaises(ValueError):
                verification.verification_url(bad)
        self.assertEqual(verification.verification_url('https://verify.example.com/'), 'https://verify.example.com')

    def test_enabled_web_service_requires_real_configuration(self):
        self.assertTrue(hasattr(verification, 'load_web_config'), 'web service configuration is not implemented')
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises((ValueError, SystemExit)):
                verification.load_web_config()


if __name__ == '__main__':
    unittest.main()
