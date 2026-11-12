"""Calendar calculations follow the user's local day, including DST changes."""
from datetime import datetime, timedelta
import unittest

from murmuratio_app.config import RoutingPolicy
from murmuratio_app.metrics.quota import WorkClock, credits_expiry
from tests.timezone_helpers import local_timezone


class QuotaTimezoneTests(unittest.TestCase):
    def test_local_work_hours_and_month_boundaries(self):
        for zone in ('UTC', 'Asia/Almaty', 'America/New_York'):
            with self.subTest(zone=zone), local_timezone(zone):
                start = datetime(2026, 11, 30, 20).timestamp()
                end = datetime(2026, 12, 1, 10).timestamp()
                clock = WorkClock(9, 21)
                self.assertEqual(clock.hours(start, end), 2)
                self.assertEqual(clock.forward(start, 2, end), end)
                self.assertEqual(clock.back(end, 2, start), start)
                midnight = datetime(2026, 12, 1).timestamp()
                self.assertEqual(credits_expiry(start, 11, 30), midnight)
                policy = RoutingPolicy(credits_reserve_months=(11,), credits_start=(12, 1))
                self.assertEqual(policy.reserve_until(start), midnight)
                self.assertIsNone(policy.reserve_until(midnight))

    def test_work_hours_across_dst_use_each_local_day(self):
        with local_timezone('America/New_York'):
            for month, day, elapsed in ((3, 7, 23), (10, 31, 25)):
                with self.subTest(month=month):
                    start = datetime(2026, month, day, 20).timestamp()
                    end = (datetime(2026, month, day, 20) + timedelta(days=1)).timestamp()
                    self.assertEqual((end - start) / 3600, elapsed)
                    clock = WorkClock(9, 21)
                    self.assertEqual(clock.hours(start, end), 12)
                    self.assertEqual(clock.forward(start, 12, end), end)
                    self.assertEqual(clock.back(end, 12, start), start)
