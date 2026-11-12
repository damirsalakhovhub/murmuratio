"""Optional exact links, using isolated homes and local provider stubs only."""
from concurrent.futures import ThreadPoolExecutor
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from murmuratio_app.agent_registry import AgentRegistry, FIELDS, card_id, process_matches, registry_path
from murmuratio_app.adapters.registry import AgentsRegistryAdapter
from murmuratio_app.grouping import apply_registry
from murmuratio_app.spawn import spawn
from murmuratio_app.cli import Paths, status_value
from tests.test_board_http import BoardHTTPHarness

ROOT = Path(__file__).resolve().parent.parent

STUB = r'''import json, os, pathlib, signal, sys, time
kind = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
brief = sys.stdin.buffer.read()
home = pathlib.Path(os.environ['HOME'])
(home / 'capture.json').write_text(json.dumps({'kind':kind, 'argv':args, 'brief':brief.decode(), 'cwd':os.getcwd(), 'pid':os.getpid()}))
if os.environ.get('STUB_SLEEP'):
    print(json.dumps({'type':'thread.started', 'thread_id':'stub-thread'}), flush=True)
    time.sleep(60)
    sys.exit(0)
if os.environ.get('STUB_NO_ID'):
    print('no thread identity', flush=True)
    sys.exit(0)
now = time.time()
code = int(os.environ.get('STUB_EXIT', '0'))
if kind == 'codex':
    thread = 'stub-thread'
    root = pathlib.Path(os.environ['CODEX_HOME']) / 'sessions/2026/11/12'
    root.mkdir(parents=True, exist_ok=True)
    rows = [{'type':'session_meta', 'timestamp':now, 'payload':{'id':thread, 'cwd':os.getcwd(), 'source':'exec', 'originator':'codex_exec', 'timestamp':now}},
        {'type':'event_msg', 'timestamp':now, 'payload':{'type':'task_started'}},
        {'type':'turn_context', 'timestamp':now, 'payload':{'model':'effective-codex', 'effort':'medium'}},
        {'type':'event_msg', 'timestamp':now, 'payload':{'type':'task_complete'}}]
    (root / ('rollout-' + thread + '.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in rows))
    print(json.dumps({'type':'thread.started', 'thread_id':thread}), flush=True)
    print(json.dumps({'type':'item.completed', 'item':{'type':'agent_message', 'text':'Stub reply'}}), flush=True)
else:
    session = args[args.index('--session-id') + 1]
    root = home / '.claude/projects/stub'
    root.mkdir(parents=True, exist_ok=True)
    rows = [{'type':'user', 'timestamp':now, 'cwd':os.getcwd(), 'message':{'role':'user','content':brief.decode()}},
        {'type':'assistant', 'timestamp':now, 'cwd':os.getcwd(), 'message':{'role':'assistant','model':'effective-claude','content':[{'type':'text','text':'Stub reply'}],'stop_reason':'end_turn'}}]
    (root / (session + '.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in rows))
    print(json.dumps({'type':'system','subtype':'init','session_id':session,'model':'effective-claude'}), flush=True)
    print(json.dumps({'type':'result','subtype':'success' if code == 0 else 'error_during_execution','session_id':session,'is_error':code != 0,'result':'Stub reply'}), flush=True)
sys.exit(code)
'''


class ProviderStubs:
    def setup_stubs(self):
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        for kind in ('claude', 'codex'):
            binary = self.bin / kind
            binary.write_text('#!' + sys.executable + '\n' + STUB)
            binary.chmod(0o700)
        self.work = self.home / 'workspace'
        self.work.mkdir()
        self.brief = self.home / 'brief.txt'
        self.brief.write_bytes(b'A private brief\nwith exact stdin bytes.\n')
        self.env = {**os.environ, 'HOME': str(self.home), 'CODEX_HOME': str(self.codex_home),
                    'PATH': str(self.bin) + os.pathsep + os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1',
                    'CLAUDE_CODE_SESSION_ID': 'inherited-wrong-claude', 'CODEX_THREAD_ID': 'inherited-wrong-codex'}

    def cli(self, *args, input=None, extra_env=None):
        return subprocess.run([sys.executable, str(ROOT / 'murmuratio'), *map(str, args)], cwd=ROOT,
                              env={**self.env, **(extra_env or {})}, input=input, capture_output=True, text=True, timeout=15)

    def spawn_args(self, to='codex', parent_kind='claude', parent_id='parent', *extra):
        return ['spawn', '--to', to, '--title', 'Mode:   Keep title verbatim', '--model', 'requested-model',
                '--effort', 'high', '--parent-kind', parent_kind, '--parent-id', parent_id,
                '--cwd', str(self.work), *extra, str(self.brief)]


