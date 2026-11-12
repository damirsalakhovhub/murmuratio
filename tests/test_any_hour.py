"""The reset boundary must work through provider files, routing publication and HTTP."""
from http.server import ThreadingHTTPServer
from datetime import datetime
import http.client
import json
import math
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace

from tests.routing_fixture import example_policy
from murmuratio_app.metrics.quota import credits_plan, quota_routing, week_forecast
from murmuratio_app.server import make_handler
from tests.any_hour_fixture import FIXTURE, START, WEEK, staged_source


class AnyHourTests(unittest.TestCase):
    def test_provider_files_publish_useful_state_at_each_week_boundary(self):
        for moment in FIXTURE['moments']:
            with self.subTest(moment=moment), tempfile.TemporaryDirectory() as directory:
                source = staged_source(Path(directory), moment)
                state = source.state()
                self.assertEqual(json.loads(source.config.routing_file.read_text()), state['routing'])
                if moment in ('empty', 'expired'):
                    self.assertEqual(state['forecast'], dict(claude=None, codex=None))
                    self.assertEqual(state['routing']['codex_boost'], {})
                    self.assertFalse(state['routing']['codex_fast'])
                    continue
                for tool in ('claude', 'codex'):
                    forecast = state['forecast'][tool]
                    self.assertTrue(math.isfinite(forecast['pace_per_day']))
                    self.assertTrue(math.isfinite(forecast['projected_at_reset']))
                    self.assertGreaterEqual(forecast['projected_at_reset'], forecast['used_percent'])
                    self.assertIn(tool, state['burn'])
                if moment in ('fresh', 'five-minutes', 'one-hour', 'last-hour'):
                    self.assertEqual(state['routing']['codex_boost'], {'luna': 'sol'})
                    self.assertTrue(state['routing']['codex_fast'])
                elif moment == 'midweek':
                    self.assertEqual(state['routing']['codex_boost'], {'terra': 'sol', 'sol': 'luna'})
                    self.assertFalse(state['routing']['codex_fast'])
                elif moment == 'overspend':
                    self.assertEqual(state['routing']['codex_boost'], {'terra': 'sol', 'sol': 'luna'})
                    self.assertFalse(state['routing']['codex_fast'])
                if moment in ('fresh', 'five-minutes', 'one-hour'):
                    self.assertNotEqual(state['credits']['credits_plan']['status'], 'over')
                    # The synthetic debit spans the reset; no opening balance was observed in this week.
                    self.assertEqual(state['credits']['credits_plan']['spent_this_week'], 0)
                    self.assertEqual(state['forecast']['codex']['credits_per_percent'], 250)
                if moment == 'one-hour':
                    self.assertIsNone(state['credits']['per_hour'])

    def test_fresh_reset_survives_the_http_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            source = staged_source(Path(directory), 'fresh')
            source.state()
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(now=source, config=source.config))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
                try:
                    connection.request('GET', '/api/now')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    state = json.loads(response.read())
                finally:
                    connection.close()
                self.assertGreater(state['forecast']['codex']['projected_at_reset'], 0)
                self.assertEqual(state['routing']['codex_boost'], {'luna': 'sol'})
                self.assertNotEqual(state['credits']['credits_plan']['status'], 'over')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_forecasts_use_defaults_history_and_planner_then_blend_to_live(self):
        reset = START + WEEK
        window = dict(window_minutes=10080, used_percent=0, resets_at=reset)
        history = [dict(at=START - 86400, tool=tool, window='week', used_percent=60, resets_at=START)
                   for tool in ('claude', 'codex')]
        for tool in ('claude', 'codex'):
            with self.subTest(tool=tool):
                default = week_forecast(tool, window, [], START)
                self.assertAlmostEqual(default['projected_at_reset'], 100)
                previous = week_forecast(tool, window, history, START)
                self.assertGreater(previous['projected_at_reset'], 70)
                self.assertLess(previous['projected_at_reset'], 100)
                planned = week_forecast(tool, window, [], START, expected_per_day=8)
                self.assertEqual(planned['projected_at_reset'], 56)
                first = week_forecast(tool, dict(window, used_percent=4), [], START + 86400, expected_per_day=8)
                self.assertEqual(first['live_weight'], 0.5)
                self.assertEqual(first['pace_per_day'], 6)
                mature = week_forecast(tool, dict(window, used_percent=12), [], START + 3 * 86400, expected_per_day=8)
                self.assertEqual(mature['pace_per_day'], 4)
                self.assertEqual(mature['live_weight'], 1)
                # Future/current windows and the other tool cannot contaminate the prior.
                extra = history + [dict(history[0], resets_at=reset, used_percent=100)]
                self.assertEqual(week_forecast(tool, window, extra, START)['prior_per_day'], previous['prior_per_day'])

    def test_night_reset_keeps_a_work_hour_projection_and_no_infinite_values(self):
        window = dict(window_minutes=10080, used_percent=0, resets_at=START + WEEK)
        f = week_forecast('codex', window, [], START, work_hours=(9, 21))
        self.assertGreater(f['rate_per_hour'], 0)
        self.assertGreater(f['projected_at_reset'], 0)
        self.assertEqual(f['rate_per_hour'], f['pace_per_day'] / 12)
        json.dumps(f, allow_nan=False)

    def test_credit_budget_is_prorated_with_a_small_tolerance(self):
        reset = START + WEEK
        for seconds in (0, 1, 300, 3600):
            with self.subTest(seconds=seconds):
                p = credits_plan(0, reset, 9400, 0.92, START + seconds)
                self.assertAlmostEqual(p['expected_by_now'], 9400 * seconds / WEEK)
                self.assertNotEqual(p['status'], 'over')
                self.assertEqual(credits_plan(0, reset, 9400, 1000, START + seconds)['status'], 'over')
        now = START + WEEK / 2
        for spent, status in ((2500, 'under'), (4700, 'on'), (7000, 'over')):
            self.assertEqual(credits_plan(50, reset, 9400, spent, now)['status'], status)
        self.assertEqual(credits_plan(0, reset, 0, 0, START)['status'], 'on')
        self.assertEqual(credits_plan(0, reset, 0, 10, START)['status'], 'over')

    def test_a_credit_warning_cannot_downshift_abundant_quota(self):
        # A genuine credit-budget excess still leaves ample included allowance.
        limits = {'codex': [dict(window_minutes=10080, used_percent=10, resets_at=START + WEEK)]}
        routing = quota_routing(limits, [], START + 3 * 86400,
                                dict(balance=1000, week_start_balance=6000),
                                credits_expire_at=START + 40 * 86400, policy=example_policy())
        self.assertEqual(routing['codex']['credits']['credits_plan']['status'], 'over')
        self.assertEqual(routing['codex_boost'], {'luna': 'sol'})
        self.assertTrue(routing['codex_fast'])

    def test_fast_follows_the_server_surplus_mode(self):
        now = START + WEEK / 2
        for used, boost, fast in ((45, {'luna': 'sol'}, True),
                                  (20, {'luna': 'sol'}, True),
                                  (50, {'terra': 'sol', 'sol': 'luna'}, False), (80, {'terra': 'sol', 'sol': 'luna'}, False)):
            with self.subTest(used=used):
                limits = {'codex': [dict(window_minutes=10080, used_percent=used, resets_at=START + WEEK)]}
                result = quota_routing(limits, [], now, policy=example_policy())
                self.assertEqual((result['codex_boost'], result['codex_fast']), (boost, fast))

    def test_credit_phase_uses_measured_spend_after_the_quota_stops_rising(self):
        now, reset = START + 6 * 86400, START + WEEK
        limits = {'codex': [dict(window_minutes=10080, used_percent=100, resets_at=reset)]}
        for burn, boost, fast in ((10000, {'terra': 'sol', 'sol': 'luna'}, False),
                                  (20000, {'terra': 'sol', 'sol': 'luna'}, False)):
            with self.subTest(burn=burn):
                credits = dict(balance=14000, week_start_balance=24000, spent_per_day=burn)
                result = quota_routing(limits, [], now, credits, credits_expire_at=reset, policy=example_policy())
                self.assertEqual((result['codex_boost'], result['codex_fast']), (boost, fast))
        forecast = week_forecast('codex', limits['codex'][0], [], now, credit_burn_per_day=40)
        self.assertEqual(forecast['pace_source'], 'live credits')
        self.assertEqual(forecast['projected_at_reset'], 140)

    def test_last_hour_at_night_is_flat_but_remaining_quota_can_still_be_used(self):
        reset = datetime(2026, 11, 16, 1).timestamp()
        now = reset - 3600
        window = dict(window_minutes=10080, used_percent=80, resets_at=reset)
        forecast = week_forecast('codex', window, [], now, work_hours=(9, 21))
        self.assertEqual(forecast['projected_at_reset'], 80)
        result = quota_routing({'codex': [window]}, [], now, work_hours=(9, 21), policy=example_policy())
        self.assertEqual(result['codex_boost'], {'luna': 'sol'})
        self.assertTrue(result['codex_fast'])

    def test_last_minute_does_not_invent_a_minimum_task_size(self):
        reset = START + WEEK
        window = dict(window_minutes=10080, used_percent=99.5, resets_at=reset)
        result = quota_routing({'codex': [window]}, [], reset - 60, policy=example_policy())
        self.assertEqual(result['codex_boost'], {'luna': 'sol'})
        self.assertTrue(result['codex_fast'])

    def test_planner_estimate_does_not_enable_its_routing_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = staged_source(Path(directory), 'fresh')
            now = START + 5400
            week = dict(window_minutes=10080, used_percent=1, resets_at=START + WEEK)
            credits = dict(balance=60000, per_hour=300, week_start_balance=60000, month_start_balance=60000)
            expected = source.routing({'codex': [week]}, now, credits)
            self.assertEqual(expected['codex_boost'], {'luna': 'sol'})
            self.assertTrue(expected['codex_fast'])
            # A completed advisory plan used to silently replace the heuristic's
            # demand and credit conversion, removing its fresh-week tier boost.
            source.planner = SimpleNamespace(result=dict(status='ok', levels=[dict(rate=2.5)], default_level=0,
                                                        credits=dict(per_percent=195.8)))
            result = source.routing({'codex': [week]}, now, credits)
            self.assertEqual(source.forecast['codex']['pace_source'], 'history and default')
            self.assertNotIn('codex_plan', result)
            self.assertFalse(hasattr(source.config, 'planner_drives_routing'))
            self.assertEqual(result, expected)
            self.assertEqual(json.loads(source.config.routing_file.read_text()), expected)


if __name__ == '__main__':
    unittest.main()
