"""Damaged source records at the real API boundary."""
from tests.test_board_http import BoardHTTPHarness
from unittest.mock import patch
import os
from pathlib import Path


class BoardBadDataHTTPTests(BoardHTTPHarness):
    def test_R3_absent_and_unreadable_session_sources_are_distinct(self):
        state = self.api()
        self.assertEqual(state['sources']['codex_sessions']['status'], 'absent')
        root = self.codex_home / 'sessions'
        root.mkdir(parents=True)
        with patch('os.scandir', side_effect=PermissionError('synthetic denied')):
            state = self.api()
        self.assertEqual(state['sources']['codex_sessions']['status'], 'unavailable')

    def test_R3_unreadable_nested_source_keeps_healthy_sessions(self):
        self.rollout('healthy')
        inaccessible = self.codex_home / 'sessions/2025'
        inaccessible.mkdir(parents=True)
        scan = os.scandir
        def denied(path):
            if Path(path) == inaccessible:
                raise PermissionError('synthetic denial')
            return scan(path)
        with patch('os.scandir', side_effect=denied):
            state = self.api()
        self.assertEqual(state['sources']['codex_sessions']['status'], 'unavailable')
        self.assertIn('codex:healthy', {a['id'] for a in state['agents']})

    def test_extreme_timestamp_and_damaged_siblings_cannot_break_healthy_cards(self):
        self.desktop('healthy')
        self.desktop('bad-time', spawnedFrom={'sessionId': 'local_healthy'})
        self.claude('healthy', [self.message('user', content='Healthy \ud800 task')])
        self.claude('bad-time', [self.message('user', content='Child'),
            self.message('assistant', at=1e300, stop_reason='end_turn', content='Done')])
        self.rollout('good-codex')
        damaged = self.home / '.claude/projects/project/damaged.jsonl'
        damaged.write_bytes(b'{invalid json}\n\xff\n')
        state = self.api()
        cards = {a['id']: a for a in state['agents'] + state['completed']}
        self.assertIn('claude:healthy', cards)
        self.assertIn('codex:good-codex', cards)
        self.assertIn('\ufffd', cards['claude:healthy']['task'])
        self.assertEqual(state['sources']['claude_sessions']['status'], 'unsupported')