class LauncherTests(ProviderStubs, BoardHTTPHarness):
    def setUp(self):
        super().setUp()
        self.setup_stubs()

    def test_all_four_orchestration_directions_reach_api_with_exact_metadata(self):
        for parent_kind, to in (('claude', 'codex'), ('codex', 'claude'), ('claude', 'claude'), ('codex', 'codex')):
            with self.subTest(parent_kind=parent_kind, to=to), tempfile.TemporaryDirectory() as isolated:
                previous_home, previous_codex = self.home, self.codex_home
                self.home, self.codex_home = Path(isolated), Path(isolated) / '.codex'
                self.setup_stubs()
                self.assertEqual(self.cli('agent', 'register', '--kind', parent_kind, '--id', 'parent',
                    '--role', 'orchestrator', '--name', 'Coordinate changes').returncode, 0)
                result = self.cli(*self.spawn_args(to, parent_kind))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Stub reply', result.stdout)
                capture = json.loads((self.home / 'capture.json').read_text())
                self.assertEqual(capture['brief'].encode(), self.brief.read_bytes())
                self.assertEqual(capture['cwd'], str(self.work.resolve()))
                argv = capture['argv']
                self.assertEqual(argv[argv.index('--model') + 1], 'requested-model')
                self.assertNotIn('--sandbox', argv)
                self.assertNotIn('--permission-mode', argv)
                if to == 'codex':
                    self.assertIn('model_reasoning_effort="high"', argv)
                    self.assertEqual(argv[-1], '-')
                else:
                    self.assertEqual(argv[argv.index('--effort') + 1], 'high')
                    self.assertIn('--verbose', argv)
                self.start_server()
                cards = self.cards()
                worker = next(c for c in cards.values() if c.get('role') == 'worker')
                self.assertEqual((worker['parent_id'], worker['link']), (parent_kind + ':parent', 'exact'))
                self.assertEqual(worker['task'], 'Mode:   Keep title verbatim')
                self.assertEqual((worker['model_requested'], worker['effort_requested']), ('requested-model', 'high'))
                self.assertEqual((worker['exit_code'], worker['registry_status'], worker['status']), (0, 'done', 'done'))
                self.assertEqual(worker['model'], 'effective-' + to)
                self.assertTrue(cards[parent_kind + ':parent']['orchestrator'])
                self.assertEqual(len(cards), 2, 'one card per provider identity')
                stored = registry_path(self.home).read_text()
                self.assertNotIn('private brief', stored)
                self.assertNotIn('Stub reply', stored)
                self.home, self.codex_home = previous_home, previous_codex

    def test_provider_failures_are_recorded_even_with_complete_transcripts(self):
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                result = self.cli(*self.spawn_args(kind), extra_env={'STUB_EXIT': '7'})
                self.assertEqual(result.returncode, 7)
                self.start_server()
                worker = next(c for c in self.cards().values() if c['kind'] == kind)
                self.assertEqual((worker['exit_code'], worker['registry_status'], worker['status']), (7, 'failed', 'crashed'))

    def test_subagent_parent_is_exact_and_not_its_session_root(self):
        self.cli('agent', 'register', '--kind', 'claude', '--id', 'parent', '--role', 'orchestrator', '--name', 'Root')
        self.cli('agent', 'register', '--kind', 'claude', '--id', 'parent', '--subagent-id', 'agent1',
                 '--role', 'orchestrator', '--name', 'Nested coordinator')
        result = self.cli(*self.spawn_args('codex', 'claude', 'parent', '--parent-subagent-id', 'agent1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        worker = self.cards()['codex:stub-thread']
        self.assertEqual(worker['parent_id'], 'claude:parent:agent-agent1')

    def test_missing_parent_or_blank_identity_never_starts_provider(self):
        args = self.spawn_args()
        index = args.index('--parent-id')
        del args[index:index + 2]
        for arguments in (args, self.spawn_args(parent_id='')):
            result = self.cli(*arguments)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.home / 'capture.json').exists())
        self.assertFalse(registry_path(self.home).exists())

    def test_permission_flags_are_provider_specific(self):
        for kind, option, value in (('codex', '--sandbox', 'workspace-write'), ('claude', '--permission-mode', 'plan')):
            result = self.cli(*self.spawn_args(kind, 'claude', 'parent', option, value))
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads((self.home / 'capture.json').read_text())['argv']
            self.assertEqual(argv[argv.index(option) + 1], value)
        (self.home / 'capture.json').unlink()
        for kind, option, value in (('claude', '--sandbox', 'read-only'), ('codex', '--permission-mode', 'manual')):
            result = self.cli(*self.spawn_args(kind, 'claude', 'parent', option, value))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.home / 'capture.json').exists())

    def test_registry_permission_denial_prevents_process_start(self):
        path = registry_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text('')
        path.chmod(0o400)
        self.addCleanup(lambda: path.chmod(0o600))
        result = self.cli(*self.spawn_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / 'capture.json').exists())

    def test_codex_without_thread_id_fails_visibly(self):
        result = self.cli(*self.spawn_args(), extra_env={'STUB_NO_ID': '1'})
        self.assertEqual(result.returncode, 1)
        self.assertIn('without reporting its thread ID', result.stderr)
        card = next(iter(self.cards().values()))
        self.assertEqual((card['exit_code'], card['registry_status']), (1, 'failed'))

    def test_missing_executable_records_failed_launch(self):
        (self.bin / 'codex').unlink()
        self.env['PATH'] = str(self.bin)
        result = self.cli(*self.spawn_args())
        self.assertEqual(result.returncode, 127)
        card = next(iter(self.cards().values()))
        self.assertEqual((card['registry_status'], card['exit_code']), ('failed', 127))

    def test_termination_records_interrupted_and_stops_worker(self):
        process = subprocess.Popen([sys.executable, str(ROOT / 'murmuratio'), *self.spawn_args()],
            cwd=ROOT, env={**self.env, 'STUB_SLEEP': '1'}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while not (self.home / 'capture.json').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, error)
        stub_pid = json.loads((self.home / 'capture.json').read_text())['pid']
        with self.assertRaises(ProcessLookupError):
            os.kill(stub_pid, 0)
        snapshots = [json.loads(line) for line in registry_path(self.home).read_text().splitlines()]
        self.assertEqual((snapshots[-1]['status'], snapshots[-1]['exit_code']), ('interrupted', 143))

    def test_sigint_during_popen_stops_child_after_handle_is_captured(self):
        real_popen = subprocess.Popen
        children = []
        def launch_then_interrupt(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            os.kill(os.getpid(), signal.SIGINT)
            return process
        args = SimpleNamespace(to='codex', permission_mode=None, sandbox=None, model='requested-model',
            effort='high', title='Worker', parent_id='parent', parent_kind='claude',
            parent_subagent_id=None, cwd=str(self.work), brief_file=self.brief)
        with patch.dict(os.environ, self.env), patch('murmuratio_app.spawn.subprocess.Popen', launch_then_interrupt), \
             patch('murmuratio_app.spawn.sys.stdout', SimpleNamespace(buffer=io.BytesIO())):
            result = spawn(self.home, args)
        self.assertEqual(result, 130)
        child, = children
        self.assertIsNotNone(child.poll(), 'the spawned provider must be reaped after SIGINT')
        child.stdout.close()


class RegistryWriteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.registry = AgentRegistry(self.home)

    def test_registration_snapshot_and_permissions(self):
        record = self.registry.register('codex', 'thread', 'Coordinate changes')
        self.assertEqual(set(record), set(FIELDS))
        self.assertEqual((record['kind'], record['provider_id'], record['role']), ('codex', 'thread', 'orchestrator'))
        self.assertIsNone(record['model'])
        self.assertEqual(self.registry.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.registry.path.read_text()), record)

    def test_concurrent_merges_keep_complete_snapshots(self):
        record = self.registry.register('codex', 'thread', 'Coordinate changes')
        changes = [{'model_requested': 'requested-model'}, {'effort_requested': 'high'}] * 8
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda update: self.registry.write(update, record['id']), changes))
        rows = [json.loads(line) for line in self.registry.path.read_text().splitlines()]
        self.assertTrue(all(set(row) == set(FIELDS) for row in rows))
        self.assertEqual((rows[-1]['name'], rows[-1]['model_requested'], rows[-1]['effort_requested']),
                         ('Coordinate changes', 'requested-model', 'high'))

    def test_launcher_thread_join_coalesces_prior_explicit_registration(self):
        registered = self.registry.register('codex', 'joined-thread', 'Coordinator')
        launcher = self.registry.write(dict(kind='codex', role='worker', name='Worker task', via='launcher',
                                            status='running', started_at=time.time()))
        joined = self.registry.write(dict(provider_id='joined-thread', pid=123, pid_start=456), launcher['id'])
        self.assertEqual((joined['id'], joined['role'], joined['name'], joined['provider_id']),
                         (registered['id'], 'orchestrator', 'Coordinator', 'joined-thread'))
        rows = [json.loads(line) for line in self.registry.path.read_text().splitlines()]
        latest = {row['id']: row for row in rows}
        identities = [row for row in latest.values() if row['provider_id'] == 'joined-thread']
        self.assertEqual(len(identities), 1)

    def test_partial_tail_cannot_swallow_next_snapshot(self):
        record = self.registry.register('claude', 'session', 'Coordinate changes')
        with self.registry.path.open('ab') as stream:
            stream.write(b'{"id":"unfinished')
        self.registry.write({'model': 'observed-model'}, record['id'])
        rows = [json.loads(line) for line in self.registry.path.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['name'], record['name'])
        self.assertEqual(rows[-1]['model'], 'observed-model')

    def test_invalid_identity_or_multiline_title_is_rejected(self):
        for provider, name in (('', 'Title'), ('session', ''), ('session', 'Two\nlines')):
            with self.assertRaises(ValueError):
                self.registry.register('claude', provider, name)
        self.assertFalse(self.registry.path.exists())

    def test_symlink_is_not_written(self):
        target = self.home / 'user-data'
        target.write_text('keep')
        self.registry.path.parent.mkdir(parents=True)
        self.registry.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.registry.register('claude', 'session', 'Title')
        self.assertEqual(target.read_text(), 'keep')

    def test_reused_pid_is_not_a_live_worker(self):
        with patch('murmuratio_app.agent_registry.process_start', return_value=234):
            self.assertFalse(process_matches({'pid': 123, 'pid_start': 123}))
        self.assertIsNone(process_matches({'pid': 123, 'pid_start': None}))

    def test_subagent_registration_does_not_replace_session(self):
        root = self.registry.register('claude', 'session', 'Root')
        child = self.registry.register('claude', 'session', 'Nested coordinator', 'agent1')
        self.assertNotEqual(root['id'], child['id'])
        records = AgentsRegistryAdapter(self.registry.path).read().values()
        self.assertEqual({(r['provider_id'], r['subagent_id']) for r in records},
                         {('session', None), ('session', 'agent1')})

    def test_registration_preserves_launch_parent(self):
        record = self.registry.write(dict(kind='claude', provider_id='session', role='worker', name='Worker',
            parent_kind='codex', parent_id='thread', via='launcher'))
        promoted = self.registry.register('claude', 'session', 'Coordinator')
        self.assertEqual((promoted['id'], promoted['parent_id'], promoted['parent_kind']),
                         (record['id'], 'thread', 'codex'))


