"""The pre-restart check at the real CLI and local HTTP boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from murmuratio_app.check_update import CheckError, archived_from_disk, board_view, candidate, check_update, read_board
from tests.test_board_http import BoardHTTPHarness


ROOT = Path(__file__).resolve().parent.parent


class CheckUpdateTests(BoardHTTPHarness):
    def setUp(self):
        super().setUp()
        self.claude('root', [self.message('user', at=self.now - 7260, content='PRIVATE TASK TEXT'),
                             self.message('assistant', at=self.now - 7200, stop_reason='end_turn', content='SECRET REPLY')],
                    modified=self.now - 7200)
        self.desktop('root', title='PRIVATE DESKTOP TITLE')
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def cli(self, mutation=None):
        script = '''
import sys
from murmuratio_app import cli, now
PREFIX
original = now.NowSource.state
def mutated(self):
    state = original(self)
    for card in state['agents']:
        if card['id'] == 'claude:root':
            ACTION
    return state
now.NowSource.state = mutated
sys.exit(cli.main(sys.argv[1:]))
'''
        action = {
            'removed': "state['agents'].remove(card)",
            'status': "card['status'] = 'working'",
            'role': "card['orchestrator'] = False",
        }.get(mutation, 'pass')
        prefix = {
            'archive': '''from murmuratio_app.adapters.claude import ClaudeDesktopAdapter
original_collect = ClaudeDesktopAdapter.collect
def broken_collect(self):
    for row in original_collect(self):
        row.value['archived'] = True
        yield row
ClaudeDesktopAdapter.collect = broken_collect''',
            'hide_chats': '''from murmuratio_app.config import Config
original_load = Config.load.__func__
def broken_load(cls, *args, **kwargs):
    value = original_load(cls, *args, **kwargs)
    value.show_codex_chats = False
    return value
Config.load = classmethod(broken_load)''',
        }.get(mutation, '')
        script = script.replace('PREFIX', prefix).replace('ACTION', action)
        return subprocess.run([sys.executable, '-B', '-c', script,
                               'check-update', '--url', self.url], cwd=ROOT, env=os.environ.copy(),
                              capture_output=True, text=True, timeout=20)

    def test_unchanged_board_passes_without_persisting_or_printing_tasks(self):
        before = {path: path.read_bytes() for path in self.home.rglob('*') if path.is_file()}
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('1 visible cards before, 1 after', result.stdout)
        self.assertNotIn('PRIVATE', result.stdout + result.stderr)
        self.assertNotIn('SECRET', result.stdout + result.stderr)
        self.assertEqual(before, {path: path.read_bytes() for path in self.home.rglob('*') if path.is_file()})
        projected, _ = board_view(self.api())
        self.assertNotIn('task', projected['claude:root'])
        self.assertNotIn('cwd', projected['claude:root'])

    def test_missing_status_and_orchestrator_changes_block_the_restart(self):
        for mutation in ('removed', 'status', 'role'):
            with self.subTest(mutation=mutation):
                result = self.cli(mutation)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('claude:root', result.stdout)
                self.assertIn('restart blocked', result.stdout)
                if mutation == 'status':
                    self.assertIn('status "waiting" to "working"', result.stdout)
                self.assertNotIn('PRIVATE', result.stdout + result.stderr)

    def test_changed_live_board_requires_a_retry(self):
        first = self.api()
        latest = json.loads(json.dumps(first))
        latest['agents'][0]['status'] = 'working'
        with patch('murmuratio_app.check_update.read_board', side_effect=[first, latest]), \
                patch('murmuratio_app.check_update.candidate', return_value=(first, set(), False)):
            with self.assertRaisesRegex(CheckError, 'changed during comparison'):
                check_update(self.url, self.home)

    def test_changed_removal_evidence_requires_a_retry(self):
        first = self.api()
        latest = json.loads(json.dumps(first))
        latest['agents'][0]['last_at'] += 1
        empty = json.loads(json.dumps(first))
        empty['agents'] = []
        with patch('murmuratio_app.check_update.read_board', side_effect=[first, latest]), \
                patch('murmuratio_app.check_update.candidate', return_value=(empty, {'claude:root'}, False)):
            with self.assertRaisesRegex(CheckError, 'evidence changed'):
                check_update(self.url, self.home)

    def test_candidate_archive_regression_cannot_justify_its_own_loss(self):
        result = self.cli('archive')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('claude:root', result.stdout)
        self.assertIn('unexplained', result.stdout)

    def test_conflicting_desktop_archive_records_never_prove_archival(self):
        directory = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b'
        active = directory / 'local_root.json'
        duplicate = directory / 'local_duplicate.json'
        for first, second in ((True, False), (False, True)):
            active.write_text(json.dumps({'cliSessionId': 'root', 'isArchived': first}))
            duplicate.write_text(json.dumps({'cliSessionId': 'root', 'isArchived': second}))
            self.assertNotIn('claude:root', archived_from_disk(self.home))
        duplicate.write_text(json.dumps({'cliSessionId': 'root', 'isArchived': True}))
        active.write_text(json.dumps({'cliSessionId': 'root', 'isArchived': True}))
        self.assertIn('claude:root', archived_from_disk(self.home))

    def test_candidate_setting_regression_cannot_justify_hiding_chat(self):
        config = self.home / '.config/murmuratio/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[board]\nshow_codex_chats = true\n')
        self.config.show_codex_chats = True
        self.rollout('visible-chat', source='cli')
        self.start_server()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        result = self.cli('hide_chats')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('codex:visible-chat', result.stdout)
        self.assertIn('unexplained', result.stdout)

    def test_candidate_reads_wrapper_without_writing_enabled_quota_modules(self):
        config = self.home / '.config/murmuratio/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[modules]\nforecast = true\nrouting = true\ncodex_wrapper = true\n')
        wrapper = self.home / '.local/share/murmuratio/codex-runs/example.json'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(json.dumps({'status': 'running', 'started': self.now - 10,
                                       'cwd': '/synthetic/project', 'title': 'PRIVATE WRAPPER'}))
        before = {path: path.read_bytes() for path in self.home.rglob('*') if path.is_file()}
        state, _, _ = candidate(self.home, None, self.codex_home, self.now)
        self.assertIn('codex:example', {card['id'] for card in state['agents']})
        self.assertEqual(before, {path: path.read_bytes() for path in self.home.rglob('*') if path.is_file()})

    def test_unhealthy_source_and_invalid_live_responses_fail_closed(self):
        first = self.api()
        broken = json.loads(json.dumps(first))
        broken['sources'] = {'claude_sessions': {'status': 'unavailable'}}
        with self.assertRaisesRegex(CheckError, 'claude_sessions'):
            board_view(broken)
        broken = json.loads(json.dumps(first))
        broken['agents'].append(broken['agents'][0])
        with self.assertRaisesRegex(CheckError, 'duplicate'):
            board_view(broken)
        first['agents'][0]['project'] = ''
        self.assertEqual(board_view(first)[0]['claude:root']['project'], '')
        for url in ('https://localhost', 'http://example.com', 'http://localhost/path',
                    'http://user@localhost', 'http://localhost:1?x=1'):
            with self.subTest(url=url), self.assertRaises(CheckError):
                read_board(url)

    def test_only_direct_evidence_allows_an_archived_or_hidden_card_to_leave(self):
        state = self.api()
        card = state['agents'][0]
        candidate = json.loads(json.dumps(state))
        candidate['agents'] = []
        with patch('murmuratio_app.check_update.read_board', side_effect=[state, state]), \
                patch('murmuratio_app.check_update.candidate', return_value=(candidate, {'claude:root'}, False)):
            self.assertEqual(check_update(self.url, self.home), 0)
        hidden = json.loads(json.dumps(state))
        hidden['agents'][0].update(id='codex:chat', kind='codex', codex_role='chat')
        with patch('murmuratio_app.check_update.read_board', side_effect=[hidden, hidden]), \
                patch('murmuratio_app.check_update.candidate', return_value=(candidate, set(), False)):
            self.assertEqual(check_update(self.url, self.home), 0)
