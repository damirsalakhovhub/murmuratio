"""Synthetic format drift and damaged records at the real API boundary."""
from tests.test_board_http import BoardHTTPHarness


class BoardFormatHTTPTests(BoardHTTPHarness):
    def test_codex_lifecycle_is_independent_of_json_member_order(self):
        started = {'payload': {'type': 'task_started', 'extra': 'ignored'},
                   'extra': {'type': 'other'}, 'timestamp': self.now - 20, 'type': 'event_msg'}
        finished = {'payload': {'extra': {'type': 'other'}, 'type': 'task_complete'},
                    'timestamp': self.now - 10, 'type': 'event_msg'}
        path = self.rollout('ordered', records=[started])
        self.assertEqual(self.cards()['codex:ordered']['status'], 'working')
        self.write(path, [finished], mode='a')
        self.assertEqual(self.cards()['codex:ordered']['status'], 'done')

    def test_large_reordered_completion_preserves_turn_boundary(self):
        path = self.rollout('large-ordered')
        self.assertEqual(self.cards()['codex:large-ordered']['status'], 'working')
        record = {'payload': {'type': 'task_complete', 'last_message': 'x' * (600 * 1024)},
                  'timestamp': self.now - 5, 'type': 'event_msg'}
        self.write(path, [record], mode='a')
        self.assertEqual(self.cards()['codex:large-ordered']['status'], 'done')

    def test_R1_oversized_reordered_lifecycle_is_not_reported_as_running(self):
        path = self.rollout('oversized')
        self.assertEqual(self.cards()['codex:oversized']['status'], 'working')
        record = {'payload': {'type': 'task_complete', 'last_message': 'x' * (9 * 1024 * 1024)},
                  'timestamp': self.now - 5, 'type': 'event_msg'}
        self.write(path, [record], mode='a')
        state = self.api()
        card = next(a for a in state['agents'] + state['completed'] if a['id'] == 'codex:oversized')
        self.assertIn(card['status'], ('done', 'unknown'))
        if card['status'] == 'unknown':
            self.assertEqual(state['sources']['codex_sessions']['status'], 'unsupported')
        self.write(path, [self.event('task_started', self.now - 2)], mode='a')
        self.assertEqual(self.cards()['codex:oversized']['status'], 'working')

    def test_unknown_lifecycle_is_reported_without_discarding_healthy_cards(self):
        self.rollout('healthy')
        path = self.rollout('unknown')
        self.write(path, [self.event('task_suspended')], mode='a')
        state = self.api()
        cards = {a['id']: a for a in state['agents'] + state['completed']}
        self.assertEqual(cards['codex:healthy']['status'], 'working')
        self.assertNotEqual(cards['codex:unknown']['status'], 'working')
        self.assertEqual(state['sources']['codex_sessions']['status'], 'unsupported')
