"""Configuration, optional modules and isolated demo mode."""
from datetime import datetime
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
import unittest
from unittest.mock import patch

from murmuratio_app.config import Config, MODULES
from murmuratio_app.now import NowSource
from murmuratio_app.adapters.parsing import consume_codex_meter, consume_usage
from murmuratio_app.metrics.quota import quota_routing
from murmuratio_app.grouping import retain_agents
from murmuratio_app import server


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / 'config.toml'

    def test_missing_file_is_public_and_groups_by_folder(self):
        config = Config.load(self.home, self.path)
        self.assertEqual(config.modules, {name: False for name in MODULES})
        self.assertEqual(config.project_order, [])
        self.assertFalse(hasattr(config, 'show_codex_chats'))
        self.assertFalse(hasattr(config, 'planner_drives_routing'))
        source = NowSource(self.home, config=config)
        self.assertEqual(source.project('/work/project-a'), 'project-a')
        self.assertEqual(source.project('/work/northstar'), 'northstar')
        self.assertEqual(source.project(''), 'Unknown')

    def test_retired_reset_option_is_ignored(self):
        key = 'context_' 'reset'
        self.path.write_text(f'[modules]\n{key} = true\n')
        self.assertNotIn(key, Config.load(self.home, self.path).modules)

    def test_retired_chat_option_is_ignored(self):
        for value in ('true', 'false', '"legacy"'):
            self.path.write_text(f'[modules]\ncodex_chats = {value}\n')
            self.assertNotIn('codex_chats', Config.load(self.home, self.path).modules)
            self.path.write_text(f'[board]\nshow_codex_chats = {value}\n')
            self.assertFalse(hasattr(Config.load(self.home, self.path), 'show_codex_chats'))

    def test_loads_custom_mappings_paths_and_hours(self):
        self.path.write_text('''[modules]
forecast = true
routing = true
[work_hours]
start = 8
end = 18
[paths]
codex_runs = "~/runs"
routing = "~/output/route.json"
limits_history = "~/output/history.jsonl"
codex_plan_errors = "~/output/plan-errors.jsonl"
[projects]
order = ["Example", "Other"]
fallback = "Other"
inherit_folders = ["notes"]
[[projects.mappings]]
path = "~/workspace/project"
name = "Example"
worktrees = true
[[projects.mappings]]
path = "~/harbor"
name = "Harbor"
after_inherit = true
''')
        config = Config.load(self.home, self.path)
        self.assertTrue(config.modules['routing'])
        self.assertFalse(config.modules['branding'])
        self.assertEqual(config.work_hours, (8, 18))
        self.assertEqual(config.wrapper_runs, self.home / 'runs')
        self.assertEqual(config.routing_file, self.home / 'output/route.json')
        self.assertEqual(config.limits_history, self.home / 'output/history.jsonl')
        self.assertFalse(hasattr(config, 'codex_plan_errors'))
        self.assertEqual(config.project_order, ['Example', 'Other'])
        source = NowSource(self.home, config=config)
        for directory in ('workspace/project', 'workspace/project/src', 'workspace/project-worktrees/fix'):
            self.assertEqual(source.project(str(self.home / directory)), 'Example')
        self.assertEqual(source.project(str(self.home / 'workspace/project2')), 'project2')
        self.assertEqual(source.project(str(self.home / 'notes/a')), '')
        self.assertEqual(source.project(str(self.home / 'harbor/tools')), 'Harbor')
        self.assertEqual(source.project(str(self.home / 'harbor/notes/a')), '')
        self.assertEqual(source.project(str(self.home / 'workspace/project/notes/a')), 'Example')
        self.assertEqual(source.project(''), 'Other')

    def test_invalid_values_fail_instead_of_silently_enabling_modules(self):
        for value in ('[modules]\nrouting = "yes"\n', '[work_hours]\nstart = 21\nend = 9\n',
                      '[projects]\norder = "project"\n',
                      'board = true\n', '[routing]\ncredits_expire = "13-01"\n',
                      '[routing]\ncredits_expire = 1231\n', 'routing = 1\n',
                      '[limits]\ncodex_live = "yes"\n', '[paths]\ncodex_cli = ""\n',
                      '[routing]\ncodex_resets = "2026-12-01T00:51+05:00"\n',
                      '[routing]\ncodex_resets = ["2026-12-01T00:51"]\n', '[routing]\ncodex_resets = ["soon"]\n'):
            self.path.write_text(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config.load(self.home, self.path)

    def test_live_codex_limits_are_off_by_default(self):
        config = Config.load(self.home, self.path)
        self.assertEqual((config.codex_live_limits, config.codex_cli), (False, None))
        self.path.write_text('[limits]\ncodex_live = true\n[paths]\ncodex_cli = "~/bin/codex"\n')
        config = Config.load(self.home, self.path)
        self.assertEqual((config.codex_live_limits, config.codex_cli), (True, self.home / 'bin/codex'))

    def test_default_paths_stay_in_the_data_folder(self):
        config = Config.load(self.home, self.path)
        data = self.home / '.local/share/murmuratio'
        self.assertEqual((config.wrapper_runs, config.routing_file, config.limits_history),
                         (data / 'codex-runs', data / 'routing.json', data / 'limits-history.jsonl'))
        self.assertFalse(hasattr(config, 'codex_plan_errors'))
        self.assertIsNone(config.claude_limits_extra)
        self.path.write_text('[paths]\nclaude_limits = "~/old/claude-limits.json"\n')
        self.assertEqual(Config.load(self.home, self.path).claude_limits_extra, self.home / 'old/claude-limits.json')

    def test_codex_full_resets_are_read_from_config(self):
        self.assertEqual(Config.load(self.home, self.path).codex_resets, ())
        self.path.write_text('[routing]\ncodex_resets = ["2026-12-02T03:26+05:00", "2026-12-01T00:51+05:00"]\n')
        self.assertEqual(Config.load(self.home, self.path).codex_resets, (1796068260.0, 1796163960.0))

    def test_retired_planner_settings_are_ignored(self):
        for flag in ('true', 'false'):
            self.path.write_text(f'[routing]\nplanner_drives = {flag}\n')
            self.assertFalse(hasattr(Config.load(self.home, self.path), 'planner_drives_routing'))

    def test_credit_expiry_defaults_to_december_31(self):
        self.assertEqual(Config.load(self.home, self.path).credits_expire, (12, 31))
        self.path.write_text('[routing]\ncredits_expire = "03-15"\n')
        self.assertEqual(Config.load(self.home, self.path).credits_expire, (3, 15))

    def test_routing_credit_capacity_uses_configured_workday(self):
        now = 1_000_000
        week = lambda percent: [dict(window_minutes=10080, used_percent=percent, resets_at=now+2*86400)]
        history = [dict(at=now-86400, tool='codex', window='week', used_percent=70, resets_at=now+2*86400)]
        result = quota_routing({'codex': week(90)}, history, now, {'balance': 4000, 'per_hour': 400}, 10)
        self.assertEqual(result['codex']['credits_percent'], 20)


class ModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = Config.load(self.home)
        self.now = 2_000_000
        self.source = NowSource(self.home, clock=lambda: self.now, config=self.config)
        self.week = dict(window_minutes=10080, used_percent=45, resets_at=self.now + 2*86400)
        self.source.claude_limits = lambda: [self.week]
        self.source.codex_limits = lambda: [self.week]
        self.source.desktop_sessions = lambda: {}

    def enable_all(self):
        self.config.modules.update({name: True for name in MODULES})

    def test_default_snapshot_skips_optional_sources_and_has_no_optional_data(self):
        self.config.wrapper_runs.mkdir(parents=True)
        (self.config.wrapper_runs / 'run.json').write_text('sentinel')
        self.config.routing_file.write_text('unchanged')
        with patch.object(self.source, 'run', side_effect=AssertionError('wrapper read')), \
             patch.object(self.source, '_collect_codex_usage') as codex_usage, \
             patch.object(self.source, 'routing', side_effect=AssertionError('history read')), \
             patch.object(self.source, 'burn_by_model', side_effect=AssertionError('burn read')), \
             patch.object(self.source, '_collect_claude_usage') as claude_usage:
            state = self.source.state()
            codex_usage.assert_not_called()
            claude_usage.assert_not_called()
        self.assertTrue({'routing', 'forecast', 'burn', 'credits'}.isdisjoint(state))
        self.assertEqual(self.config.routing_file.read_text(), 'unchanged')
        self.assertFalse(self.config.limits_history.exists())

    def test_cold_usage_scan_does_not_block_and_populates_later(self):
        self.config.modules['codex_credits'] = True
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def slow_scan(*args):
            calls.append(args)
            started.set()
            release.wait(5)
            finished.set()
            return 'available', ({'balance': 80}, [], [])
        self.addCleanup(release.set)
        with patch.object(self.source, '_collect_codex_usage', side_effect=slow_scan):
            before = time.monotonic()
            first = self.source.state()
            self.assertLess(time.monotonic() - before, 1)
            self.assertTrue(started.wait(1))
            self.assertIsNone(first['credits'])
            for _ in range(3):
                self.assertIsNone(self.source.state()['credits'])
            self.assertEqual(len(calls), 1)
            release.set()
            self.assertTrue(finished.wait(1))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = self.source.state()
                if state['credits'] is not None:
                    break
                time.sleep(0.01)
            self.assertEqual(state['credits'], {'balance': 80})
            self.assertEqual(len(calls), 1)

    def test_routing_file_waits_for_the_cold_credit_scan_and_live_read(self):
        # Right after a restart the credit scan had not finished, so routing.json was written with no
        # credit rate (Codex looked exhausted) and a stale Codex sample.
        self.config.modules.update(routing=True, codex_credits=True)
        self.config.codex_live_limits = True
        release = threading.Event()
        self.addCleanup(release.set)
        def slow_scan(*args):
            release.wait(5)
            return 'available', ({'balance': 80, 'per_hour': 10}, [], [])
        def live():
            release.wait(5)
            return 'available', (self.now, [self.week], 80)
        target = self.config.routing_file
        with patch.object(self.source, '_collect_codex_usage', side_effect=slow_scan), \
                patch.object(self.source, '_collect_codex_live', side_effect=live):
            state = self.source.state()
            self.assertIsNotNone(state['routing'])
            self.assertFalse(target.exists(), 'partial routing is not written')
            release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not target.exists():
                self.source.state()
                time.sleep(0.01)
        self.assertEqual(json.loads(target.read_text())['codex']['used_percent'], 45)

    def test_concurrent_requests_share_the_first_completed_snapshot(self):
        started, second_started, release = threading.Event(), threading.Event(), threading.Event()
        calls, results = [], []
        original_clock = time.monotonic_ns
        def stamp():
            value = original_clock()
            if threading.current_thread().name == 'second-request':
                second_started.set()
            return value
        def slow_desktop():
            calls.append(1)
            started.set()
            release.wait(2)
            return {}
        self.source.desktop_sessions = slow_desktop
        self.addCleanup(release.set)
        with patch('murmuratio_app.now.time.monotonic_ns', side_effect=stamp):
            first = threading.Thread(target=lambda: results.append(self.source.state()), name='first-request')
            second = threading.Thread(target=lambda: results.append(self.source.state()), name='second-request')
            first.start()
            self.assertTrue(started.wait(1))
            second.start()
            self.assertTrue(second_started.wait(1))
            release.set()
            first.join(2)
            second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])

    def test_routing_off_never_reads_or_writes_routing_file(self):
        self.enable_all()
        self.config.modules['routing'] = False
        self.config.routing_file.parent.mkdir(parents=True)
        self.config.routing_file.write_text('unchanged')
        with patch('murmuratio_app.now.quota_routing', side_effect=AssertionError('routing calculated')):
            state = self.source.state()
        self.assertNotIn('routing', state)
        self.assertIn('forecast', state)
        self.assertIn('burn', state)
        self.assertEqual(self.config.routing_file.read_text(), 'unchanged')

    def test_forecast_off_does_not_expose_it_when_burn_is_on(self):
        self.enable_all()
        self.config.modules['forecast'] = False
        state = self.source.state()
        self.assertNotIn('forecast', state)
        self.assertIn('burn', state)
        self.assertIn('routing', state)

    def test_scenarios_off_does_not_collect_worker_split(self):
        self.config.modules.update(forecast=True, scenarios=False, burn_rate=False)
        with patch.object(self.source, '_collect_claude_usage') as collect:
            state = self.source.state()
            collect.assert_not_called()
        self.assertNotIn('worker_share', state['forecast']['claude'])

    def test_scenarios_off_does_not_calculate_split_when_burn_is_on(self):
        self.config.modules.update(forecast=True, scenarios=False, burn_rate=True)
        called = threading.Event()
        def collect(since, desktop, split, burn):
            self.assertFalse(split)
            self.assertTrue(burn)
            called.set()
            return 'available', (None, {})
        with patch.object(self.source, '_collect_claude_usage', side_effect=collect):
            state = self.source.state()
            self.assertTrue(called.wait(1))
        self.assertIn('burn', state)
        self.assertNotIn('worker_share', state['forecast']['claude'])

    def test_credit_and_burn_parsers_can_be_disabled_independently(self):
        record = dict(type='event_msg', timestamp=self.now, payload=dict(rate_limits=dict(
            credits=dict(balance='100'), primary=self.week)))
        with patch('murmuratio_app.adapters.parsing.consume_credits', side_effect=AssertionError('credits parsed')):
            data = {}
            consume_codex_meter(data, record, include_credits=False)
        self.assertIn('usage', data)
        self.assertNotIn('points', data)
        data = {}
        consume_codex_meter(data, record, include_burn=False)
        self.assertIn('points', data)
        self.assertNotIn('usage', data)
        model_data = {}
        consume_usage(model_data, dict(type='assistant', timestamp=self.now,
            message=dict(id='m', model='model', usage=dict(input_tokens=10))), include_models=False)
        self.assertNotIn('models', model_data)

    def test_pending_approval_survives_midnight(self):
        midnight = datetime.fromtimestamp(self.now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        old = midnight - 3600
        pending = dict(id='pending', status='blocked', current_action='waiting for approval', last_at=old)
        other = dict(id='other', status='blocked', current_action='', last_at=old)
        self.assertEqual([a['id'] for a in retain_agents([pending, other], midnight+300)], ['pending'])

    def test_wrapper_off_avoids_run_files_even_when_other_modules_on(self):
        self.enable_all()
        self.config.modules['codex_wrapper'] = False
        with patch.object(self.source, 'run', side_effect=AssertionError('wrapper read')):
            self.source.state()


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler(config=self.config))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def get(self, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        connection.request('GET', path)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_branding_off_does_not_load_or_serve_logos(self):
        original = Path.read_bytes
        def guarded(path):
            if path.name == 'branding.html':
                raise AssertionError('disabled branding was read')
            return original(path)
        with patch.object(Path, 'read_bytes', guarded):
            status, body = self.get('/')
        self.assertEqual(status, 200)
        self.assertNotIn(b'id="now-codex"', body)
        self.config.modules['branding'] = True
        self.assertIn(b'id="now-codex"', self.get('/')[1])

    def test_removed_chat_endpoints_are_not_available(self):
        for route in ('/api/codex/state', '/api/codex/session?id=x', '/api/codex/event?id=x&event=y',
                      '/api/codex/reset/status?id=x', '/panels.js', '/activity.js'):
            self.assertEqual(self.get(route)[0], 404)
        config = json.loads(self.get('/api/config')[1])
        self.assertNotIn('codex_chats', config['modules'])


class DemoTests(unittest.TestCase):
    def test_demo_never_resolves_home_or_opens_personal_paths(self):
        calls = []
        original_open = Path.open
        fixture = server.ROOT / 'fixtures/demo.json'
        class FakeServer:
            server_port = 1
            def __init__(self, address, handler):
                self.handler = handler
            def serve_forever(self):
                raise KeyboardInterrupt
            def server_close(self):
                pass
        def guarded_open(path, *args, **kwargs):
            calls.append(path)
            if path != fixture:
                raise AssertionError(f'unexpected file read: {path}')
            return original_open(path, *args, **kwargs)
        with patch('murmuratio_app.server.ThreadingHTTPServer', FakeServer), \
             patch.object(Path, 'home', side_effect=AssertionError('home accessed')), \
             patch.object(Path, 'open', guarded_open), \
             patch('sys.argv', ['server.py', '--demo', '--config', '/personal/config.toml',
                                '--codex-home', '/personal/codex']):
            server.main()
        self.assertEqual(calls, [fixture])
        state = json.loads(fixture.read_text())
        self.assertTrue(any(a.get('orchestrator') for a in state['agents']))
        self.assertTrue(any(a['status'] == 'blocked' and a.get('parent_id') for a in state['agents']))
        self.assertTrue(state['completed'])
        self.assertTrue(state['limits'])

    def test_demo_exposes_fresh_modules_and_no_optional_values(self):
        captured = {}
        class FakeServer:
            server_port = 1
            def __init__(self, address, handler):
                pass
            def serve_forever(self):
                raise KeyboardInterrupt
            def server_close(self):
                pass
        def handler(public_url, now, config):
            captured.update(config=config, state=now.state())
            return object()
        with patch('murmuratio_app.server.ThreadingHTTPServer', FakeServer), patch('murmuratio_app.server.make_handler', side_effect=handler), \
             patch('sys.argv', ['server.py', '--demo']):
            server.main()
        self.assertEqual(captured['config'].modules, Config().modules)
        self.assertEqual(captured['config'].project_order, [])
        self.assertTrue({'forecast', 'routing', 'burn', 'credits'}.isdisjoint(captured['state']))


if __name__ == '__main__':
    unittest.main()
