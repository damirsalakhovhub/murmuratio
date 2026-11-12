"""The pre-restart check at the real CLI and local HTTP boundaries."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from murmuratio_app.check_update import CheckError, archived_from_disk, board_view, candidate, check_update, read_board
from murmuratio_app.now import NowSource
from tests.test_board_http import BoardHTTPHarness


ROOT = Path(__file__).resolve().parent.parent


class CheckUpdateTests(BoardHTTPHarness):
    def setUp(self):
        super().setUp()
        self.claude('root', [self.message('user', at=self.now - 7260, content='PRIVATE TASK TEXT'),
                             self.message('assistant', at=self.now - 7200, stop_reason='end_turn', content='SECRET REPLY')],
                    modified=self.now - 7200)
        self.desktop('root', title='PRIVATE DESKTOP TITLE')
        registry = self.home / '.claude/tools/agents-registry.jsonl'
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps(dict(id='root', kind='claude', role='orchestrator')) + '\n')
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
                patch('murmuratio_app.check_update.candidate', return_value=(first, set(), {}, {})):
            with self.assertRaisesRegex(CheckError, 'changed during comparison'):
                check_update(self.url, self.home)

    def test_recognised_codex_orchestrator_and_its_worker_explain_their_changes(self):
        def board(orchestrator, worker_parent, worker_project, worker_from=None):
            role = 'orchestrator' if orchestrator else 'worker'
            cards = [
                {'id': 'claude:harbor', 'kind': 'claude', 'project': 'Harbor', 'status': 'waiting', 'orchestrator': True},
                {'id': 'codex:space-orch', 'kind': 'codex', 'codex_role': role, 'project': 'Atlas',
                 'status': 'waiting' if orchestrator else 'done', 'orchestrator': orchestrator,
                 'completed': not orchestrator, 'ended': None if orchestrator else self.now - 60},
                {'id': 'codex:codex-task.env', 'kind': 'codex', 'codex_role': 'worker', 'project': worker_project,
                 'status': 'working', 'orchestrator': False, 'parent_id': worker_parent}]
            return {'now': self.now, 'agents': [c for c in cards if not c.get('completed')],
                    'completed': [c for c in cards if c.get('completed')]}
        live = board(False, 'claude:harbor', 'Harbor')
        for fixed, code in ((board(True, 'codex:space-orch', 'Atlas'), 0),
                            (board(True, 'claude:harbor', 'Atlas'), 1)):
            with self.subTest(code=code), \
                    patch('murmuratio_app.check_update.read_board', side_effect=[live, live]), \
                    patch('murmuratio_app.check_update.candidate', return_value=(fixed, set(), {}, {})), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(check_update(self.url, self.home), code, output.getvalue())
            self.assertIn('now a Codex orchestrator', output.getvalue())
            if code:
                self.assertIn('"codex:codex-task.env": project "Harbor" to "Atlas"; unexplained', output.getvalue())
            else:
                self.assertIn('now nested under "codex:space-orch"', output.getvalue())

    def test_changed_removal_evidence_requires_a_retry(self):
        first = self.api()
        latest = json.loads(json.dumps(first))
        latest['agents'][0]['last_at'] += 1
        empty = json.loads(json.dumps(first))
        empty['agents'] = []
        with patch('murmuratio_app.check_update.read_board', side_effect=[first, latest]), \
                patch('murmuratio_app.check_update.candidate', return_value=(empty, {'claude:root'}, {}, {})):
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

    def test_candidate_reads_wrapper_without_writing_enabled_quota_modules(self):
        config = self.home / '.config/murmuratio/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[modules]\nforecast = true\nrouting = true\ncodex_wrapper = true\n')
        wrapper = self.home / '.local/share/murmuratio/codex-runs/example.json'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(json.dumps({'status': 'running', 'started': self.now - 10,
                                       'thread': 'thread-1', 'parent_session': 'fixture-launcher',
                                       'cwd': '/synthetic/project', 'title': 'PRIVATE WRAPPER'}))
        before = {path: path.read_bytes() for path in self.home.rglob('*') if path.is_file()}
        state, _, wrapper_threads, _ = candidate(self.home, None, self.codex_home, self.now)
        self.assertIn('codex:example', {card['id'] for card in state['agents']})
        self.assertEqual(wrapper_threads, {'codex:example': 'thread-1'})
        self.assertEqual(before, {path: path.read_bytes() for path in self.home.rglob('*') if path.is_file()})

    def test_candidate_disables_live_limits_even_when_configured(self):
        config = self.home / '.config/murmuratio/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[limits]\ncodex_live = true\n')
        with patch('murmuratio_app.check_update.NowSource', wraps=NowSource) as source, \
                patch('murmuratio_app.now.app_server_limits') as live_limits:
            state, *_ = candidate(self.home, None, self.codex_home, self.now)
            self.assertFalse(source.call_args.kwargs['config'].codex_live_limits)
            live_limits.assert_not_called()
        self.assertIn('claude:root', {card['id'] for card in state['agents']})

    def test_unhealthy_source_and_invalid_live_responses_fail_closed(self):
        first = self.api()
        for name in ('claude_sessions', 'agents_registry'):
            with self.subTest(source=name):
                broken = json.loads(json.dumps(first))
                broken['sources'] = {name: {'status': 'unavailable'}}
                with self.assertRaisesRegex(CheckError, name):
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

    def test_unhealthy_baseline_source_is_skipped_when_candidate_is_healthy(self):
        baseline = self.api()
        baseline['sources'] = {'codex_wrapper': {'status': 'unsupported', 'reason': 'unrecognized_format'}}
        candidate_state = json.loads(json.dumps(baseline))
        candidate_state['sources']['codex_wrapper'] = {'status': 'available'}
        output = io.StringIO()
        with patch('murmuratio_app.check_update.read_board', side_effect=[baseline, baseline]), \
                patch('murmuratio_app.check_update.candidate', return_value=(candidate_state, set(), {}, {})), \
                redirect_stdout(output):
            self.assertEqual(check_update(self.url, self.home), 0)
        self.assertEqual(output.getvalue().count('No baseline for source codex_wrapper'), 1)

    def test_unchanged_unhealthy_source_does_not_block_but_card_loss_still_does(self):
        baseline = self.api()
        for status, reason in (('unsupported', 'unrecognized_format'), ('unavailable', 'permission_denied')):
            baseline['sources'] = {'codex_wrapper': {'status': status, 'reason': reason}}
            for remove, expected in ((False, 0), (True, 1)):
                candidate_state = json.loads(json.dumps(baseline))
                if remove:
                    candidate_state['agents'] = []
                with self.subTest(status=status, remove=remove), \
                        patch('murmuratio_app.check_update.read_board', side_effect=[baseline, baseline]), \
                        patch('murmuratio_app.check_update.candidate', return_value=(candidate_state, set(), {}, {})), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(check_update(self.url, self.home), expected, output.getvalue())
                self.assertIn('No baseline for source codex_wrapper', output.getvalue())

    def test_new_or_changed_source_failure_blocks_the_restart(self):
        baseline = self.api()
        for old, new in (({'status': 'available'}, {'status': 'unsupported', 'reason': 'unrecognized_format'}),
                         ({'status': 'absent'}, {'status': 'unavailable', 'reason': 'permission_denied'}),
                         ({'status': 'unavailable', 'reason': 'permission_denied'},
                          {'status': 'unavailable', 'reason': 'reader_failed'}),
                         ({'status': 'unsupported', 'reason': 'unrecognized_format'},
                          {'status': 'unavailable', 'reason': 'read_failed'}),
                         ({'status': 'unsupported', 'reason': 'unrecognized_format'}, {'status': 'absent'})):
            baseline['sources'] = {'codex_sessions': old}
            candidate_state = json.loads(json.dumps(baseline))
            candidate_state['sources']['codex_sessions'] = new
            with self.subTest(old=old, new=new), \
                    patch('murmuratio_app.check_update.read_board', side_effect=[baseline, baseline]), \
                    patch('murmuratio_app.check_update.candidate', return_value=(candidate_state, set(), {}, {})):
                with self.assertRaisesRegex(CheckError, 'codex_sessions.*restart blocked'):
                    check_update(self.url, self.home)

    def test_integration_registry_health_is_checked_even_with_identical_cards(self):
        baseline = self.api()
        failed = {'status': 'unavailable', 'reason': 'permission_denied'}
        for old, new, blocked in (
                ({}, failed, True),
                ({'status': 'absent'}, failed, True),
                (failed, {'status': 'unavailable', 'reason': 'read_failed'}, True),
                (failed, {'status': 'unsupported', 'reason': 'unrecognized_format'}, True),
                (failed, failed, False),
                (failed, {'status': 'available'}, False)):
            baseline['sources'] = {'integration_registry': old}
            updated = json.loads(json.dumps(baseline))
            updated['sources']['integration_registry'] = new
            with self.subTest(old=old, new=new), \
                    patch('murmuratio_app.check_update.read_board', side_effect=[baseline, baseline]), \
                    patch('murmuratio_app.check_update.candidate', return_value=(updated, set(), {}, {})), \
                    redirect_stdout(io.StringIO()):
                if blocked:
                    with self.assertRaisesRegex(CheckError, 'integration_registry.*restart blocked'):
                        check_update(self.url, self.home)
                else:
                    self.assertEqual(check_update(self.url, self.home), 0)

    def test_recovered_codex_source_can_identify_the_same_previously_unknown_turn(self):
        baseline = self.api()
        baseline['sources'] = {'codex_sessions': {'status': 'unsupported', 'reason': 'unrecognized_format'}}
        baseline['agents'][0].update(id='codex:thread', kind='codex', thread='thread', codex_role='worker',
                                     status='unknown', turn_started=self.now - 20)
        for change, expected in (({}, 0), ({'turn_started': self.now - 10}, 1), ({'project': 'Elsewhere'}, 1),
                                 ({'thread': 'different-thread'}, 1), ({'parent_id': 'codex:other'}, 1)):
            candidate_state = json.loads(json.dumps(baseline))
            candidate_state['sources']['codex_sessions'] = {'status': 'available'}
            candidate_state['agents'][0].update(status='working', **change)
            with self.subTest(change=change), \
                    patch('murmuratio_app.check_update.read_board', side_effect=[baseline, baseline]), \
                    patch('murmuratio_app.check_update.candidate', return_value=(candidate_state, set(), {}, {})), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(check_update(self.url, self.home), expected, output.getvalue())
        candidate_state = json.loads(json.dumps(baseline))
        candidate_state['agents'][0]['status'] = 'working'
        with patch('murmuratio_app.check_update.read_board', side_effect=[baseline, baseline]), \
                patch('murmuratio_app.check_update.candidate', return_value=(candidate_state, set(), {}, {})), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check_update(self.url, self.home), 1, output.getvalue())

    def test_missing_card_from_healthy_codex_source_still_blocks(self):
        baseline = self.api()
        baseline['sources'] = {'codex_sessions': {'status': 'available'}}
        baseline['agents'][0].update(id='codex:thread', kind='codex')
        candidate_state = json.loads(json.dumps(baseline))
        candidate_state['agents'] = []
        output = io.StringIO()
        with patch('murmuratio_app.check_update.read_board', side_effect=[baseline, baseline]), \
                patch('murmuratio_app.check_update.candidate', return_value=(candidate_state, set(), {}, {})), \
                redirect_stdout(output):
            self.assertEqual(check_update(self.url, self.home), 1)
        self.assertIn('unexplained; restart blocked', output.getvalue())

    def test_native_thread_rekeyed_to_wrapper_card_with_same_thread_passes(self):
        baseline = self.api()
        baseline_card = baseline['agents'][0]
        baseline_card.update(id='codex:thread', kind='codex', thread='thread')
        baseline['sources']['codex_wrapper'] = {'status': 'unsupported'}
        candidate_state = json.loads(json.dumps(baseline))
        replacement = dict(candidate_state['agents'][0], id='codex:codex-task.nNvgAY')
        candidate_state['agents'] = [replacement]
        candidate_state['sources']['codex_wrapper'] = {'status': 'available'}
        output = io.StringIO()
        with patch('murmuratio_app.check_update.read_board', side_effect=[baseline, baseline]), \
                patch('murmuratio_app.check_update.candidate', return_value=(candidate_state, set(),
                    {'codex:codex-task.nNvgAY': 'thread'}, {})), \
                redirect_stdout(output):
            self.assertEqual(check_update(self.url, self.home), 0)
        self.assertIn('Re-keyed "project" "codex:thread" as "codex:codex-task.nNvgAY"', output.getvalue())

    def test_claude_task_card_merged_into_its_claude_session_passes(self):
        def card(card_id, kind, parent=None):
            return {'id': card_id, 'kind': kind, 'project': 'Atlas', 'status': 'done', 'orchestrator': False,
                    'completed': True, 'ended': self.now - 60, 'parent_id': parent, 'parent_session': 'fixture-launcher'}
        orchestrator = {'id': 'codex:space-orch', 'kind': 'codex', 'codex_role': 'orchestrator', 'project': 'Atlas',
                        'status': 'waiting', 'orchestrator': True}
        live = {'now': self.now, 'agents': [orchestrator],
                'completed': [card('claude:consult', 'claude'), card('codex:claude-task.run-1', 'codex')]}
        fixed = {'now': self.now, 'agents': [orchestrator], 'completed': [card('claude:consult', 'claude', 'codex:space-orch')]}
        for merged, code in (({'codex:claude-task.run-1': 'claude:consult'}, 0),
                             ({'codex:claude-task.run-1': 'claude:other'}, 1)):
            with self.subTest(code=code), \
                    patch('murmuratio_app.check_update.read_board', side_effect=[live, live]), \
                    patch('murmuratio_app.check_update.candidate', return_value=(fixed, set(), {}, merged)), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(check_update(self.url, self.home), code, output.getvalue())
            if code:
                self.assertIn('"codex:claude-task.run-1" status "done": unexplained', output.getvalue())
            else:
                self.assertIn('Merged "Atlas" "codex:claude-task.run-1" into its Claude session card "claude:consult"',
                              output.getvalue())

    def test_orchestrator_idle_under_a_newly_nested_working_worker_passes(self):
        def board(nested, orchestrator_status):
            return {'now': self.now, 'agents': [
                {'id': 'codex:space-orch', 'kind': 'codex', 'codex_role': 'orchestrator', 'project': 'Atlas',
                 'status': orchestrator_status, 'orchestrator': True},
                {'id': 'claude:consult', 'kind': 'claude', 'project': 'Atlas', 'status': 'working',
                 'orchestrator': False, 'parent_id': 'codex:space-orch' if nested else None}], 'completed': []}
        live = board(False, 'waiting')
        for fixed, code in ((board(True, 'idle'), 0), (board(False, 'idle'), 1)):
            with self.subTest(code=code), \
                    patch('murmuratio_app.check_update.read_board', side_effect=[live, live]), \
                    patch('murmuratio_app.check_update.candidate', return_value=(fixed, set(), {}, {})), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(check_update(self.url, self.home), code, output.getvalue())
            self.assertIn('idle while its newly nested worker "claude:consult" works' if not code else 'unexplained',
                          output.getvalue())

    def test_wrapper_card_for_different_thread_does_not_explain_removal(self):
        baseline = self.api()
        baseline['agents'][0].update(id='codex:thread', kind='codex', thread='thread')
        baseline['sources']['codex_wrapper'] = {'status': 'unsupported'}
        candidate_state = json.loads(json.dumps(baseline))
        replacement = dict(candidate_state['agents'][0], id='codex:codex-task.nNvgAY',
                           thread='another-thread')
        candidate_state['agents'] = [replacement]
        candidate_state['sources']['codex_wrapper'] = {'status': 'available'}
        output = io.StringIO()
        with patch('murmuratio_app.check_update.read_board', side_effect=[baseline, baseline]), \
                patch('murmuratio_app.check_update.candidate', return_value=(candidate_state, set(),
                    {'codex:codex-task.nNvgAY': 'another-thread'}, {})), \
                redirect_stdout(output):
            self.assertEqual(check_update(self.url, self.home), 1)
        self.assertIn('unexplained; restart blocked', output.getvalue())

    def test_only_direct_evidence_allows_an_archived_or_hidden_card_to_leave(self):
        state = self.api()
        card = state['agents'][0]
        candidate = json.loads(json.dumps(state))
        candidate['agents'] = []
        with patch('murmuratio_app.check_update.read_board', side_effect=[state, state]), \
                patch('murmuratio_app.check_update.candidate', return_value=(candidate, {'claude:root'}, {}, {})):
            self.assertEqual(check_update(self.url, self.home), 0)
        hidden = json.loads(json.dumps(state))
        hidden['agents'][0].update(id='codex:chat', kind='codex', codex_role='chat', registered=False, orchestrator=False)
        with patch('murmuratio_app.check_update.read_board', side_effect=[hidden, hidden]), \
                patch('murmuratio_app.check_update.candidate', return_value=(candidate, set(), {}, {})):
            self.assertEqual(check_update(self.url, self.home), 0)

    def test_eligible_root_removal_is_blocked_even_after_child_leaves_the_ui(self):
        root = dict(id='claude:plain', kind='claude', project='Example', status='waiting',
                    orchestrator=False, last_at=self.now - 3600)
        child = dict(id='codex:child', kind='codex', project='Example', status='done', completed=True,
                     parent_id=root['id'], ended=self.now - 3600)
        for cards in ([root, child], [{**root, 'has_eligible_children': True}]):
            live = dict(now=self.now, agents=[cards[0]], completed=cards[1:])
            empty = dict(now=self.now, agents=[], completed=[])
            with patch('murmuratio_app.check_update.read_board', side_effect=[live, live]), \
                    patch('murmuratio_app.check_update.candidate', return_value=(empty, set(), {}, {})), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(check_update(self.url, self.home), 1, output.getvalue())
                self.assertIn('unexplained; restart blocked', output.getvalue())

    def test_finished_cli_parent_with_api_child_flag_is_retained(self):
        root = dict(id='claude:plain', kind='claude', project='Example', status='done',
                    ended=self.now - 3600, has_eligible_children=True)
        live = dict(now=self.now, agents=[root], completed=[])
        self.assertEqual(set(board_view(live)[0]), {root['id']})
        empty = dict(now=self.now, agents=[], completed=[])
        with patch('murmuratio_app.check_update.read_board', side_effect=[live, live]), \
                patch('murmuratio_app.check_update.candidate', return_value=(empty, set(), {}, {})), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check_update(self.url, self.home), 1, output.getvalue())

    def test_unparented_codex_worker_loss_is_not_a_hidden_chat(self):
        root = dict(id='codex:worker', kind='codex', codex_role='worker', project='Example',
                    status='working', last_at=self.now)
        live = dict(now=self.now, agents=[root], completed=[])
        empty = dict(now=self.now, agents=[], completed=[])
        with patch('murmuratio_app.check_update.read_board', side_effect=[live, live]), \
                patch('murmuratio_app.check_update.candidate', return_value=(empty, set(), {}, {})), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check_update(self.url, self.home), 1, output.getvalue())

    def test_only_yesterday_finished_child_does_not_qualify_a_plain_root(self):
        from datetime import datetime
        midnight = datetime.fromtimestamp(self.now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        root = dict(id='claude:plain', kind='claude', project='Example', status='waiting', last_at=midnight - 600)
        child = dict(id='codex:child', kind='codex', project='Example', status='done', completed=True,
                     parent_id=root['id'], ended=midnight - 60)
        live = dict(now=midnight + 60, agents=[root], completed=[child])
        after = dict(now=midnight + 60, agents=[], completed=[child])
        with patch('murmuratio_app.check_update.read_board', side_effect=[live, live]), \
                patch('murmuratio_app.check_update.candidate', return_value=(after, set(), {}, {})), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check_update(self.url, self.home), 0, output.getvalue())

    def test_named_desktop_orchestrator_loss_is_unexplained_but_plain_chat_can_hide(self):
        for task, expected in (('Example orchestrator 001 CLAUDE', 1), ('Example brand discussion', 0)):
            with self.subTest(task=task):
                self.desktop('root', title=task)
                registry = self.home / '.claude/tools/agents-registry.jsonl'
                registry.unlink()
                baseline = self.api()
                # Capture the older running board's visible top-level desktop chat.
                if not baseline['agents']:
                    with patch('murmuratio_app.now.board_sessions', side_effect=lambda agents, *args: agents):
                        baseline = self.api()
                self.assertEqual(len(baseline['agents']), 1)
                fixed = dict(baseline, agents=[])
                with patch('murmuratio_app.check_update.read_board', side_effect=[baseline, baseline]), \
                     patch('murmuratio_app.check_update.candidate', return_value=(fixed, set(), {}, {})), \
                     redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(check_update(self.url, self.home), expected, output.getvalue())
                self.assertIn('unexplained' if expected else 'top-level session has no active or today-finished children', output.getvalue())
