"""Release regressions: damaged local input must not erase healthy observations."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from murmuratio_app.adapters.base import Adapter
from murmuratio_app.adapters.codex_board import CodexBoardAdapter
from murmuratio_app.adapters.claude import ClaudeDesktopAdapter
from murmuratio_app.adapters.shared import JsonlSummary
from murmuratio_app.adapters.parsing import consume_claude, recorded_thread
from murmuratio_app.now import NowSource


class ReleaseRobustnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.source = NowSource(self.home, clock=lambda: 2_000_000)

    def test_adapter_keeps_observations_yielded_before_file_failure(self):
        class Broken(Adapter):
            name = 'test'
            def collect(self):
                yield self.observation({'id': 'healthy'})
                raise PermissionError()
        result = Broken().read()
        self.assertEqual(result.values(), [{'id': 'healthy'}])
        self.assertEqual(result.status, 'unavailable')

    def test_bad_desktop_file_does_not_skip_later_metadata(self):
        root = self.home / 'desktop'
        root.mkdir()
        bad, good = root / 'bad.json', root / 'good.json'
        bad.write_text('deep')
        good.write_text('{"cliSessionId":"healthy","title":"Example orchestrator"}')
        adapter = ClaudeDesktopAdapter(root)
        loads = json.loads
        def parse(raw):
            if raw == 'deep':
                raise RecursionError()
            return loads(raw)
        with patch.object(adapter, 'files', return_value=[bad, good]), patch('json.loads', side_effect=parse):
            result = adapter.read()
        self.assertEqual([r['cli_session_id'] for r in result.values()], ['healthy'])

    def test_codex_bad_tool_names_and_files_do_not_remove_healthy_cards(self):
        root = self.home / 'sessions'
        folder = root / '2026/11/12'
        folder.mkdir(parents=True)
        def write(name, records):
            (folder / f'rollout-{name}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
        meta = lambda name: dict(type='session_meta', payload=dict(id=name, source='exec', cwd='/example'))
        write('healthy', [meta('healthy')])
        write('bad', [meta('bad'), dict(type='response_item', payload=dict(type='function_call', name=['bad']))])
        adapter = CodexBoardAdapter(root, lambda cwd: 'Example')
        result = adapter.read(2_000_000)
        self.assertEqual({r['thread'] for r in result.values()}, {'healthy', 'bad'})
        with patch.object(adapter.cache[folder / 'rollout-bad.jsonl'], 'refresh', side_effect=KeyError('broken file')):
            self.assertEqual({r['thread'] for r in adapter.read(2_000_000).values()}, {'healthy'})

    def test_deep_claude_tool_result_does_not_abort_record_or_next_file(self):
        path = self.home / 'claude.jsonl'
        content = {'thread_id': 'worker'}
        for _ in range(120):
            content = {'content': [content]}
        records = [dict(type='user', timestamp=1, message=dict(content='Build an example')),
                   dict(type='assistant', timestamp=2, message=dict(content=[dict(type='tool_use', name='Bash', id='t', input=dict(command='codex exec task'))])),
                   dict(type='user', timestamp=3, message=dict(content=[dict(type='tool_result', tool_use_id='t', content=content)])),
                   dict(type='assistant', timestamp=4, message=dict(content='Ready', stop_reason='end_turn'))]
        path.write_text(''.join(json.dumps(r) + '\n' for r in records))
        # A regular JSON document can still exceed the recursive thread lookup's stack.
        with patch('murmuratio_app.adapters.parsing.recorded_thread', side_effect=RecursionError()):
            summary = JsonlSummary()
            data, _ = summary.refresh(path, consume_claude)
        self.assertEqual(data['last_turn'], 'end_turn')
        self.assertGreater(summary.malformed, 0)

    def test_thread_lookup_has_a_depth_bound(self):
        self.assertEqual(recorded_thread({'content': [{'thread_id': 'worker'}]}), 'worker')
        content = {'thread_id': 'worker'}
        for _ in range(2000):
            content = {'content': [content]}
        self.assertIsNone(recorded_thread(content))

    def test_claude_huge_record_is_skipped_and_unused_action_is_absent(self):
        path = self.home / 'claude.jsonl'
        path.write_text(json.dumps(dict(type='assistant', message=dict(content='x' * (10 * 1024 * 1024)))) + '\n' +
                        json.dumps(dict(type='assistant', timestamp=4, message=dict(content='Ready', stop_reason='end_turn'))) + '\n')
        summary = JsonlSummary()
        data, _ = summary.refresh(path, consume_claude)
        self.assertGreater(summary.malformed, 0)
        self.assertEqual(data['last_turn'], 'end_turn')
        self.assertNotIn('last_action', data)

    def test_history_is_not_read_with_all_modules_off(self):
        with patch('murmuratio_app.now.read_limit_history') as read:
            self.source.state()
        read.assert_not_called()

    def test_unreadable_history_does_not_abort_snapshot_or_forecast(self):
        self.source.config.modules['forecast'] = True
        with patch('murmuratio_app.now.read_limit_history', side_effect=PermissionError()):
            state = self.source.state()
        self.assertIn('agents', state)
        self.assertIn('forecast', state)
        self.assertEqual(state['sources']['quota_history']['status'], 'unavailable')

    def test_failed_refresh_marks_cached_snapshot_and_recovery_clears_flag(self):
        first = self.source.snapshot()
        self.assertEqual(first['snapshot_at'], 2_000_000)
        with patch.object(self.source, 'state', side_effect=RuntimeError()):
            failed = self.source.snapshot()
        self.assertTrue(failed['refresh_failed'])
        self.assertEqual(failed['now'], first['now'])
        self.assertFalse(self.source.snapshot()['refresh_failed'])


if __name__ == '__main__':
    unittest.main()