class HookAndInstallTests(ProviderStubs, BoardHTTPHarness):
    def setUp(self):
        super().setUp()
        self.setup_stubs()
        self.paths = Paths(self.home)

    def settings(self, raw):
        self.paths.settings.parent.mkdir(parents=True, exist_ok=True)
        self.paths.settings.write_bytes(raw)

    def hook(self, event, **fields):
        return self.cli('hook', 'claude', input=json.dumps(dict(hook_event_name=event, session_id='session',
                                                              cwd=str(self.work), **fields)))

    def files(self):
        return {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}

    def test_plain_install_keeps_integrations_opt_in(self):
        self.settings(b'{"theme":"dark"}\n')
        result = self.cli('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(self.paths.settings.read_bytes())
        self.assertNotIn('hooks', settings)
        self.assertFalse((self.home / '.claude/skills/murmuratio').exists())
        self.assertFalse((self.home / '.codex').exists())
        self.assertNotIn('Codex AGENTS.md block', result.stdout)

    def test_skill_is_installed_and_codex_block_only_printed(self):
        import shlex
        codex = self.home / '.codex/AGENTS.md'
        codex.parent.mkdir()
        original = b'# My existing Codex instructions\nKeep these exact bytes.\n'
        codex.write_bytes(original)
        result = self.cli('install', '--integrations')
        self.assertEqual(result.returncode, 0, result.stderr)
        skill = self.home / '.claude/skills/murmuratio/SKILL.md'
        self.assertTrue(skill.is_file())
        self.assertEqual(skill.stat().st_mode & 0o777, 0o600)
        content = skill.read_text()
        self.assertNotIn('@M@', content)
        self.assertIn('--kind claude --id "$CLAUDE_CODE_SESSION_ID"', content)
        self.assertIn('--parent-subagent-id <agent-id>', content)
        invocation = shlex.quote(sys.executable) + ' ' + shlex.quote(str(ROOT / 'murmuratio'))
        expected = (ROOT / 'integrations/codex/AGENTS.murmuratio.md').read_text().replace('@M@', invocation)
        self.assertIn(expected, result.stdout)
        self.assertEqual(codex.read_bytes(), original)
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertFalse(skill.exists())
        self.assertEqual(codex.read_bytes(), original)

    def test_existing_user_skill_is_a_preflight_conflict(self):
        skill = self.home / '.claude/skills/murmuratio/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_bytes(b'User-owned skill\n')
        before = self.files()
        result = self.cli('install', '--integrations')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.files(), before)

    def test_edited_installed_skill_preserves_every_file_on_uninstall_conflict(self):
        self.assertEqual(self.cli('install', '--integrations').returncode, 0)
        AgentRegistry(self.home).register('codex', 'parent', 'Coordinate changes')
        skill = self.home / '.claude/skills/murmuratio/SKILL.md'
        skill.write_bytes(skill.read_bytes() + b'User edit\n')
        before = self.files()
        result = self.cli('uninstall')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('skill was edited', result.stderr)
        self.assertEqual(self.files(), before)

    def test_interrupted_skill_creation_recovers_before_and_after_write(self):
        import murmuratio_app.cli as cli
        from murmuratio_app.hooks import install_integrations, skill_path
        for stop in ('before', 'after'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as home, redirect_stdout(io.StringIO()):
                paths = Paths(home)
                paths.settings.parent.mkdir()
                original = b'{"theme":"dark"}\n'
                paths.settings.write_bytes(original)
                args = argparse.Namespace(integrations=False, statusline=False, launch_agent=False, port=8765, dry_run=False)
                cli.install(paths, args)
                real_create = cli.create_new
                def interrupted(path, data):
                    if path == skill_path(paths):
                        if stop == 'after':
                            real_create(path, data)
                        raise SystemExit('interrupted skill creation')
                    return real_create(path, data)
                with patch.object(cli, 'create_new', side_effect=interrupted):
                    with self.assertRaises(SystemExit):
                        install_integrations(paths)
                args.integrations = True
                cli.install(paths, args)
                self.assertTrue(skill_path(paths).is_file())
                cli.uninstall(paths, argparse.Namespace(dry_run=False))
                self.assertEqual(paths.settings.read_bytes(), original)
                self.assertFalse(skill_path(paths).exists())
                self.assertFalse((paths.install / 'integrations.json').exists())

    def test_user_hooks_preserved_byte_for_byte_and_full_round_trip(self):
        original = (b'{\n  "hooks" : { "SessionStart": [ { "matcher" : "startup", "hooks": [ { "type":"command", "command":"echo user", "timeout":7 } ] } ],\n'
                    b'    "SubagentStart" : [], "SessionEnd": [{"hooks":[{"type":"command","command":"echo end"}]}] },\n'
                    b'  "theme" : "dark"\n}\n')
        self.settings(original)
        user_start = b'{ "matcher" : "startup", "hooks": [ { "type":"command", "command":"echo user", "timeout":7 } ] }'
        installed = self.cli('install', '--integrations')
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn(user_start, self.paths.settings.read_bytes())
        hooks = json.loads(self.paths.settings.read_bytes())['hooks']
        self.assertEqual(set(hooks), {'SessionStart', 'SubagentStart', 'SessionEnd'})
        for event in hooks:
            owned = hooks[event][-1]
            self.assertEqual(set(owned), {'hooks'})
            self.assertEqual(owned['hooks'][0]['timeout'], 2)
            self.assertTrue(owned['hooks'][0]['command'].endswith(' hook claude 2>/dev/null || true'))
        before = self.files()
        self.assertEqual(self.cli('install', '--integrations').returncode, 0)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(self.paths.settings.read_bytes(), original)
        self.assertFalse(self.paths.state.exists())
        self.assertFalse((self.paths.install / 'integrations.json').exists())

    def test_fresh_install_is_idempotent_and_uninstall_removes_created_settings(self):
        result = self.cli('install', '--integrations')
        self.assertEqual(result.returncode, 0, result.stderr)
        first = self.files()
        self.assertEqual(self.cli('install', '--integrations').returncode, 0)
        self.assertEqual(self.files(), first)
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertFalse(self.paths.settings.exists())
        self.assertFalse(self.paths.settings.parent.exists())

    def test_explicit_statusline_flag_works_without_claude_directory(self):
        result = self.cli('install', '--statusline')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('statusLine', json.loads(self.paths.settings.read_bytes()))
        self.assertNotIn('hooks', json.loads(self.paths.settings.read_bytes()))
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertFalse(self.paths.settings.exists())

    def test_hook_install_and_uninstall_dry_run_never_write(self):
        initial = self.files()
        result = self.cli('install', '--integrations', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('optional Claude hooks', result.stdout)
        self.assertEqual(self.files(), initial)
        self.assertEqual(self.cli('install', '--integrations').returncode, 0)
        installed = self.files()
        self.assertEqual(self.cli('uninstall', '--dry-run').returncode, 0)
        self.assertEqual(self.files(), installed)

    def test_uninstall_retains_unrelated_settings_edits_and_exact_user_hook_bytes(self):
        original = b'{ "hooks" : { "SessionStart" : [{ "hooks" : [{ "type":"command", "command":"cat" }] }] }, "theme":"dark" }\n'
        self.settings(original)
        self.assertEqual(self.cli('install', '--integrations').returncode, 0)
        self.paths.settings.write_bytes(self.paths.settings.read_bytes().replace(b'"dark"', b'"light"'))
        result = self.cli('uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = self.paths.settings.read_bytes()
        self.assertEqual(status_value(restored, 'hooks'), status_value(original, 'hooks'))
        self.assertEqual(json.loads(restored)['theme'], 'light')

    def test_uninstall_preserves_hooks_added_after_install(self):
        self.settings(b'{"theme":"dark"}\n')
        self.assertEqual(self.cli('install', '--integrations').returncode, 0)
        settings = json.loads(self.paths.settings.read_bytes())
        user = {'matcher': 'resume', 'hooks': [{'type': 'command', 'command': 'echo new user hook'}]}
        settings['hooks']['SessionStart'].append(user)
        settings['hooks']['Notification'] = [user]
        self.paths.settings.write_text(json.dumps(settings))
        result = self.cli('uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        remaining = json.loads(self.paths.settings.read_bytes())['hooks']
        self.assertEqual(remaining, {'SessionStart': [user], 'Notification': [user]})

    def test_modified_owned_hook_is_a_conflict_and_retains_backup_and_data(self):
        original = b'{"theme":"dark"}\n'
        self.settings(original)
        self.assertEqual(self.cli('install', '--integrations').returncode, 0)
        AgentRegistry(self.home).register('codex', 'parent', 'Coordinate changes')
        changed = self.paths.settings.read_bytes().replace(b'hook claude', b'hook changed', 1)
        self.paths.settings.write_bytes(changed)
        result = self.cli('uninstall')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('backup', result.stderr)
        self.assertEqual(self.paths.settings.read_bytes(), changed)
        self.assertEqual(self.paths.backup.read_bytes(), original)
        self.assertTrue((self.paths.install / 'integrations.json').exists())
        self.assertTrue(registry_path(self.home).exists())

    def test_invalid_hook_structure_rejected_before_default_install(self):
        for raw in (b'{"hooks": []}', b'{"hooks":{"SessionStart":{}}}'):
            self.settings(raw)
            result = self.cli('install', '--integrations')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.paths.settings.read_bytes(), raw)
            self.assertFalse(self.paths.state.exists())

    def test_owned_data_removed_or_kept_explicitly(self):
        for keep in (True, False):
            with self.subTest(keep=keep):
                self.assertEqual(self.cli('install', '--integrations').returncode, 0)
                AgentRegistry(self.home).register('codex', 'parent', 'Coordinate changes')
                result = self.cli('uninstall', *(['--keep-integration-data'] if keep else []))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(registry_path(self.home).exists(), keep)

    def test_hook_merges_available_fields_without_payload_storage(self):
        started = self.hook('SessionStart', model='observed-model', session_title='Hook title', secret='do not store')
        self.assertEqual((started.returncode, started.stdout), (0, ''))
        record = json.loads(registry_path(self.home).read_text())
        self.assertEqual((record['provider_id'], record['model'], record['status']), ('session', 'observed-model', 'running'))
        ended = self.hook('SessionEnd', reason='private reason', final_message='do not store')
        self.assertEqual(ended.returncode, 0)
        snapshots = [json.loads(line) for line in registry_path(self.home).read_text().splitlines()]
        self.assertEqual(snapshots[-1]['name'], 'Hook title')
        self.assertEqual(snapshots[-1]['status'], 'done')
        self.assertIsNotNone(snapshots[-1]['ended_at'])
        self.assertNotIn('do not store', registry_path(self.home).read_text())
        self.assertNotIn('private reason', registry_path(self.home).read_text())

    def test_subagent_hook_supplies_identity_without_duplicate_registry_record(self):
        result = self.hook('SubagentStart', agent_id='native-agent', agent_type='general-purpose', last_assistant_message='private')
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)['hookSpecificOutput']
        self.assertEqual(output['hookEventName'], 'SubagentStart')
        self.assertIn('session_id=session', output['additionalContext'])
        self.assertIn('agent_id=native-agent', output['additionalContext'])
        self.assertFalse(registry_path(self.home).exists())

    def test_hook_never_replaces_launcher_identity_title_role_or_exit_status(self):
        registry = AgentRegistry(self.home)
        run = registry.write(dict(kind='claude', provider_id='session', role='worker', name='Mode:   Literal',
            parent_kind='codex', parent_id='parent', status='starting', via='launcher',
            model_requested='requested-model', effort_requested='high'))
        self.hook('SessionStart', session_title='Wrong title', model='observed-model')
        self.hook('SessionEnd', final_message='private')
        latest = AgentsRegistryAdapter(registry.path).read().values()[0]
        self.assertEqual((latest['via'], latest['name'], latest['role'], latest['parent_id']),
                         ('launcher', 'Mode:   Literal', 'worker', 'parent'))
        self.assertEqual(latest['status'], 'starting')
        registry.write(dict(status='failed', exit_code=9, ended_at=self.now), run['id'])
        self.hook('SessionEnd')
        latest = AgentsRegistryAdapter(registry.path).read().values()[0]
        self.assertEqual((latest['status'], latest['exit_code'], latest['ended_at']), ('failed', 9, self.now))

    def test_hook_errors_always_exit_zero(self):
        for payload in ('not json', '[]', '{}', '{"session_id":null}', '{"hook_event_name":"SessionStart","session_id":"bad\\nid"}'):
            result = self.cli('hook', 'claude', input=payload)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        path = registry_path(self.home)
        path.parent.mkdir(parents=True)
        path.mkdir()
        result = self.hook('SessionStart')
        self.assertEqual((result.returncode, result.stderr), (0, ''))

    def test_installed_shell_command_handles_spaces_and_quotes(self):
        nested = self.home / "quoted home ' with spaces"
        nested.mkdir()
        self.home, self.codex_home = nested, nested / '.codex'
        self.env.update(HOME=str(nested), CODEX_HOME=str(self.codex_home))
        self.paths = Paths(nested)
        self.assertEqual(self.cli('install', '--integrations').returncode, 0)
        command = json.loads(self.paths.settings.read_bytes())['hooks']['SessionStart'][0]['hooks'][0]['command']
        result = subprocess.run(command, shell=True, input=json.dumps({'hook_event_name':'SessionStart', 'session_id':'quoted-session'}),
                                env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(registry_path(nested).read_text())['provider_id'], 'quoted-session')

    def test_interrupted_hook_install_is_retried_before_and_after_settings_write(self):
        import murmuratio_app.cli as cli
        from murmuratio_app.hooks import install_integrations
        for stop in ('before', 'after'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as home, redirect_stdout(io.StringIO()):
                paths = Paths(home)
                paths.settings.parent.mkdir()
                original = b'{ "theme":"dark", "hooks":{"SessionStart":[]} }\n'
                paths.settings.write_bytes(original)
                args = argparse.Namespace(integrations=False, statusline=False, launch_agent=False, port=8765, dry_run=False)
                cli.install(paths, args)
                real_write = cli.atomic_write
                def interrupted(path, data, mode=0o600):
                    if path == paths.settings:
                        if stop == 'after':
                            real_write(path, data, mode)
                        raise SystemExit('interrupted integration install')
                    return real_write(path, data, mode)
                with patch.object(cli, 'atomic_write', side_effect=interrupted):
                    with self.assertRaises(SystemExit):
                        install_integrations(paths)
                args.integrations = True
                cli.install(paths, args)
                self.assertEqual(len(json.loads(paths.settings.read_bytes())['hooks']['SessionStart']), 1)
                cli.uninstall(paths, argparse.Namespace(dry_run=False))
                self.assertEqual(paths.settings.read_bytes(), original)
                self.assertFalse(paths.state.exists())
                self.assertFalse((paths.install / 'integrations.json').exists())

    def test_interrupted_hook_uninstall_retains_journal_and_recovers_user_edits(self):
        import murmuratio_app.cli as cli
        from murmuratio_app.hooks import uninstall_integrations
        original = b'{ "hooks":{"SessionStart":[]}, "theme":"dark" }\n'
        self.settings(original)
        self.assertEqual(self.cli('install', '--integrations').returncode, 0)
        AgentRegistry(self.home).register('codex', 'parent', 'Coordinate changes')
        self.paths.settings.write_bytes(self.paths.settings.read_bytes().replace(b'"dark"', b'"light"'))
        real_write = cli.atomic_write
        def interrupted(path, data, mode=0o600):
            result = real_write(path, data, mode)
            if path == self.paths.settings:
                raise SystemExit('interrupted integration uninstall')
            return result
        with patch.object(cli, 'atomic_write', side_effect=interrupted):
            with self.assertRaises(SystemExit):
                uninstall_integrations(self.paths)
        self.assertTrue((self.paths.install / 'integrations.json').exists())
        result = self.cli('uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.paths.settings.read_bytes(), original.replace(b'"dark"', b'"light"'))
        self.assertFalse(registry_path(self.home).exists())
        self.assertFalse(self.paths.state.exists())

    def test_baseline_uninstall_conflicts_preserve_all_integration_files(self):
        original = b'{"theme":"dark"}\n'
        self.settings(original)
        self.assertEqual(self.cli('install', '--integrations').returncode, 0)
        AgentRegistry(self.home).register('codex', 'parent', 'Coordinate changes')
        backup = self.paths.backup.read_bytes()
        for changed in ('tampered', 'missing', 'agent'):
            with self.subTest(changed=changed):
                if changed == 'tampered':
                    self.paths.backup.write_bytes(b'{"changed":true}')
                elif changed == 'missing':
                    self.paths.backup.unlink()
                else:
                    state = json.loads(self.paths.state.read_bytes())
                    state['agent_sha'] = 'different-hash'
                    self.paths.state.write_text(json.dumps(state))
                    self.paths.agent.parent.mkdir(parents=True)
                    self.paths.agent.write_bytes(b'changed agent')
                before = self.files()
                result = self.cli('uninstall')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.files(), before)
                self.assertTrue(registry_path(self.home).exists())
                if changed != 'agent':
                    self.paths.backup.write_bytes(backup)


class IntegrationBoardTests(BoardHTTPHarness):
    def register_owned(self, kind, provider, name, **fields):
        record = dict(kind=kind, provider_id=provider, role='orchestrator', name=name,
                      cwd='/synthetic/project', via='manual', started_at=self.now - 20)
        record.update(fields)
        return AgentRegistry(self.home).write(record)

    def test_registry_effort_cannot_replace_current_transcript_unknown(self):
        self.register_owned('claude', 'parent', 'Coordinator', effort='high')
        self.register_owned('codex', 'thread', 'Coordinator', effort='high')
        # Verify missing transcript settings can still use an observed hook setting.
        self.assertEqual(self.cards()['claude:parent']['effort'], 'high')
        for value in ('low', None):
            with self.subTest(effort=value):
                self.claude('parent', [self.message('user', content='Coordinate'),
                    {**self.message('assistant', content='Working', stop_reason='tool_use'), 'perTurnEffort': value}])
                self.rollout('thread', records=[{'type': 'turn_context', 'payload': {'effort': value}},
                                                self.event('task_started')])
                cards = self.cards()
                for card_id in ('claude:parent', 'codex:thread'):
                    self.assertEqual(cards[card_id]['effort'], value)
                    self.assertNotIn('_effort_recorded', cards[card_id])

    def test_owned_registry_reports_corruption_and_recovers_after_append(self):
        self.register_owned('claude', 'parent', 'Coordinator')
        path = registry_path(self.home)
        good = path.read_text()
        for content, issue, visible in (
                ('{broken}\n', {'status': 'unsupported', 'reason': 'unrecognized_format', 'malformed': 1}, False),
                (good + '{broken}\n', {'status': 'available', 'malformed': 1}, True),
                (good + '{"id":', None, True),
                (good, None, True),
                ('{"id":', None, False),
                ('', None, False)):
            with self.subTest(content=content):
                path.write_text(content)
                state = self.api()
                self.assertEqual(state.get('sources', {}).get('integration_registry'), issue)
                self.assertEqual(any(card['id'] == 'claude:parent' for card in state['agents']), visible)

    def test_native_unknown_effort_survives_wrapper_and_registry_merges(self):
        self.config.modules['codex_wrapper'] = True
        self.start_server()
        self.register_owned('codex', 'thread', 'Coordinator', effort='high')
        self.rollout('thread', records=[{'type': 'turn_context', 'payload': {'effort': None}},
                                      self.event('task_started')])
        path = self.home / '.local/share/murmuratio/codex-runs/run.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(status='running', started=self.now - 30, thread='thread',
                                        effort='high', parent_session='fixture-launcher')))
        card = self.cards()['codex:run']
        self.assertIsNone(card['effort'])

    def test_registered_orchestrators_need_no_provider_transcript(self):
        self.register_owned('claude', 'parent', 'Coordinate changes')
        self.register_owned('codex', 'thread', 'Coordinate reviews')
        cards = self.cards()
        self.assertEqual(set(cards), {'claude:parent', 'codex:thread'})
        self.assertTrue(all(c['registered'] and c['orchestrator'] for c in cards.values()))

    def test_running_registered_orchestrator_uses_transcript_completion_without_hooks(self):
        self.claude('parent', [self.message('user', content='Coordinate changes'),
                               self.message('assistant', content='Finished', stop_reason='end_turn')])
        self.register_owned('claude', 'parent', 'Murmuratio orchestrator', status='running',
                            started_at=self.now - 3600, ended_at=None)
        card = self.cards()['claude:parent']
        self.assertTrue(card['completed'])
        self.assertEqual(card['status'], 'done')

    def test_old_unfinished_registered_coordinator_survives_retention(self):
        self.register_owned('claude', 'old-parent', 'Murmuratio orchestrator', status='running',
                            started_at=self.now - 3600, ended_at=None)
        card = self.cards()['claude:old-parent']
        self.assertEqual((card['status'], card['completed']), ('unknown', False))

    def test_owned_card_is_never_marked_guessed_by_legacy_coverage(self):
        legacy = self.home / '.claude/tools/agents-registry.jsonl'
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(dict(id='old', kind='claude', started_at=self.now - 100)) + '\n')
        self.register_owned('codex', 'thread', 'Coordinate changes')
        self.config.mark_guessed = True
        self.start_server()
        self.assertNotIn('guessed', self.cards()['codex:thread'])

    def test_owned_parent_is_exact_even_when_unresolved(self):
        self.rollout('worker')
        AgentRegistry(self.home).write(dict(kind='codex', provider_id='worker', role='worker', name='Mode:   literal title',
            parent_kind='claude', parent_id='unavailable', via='launcher', status='done', exit_code=0,
            started_at=self.now - 20, ended_at=self.now - 1, cwd='/synthetic/project'))
        card = self.cards()['codex:worker']
        self.assertEqual((card['parent_id'], card['link'], card['task']),
                         ('claude:unavailable', 'exact', 'Mode:   literal title'))
        self.assertEqual((card['status'], card['exit_code'], card['role']), ('done', 0, 'worker'))

    def test_explicit_orchestrator_retains_parent(self):
        self.register_owned('claude', 'parent', 'Coordinate changes')
        self.register_owned('codex', 'nested', 'Coordinate reviews', parent_kind='claude', parent_id='parent')
        card = self.cards()['codex:nested']
        self.assertTrue(card['orchestrator'])
        self.assertEqual((card['parent_id'], card['link']), ('claude:parent', 'exact'))

    def test_launched_orchestrator_finish_keeps_exact_exit_status_and_end_time(self):
        AgentRegistry(self.home).write(dict(kind='codex', provider_id='nested', name='Coordinate work',
            role='orchestrator', parent_kind='claude', parent_id='parent', cwd='/synthetic/project',
            status='done', exit_code=0, via='launcher', started_at=self.now - 20, ended_at=self.now - 1))
        card = self.cards()['codex:nested']
        self.assertEqual((card['status'], card['completed'], card['ended'], card['exit_code']), ('done', True, self.now - 1, 0))

    def test_legacy_orchestrator_override_keeps_owned_launch_completion(self):
        AgentRegistry(self.home).write(dict(kind='codex', provider_id='finished', name='Project orchestrator',
            role='orchestrator', cwd='/synthetic/project', status='done', exit_code=0, via='launcher',
            started_at=self.now - 20, ended_at=self.now - 1))
        legacy = self.home / '.claude/tools/agents-registry.jsonl'
        self.config.agents_registry = legacy
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps(dict(id='finished', kind='codex', role='orchestrator',
                                          name='Project orchestrator')) + '\n')
        card = self.cards()['codex:finished']
        self.assertEqual((card['status'], card['registry_status'], card['completed']), ('done', 'done', True))

    def test_owned_project_inheritance_is_independent_of_provider_order(self):
        for provider, parent in (('leaf', 'middle'), ('middle', 'root'), ('root', None)):
            AgentRegistry(self.home).write(dict(kind='codex', provider_id=provider, role='orchestrator', name=provider,
                parent_kind='codex' if parent else None, parent_id=parent, cwd='/synthetic/' + provider,
                via='manual', started_at=self.now - 20))
        self.assertEqual({card['project'] for card in self.cards().values()}, {'root'})

    def test_owned_same_title_orchestrator_is_not_collapsed_as_a_handoff(self):
        for name in ('root', 'nested'):
            self.desktop(name, title='Project orchestrator',
                         spawnedFrom={'sessionId': 'local_root'} if name == 'nested' else {})
            self.claude(name, [self.message('user', content='Coordinate changes')])
        self.register_owned('claude', 'root', 'Project orchestrator')
        self.register_owned('claude', 'nested', 'Project orchestrator', parent_kind='claude', parent_id='root')
        cards = self.cards()
        self.assertEqual(set(cards), {'claude:root', 'claude:nested'})
        self.assertEqual(cards['claude:nested']['parent_id'], 'claude:root')

    def test_lifecycle_hook_registration_does_not_disable_legacy_handoff_deduplication(self):
        for name in ('old', 'new'):
            self.desktop(name, title='Project orchestrator',
                         spawnedFrom={'sessionId': 'local_old'} if name == 'new' else {})
            self.claude(name, [self.message('user', content='Coordinate changes')])
        for name in ('old', 'new'):
            AgentRegistry(self.home).write(dict(kind='claude', provider_id=name, role=None,
                name='Project orchestrator', cwd='/synthetic/project', via='hook', status='running',
                started_at=self.now - (100 if name == 'old' else 50)))
        cards = self.cards()
        self.assertEqual(set(cards), {'claude:new'})

    def test_owned_and_configured_legacy_registry_are_both_read(self):
        self.config.agents_registry = self.home / 'legacy.jsonl'
        self.config.agents_registry.write_text(json.dumps(dict(id='legacy', kind='codex', role='orchestrator', name='Legacy')) + '\n')
        self.rollout('legacy')
        self.register_owned('claude', 'parent', 'Coordinate changes')
        self.start_server()
        cards = self.cards()
        self.assertTrue(cards['codex:legacy']['orchestrator'])
        self.assertTrue(cards['claude:parent']['orchestrator'])

    def test_subagent_identity_is_distinct_from_root(self):
        self.register_owned('claude', 'parent', 'Coordinate changes')
        AgentRegistry(self.home).register('claude', 'parent', 'Nested coordinator', 'agent1')
        cards = self.cards()
        child = cards['claude:parent:agent-agent1']
        self.assertEqual((child['parent_id'], child['task']), ('claude:parent', 'Nested coordinator'))
        self.assertEqual(cards['claude:parent']['task'], 'Coordinate changes')

    def test_pid_reuse_exposes_unknown_without_claiming_success(self):
        AgentRegistry(self.home).write(dict(kind='codex', provider_id='worker', role='worker', name='Check parser',
            parent_kind='claude', parent_id='parent', cwd='/synthetic/project', via='launcher', status='running',
            started_at=self.now - 20, pid=123, pid_start=123))
        with patch('murmuratio_app.agent_registry.process_start', return_value=234):
            card = self.cards()['codex:worker']
        self.assertEqual((card['status'], card['registry_status'], card['exit_code']), ('unknown', 'unknown', None))

    def test_stale_launch_with_incomplete_process_identity_is_unknown(self):
        for provider, status, pid in (('before-fork', 'starting', None), ('no-start-time', 'running', 123)):
            AgentRegistry(self.home).write(dict(kind='codex', provider_id=provider, role='worker', name='Check parser',
                parent_kind='claude', parent_id='parent', cwd='/synthetic/project', via='launcher', status=status,
                started_at=self.now - 60, pid=pid, pid_start=None))
        cards = self.cards()
        for provider in ('before-fork', 'no-start-time'):
            card = cards['codex:' + provider]
            self.assertEqual((card['status'], card['registry_status'], card['exit_code']), ('unknown', 'unknown', None))
