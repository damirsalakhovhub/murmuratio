"""Synthetic provider files exercised through the real loopback board API."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from tests.board_fixture_helpers import fixture_parent
from murmuratio_app.config import Config
from murmuratio_app.now import NowSource
from murmuratio_app.server import make_handler, sanitize


class BoardHTTPHarness(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.codex_home = self.home / 'isolated-codex'
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'CODEX_HOME': str(self.codex_home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = time.time()
        self.config = Config()
        self.start_server()

    def start_server(self):
        source = NowSource(home=self.home, codex_home=self.codex_home, config=self.config,
                           clock=lambda: self.now, sanitize=sanitize)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(now=source, config=self.config))
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server

    def api(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request('GET', '/api/now')
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200, body.decode('utf-8', 'replace'))
        return json.loads(body)

    def cards(self):
        snapshot = self.api()
        return {a['id']: a for a in snapshot['agents'] + snapshot['completed']}

    def write(self, path, records, mode='w', modified=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding='utf-8') as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=True) + '\n')
        stamp = self.now if modified is None else modified
        os.utime(path, (stamp, stamp))
        return path

    def event(self, kind, at=None, **payload):
        return {'type': 'event_msg', 'timestamp': self.now - 10 if at is None else at,
                'payload': {'type': kind, **payload}}

    def rollout(self, name, source='exec', records=None):
        path = self.codex_home / 'sessions/2026/11/07' / ('rollout-' + name + '.jsonl')
        meta = {'type': 'session_meta', 'timestamp': self.now - 60,
                'payload': {'id': name, 'timestamp': self.now - 60,
                            'cwd': '/synthetic/project', 'source': source,
                            **({'parent_session': 'fixture-launcher'} if source == 'exec' else {})}}
        return self.write(path, [meta] + (records if records is not None else [self.event('task_started')]))

    def claude(self, name, records, modified=None):
        fixture_parent(self.home, name)
        return self.write(self.home / '.claude/projects/project' / (name + '.jsonl'), records, modified=modified)

    def desktop(self, name, **fields):
        path = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b' / ('local_' + name + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        if not fields.get('spawnedFrom'):
            fixture_parent(self.home, name, role='orchestrator')
        path.write_text(json.dumps({'cliSessionId': name, **fields}), encoding='utf-8')
        return path

    def message(self, kind, at=None, **message):
        return {'type': kind, 'timestamp': self.now - 30 if at is None else at,
                'cwd': '/synthetic/project', 'message': message}


class BoardHTTPTests(BoardHTTPHarness):
    def test_now_accepts_loopback_forwarding_and_checks_last_proxy_hop(self):
        cases = (
            ('::1', 200),
            ('::ffff:127.0.0.1', 200),
            ('203.0.113.10, 127.0.0.1', 200),
            ('203.0.113.10, ::1', 200),
            ('127.0.0.1, 203.0.113.10', 403),
            ('::1, 203.0.113.10', 403),
            ('127.0.0.1, invalid', 403),
        )
        for forwarded, expected in cases:
            with self.subTest(forwarded=forwarded):
                connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
                try:
                    connection.request('GET', '/api/now', headers={'X-Forwarded-For': forwarded})
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, expected)
                finally:
                    connection.close()

    def test_native_codex_children_make_both_worker_and_chat_parents_visible(self):
        self.rollout('worker')
        self.rollout('child', source={'subagent': {'thread_spawn': {'parent_thread_id': 'worker'}}})
        self.rollout('chat', source='cli')
        self.rollout('chat-child', source={'subagent': {'thread_spawn': {'parent_thread_id': 'chat'}}})
        cards = self.cards()
        self.assertEqual(cards['codex:child']['parent_id'], 'codex:worker')
        self.assertEqual(cards['codex:chat-child']['parent_id'], 'codex:chat')
        self.assertEqual(cards['codex:chat-child']['status'], 'working')

    def test_claude_sidechain_has_a_nested_card_and_moves_to_finished(self):
        self.desktop('parent')
        self.claude('parent', [self.message('user', content='Coordinate'),
            self.message('assistant', stop_reason='tool_use', content=[{'type': 'tool_use', 'name': 'Agent', 'id': 'synthetic-tool', 'input': {}}])])
        path = self.home / '.claude/projects/project/parent/subagents/agent-synthetic.jsonl'
        self.write(path, [dict(self.message('user', content='Do the work'), isSidechain=True),
                          dict(self.message('assistant', stop_reason='tool_use', content='Working'), isSidechain=True)])
        cards = self.cards()
        child_id = 'claude:parent:agent-synthetic'
        self.assertEqual(set(cards), {'claude:parent', child_id})
        self.assertEqual(cards['claude:parent']['status'], 'working')
        child = cards[child_id]
        self.assertEqual((child['parent_id'], child['link'], child['project']),
                         ('claude:parent', 'exact', cards['claude:parent']['project']))
        self.assertEqual((child['status'], child['completed']), ('working', False))

        self.write(path, [dict(self.message('assistant', at=self.now - 5,
                                           stop_reason='end_turn', content='Done'), isSidechain=True)], mode='a')
        state = self.api()
        active = {card['id']: card for card in state['agents']}
        finished = {card['id']: card for card in state['completed']}
        self.assertEqual(set(active), {'claude:parent'})
        self.assertEqual(active['claude:parent']['status'], 'working')
        self.assertEqual(set(finished), {child_id})
        self.assertEqual((finished[child_id]['status'], finished[child_id]['completed'],
                          finished[child_id]['parent_id'], finished[child_id]['ended']),
                         ('done', True, 'claude:parent', self.now - 5))
