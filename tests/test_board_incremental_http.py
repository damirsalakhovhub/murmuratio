"""Incremental source files read through the real board API."""
import json
import os

from tests.test_board_http import BoardHTTPHarness


class BoardIncrementalHTTPTests(BoardHTTPHarness):
    def test_codex_append_partial_replace_and_copy_truncate(self):
        path = self.rollout('incremental', records=[self.event('user_message', message='Old request'),
                                                    self.event('task_started'), self.event('task_complete')])
        self.assertEqual(self.cards()['codex:incremental']['status'], 'done')
        line = json.dumps(self.event('task_started')).encode() + b'\n'
        with path.open('ab') as stream:
            stream.write(line[:len(line) // 2])
        self.assertEqual(self.cards()['codex:incremental']['status'], 'done')
        with path.open('ab') as stream:
            stream.write(line[len(line) // 2:])
        self.assertEqual(self.cards()['codex:incremental']['status'], 'working')
        replacement = path.with_suffix('.new')
        meta = {'type': 'session_meta', 'timestamp': self.now - 60,
                'payload': {'id': 'incremental', 'parent_session': 'fixture-launcher', 'source': 'exec', 'cwd': '/synthetic/project'}}
        self.write(replacement, [meta, self.event('user_message', message='New inode'), self.event('task_complete')])
        os.replace(replacement, path)
        self.assertEqual(self.cards()['codex:incremental']['task'], 'New inode')
        old_size, old_inode = path.stat().st_size, path.stat().st_ino
        self.write(path, [meta, self.event('user_message', message='Rewritten request'),
                          self.event('task_started', padding='x' * (old_size + 100))])
        self.assertEqual(path.stat().st_ino, old_inode)
        self.assertGreater(path.stat().st_size, old_size)
        card = self.cards()['codex:incremental']
        self.assertEqual((card['task'], card['status']), ('Rewritten request', 'working'))

    def test_claude_copy_truncate_regrowing_beyond_old_offset(self):
        path = self.claude('rewritten', [self.message('user', content='Old request'),
                           self.message('assistant', stop_reason='end_turn', content='Finished')])
        self.assertEqual(self.cards()['claude:rewritten']['status'], 'done')
        old_size, old_inode = path.stat().st_size, path.stat().st_ino
        self.write(path, [self.message('user', content='Rewritten request'),
                          self.message('assistant', stop_reason='tool_use', content='x' * (old_size + 100))])
        self.assertEqual(path.stat().st_ino, old_inode)
        self.assertGreater(path.stat().st_size, old_size)
        card = self.cards()['claude:rewritten']
        self.assertEqual((card['task'], card['status']), ('Rewritten request', 'working'))
