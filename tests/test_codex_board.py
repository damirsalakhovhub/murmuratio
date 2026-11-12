"""Default Codex board discovery and conservative Claude attribution."""
import json
import errno
import http.client
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from murmuratio_app.adapters.codex_board import rollout_writers
from murmuratio_app.adapters.codex_sessions import CodexSource
from tests.board_fixture_helpers import fixture_parent
from murmuratio_app.config import Config
from murmuratio_app.discovery import Discovery
from murmuratio_app.now import NowSource


class CodexBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = time.time()
        self.cwd = str(self.home / 'project')
        self.source = NowSource(self.home, clock=lambda: self.now, alive=lambda pid: pid == 7, config=Config())

    def write(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            for record in records:
                stream.write(json.dumps(record) + '\n')
        os.utime(path, (self.now, self.now))
        return path

    def event(self, kind, at, **payload):
        return {'type': 'event_msg', 'timestamp': at, 'payload': {'type': kind, **payload}}

    def rollout(self, thread, *, source='exec', cwd=None, started=None, finished=False, run_id=None, metadata=None):
        started = started if started is not None else self.now - 58
        cwd = cwd or self.cwd
        path = self.home / '.codex/sessions/2026/11/07' / f'rollout-test-{thread}.jsonl'
        records = [
            {'type': 'session_meta', 'timestamp': started, 'payload': {
                'id': thread, 'timestamp': started, 'cwd': cwd, 'source': source, 'run_id': run_id,
                **(metadata or {})}},
            self.event('task_started', started + 1, model_context_window=200000),
            {'type': 'turn_context', 'timestamp': started + 1, 'payload': {'model': 'gpt-6.1-sol', 'cwd': cwd}},
            {'type': 'response_item', 'timestamp': started + 2, 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'Build the board'}]}},
            self.event('token_count', started + 3, info={'model_context_window': 200000,
                'last_token_usage': {'input_tokens': 42000}}),
        ]
        if finished:
            records.append(self.event('task_complete', started + 4))
        return self.write(path, records)

    def claude(self, *, name='parent', command=None, cwd=None, at=None, output=None, tool_id='tool-1'):
        cwd = cwd or self.cwd
        at = at if at is not None else self.now - 60
        path = self.home / '.claude/projects/project' / f'{name}.jsonl'
        records = [{'type': 'user', 'cwd': cwd, 'timestamp': at - 2,
                    'message': {'content': 'Coordinate work'}}]
        if command is not None:
            records.append({'type': 'assistant', 'cwd': cwd, 'timestamp': at,
                'message': {'stop_reason': 'tool_use', 'content': [
                    {'type': 'tool_use', 'name': 'Bash', 'id': tool_id, 'input': {'command': command}}]}})
            if output is not None:
                records.append({'type': 'user', 'timestamp': at + 1, 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': tool_id, 'content': [{'type': 'text', 'text': output}]}]}})
        records.append({'type': 'assistant', 'timestamp': at + 2,
                        'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-5-5', 'content': 'Ready'}})
        return self.write(path, records)

    def agents(self):
        state = self.source.state()
        return {agent['id']: agent for agent in state['agents'] + state['completed']}

    def test_default_codex_only_and_missing_sqlite(self):
        self.rollout('interactive', source='vscode', finished=True)
        self.rollout('finished', finished=True)
        self.rollout('running')
        self.source.codex = CodexSource(self.home / '.codex')
        agents = self.agents()
        self.assertEqual(set(agents), {'codex:finished', 'codex:running'})
        self.assertEqual(agents['codex:finished']['status'], 'done')
        running = agents['codex:running']
        self.assertEqual((running['model'], running['task'], running['cwd']), ('gpt-6.1-sol', 'Build the board', self.cwd))
        self.assertEqual((running['context_used'], running['context_window']), (42000, 200000))
        self.assertIsNone(running['parent_id'])
        self.assertFalse((self.home / '.claude').exists())

    def launch_metadata(self, name):
        path = Path(__file__).parent / 'fixtures/adapters/codex-launch-metadata.json'
        return json.loads(path.read_text())[name]

    def test_launch_metadata_separates_workers_and_running_chats_without_claude(self):
        for name in ('exec', 'exec_originator', 'desktop', 'cli', 'desktop_originator', 'subagent'):
            self.rollout(name, source=None, metadata=self.launch_metadata(name))
        self.rollout('waiting-chat', finished=True, metadata=self.launch_metadata('desktop'))
        state = self.source.state()
        self.assertEqual({a['id'] for a in state['agents']},
                         {'codex:exec', 'codex:exec_originator', 'codex:subagent'})
        raw = {a['id']: a for a in self.source.codex_board_adapter.read(self.now).values()}
        for name in ('exec', 'exec_originator', 'subagent'):
            self.assertEqual(raw['codex:' + name]['codex_role'], 'worker')
        self.assertTrue(all(a['codex_role'] == 'worker' for a in state['agents']))
        self.assertFalse((self.home / '.claude').exists())

    def test_desktop_threads_opened_by_an_agent_are_workers(self):
        # A Codex Desktop chat persona starting workers: same source and originator as a
        # person's chat, only session_meta.thread_source differs (real 0.167 metadata, sanitized).
        for name in ('desktop_agent_created', 'desktop_agent_forked', 'desktop_user'):
            self.rollout(name, source=None, metadata=self.launch_metadata(name))
        self.rollout('agent-done', source=None, finished=True, metadata=self.launch_metadata('desktop_agent_created'))
        state = self.source.state()
        self.assertEqual({a['id'] for a in state['agents']},
                         {'codex:desktop_agent_created', 'codex:desktop_agent_forked'})
        raw = {a['id']: a for a in self.source.codex_board_adapter.read(self.now).values()}
        for name in ('desktop_agent_created', 'desktop_agent_forked', 'agent-done'):
            self.assertEqual(raw['codex:' + name]['codex_role'], 'worker')
        self.assertEqual({a['id'] for a in state['completed']}, {'codex:agent-done'})
        self.assertTrue(all(a['codex_role'] == 'worker' for a in state['agents'] + state['completed']))

    def test_interactive_metadata_with_claude_link_is_a_worker(self):
        self.claude(command='codex exec task', output='thread_id: linked')
        self.rollout('linked', metadata=self.launch_metadata('desktop'))
        agent = self.agents()['codex:linked']
        self.assertEqual((agent['parent_id'], agent['codex_role']), ('claude:parent', 'worker'))
        self.write(self.home / '.codex/sessions/2026/11/07/rollout-test-linked.jsonl',
                   [self.event('task_complete', self.now - 1)])
        agent = self.agents()['codex:linked']
        self.assertEqual((agent['status'], agent['completed'], agent['ended']), ('done', True, self.now - 1))

    def test_recorded_parent_or_wrapper_identifies_worker_even_without_claude(self):
        self.rollout('parent-meta', finished=True, metadata={**self.launch_metadata('desktop'), 'parent_session': 'absent'})
        self.rollout('wrapped', metadata=self.launch_metadata('desktop'))
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'running', 'thread': 'wrapped', 'cwd': self.cwd,
                                    'started': self.now - 60, 'pid': 7}))
        agents = self.agents()
        self.assertEqual(set(agents), {'codex:parent-meta', 'codex:run'})
        self.assertTrue(all(a['codex_role'] == 'worker' for a in agents.values()))
        self.assertEqual(agents['codex:parent-meta']['status'], 'done')

    def codex_orchestrators(self):
        """Real 2026-11-10 case, anonymised: Codex app orchestrators and a worker one of them dispatched."""
        path = self.home / '.config/murmuratio/config.toml'
        path.parent.mkdir(parents=True)
        path.write_text('[modules]\ncodex_wrapper = true\n[projects]\norder = ["Atlas", "Beacon", "Harbor"]\n'
                        'fallback = "Harbor"\n[[projects.mappings]]\npath = "~/godot/Atlas"\nname = "Atlas"\n'
                        'worktrees = true\n[[projects.mappings]]\npath = "~/beacon-app"\nname = "Beacon"\n'
                        '[[projects.mappings]]\npath = "~/.claude"\nname = "Harbor"\nafter_inherit = true\n')
        self.source.config = Config.load(self.home)
        database = self.home / '.codex/state_5.sqlite'
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database)
        connection.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, source TEXT)')
        connection.executemany('INSERT INTO threads VALUES (?, ?, ?)', [
            ('space-orch', 'Atlas orchestrator 005 CODEX', 'vscode'),
            ('beacon-orch', 'Beacon orchestrator 002 CODEX', 'vscode'),
            ('plain-chat', None, 'vscode'), ('env-worker', None, 'exec')])
        connection.commit()
        connection.close()
        dev, environment = (str(self.home / 'godot/Atlas-worktrees' / name) for name in ('dev', 'environment'))
        for coordinator in ('space-orch', 'beacon-orch'):
            fixture_parent(self.home, coordinator, kind='codex', role='orchestrator')
        app = {'originator': 'Codex Desktop', 'source': 'vscode'}
        # Opened by its predecessor's handoff: the app records it like an agent-created worker.
        self.rollout('space-orch', source='vscode', cwd=dev, started=self.now - 120,
                     metadata={**app, 'thread_source': 'agent_created_thread'})
        # Its open turn launches the worker and reads the launcher's output.
        self.write(self.home / '.codex/sessions/2026/11/07/rollout-test-space-orch.jsonl', [
            {'type': 'response_item', 'timestamp': self.now - 60, 'payload': {'type': 'custom_tool_call', 'name': 'exec'}},
            {'type': 'response_item', 'timestamp': self.now - 59, 'payload': {'type': 'custom_tool_call_output'}}])
        # Opened by the orchestrator tool: an unknown originator on an app source.
        self.rollout('beacon-orch', source='vscode', cwd=str(self.home / 'beacon-app'), finished=True,
                     started=self.now - 3000, metadata={'originator': 'codex-orch', 'source': 'vscode'})
        self.rollout('plain-chat', source='vscode', cwd=str(self.home / 'notes'), finished=True,
                     started=self.now - 3000, metadata={**app, 'thread_source': 'user'})
        self.rollout('env-worker', cwd=environment, started=self.now - 59,
                     metadata={'originator': 'Codex Desktop', 'source': 'exec', 'thread_source': 'user'})
        # The Codex app inherited a Claude chat's environment, so the wrapper names that chat.
        self.claude(name='harbor-orch', cwd=str(self.home / '.claude/tools'), at=self.now - 3000)
        runs = self.home / '.local/share/murmuratio/codex-runs'
        runs.mkdir(parents=True)
        (runs / 'codex-task.env.json').write_text(json.dumps({
            'status': 'running', 'title': 'Task: ATL-0034', 'mode': 'new', 'target': environment,
            'thread': 'env-worker', 'tier': 'luna', 'model': 'gpt-6-luna', 'started': self.now - 60, 'pid': 7,
            'parent_session': 'harbor-orch', 'parent_app_session': 'local_app', 'from_cwd': dev}))
        return runs, dev

    def test_codex_app_orchestrators_are_project_orchestrators_with_their_workers(self):
        self.codex_orchestrators()
        state = self.source.state()
        agents = {agent['id']: agent for agent in state['agents'] + state['completed']}
        for thread, project, name in (('space-orch', 'Atlas', 'Atlas orchestrator 005 CODEX'),
                                      ('beacon-orch', 'Beacon', 'Beacon orchestrator 002 CODEX')):
            agent = agents['codex:' + thread]
            self.assertEqual((agent['orchestrator'], agent['codex_role'], agent['project'], agent['task'],
                              agent['parent_id'], agent['completed']), (True, 'orchestrator', project, name, None, False))
        self.assertEqual(agents['codex:space-orch']['status'], 'working')
        self.assertEqual(agents['codex:beacon-orch']['status'], 'waiting')
        worker = agents['codex:codex-task.env']
        self.assertEqual((worker['parent_id'], worker['project'], worker['codex_role'], worker['orchestrator']),
                         ('codex:space-orch', 'Atlas', 'worker', False))
        self.assertNotIn('codex:plain-chat', agents)
        self.assertNotIn('dev', {agent['project'] for agent in agents.values()})

    def test_wrapper_keeps_its_claude_parent_when_launched_from_its_folder(self):
        runs, dev = self.codex_orchestrators()
        tools = str(self.home / '.claude/tools')
        run = json.loads((runs / 'codex-task.env.json').read_text())
        (runs / 'codex-task.env.json').write_text(json.dumps({**run, 'from_cwd': tools}))
        worker = self.agents()['codex:codex-task.env']
        self.assertEqual((worker['parent_id'], worker['project']), ('claude:harbor-orch', 'Harbor'))

    def test_wrapper_keeps_its_claude_parent_when_no_codex_chat_was_open_there(self):
        runs, dev = self.codex_orchestrators()
        run = json.loads((runs / 'codex-task.env.json').read_text())
        (runs / 'codex-task.env.json').write_text(json.dumps({**run, 'from_cwd': str(self.home / 'elsewhere')}))
        self.assertEqual(self.agents()['codex:codex-task.env']['parent_id'], 'claude:harbor-orch')

    def test_thread_name_from_session_index_titles_the_orchestrator_card(self):
        """Real 2026-11-10 case, anonymised: a handed-off Codex orchestrator named by thread/name/set.

        Its last user message must not become the title, and its launch folder is not its role."""
        self.codex_orchestrators()
        (self.home / '.codex/state_5.sqlite').unlink()
        index = self.home / '.codex/session_index.jsonl'
        index.write_text(''.join(json.dumps(record) + '\n' for record in (
            {'id': 'space-orch', 'thread_name': 'Atlas orchestrator 004 CODEX', 'updated_at': '2026-11-10T12:15:34Z'},
            {'id': 'env-worker', 'thread_name': 'Environment pass', 'updated_at': '2026-11-10T12:15:40Z'},
            {'id': 'space-orch', 'thread_name': 'Atlas orchestrator 005 CODEX', 'updated_at': '2026-11-10T12:15:34Z'})))
        self.write(self.home / '.codex/sessions/2026/11/07/rollout-test-space-orch.jsonl', [
            {'type': 'response_item', 'timestamp': self.now - 30, 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'merge the undo and redo buttons and put them in the bar'}]}}])
        agents = self.agents()
        orchestrator = agents['codex:space-orch']
        self.assertEqual((orchestrator['task'], orchestrator['codex_role'], orchestrator['orchestrator'],
                          orchestrator['project'], orchestrator['parent_id']),
                         ('Atlas orchestrator 005 CODEX', 'orchestrator', True, 'Atlas', None))
        # A thread title names a worker too, without making it an orchestrator.
        worker = agents['codex:codex-task.env']
        self.assertEqual((worker['task'], worker['codex_role']), ('Environment pass', 'worker'))
        self.rollout('named-worker', cwd=str(self.home / 'godot/Atlas'))
        with index.open('a') as stream:
            stream.write(json.dumps({'id': 'named-worker', 'thread_name': 'Sun path readability'}) + '\n')
        self.assertEqual(self.agents()['codex:named-worker']['task'], 'Sun path readability')

    def test_state_database_name_wins_over_session_index(self):
        self.codex_orchestrators()
        (self.home / '.codex/session_index.jsonl').write_text(
            json.dumps({'id': 'space-orch', 'thread_name': 'old title'}) + '\n')
        self.assertEqual(self.agents()['codex:space-orch']['task'], 'Atlas orchestrator 005 CODEX')

    def claude_task(self, *, session_id=None, transcript=True, prompt=None):
        """Real 2026-11-10 case, anonymised: the Codex orchestrator ran claude-task for an Opus consult."""
        runs, dev = self.codex_orchestrators()
        environment = str(self.home / 'godot/Atlas-worktrees/environment')
        events = self.home / '.claude/tools/codex-orchestrator/claude-runs/run-1/events.jsonl'
        self.write(events, [
            {'type': 'system', 'subtype': 'hook_started', 'session_id': 'consult-session'},
            {'type': 'system', 'subtype': 'init', 'cwd': environment, 'session_id': 'consult-session'}])
        record = {'status': 'done', 'tool': 'claude', 'title': 'Claude: Task: ATL-0040', 'mode': 'audit',
                  'target': environment, 'cwd': environment, 'tier': 'claude-opus-5-5',
                  'model': 'claude-claude-opus-5-5', 'events': str(events), 'started': self.now - 80, 'pid': 99,
                  'task_id': 'ATL-0040', 'parent_session': 'space-orch', 'from_cwd': environment,
                  'ended': self.now - 2, 'exit': 0}
        if session_id:
            record['session_id'] = session_id
        (runs / 'claude-task.run-1.json').write_text(json.dumps(record))
        if transcript:
            rover = environment + '/scripts/rover-godot'
            self.write(self.home / '.claude/projects/environment/consult-session.jsonl', [
                {'type': 'custom-title', 'customTitle': 'Atlas environment: Task: ATL-0040'},
                {'type': 'user', 'cwd': environment, 'timestamp': self.now - 78, 'message': {'content': prompt or
                    '<stall-guard-worker/>\nRead-only review for the orchestrator. Do not write, commit or push.\n'
                    'You are a Atlas direction worker.'}},
                {'type': 'assistant', 'cwd': rover, 'timestamp': self.now - 2, 'message': {
                    'stop_reason': 'end_turn', 'model': 'claude-opus-5-5', 'content': 'Both panels can share one style.'}}])

    def test_claude_task_session_nests_under_the_codex_thread_that_ran_it(self):
        for session_id in (None, 'consult-session'):
            with self.subTest(recorded=session_id):
                self.setUp()
                self.claude_task(session_id=session_id)
                agents = self.agents()
                worker = agents['claude:consult-session']
                self.assertEqual((worker['parent_id'], worker['project'], worker['orchestrator']),
                                 ('codex:space-orch', 'Atlas', False))
                self.assertEqual(worker['task'], 'Read-only review for the orchestrator.')
                # The run and its session are one worker: no second card for the run.
                self.assertNotIn('codex:claude-task.run-1', agents)
                self.assertTrue(agents['codex:space-orch']['orchestrator'])

    def test_current_claude_task_prompt_titles_by_its_session_name(self):
        # Current launcher: line 1 is the session name, line 2 the worker marker carrying the parent.
        self.claude_task(session_id='consult-session', prompt=(
            'Atlas design consult: Panel style\n<stall-guard-worker parent="space-orch"/>\nRead-only review.'))
        agents = self.agents()
        worker = agents['claude:consult-session']
        self.assertEqual((worker['task'], worker['parent_id']), ('Atlas design consult: Panel style', 'codex:space-orch'))
        self.assertNotIn('codex:claude-task.run-1', agents)

    def test_claude_task_run_without_its_transcript_still_nests_under_the_codex_thread(self):
        self.claude_task(transcript=False)
        run = self.agents()['codex:claude-task.run-1']
        self.assertEqual((run['parent_id'], run['project'], run['task']),
                         ('codex:space-orch', 'Atlas', 'ATL-0040'))

    def test_finished_subagent_uses_worker_completion(self):
        self.rollout('child', finished=True, metadata=self.launch_metadata('subagent'))
        self.assertEqual(self.agents()['codex:child']['status'], 'done')

    def test_finished_rollout_expires_by_end_time_even_with_recent_mtime(self):
        self.rollout('finished', finished=True, started=self.now - 86400)
        self.assertIn('codex:finished', self.agents())
        self.now += 4
        self.assertNotIn('codex:finished', self.agents())

    def test_claude_only_and_neither_are_read_only(self):
        self.assertEqual(self.agents(), {})
        self.assertEqual(list(self.home.iterdir()), [])
        self.claude()
        self.assertEqual(self.agents(), {})

    def test_exact_thread_from_bash_output_beats_cwd_and_time(self):
        self.claude(command='codex exec --json "task"', output='{"thread_id":"thread-exact"}')
        self.rollout('thread-exact', cwd=str(self.home / 'other'), started=self.now - 300)
        agent = self.agents()['codex:thread-exact']
        self.assertEqual((agent['parent_id'], agent['link']), ('claude:parent', 'exact'))

    def test_unique_background_launch_uses_cwd_and_time(self):
        self.claude(command=f'codex --config feature=true exec -C {self.cwd} --json "task" &', cwd=str(self.home), output='Running in background')
        self.rollout('thread-inferred')
        agent = self.agents()['codex:thread-inferred']
        self.assertEqual((agent['parent_id'], agent['link']), ('claude:parent', 'inferred'))

    def test_ambiguous_launches_or_sessions_remain_standalone(self):
        self.claude(command='codex exec task')
        self.rollout('first')
        second = self.rollout('second')
        for agent in (self.agents()['codex:first'], self.agents()['codex:second']):
            self.assertIsNone(agent['parent_id'])
            self.assertNotIn('link', agent)
        second.unlink()
        self.claude(name='other', command='codex exec task')
        self.assertIsNone(self.agents()['codex:first']['parent_id'])

    def test_conflicting_recorded_parents_are_not_guessed_by_time(self):
        self.claude(name='first', command='codex exec task', output='thread_id: same-thread')
        self.claude(name='other', command='codex exec task', output='thread_id: same-thread', at=self.now - 500)
        self.rollout('same-thread')
        agent = self.agents()['codex:same-thread']
        self.assertIsNone(agent['parent_id'])
        self.assertNotIn('link', agent)

    def test_resumed_thread_uses_latest_turn_and_incremental_updates(self):
        path = self.rollout('resumed', finished=True, started=self.now - 500)
        self.assertEqual(self.agents()['codex:resumed']['status'], 'done')
        self.claude(command='codex exec resume resumed')
        self.write(path, [self.event('task_started', self.now - 58),
            {'type': 'turn_context', 'timestamp': self.now - 58, 'payload': {'model': 'gpt-6-astra', 'cwd': self.cwd}},
            {'type': 'response_item', 'timestamp': self.now - 57, 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'Continue the work'}]}},
            self.event('token_count', self.now - 56, info={'model_context_window': 400000,
                'last_token_usage': {'input_tokens': 150000}})])
        resumed = self.agents()['codex:resumed']
        self.assertEqual((resumed['status'], resumed['model'], resumed['task']), ('working', 'gpt-6-astra', 'Continue the work'))
        self.assertEqual((resumed['context_used'], resumed['context_window']), (150000, 400000))
        self.assertEqual((resumed['parent_id'], resumed['link']), ('claude:parent', 'inferred'))
        self.write(path, [self.event('task_complete', self.now - 50)])
        self.assertEqual(self.agents()['codex:resumed']['status'], 'done')

    def test_non_user_message_updates_last_activity_without_changing_task(self):
        path = self.rollout('activity')
        self.write(path, [{'type': 'response_item', 'timestamp': self.now - 5,
            'payload': {'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': 'Progress'}]}}])
        agent = self.agents()['codex:activity']
        self.assertEqual(agent['last_at'], self.now - 5)
        self.assertEqual(agent['task'], 'Build the board')

    def test_partial_append_retries_and_truncation_resets(self):
        path = self.rollout('partial', finished=True)
        partial = json.dumps(self.event('task_started', self.now - 10))
        with path.open('a') as stream:
            stream.write(partial[:len(partial)//2])
        self.assertEqual(self.agents()['codex:partial']['status'], 'done')
        with path.open('a') as stream:
            stream.write(partial[len(partial)//2:] + '\n')
        self.assertEqual(self.agents()['codex:partial']['status'], 'working')
        path.unlink()
        self.rollout('partial', source='vscode', finished=True)
        self.assertNotIn('codex:partial', self.agents())
        self.assertEqual(self.source.codex_board_adapter.read(self.now).values()[0]['status'], 'waiting')

    def test_partial_large_session_metadata_is_retried(self):
        thread = '12345678-1234-1234-1234-123456789abc'
        path = self.home / '.codex/sessions/2026/11/07' / f'rollout-test-{thread}.jsonl'
        path.parent.mkdir(parents=True)
        meta = json.dumps({'type': 'session_meta', 'timestamp': self.now - 10, 'payload': {
            'base_instructions': 'x' * 600000, 'id': thread, 'parent_session': 'fixture-launcher', 'cwd': self.cwd, 'source': 'exec'}})
        cut = 550000
        path.write_text(meta[:cut])
        self.agents()
        with path.open('a') as stream:
            stream.write(meta[cut:] + '\n')
        self.write(path, [self.event('task_started', self.now - 5)])
        agent = self.agents()['codex:' + thread]
        self.assertEqual((agent['cwd'], agent['status']), (self.cwd, 'working'))

    def test_resumed_start_in_middle_of_large_file_is_not_lost(self):
        path = self.rollout('middle', finished=True, started=self.now - 500)
        filler = lambda: {'type': 'response_item', 'timestamp': self.now - 50, 'payload': {
            'type': 'function_call_output', 'output': 'x' * 300000}}
        self.write(path, [filler(), filler(), self.event('task_started', self.now - 40), filler(), filler()])
        self.assertEqual(self.agents()['codex:middle']['status'], 'working')

    def test_large_completion_message_preserves_turn_boundary(self):
        path = self.rollout('large-completion')
        self.write(path, [self.event('task_complete', self.now - 5, last_agent_message='x' * 600000)])
        self.assertEqual(self.agents()['codex:large-completion']['status'], 'done')

    def test_wrapper_deduplication_and_missing_pid(self):
        self.rollout('thread', run_id='run-identifier')
        self.source.config.modules['codex_wrapper'] = True
        self.source.codex = CodexSource(self.home / '.codex')
        path = self.home / '.local/share/murmuratio/codex-runs/run-identifier.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'running', 'title': 'Wrapper title', 'thread': 'thread',
                                    'cwd': self.cwd, 'started': self.now - 60}))
        with patch.object(self.source.codex, 'read', side_effect=AssertionError('duplicate SQLite lookup')):
            agents = self.agents()
        self.assertEqual(set(agents), {'codex:run-identifier'})
        self.assertEqual(agents['codex:run-identifier']['task'], 'Wrapper title')
        self.assertEqual(agents['codex:run-identifier']['status'], 'working')
        self.assertEqual(agents['codex:run-identifier']['context_used'], 42000)

    def test_R3_finished_wrapper_with_expired_temporary_events_is_available(self):
        self.source.config.modules['codex_wrapper'] = True
        directory = self.home / '.local/share/murmuratio/codex-runs'
        directory.mkdir(parents=True)
        (directory / 'finished.json').write_text(json.dumps({'parent_session': 'fixture-launcher',
            'status': 'done', 'started': self.now - 60, 'ended': self.now - 10,
            'events': str(self.home / 'expired-temporary-events.jsonl')}))
        state = self.source.state()
        self.assertEqual(state['completed'][0]['status'], 'done')
        self.assertNotIn('codex_wrapper', state.get('sources', {}))

    def test_R3_wrapper_missing_or_empty_directory_is_absent(self):
        self.source.config.modules['codex_wrapper'] = True
        for create in (False, True):
            with self.subTest(directory_exists=create):
                if create:
                    (self.home / '.local/share/murmuratio/codex-runs').mkdir(parents=True)
                state = self.source.state()
                self.assertEqual(state['sources']['codex_wrapper']['status'], 'absent')

    def test_R3_wrapper_permission_failure_has_a_private_safe_reason(self):
        self.source.config.modules['codex_wrapper'] = True
        directory = self.home / '.local/share/murmuratio/codex-runs'
        directory.mkdir(parents=True)
        events = self.home / 'events.jsonl'
        events.write_text('{}\n')
        (directory / 'running.json').write_text(json.dumps({'parent_session': 'fixture-launcher',
            'status': 'running', 'started': self.now - 60, 'pid': 7, 'events': str(events)}))
        open_file = Path.open
        def denied(path, *args, **kwargs):
            if path == events:
                raise PermissionError(errno.EACCES, 'private exception detail', str(events))
            return open_file(path, *args, **kwargs)
        with patch.object(Path, 'open', denied):
            state = self.source.state()
        self.assertEqual(state['sources']['codex_wrapper'], {
            'status': 'unavailable', 'reason': 'permission_denied'})
        self.assertNotIn('private exception detail', json.dumps(state['sources']))
        self.assertNotIn(str(self.home), json.dumps(state['sources']))

    def test_R3_unreadable_empty_wrapper_directory_is_a_problem(self):
        self.source.config.modules['codex_wrapper'] = True
        (self.home / '.local/share/murmuratio/codex-runs').mkdir(parents=True)
        with patch('os.scandir', side_effect=PermissionError(errno.EACCES, 'synthetic denial')):
            state = self.source.state()
        self.assertEqual(state['sources']['codex_wrapper'], {
            'status': 'unavailable', 'reason': 'permission_denied'})

    def test_run_id_deduplicates_when_wrapper_has_no_thread(self):
        self.rollout('thread', run_id='run-identifier')
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run-identifier.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'running', 'title': 'Wrapper title', 'cwd': self.cwd,
                                    'started': self.now - 60, 'pid': 7}))
        agents = self.agents()
        self.assertEqual(set(agents), {'codex:run-identifier'})
        self.assertEqual(agents['codex:run-identifier']['thread'], 'thread')

    def test_missing_wrapper_pid_uses_completed_turn_status(self):
        self.rollout('finished-thread', finished=True)
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'running', 'thread': 'finished-thread', 'cwd': self.cwd,
                                    'started': self.now - 60}))
        agent = self.agents()['codex:run']
        self.assertEqual((agent['status'], agent['completed']), ('done', True))

    def test_completed_wrapper_without_pid_survives_incomplete_rollout(self):
        self.rollout('finished-thread')
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'done', 'thread': 'finished-thread', 'cwd': self.cwd,
                                    'started': self.now - 60, 'ended': self.now - 10}))
        agent = self.agents()['codex:run']
        self.assertEqual((agent['status'], agent['completed']), ('done', True))

    def test_invalid_wrapper_thread_does_not_hide_other_sessions(self):
        self.rollout('healthy')
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/bad.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'running', 'thread': {'unexpected': True},
                                    'cwd': self.cwd, 'started': self.now - 60}))
        agents = self.agents()
        self.assertIn('codex:healthy', agents)
        self.assertIsNone(agents['codex:bad']['thread'])

    def test_completed_wrapper_wins_over_newer_incomplete_rollout(self):
        path = self.rollout('resumed-wrapper', finished=True, started=self.now - 500)
        self.claude(name='old')
        self.claude(name='new', command='codex exec resume resumed-wrapper')
        self.write(path, [self.event('task_started', self.now - 58),
            {'type': 'turn_context', 'timestamp': self.now - 58, 'payload': {'model': 'gpt-6-astra', 'cwd': self.cwd}},
            {'type': 'response_item', 'timestamp': self.now - 57, 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'New independent task'}]}}])
        self.source.config.modules['codex_wrapper'] = True
        run = self.home / '.local/share/murmuratio/codex-runs/old-run.json'
        run.parent.mkdir(parents=True)
        run.write_text(json.dumps({'status': 'done', 'thread': 'resumed-wrapper', 'title': 'Old task',
            'model': 'old-model', 'cwd': self.cwd, 'parent_session': 'old',
            'started': self.now - 500, 'ended': self.now - 300}))
        agent = self.agents()['codex:old-run']
        self.assertEqual((agent['status'], agent['completed'], agent['model'], agent['task']),
                         ('done', True, 'old-model', 'Old task'))
        self.assertEqual((agent['parent_id'], agent['link']), ('claude:old', 'exact'))
        self.assertNotIn('codex:old-run', {a['id'] for a in self.source.state()['agents']})

    def test_done_wrapper_verdict_wins_over_unfinished_rollout_with_or_without_pid(self):
        self.rollout('unfinished', started=self.now - 3600)
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True)
        for verdict, expected in [('CRASHED: wrapper exited', 'crashed'), ('DONE', 'done'),
                                  ('', 'done'), ('BLOCKED: missing input', 'done')]:
            for pid in (None, 7, 8):
                with self.subTest(verdict=verdict, pid=pid):
                    path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'done', 'thread': 'unfinished', 'pid': pid,
                        'cwd': self.cwd, 'started': self.now - 3700, 'ended': self.now - 3650,
                        'verdict': verdict}))
                    before = path.read_bytes(), path.stat().st_mtime_ns
                    state = self.source.state()
                    self.assertEqual(state['agents'], [])
                    agent, = state['completed']
                    self.assertEqual((agent['status'], agent['completed'], agent['ended']),
                                     (expected, True, self.now - 3650))
                    self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_done_wrapper_without_end_time_is_completed(self):
        self.rollout('unfinished')
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'done', 'thread': 'unfinished',
                                    'started': self.now - 60, 'verdict': 'CRASHED: wrapper exited'}))
        state = self.source.state()
        self.assertEqual(state['agents'], [])
        agent, = state['completed']
        self.assertEqual((agent['status'], agent['completed'], agent['ended']),
                         ('crashed', True, self.now - 60))

    def test_previous_day_wrapper_still_wins_over_recent_rollout(self):
        self.now = time.mktime((2026, 11, 7, 12, 0, 0, 0, 0, -1))
        self.rollout('unfinished', started=self.now - 20 * 3600)
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True)
        events = self.home / 'events.jsonl'
        self.write(events, [{'type': 'thread.started', 'thread_id': 'unfinished'}])
        ended = self.now - 19 * 3600
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'done', 'events': str(events),
                                    'started': self.now - 20 * 3600, 'ended': ended, 'verdict': 'DONE'}))
        state = self.source.state()
        self.assertEqual(state['agents'], [])
        agent, = state['completed']
        self.assertEqual((agent['id'], agent['status'], agent['ended']), ('codex:run', 'done', ended))

    def test_inactive_codex_without_writer_moves_to_completed_after_30_minutes(self):
        for source, finished in [('exec', False), ('vscode', False)]:
            with self.subTest(source=source, finished=finished):
                path = self.rollout('stale', source=source, started=self.now - 1900, finished=finished,
                                    metadata={'parent_session': 'fixture-launcher'})
                last_at = self.now - 1800
                self.write(path, [self.event('token_count', last_at)])
                # A recent mtime must not disguise an old last activity timestamp.
                before = path.read_bytes(), path.stat().st_mtime_ns
                with patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value=set()) as probe:
                    self.assertEqual(len(self.source.state()['agents']), 1)
                    probe.assert_not_called()
                    self.now += 1
                    state = self.source.state()
                    probe.assert_called_once_with([path])
                    self.assertEqual(state['agents'], [])
                    agent, = state['completed']
                    self.assertEqual((agent['status'], agent['completed'], agent['ended'], agent['last_at']),
                                     ('crashed', True, last_at, last_at))
                    seconds = agent['seconds']
                    self.now += 300
                    self.assertEqual(self.source.state()['completed'][0]['seconds'], seconds)
                self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
                path.unlink()

    def test_S4_finished_chat_does_not_crash_after_idle(self):
        path = self.rollout('finished-chat', source='vscode', started=self.now - 1900, finished=True)
        with patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value=set()) as probe:
            state = self.source.state()
        self.assertEqual(state['agents'], [])
        self.assertEqual(state['completed'], [])
        probe.assert_not_called()

    def test_B2_run_id_does_not_match_a_different_thread_namespace(self):
        self.source.config.modules['codex_wrapper'] = True
        directory = self.home / '.local/share/murmuratio/codex-runs'
        directory.mkdir(parents=True)
        for name, thread in [('collision', 'thread-one'), ('run-two', 'collision')]:
            (directory / (name + '.json')).write_text(json.dumps({'parent_session': 'fixture-launcher',
                'status': 'running', 'thread': thread, 'started': self.now - 10, 'pid': 7}))
        agents = self.agents()
        self.assertEqual(set(agents), {'codex:collision', 'codex:run-two'})

    def test_S5_wrapper_finished_before_midnight_is_still_discovered(self):
        midnight = time.mktime((2026, 11, 7, 0, 0, 0, 0, 0, -1))
        self.now = midnight + 60
        self.source.config.modules['codex_wrapper'] = True
        directory = self.home / '.local/share/murmuratio/codex-runs'
        directory.mkdir(parents=True)
        (directory / 'before-midnight.json').write_text(json.dumps({'parent_session': 'fixture-launcher',
            'status': 'done', 'started': midnight - 600, 'ended': midnight - 300}))
        self.assertIn('codex:before-midnight', self.agents())

    def test_inactive_codex_with_writer_or_failed_probe_stays_active(self):
        path = self.rollout('alive', started=self.now - 1900)
        os.utime(path, (self.now - 1801, self.now - 1801))
        for writers in ({path}, None):
            with self.subTest(writers=writers):
                with patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value=writers):
                    state = self.source.state()
                self.assertEqual(state['completed'], [])
                agent, = state['agents']
                self.assertEqual((agent['status'], agent['completed']), ('hung', False))

    def test_live_wrapper_pid_keeps_inactive_rollout_active(self):
        self.rollout('alive', started=self.now - 1900)
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'running', 'thread': 'alive', 'pid': 7,
                                    'cwd': self.cwd, 'started': self.now - 1900}))
        with patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value=set()):
            state = self.source.state()
        self.assertEqual(state['completed'], [])
        agent, = state['agents']
        self.assertEqual((agent['status'], agent['completed']), ('hung', False))

    def test_live_rollout_keeps_card_active_after_wrapper_process_exits(self):
        rollout = self.rollout('alive', started=self.now - 1900)
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'running', 'thread': 'alive', 'pid': 8,
                                    'cwd': self.cwd, 'started': self.now - 1900}))
        with patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value={rollout}):
            state = self.source.state()
        self.assertEqual(state['completed'], [])
        agent, = state['agents']
        self.assertEqual((agent['status'], agent['completed'], agent['ended']), ('crashed', False, None))
        self.assertNotIn('_dead_expired', agent)
        with patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value=set()):
            state = self.source.state()
        self.assertEqual(state['agents'], [])
        self.assertEqual(state['completed'][0]['status'], 'crashed')

    def test_expired_rollout_without_turn_start_completes_wrapper_without_pid(self):
        rollout = self.rollout('incomplete', started=self.now - 1900)
        rollout.write_text(rollout.read_text().splitlines()[0] + '\n')
        os.utime(rollout, (self.now - 1900, self.now - 1900))
        self.source.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'running', 'thread': 'incomplete',
                                    'cwd': self.cwd, 'started': self.now - 1901}))
        with patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value=set()):
            state = self.source.state()
        self.assertEqual(state['agents'], [])
        agent, = state['completed']
        self.assertEqual((agent['status'], agent['completed']), ('crashed', True))

    def test_rollout_writer_probe_ignores_readers_and_handles_partial_matches(self):
        paths = [self.home / name for name in ('writer', 'reader', 'closed')]
        result = subprocess.CompletedProcess([], 1, stdout=(
            f'p7\nf10\nau\nn{paths[0].resolve()}\nf11\nar\nn{paths[1].resolve()}\n'), stderr='')
        with patch('murmuratio_app.adapters.codex_board.subprocess.run', return_value=result) as probe:
            self.assertEqual(rollout_writers(paths), {paths[0]})
            self.assertEqual(probe.call_args.args[0][-3:], [str(path.resolve()) for path in paths])
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired('lsof', 5)):
            with self.subTest(failure=failure):
                with patch('murmuratio_app.adapters.codex_board.subprocess.run', side_effect=failure):
                    self.assertIsNone(rollout_writers(paths))
        result.stderr = 'lsof: permission denied'
        with patch('murmuratio_app.adapters.codex_board.subprocess.run', return_value=result):
            self.assertIsNone(rollout_writers(paths))

    @unittest.skipUnless(Path('/usr/sbin/lsof').is_file(), 'macOS lsof is required')
    def test_actual_rollout_writer_keeps_old_thread_active_until_closed(self):
        path = self.rollout('writer', started=self.now - 1900)
        os.utime(path, (self.now - 1801, self.now - 1801))
        with path.open('a'):
            self.assertEqual(rollout_writers([path]), {path})
            state = self.source.state()
            self.assertEqual(state['completed'], [])
            self.assertEqual(state['agents'][0]['status'], 'hung')
        with path.open('r'):
            self.assertEqual(rollout_writers([path]), set())
            state = self.source.state()
            self.assertEqual(state['agents'], [])
            self.assertEqual(state['completed'][0]['status'], 'crashed')

    @unittest.skipUnless(Path('/usr/sbin/lsof').is_file(), 'macOS lsof is required')
    def test_B1_live_writer_stays_on_the_board_however_long_it_is_quiet(self):
        # Three days quiet, still open for writing: an unfinished worker never leaves the board.
        path = self.rollout('quiet-live', started=self.now - 3 * 86400 - 10)
        os.utime(path, (self.now - 3 * 86400, self.now - 3 * 86400))
        with path.open('a'):
            self.assertEqual(rollout_writers([path]), {path})
            self.assertEqual(self.agents()['codex:quiet-live']['status'], 'hung')
            self.write(path, [self.event('token_count', self.now - 1)])
            self.assertEqual(self.agents()['codex:quiet-live']['status'], 'working')

    def test_old_rollouts_are_checked_cheaply_and_only_open_workers_stay(self):
        day = 86400
        open_worker = self.rollout('open-worker', started=self.now - 3 * day - 10)
        finished = self.rollout('finished-worker', started=self.now - 3 * day - 10, finished=True)
        chat = self.rollout('old-chat', source='vscode', started=self.now - 3 * day - 10)
        for path in (open_worker, finished, chat):
            os.utime(path, (self.now - 3 * day, self.now - 3 * day))
        writers = {open_worker, finished, chat}
        with patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value=writers) as probe:
            agents = self.agents()
            self.assertEqual(set(agents), {'codex:open-worker'})
            self.assertEqual(agents['codex:open-worker']['status'], 'hung')
            self.assertEqual([call.args[0] for call in probe.call_args_list], [[open_worker]])
        adapter = self.source.codex_board_adapter
        self.assertEqual({path.name: value[1] for path, value in adapter.old.items()},
                         {open_worker.name: True, finished.name: False, chat.name: False})
        # A crashed verdict is kept until the file changes: no writer check on later polls.
        with patch('murmuratio_app.adapters.codex_board.rollout_writers', return_value=set()):
            self.assertNotIn('codex:open-worker', self.agents())
        with patch('murmuratio_app.adapters.codex_board.rollout_writers', side_effect=AssertionError('probed')):
            self.assertNotIn('codex:open-worker', self.agents())
            self.assertNotIn('codex:open-worker', self.agents())
        with patch('murmuratio_app.adapters.codex_board.open_worker', side_effect=AssertionError('reread')):
            self.agents()

    def test_claude_subagent_bash_result_links_to_owning_session(self):
        self.claude()
        path = self.home / '.claude/projects/project/parent/subagents/agent-one.jsonl'
        self.write(path, [
            {'type': 'assistant', 'cwd': self.cwd, 'isSidechain': True, 'timestamp': self.now - 60,
             'sessionId': 'parent', 'message': {'stop_reason': 'tool_use', 'content': [
                 {'type': 'tool_use', 'name': 'Bash', 'id': 'sub-call', 'input': {'command': 'codex exec task'}}]}},
            {'type': 'user', 'isSidechain': True, 'timestamp': self.now - 59, 'sessionId': 'parent',
             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'sub-call',
                                      'content': 'thread_id: sub-thread'}]}}])
        self.rollout('sub-thread')
        agent = self.agents()['codex:sub-thread']
        self.assertEqual((agent['parent_id'], agent['link']), ('claude:parent', 'exact'))

    def test_long_running_bash_keeps_quiet_claude_parent_after_worker_finishes(self):
        launched = self.now - 300
        parent = self.home / '.claude/projects/project/parent.jsonl'
        self.write(parent, [
            {'type': 'user', 'cwd': self.cwd, 'timestamp': launched - 1, 'message': {'content': 'Coordinate work'}},
            {'type': 'assistant', 'cwd': self.cwd, 'timestamp': launched, 'message': {'stop_reason': 'tool_use',
                'content': [{'type': 'tool_use', 'name': 'Bash', 'id': 'long-call', 'input': {'command': 'codex exec task'}}]}}])
        os.utime(parent, (launched, launched))
        path = self.rollout('long-running', started=launched + 2)
        agents = self.agents()
        self.assertEqual((agents['codex:long-running']['parent_id'], agents['codex:long-running']['link']),
                         ('claude:parent', 'inferred'))
        self.assertEqual(agents['claude:parent']['status'], 'working')
        self.assertTrue(agents['claude:parent']['quiet'])
        self.write(path, [self.event('task_complete', self.now - 1)])
        self.assertEqual(self.agents()['claude:parent']['status'], 'working')

    def test_ambiguous_long_running_launch_keeps_quiet_parent_unlinked(self):
        launched = self.now - 300
        parent = self.home / '.claude/projects/project/parent.jsonl'
        self.write(parent, [{'type': 'assistant', 'cwd': self.cwd, 'timestamp': launched,
            'message': {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'name': 'Bash',
                'id': 'ambiguous', 'input': {'command': 'codex exec task'}}]}}])
        os.utime(parent, (launched, launched))
        self.rollout('first-long', started=launched + 2)
        self.rollout('second-long', started=launched + 2)
        agents = self.agents()
        self.assertNotIn('claude:parent', agents)
        self.assertIsNone(agents['codex:first-long']['parent_id'])
        self.assertIsNone(agents['codex:second-long']['parent_id'])

    def test_wrapper_context_survives_missing_native_usage(self):
        path = self.rollout('context-thread')
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records = [record for record in records if record.get('payload', {}).get('type') != 'token_count']
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        events = self.home / 'wrapper-events.jsonl'
        self.write(events, [self.event('token_count', self.now - 3, info={
            'last_token_usage': {'input_tokens': 42000}})])
        self.source.config.modules['codex_wrapper'] = True
        run = self.home / '.local/share/murmuratio/codex-runs/context-run.json'
        run.parent.mkdir(parents=True)
        run.write_text(json.dumps({'parent_session': 'fixture-launcher', 'status': 'running', 'thread': 'context-thread', 'events': str(events),
                                   'cwd': self.cwd, 'pid': 7, 'started': self.now - 60}))
        agent = self.agents()['codex:context-run']
        self.assertEqual((agent['context_used'], agent['context_window']), (42000, 200000))

    def test_recent_window_and_large_rollout_head_tail(self):
        old = self.rollout('old', finished=True)  # finished over a day ago: left the board
        os.utime(old, (self.now - 90000, self.now - 90000))
        path = self.rollout('large', finished=True)
        self.write(path, [{'type': 'response_item', 'timestamp': self.now - 30, 'payload': {
            'type': 'function_call_output', 'output': 'x' * 600000}}, self.event('task_started', self.now - 5)])
        agents = self.agents()
        self.assertNotIn('codex:old', agents)
        self.assertEqual(agents['codex:large']['task'], 'Build the board')
        self.assertEqual(agents['codex:large']['status'], 'working')

    def test_codex_home_environment_and_explicit_override(self):
        custom = self.home / 'custom'
        with patch.dict(os.environ, {'CODEX_HOME': str(custom)}):
            self.assertEqual(Discovery.at(self.home, Config()).codex_rollouts, custom / 'sessions')
            self.assertEqual(Discovery.at(self.home, Config(), self.home / 'override').codex_home, self.home / 'override')
            path = self.rollout('custom-thread')
            target = custom / 'sessions/2026/11/07' / path.name
            target.parent.mkdir(parents=True)
            path.rename(target)
            source = NowSource(self.home, clock=lambda: self.now, config=Config())
            self.assertEqual([a['id'] for a in source.state()['agents']], ['codex:custom-thread'])

    def test_rollout_symlink_outside_session_root_is_ignored(self):
        path = self.rollout('outside')
        target = self.home / 'outside.jsonl'
        path.rename(target)
        self.write(target, [self.event('token_count', self.now - 1, rate_limits={
            'primary': {'used_percent': 77, 'window_minutes': 10080, 'resets_at': self.now + 3600},
            'credits': {'balance': '999'}})])
        path.symlink_to(target)
        self.rollout('inside')
        state = self.source.state()
        self.assertEqual({agent['id'] for agent in state['agents'] + state['completed']}, {'codex:inside'})
        self.assertEqual(state['limits']['codex'], [])
        meter = self.source.codex_meter_adapter.read(self.now, True, True).values()
        self.assertTrue(all(not item['points'] and not item['usage'] for item in meter))

    def test_codex_only_http_uses_explicit_home_without_sqlite(self):
        path = self.rollout('http-thread')
        self.rollout('http-finished', finished=True)
        for name in ('desktop_agent_created', 'desktop_agent_forked'):
            self.rollout(name, source=None, metadata=self.launch_metadata(name))
        self.rollout('desktop-agent-done', source=None, finished=True,
                     metadata=self.launch_metadata('desktop_agent_created'))
        self.rollout('http-chat', source='cli')
        custom = self.home / 'custom'
        target = custom / 'sessions/2026/11/07' / path.name
        target.parent.mkdir(parents=True)
        for rollout in path.parent.glob('*.jsonl'):
            rollout.rename(target.parent / rollout.name)
        env = {**os.environ, 'HOME': str(self.home), 'CODEX_HOME': str(self.home / 'ignored'), 'PORTLESS_URL': ''}
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.Popen([sys.executable, '-B', '-c', 'from murmuratio_app.server import main; main()',
                                 '--port', '0', '--codex-home', str(custom)], cwd=root, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            address = proc.stdout.readline().strip().rsplit(':', 1)[-1]
            conn = http.client.HTTPConnection('127.0.0.1', int(address), timeout=5)
            conn.request('GET', '/api/now')
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            state = json.loads(response.read())
            conn.close()
            self.assertEqual({agent['id'] for agent in state['agents']},
                             {'codex:http-thread', 'codex:desktop_agent_created', 'codex:desktop_agent_forked'})
            self.assertEqual({agent['id'] for agent in state['completed']},
                             {'codex:http-finished', 'codex:desktop-agent-done'})
            self.assertTrue(all(agent['codex_role'] == 'worker' and not agent.get('parent_session')
                                for agent in state['agents'] + state['completed']))
            self.assertFalse((self.home / '.claude').exists())
        finally:
            proc.terminate()
            proc.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
