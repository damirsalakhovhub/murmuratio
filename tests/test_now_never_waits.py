"""/api/now stays responsive while a source stalls; retired planner routes stay retired."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from murmuratio_app.config import Config
from murmuratio_app.now import NowSource
from murmuratio_app.server import make_handler

LIMIT = 1.5


class SlowBoardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.source = NowSource(self.home)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(now=self.source, config=self.source.config))
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def get(self, path='/api/now'):
        started = time.monotonic()
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=10)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            return response.status, response.read(), time.monotonic() - started
        finally:
            connection.close()

    def test_slow_board_state_serves_last_snapshot_without_holding_requests(self):
        status, body, _ = self.get()
        first = json.loads(body)
        self.assertEqual(status, 200)
        self.assertNotIn('stale', first)
        blocked, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def desktop():
            blocked.set()
            release.wait(10)
            return {}
        self.source.desktop_sessions = desktop
        for _ in range(3):
            status, body, seconds = self.get()
            state = json.loads(body)
            self.assertEqual(status, 200)
            self.assertLess(seconds, LIMIT)
            self.assertTrue(state['stale'])
            self.assertEqual(state['agents'], first['agents'])
        self.assertTrue(blocked.is_set())
        release.set()
        self.assertTrue(self.source.refreshing.wait(3))
        status, body, _ = self.get()
        self.assertEqual(status, 200)
        self.assertNotIn('stale', json.loads(body))

    def test_calculator_and_old_plan_redirect_do_not_compute_on_the_request_thread(self):
        with patch.object(self.source, 'state', side_effect=AssertionError('computed state')):
            for path, expected in (('/calculator', 200), ('/codex-plan', 302), ('/api/codex-plan', 404)):
                status, _, seconds = self.get(path)
                self.assertEqual(status, expected)
                self.assertLess(seconds, LIMIT)

    def test_no_state_yet_answers_503_without_waiting(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.source.desktop_sessions = lambda: release.wait(10) and {}
        with patch('murmuratio_app.now.FIRST_REQUEST_SECONDS', 0.5):
            status, body, seconds = self.get()
        self.assertEqual(status, 503)
        self.assertIn('error', json.loads(body))
        self.assertLess(seconds, LIMIT)
