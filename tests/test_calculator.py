"""Fixed-time policy regressions and publication through the real snapshot boundary."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from tests.routing_fixture import example_policy
from tests.timezone_helpers import local_timezone
from murmuratio_app.config import Config
from murmuratio_app.metrics.quota import (FAST_PERCENT_FACTOR, calculator_timeline, codex_mode, credits_expiry, manual_reset_started,
                                         quota_routing, week_forecast)
from murmuratio_app.now import NowSource

DAY = 86400

def at(value):
    return datetime.fromisoformat(value + '+05:00').timestamp()


class CalculatorTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(local_timezone('Asia/Almaty'))
        self.now = at('2026-11-10T12:51')
        self.reset = at('2026-11-16T12:51')
        self.resets = (at('2026-12-01T00:51'), at('2026-12-02T03:26'))

    def mode(self, used, projected, now=None, reset=None, resets=None):
        now, reset = now or self.now, reset or self.reset
        forecast = dict(used_percent=used, resets_at=reset, projected_at_reset=projected,
                        pace_per_day=(projected - used) / ((reset - now) / DAY))
        return codex_mode(forecast, self.resets if resets is None else resets, now, policy=example_policy())

    def test_reset_requires_99_percent_and_three_days(self):
        self.assertEqual(self.mode(99, 110)['id'], 'reset')
        self.assertEqual(self.mode(98.9, 110)['id'], 'surplus')
        now = at('2026-11-13T12:51')
        self.assertEqual(self.mode(99, 199, now=now)['id'], 'reset')
        # There is still a later November opportunity after Monday, at this pace.
        now += 1
        self.assertNotEqual(self.mode(99, 199, now=now)['id'], 'reset')

    def test_november_30_last_chance_ignores_three_day_minimum(self):
        now, reset = at('2026-11-30T12:00'), at('2026-12-01T12:00')
        mode = self.mode(99, 120, now=now, reset=reset)
        self.assertEqual(mode['id'], 'reset')
        self.assertIn('last chance', mode['reason'])
        self.assertNotEqual(self.mode(98, 120, now=now, reset=reset)['id'], 'reset')

    def test_last_chance_when_next_natural_window_cannot_fill_in_november(self):
        now, reset = at('2026-11-27T12:00'), at('2026-11-29T12:00')
        self.assertEqual(self.mode(99, 139, now=now, reset=reset)['id'], 'reset')

    def test_manual_reset_restarts_window_when_counting_later_opportunities(self):
        now, reset = at('2026-11-19T12:00'), at('2026-11-21T12:00')
        # At 20%/day only one more 99% crossing fits after the natural reset;
        # the second manual reset would push its fresh window into December.
        mode = self.mode(99, 139, now=now, reset=reset)
        self.assertEqual(mode['id'], 'reset')
        self.assertEqual(mode['reset_plan'][0], now)
        self.assertNotEqual(self.mode(99, 139, now=now, reset=reset, resets=self.resets[:1])['id'], 'reset')

    def test_expired_and_missing_resets_cannot_be_pressed_or_add_surplus(self):
        for resets in ((), (self.now - 1,), (self.now,)):
            mode = self.mode(99, 200, resets=resets)
            self.assertEqual(mode['id'], 'saving')
            self.assertIsNone(mode['reset_reminder'])
            self.assertEqual(mode['reset_share'], 0)

    def test_unused_november_resets_add_to_the_burn_target(self):
        self.assertEqual(self.mode(42, 120, resets=())['id'], 'saving')
        mode = self.mode(42, 120)
        self.assertEqual(mode['id'], 'surplus')
        weeks_left = (at('2026-12-01T00:00') - self.now) / (7 * DAY)
        self.assertAlmostEqual(mode['reset_share'], 200 / weeks_left)
        self.assertIn('2 resets', mode['reason'])

    def test_earlier_reset_expiry_increases_its_share(self):
        mode = self.mode(42, 120, resets=(self.now + 10 * DAY, self.resets[1]))
        self.assertAlmostEqual(mode['reset_share'], 70 + 100 * 7 * DAY / (at('2026-12-01T00:00') - self.now))

    def test_surplus_enables_fast_sol_xhigh_terra_max_never_astra(self):
        mode = self.mode(42, 71, resets=())
        self.assertEqual(mode['id'], 'surplus')
        self.assertEqual(mode['title'], 'Surplus: run Fast; Sol xhigh, Terra max, never Astra')
        self.assertNotIn('Sol max', mode['title'])
        self.assertEqual(mode['boost'], {'luna': 'sol'})
        self.assertEqual((mode['fast'], mode['effort']), (True, 'max'))
        self.assertNotIn('astra', mode['boost'].values())
        self.assertIn('71%', mode['reason'])
        self.assertIn('Mon 16', mode['reason'])

    def test_saving_when_forecast_exhausts_the_week_without_resets(self):
        for projected in (100, 140):
            mode = self.mode(42, projected, resets=())
            self.assertEqual(mode['id'], 'saving')
            self.assertFalse(mode['fast'])
            self.assertIn('credits kept for December', mode['reason'])

    def test_november_reserves_credits_even_after_all_resets_are_spent(self):
        week = dict(window_minutes=10080, used_percent=90, resets_at=self.reset)
        routing = quota_routing({'codex': [week]}, [], self.now, {'balance': 48000},
                                credits_expire_at=at('2027-01-01T00:00'), policy=example_policy())
        self.assertEqual(routing['codex']['credits']['week_share_left'], 0)
        self.assertEqual(routing['codex_mode']['id'], 'saving')
        self.assertIn('credits kept for December', routing['codex_mode']['reason'])

    def test_future_reset_share_cannot_spend_november_credits_today(self):
        now, reset = at('2026-11-10T12:00'), at('2026-11-12T12:00')
        mode = self.mode(100, 132, now=now, reset=reset, resets=self.resets[:1])
        self.assertEqual(mode['id'], 'saving')
        self.assertFalse(mode['fast'])
        self.assertEqual(mode['available'], 0)
        self.assertIn('credits kept for December', mode['reason'])

    def test_exhausted_night_cannot_route_work_to_codex_even_with_zero_demand(self):
        now = at('2026-11-15T23:00')
        reset = at('2026-11-16T01:00')
        window = dict(window_minutes=10080, used_percent=100, resets_at=reset)
        routing = quota_routing({'codex': [window], 'claude': [window]}, [], now,
                                {'balance': 48000}, credits_expire_at=at('2027-01-01T00:00'),
                                codex_resets=self.resets, work_hours=(9, 21), policy=example_policy())
        self.assertEqual(routing['codex_mode']['demand'], 0)
        self.assertFalse(routing['codex_fast'])
        self.assertEqual((routing['code_to'], routing['design_to']), ('claude', 'claude'))

    def test_december_can_spend_credits_with_a_reset_still_available(self):
        now = at('2026-12-01T12:00')
        week = dict(window_minutes=10080, used_percent=10, resets_at=now + 6 * DAY)
        routing = quota_routing({'codex': [week]}, [], now, {'balance': 48000},
                                credits_expire_at=at('2027-01-01T00:00'), codex_resets=self.resets, policy=example_policy())
        self.assertGreater(routing['codex']['credits']['week_share_left'], 0)

    def test_missing_week_has_no_invented_mode_or_routing_boost(self):
        mode = codex_mode(None, self.resets, self.now, policy=example_policy())
        self.assertEqual((mode['boost'], mode['fast']), ({}, False))
        self.assertIn('unavailable', mode['reason'])

    def test_snapshot_and_routing_file_share_mode_and_real_values(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config.load(directory)
            config.routing_policy = example_policy()
            config.modules.update(routing=True, codex_credits=True)
            config.codex_resets = self.resets
            source = NowSource(directory, config=config, clock=lambda: self.now)
            week = dict(window_minutes=10080, used_percent=10, resets_at=self.reset)
            source.codex_limits = lambda: [week]
            credits = dict(balance=48000.34, at=self.now)
            source.usage_cache['codex_meter'] = dict(key=(True, False), at=self.now,
                                                   status='available', value=(credits, [], []))
            state = source.state()
            calculator, routing = state['calculator'], state['routing']
            self.assertEqual(calculator['used_percent'], 10)
            self.assertEqual(calculator['remaining_percent'], 90)
            self.assertEqual(calculator['next_reset'], self.reset)
            self.assertEqual(calculator['credits_balance'], 48000.34)
            self.assertEqual(datetime.fromtimestamp(calculator['credits_expire_at']).date().isoformat(), '2027-01-01')
            self.assertEqual(calculator['extra_resets'], list(self.resets))
            self.assertEqual(calculator['mode'], routing['codex_mode'])
            self.assertEqual((routing['codex_boost'], routing['codex_fast'], routing['codex_effort']),
                             (calculator['mode']['boost'], calculator['mode']['fast'], calculator['mode']['effort']))
            self.assertEqual(json.loads(config.routing_file.read_text()), routing)

    def test_calculator_mode_also_works_when_optional_modules_are_off(self):
        with tempfile.TemporaryDirectory() as directory:
            source = NowSource(directory, clock=lambda: self.now)
            source.codex_limits = lambda: [dict(window_minutes=10080, used_percent=10, resets_at=self.reset)]
            state = source.state()
            self.assertNotIn('routing', state)
            self.assertEqual(state['calculator']['mode']['id'], 'surplus')

    def test_reset_plan_uses_surplus_pace_and_restarts_the_window(self):
        mode = self.mode(10, 10 + 15 * 6, resets=self.resets)
        plan = mode['reset_plan']
        first = self.now + 90 / (15 * FAST_PERCENT_FACTOR) * DAY
        self.assertEqual(len(plan), 2)
        self.assertAlmostEqual(plan[0], first, delta=1)
        self.assertAlmostEqual(plan[1], first + 100 / (15 * FAST_PERCENT_FACTOR) * DAY, delta=1)
        self.assertEqual(mode['reset_deadlines'], [at('2026-12-01T00:00')] * 2)

    def test_live_usage_plans_each_press_by_day_four(self):
        for hours in (None, (9, 21)):
            with self.subTest(work_hours=hours):
                forecast = dict(used_percent=14, resets_at=self.reset, pace_per_day=14,
                                projected_at_reset=14 + 14 * 6)
                mode = codex_mode(forecast, self.resets, self.now, work_hours=hours, policy=example_policy())
                self.assertEqual(mode['id'], 'surplus')
                self.assertEqual(FAST_PERCENT_FACTOR, 2.5)
                start = self.reset - 7 * DAY
                for press in mode['reset_plan']:
                    self.assertIsNotNone(press)
                    self.assertGreater(press, start)
                    self.assertLessEqual(press - start, 4 * DAY)
                    start = press
                first = datetime.fromtimestamp(mode['reset_plan'][0])
                self.assertIn(f'To press a reset on {first:%a} {first.day}, burn ~35%/day', mode['reason'])

    def test_reset_mode_plans_its_first_press_now(self):
        mode = self.mode(99, 110)
        self.assertEqual(mode['id'], 'reset')
        self.assertEqual(mode['reset_plan'][0], self.now)

    def test_slow_surplus_falls_back_to_november_30_without_duplicate_presses(self):
        mode = self.mode(10, 11)
        self.assertEqual(mode['reset_plan'], [at('2026-12-01T00:00') - 1, None])
        self.assertIn('Cannot reach 100% with three days left', mode['reason'])
        self.assertIn('last chance Mon 30', mode['reason'])

    def test_slow_surplus_last_chance_respects_work_hours_and_earlier_expiry(self):
        for deadline in (at('2026-12-01T00:00'), at('2026-11-15T08:00')):
            with self.subTest(deadline=deadline):
                forecast = dict(used_percent=14, resets_at=self.reset, pace_per_day=1,
                                projected_at_reset=20)
                mode = codex_mode(forecast, (deadline,), self.now, work_hours=(9, 21), policy=example_policy())
                press = mode['reset_plan'][0]
                self.assertLess(press, deadline)
                local = datetime.fromtimestamp(press)
                self.assertEqual((local.hour, local.minute, local.second), (20, 59, 59))
                self.assertLessEqual(local.date().isoformat(), '2026-11-30')
                self.assertIn('Cannot reach', mode['reason'])

    def test_surplus_that_only_fills_late_warns_and_uses_last_chance_rule(self):
        mode = self.mode(14, 14 + 8 * 6)
        self.assertEqual(mode['id'], 'surplus')
        self.assertIn('Cannot reach 100% with three days left even at ~20%/day', mode['reason'])
        self.assertIn('last chance', mode['reason'])
        for press in mode['reset_plan']:
            self.assertIsNotNone(press)
            self.assertLess(press, at('2026-12-01T00:00'))

    def test_last_chance_is_before_a_natural_reset_after_work_hours(self):
        reset = at('2026-11-30T22:00')
        now = reset - 6 * DAY
        forecast = dict(used_percent=10, resets_at=reset, pace_per_day=0, projected_at_reset=10)
        mode = codex_mode(forecast, self.resets, now, work_hours=(9, 21), policy=example_policy())
        self.assertEqual(mode['reset_plan'], [at('2026-11-30T21:00') - 1, None])
        self.assertIn('last chance Mon 30', mode['reason'])

    def test_no_work_time_before_expiry_keeps_reset_unplanned_and_explains_why(self):
        now = at('2026-11-30T22:00')
        forecast = dict(used_percent=10, resets_at=now + DAY, pace_per_day=0, projected_at_reset=10)
        mode = codex_mode(forecast, self.resets, now, work_hours=(9, 21), policy=example_policy())
        self.assertEqual(mode['reset_plan'], [None, None])
        self.assertIn('Cannot reach 100% with three days left', mode['reason'])
        self.assertIn('no work time remains', mode['reason'])

    def test_timeline_windows_end_at_natural_resets_presses_and_deadline(self):
        end = at('2026-12-01T00:00')
        press = at('2026-11-22T16:00')
        timeline = calculator_timeline(self.reset, [end, end], [press, None], self.now)
        self.assertEqual(timeline['start'], self.reset - 7 * DAY)
        self.assertEqual((timeline['end'], timeline['expires_at']), (end, end))
        self.assertEqual([(w['start'], w['end'], w['ends'], w['current']) for w in timeline['windows']], [
            (self.reset - 7 * DAY, self.reset, 'natural', True),
            (self.reset, press, 'manual', False),
            (press, press + 7 * DAY, 'natural', False),
            (press + 7 * DAY, end, 'end', False)])
        self.assertEqual(timeline['resets'], [dict(at=press, status='planned', expires_at=end),
                                              dict(at=end, status='unplanned', expires_at=end)])
        self.assertIsNone(calculator_timeline(None, [end], [press], self.now))

    def test_manual_press_detection_ignores_provider_deadline_drift(self):
        start = self.reset - 7 * DAY
        drift = [dict(at=start - 49 * 60, tool='codex', window='week', used_percent=0, resets_at=self.reset - 50 * 60)]
        natural = [dict(at=start - DAY, tool='codex', window='week', used_percent=80, resets_at=start - 50 * 60)]
        pressed = [dict(at=start - DAY, tool='codex', window='week', used_percent=99, resets_at=start + 2 * DAY)]
        self.assertFalse(manual_reset_started(drift + natural, self.reset))
        self.assertTrue(manual_reset_started(pressed, self.reset))
        timeline = calculator_timeline(self.reset, [], [], self.now, used_reset=True)
        self.assertEqual(timeline['resets'], [dict(at=start, status='used')])

    def test_snapshot_publishes_timeline_and_ring_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config.load(directory)
            config.routing_policy = example_policy()
            config.modules.update(routing=True, codex_credits=True)
            config.codex_resets = self.resets
            source = NowSource(directory, config=config, clock=lambda: self.now)
            source.codex_limits = lambda: [dict(window_minutes=10080, used_percent=10, resets_at=self.reset)]
            source.usage_cache['codex_meter'] = dict(key=(True, False), at=self.now, status='available',
                                                   value=(dict(balance=48000.34, at=self.now), [], []))
            calculator = source.state()['calculator']
            self.assertEqual(calculator['resets_total'], 2)
            self.assertEqual(calculator['credits_start_balance'], 48000.34)
            timeline = calculator['timeline']
            self.assertEqual(timeline['start'], self.reset - 7 * DAY)
            self.assertEqual(timeline['expires_at'], at('2026-12-01T00:00'))
            self.assertEqual([(r['at'], r['status']) for r in timeline['resets']],
                             [(deadline, 'unplanned') if press is None else (press, 'planned') for press, deadline
                              in zip(calculator['mode']['reset_plan'], calculator['mode']['reset_deadlines'])])
            self.assertEqual(timeline['windows'][0]['current'], True)

    def test_manual_reset_observation_uses_the_new_provider_window(self):
        # Local rollout observations: 2026-10-29 15:46 -> 2026-11-05 15:46,
        # before the previous window's 2026-11-04 11:36 end.
        now = at('2026-10-29T15:48:57')
        reset = at('2026-11-05T15:46:26')
        old_reset = at('2026-11-04T11:36:09')
        history = [dict(at=now - 148, tool='codex', window='week', used_percent=100, resets_at=old_reset)]
        forecast = week_forecast('codex', dict(used_percent=0, resets_at=reset), history, now)
        self.assertEqual(forecast['resets_at'], reset)
        self.assertEqual(forecast['start'], at('2026-10-29T15:46:26'))
        self.assertEqual(forecast['points'], [[now, 0]])
