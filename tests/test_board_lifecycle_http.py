"""Claude lifecycle and quiet work through the loopback API."""
import os
import time
import unittest
from datetime import datetime, timedelta

from tests.test_board_http import BoardHTTPHarness


class BoardLifecycleHTTPTests(BoardHTTPHarness):
    def test_T2_api_publishes_server_local_day_boundaries(self):
        today = datetime.fromtimestamp(self.now).date()
        start = datetime.combine(today, datetime.min.time()).timestamp()
        end = datetime.combine(today + timedelta(days=1), datetime.min.time()).timestamp()
        self.assertEqual(self.api()['local_day'], {'start': start, 'end': end})

    @unittest.skipUnless(hasattr(time, 'tzset'), 'requires local timezone switching')
    def test_T2_server_day_accounts_for_daylight_saving(self):
        previous = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'America/New_York'
            time.tzset()
            self.now = datetime(2026, 3, 8, 12).timestamp()
            day = self.api()['local_day']
            self.assertEqual(day['end'] - day['start'], 23 * 3600)
        finally:
            if previous is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = previous
            time.tzset()

    def test_S5_worker_finished_before_midnight_stays_for_half_an_hour(self):
        midnight = datetime.fromtimestamp(self.now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        self.now = midnight + 60
        self.desktop('worker', spawnedFrom={'sessionId': 'local_parent'})
        self.claude('worker', [self.message('user', at=midnight - 600, content='Work'),
            self.message('assistant', at=midnight - 300, stop_reason='end_turn', content='Done')], modified=midnight - 300)
        card = self.cards()['claude:worker']
        self.assertEqual((card['status'], card['ended']), ('done', midnight - 300))
        self.now = midnight + 1500
        self.assertNotIn('claude:worker', self.cards())

    def test_quiet_claude_work_stays_working_without_inventing_approval(self):
        self.desktop('quiet')
        path = self.claude('quiet', [self.message('user', content='Investigate'),
            self.message('assistant', stop_reason='tool_use', content=[{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'run work'}}])])
        cli = self.claude('cli', [self.message('user', content='Independent work')])
        for age in (121, 601, 86399):
            with self.subTest(age=age):
                os.utime(path, (self.now - age, self.now - age))
                os.utime(cli, (self.now - age, self.now - age))
                cards = self.cards()
                for name in ('quiet', 'cli'):
                    self.assertEqual(cards['claude:' + name]['status'], 'working')
                    self.assertTrue(cards['claude:' + name]['quiet'])
                    self.assertNotEqual(cards['claude:' + name]['current_action'], 'waiting for approval')
        self.write(path, [self.message('assistant', stop_reason='end_turn', content='Done')], mode='a')
        self.assertEqual(self.cards()['claude:quiet']['status'], 'waiting')

    def test_quiet_parent_remains_working_during_claude_sidechain(self):
        self.desktop('parent')
        self.claude('parent', [self.message('user', content='Coordinate'),
            self.message('assistant', stop_reason='tool_use', content=[
                {'type': 'tool_use', 'name': 'Agent', 'id': 'synthetic-tool', 'input': {}}])],
            modified=self.now - 121)
        self.write(self.home / '.claude/projects/project/parent/subagents/agent-synthetic.jsonl',
                   [dict(self.message('assistant', stop_reason='tool_use', content='Working'), isSidechain=True)])
        cards = self.cards()
        child_id = 'claude:parent:agent-synthetic'
        self.assertEqual(set(cards), {'claude:parent', child_id})
        self.assertEqual(cards['claude:parent']['status'], 'working')
        self.assertTrue(cards['claude:parent']['quiet'])
        self.assertEqual((cards[child_id]['parent_id'], cards[child_id]['link'], cards[child_id]['status']),
                         ('claude:parent', 'exact', 'working'))
        self.assertFalse(cards[child_id]['completed'])
        self.assertFalse(cards[child_id].get('quiet', False))

    def test_claude_lifecycle_uses_one_card_across_turns(self):
        self.desktop('lifecycle', title='Synthetic task', model='claude-sonnet-5-5')
        path = self.claude('lifecycle', [self.message('user', at=self.now - 90, content='Start')])
        card = self.cards()['claude:lifecycle']
        self.assertEqual((card['status'], card['task'], card['project'], card['model']),
                         ('working', 'Synthetic task', 'project', 'claude-sonnet-5-5'))
        self.write(path, [self.message('assistant', at=self.now - 60, stop_reason='tool_use', content='Working')], mode='a')
        self.assertEqual(self.cards()['claude:lifecycle']['status'], 'working')
        self.write(path, [self.message('assistant', at=self.now - 40, stop_reason='end_turn', content='Done')], mode='a')
        card = self.cards()['claude:lifecycle']
        self.assertEqual((card['status'], card['seconds']), ('waiting', 50))
        self.write(path, [self.message('user', at=self.now - 10, content='Continue')], mode='a')
        cards = self.cards()
        self.assertEqual(list(cards), ['claude:lifecycle'])
        self.assertEqual((cards['claude:lifecycle']['status'], cards['claude:lifecycle']['seconds']), ('working', 60))

    def test_codex_working_duration_excludes_idle_gaps_and_freezes(self):
        path = self.rollout('timed', records=[
            self.event('task_started', self.now - 4000), self.event('task_complete', self.now - 3940),
            self.event('task_started', self.now - 1200), self.event('task_complete', self.now - 1100),
            self.event('task_started', self.now - 30)])
        card = self.cards()['codex:timed']
        self.assertEqual((card['status'], card['seconds']), ('working', 190))
        self.write(path, [self.event('task_complete', self.now - 10)], mode='a')
        self.assertEqual((self.cards()['codex:timed']['status'], self.cards()['codex:timed']['seconds']), ('done', 180))
        self.now += 300
        self.assertEqual(self.cards()['codex:timed']['seconds'], 180)
        self.write(path, [self.event('task_started', self.now - 20), self.event('turn_aborted', self.now - 5)], mode='a')
        self.assertEqual((self.cards()['codex:timed']['status'], self.cards()['codex:timed']['seconds']), ('blocked', 195))
