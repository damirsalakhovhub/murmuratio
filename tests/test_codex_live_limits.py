"""Live Codex limits read through Codex's own app server, with a fake `codex` binary."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from murmuratio_app.adapters.base import UnavailableSource
from murmuratio_app.adapters.quota_sources import app_server_limits
from murmuratio_app.config import Config
from murmuratio_app.now import NowSource

FAKE = Path(__file__).parent / 'fixtures/codex-app-server/codex'
NOW = 1794113000.0  # 2026-11-08, before the fixture's weekly reset


class CodexLiveLimitsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.log = self.home / 'requests.jsonl'
        patcher = patch.dict(os.environ, {'FAKE_CODEX_LOG': str(self.log)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_windows_and_balance_with_read_only_requests(self):
        at, windows, balance = app_server_limits(str(FAKE), clock=lambda: NOW)
        self.assertEqual(at, NOW)
        self.assertEqual(windows, [dict(window_minutes=10080, used_percent=99, resets_at=1794207458.0)])
        self.assertAlmostEqual(balance, 60831.6998586665)
        methods = [json.loads(line).get('method') for line in self.log.read_text().splitlines()]
        self.assertEqual(methods, ['initialize', 'initialized', 'account/rateLimits/read'])

    def test_errors_timeouts_and_missing_binary_are_unavailable(self):
        for mode in ('error', 'hang'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'FAKE_CODEX_MODE': mode}):
                started = time.monotonic()
                with self.assertRaises(UnavailableSource):
                    app_server_limits(str(FAKE), timeout=2)
                self.assertLess(time.monotonic() - started, 6)
        with self.assertRaises(UnavailableSource):
            app_server_limits(str(self.home / 'missing-codex'))

    def write_rollout(self, at, used):
        path = self.home / '.codex/sessions/2026/11/08/rollout-test.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'type': 'event_msg', 'timestamp': at, 'payload': {'rate_limits': {
            'primary': {'used_percent': used, 'window_minutes': 10080, 'resets_at': 1794207458}}}}) + '\n')

    def source(self, live):
        config_path = self.home / '.config/murmuratio/config.toml'
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(f'[modules]\nforecast = true\nrouting = true\ncodex_credits = true\n'
                               f'[limits]\ncodex_live = {str(live).lower()}\n[paths]\ncodex_cli = "{FAKE}"\n')
        return NowSource(self.home, config=Config.load(self.home), clock=lambda: NOW)

    def wait_live(self, source):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = source.state()
            if source.usage_cache.get('codex_live_limits'):
                return source.state()
            time.sleep(0.05)
        self.fail('live limits read finished')

    def test_live_read_replaces_a_stale_rollout_sample(self):
        # The owner barely uses Codex: its last rollout sample is two hours old.
        self.write_rollout(NOW - 7200, 97)
        state = self.wait_live(self.source(True))
        self.assertEqual(state['limits']['codex'], [dict(window_minutes=10080, used_percent=99, resets_at=1794207458.0)])
        self.assertNotIn('codex_limits', state.get('sources', {}))
        self.assertNotIn('last_known_age_seconds', state['forecast']['codex'])
        self.assertAlmostEqual(state['credits']['balance'], 60831.6998586665)
        self.assertEqual(state['credits']['at'], NOW)

    def test_newer_rollout_sample_wins_and_live_read_is_off_by_default(self):
        self.write_rollout(NOW + 1, 98)
        state = self.wait_live(self.source(True))
        self.assertEqual(state['limits']['codex'][0]['used_percent'], 98)
        self.log.unlink()
        self.write_rollout(NOW - 7200, 97)
        source = self.source(False)
        state = source.state()
        self.assertEqual(state['limits']['codex'][0]['used_percent'], 97)
        self.assertEqual(state['sources']['codex_limits']['status'], 'stale')
        self.assertEqual(state['forecast']['codex']['last_known_age_seconds'], 7200)
        time.sleep(0.2)
        self.assertFalse(self.log.exists(), 'Codex is never started unless enabled')


if __name__ == '__main__':
    unittest.main()
