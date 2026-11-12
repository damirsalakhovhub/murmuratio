"""The agents registry: launch wrappers record who each agent is, the board trusts it."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from murmuratio_app.check_update import reason_for_change
from murmuratio_app.adapters.registry import AgentsRegistryAdapter, parse_record
from murmuratio_app.config import Config
from murmuratio_app.grouping import apply_registry, registry_column
from tests.test_board_http import BoardHTTPHarness


class RegistryReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'agents-registry.jsonl'
        self.adapter = AgentsRegistryAdapter(self.path)

    def read(self):
        result = self.adapter.read()
        return result.status, {record['id']: record for record in result.values()}

    def test_absent_file_is_a_normal_absent_source(self):
        self.assertEqual(self.read(), ('absent', {}))
        self.assertEqual(self.adapter.malformed, 0)

    def test_last_record_per_id_wins_and_unknown_fields_are_ignored(self):
        self.path.write_text(
            json.dumps({'id': 'a', 'kind': 'claude', 'project': 'Old', 'role': 'worker'}) + '\n'
            + json.dumps({'id': 'b', 'kind': 'codex', 'name': 'B'}) + '\n'
            + json.dumps({'id': 'a', 'kind': 'claude', 'project': 'New', 'role': 'Orchestrator',
                          'future_field': {'x': 1}, 'started_at': 1700000000}) + '\n')
        status, records = self.read()
        self.assertEqual(status, 'available')
        self.assertEqual(records['a'], {'id': 'a', 'kind': 'claude', 'project': 'New',
                                        'role': 'orchestrator', 'started_at': 1700000000.0})
        self.assertEqual(records['b'], {'id': 'b', 'kind': 'codex', 'name': 'B'})

    def test_malformed_lines_are_skipped_and_counted(self):
        lines = ['{not json', '[1, 2]', json.dumps({'kind': 'claude'}), json.dumps({'id': 'x'}),
                 json.dumps({'id': 'ok', 'kind': 'codex', 'parent_id': 5, 'started_at': 'soon', 'name': ['n']}),
                 '\xff\xfe', '', 'x' * 70000]
        self.path.write_text('\n'.join(lines) + '\n', encoding='utf-8', errors='surrogateescape')
        status, records = self.read()
        self.assertEqual(status, 'available')
        # Wrong-typed optional fields count as missing, not as a broken line.
        self.assertEqual(records, {'ok': {'id': 'ok', 'kind': 'codex'}})
        self.assertEqual(self.adapter.malformed, 6)

    def test_unfinished_last_line_is_not_counted_until_complete(self):
        good = json.dumps({'id': 'a', 'kind': 'claude'}) + '\n'
        self.path.write_text(good + '{"id": "b", "ki')
        self.assertEqual(set(self.read()[1]), {'a'})
        self.assertEqual(self.adapter.malformed, 0)
        self.path.write_text(good + json.dumps({'id': 'b', 'kind': 'codex'}))
        self.assertEqual(set(self.read()[1]), {'a', 'b'})

    def test_parse_record_keeps_text_on_one_line(self):
        record = parse_record({'id': ' t1 ', 'kind': 'Codex', 'name': 'Two\nlines', 'started_at': float('nan')})
        self.assertEqual(record, {'id': 't1', 'kind': 'codex', 'name': 'Two lines'})

    def test_config_path_and_flag(self):
        with tempfile.TemporaryDirectory() as home:
            config = Config.load(home)
            self.assertEqual(config.agents_registry, Path(home) / '.claude/tools/agents-registry.jsonl')
            self.assertFalse(config.mark_guessed)
            path = Path(home) / 'config.toml'
            path.write_text('[board]\nmark_guessed = true\n[paths]\nagents_registry = "~/r.jsonl"\n')
            config = Config.load(home, path)
            self.assertEqual((config.agents_registry, config.mark_guessed), (Path(home) / 'r.jsonl', True))
            path.write_text('[board]\nmark_guessed = "yes"\n')
            with self.assertRaises(ValueError):
                Config.load(home, path)


class ApplyRegistryTests(unittest.TestCase):
    def card(self, card_id, kind, **fields):
        return {'id': card_id, 'kind': kind, 'project': 'guess', 'task': 'guess', 'status': 'working',
                'orchestrator': False, 'parent_id': None, **fields}

    def test_registry_parent_cycle_is_broken(self):
        a, b = self.card('codex:a', 'codex', thread='a'), self.card('codex:b', 'codex', thread='b')
        apply_registry([a, b], {'a': {'id': 'a', 'kind': 'codex', 'parent_id': 'b'},
                                'b': {'id': 'b', 'kind': 'codex', 'parent_id': 'a'}})
        self.assertEqual(sum(card['parent_id'] is not None for card in (a, b)), 1)

    def test_registered_descendants_inherit_the_column_independent_of_card_order(self):
        registry = {'root': {'id': 'root', 'kind': 'codex', 'project': 'Alpha', 'role': 'orchestrator'},
                    'child': {'id': 'child', 'kind': 'codex', 'parent_id': 'root'},
                    'leaf': {'id': 'leaf', 'kind': 'codex', 'parent_id': 'child'}}
        for order in (('leaf', 'child', 'root'), ('root', 'child', 'leaf')):
            with self.subTest(order=order):
                cards = [self.card('codex:' + key, 'codex', thread=key) for key in order]
                apply_registry(cards, registry)
                self.assertEqual([card['project'] for card in cards], ['Alpha'] * 3)

    def test_kind_must_agree_and_unresolved_parent_keeps_heuristics(self):
        parent = self.card('claude:other', 'claude')
        claude = self.card('claude:x', 'claude', parent_id='claude:other', link='inferred')
        codex = self.card('codex:x', 'codex', thread='x')
        codex['started'] = 1700000100
        apply_registry([parent, claude, codex], {'x': {'id': 'x', 'kind': 'claude', 'role': 'worker', 'parent_id': 'gone',
                                               'started_at': 1700000000.0}}, mark_guessed=True)
        self.assertTrue(claude['registered'])
        self.assertEqual(claude['parent_id'], parent['id'])
        self.assertEqual(claude['link'], 'inferred')
        self.assertTrue(codex['guessed'])
        self.assertNotIn('registered', codex)

    def test_check_update_explains_a_newly_registered_card(self):
        before = {'c': {'project': 'guess', 'status': 'working', 'parent_id': None}}
        after = {'c': {'project': 'Alpha', 'status': 'working', 'parent_id': None, 'registered': True}}
        self.assertEqual(reason_for_change('c', before, after), 'now placed by the agents registry')

    def test_registration_does_not_explain_a_workers_status_regression(self):
        before = {'c': {'project': 'Alpha', 'status': 'working', 'completed': False}}
        after = {'c': {'project': 'Alpha', 'status': 'done', 'completed': True, 'registered': True}}
        self.assertIsNone(reason_for_change('c', before, after))

    def test_guessed_only_from_registry_coverage_start(self):
        old = self.card('codex:old', 'codex', thread='old', started=1699999999)
        new = self.card('codex:new', 'codex', thread='new', started=1700000001)
        registry = {'r': {'id': 'r', 'kind': 'codex', 'started_at': 1700000000.0, 'predecessor_id': 'p'}}
        apply_registry([old, new], registry, mark_guessed=True)
        self.assertNotIn('guessed', old)
        self.assertTrue(new['guessed'])
        undated = self.card('codex:u', 'codex', thread='u', started=1700000001)
        apply_registry([undated], {'r': {'id': 'r', 'kind': 'codex'}}, mark_guessed=True)
        self.assertNotIn('guessed', undated)

    def test_subagents_are_not_guessed_but_unregistered_sessions_still_are(self):
        parent = self.card('claude:parent', 'claude', started=1700000000)
        subagent = self.card('claude:parent:agent-child', 'claude', subagent_id='child',
                             parent_id=parent['id'], link='exact', started=1700000001)
        session = self.card('claude:session', 'claude', parent_id=parent['id'],
                            link='exact', started=1700000001)
        registry = {'parent': {'id': 'parent', 'kind': 'claude', 'started_at': 1700000000}}
        apply_registry([parent, subagent, session], registry, mark_guessed=True)
        self.assertNotIn('guessed', subagent)
        self.assertNotIn('registered', subagent)
        self.assertEqual((subagent['parent_id'], subagent['link']), (parent['id'], 'exact'))
        self.assertTrue(session['guessed'])

    def test_null_name_and_role_keep_heuristics(self):
        card = self.card('codex:n', 'codex', thread='n', task='Heuristic title', orchestrator=True)
        apply_registry([card], {'n': {'id': 'n', 'kind': 'codex', 'name': None, 'role': None}})
        self.assertTrue(card['registered'])
        self.assertEqual((card['task'], card['orchestrator']), ('Heuristic title', True))

    def test_no_marks_without_the_flag(self):
        card = self.card('codex:x', 'codex', thread='x')
        apply_registry([card], {})
        self.assertNotIn('guessed', card)


COLUMNS = ['Atlas', 'Northstar', 'Northstar App', 'Beacon', 'Meridian', 'Murmuratio', 'Harbor']
# Anonymised copies of real schema v1 record shapes (task-ledger docs/agents-registry.md).
ORCH_005 = {'id': '0a000000-0000-7000-8000-000000000005', 'kind': 'codex', 'project': 'atlas',
            'role': 'orchestrator', 'name': 'Atlas orchestrator 005 CODEX', 'parent_id': None,
            'cwd': '/synthetic/atlas', 'started_at': 1794300000, 'v': 1, 'via': 'codex-orch-spawn'}
ORCH_006 = {'id': '0a000000-0000-7000-8000-000000000006', 'kind': 'codex', 'project': 'atlas',
            'role': 'orchestrator', 'name': 'Atlas orchestrator 006 CODEX', 'parent_id': ORCH_005['id'],
            'cwd': '/synthetic/atlas', 'started_at': 1794318320, 'v': 1, 'via': 'codex-app-native-handoff'}
APP_CHAT = {'id': '11111111-2222-4333-8444-555555555555', 'kind': 'claude', 'project': 'northstar-app',
            'role': 'orchestrator', 'name': 'Northstar App orchestrator 002 CLAUDE', 'parent_id': None,
            'cwd': '/synthetic/northstar', 'started_at': 1794300000, 'app_id': 'local_aaaa-bbbb', 'v': 1,
            'via': 'session-start'}
WORKER = {'id': '0a000000-0000-7000-8000-0000000000aa', 'kind': 'codex', 'project': 'northstar-app',
          'role': 'worker', 'name': 'Fix the list', 'parent_id': 'local_aaaa-bbbb', 'cwd': '/synthetic/northstar',
          'started_at': 1794300100, 'v': 1, 'via': 'codex-task',
          'run': '/synthetic/.claude/tools/codex-runs/codex-task.1.json'}


class RegistryV1Tests(unittest.TestCase):
    def card(self, card_id, kind, **fields):
        return {'id': card_id, 'kind': kind, 'project': 'guess', 'task': 'guess', 'status': 'working',
                'orchestrator': False, 'parent_id': None, **fields}

    def records(self, *raw):
        return {record['id']: record for record in map(parse_record, raw)}

    def test_project_stem_maps_to_the_configured_column(self):
        self.assertEqual(registry_column('northstar-app', COLUMNS), 'Northstar App')
        self.assertEqual(registry_column('northstar_app', COLUMNS), 'Northstar App')
        self.assertEqual(registry_column('atlas', COLUMNS), 'Atlas')
        self.assertEqual(registry_column('harbor', COLUMNS), 'Harbor')
        self.assertEqual(registry_column('new-thing', COLUMNS), 'New Thing')

    def test_v1_optional_fields_are_accepted(self):
        record = parse_record(WORKER)
        self.assertEqual(record['parent_id'], 'local_aaaa-bbbb')
        self.assertNotIn('via', record)
        self.assertNotIn('run', record)
        self.assertEqual(parse_record(APP_CHAT)['app_id'], 'local_aaaa-bbbb')

    def test_handoff_orchestrator_is_a_root_that_follows_its_predecessor(self):
        old = self.card('codex:t5', 'codex', thread=ORCH_005['id'])
        new = self.card('codex:t6', 'codex', thread=ORCH_006['id'], parent_id=old['id'], link='inferred')
        apply_registry([old, new], self.records(ORCH_005, ORCH_006), columns=COLUMNS)
        for card in (old, new):
            self.assertEqual((card['project'], card['orchestrator'], card['parent_id']), ('Atlas', True, None))
            self.assertNotIn('link', card)
        self.assertEqual(new['follows'], 'Atlas orchestrator 005 CODEX')
        self.assertNotIn('follows', old)

    def test_follows_uses_the_registry_name_when_the_predecessor_is_off_the_board(self):
        new = self.card('codex:t6', 'codex', thread=ORCH_006['id'])
        apply_registry([new], self.records(ORCH_005, ORCH_006), columns=COLUMNS)
        self.assertEqual(new['follows'], 'Atlas orchestrator 005 CODEX')
        apply_registry([new], self.records(ORCH_006), columns=COLUMNS)
        self.assertNotIn('follows', new)
        self.assertIsNone(new['parent_id'])

    def test_local_parent_resolves_through_registry_app_id(self):
        # The app chat card is known only by its CLI id: the worker's local_ parent finds it via app_id.
        chat = self.card('claude:' + APP_CHAT['id'], 'claude')
        worker = self.card('codex:run', 'codex', thread=WORKER['id'])
        apply_registry([chat, worker], self.records(APP_CHAT, WORKER), columns=COLUMNS)
        self.assertEqual((chat['project'], chat['orchestrator']), ('Northstar App', True))
        self.assertEqual((worker['project'], worker['parent_id'], worker['link']),
                         ('Northstar App', chat['id'], 'exact'))

    def test_local_parent_resolves_through_the_board_app_session(self):
        chat = self.card('claude:other-cli', 'claude', app_session='local_aaaa-bbbb')
        worker = self.card('codex:run', 'codex', thread=WORKER['id'])
        apply_registry([chat, worker], self.records(WORKER), columns=COLUMNS)
        self.assertEqual(worker['parent_id'], chat['id'])

    def test_card_matches_a_record_by_its_app_id(self):
        chat = self.card('claude:renamed-cli', 'claude', app_session='local_aaaa-bbbb')
        apply_registry([chat], self.records(APP_CHAT), columns=COLUMNS)
        self.assertTrue(chat['registered'])
        self.assertEqual(chat['task'], 'Northstar App orchestrator 002 CLAUDE')

    def test_no_duplicate_column_for_a_heuristic_column(self):
        guessed = self.card('codex:g', 'codex', thread='g', project='Murmuratio')
        registered = self.card('codex:r', 'codex', thread='r')
        apply_registry([guessed, registered], self.records(
            {'id': 'r', 'kind': 'codex', 'project': 'murmuratio', 'role': 'worker'}))
        self.assertEqual(registered['project'], 'Murmuratio')


class RegistryBoardTests(BoardHTTPHarness):
    """Fake launches through every wrapper path, read back through /api/now."""

    def setUp(self):
        super().setUp()
        self.runs = self.home / '.local/share/murmuratio/codex-runs'
        self.runs.mkdir(parents=True)
        self.config.modules['codex_wrapper'] = True
        self.config.wrapper_runs = self.runs
        self.registry = self.home / '.claude/tools/agents-registry.jsonl'

    def register(self, **record):
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        with self.registry.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'cwd': '/synthetic/project', 'started_at': self.now - 60, **record}) + '\n')

    def wrapper_run(self, stem, **record):
        path = self.runs / (stem + '.json')
        path.write_text(json.dumps({'status': 'running', 'mode': 'new', 'cwd': '/synthetic/project',
                                    'started': self.now - 60, **record}))
        os.utime(path, (self.now, self.now))

    def launches(self):
        # Claude Code desktop session: the Alpha orchestrator.
        self.desktop('alpha-orch', title='Some desktop title')
        self.claude('alpha-orch', [self.message('user', content='Coordinate Alpha')])
        self.register(id='alpha-orch', kind='claude', project='Alpha', role='orchestrator',
                      name='Alpha orchestrator', parent_id=None)
        # Codex app chat: an interactive chat the heuristics would hide, registered as Beta's orchestrator.
        self.rollout('beta-orch', source='vscode')
        self.register(id='beta-orch', kind='codex', project='Beta', role='orchestrator', name='Beta orchestrator')
        # codex-task: a wrapper run and its rollout, launched by the Alpha orchestrator.
        self.wrapper_run('codex-task.1', thread='t-codex', title='Wrapper title')
        self.rollout('t-codex')
        self.register(id='t-codex', kind='codex', project='Alpha', role='worker', name='Fix the parser',
                      parent_id='alpha-orch')
        # claude-task: a Claude worker started from the Beta Codex thread.
        self.wrapper_run('claude-task.1', tool='claude', session_id='claude-worker', title='Claude: task',
                         parent_session='beta-orch')
        self.claude('claude-worker', [self.message('user', content='Review the docs')])
        self.register(id='claude-worker', kind='claude', project='Beta', role='worker', name='Review the docs',
                      parent_id='beta-orch')
        # atlas-task: a Atlas worker under the Alpha orchestrator, in its own column, no name given.
        self.wrapper_run('atlas-task.1', thread='t-space', title='Atlas: Task: ATL-0001')
        self.rollout('t-space')
        self.register(id='t-space', kind='codex', project='Atlas', role='worker', parent_id='alpha-orch')
        # Not launched through a wrapper: heuristics only.
        self.rollout('stray')

    def test_registered_cards_take_column_role_parent_and_title(self):
        self.launches()
        self.config.mark_guessed = True
        self.start_server()
        cards = self.cards()
        def identity(card_id):
            card = cards[card_id]
            return card['project'], bool(card.get('orchestrator')), card.get('parent_id'), card['task']
        self.assertEqual(identity('claude:alpha-orch'), ('Alpha', True, None, 'Some desktop title'))
        self.assertEqual(cards['claude:alpha-orch']['title_source'], 'desktop')
        self.assertEqual(cards['codex:codex-task.1']['title_source'], 'registry')
        self.assertEqual(identity('codex:beta-orch'), ('Beta', True, None, 'Beta orchestrator'))
        self.assertEqual(identity('codex:codex-task.1'), ('Alpha', False, 'claude:alpha-orch', 'Fix the parser'))
        self.assertEqual(identity('claude:claude-worker'), ('Beta', False, 'codex:beta-orch', 'Review the docs'))
        self.assertEqual(identity('codex:atlas-task.1'),
                         ('Atlas', False, 'claude:alpha-orch', 'Atlas: Task: ATL-0001'))
        self.assertNotIn('codex:claude-task.1', cards)  # merged into its Claude session card
        for card_id in ('claude:alpha-orch', 'codex:beta-orch', 'codex:codex-task.1',
                        'claude:claude-worker', 'codex:atlas-task.1'):
            self.assertTrue(cards[card_id].get('registered'), card_id)
            self.assertNotIn('guessed', cards[card_id])
        stray = cards['codex:stray']
        self.assertTrue(stray['guessed'])
        self.assertEqual(stray['project'], 'project')
        self.assertNotIn('agents_registry', self.api().get('sources', {}))

    def test_guessed_mark_is_off_by_default(self):
        self.launches()
        cards = self.cards()
        self.assertFalse(any('guessed' in card for card in cards.values()))
        self.assertEqual(cards['codex:codex-task.1']['project'], 'Alpha')

    def test_null_registry_parent_keeps_the_desktop_parent_on_the_board(self):
        self.desktop('parent', title='Alpha orchestrator')
        self.claude('parent', [self.message('user', content='Coordinate Alpha')])
        self.desktop('worker', title='Review the parser', spawnedFrom={'sessionId': 'local_parent'})
        self.claude('worker', [self.message('user', content='Review the parser')])
        self.register(id='worker', kind='claude', role=None, parent_id=None, via='session-start')

        cards = self.cards()
        self.assertTrue(cards['claude:parent']['orchestrator'])
        self.assertTrue(cards['claude:worker']['registered'])
        self.assertFalse(cards['claude:worker']['orchestrator'])
        self.assertEqual(cards['claude:worker']['parent_id'], 'claude:parent')

    def test_resolvable_registry_parent_overrides_the_desktop_parent(self):
        for name in ('parent', 'override', 'worker'):
            self.desktop(name, spawnedFrom={'sessionId': 'local_parent'} if name == 'worker' else {})
            self.claude(name, [self.message('user', content='Work on Alpha')])
        self.register(id='worker', kind='claude', role=None, parent_id='override', via='session-start')

        cards = self.cards()
        self.assertIn('claude:parent', cards)
        self.assertIn('claude:override', cards)
        self.assertEqual(cards['claude:worker']['parent_id'], 'claude:override')
        self.assertEqual(cards['claude:worker']['link'], 'exact')

    def test_absent_registry_keeps_heuristics(self):
        self.rollout('stray')
        state = self.api()
        self.assertEqual(state['sources']['agents_registry'], {'status': 'absent'})
        card = {a['id']: a for a in state['agents']}['codex:stray']
        self.assertEqual(card['project'], 'project')
        self.assertNotIn('registered', card)

    def test_malformed_lines_are_counted_without_failing_the_source(self):
        self.rollout('t-codex')
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text('{broken\n')
        self.register(id='t-codex', kind='codex', project='Alpha', role='worker')
        state = self.api()
        self.assertEqual(state['sources']['agents_registry'], {'status': 'available', 'malformed': 1})
        self.assertEqual({a['id']: a for a in state['agents']}['codex:t-codex']['project'], 'Alpha')


if __name__ == '__main__':
    unittest.main()
