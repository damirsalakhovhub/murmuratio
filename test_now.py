"""Read-only Now dashboard contract, using isolated source files and HTTP."""
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from codex_source import CodexSource
from murmuratio_config import Config, MODULES
from now_source import codex_burn, consume_codex_meter, consume_credits, consume_usage, credit_spend, NowSource, claude_status, codex_status, current_action, limit_window, pid_alive, quota_routing, timestamp, week_forecast, weekly_pace
from server import make_handler, sanitize


class NowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp()
        self.runs = self.home / '.claude/tools/codex-runs'
        self.runs.mkdir(parents=True)
        config = Config.load(self.home)
        config.modules = {name: True for name in MODULES}
        config.project_mappings = [(self.home / 'northstarcom', 'Northstar', True),
                                   (self.home / 'projects/Atlas', 'Atlas', True),
                                   (self.home / 'beacon-app', 'Beacon', True),
                                   (self.home / 'beacon-worktrees', 'Beacon', True)]
        config.project_fallback_mappings = [(self.home / '.claude', 'Harbor', False),
                                   (Path(__file__).resolve().parent, 'Harbor', False)]
        config.inherit_folders = ['side-session-notes']
        config.fallback_project = 'Harbor'
        self.source = NowSource(self.home, sanitize, clock=lambda: self.now, alive=lambda pid: pid == 7, config=config)

    def write_run(self, name='run', **changes):
        run = dict(status='running', title='Example task', mode='new', tier='sol', model='gpt-6.1-sol',
                   target=str(self.home / 'northstarcom-worktrees/dev'), started=self.now - 300, pid=7)
        run.update(changes)
        path = self.runs / (name + '.json')
        path.write_text(json.dumps(run))
        return path

    def append(self, path, record, modified=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')
        os.utime(path, (modified or self.now, modified or self.now))

    def claude(self, name='session', cwd=None, modified=None, stop='end_turn'):
        path = self.home / '.claude/projects/project' / (name + '.jsonl')
        self.append(path, dict(type='user', cwd=cwd or str(self.home / '.claude/tools'), timestamp=self.now - 100,
                               message={'content': [{'type': 'text', 'text': 'First task line\nMore instructions'}]}))
        self.append(path, dict(type='assistant', timestamp=self.now - 50,
                               message={'model': 'claude-opus-5-5', 'stop_reason': stop,
                                        'content': [{'type': 'text', 'text': 'Finished\nall checks'}],
                                        'usage': {'input_tokens': 2, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 20}}), modified)
        return path

    def desktop(self, cli, app, **fields):
        path = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b' / (app + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(cliSessionId=cli, **fields)))
        return path

    def test_parent_transcript_id_wins_over_app_id_and_target_project(self):
        self.claude('harbor')
        self.claude('northstar', cwd=str(self.home / 'northstarcom-worktrees/dev'))
        self.desktop('harbor', 'local_harbor')
        self.desktop('northstar', 'local_northstar')
        self.write_run('cli', parent_session='harbor', parent_app_session='local_northstar')
        self.write_run('app', parent_session='unknown', parent_app_session='local_harbor')
        self.write_run('from', from_cwd=str(self.home / '.claude/tools'))
        self.write_run('legacy')
        self.write_run('inspector', target=str(Path(__file__).resolve().parent))
        self.write_run('inspector-nested', target=str(Path(__file__).resolve().parent / 'web'))
        agents = {a['id']: a for a in self.source.state()['agents']}
        for name in ('cli', 'app', 'from', 'inspector', 'inspector-nested'):
            with self.subTest(name=name):
                self.assertEqual(agents['codex:' + name]['parent_id'], 'claude:harbor')
                self.assertEqual(agents['codex:' + name]['project'], 'Harbor')
        self.assertEqual(agents['codex:legacy']['parent_id'], 'claude:northstar')
        self.assertEqual(agents['claude:harbor']['status'], 'idle')
        self.assertEqual(agents['codex:cli']['started'], self.now - 300)

    def test_project_orchestrator_under_archived_master_and_nested_children(self):
        self.desktop('master', 'local_master', isArchived=True)
        self.claude('atlas', cwd=str(self.home / 'projects/Atlas'))
        self.desktop('atlas', 'local_atlas', spawnedFrom={'sessionId': 'local_master'})
        self.claude('worker', cwd=str(self.home / 'projects/Atlas-worktrees/interfaces'))
        self.desktop('worker', 'local_worker', spawnedFrom={'sessionId': 'local_atlas'})
        self.write_run(parent_session='worker')
        agents = {a['id']: a for a in self.source.state()['agents']}
        self.assertTrue(agents['claude:atlas']['orchestrator'])
        self.assertFalse(agents['claude:worker']['orchestrator'])
        self.assertEqual(agents['claude:atlas']['status'], 'idle')
        self.assertEqual(agents['claude:atlas']['app_session'], 'local_atlas')
        self.assertEqual(agents['claude:worker']['parent_id'], 'claude:atlas')
        self.assertEqual(agents['codex:run']['parent_id'], 'claude:worker')
        self.assertEqual(agents['codex:run']['project'], 'Atlas')
        self.assertNotIn('claude:master', agents)

    def test_scheduled_desktop_sessions_never_reach_board(self):
        self.claude('parent')
        self.desktop('parent', 'local_parent', scheduledTaskId=None)
        for name, fields, task_id, stop in (
                ('scheduled-root', {}, 'quota-check', 'tool_use'),
                ('scheduled-worker', {'spawnedFrom': {'sessionId': 'local_parent'}}, 'quota-check', 'tool_use'),
                ('scheduled-done', {'spawnedFrom': {'sessionId': 'local_parent'}}, 'quota-check', 'end_turn'),
                ('scheduled-empty-id', {}, '', 'tool_use')):
            self.claude(name, stop=stop)
            path = self.desktop(name, 'local_' + name, **fields)
            # Prime both transcript and desktop caches before metadata changes.
            self.assertIn('claude:' + name, {a['id'] for a in self.source.state()['agents']})
            meta = json.loads(path.read_text())
            meta.update(scheduledTaskId=task_id, title='Scheduled sample task')
            path.write_text(json.dumps(meta))
            for _ in range(2):
                state = self.source.state()
                ids = {a['id'] for a in state['agents'] + state['completed']}
                self.assertNotIn('claude:' + name, ids)
                self.assertIn('claude:parent', ids)
                self.assertEqual(next(a for a in state['agents'] if a['id'] == 'claude:parent')['status'], 'waiting')

    def test_parent_cycle_and_missing_parent_do_not_drop_cards(self):
        for name, parent in [('a', 'local_b'), ('b', 'local_a'), ('orphan', 'missing')]:
            self.claude(name)
            self.desktop(name, 'local_' + name, spawnedFrom={'sessionId': parent})
        agents = {a['id']: a for a in self.source.state()['agents']}
        for agent in agents.values():
            seen = {agent['id']}
            while agent.get('parent_id'):
                self.assertNotIn(agent['parent_id'], seen)
                seen.add(agent['parent_id'])
                agent = agents[agent['parent_id']]
        self.assertEqual(agents['claude:orphan']['status'], 'done')

    def test_hidden_starter_children_join_project_orchestrator_and_approval_wait_is_blocked(self):
        # The old orchestrator waits on an approval prompt for 15 minutes; its chats still hang off the board root.
        self.claude('old', stop='tool_use', modified=self.now - 900)
        self.desktop('old', 'local_old', title='Northstar orchestrator')
        self.claude('new')
        self.desktop('new', 'local_new', title='Northstar orchestrator', spawnedFrom={'sessionId': 'local_old'})
        self.claude('task')
        self.desktop('task', 'local_task', title='Northstar sample task', spawnedFrom={'sessionId': 'local_old'})
        agents = {a['id']: a for a in self.source.state()['agents']}
        roots = [a for a in agents.values() if a['orchestrator']]
        self.assertEqual([a['id'] for a in roots], ['claude:new'])
        self.assertEqual(agents['claude:task']['parent_id'], 'claude:new')

    def test_board_keeps_today_running_and_orchestrators(self):
        midnight = datetime.fromtimestamp(self.now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        self.claude('orch', modified=midnight - 7200)
        self.desktop('orch', 'local_orch')
        self.claude('today-worker', modified=midnight + 60)
        self.desktop('today-worker', 'local_today', spawnedFrom={'sessionId': 'local_orch'})
        real_now, self.now = self.now, midnight - 10
        self.claude('yesterday-worker', modified=midnight - 60)
        self.now = real_now
        self.desktop('yesterday-worker', 'local_y', spawnedFrom={'sessionId': 'local_orch'})
        self.write_run('stale-running', started=self.now - 43201)
        self.write_run('today-done', status='done', started=midnight + 10, ended=midnight + 60)
        self.write_run('yesterday-done', status='done', started=midnight - 600, ended=midnight - 60)
        state = self.source.state()
        ids = {a['id'] for a in state['agents']}
        self.assertIn('claude:orch', ids)
        self.assertIn('claude:today-worker', ids)
        self.assertNotIn('claude:yesterday-worker', ids)
        self.assertIn('codex:stale-running', ids)
        self.assertEqual([a['id'] for a in state['completed']], ['codex:today-done'])

    def test_waiting_since_done_end_and_compact_current_action(self):
        self.claude('parent')
        self.desktop('parent', 'local_parent')
        worker = self.claude('worker', stop='tool_use')
        self.desktop('worker', 'local_worker', spawnedFrom={'sessionId': 'local_parent'})
        self.append(worker, dict(type='assistant', timestamp=self.now - 20, message={
            'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': '/project/panel.js'}}]}))
        agents = {a['id']: a for a in self.source.state()['agents']}
        self.assertEqual(agents['claude:worker']['current_action'], 'editing panel.js')
        self.assertEqual(agents['claude:parent']['waiting_since'], self.now - 50)
        self.assertEqual(agents['claude:parent']['status'], 'idle')
        self.append(worker, dict(type='assistant', timestamp=self.now - 10,
                                message={'stop_reason': 'end_turn', 'content': 'A very long final report ' * 500}))
        agent = {a['id']: a for a in self.source.state()['agents']}['claude:worker']
        self.assertEqual((agent['started'], agent['ended'], agent['seconds']), (self.now - 100, self.now - 10, 90))
        self.assertNotIn('report', agent['current_action'])

    def test_project_folder_matching_respects_boundaries(self):
        self.assertEqual(self.source.project(str(self.home / 'northstarcompany')), 'northstarcompany')
        self.assertEqual(self.source.project(str(self.home / 'projects/Atlas0')), 'Atlas0')
        self.assertEqual(self.source.project(str(self.home / '.claude/side-session-notes/a')), '')
        self.assertEqual(self.source.project(str(self.home / 'northstarcom/side-session-notes/a')), 'Northstar')
        self.assertEqual(self.source.project(str(self.home / 'northstarcom-worktrees/arts')), 'Northstar')
        for folder in ('beacon-app', 'beacon-app/src', 'beacon-worktrees/ui', 'beacon-app-worktrees/ui'):
            self.assertEqual(self.source.project(str(self.home / folder)), 'Beacon')

    def test_handoff_replaces_predecessors_and_redirects_all_children(self):
        real_now = self.now
        for name, parent, age in [('third', 'local_second', 0), ('second', 'local_first', 100), ('first', None, 200)]:
            self.now = real_now - age
            self.claude(name)
            self.desktop(name, 'local_' + name, title='Harbor orchestrator',
                         spawnedFrom={'sessionId': parent} if parent else {})
        self.now = real_now
        self.claude('worker')
        self.desktop('worker', 'local_worker', spawnedFrom={'sessionId': 'local_first'})
        self.write_run('cli', parent_session='first')
        self.write_run('app', parent_app_session='local_second')
        agents = {a['id']: a for a in self.source.state()['agents']}
        self.assertNotIn('claude:first', agents)
        self.assertNotIn('claude:second', agents)
        self.assertTrue(agents['claude:third']['orchestrator'])
        self.assertIsNone(agents['claude:third']['parent_id'])
        for name in ('claude:worker', 'codex:cli', 'codex:app'):
            self.assertEqual(agents[name]['parent_id'], 'claude:third')

    def test_different_title_is_a_worker_not_a_handoff(self):
        self.claude('parent')
        self.desktop('parent', 'local_parent', title='Harbor orchestrator')
        self.claude('child')
        self.desktop('child', 'local_child', title='Worker orchestrator', spawnedFrom={'sessionId': 'local_parent'})
        agents = {a['id']: a for a in self.source.state()['agents']}
        self.assertEqual(agents['claude:child']['parent_id'], 'claude:parent')
        self.assertFalse(agents['claude:child']['orchestrator'])

    def test_notes_folder_joins_starters_column(self):
        self.claude('orphan-notes', cwd=str(self.home / 'side-session-notes/local_orphan'))
        self.desktop('orphan-notes', 'local_orphan', spawnedFrom={'sessionId': 'missing'})
        self.claude('nested-notes', cwd=str(self.home / 'side-session-notes/nested'))
        self.desktop('nested-notes', 'local_nested', spawnedFrom={'sessionId': 'local_notes'})
        self.claude('notes', cwd=str(self.home / 'side-session-notes/local_12345678-abcd-1234-abcd-123456789abc'))
        self.desktop('notes', 'local_notes', spawnedFrom={'sessionId': 'local_parent'})
        self.claude('parent', cwd=str(self.home / 'northstarcom'))
        self.desktop('parent', 'local_parent')
        self.write_run(parent_app_session='local_notes', cwd=str(self.home / 'side-session-notes/run'))
        agents = {a['id']: a for a in self.source.state()['agents']}
        self.assertEqual(agents['claude:notes']['project'], 'Northstar')
        self.assertEqual(agents['claude:notes']['parent_id'], 'claude:parent')
        self.assertEqual(agents['claude:nested-notes']['project'], 'Northstar')
        self.assertEqual(agents['claude:nested-notes']['parent_id'], 'claude:notes')
        self.assertEqual(agents['codex:run']['project'], 'Northstar')
        self.assertEqual(agents['codex:run']['parent_id'], 'claude:notes')
        for name in ('claude:notes', 'claude:nested-notes', 'codex:run'):
            self.assertEqual(agents[name]['folder'], 'northstarcom')
        self.assertEqual(agents['claude:orphan-notes']['folder'], '—')

    def test_readable_current_actions(self):
        tool_id = '11111111-1111-4111-8111-111111111111'
        for tool, server, expected in [
            ('mcp__' + tool_id + '__get_screenshot', '', 'MCP tool'),
            ('mcp__' + tool_id + '__get_screenshot', 'mcp__' + tool_id, 'MCP tool'),
            ('mcp__' + tool_id + '__get_screenshot', 'figma', 'Figma'),
            ('mcp__figma__get_design_context', '', 'Figma'),
            ('mcp__cua_repl__js', '', 'Browser'),
            ('mcp__sessions__list', '', 'Sessions'),
            ('read_' + tool_id, '', 'reading files')]:
            with self.subTest(tool=tool):
                self.assertEqual(current_action(tool, server=server), expected)
        self.assertEqual(current_action('Edit', '/project/panel-abcdef123.js'), 'editing panel.js')
        self.assertEqual(current_action('custom_' + tool_id), 'custom')

    def test_current_orchestrator_turn_start_ignores_tool_results_and_meta(self):
        path = self.claude('orch', stop='tool_use')
        self.desktop('orch', 'local_orch')
        self.append(path, dict(type='user', timestamp=self.now - 10, message={'content': 'New turn'}))
        self.append(path, dict(type='user', timestamp=self.now - 5, isMeta=True, message={'content': 'Notification'}))
        self.append(path, dict(type='user', timestamp=self.now - 2,
                               message={'content': [{'type': 'tool_result', 'content': 'ok'}]}))
        agent, = self.source.state()['agents']
        self.assertEqual(agent['turn_started'], self.now - 10)
        self.assertEqual(agent['started'], self.now - 100)

    def test_unarchived_orchestrator_and_long_running_work_survive_age_cutoffs(self):
        real_now = self.now
        self.now -= 5 * 86400
        self.claude('orch')
        self.desktop('orch', 'local_orch', title='Harbor orchestrator')
        self.write_run('running')
        self.now = real_now
        agents = {a['id']: a for a in self.source.state()['agents']}
        self.assertEqual(agents['claude:orch']['status'], 'waiting')
        self.assertIn('codex:running', agents)
        self.desktop('orch', 'local_orch', isArchived=True)
        self.assertNotIn('claude:orch', {a['id'] for a in self.source.state()['agents']})

    def test_claude_limits_iso_and_epoch_resets_and_invalid_files(self):
        path = self.home / '.claude/tools/claude-limits.json'
        self.assertEqual(self.source.claude_limits(), [])
        path.write_text(json.dumps({'rate_limits': {
            'five_hour': {'used_percentage': 82, 'resets_at': '2026-11-06T12:00:00Z'},
            'seven_day': {'used_percentage': 27.5, 'resets_at': self.now + 86400}}}))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(self.source.claude_limits(), [
            dict(window_minutes=300, used_percent=82, resets_at=datetime(2026, 11, 6, 12, tzinfo=timezone.utc).timestamp()),
            dict(window_minutes=10080, used_percent=27.5, resets_at=self.now + 86400)])
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        for invalid in ('{', '[]', '{"rate_limits": []}', '{"rate_limits":{"five_hour":{"used_percentage":"bad"}}}'):
            path.write_text(invalid)
            self.assertEqual(self.source.claude_limits(), [])

    def test_codex_limits_newest_rollout_and_latest_valid_event(self):
        root = self.home / '.codex/sessions/2026/11/05'
        older, newer = root / 'rollout-old.jsonl', root / 'rollout-new.jsonl'
        self.append(older, {'rate_limits': {'primary': {'window_minutes': 300, 'used_percent': 99}}}, self.now - 10)
        windows = {'primary': {'window_minutes': 300, 'used_percent': 80, 'resets_at': self.now + 3600},
                   'secondary': {'window_minutes': 10080, 'used_percent': 35, 'resets_at': self.now + 86400}}
        self.append(newer, {'payload': {'rate_limits': {'primary': {'window_minutes': 300, 'used_percent': 10}}}})
        self.append(newer, {'payload': {'rate_limits': windows}})
        self.append(newer, {'payload': {'rate_limits': None}})
        with newer.open('a') as stream:
            stream.write('bad "rate_limits" record\n{"rate_limits":')
        before = newer.read_bytes()
        self.assertEqual(self.source.codex_limits(), list(windows.values()))
        self.assertEqual(newer.read_bytes(), before)

    def test_limits_reject_nonfinite_values_before_routing_json(self):
        for value in (float('inf'), float('-inf'), float('nan'), True, 'invalid'):
            with self.subTest(value=value):
                self.assertIsNone(timestamp(value))
                self.assertIsNone(limit_window(dict(window_minutes=value, used_percent=20)))
                self.assertIsNone(limit_window(dict(window_minutes=300, used_percent=value)))
        path = self.home / '.claude/tools/claude-limits.json'
        path.write_text('{"rate_limits":{"seven_day":{"used_percentage":20,"resets_at":Infinity}}}')
        state = self.source.state()
        self.assertIsNone(state['limits']['claude'][0]['resets_at'])
        self.assertIsNone(state['routing']['claude']['headroom'])
        json.dumps(state, allow_nan=False)

    def week(self, used=30, days=7):
        return dict(window_minutes=10080, used_percent=used, resets_at=self.now + days * 86400)

    def history_point(self, age, used, reset=None, tool='claude'):
        return dict(at=self.now - age * 86400, tool=tool, window='week', used_percent=used,
                    resets_at=reset if reset is not None else self.now + 7 * 86400)

    def test_weekly_pace_no_history_and_missing_reset(self):
        stats = weekly_pace('claude', self.week(30), [], self.now)
        self.assertEqual(stats['remaining'], 70)
        self.assertEqual(stats['days_left'], 7)
        self.assertEqual(stats['allowance_per_day'], 10)
        self.assertEqual(stats['burn_per_day'], 0)
        self.assertEqual(stats['headroom'], 10)
        for reset in (None, self.now, self.now - 1):
            window = self.week()
            window['resets_at'] = reset
            self.assertIsNone(weekly_pace('claude', window, [], self.now)['headroom'])

    def test_weekly_pace_three_day_average_and_unchanged_tail(self):
        history = [self.history_point(4, 10), self.history_point(2, 30), self.history_point(1, 40)]
        stats = weekly_pace('claude', self.week(40), history, self.now)
        self.assertAlmostEqual(stats['burn_per_day'], 20 / 3)
        self.assertAlmostEqual(stats['headroom'], (60 / 7) / (20 / 3))
        self.assertEqual(weekly_pace('claude', self.week(10), [self.history_point(4, 10)], self.now)['burn_per_day'], 0)

    def test_weekly_pace_reset_starts_new_segment(self):
        history = [self.history_point(3, 60), self.history_point(2, 95), self.history_point(1, 5)]
        self.assertEqual(weekly_pace('claude', self.week(15), history, self.now)['burn_per_day'], 10)
        # A changed reset date also starts a segment if new usage exceeds the old percentage.
        history = [self.history_point(2, 2, self.now + 86400), self.history_point(1, 5)]
        self.assertEqual(weekly_pace('claude', self.week(15), history, self.now)['burn_per_day'], 10)
        history = [self.history_point(1, 95)]
        self.assertEqual(weekly_pace('claude', self.week(5), history, self.now)['burn_per_day'], 0)

    def test_routing_missing_one_or_both_tools(self):
        for tool in ('claude', 'codex'):
            with self.subTest(tool=tool):
                routing = quota_routing({tool: [self.week()]}, [], self.now)
                self.assertEqual(routing['leader'], tool)
                self.assertEqual(routing['code_to'], tool)
                self.assertEqual(routing['design_to'], tool)
                other = 'codex' if tool == 'claude' else 'claude'
                self.assertIsNone(routing[other]['headroom'])
        routing = quota_routing({}, [], self.now)
        self.assertIsNone(routing['leader'])
        self.assertEqual((routing['code_to'], routing['design_to']), ('codex', 'claude'))

    def test_routing_thresholds_and_cheaper_tiers(self):
        def route(claude, codex):
            return quota_routing({'claude': [self.week(claude)], 'codex': [self.week(codex)]}, [], self.now)
        self.assertEqual(route(30, 40)['leader'], 'default')
        routing = route(30, 95)
        self.assertEqual((routing['leader'], routing['code_to'], routing['design_to']), ('claude', 'claude', 'claude'))
        self.assertEqual(routing['tiers']['claude'], dict(simple='Sonnet', hard='Opus'))
        routing = route(95, 30)
        self.assertEqual((routing['leader'], routing['code_to'], routing['design_to']), ('codex', 'codex', 'codex'))
        self.assertIn('ready specifications', routing['reason'])
        routing = route(95, 96)
        self.assertEqual(routing['leader'], 'claude')
        self.assertEqual(routing['tiers']['codex'], dict(simple='luna', hard='luna'))
        self.assertEqual(routing['tiers']['claude'], dict(simple='Sonnet', hard='Sonnet'))
        self.assertIn('deferred', routing['reason'])
        # At headroom 1 the strict reroute condition does not apply.
        self.assertEqual(route(30, 93)['code_to'], 'codex')
        routing = quota_routing({'claude': [self.week(30, 10)], 'codex': [self.week(88, 10)]}, [], self.now)
        self.assertEqual(routing['leader'], 'default')
        for claude, codex in ((30, 40), (30, 95), (95, 30), (95, 96)):
            self.assertLessEqual(set(route(claude, codex)['tiers']['codex'].values()), {'luna', 'sol'})

    def test_history_prunes_malformed_future_and_nonfinite_rows(self):
        path = self.home / '.claude/tools/limits-history.jsonl'
        valid = self.history_point(1, 20)
        for row in (valid, self.history_point(-1, 30), self.history_point(2, True),
                    self.history_point(2, float('nan')), [], {'at': float('inf')}):
            self.append(path, row)
        with path.open('a') as stream:
            stream.write('not json\n')
        windows = {'claude': [self.week(30)]}
        routing = self.source.routing(windows, self.now)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], valid)
        self.assertEqual(routing['claude']['burn_per_day'], 10)
        json.dumps(routing, allow_nan=False)

    def test_history_only_appends_changes_and_keeps_21_days(self):
        path = self.home / '.claude/tools/limits-history.jsonl'
        self.append(path, self.history_point(22, 10))
        self.append(path, self.history_point(20, 15))
        self.source.claude_limits = lambda: [self.week(30), dict(window_minutes=300, used_percent=10, resets_at=self.now + 3600)]
        self.source.codex_limits = lambda: []
        first = self.source.state()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['at'] >= self.now - 21 * 86400 for row in rows))
        self.assertEqual({row['window'] for row in rows}, {'5h', 'week'})
        before = path.read_bytes()
        self.now += 3600
        # Keep the measurements unchanged while advancing observation time.
        windows = [dict(window_minutes=10080, used_percent=30, resets_at=first['routing']['claude']['resets_at']),
                   dict(window_minutes=300, used_percent=10, resets_at=self.now)]
        self.source.claude_limits = lambda: windows
        self.source.state()
        self.assertEqual(path.read_bytes(), before)
        windows[0]['used_percent'] = 31
        current = self.source.state()
        self.assertEqual(len(path.read_text().splitlines()), 4)
        target = self.home / '.claude/tools/routing.json'
        self.assertEqual(json.loads(target.read_text()), current['routing'])
        windows[0]['resets_at'] += 7 * 86400
        self.source.state()
        self.assertEqual(len(path.read_text().splitlines()), 5)

    def test_codex_status_thresholds_and_pid(self):
        run = {'status': 'running', 'started': self.now - 1000}
        for age, alive, expected in [(0, True, 'working'), (120, True, 'working'), (121, True, 'working'),
                                      (600, True, 'working'), (601, True, 'hung'), (0, False, 'crashed')]:
            with self.subTest(age=age, alive=alive):
                self.assertEqual(codex_status(run, self.now, self.now - age, alive), expected)
        self.assertEqual(codex_status(run, self.now, None, True), 'hung')
        self.assertEqual(codex_status({'status': 'done'}, self.now, None, False), 'done')
        self.assertEqual(codex_status({'status': 'done', 'verdict': 'BLOCKED: missing input'}, self.now, None, True), 'blocked')
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(-1))

    def test_claude_activity_and_waiting(self):
        for age, last, expected in [(10, 'tool_use', 'working'), (10, 'end_turn', 'waiting'),
                                     (120, 'user', 'working'), (121, 'tool_use', None),
                                     (86399, 'end_turn', 'waiting'), (86400, 'end_turn', None)]:
            with self.subTest(age=age, last=last):
                self.assertEqual(claude_status(self.now - age, last, self.now), expected)

    def test_claude_elapsed_since_session_start_survives_notifications_and_polling(self):
        path = self.claude(stop='tool_use')
        self.source.state()  # Exercise the incremental summary too.
        self.append(path, dict(type='user', timestamp=self.now - 5, isMeta=True,
                               message={'content': 'A later task notification'}))
        self.append(path, dict(type='assistant', timestamp=self.now - 2,
                               message={'stop_reason': 'end_turn', 'content': 'Acknowledged'}))
        agent, = self.source.state()['agents']
        self.assertEqual(agent['status'], 'waiting')
        self.assertEqual(agent['seconds'], 100)
        self.now += 180
        self.assertEqual(self.source.state()['agents'][0]['seconds'], 280)

    def test_spawned_idle_session_is_done_but_top_level_waits(self):
        self.claude('top')
        worker = self.claude('worker')
        desktop = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b/local_worker.json'
        desktop.parent.mkdir(parents=True)
        desktop.write_text(json.dumps({'cliSessionId': 'worker', 'spawnedFrom': {'sessionId': 'local_parent', 'sideSession': True}}))
        agents = {a['id']: a for a in self.source.state()['agents']}
        self.assertEqual(agents['claude:top']['status'], 'waiting')
        self.assertEqual(agents['claude:worker']['status'], 'done')
        self.assertEqual(agents['claude:worker']['seconds'], 50)
        self.now += 60
        self.assertEqual({a['id']: a for a in self.source.state()['agents']}['claude:worker']['seconds'], 50)
        self.append(worker, dict(type='assistant', timestamp=self.now,
                                message={'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'test'}}]}))
        agents = {a['id']: a for a in self.source.state()['agents']}
        self.assertEqual(agents['claude:worker']['status'], 'working')
        self.assertEqual(agents['claude:worker']['seconds'], 160)
        # A relationship field without a parent ID is not evidence of a worker.
        desktop.write_text(json.dumps({'cliSessionId': 'top', 'spawnedFrom': {}}))
        self.assertEqual({a['id']: a for a in self.source.state()['agents']}['claude:top']['status'], 'waiting')

    def test_codex_elapsed_uses_current_time_or_completed_end(self):
        self.write_run(started=self.now - 125)
        self.assertEqual(self.source.state()['agents'][0]['seconds'], 125)
        self.now += 75
        self.assertEqual(self.source.state()['agents'][0]['seconds'], 200)
        self.write_run(status='done', started=self.now - 500, ended=self.now - 100)
        self.assertEqual(self.source.state()['completed'][0]['seconds'], 400)
        self.now += 75
        self.assertEqual(self.source.state()['completed'][0]['seconds'], 400)

    def test_claude_model_labels_preserve_raw_model(self):
        path = self.claude()
        for model, label in [('claude-opus-5-5', 'Claude Opus 5.5'), ('claude-sonnet-5-5', 'Claude Sonnet 5.5'),
                             ('claude-haiku-4-5-20251001', 'Claude Haiku 4.5'), ('claude-fable-5-5', 'Claude Fable 5.5')]:
            with self.subTest(model=model):
                self.append(path, dict(type='assistant', timestamp=self.now,
                                       message={'model': model, 'stop_reason': 'end_turn'}))
                agent, = self.source.state()['agents']
                self.assertEqual(agent['who'], label)
                self.assertEqual(agent['model'], model)

    def test_run_event_thread_action_context_and_no_source_writes(self):
        events = self.home / 'events.jsonl'
        self.append(events, {'type': 'thread.started', 'thread_id': 'thread-123'})
        self.append(events, {'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'python3\n test_now.py'}})
        self.write_run(events=str(events))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (events, self.runs / 'run.json')}
        agent, = self.source.state()['agents']
        self.assertEqual((agent['project'], agent['folder'], agent['who']), ('Northstar', 'dev', 'Codex sol'))
        self.assertEqual(agent['thread'], 'thread-123')
        self.assertEqual(agent['last_action'], 'python3 test_now.py')
        self.assertEqual(agent['seconds'], 300)
        self.assertIsNone(agent['context_tokens'])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        self.append(events, {'type': 'turn.completed', 'usage': {'input_tokens': 99999}})
        self.assertIsNone(self.source.state()['agents'][0]['context_tokens'])
        self.append(events, {'type': 'token_usage_record', 'usage': {'input_tokens': 42}})
        self.assertEqual(self.source.state()['agents'][0]['context_tokens'], 42)

    def test_missing_events_and_dead_running_run(self):
        self.write_run(events=str(self.home / 'missing'), started=self.now - 601)
        self.assertEqual(self.source.state()['agents'][0]['status'], 'hung')
        self.write_run(pid=8)
        self.assertEqual(self.source.state()['agents'][0]['status'], 'crashed')

    def test_completed_today_order_blocked_and_ledger_ignored(self):
        for name, ended, verdict in [('old', self.now - 86400, 'DONE'), ('a', self.now - 80, 'PARTIAL: checks'),
                                     ('b', self.now - 10, 'BLOCKED: input')]:
            self.write_run(name, status='done', ended=ended, verdict=verdict, head='a1b2c3d')
        (self.home / '.claude/tools/codex-ledger.jsonl').write_text('{"title":"untracked old run"}\n')
        state = self.source.state()
        self.assertFalse(state['agents'])
        self.assertEqual([a['id'] for a in state['completed']], ['codex:b', 'codex:a'])
        self.assertEqual(state['completed'][0]['status'], 'blocked')
        self.assertEqual(state['completed'][0]['head'], 'a1b2c3d')

    def test_desktop_titles_session_id_mapping_and_usage(self):
        path = self.claude()
        desktop = self.home / 'Library/Application Support/Claude/claude-code-sessions/account/org/local_desktop.json'
        desktop.parent.mkdir(parents=True)
        desktop.write_text(json.dumps({'cliSessionId': 'session', 'title': 'Harbor orchestrator', 'isArchived': False}))
        self.append(path, {'type': 'system', 'subtype': 'stop_hook_summary'})
        self.append(path, {'type': 'cost-state'})
        agent, = self.source.state()['agents']
        self.assertEqual((agent['task'], agent['title_source'], agent['project']), ('Harbor orchestrator', 'desktop', 'Harbor'))
        self.assertEqual((agent['status'], agent['context_tokens'], agent['seconds']), ('waiting', 122, 100))
        desktop.write_text(json.dumps({'cliSessionId': 'session', 'title': 'Renamed'}))
        self.assertEqual(self.source.state()['agents'][0]['task'], 'Renamed')
        desktop.write_text('{')
        self.assertEqual(self.source.state()['agents'][0]['task'], 'First task line')

    def test_only_recent_nonarchived_active_claude_sessions(self):
        self.claude('working', cwd=str(self.home / 'projects/Atlas-worktrees/interfaces'), stop='tool_use')
        self.claude('old', modified=self.now - 86401)
        self.claude('idle', modified=self.now - 121, stop='tool_use')
        self.claude('waiting', modified=self.now - 121)
        self.claude('archived')
        desktop = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b/local_archive.json'
        desktop.parent.mkdir(parents=True)
        desktop.write_text(json.dumps({'cliSessionId': 'archived', 'isArchived': True}))
        agents = self.source.state()['agents']
        self.assertEqual([a['id'] for a in agents], ['claude:working', 'claude:waiting'])
        self.assertEqual(agents[0]['project'], 'Atlas')

    def test_incremental_partial_malformed_and_truncated_records(self):
        events = self.home / 'events.jsonl'
        self.append(events, {'type': 'item.completed', 'item': {'text': 'First'}})
        self.write_run(events=str(events))
        self.assertEqual(self.source.state()['agents'][0]['last_action'], 'First')
        with events.open('a') as stream:
            stream.write('bad record\n[]\n{"type":"item.completed","item":{"text":"Second"}')
        self.assertEqual(self.source.state()['agents'][0]['last_action'], 'First')
        with events.open('a') as stream:
            stream.write('}\n')
        self.assertEqual(self.source.state()['agents'][0]['last_action'], 'Second')
        events.write_text('{"item":{"text":"New"}}\n')
        self.assertEqual(self.source.state()['agents'][0]['last_action'], 'New')
        (self.runs / 'bad.json').write_text('{')
        (self.runs / 'array.json').write_text('[]')
        self.assertEqual(len(self.source.state()['agents']), 1)

    def test_resume_cwd_read_only_lookup(self):
        codex_home = self.home / '.codex'
        codex_home.mkdir()
        db = codex_home / 'state_5.sqlite'
        connection = sqlite3.connect(db)
        connection.execute('CREATE TABLE threads(id TEXT, cwd TEXT)')
        connection.execute('INSERT INTO threads VALUES (?,?)', ('thread', str(self.home / 'projects/Atlas')))
        connection.commit()
        connection.close()
        self.source.codex = CodexSource(codex_home, sanitize)
        self.write_run(mode='resume', target='thread', tier='terra')
        before = db.read_bytes()
        agent, = self.source.state()['agents']
        self.assertEqual((agent['thread'], agent['project'], agent['who']), ('thread', 'Atlas', 'Codex terra'))
        self.assertEqual(before, db.read_bytes())

    def test_http_snapshot_and_same_origin_security(self):
        self.claude()
        self.write_run(title='<script>window.pwned=1</script> sk-abcdefghijklmnop')
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(now=self.source))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        self.addCleanup(connection.close)
        connection.request('GET', '/api/now')
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        state = json.loads(response.read())
        self.assertEqual(len(state['agents']), 2)
        self.assertIn('[СКРЫТО]', state['agents'][0]['task'])
        connection.request('GET', '/api/now', headers={'Origin': 'https://external.example'})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()



class WeekForecastTest(unittest.TestCase):
    def test_average_pace_projects_to_reset_and_finds_the_100_crossing(self):
        now, reset = 1_000_000.0, 1_000_000.0 + 2 * 86400
        week = dict(window_minutes=10080, used_percent=75, resets_at=reset)
        history = [dict(at=now - 86400, tool='codex', window='week', used_percent=60, resets_at=reset + 5),
                   dict(at=now - 3600, tool='codex', window='week', used_percent=74, resets_at=reset - 7 * 86400)]
        result = week_forecast('codex', week, history, now)
        self.assertEqual(result['points'], [[now - 86400, 60], [now, 75]])
        self.assertAlmostEqual(result['pace_per_day'], 15)
        self.assertAlmostEqual(result['projected_at_reset'], 105)
        self.assertAlmostEqual(result['hits_100_at'], now + 25 / 15 * 86400)

    def test_under_pace_has_no_crossing_and_missing_window_is_none(self):
        now = 1_000_000.0
        result = week_forecast('claude', dict(window_minutes=10080, used_percent=20, resets_at=now + 3.5 * 86400), [], now)
        self.assertAlmostEqual(result['projected_at_reset'], 40)
        self.assertIsNone(result['hits_100_at'])
        self.assertIsNone(week_forecast('claude', None, [], now))


class UsageCostTest(unittest.TestCase):
    def test_price_weighted_cost_counted_once_per_message(self):
        data = {}
        record = dict(type='assistant', timestamp='2026-11-06T10:30:00Z',
                      message=dict(id='m1', usage=dict(input_tokens=10, cache_creation_input_tokens=100,
                                                       cache_read_input_tokens=1000, output_tokens=20)))
        consume_usage(data, record)
        consume_usage(data, record)
        consume_usage(data, dict(type='user', message=dict(content='x')))
        self.assertEqual(list(data['hours'].values()), [10 + 125 + 100 + 100])


class CreditsTest(unittest.TestCase):
    def test_balance_changes_and_spend_rate(self):
        data = {}
        for minute, balance in [(0, 100), (1, 100), (2, 90), (12, 70), (200, 70), (210, 60)]:
            consume_credits(data, dict(timestamp=1000 + minute * 60, payload=dict(rate_limits=dict(credits=dict(balance=str(balance))))))
        self.assertEqual([b for _, b in data['points']], [100, 100, 90, 70, 70, 60])
        per_hour, spent, seconds = credit_spend(data['points'])
        self.assertEqual(spent, 40)
        self.assertEqual(seconds, 11 * 60 + 10 * 60)  # minute 1 to 12, then minute 200 to 210
        self.assertAlmostEqual(per_hour, 40 / (1260 / 3600))


class CreditRoutingTest(unittest.TestCase):
    def test_credits_raise_codex_headroom(self):
        now = 1_000_000.0
        week = lambda used: [dict(window_minutes=10080, used_percent=used, resets_at=now + 2 * 86400)]
        history = [dict(at=now - 86400, tool=t, window='week', used_percent=u, resets_at=now + 2 * 86400)
                   for t, u in (('claude', 50), ('codex', 70))]
        limits = dict(claude=week(60), codex=week(90))
        plain = quota_routing(limits, history, now)
        boosted = quota_routing(limits, history, now, dict(balance=4000, per_hour=400))
        self.assertAlmostEqual(boosted['codex']['credits_percent'], 4000 / 400 * 20 / 12)
        self.assertGreater(boosted['codex']['headroom'], plain['codex']['headroom'])


class BurnTest(unittest.TestCase):
    def test_codex_new_highs_per_hour_and_model(self):
        data = {}
        reset = 10 * 86400
        rows = [dict(type='turn_context', payload=dict(model='gpt-6.1-sol')),
                dict(type='event_msg', timestamp=3600 * 2 + 10, payload=dict(rate_limits=dict(primary=dict(used_percent=10, window_minutes=10080, resets_at=reset)))),
                dict(type='event_msg', timestamp=3600 * 2 + 20, payload=dict(rate_limits=dict(primary=dict(used_percent=12, window_minutes=10080, resets_at=reset)))),
                dict(type='turn_context', payload=dict(model='gpt-6.1-terra')),
                dict(type='event_msg', timestamp=3600 * 3 + 5, payload=dict(rate_limits=dict(primary=dict(used_percent=17, window_minutes=10080, resets_at=reset))))]
        for row in rows:
            consume_codex_meter(data, row)
        stale = [(3600 * 3 + 6, 11, reset, 'gpt-6.1-luna')]
        hours = codex_burn(data['usage'] + stale, 0, reset)
        self.assertEqual(hours, {7200: {'gpt-6.1-sol': 2}, 10800: {'gpt-6.1-terra': 5}})

    def test_claude_usage_is_split_by_model(self):
        from now_source import consume_usage
        data = {}
        for i, model in enumerate(['claude-opus-5-5', 'claude-fable-5-1', 'claude-opus-5-5']):
            consume_usage(data, dict(type='assistant', timestamp=3600 + i, message=dict(id=str(i), model=model, usage=dict(output_tokens=1))))
        self.assertEqual(data['models'], {3600: {'claude-opus-5-5': 10, 'claude-fable-5-1': 5}})

if __name__ == '__main__':
    unittest.main()
