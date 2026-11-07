"""Installer and status-line contracts; all user data lives under a temporary HOME."""
import argparse
from contextlib import redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from murmuratio_app.adapters.quota_sources import CLAUDE_LIMITS_STALE_SECONDS, ClaudeLimitsAdapter, CodexLimitsAdapter
import murmuratio_app.cli as cli_module
from murmuratio_app.cli import DEFAULT_CONFIG, LABEL, Paths, install, uninstall
from murmuratio_app.config import Config, MODULES
from murmuratio_app.now import NowSource

ROOT = Path(__file__).resolve().parent.parent


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="inspector home '")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.paths = Paths(self.home)
        self.env = {**os.environ, 'HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        self.env.pop('PORTLESS_URL', None)

    def cli(self, *args):
        command = [sys.executable, str(ROOT / 'murmuratio')]
        return subprocess.run(command + list(args), env=self.env, cwd=ROOT, capture_output=True, text=True)

    def settings(self, content):
        self.paths.settings.parent.mkdir(parents=True, exist_ok=True)
        self.paths.settings.write_bytes(content)

    def tree(self):
        return {str(p.relative_to(self.home)): (p.read_bytes() if p.is_file() else None)
                for p in self.home.rglob('*')}

    def invoke_status(self, raw):
        settings = json.loads(self.paths.settings.read_bytes())
        return subprocess.run(settings['statusLine']['command'], shell=True, input=raw, env=self.env, capture_output=True)

    def test_version_works_outside_checkout_without_writing(self):
        result = subprocess.run([sys.executable, str(ROOT / 'murmuratio'), '--version'],
                                cwd=self.home, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'murmuratio 0.1.0-beta')
        self.assertEqual(self.tree(), {})

    def test_no_existing_statusline_and_uninstall_twice(self):
        original = b'{\n  "theme": "dark",\n  "env": {"KEEP": "unchanged"}\n}\n'
        self.settings(original)
        installed = self.cli('install')
        self.assertEqual(installed.returncode, 0, installed.stderr)
        current = json.loads(self.paths.settings.read_bytes())
        self.assertEqual(current['theme'], 'dark')
        self.assertEqual(current['env'], {'KEEP': 'unchanged'})
        self.assertEqual(self.paths.backup.read_bytes(), original)
        self.assertEqual(self.paths.backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.paths.config.read_bytes(), DEFAULT_CONFIG)
        self.assertFalse(self.paths.agent.exists())
        self.assertEqual(Config.load(self.home).modules, {name: False for name in MODULES})
        self.assertEqual(self.invoke_status(b'{}').stdout, b'Claude\n')
        first = self.tree()
        self.assertEqual(self.cli('install').returncode, 0)
        self.assertEqual(self.tree(), first)
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(self.paths.settings.read_bytes(), original)
        self.assertFalse(self.paths.config.exists())
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(self.paths.settings.read_bytes(), original)

    def test_missing_settings_is_removed_on_uninstall(self):
        self.paths.settings.parent.mkdir()
        self.assertEqual(self.cli('install').returncode, 0)
        self.assertTrue(self.paths.settings.is_file())
        self.assertFalse(self.paths.backup.exists())
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertFalse(self.paths.settings.exists())

    def test_previous_command_gets_identical_input_and_output_and_is_restored_byte_for_byte(self):
        previous = self.home / 'original status.py'
        previous.write_text('import sys\nraw=sys.stdin.buffer.read()\nsys.stdout.buffer.write(b"\\x1b[31m"+raw+b"\\xff\\n")\nsys.exit(7)\n')
        old = f'{shlex.quote(sys.executable)} {shlex.quote(str(previous))}'
        original = ('{\n  "statusLine" : { "type" : "command", "command" : ' +
                    json.dumps(old) + ', "padding": 4 },\n  "other": [1, 2]\n}\n').encode()
        self.settings(original)
        self.assertEqual(self.cli('install').returncode, 0)
        input_data = json.dumps({'session_id': 'must-not-be-cached', 'rate_limits': {
            'five_hour': {'used_percentage': 12.5, 'resets_at': 2000000000, 'private': 'ignored'},
            'seven_day': {'used_percentage': 30, 'resets_at': 2000001000},
            'spend_limit': {'used_percentage': 99}}}, separators=(',', ':')).encode() + b'\n'
        result = self.invoke_status(input_data)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, b'\x1b[31m' + input_data + b'\xff\n')
        cache = json.loads((self.paths.data / 'claude-limits.json').read_bytes())
        self.assertEqual(set(cache), {'at', 'rate_limits'})
        self.assertEqual(cache['rate_limits'], {
            'five_hour': {'used_percentage': 12.5, 'resets_at': 2000000000},
            'seven_day': {'used_percentage': 30, 'resets_at': 2000001000}})
        self.assertLess(abs(cache['at'] - time.time()), 5)
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(self.paths.settings.read_bytes(), original)

    def test_unrelated_settings_changes_are_preserved_and_original_status_value_is_exact(self):
        original = b'{"statusLine": { "type":"command", "command":"cat", "padding": 4 }, "theme":"dark"}\n'
        self.settings(original)
        self.assertEqual(self.cli('install').returncode, 0)
        edited = self.paths.settings.read_bytes().replace(b'"theme":"dark"', b'"theme":"light", "new":true')
        self.paths.settings.write_bytes(edited)
        self.assertEqual(self.cli('uninstall').returncode, 0)
        restored = self.paths.settings.read_bytes()
        self.assertIn(b'{ "type":"command", "command":"cat", "padding": 4 }', restored)
        self.assertEqual(json.loads(restored)['theme'], 'light')
        self.assertIs(json.loads(restored)['new'], True)

    def test_new_settings_with_unrelated_edits_loses_only_our_statusline(self):
        self.paths.settings.parent.mkdir()
        self.assertEqual(self.cli('install').returncode, 0)
        data = json.loads(self.paths.settings.read_bytes())
        data['theme'] = 'light'
        self.paths.settings.write_text(json.dumps(data))
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(json.loads(self.paths.settings.read_bytes()), {'theme': 'light'})

    def test_existing_or_modified_config_is_preserved_on_uninstall(self):
        self.paths.config.parent.mkdir(parents=True)
        self.paths.config.write_text('[modules]\nforecast = true\n')
        self.assertEqual(self.cli('install').returncode, 0)
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(self.paths.config.read_text(), '[modules]\nforecast = true\n')
        self.paths.config.unlink()
        self.assertEqual(self.cli('install').returncode, 0)
        self.paths.config.write_text('[modules]\nforecast = true\n')
        result = self.cli('uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.paths.config.read_text(), '[modules]\nforecast = true\n')

    def test_install_without_claude_then_uninstall_does_not_touch_later_settings(self):
        self.assertEqual(self.cli('install').returncode, 0)
        self.assertFalse(self.paths.settings.parent.exists())
        self.assertEqual(self.cli('install').returncode, 0)
        original = b'{"theme":"dark"}\n'
        self.settings(original)
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(self.paths.settings.read_bytes(), original)

    def test_reinstall_adds_hook_if_claude_appears_later(self):
        self.assertEqual(self.cli('install').returncode, 0)
        original = b'{"theme":"dark"}\n'
        self.settings(original)
        before = self.tree()
        plan = self.cli('install', '--dry-run')
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(self.tree(), before)
        self.assertIn('backup', plan.stdout)
        self.assertIn(f'change: {self.paths.settings}', plan.stdout)
        self.assertEqual(self.cli('install').returncode, 0)
        self.assertIn('statusLine', json.loads(self.paths.settings.read_bytes()))
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(self.paths.settings.read_bytes(), original)

    def test_interrupted_late_hook_install_recovers_before_and_after_settings_write(self):
        for stop in ('before', 'after'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as home:
                paths = Paths(home)
                args = argparse.Namespace(launch_agent=False, port=8765, dry_run=False)
                with redirect_stdout(io.StringIO()):
                    install(paths, args)
                    paths.settings.parent.mkdir()
                    original = b'{"theme":"dark"}\n'
                    paths.settings.write_bytes(original)
                    real_write, real_save = cli_module.atomic_write, cli_module.save_state
                    def interrupted_write(path, data, mode=0o600):
                        if stop == 'before' and path == paths.settings:
                            raise SystemExit('simulated interruption')
                        return real_write(path, data, mode)
                    def interrupted_save(target, state):
                        if stop == 'after' and state.get('hook_installed') and not state.get('hook_install'):
                            raise SystemExit('simulated interruption')
                        return real_save(target, state)
                    with patch.object(cli_module, 'atomic_write', side_effect=interrupted_write), \
                         patch.object(cli_module, 'save_state', side_effect=interrupted_save):
                        with self.assertRaises(SystemExit):
                            install(paths, args)
                    uninstall(paths, argparse.Namespace(dry_run=False))
                self.assertEqual(paths.settings.read_bytes(), original)
                self.assertFalse(paths.state.exists())

    def test_interrupted_late_hook_install_can_be_retried(self):
        args = argparse.Namespace(launch_agent=False, port=8765, dry_run=False)
        with redirect_stdout(io.StringIO()):
            install(self.paths, args)
            original = b'{"theme":"dark"}\n'
            self.settings(original)
            real_write = cli_module.atomic_write
            def interrupted_write(path, data, mode=0o600):
                if path == self.paths.settings:
                    raise SystemExit('simulated interruption')
                return real_write(path, data, mode)
            with patch.object(cli_module, 'atomic_write', side_effect=interrupted_write):
                with self.assertRaises(SystemExit):
                    install(self.paths, args)
            install(self.paths, args)
            self.assertIn('statusLine', json.loads(self.paths.settings.read_bytes()))
            uninstall(self.paths, argparse.Namespace(dry_run=False))
        self.assertEqual(self.paths.settings.read_bytes(), original)

    def test_interrupted_initial_hook_install_recovers(self):
        for stop in ('before', 'after'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as home:
                paths = Paths(home)
                paths.settings.parent.mkdir()
                original = b'{"theme":"dark"}\n'
                paths.settings.write_bytes(original)
                args = argparse.Namespace(launch_agent=False, port=8765, dry_run=False)
                real_write, real_save = cli_module.atomic_write, cli_module.save_state
                def interrupted_write(path, data, mode=0o600):
                    if stop == 'before' and path == paths.settings:
                        raise SystemExit('simulated interruption')
                    return real_write(path, data, mode)
                def interrupted_save(target, state):
                    if stop == 'after' and state.get('hook_installed') and not state.get('hook_install'):
                        raise SystemExit('simulated interruption')
                    return real_save(target, state)
                with redirect_stdout(io.StringIO()):
                    with patch.object(cli_module, 'atomic_write', side_effect=interrupted_write), \
                         patch.object(cli_module, 'save_state', side_effect=interrupted_save):
                        with self.assertRaises(SystemExit):
                            install(paths, args)
                    uninstall(paths, argparse.Namespace(dry_run=False))
                self.assertEqual(paths.settings.read_bytes(), original)
                self.assertFalse(paths.state.exists())
                self.assertFalse(paths.backup.exists())

    def test_uninstall_preserves_unowned_backup(self):
        for claude_exists in (False, True):
            with self.subTest(claude_exists=claude_exists), tempfile.TemporaryDirectory() as home:
                paths = Paths(home)
                if claude_exists:
                    paths.settings.parent.mkdir()
                args = argparse.Namespace(launch_agent=False, port=8765, dry_run=False)
                with redirect_stdout(io.StringIO()):
                    install(paths, args)
                    paths.backup.write_bytes(b'user-owned backup')
                    uninstall(paths, argparse.Namespace(dry_run=False))
                self.assertEqual(paths.backup.read_bytes(), b'user-owned backup')

    def test_uninstall_preserves_user_hard_link_to_created_config(self):
        self.assertEqual(self.cli('install').returncode, 0)
        other = self.paths.legacy_config_dir / 'config.toml'
        other.parent.mkdir(parents=True)
        os.link(self.paths.config, other)
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(self.paths.config.read_bytes(), DEFAULT_CONFIG)
        self.assertEqual(other.read_bytes(), DEFAULT_CONFIG)

    def test_dry_run_changes_nothing_and_lists_sources(self):
        self.settings(b'{"statusLine":{"type":"command","command":"cat"}}\n')
        before = self.tree()
        result = self.cli('install', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tree(), before)
        for path in (self.paths.settings, self.paths.backup, self.paths.state, self.paths.config,
                     self.paths.data / 'claude-limits.json', self.home / '.claude/tools/claude-limits.json',
                     self.home / '.claude/projects', self.home / '.codex/sessions'):
            self.assertIn(str(path), result.stdout)
        self.assertIn(str(self.home / '.claude/projects/*/*/subagents/*.jsonl'), result.stdout)
        self.assertEqual(self.cli('install').returncode, 0)
        before = self.tree()
        self.assertEqual(self.cli('uninstall', '--dry-run').returncode, 0)
        self.assertEqual(self.cli('doctor', '--dry-run').returncode, 0)
        self.assertEqual(self.cli('run', '--dry-run').returncode, 0)
        self.assertEqual(self.tree(), before)

    def test_malformed_and_unsupported_settings_are_refused_without_changes(self):
        for raw in (b'{oops', b'[]', b'{"statusLine": {"type":"future"}}',
                    b'{"statusLine": null, "statusLine": null}', b'{"x": NaN}'):
            with self.subTest(raw=raw):
                self.settings(raw)
                before = self.tree()
                for args in (('install',), ('install', '--dry-run')):
                    result = self.cli(*args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue('settings.json' in result.stderr)
                    self.assertEqual(self.tree(), before)

    def test_uninstall_refuses_changed_wrapper_or_backup(self):
        original = b'{"statusLine":{"type":"command","command":"cat"}}\n'
        self.settings(original)
        self.assertEqual(self.cli('install').returncode, 0)
        installed = self.paths.settings.read_bytes()
        data = json.loads(installed)
        data['statusLine']['command'] = 'echo changed'
        self.paths.settings.write_text(json.dumps(data))
        before = self.tree()
        self.assertNotEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(self.tree(), before)
        self.paths.settings.write_bytes(installed)
        self.paths.backup.write_bytes(b'changed')
        before = self.tree()
        self.assertNotEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(self.tree(), before)

    def test_cache_failure_and_missing_limits_do_not_break_previous_command(self):
        self.settings(b'{"statusLine":{"type":"command","command":"cat"}}')
        self.assertEqual(self.cli('install').returncode, 0)
        self.paths.data.mkdir(parents=True, exist_ok=True)
        cache = self.paths.data / 'claude-limits.json'
        cache.write_bytes(b'original cache')
        self.assertEqual(self.invoke_status(b'{"other":1}').stdout, b'{"other":1}')
        self.assertEqual(cache.read_bytes(), b'original cache')
        cache.unlink()
        cache.mkdir()  # os.replace fails, but the old command still runs.
        raw = b'{"rate_limits":{"five_hour":{"used_percentage":5}}}'
        self.assertEqual(self.invoke_status(raw).stdout, raw)

    def test_doctor_reports_tools_paths_and_cache_age(self):
        cache = self.paths.data / 'claude-limits.json'
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({'at': time.time() - 4000, 'rate_limits': {
            'five_hour': {'used_percentage': 21, 'resets_at': 2000000000}}}))
        before = self.tree()
        result = self.cli('doctor')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Claude:', result.stdout)
        self.assertIn('Codex:', result.stdout)
        self.assertIn(str(cache), result.stdout)
        self.assertIn('claude_limits: stale, age ', result.stdout)
        self.assertIn('claude_sessions: absent', result.stdout)
        self.assertIn('codex_sessions: absent', result.stdout)
        self.assertEqual(self.tree(), before)

    def test_doctor_distinguishes_absent_and_unreadable_sources(self):
        codex = self.home / '.codex'
        root = codex / 'sessions'
        root.mkdir(parents=True)
        scan = os.scandir
        def denied(path):
            if Path(path) == root:
                raise PermissionError('synthetic denial')
            return scan(path)
        output = io.StringIO()
        with patch.dict(os.environ, {'CODEX_HOME': str(codex)}), patch('os.scandir', side_effect=denied), redirect_stdout(output):
            cli_module.doctor(self.paths)
        self.assertIn('claude_sessions: absent', output.getvalue())
        self.assertIn('codex_sessions: unavailable', output.getvalue())

    def test_run_serves_loopback_with_temp_home(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, str(ROOT / 'murmuratio'), 'run', '--port', str(port)],
                                env={**self.env, 'PORTLESS_URL': 'http://foreign.localhost:1355'}, cwd=self.home,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            self.assertIn(f'http://127.0.0.1:{port}', line)
            with urlopen(f'http://127.0.0.1:{port}/', timeout=5) as response:
                self.assertEqual(response.status, 200)
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                connection.request('GET', '/', headers={'Host': 'foreign.localhost:1355'})
                self.assertEqual(connection.getresponse().status, 403)
            finally:
                connection.close()
        finally:
            proc.terminate()
            proc.communicate(timeout=5)

    def test_demo_child_never_writes_bytecode(self):
        env = dict(self.env)
        env.pop('PYTHONDONTWRITEBYTECODE', None)
        cache = self.home / 'bytecode'
        env['PYTHONPYCACHEPREFIX'] = str(cache)
        dry = subprocess.run([sys.executable, '-B', str(ROOT / 'murmuratio'), 'run', '--demo', '--dry-run'],
                             env=env, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(cache.exists())
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, '-B', str(ROOT / 'murmuratio'), 'run', '--demo', '--port', str(port)],
                                env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn(f'http://127.0.0.1:{port}', proc.stdout.readline())
            with urlopen(f'http://127.0.0.1:{port}/api/now', timeout=5) as response:
                self.assertEqual(response.status, 200)
            self.assertFalse(cache.exists())
        finally:
            proc.terminate()
            proc.communicate(timeout=5)

    def test_python_version_and_startup_errors_are_plain(self):
        program = "import runpy,sys; sys.version_info=(3,10,0); runpy.run_path(sys.argv[1], run_name='__main__')"
        old = subprocess.run([sys.executable, '-B', '-c', program, str(ROOT / 'murmuratio')],
                             env=self.env, capture_output=True, text=True)
        self.assertNotEqual(old.returncode, 0)
        self.assertEqual(old.stderr.splitlines(), ['Error: Python 3.11 or newer is required.'])
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            sock.listen()
            occupied = self.cli('run', '--port', str(sock.getsockname()[1]))
        self.assertNotEqual(occupied.returncode, 0)
        self.assertEqual(len(occupied.stderr.splitlines()), 1)
        self.assertIn('address already in use', occupied.stderr.lower())
        bad_port = self.cli('run', '--port', 'invalid')
        self.assertNotEqual(bad_port.returncode, 0)
        self.assertEqual(bad_port.stderr.splitlines(), ['Error: argument --port: port must be an integer'])
        config = self.home / 'invalid.toml'
        config.write_text('[modules\n')
        invalid = self.cli('run', '--config', str(config))
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(len(invalid.stderr.splitlines()), 1)
        self.assertTrue(invalid.stderr.startswith('Error: '))

    def test_opt_in_launch_agent_is_owned_and_uninstalled(self):
        args = argparse.Namespace(launch_agent=True, port=9876, dry_run=False)
        calls = []
        def launchctl(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1 if command[1] == 'print' and len(calls) <= 2 else 0)
        with patch('murmuratio_app.cli.sys.platform', 'darwin'), patch('murmuratio_app.cli.subprocess.run', side_effect=launchctl):
            install(self.paths, args)
            self.assertTrue(self.paths.agent.is_file())
            self.assertIn(b'9876', self.paths.agent.read_bytes())
            self.assertIn(LABEL, self.paths.agent.name)
            uninstall(self.paths, argparse.Namespace(dry_run=False))
        self.assertFalse(self.paths.agent.exists())
        self.assertTrue(any(command[1] == 'bootstrap' for command in calls))
        self.assertTrue(any(command[1] == 'bootout' for command in calls))

    def test_launch_agent_failure_leaves_installed_hook_recoverable(self):
        args = argparse.Namespace(launch_agent=True, port=8765, dry_run=False)
        def launchctl(command, **kwargs):
            return subprocess.CompletedProcess(command, 1)
        with patch('murmuratio_app.cli.sys.platform', 'darwin'), patch('murmuratio_app.cli.subprocess.run', side_effect=launchctl):
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(Exception, 'could not be loaded'):
                install(self.paths, args)
        self.assertTrue(self.paths.state.exists())
        self.assertFalse(self.paths.agent.exists())
        self.assertNotIn('agent_sha', json.loads(self.paths.state.read_text()))
        self.assertEqual(self.cli('uninstall').returncode, 0)

    def test_conflicting_launch_agent_is_not_changed(self):
        self.paths.agent.parent.mkdir(parents=True)
        self.paths.agent.write_bytes(b'another service')
        before = self.tree()
        args = argparse.Namespace(launch_agent=True, port=8765, dry_run=False)
        with patch('murmuratio_app.cli.sys.platform', 'darwin'):
            with self.assertRaisesRegex(Exception, 'already exists'):
                install(self.paths, args)
        self.assertEqual(self.tree(), before)

    def test_install_does_not_remove_a_racing_config(self):
        from murmuratio_app import cli as cli_module
        actual_create = cli_module.create_new
        def race(path, data):
            if path == self.paths.config:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(DEFAULT_CONFIG)
            return actual_create(path, data)
        args = argparse.Namespace(launch_agent=False, port=8765, dry_run=False)
        with patch('murmuratio_app.cli.create_new', side_effect=race):
            with self.assertRaises(FileExistsError):
                install(self.paths, args)
        self.assertEqual(self.paths.config.read_bytes(), DEFAULT_CONFIG)
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.settings.exists())


class LimitsCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.paths = Paths(self.home)
        self.legacy = self.home / '.claude/tools/claude-limits.json'
        self.cache = self.paths.data / 'claude-limits.json'
        self.now = 2000000
        self.adapter = ClaudeLimitsAdapter(self.cache, self.legacy, lambda: self.now)

    def write_cache(self, path, at, used=12):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'at': at, 'rate_limits': {'five_hour': {'used_percentage': used, 'resets_at': 3000000}}}))

    def test_R4_codex_limit_freshness_and_forecast_exclusion(self):
        path = self.home / '.codex/sessions/2026/11/07/rollout-test.jsonl'
        path.parent.mkdir(parents=True)
        def sample(at, reset=3000000):
            record = {'type': 'event_msg', 'payload': {'rate_limits': {'secondary': {
                'used_percent': 12, 'window_minutes': 10080, 'resets_at': reset}}}}
            if at is not None:
                record['timestamp'] = at
            path.write_text(json.dumps(record) + '\n')
        adapter = CodexLimitsAdapter(self.home / '.codex/sessions', clock=lambda: self.now)
        for at, reset, status in [(self.now - 1800, 3000000, 'available'),
                                  (self.now - 1801, 3000000, 'stale'),
                                  (None, 3000000, 'stale'), (self.now, self.now, 'stale')]:
            with self.subTest(at=at, reset=reset):
                sample(at, reset)
                self.assertEqual(adapter.read().status, status)
        sample(self.now - 1801)
        config = Config.load(self.home)
        config.modules['forecast'] = True
        config.modules['routing'] = True
        source = NowSource(self.home, config=config, clock=lambda: self.now)
        state = source.state()
        self.assertEqual(state['sources']['codex_limits']['status'], 'stale')
        self.assertIsNone(state['forecast']['codex'])
        self.assertIsNone(state['routing']['codex']['headroom'])

    def test_R3_and_R4_stale_fallback_does_not_hide_unreadable_cache(self):
        self.write_cache(self.legacy, self.now - 1801)
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text('{}')
        read_text = Path.read_text
        def deny_primary(path, *args, **kwargs):
            if path == self.cache:
                raise PermissionError('synthetic denial')
            return read_text(path, *args, **kwargs)
        config = Config.load(self.home)
        config.modules['forecast'] = True
        source = NowSource(self.home, config=config, clock=lambda: self.now)
        with patch.object(Path, 'read_text', deny_primary):
            state = source.state()
        self.assertEqual(state['sources']['claude_limits']['status'], 'unavailable')
        self.assertTrue(state['sources']['claude_limits']['stale'])
        self.assertIsNone(state['forecast']['claude'])

    def test_missing_and_stale_have_age_and_stale_is_not_used_for_forecasts(self):
        missing = self.adapter.read()
        self.assertEqual(missing.status, 'absent')
        self.assertIsNone(missing.age_seconds)
        self.write_cache(self.cache, self.now - CLAUDE_LIMITS_STALE_SECONDS - 1)
        stale = self.adapter.read()
        self.assertEqual(stale.status, 'stale')
        self.assertEqual(stale.age_seconds, CLAUDE_LIMITS_STALE_SECONDS + 1)
        self.assertEqual(stale.values()[0]['used_percent'], 12)
        config = Config.load(self.home)
        config.modules['forecast'] = True
        source = NowSource(self.home, config=config, clock=lambda: self.now)
        state = source.state()
        self.assertEqual(state['sources']['claude_limits'], {'status': 'stale', 'age_seconds': CLAUDE_LIMITS_STALE_SECONDS + 1})
        self.assertEqual(state['limits']['claude'][0]['used_percent'], 12)
        self.assertIsNone(state['forecast']['claude'])
        self.assertFalse(config.limits_history.exists())

    def test_newest_cache_wins_even_if_other_file_was_modified_later(self):
        self.write_cache(self.legacy, self.now - 5, used=20)
        self.write_cache(self.cache, self.now - 10, used=10)
        result = self.adapter.read()
        self.assertEqual(result.status, 'available')
        self.assertEqual(result.age_seconds, 5)
        self.assertEqual(result.values()[0]['used_percent'], 20)
        self.write_cache(self.cache, self.now - 1, used=30)
        self.assertEqual(self.adapter.read().values()[0]['used_percent'], 30)
        self.cache.write_text('{broken')
        self.assertEqual(self.adapter.read().values()[0]['used_percent'], 20)

    def test_unreadable_and_missing_cache_never_invents_values(self):
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text('{"at": 3, "rate_limits":{"five_hour":{"used_percentage":"unknown"}}}')
        result = self.adapter.read()
        self.assertEqual(result.status, 'unsupported')
        self.assertEqual(result.values(), [])
        self.cache.write_text('{"at": 3, "rate_limits":{}}')
        result = self.adapter.read()
        self.assertEqual(result.status, 'available')
        self.assertEqual(result.age_seconds, self.now - 3)

    def test_passed_reset_and_partially_invalid_old_cache_are_stale(self):
        self.write_cache(self.cache, self.now - 10)
        value = json.loads(self.cache.read_text())
        value['rate_limits']['five_hour']['resets_at'] = self.now - 1
        self.cache.write_text(json.dumps(value))
        self.assertEqual(self.adapter.read().status, 'stale')
        value['at'] = self.now - CLAUDE_LIMITS_STALE_SECONDS - 1
        value['rate_limits']['seven_day'] = {'used_percentage': 'unknown'}
        self.cache.write_text(json.dumps(value))
        result = self.adapter.read()
        self.assertEqual(result.status, 'unsupported')
        self.assertTrue(self.adapter.stale)
        self.assertEqual(result.values()[0]['used_percent'], 12)


if __name__ == '__main__':
    unittest.main()
