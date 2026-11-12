"""Release regressions for provider roles and recorded titles."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from murmuratio_app.adapters.codex_board import thread_names
from murmuratio_app.adapters.usage import ClaudeUsageAdapter
from murmuratio_app.now import NowSource


class ReleaseBoardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.now = 2_000_000

    def test_named_codex_orchestrator_is_visible_without_registry_or_children(self):
        folder = self.home / '.codex/sessions/2026/11/12'
        folder.mkdir(parents=True)
        path = folder / 'rollout-example.jsonl'
        rows = [dict(type='session_meta', timestamp=self.now - 60,
                     payload=dict(id='example', source='vscode', cwd='/example')),
                dict(type='event_msg', timestamp=self.now - 10, payload=dict(type='task_complete'))]
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        os.utime(path, (self.now, self.now))
        (self.home / '.codex/session_index.jsonl').write_text('{"id":"example","thread_name":"Example orchestrator"}\n')
        state = NowSource(self.home, clock=lambda: self.now).state()
        self.assertEqual([a['id'] for a in state['agents']], ['codex:example'])
        self.assertTrue(state['agents'][0]['orchestrator'])

    def test_native_claude_subagent_usage_is_worker_even_when_parent_is_chat(self):
        folder = self.home / 'projects/example'
        child = folder / 'parent/subagents/agent-child.jsonl'
        child.parent.mkdir(parents=True)
        row = dict(type='assistant', timestamp=self.now, message=dict(model='claude-sonnet', usage=dict(input_tokens=100)))
        for path in (folder / 'parent.jsonl', child):
            path.write_text(json.dumps(row) + '\n')
        rows = ClaudeUsageAdapter(self.home / 'projects').read(self.now - 60, {}, True).values()
        self.assertEqual(sorted(r['kind'] for r in rows), ['chat', 'worker'])

    def test_idle_codex_orchestrators_survive_a_day_and_later_registration(self):
        folder = self.home / '.codex/sessions/2026/11/10'
        folder.mkdir(parents=True)
        source = NowSource(self.home, clock=lambda: self.now)
        old = self.now - 90000
        for name in ('named', 'registered', 'plain'):
            path = folder / f'rollout-{name}.jsonl'
            rows = [dict(type='session_meta', timestamp=old,
                         payload=dict(id=name, source='vscode', cwd='/example')),
                    dict(type='event_msg', timestamp=old + 1, payload=dict(type='task_complete'))]
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            os.utime(path, (old + 1, old + 1))
        source.state()  # An unchanged old file must be reconsidered after a title or role changes.
        (self.home / '.codex/session_index.jsonl').write_text('{"id":"named","thread_name":"Example orchestrator"}\n')
        registry = self.home / '.claude/tools/agents-registry.jsonl'
        registry.parent.mkdir(parents=True)
        registry.write_text('{"id":"registered","kind":"codex","role":"orchestrator"}\n')
        cards = source.state()['agents']
        self.assertEqual({a['id'] for a in cards}, {'codex:named', 'codex:registered'})
        self.assertTrue(all(a['orchestrator'] and a['status'] == 'waiting' for a in cards))

    def test_thread_titles_support_both_database_schemas(self):
        for column in ('name', 'title'):
            with self.subTest(column=column):
                path = self.home / (column + '.sqlite')
                db = sqlite3.connect(path)
                try:
                    db.execute(f'CREATE TABLE threads(id TEXT, {column} TEXT)')
                    db.execute('INSERT INTO threads VALUES (?, ?)', ('example', 'Example orchestrator'))
                    db.commit()
                finally:
                    db.close()
                self.assertEqual(thread_names(path), {'example': 'Example orchestrator'})


if __name__ == '__main__':
    unittest.main()
