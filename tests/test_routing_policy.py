"""Neutral defaults and byte-level replay of synthetic quota snapshots."""
from datetime import datetime
import json
import os
import re
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from murmuratio_app.config import Config
from murmuratio_app.metrics.quota import quota_routing
from tests.routing_fixture import replay

FIXTURES = Path(__file__).parent / 'fixtures'


class RoutingPolicyTests(unittest.TestCase):
    def test_fresh_config_has_no_calendar_reservation_or_named_tiers(self):
        sample = json.loads((FIXTURES / 'routing-snapshot.json').read_text())['samples'][0]
        routing = quota_routing(sample['limits'], sample['history'], sample['now'], sample['credits'],
                                credits_expire_at=sample['now'] + 30 * 86400)
        self.assertNotIn('reserved_until', routing['codex']['credits'])
        self.assertEqual(routing['codex_boost'], {})
        self.assertEqual(routing['tiers'], {t: dict(simple='default', hard='default') for t in ('claude', 'codex')})

    def test_configured_policy_preserves_file_and_api_bytes(self):
        previous = os.environ.get('TZ')
        with patch.dict(os.environ, {'TZ': 'Asia/Almaty'}):
            time.tzset()
            try:
                samples = json.loads((FIXTURES / 'routing-snapshot.json').read_text())['samples']
                self.assertEqual([json.loads(s['expected']['routing'])['codex_mode']['id'] for s in samples],
                                 ['surplus', 'surplus', 'saving'])
                for sample in samples:
                    with self.subTest(now=sample['now']), tempfile.TemporaryDirectory() as home:
                        self.assertEqual(replay(Path(home), sample, FIXTURES / 'routing-policy.toml'), sample['expected'])
            finally:
                if previous is None:
                    os.environ.pop('TZ', None)
                else:
                    os.environ['TZ'] = previous
                time.tzset()

    def test_calendar_and_tier_names_are_configurable(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / 'config.toml'
            path.write_text('[routing]\ncredits_reserve_months = [3]\ncredits_start = "04-01"\n'
                            'codex_tiers = { simple = "small", hard = "large" }\n'
                            'surplus_boost = { small = "large" }\nsurplus_effort = "high"\n'
                            'surplus_title = "Surplus: large"\ncredit_days_before_reset = 1\n')
            policy = Config.load(home, path).routing_policy
            now = datetime(2027, 3, 5, 12).timestamp()
            reset = now + 4 * 86400
            routing = quota_routing({'codex': [dict(window_minutes=10080, used_percent=10, resets_at=reset)]},
                                    [], now, dict(balance=1000), credits_expire_at=now + 60 * 86400, policy=policy)
            self.assertEqual(routing['codex']['credits']['reserved_until'], datetime(2027, 4, 1).timestamp())
            self.assertEqual(routing['tiers']['codex'], dict(simple='small', hard='large'))
            self.assertEqual(routing['codex_mode']['title'], 'Surplus: large')
            self.assertEqual((routing['codex_boost'], routing['codex_effort']), ({'small': 'large'}, 'high'))
            self.assertEqual(routing['codex']['credits']['credits_plan']['credit_days_start'], reset - 86400)
            self.assertIsNone(policy.reserve_until(datetime(2027, 4, 1).timestamp()))
            self.assertEqual(policy.spend_from(datetime(2027, 4, 5).timestamp()), datetime(2027, 4, 1).timestamp())

    def test_application_contains_no_personal_routing_names(self):
        forbidden = re.compile(r'november|december|astra|terra|luna', re.I)
        for path in (Path(__file__).parents[1] / 'murmuratio_app').rglob('*.py'):
            self.assertIsNone(forbidden.search(path.read_text()), str(path))

    def test_invalid_policy_fails_before_any_routing_write(self):
        for setting in ('credits_reserve_months = [0]', 'credits_reserve_months = [true]',
                        'credits_start = "13-01"', 'surplus_effort = "invalid"',
                        'codex_tiers = "invalid"', 'surplus_boost = { small = 2 }',
                        'credit_days_before_reset = -1'):
            with self.subTest(setting=setting), tempfile.TemporaryDirectory() as home:
                path = Path(home) / 'config.toml'
                path.write_text('[routing]\n' + setting + '\n')
                with self.assertRaises(ValueError):
                    Config.load(home, path)
