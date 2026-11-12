"""Synthetic format drift and damaged records at the real API boundary."""
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from murmuratio_app.adapters.codex_board import BoardSummary
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

    def test_oversized_reordered_lifecycle_is_not_reported_as_running(self):
        path = self.rollout('oversized', records=[self.event('task_started'),
            self.event('token_count', info={'last_token_usage': {'input_tokens': 1234}})])
        self.assertEqual(self.cards()['codex:oversized']['status'], 'working')
        record = {'payload': {'type': 'task_complete', 'last_message': 'x' * (9 * 1024 * 1024)},
                  'timestamp': self.now - 5, 'type': 'event_msg'}
        # Exercise the hard bound without allocating a 65 MB test record.
        with patch('murmuratio_app.adapters.codex_board.MAX_BUFFERED_BYTES', 1024 * 1024):
            self.write(path, [record], mode='a')
            state = self.api()
        card = next(a for a in state['agents'] + state['completed'] if a['id'] == 'codex:oversized')
        self.assertEqual(card['status'], 'unknown')
        self.assertIsNone(card['context_used'])
        self.assertEqual(state.get('sources', {}).get('codex_sessions', {'status': 'available'})['status'], 'available')
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
        self.assertEqual(state.get('sources', {}).get('codex_sessions', {'status': 'available'})['status'], 'available')

    def test_multimodal_message_from_real_record_keeps_the_turn_and_other_cards(self):
        # Sanitized 2026-11-12 message: the three image strings caused the real
        # 10.9 MB record to exceed the old 8 MB limit. Restore only their sizes.
        fixture = Path(__file__).parent / 'fixtures/adapters/codex-multimodal-record.json'
        record = json.loads(fixture.read_text())
        record['timestamp'] = self.now - 2
        quoted_example = 'Text containing {"image_url":"data:image/png;base64,KEEP"}'
        record['payload']['content'][1]['text'] = quoted_example
        sizes = iter((3835998, 3740818, 3372470))
        for part in record['payload']['content']:
            if part['type'] == 'input_image':
                part['image_url'] = 'data:image/png;base64,' + 'A' * (next(sizes) - len('data:image/png;base64,'))
        summary = BoardSummary()
        summary._consume(json.dumps(record).encode())
        self.assertIn(quoted_example, summary.data['task_raw'])
        self.rollout('healthy')
        path = self.rollout('images')
        self.write(path, [record], mode='a')
        state = self.api()
        cards = {a['id']: a for a in state['agents'] + state['completed']}
        self.assertEqual(cards['codex:images']['status'], 'working')
        self.assertEqual(cards['codex:images']['task'], 'Review these images')
        self.assertEqual(cards['codex:healthy']['status'], 'working')
        self.assertEqual(state.get('sources', {}).get('codex_sessions', {'status': 'available'})['status'], 'available')

    def test_large_compaction_invalidates_context_without_changing_turn_state(self):
        path = self.rollout('compaction', records=[self.event('task_started'),
            self.event('token_count', info={'last_token_usage': {'input_tokens': 1234}})])
        record = {'type': 'compacted', 'timestamp': self.now - 2,
                  'payload': {'replacement_history': [{'content': 'x' * (31 * 1024 * 1024)}]}}
        self.write(path, [record], mode='a')
        state = self.api()
        card = next(a for a in state['agents'] if a['id'] == 'codex:compaction')
        self.assertEqual(card['status'], 'working')
        self.assertIsNone(card['context_used'])
        self.assertEqual(state.get('sources', {}).get('codex_sessions', {'status': 'available'})['status'], 'available')

    def test_unrecognized_file_does_not_affect_a_healthy_sibling(self):
        self.rollout('healthy')
        self.rollout('unrecognized')
        original = BoardSummary.refresh

        def read(summary, path):
            if path.name == 'rollout-unrecognized.jsonl':
                raise ValueError('synthetic unrecognized file')
            return original(summary, path)

        with patch.object(BoardSummary, 'refresh', read):
            state = self.api()
        self.assertIn('codex:healthy', {a['id'] for a in state['agents']})
        self.assertEqual(state.get('sources', {}).get('codex_sessions', {'status': 'available'})['status'], 'available')

    def test_invalid_session_index_record_does_not_hide_other_sessions(self):
        self.rollout('healthy')
        index = self.codex_home / 'session_index.jsonl'
        index.write_text('[' * 2000 + ']' * 2000 + '\n'
                         + json.dumps({'id': 'healthy', 'thread_name': 'Healthy worker'}) + '\n')
        state = self.api()
        cards = {a['id']: a for a in state['agents'] + state['completed']}
        self.assertEqual(cards['codex:healthy']['task'], 'Healthy worker')
        self.assertEqual(state.get('sources', {}).get('codex_sessions', {'status': 'available'})['status'], 'available')

    def test_damaged_archived_wrapper_lookup_does_not_poison_session_health(self):
        self.rollout('healthy')
        self.config.modules['codex_wrapper'] = True
        archive = self.codex_home / 'archived_sessions/rollout-archive.jsonl'
        archive.parent.mkdir(parents=True)
        archive.write_text(json.dumps(self.event('token_count', info={'last_token_usage': {'input_tokens': 1234}}))
                           + '\n{invalid json}\n')
        database = sqlite3.connect(self.codex_home / 'state_5.sqlite')
        try:
            database.execute('CREATE TABLE threads (id TEXT, cwd TEXT, rollout_path TEXT)')
            database.execute('INSERT INTO threads VALUES (?, ?, ?)', ('archived-thread', '/synthetic/project', str(archive)))
            database.commit()
        finally:
            database.close()
        wrapper = self.home / '.local/share/murmuratio/codex-runs/wrapped.json'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(json.dumps({'status': 'running', 'thread': 'archived-thread',
                                       'parent_session': 'fixture-launcher', 'started': self.now - 10}))
        state = self.api()
        cards = {a['id']: a for a in state['agents'] + state['completed']}
        self.assertIn('codex:healthy', cards)
        self.assertIn('codex:wrapped', cards)
        self.assertIsNone(cards['codex:wrapped']['context_used'])
        self.assertEqual(state.get('sources', {}).get('codex_sessions', {'status': 'available'})['status'], 'available')
