"""Board durations come from saved turns, including after incremental appends."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from murmuratio_app.config import Config
from murmuratio_app.now import NowSource


class WorkingTimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.now = time.time()
        self.source = NowSource(self.home, clock=lambda: self.now, config=Config(), alive=lambda pid: pid == 7)
        self.claude = self.home / '.claude/projects/project/chat.jsonl'
        self.codex = self.home / '.codex/sessions/2026/11/07/rollout-test-thread.jsonl'

    def append(self, path, *records):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            for record in records:
                stream.write(json.dumps(record) + '\n')
        os.utime(path, (self.now, self.now))

    def message(self, role, at, content='Work', **extra):
        return dict(type=role, timestamp=at, message=dict(content=content, **extra))

    def event(self, kind, at, **extra):
        return dict(type='event_msg', timestamp=at, payload=dict(type=kind, **extra))

    def rollout(self, *records):
        self.append(self.codex, dict(type='session_meta', timestamp=self.now - 40000,
                                    payload=dict(id='thread', source='exec')), *records)

    def wrapper(self, **extra):
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(status='running', started=self.now - 40000, pid=7, **extra)))

    def agent(self):
        state = self.source.state()
        agents = state['agents'] + state['completed']
        self.assertEqual(len(agents), 1)
        return agents[0]

    def test_claude_excludes_idle_gap_and_adds_current_turn(self):
        self.append(self.claude, self.message('user', self.now - 40000),
                    self.message('assistant', self.now - 39940, stop_reason='end_turn'))
        self.assertEqual(self.source.state()['agents'], [], 'S5: an old completed turn has expired')
        self.append(self.claude, self.message('user', self.now - 20),
                    self.message('assistant', self.now - 10, stop_reason='tool_use'))
        self.assertEqual(self.agent()['seconds'], 80)
        self.now += 10
        self.assertEqual(self.agent()['seconds'], 90)
        self.append(self.claude, self.message('assistant', self.now - 5, stop_reason='end_turn'))
        self.assertEqual(self.agent()['seconds'], 85)
        self.now += 300
        self.assertEqual(self.agent()['seconds'], 85)
        self.now += 1800
        self.assertEqual(self.source.state()['agents'], [], 'S5: completion expires without losing turn totals')
        self.append(self.claude, self.message('user', self.now - 5),
                    self.message('assistant', self.now, stop_reason='end_turn'))
        self.assertEqual(self.agent()['seconds'], 90)

    def test_claude_missing_completion_closes_at_previous_last_event(self):
        self.append(self.claude, self.message('user', self.now - 40000),
                    self.message('assistant', self.now - 39940, stop_reason='tool_use'),
                    self.message('user', self.now - 20),
                    self.message('assistant', self.now - 5, stop_reason='end_turn'))
        self.assertEqual(self.agent()['seconds'], 75)

    def test_claude_tool_results_and_metadata_do_not_start_turns(self):
        self.append(self.claude, self.message('user', self.now - 100),
                    self.message('assistant', self.now - 90, stop_reason='tool_use'),
                    self.message('user', self.now - 80, [{'type': 'tool_result', 'content': 'ok'}]),
                    {**self.message('user', self.now - 70), 'isMeta': True},
                    self.message('assistant', self.now - 60, stop_reason='end_turn'))
        self.assertEqual(self.agent()['seconds'], 40)

    def test_quiet_claude_turn_remains_working(self):
        self.append(self.claude, self.message('user', self.now - 40000),
                    self.message('assistant', self.now - 39940, stop_reason='end_turn'),
                    self.message('user', self.now - 2000),
                    self.message('assistant', self.now - 1900, stop_reason='tool_use'))
        meta = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b/local_chat.json'
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps(dict(cliSessionId='chat')))
        os.utime(self.claude, (self.now - 1900, self.now - 1900))
        self.assertEqual((self.agent()['status'], self.agent()['seconds']), ('working', 2060))
        self.assertTrue(self.agent()['quiet'])
        self.now += 300
        self.assertEqual(self.agent()['seconds'], 2360)

    def test_codex_excludes_idle_gap_and_freezes_completed_turns(self):
        self.rollout(self.event('task_started', self.now - 39999),
                     self.event('task_complete', self.now - 39939))
        self.assertEqual(self.agent()['seconds'], 60)
        self.append(self.codex, self.event('task_started', self.now - 20),
                    self.event('token_count', self.now - 10))
        self.assertEqual(self.agent()['seconds'], 80)
        self.append(self.codex, self.event('task_complete', self.now - 5))
        self.assertEqual(self.agent()['seconds'], 75)
        self.now += 3600
        self.assertEqual(self.agent()['seconds'], 75)

    def test_codex_stalled_turn_counts_all_last_events_including_large_outputs(self):
        writers = patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value={self.codex})
        writers.start()
        self.addCleanup(writers.stop)
        self.rollout(self.event('task_started', self.now - 39999),
                     self.event('task_complete', self.now - 39939),
                     self.event('task_started', self.now - 2000))
        self.append(self.codex, dict(type='response_item', timestamp=self.now - 1800,
                    payload=dict(type='function_call_output', output='x' * 600000)))
        os.utime(self.codex, (self.now - 1800, self.now - 1800))
        self.assertEqual((self.agent()['status'], self.agent()['seconds']), ('hung', 260))
        self.now += 300
        self.assertEqual(self.agent()['seconds'], 260)

    def test_codex_resume_settings_do_not_extend_unfinished_previous_turn(self):
        self.rollout(self.event('task_started', self.now - 39999),
                     self.event('token_count', self.now - 39939))
        with patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value={self.codex}):
            self.assertEqual(self.agent()['seconds'], 39999)
        self.append(self.codex, self.event('thread_settings_applied', self.now - 21),
                    dict(type='session_meta', timestamp=self.now - 21, payload=dict(id='thread', source='exec')),
                    dict(type='turn_context', timestamp=self.now - 21, payload=dict(model='gpt-6.1-sol')),
                    self.event('task_started', self.now - 20),
                    self.event('task_complete', self.now - 5))
        self.assertEqual(self.agent()['seconds'], 75)

    def test_codex_sum_is_not_limited_by_recent_turn_link_history(self):
        self.rollout(*(record for turn in range(40) for record in (
            self.event('task_started', self.now - 40000 + turn * 900),
            self.event('task_complete', self.now - 39940 + turn * 900))))
        self.assertEqual(self.agent()['seconds'], 2400)

    def test_dead_wrapper_stops_native_working_turn_at_last_event(self):
        self.rollout(self.event('task_started', self.now - 39999),
                     self.event('task_complete', self.now - 39939),
                     self.event('task_started', self.now - 100),
                     self.event('token_count', self.now - 40))
        self.wrapper(thread='thread')
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        run = json.loads(path.read_text())
        run['pid'] = 8
        path.write_text(json.dumps(run))
        self.assertEqual((self.agent()['status'], self.agent()['seconds']), ('crashed', 120))
        self.now += 300
        self.assertEqual(self.agent()['seconds'], 120)

    def test_wrapper_timestamped_turns_exclude_idle_gap(self):
        events = self.home / 'events.jsonl'
        self.append(events, dict(type='turn.started', timestamp=self.now - 39999),
                    dict(type='turn.completed', timestamp=self.now - 39939),
                    dict(type='turn.started', timestamp=self.now - 20),
                    dict(type='item.completed', timestamp=self.now - 10))
        self.wrapper(events=str(events))
        self.assertEqual(self.agent()['seconds'], 80)
        self.append(events, dict(type='turn.completed', timestamp=self.now - 5))
        self.assertEqual(self.agent()['seconds'], 75)
        self.now += 300
        self.assertEqual(self.agent()['seconds'], 75)

    def test_sources_without_turn_boundaries_use_last_event_never_now(self):
        self.append(self.claude, self.message('assistant', self.now - 100, stop_reason='tool_use'),
                    self.message('assistant', self.now - 40, stop_reason='tool_use'))
        self.assertEqual(self.agent()['seconds'], 60)
        self.now += 30
        self.assertEqual(self.agent()['seconds'], 60)
        self.claude.unlink()
        self.rollout(self.event('token_count', self.now - 400))
        self.assertEqual(self.agent()['seconds'], 39600)
        self.now += 30
        self.assertEqual(self.agent()['seconds'], 39600)

    def test_wrapper_without_timestamps_uses_event_file_time(self):
        events = self.home / 'events.jsonl'
        self.append(events, dict(type='turn.started'), dict(type='item.completed'))
        os.utime(events, (self.now - 39900, self.now - 39900))
        self.wrapper(events=str(events))
        self.assertEqual(self.agent()['seconds'], 100)
        self.now += 300
        self.assertEqual(self.agent()['seconds'], 100)


if __name__ == '__main__':
    unittest.main()
