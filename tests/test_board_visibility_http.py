"""Top-level visibility uses real provider files, without lifecycle fixture registrations."""
import json
from datetime import datetime
from tests.test_board_http import BoardHTTPHarness


class BoardVisibilityHTTPTests(BoardHTTPHarness):
    def session(self, provider, name, *, parent=None, registered=False, finished=None, age=60):
        at = self.now - age
        if provider == 'claude':
            records = [self.message('user', at=at - 10, content=name),
                       self.message('assistant', at=at, stop_reason='end_turn' if finished else 'tool_use', content='Work')]
            self.write(self.home / '.claude/projects/project' / (name + '.jsonl'), records, modified=at)
            if parent:
                path = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b' / ('local_' + name + '.json')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(dict(cliSessionId=name, spawnedFrom=dict(sessionId=parent))))
        else:
            source = {'subagent': {'thread_spawn': {'parent_thread_id': parent}}} if parent else 'vscode'
            records = [dict(type='session_meta', timestamp=at - 10,
                            payload=dict(id=name, timestamp=at - 10, cwd='/synthetic/project', source=source)),
                       self.event('task_started', at=at - 9)]
            if finished:
                records.append(self.event('task_complete', at=at))
            self.write(self.codex_home / 'sessions/2026/11/07' / ('rollout-' + name + '.jsonl'), records, modified=at)
        if registered:
            self.write(self.home / '.claude/tools/agents-registry.jsonl',
                       [dict(id=name, kind=provider, role='orchestrator')], mode='a')

    def test_three_session_classes_with_and_without_children_for_both_providers(self):
        for provider in ('claude', 'codex'):
            for role in ('registered', 'worker', 'chat'):
                for child in (False, True):
                    name = f'{provider}-{role}-{child}'
                    self.session(provider, name, registered=role == 'registered',
                                 parent='missing-launcher' if role == 'worker' else None)
                    if child:
                        self.session(provider, name + '-child', parent=name)
        cards = self.cards()
        for provider in ('claude', 'codex'):
            for role in ('registered', 'worker', 'chat'):
                for child in (False, True):
                    name = f'{provider}-{role}-{child}'
                    with self.subTest(provider=provider, role=role, child=child):
                        self.assertEqual(provider + ':' + name in cards, role != 'chat' or child)
                        if child:
                            self.assertIn(provider + ':' + name + '-child', cards)
        self.assertNotIn('codex_chats', self.api())

    def test_finished_today_child_keeps_old_waiting_root_until_calendar_changes(self):
        self.now = datetime.fromtimestamp(self.now).replace(hour=12, minute=0, second=0).timestamp()
        for provider in ('claude', 'codex'):
            self.session(provider, provider + '-root', finished=True, age=7200)
            self.session(provider, provider + '-child', parent=provider + '-root', finished=True, age=3600)
        cards = self.cards()
        for provider in ('claude', 'codex'):
            self.assertIn(provider + ':' + provider + '-root', cards)
            if provider == 'codex':
                self.assertIn(provider + ':' + provider + '-child', cards)
        self.now += 86400
        cards = self.cards()
        for provider in ('claude', 'codex'):
            self.assertNotIn(provider + ':' + provider + '-root', cards)

    def test_child_expired_by_retention_does_not_leave_a_lone_root(self):
        from unittest.mock import patch
        root = dict(id='claude:root', kind='claude', task='Root', project='project',
                    status='waiting', last_at=self.now - 3600, waiting_since=self.now - 3600,
                    app_session='local_root', orchestrator=True)
        child = dict(id='claude:child', kind='claude', task='Child', project='project',
                     status='waiting', last_at=self.now - 3600, waiting_since=self.now - 3600,
                     parent_id='claude:root', parent_session='root', orchestrator=False)
        from murmuratio_app.now import NowSource
        with patch.object(NowSource, 'link_parents', return_value=[root, child]):
            self.assertEqual(self.cards(), {})

    def test_lone_exec_and_wrapper_stay_visible_but_finished_chats_hide(self):
        self.write(self.codex_home / 'sessions/2026/11/07/rollout-lone-exec.jsonl', [
            dict(type='session_meta', timestamp=self.now - 20, payload=dict(id='lone-exec', source='exec', cwd='/synthetic/project')),
            self.event('task_started')])
        self.config.modules['codex_wrapper'] = True
        run = self.home / '.local/share/murmuratio/codex-runs/lone-wrapper.json'
        run.parent.mkdir(parents=True)
        run.write_text(json.dumps(dict(status='done', started=self.now - 20, ended=self.now - 10, title='Lone wrapper')))
        for provider in ('claude', 'codex'):
            self.session(provider, 'lone-finished', finished=True)
        self.start_server()
        self.assertEqual(set(self.cards()), {'codex:lone-exec', 'codex:lone-wrapper'})

    def test_native_child_with_missing_parent_remains_visible(self):
        self.session('codex', 'native-child', parent='missing-parent')
        card = self.cards()['codex:native-child']
        self.assertEqual(card['parent_session'], 'missing-parent')
        self.assertIsNone(card['parent_id'])

    def test_child_in_another_project_still_qualifies_its_starter(self):
        self.session('claude', 'cross-root')
        self.session('claude', 'cross-child', parent='cross-root')
        path = self.home / '.claude/projects/project/cross-child.jsonl'
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for record in records:
            record['cwd'] = '/synthetic/another-project'
        self.write(path, records)
        cards = self.cards()
        self.assertIn('claude:cross-root', cards)
        self.assertIsNone(cards['claude:cross-child']['parent_id'])
        self.assertEqual(cards['claude:cross-child']['parent_session'], 'cross-root')
        self.assertEqual(cards['claude:cross-child']['project'], 'another-project')

    def test_unregistered_desktop_orchestrator_is_visible_without_children(self):
        for child in (False, True):
            name = 'desktop-' + str(child)
            self.session('claude', name)
            path = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b' / ('local_' + name + '.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dict(cliSessionId=name, title='Example orchestrator')))
            if child:
                self.session('claude', name + '-child', parent='local_' + name)
        cards = self.cards()
        self.assertIn('claude:desktop-False', cards)
        self.assertTrue(cards['claude:desktop-False']['orchestrator'])
        self.assertTrue(cards['claude:desktop-True']['orchestrator'])
        self.assertNotIn('registered', cards['claude:desktop-True'])
        self.assertEqual(cards['claude:desktop-True-child']['parent_id'], 'claude:desktop-True')

    def test_sanitized_live_named_orchestrator_survives_but_plain_desktop_chat_stays_hidden(self):
        for name, title in (('coordinator', 'Example orchestrator 001 CLAUDE'),
                            ('plain', 'Example brand discussion')):
            self.session('claude', name, finished=True, age=7200)
            path = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b' / ('local_' + name + '.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dict(cliSessionId=name, title=title)))
        cards = self.cards()
        self.assertIn('claude:coordinator', cards)
        self.assertEqual(cards['claude:coordinator']['status'], 'waiting')
        self.assertNotIn('registered', cards['claude:coordinator'])
        self.assertIsNone(cards['claude:coordinator']['parent_id'])
        self.assertNotIn('claude:plain', cards)
