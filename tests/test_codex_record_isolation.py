"""Rejected Codex JSONL records must not poison readable usage or context."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from murmuratio_app.adapters.codex_board import CodexBoardAdapter
from murmuratio_app.adapters.codex_sessions import CodexSource
from murmuratio_app.adapters.parsing import consume_codex
from murmuratio_app.adapters.quota_sources import CodexLimitsAdapter
from murmuratio_app.adapters.shared import JsonlSummary, MAX_RECORD_BYTES
from murmuratio_app.adapters.usage import CodexMeterAdapter
from murmuratio_app.config import Config
from murmuratio_app.now import NowSource


class CodexRecordIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / '.codex/sessions'
        self.path = self.root / '2026/11/12/rollout-test.jsonl'
        self.path.parent.mkdir(parents=True)
        self.now = 2_000_000

    def write(self, record, mode='ab'):
        with self.path.open(mode) as stream:
            stream.write(json.dumps(record).encode() + b'\n')

    def sample(self, at, balance, used):
        return {'type': 'event_msg', 'timestamp': at, 'payload': {
            'type': 'token_count', 'rate_limits': {
                'credits': {'balance': balance},
                'primary': {'used_percent': used, 'window_minutes': 10080,
                            'resets_at': self.now + 10080 * 60}}}}

    def damaged_records(self):
        self.write({'type': 'response_item', 'payload': {
            'type': 'message', 'content': 'x' * MAX_RECORD_BYTES}})
        with self.path.open('ab') as stream:
            stream.write(b'{broken}\n\xff\n[]\n' + b'[' * 2000 + b']' * 2000 + b'\n')

    def test_meter_keeps_samples_on_both_sides_of_rejected_records(self):
        self.write(self.sample(self.now - 10, 80, 25))
        self.damaged_records()
        self.write(self.sample(self.now, 70, 30))
        adapter = CodexMeterAdapter(self.root)
        for _ in range(2):  # Both the cold scan and cached refresh remain healthy.
            result = adapter.read(self.now, True, True)
            self.assertEqual(result.status, 'available')
            value, = result.values()
            self.assertEqual(value['points'], [(self.now - 10, 80), (self.now, 70)])
            self.assertEqual([row[1] for row in value['usage']], [25, 30])
        self.write(self.sample(self.now + 1, 60, 35))
        value, = adapter.read(self.now + 1, True, True).values()
        self.assertEqual(value['latest'], (self.now + 1, 60))
        self.assertEqual(len(value['usage']), 3)

    def test_meter_reports_only_a_wholly_unreadable_source(self):
        self.path.write_bytes(b'{broken}\n')
        adapter = CodexMeterAdapter(self.root)
        self.assertEqual(adapter.read(self.now, True, True).status, 'unsupported')
        sibling = self.path.with_name('rollout-healthy.jsonl')
        sibling.touch()
        self.assertEqual(adapter.read(self.now, True, True).status, 'unsupported')
        sibling.write_text(json.dumps(self.sample(self.now, 80, 25)) + '\n')
        result = adapter.read(self.now, True, True)
        self.assertEqual(result.status, 'available')
        self.assertTrue(any(value['latest'] == (self.now, 80) for value in result.values()))

    def test_context_and_wrapper_share_record_isolation(self):
        self.write({'type': 'thread.started', 'thread_id': 'chat'})
        self.damaged_records()
        self.write({'type': 'token_usage_record', 'usage': {'input_tokens': 4321}})
        codex_home = self.root.parent
        connection = sqlite3.connect(codex_home / 'state_5.sqlite')
        try:
            connection.execute('CREATE TABLE threads (id TEXT, cwd TEXT, rollout_path TEXT)')
            connection.execute('INSERT INTO threads VALUES (?, ?, ?)',
                               ('chat', '/example', str(self.path)))
            connection.commit()
        finally:
            connection.close()
        source = NowSource(self.home, config=Config.load(self.home), clock=lambda: self.now)
        result = CodexSource(codex_home).read('context', 'chat', source.summary, cache=source.cache)
        self.assertEqual(result.status, 'available')
        self.assertEqual(result.values()[0]['context_tokens'], 4321)
        runs = source.config.wrapper_runs
        runs.mkdir(parents=True)
        (runs / 'worker.json').write_text(json.dumps({
            'status': 'running', 'started': self.now - 10, 'thread': 'chat',
            'cwd': '/example', 'events': str(self.path), 'parent_session': 'launcher'}))
        result = source.wrapper_adapter.read(self.now, set())
        self.assertEqual(result.status, 'available')
        self.assertEqual(len(result.values()), 1)

    def test_partial_oversized_record_is_retried_after_its_newline(self):
        self.path.write_bytes(b'x' * (MAX_RECORD_BYTES + 1))
        summary = JsonlSummary()
        summary.refresh(self.path, consume_codex)
        self.assertEqual(summary.offset, 0)
        with self.path.open('ab') as stream:
            stream.write(b'\n')
        self.write({'type': 'token_usage_record', 'usage': {'input_tokens': 1234}})
        data, _ = summary.refresh(self.path, consume_codex)
        self.assertFalse(summary.unreadable())
        self.assertEqual(data['context_tokens'], 1234)
        self.assertEqual(summary.offset, self.path.stat().st_size)

    def test_rejected_records_invalidate_archived_context_until_measured_again(self):
        self.path = self.root.parent / 'archived_sessions/rollout-archive.jsonl'
        self.path.parent.mkdir()
        database = sqlite3.connect(self.root.parent / 'state_5.sqlite')
        with database:
            database.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
            database.execute('INSERT INTO threads VALUES (?, ?)', ('chat', str(self.path)))
        database.close()
        source = NowSource(self.home, config=Config.load(self.home), clock=lambda: self.now)
        adapter = CodexSource(self.root.parent)
        for rejected in (b'{broken}\n', b'[]\n', b'{"type":42}\n',
                         json.dumps({'type': 'compacted', 'payload': {
                             'replacement_history': 'x' * MAX_RECORD_BYTES}}).encode() + b'\n'):
            with self.subTest(rejected_bytes=len(rejected)):
                self.write({'type': 'token_usage_record', 'usage': {'input_tokens': 12345}})
                self.assertEqual(adapter.read('context', 'chat', source.summary,
                                             cache=source.cache).values()[0]['context_tokens'], 12345)
                with self.path.open('ab') as stream:
                    stream.write(rejected)
                for _ in range(2):
                    result = adapter.read('context', 'chat', source.summary, cache=source.cache)
                    self.assertEqual(result.status, 'available')
                    self.assertIsNone(result.values()[0]['context_tokens'])
                    self.assertTrue(result.values()[0]['_context_invalidated'])
                self.write({'type': 'token_usage_record', 'usage': {'input_tokens': 2345}})
                data = adapter.read('context', 'chat', source.summary, cache=source.cache).values()[0]
                self.assertEqual(data['context_tokens'], 2345)
                self.assertFalse(data.get('_context_invalidated', False))

    def test_unreadable_rollout_reports_health_and_preserves_siblings(self):
        self.write({'type': 'session_meta', 'payload': {'id': 'healthy', 'source': 'exec'}})
        sibling = self.path.with_name('rollout-denied.jsonl')
        sibling.touch()
        original = Path.open
        def denied(path, *args, **kwargs):
            if path == sibling:
                raise PermissionError('private path must not be reported')
            return original(path, *args, **kwargs)
        adapter = CodexBoardAdapter(self.root, lambda cwd: 'Example')
        with patch.object(Path, 'open', denied):
            result = adapter.read(self.now)
        self.assertEqual([value['thread'] for value in result.values()], ['healthy'])
        self.assertEqual((result.status, result.reason), ('unavailable', 'permission_denied'))
        self.assertEqual(adapter.read(self.now).status, 'available')

    def test_quota_discards_oversized_tail_fragment_and_keeps_healthy_sibling(self):
        sibling = self.path.with_name('rollout-healthy.jsonl')
        sibling.write_text(json.dumps(self.sample(self.now, 80, 25)) + '\n')
        self.write({'type': 'response_item', 'payload': {
            'type': 'message', 'content': 'x' * (11 * 1024 * 1024)}})
        adapter = CodexLimitsAdapter(self.root, clock=lambda: self.now)
        # The oversized file is examined first, including when it ends in a partial append.
        os.utime(sibling, (self.now - 1, self.now - 1))
        os.utime(self.path, (self.now, self.now))
        with patch.object(adapter, 'files', return_value=[self.path, sibling]):
            for partial in (False, True):
                if partial:
                    with self.path.open('ab') as stream:
                        stream.write(b'{"type":"event_msg","payload":{"rate_limits":')
                result = adapter.read()
                self.assertEqual(result.status, 'available')
                self.assertEqual(result.values()[0]['used_percent'], 25)
        # A newer usable record in the same file also survives the discarded suffix.
        with self.path.open('ab') as stream:
            stream.write(b'\n')
        self.write(self.sample(self.now, 75, 30))
        result = adapter.read()
        self.assertEqual(result.status, 'available')
        self.assertEqual(result.values()[0]['used_percent'], 30)


if __name__ == '__main__':
    unittest.main()
