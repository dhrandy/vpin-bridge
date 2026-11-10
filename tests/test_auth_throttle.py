"""Synthetic wrong-key lockout checks. Valid keys are never throttled."""
import http.client
import importlib.util
import json
from pathlib import Path
import secrets
import threading
import unittest

spec = importlib.util.spec_from_file_location('throttle_bridge', str(Path(__file__).resolve().parents[1] / 'bridge.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
b.API_KEY = secrets.token_urlsafe(32)
b.cabinet_get = lambda path, **kw: {'gameId': 0}
server = b.LimitedServer(('127.0.0.1', 0), b.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def request(path='/health', key='wrong', xff=None):
    h = {}
    if key is not None:
        h['X-Api-Key'] = key
    if xff is not None:
        h['X-Forwarded-For'] = xff
    c = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    c.request('GET', path, headers=h)
    r = c.getresponse(); data = json.loads(r.read()); c.close()
    return r.status, data


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        with b._AUTH_FAILURES_LOCK:
            b._AUTH_FAILURES.clear()
        self.old = (b.AUTH_MAX_ATTEMPTS, b.AUTH_WINDOW_SECONDS, b.TRUSTED_PROXIES)
        b.AUTH_MAX_ATTEMPTS = 5
        b.AUTH_WINDOW_SECONDS = 900
        b.TRUSTED_PROXIES = []

    def tearDown(self):
        b.AUTH_MAX_ATTEMPTS, b.AUTH_WINDOW_SECONDS, b.TRUSTED_PROXIES = self.old

    def test_wrong_keys_lock_out_after_limit(self):
        for _ in range(5):
            self.assertEqual(request()[0], 401)
        code, data = request()
        self.assertEqual(code, 429)
        self.assertGreater(data['retryAfterSeconds'], 0)
        self.assertNotIn('127.0.0.1', json.dumps(data))

    def test_valid_key_passes_during_lockout_and_resets(self):
        for _ in range(5):
            request()
        self.assertEqual(request()[0], 429)
        # A valid key is never throttled, even mid-lockout, and clears the count.
        self.assertEqual(request(key=b.API_KEY)[0], 200)
        self.assertEqual(request()[0], 401)

    def test_valid_key_burst_unaffected(self):
        for _ in range(4):
            request()
        for _ in range(50):
            code, data = request(key=b.API_KEY)
            self.assertEqual(code, 200)
            self.assertTrue(data['ok'])
        with b._AUTH_FAILURES_LOCK:
            self.assertEqual(b._AUTH_FAILURES, {})

    def test_missing_key_never_counts(self):
        for _ in range(20):
            self.assertEqual(request(key=None)[0], 401)

    def test_window_expiry_restarts_count(self):
        b.AUTH_WINDOW_SECONDS = 0
        for _ in range(8):
            self.assertEqual(request()[0], 401)

    def test_xff_ignored_without_trusted_proxy(self):
        for _ in range(5):
            request(xff='203.0.113.1')
        # Lockout is keyed to the real peer; a fresh spoofed header does not dodge it.
        self.assertEqual(request(xff='203.0.113.2')[0], 429)

    def test_xff_honored_from_trusted_proxy_per_client(self):
        b.TRUSTED_PROXIES = [b.ipaddress.ip_network('127.0.0.0/8')]
        for _ in range(5):
            request(xff='203.0.113.1')
        self.assertEqual(request(xff='203.0.113.1')[0], 429)
        # A different client behind the same proxy keeps its own counter.
        self.assertEqual(request(xff='203.0.113.2')[0], 401)
        # Valid key from the throttled client still passes.
        self.assertEqual(request(key=b.API_KEY, xff='203.0.113.1')[0], 200)

    def test_xff_malformed_falls_back_to_peer(self):
        for _ in range(5):
            request(xff='not-an-ip')
        self.assertEqual(request(xff='also-not-an-ip')[0], 429)


def tearDownModule():
    server.shutdown(); server.server_close()


if __name__ == '__main__':
    unittest.main()
