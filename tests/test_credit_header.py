"""Synthetic credit readings across a weekly reset, with no live file writes."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from murmuratio_app.adapters.parsing import consume_credits
from murmuratio_app.adapters.shared import JsonlSummary
from murmuratio_app.adapters.wrapper import CodexWrapperAdapter
from tests.routing_fixture import example_policy
from murmuratio_app.config import Config
from murmuratio_app.metrics.quota import balance_at, balance_since, quota_routing
from murmuratio_app.metrics.usage import credit_totals
from murmuratio_app.now import NowSource


FIXTURES = Path(__file__).parent / 'fixtures/adapters'


class CreditHeaderTests(unittest.TestCase):
    def test_snapshot_publishes_the_reserve_and_observed_week_spend(self):
        records = (FIXTURES / 'codex-credit-boundary.jsonl').read_text()
        now = datetime.fromisoformat(json.loads(records.splitlines()[-1])['timestamp']).timestamp() + 1
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / '.codex/sessions/2026/11/10/rollout-credits.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text(records)
            config = Config.load(home)
            config.routing_policy = example_policy()
            config.modules.update(routing=True, codex_credits=True)
            source = NowSource(home, config=config, clock=lambda: now)
            status, value = source._collect_codex_usage(now, True, False)
            source.usage_cache['codex_meter'] = dict(key=(True, False), at=now, status=status, value=value)
            state = source.state()
            self.assertEqual(state['credits']['reserved_until'], datetime(2026, 12, 1).timestamp())
            self.assertEqual(state['credits']['balance'], 48000)
            self.assertEqual(state['credits']['credits_plan']['spent_this_week'], 0)
            self.assertEqual(state['routing']['codex']['credits']['month_pool'], 0)

    def test_synthetic_balance_drop_crossing_reset_is_not_this_weeks_spend(self):
        fixture = json.loads((FIXTURES / 'codex-credit-header.json').read_text())
        readings = []
        for line in (FIXTURES / 'codex-credit-boundary.jsonl').read_text().splitlines():
            data = {}
            consume_credits(data, json.loads(line))
            readings.append(dict(points=data['points'], latest=data['latest'], usage=[]))
        credits, _, points = credit_totals(readings, True, fixture['at'])
        start = fixture['reset'] - 7 * 86400
        # The old calculation attributed this entire overnight gap to the new week.
        self.assertAlmostEqual(balance_at(points, start) - credits['balance'], 240)
        credits['week_start_balance'] = balance_since(points, start)
        limits = {'codex': [dict(window_minutes=10080, used_percent=44, resets_at=fixture['reset'])]}
        routing = quota_routing(limits, [], fixture['at'], credits,
                                credits_expire_at=fixture['credits']['month_ends_at'], policy=example_policy())
        pool = routing['codex']['credits']
        self.assertEqual(pool['balance'], 48000)
        self.assertEqual(pool['reserved_until'], datetime(2026, 12, 1).timestamp())
        self.assertEqual((pool['month_left'], pool['week_share_left']), (0, 0))
        self.assertEqual(pool['credits_plan']['spent_this_week'], 0)
        self.assertEqual(pool['credits_plan']['status'], 'on')
        # A debit actually observed after this week's opening still counts.
        credits['balance'] -= 12
        pool = quota_routing(limits, [], fixture['at'], credits,
                             credits_expire_at=fixture['credits']['month_ends_at'], policy=example_policy())['codex']['credits']
        self.assertEqual(pool['credits_plan']['spent_this_week'], 12)
        self.assertEqual(pool['credits_plan']['status'], 'over')
        self.assertIsNone(balance_since(points[:1], start))

    def test_december_uses_the_balance_without_a_reservation(self):
        now = datetime(2026, 12, 1, 12).timestamp()
        routing = quota_routing({'codex': [dict(window_minutes=10080, used_percent=100,
                                               resets_at=now + 86400)]}, [], now,
                                dict(balance=48000),
                                credits_expire_at=datetime(2027, 1, 1).timestamp(), policy=example_policy())
        pool = routing['codex']['credits']
        self.assertNotIn('reserved_until', pool)
        self.assertEqual(pool['month_left'], pool['balance'])
        self.assertGreater(pool['week_share_left'], 0)

    def test_continuous_rollout_keeps_the_new_week_opening_even_without_a_debit(self):
        records = [json.loads(line) for line in (FIXTURES / 'codex-credit-boundary.jsonl').read_text().splitlines()]
        data = {}
        consume_credits(data, records[0])
        # The same long-running session can cross a reset without spending credits.
        opening = records[1]
        opening['payload']['rate_limits']['credits']['balance'] = '48240'
        consume_credits(data, opening)
        consume_credits(data, records[2])
        self.assertEqual(balance_since(data['points'], 1794769200 - 7 * 86400), 48240)
        self.assertAlmostEqual(data['points'][1][1] - data['latest'][1], 240)


class BlockedWrapperTests(unittest.TestCase):
    def test_current_codex_task_blocked_metadata_is_a_completed_observation(self):
        run = json.loads((FIXTURES / 'codex-wrapper-blocked.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'run.json'
            path.write_text(json.dumps(run))
            cache = {}
            def summary(events, consume):
                return cache.setdefault(events, JsonlSummary()).refresh(events, consume)
            adapter = CodexWrapperAdapter(root, summary, cache, lambda thread: '/work/example',
                                          lambda thread, seen: {}, lambda cwd: 'Example', lambda pid: True)
            result = adapter.read(run['ended'] + 1, set())
            self.assertEqual(result.status, 'available')
            agent, = result.values()
            self.assertEqual(agent['status'], 'blocked')
            self.assertTrue(agent['completed'])
            self.assertEqual(agent['ended'], run['ended'])
            self.assertEqual(agent['thread'], run['thread'])
            self.assertEqual(agent['verdict'], run['verdict'])
            # Finished blocked runs expire like done runs, even with a live/reused pid.
            self.assertEqual(adapter.read(run['ended'] + 2 * 86400, set()).values(), [])


if __name__ == '__main__':
    unittest.main()
