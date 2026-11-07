"""Black-box migration and compatibility checks with isolated home directories."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from murmuratio_app.cli import DEFAULT_CONFIG, InstallError, LABEL, LEGACY_LABEL, Paths, install, migrate, move_directory, uninstall

ROOT = Path(__file__).resolve().parent.parent


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.paths = Paths(self.home)
        self.env = {**os.environ, 'HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        self.env.pop('PORTLESS_URL', None)
        self.env.pop('PORT', None)

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(ROOT / 'murmuratio'), *args], cwd=ROOT,
                              env=self.env, text=True, capture_output=True, timeout=15)

    @staticmethod
    def put(path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)

    def test_run_moves_both_directories_once_and_flushes_message(self):
        config = '[modules]\nforecast = true\n'
        self.put(self.paths.legacy_config_dir / 'config.toml', config)
        self.put(self.paths.legacy_data / 'reset' / 'history.json', 'old data')
        before = self.cli('run', '--demo', '--dry-run')
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertIn('Would migrate', before.stdout)
        self.assertTrue(self.paths.legacy_config_dir.exists())
        self.assertTrue(self.paths.legacy_data.exists())
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, '-B', str(ROOT / 'murmuratio'), 'run', '--demo', '--port', str(port)],
                                env=self.env, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'Migrated legacy configuration and/or data to murmuratio.')
            self.assertIn(f'http://127.0.0.1:{port}', proc.stdout.readline())
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            connection.request('GET', '/api/now')
            self.assertEqual(connection.getresponse().status, 200)
            connection.close()
        finally:
            proc.terminate()
            proc.communicate(timeout=5)
        self.assertEqual(self.paths.config.read_text(), config)
        self.assertEqual((self.paths.data / 'reset' / 'history.json').read_text(), 'old data')
        self.assertFalse(self.paths.legacy_config_dir.exists())
        self.assertFalse(self.paths.legacy_data.exists())
        repeated = self.cli('run', '--demo', '--dry-run')
        self.assertNotIn('migrate', repeated.stdout.lower())

    def test_install_moves_each_directory_and_dry_run_never_moves(self):
        for directory in ('config', 'data'):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as home:
                env = {**self.env, 'HOME': home}
                paths = Paths(home)
                old = paths.legacy_config_dir if directory == 'config' else paths.legacy_data
                new = paths.config_dir if directory == 'config' else paths.data
                self.put(old / 'sentinel', directory)
                command = [sys.executable, '-B', str(ROOT / 'murmuratio'), 'install']
                dry = subprocess.run(command + ['--dry-run'], env=env, capture_output=True, text=True)
                self.assertEqual(dry.returncode, 0, dry.stderr)
                self.assertTrue(old.exists())
                self.assertFalse(new.exists())
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count('Migrated legacy'), 1)
                self.assertEqual((new / 'sentinel').read_text(), directory)
                self.assertFalse(old.exists())

    def test_dry_run_shows_sources_from_config_to_be_migrated(self):
        self.put(self.paths.legacy_config_dir / 'config.toml', '[modules]\nforecast = true\n')
        for command in ('install', 'run'):
            result = self.cli(command, '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(self.home / '.claude/tools/limits-history.jsonl'), result.stdout)
            self.assertNotIn(f'create: {self.paths.config}', result.stdout)
            self.assertTrue(self.paths.legacy_config_dir.exists())
            self.assertFalse(self.paths.config_dir.exists())

    def test_invalid_settings_do_not_move_legacy_directories(self):
        self.put(self.paths.legacy_config_dir / 'config.toml', '[modules]\nforecast = true\n')
        self.put(self.paths.legacy_data / 'sentinel', 'old')
        self.put(self.paths.settings, '{broken')
        result = self.cli('install')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.paths.legacy_config_dir.exists())
        self.assertEqual((self.paths.legacy_data / 'sentinel').read_text(), 'old')
        self.assertFalse(self.paths.config_dir.exists())
        self.assertFalse(self.paths.data.exists())

    def test_explicit_legacy_config_path_follows_migration(self):
        old_config = self.paths.legacy_config_dir / 'config.toml'
        self.put(old_config, '[modules]\nforecast = true\n')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, '-B', str(ROOT / 'murmuratio'), 'run', '--port', str(port),
                                 '--config', str(old_config)], env=self.env, cwd=ROOT, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertIn('Migrated legacy', proc.stdout.readline())
            self.assertIn(f'http://127.0.0.1:{port}', proc.stdout.readline())
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            connection.request('GET', '/api/config')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())['modules']['forecast'])
            connection.close()
            self.assertFalse(old_config.exists())
            self.assertTrue(self.paths.config.exists())
        finally:
            proc.terminate()
            proc.communicate(timeout=5)

    def test_existing_destinations_survive_install_and_uninstall(self):
        for directory in (self.paths.legacy_config_dir, self.paths.legacy_data):
            self.put(directory / 'sentinel', 'old')
        for directory in (self.paths.config_dir, self.paths.data):
            self.put(directory / 'sentinel', 'new')
        result = self.cli('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        for directory in (self.paths.legacy_config_dir, self.paths.legacy_data):
            self.assertEqual((directory / 'sentinel').read_text(), 'old')
        for directory in (self.paths.config_dir, self.paths.data):
            self.assertEqual((directory / 'sentinel').read_text(), 'new')
        dry = self.cli('uninstall', '--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertTrue(self.paths.legacy_data.exists())
        result = self.cli('uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        for directory in (self.paths.config_dir, self.paths.legacy_config_dir, self.paths.data, self.paths.legacy_data):
            self.assertTrue((directory / 'sentinel').is_file())

    def test_install_finds_old_record_when_new_data_directory_exists(self):
        original = b'{"statusLine":{"type":"command","command":"cat"},"theme":"dark"}\n'
        self.paths.settings.parent.mkdir(parents=True)
        self.paths.settings.write_bytes(original)
        self.assertEqual(self.cli('install').returncode, 0)
        installed = self.paths.settings.read_bytes()
        self.paths.data.rename(self.paths.legacy_data)
        self.put(self.paths.data / 'sentinel', 'new')
        result = self.cli('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Already installed', result.stdout)
        self.assertEqual(self.paths.settings.read_bytes(), installed)
        result = self.cli('uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.paths.settings.read_bytes(), original)
        self.assertEqual((self.paths.data / 'sentinel').read_text(), 'new')
        self.assertFalse(self.paths.legacy_data.exists())

    def test_legacy_record_never_deletes_independent_new_config(self):
        for old_record in (False, True):
            with self.subTest(old_record=old_record), tempfile.TemporaryDirectory() as home:
                paths = Paths(home)
                env = {**self.env, 'HOME': home}
                command = [sys.executable, '-B', str(ROOT / 'murmuratio')]
                first = subprocess.run(command + ['install'], cwd=ROOT, env=env, capture_output=True, text=True)
                self.assertEqual(first.returncode, 0, first.stderr)
                paths.config_dir.rename(paths.legacy_config_dir)
                paths.data.rename(paths.legacy_data)
                if old_record:
                    state_path = paths.legacy_data / 'install/installation.json'
                    state = json.loads(state_path.read_text())
                    state.pop('created_config_identity')
                    state_path.write_text(json.dumps(state))
                paths.config.parent.mkdir(parents=True)
                paths.config.write_bytes(DEFAULT_CONFIG)
                migrated = subprocess.run(command + ['install'], cwd=ROOT, env=env, capture_output=True, text=True)
                self.assertEqual(migrated.returncode, 0, migrated.stderr)
                removed = subprocess.run(command + ['uninstall'], cwd=ROOT, env=env, capture_output=True, text=True)
                self.assertEqual(removed.returncode, 0, removed.stderr)
                self.assertEqual(paths.config.read_bytes(), DEFAULT_CONFIG)
                legacy_config = paths.legacy_config_dir / 'config.toml'
                self.assertEqual(legacy_config.exists(), old_record)

    def test_no_record_uninstall_preserves_old_and_new_directories(self):
        for directory in (self.paths.config_dir, self.paths.legacy_config_dir, self.paths.data, self.paths.legacy_data):
            self.put(directory / 'sentinel', 'data')
        result = self.cli('uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        for directory in (self.paths.config_dir, self.paths.legacy_config_dir, self.paths.data, self.paths.legacy_data):
            self.assertEqual((directory / 'sentinel').read_text(), 'data')

    def test_conflicting_installation_records_are_preserved(self):
        old = self.paths.legacy_data / 'install' / 'installation.json'
        self.put(old, 'old record')
        self.put(self.paths.state, 'new record')
        self.put(self.paths.legacy_config_dir / 'sentinel', 'config')
        for command in ('install', 'uninstall'):
            result = self.cli(command)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(old.read_text(), 'old record')
            self.assertEqual(self.paths.state.read_text(), 'new record')
            self.assertTrue(self.paths.legacy_config_dir.exists())

    def test_uninstall_resumes_after_cleanup_fails_without_losing_settings(self):
        for original in (None, b'{"theme":"dark"}\n'):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as home:
                paths = Paths(home)
                if original is not None:
                    paths.settings.parent.mkdir(parents=True)
                    paths.settings.write_bytes(original)
                with redirect_stdout(StringIO()):
                    install(paths, argparse.Namespace(launch_agent=False, port=8765, dry_run=False))
                    with patch('murmuratio_app.cli.remove_installation_files', side_effect=PermissionError('blocked')):
                        with self.assertRaises(PermissionError):
                            uninstall(paths, argparse.Namespace(dry_run=False))
                self.assertTrue(paths.state.exists())
                if original is None:
                    self.assertFalse(paths.settings.exists())
                else:
                    self.assertEqual(paths.settings.read_bytes(), original)
                with redirect_stdout(StringIO()):
                    uninstall(paths, argparse.Namespace(dry_run=False))
                self.assertFalse(paths.state.exists())
                self.assertFalse(paths.config_dir.exists())
                self.assertFalse(paths.data.exists())
                if original is not None:
                    self.assertEqual(paths.settings.read_bytes(), original)

    def test_uninstall_resumes_after_backup_was_removed(self):
        self.paths.settings.parent.mkdir(parents=True)
        original = b'{"theme":"dark"}\n'
        self.paths.settings.write_bytes(original)
        args = argparse.Namespace(launch_agent=False, port=8765, dry_run=False)
        real_unlink = Path.unlink
        def fail_state(path, *args, **kwargs):
            if path == self.paths.state:
                raise PermissionError('simulated interruption')
            return real_unlink(path, *args, **kwargs)
        with redirect_stdout(StringIO()):
            install(self.paths, args)
            with patch.object(Path, 'unlink', fail_state):
                with self.assertRaises(PermissionError):
                    uninstall(self.paths, argparse.Namespace(dry_run=False))
            self.assertTrue(self.paths.state.exists())
            self.assertFalse(self.paths.backup.exists())
            self.assertEqual(self.paths.settings.read_bytes(), original)
            uninstall(self.paths, argparse.Namespace(dry_run=False))
        self.assertFalse(self.paths.state.exists())

    def test_exclusive_directory_move_refuses_even_an_empty_destination(self):
        self.put(self.paths.legacy_data / 'sentinel', 'old')
        self.paths.data.mkdir(parents=True)
        with self.assertRaises(OSError):
            move_directory(self.paths.legacy_data, self.paths.data)
        self.assertEqual((self.paths.legacy_data / 'sentinel').read_text(), 'old')
        self.assertEqual(list(self.paths.data.iterdir()), [])

    def test_existing_statusline_does_not_prevent_data_migration(self):
        self.put(self.paths.legacy_data / 'sentinel', 'old')
        raw = '{"rate_limits":{"five_hour":{"used_percentage":12}}}'
        command = [sys.executable, '-B', str(ROOT / 'statusline_wrapper.py')]
        result = subprocess.run(command, env=self.env, input=raw, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.paths.legacy_data / 'claude-limits.json').is_file())
        self.assertFalse(self.paths.data.exists())
        result = self.cli('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.paths.data / 'sentinel').read_text(), 'old')
        self.assertTrue((self.paths.data / 'claude-limits.json').is_file())
        self.assertFalse(self.paths.legacy_data.exists())
        result = subprocess.run(command, env=self.env, input=raw, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.paths.data / 'claude-limits.json').is_file())
        self.assertFalse(self.paths.legacy_data.exists())

    def test_portless_opt_in_uses_proxy_port_and_rejects_other_hosts(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {**self.env, 'PORT': str(port), 'PORTLESS_URL': 'http://preview.localhost:1355'}
        proc = subprocess.Popen([sys.executable, '-B', str(ROOT / 'murmuratio'), 'run', '--demo', '--portless'],
                                env=env, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertIn(f'http://127.0.0.1:{port}', proc.stdout.readline())
            for host, expected in (('preview.localhost:1355', 200), ('foreign.localhost:1355', 403)):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/', headers={'Host': host})
                self.assertEqual(connection.getresponse().status, expected)
                connection.close()
        finally:
            proc.terminate()
            proc.communicate(timeout=5)

    def legacy_agent(self):
        result = self.cli('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = plistlib.dumps({'Label': LEGACY_LABEL, 'ProgramArguments': [sys.executable, str(ROOT / 'agent-inspector'),
                             'run', '--port', '9876'], 'RunAtLoad': True, 'KeepAlive': True})
        self.paths.legacy_agent.parent.mkdir(parents=True, exist_ok=True)
        self.paths.legacy_agent.write_bytes(plist)
        state = json.loads(self.paths.state.read_text())
        state['agent_sha'] = hashlib.sha256(plist).hexdigest()
        self.paths.state.write_text(json.dumps(state))
        self.paths.data.rename(self.paths.legacy_data)
        migrate(self.paths)
        self.paths.select_install()
        return state, plist

    def test_existing_launch_agent_migrates_and_remains_uninstallable(self):
        self.legacy_agent()
        calls = []
        def launchctl(command, **kwargs):
            calls.append(command)
            if command[1] == 'print' and command[-1].endswith('/' + LABEL):
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)
        with patch('murmuratio_app.cli.sys.platform', 'darwin'), patch('murmuratio_app.cli.subprocess.run', side_effect=launchctl):
            with redirect_stdout(StringIO()) as output:
                install(self.paths, argparse.Namespace(launch_agent=False, port=8765, dry_run=True))
            self.assertIn('replace and reload', output.getvalue())
            self.assertTrue(self.paths.legacy_agent.exists())
            self.assertFalse(self.paths.agent.exists())
            install(self.paths, argparse.Namespace(launch_agent=False, port=8765, dry_run=False))
        self.assertFalse(self.paths.legacy_agent.exists())
        agent = plistlib.loads(self.paths.agent.read_bytes())
        self.assertEqual(agent['Label'], LABEL)
        self.assertEqual(agent['ProgramArguments'][1:], [str(ROOT / 'murmuratio'), 'run', '--port', '9876'])
        self.assertTrue(any(c[1] == 'bootout' and c[-1].endswith(LEGACY_LABEL) for c in calls))
        self.assertEqual(json.loads(self.paths.state.read_text())['agent_sha'], hashlib.sha256(self.paths.agent.read_bytes()).hexdigest())
        with patch('murmuratio_app.cli.sys.platform', 'darwin'), patch('murmuratio_app.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
            uninstall(self.paths, argparse.Namespace(dry_run=False))
        self.assertFalse(self.paths.agent.exists())
        self.assertFalse(self.paths.data.exists())

    def test_launch_agent_migration_failure_restores_old_agent_and_record(self):
        state, original = self.legacy_agent()
        calls = []
        def launchctl(command, **kwargs):
            calls.append(command)
            if command[1] == 'print' and command[-1].endswith('/' + LABEL):
                return subprocess.CompletedProcess(command, 1)
            if command[1] == 'bootstrap' and command[-1] == str(self.paths.agent):
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)
        with patch('murmuratio_app.cli.sys.platform', 'darwin'), patch('murmuratio_app.cli.subprocess.run', side_effect=launchctl):
            with self.assertRaises(InstallError):
                install(self.paths, argparse.Namespace(launch_agent=False, port=8765, dry_run=False))
        self.assertEqual(self.paths.legacy_agent.read_bytes(), original)
        self.assertFalse(self.paths.agent.exists())
        self.assertEqual(json.loads(self.paths.state.read_text())['agent_sha'], state['agent_sha'])
        self.assertTrue(any(c[1] == 'bootstrap' and c[-1] == str(self.paths.legacy_agent) for c in calls))

    def test_failed_agent_rollback_reports_service_was_not_restarted(self):
        self.legacy_agent()
        def launchctl(command, **kwargs):
            if command[1] == 'print' and command[-1].endswith('/' + LABEL):
                return subprocess.CompletedProcess(command, 1)
            if command[1] == 'bootstrap':
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)
        with patch('murmuratio_app.cli.sys.platform', 'darwin'), patch('murmuratio_app.cli.subprocess.run', side_effect=launchctl):
            with self.assertRaisesRegex(InstallError, 'Could not restart the old LaunchAgent'):
                install(self.paths, argparse.Namespace(launch_agent=False, port=8765, dry_run=False))
        self.assertTrue(self.paths.legacy_agent.exists())
        self.assertFalse(self.paths.agent.exists())


if __name__ == '__main__':
    unittest.main()
