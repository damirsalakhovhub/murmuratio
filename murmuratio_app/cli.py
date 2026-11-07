"""Install, remove, diagnose and run the local inspector."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from murmuratio_app.discovery import Discovery
from murmuratio_app.common import PlainArgumentParser
from murmuratio_config import Config, MODULES

ROOT = Path(__file__).resolve().parent.parent
LABEL = 'org.murmuratio.app'
LEGACY_LABEL = 'org.agent-inspector.app'
DEFAULT_CONFIG = ('# Optional modules are disabled by default.\n[modules]\n' +
                  ''.join(f'{name} = false\n' for name in MODULES)).encode()


class InstallError(Exception):
    pass


class Paths:
    def __init__(self, home):
        self.home = Path(home)
        self.settings = self.home / '.claude/settings.json'
        self.config_dir = self.home / '.config/murmuratio'
        self.legacy_config_dir = self.home / '.config/agent-inspector'
        self.config = self.config_dir / 'config.toml'
        self.data = self.home / '.local/share/murmuratio'
        self.legacy_data = self.home / '.local/share/agent-inspector'
        self.install = self.data / 'install'
        self.backup = self.install / 'settings.json.backup'
        self.state = self.install / 'installation.json'
        self.agent = self.home / f'Library/LaunchAgents/{LABEL}.plist'
        self.legacy_agent = self.home / f'Library/LaunchAgents/{LEGACY_LABEL}.plist'

    def select_install(self):
        legacy = self.legacy_data / 'install'
        old_state = legacy / 'installation.json'
        if (old_state.exists() or old_state.is_symlink()) and (self.state.exists() or self.state.is_symlink()):
            raise InstallError('Both old and new installation records exist; retain both and resolve the conflict')
        if old_state.exists() or old_state.is_symlink():
            self.install = legacy
            self.backup = legacy / 'settings.json.backup'
            self.state = old_state


def move_directory(old, new):
    """Atomically rename without replacing a destination created concurrently."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(old), os.fsencode(new), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(old), -100, os.fsencode(new), 1)  # RENAME_NOREPLACE
    else:
        raise InstallError('This platform cannot safely migrate directories without replacement')
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(old), str(new))


def migrate(paths, dry_run=False):
    moves = []
    for old, new in ((paths.legacy_config_dir, paths.config_dir), (paths.legacy_data, paths.data)):
        if old.is_symlink() or new.is_symlink():
            raise InstallError(f'Refusing to migrate a symlink: {old if old.is_symlink() else new}')
        if old.exists() and not new.exists():
            if not old.is_dir():
                raise InstallError(f'Expected a directory: {old}')
            moves.append((old, new))
    if dry_run:
        if moves:
            print('Would migrate legacy configuration and/or data to murmuratio.')
        return []
    moved = []
    try:
        for old, new in moves:
            new.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if new.exists() or new.is_symlink():
                raise InstallError(f'Migration destination appeared: {new}')
            move_directory(old, new)
            moved.append((old, new))
    except (OSError, InstallError):
        for old, new in reversed(moved):
            if not old.exists() and not old.is_symlink():
                move_directory(new, old)
        raise
    if moved:
        print('Migrated legacy configuration and/or data to murmuratio.', flush=True)
    return moved


def planned_config(paths):
    legacy = paths.legacy_config_dir / 'config.toml'
    return legacy if not paths.config_dir.exists() and legacy.is_file() else paths.config


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_object(data, label):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('duplicate key')
            obj[key] = value
        return obj
    def invalid_constant(value):
        raise ValueError(f'invalid constant {value}')
    try:
        obj = json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise InstallError(f'{label} is malformed JSON ({exc}); no settings were changed') from exc
    if not isinstance(obj, dict):
        raise InstallError(f'{label} must contain a JSON object; no settings were changed')
    return obj


def settings_bytes(path):
    if path.is_symlink():
        raise InstallError(f'Refusing to replace symlink: {path}')
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    return data, json_object(data, str(path))


def status_spans(text):
    """Locate top-level values after validating JSON, without reformatting other keys."""
    decoder = json.JSONDecoder()
    pos = text.index('{') + 1
    entries = []
    previous_comma = None
    while True:
        while text[pos].isspace():
            pos += 1
        if text[pos] == '}':
            return entries, pos
        start = pos
        key, pos = decoder.raw_decode(text, pos)
        while text[pos].isspace():
            pos += 1
        pos += 1  # colon
        while text[pos].isspace():
            pos += 1
        value_start = pos
        _, pos = decoder.raw_decode(text, pos)
        value_end = pos
        while text[pos].isspace():
            pos += 1
        entries.append((key, start, value_start, value_end, previous_comma))
        if text[pos] == '}':
            return entries, pos
        previous_comma = pos
        pos += 1


def status_value(raw):
    text = raw.decode('utf-8')
    entries, _ = status_spans(text)
    return next((text[start:end] for key, _, start, end, _ in entries if key == 'statusLine'), None)


def patch_status(raw, replacement):
    text = raw.decode('utf-8')
    entries, closing = status_spans(text)
    for index, (key, start, value_start, value_end, previous_comma) in enumerate(entries):
        if key != 'statusLine':
            continue
        if replacement is not None:
            return (text[:value_start] + replacement + text[value_end:]).encode()
        if index + 1 < len(entries):
            return (text[:start] + text[entries[index + 1][1]:]).encode()
        return (text[:previous_comma if previous_comma is not None else start] + text[value_end:]).encode()
    if replacement is None:
        return raw
    addition = (', ' if entries else '') + '"statusLine": ' + replacement
    return (text[:closing] + addition + text[closing:]).encode()


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.murmuratio-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def create_new(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def save_state(paths, state):
    atomic_write(paths.state, (json.dumps(state, indent=2) + '\n').encode())


def load_state(paths):
    if paths.state.is_symlink():
        raise InstallError(f'Refusing to read symlink: {paths.state}')
    try:
        state = json_object(paths.state.read_bytes(), str(paths.state))
    except FileNotFoundError:
        return None
    if (state.get('version') != 1 or not isinstance(state.get('command'), str)
            or not isinstance(state.get('settings_existed'), bool)
            or not isinstance(state.get('created_config'), bool)
            or not isinstance(state.get('installed_settings_sha'), str)):
        raise InstallError(f'Invalid installation record: {paths.state}')
    return state


def old_command(settings):
    status = settings.get('statusLine')
    if status is None:
        return None
    if not isinstance(status, dict) or status.get('type') != 'command' or not isinstance(status.get('command'), str):
        raise InstallError('Unsupported statusLine in settings.json; expected a command')
    return status['command'] or None


def wrapper_command(previous):
    args = [sys.executable, str(ROOT / 'statusline_wrapper.py')]
    if previous:
        args.append(previous)
    return ' '.join(shlex.quote(arg) for arg in args)


def read_plan(paths, config, demo=False, config_path=None):
    print('Application reads:')
    print(f'  {ROOT}/ (Python application files)')
    print(f'  {ROOT}/web/ (served interface files)')
    if demo:
        print(f'  {ROOT}/fixtures/demo.json')
        return
    found = Discovery.at(paths.home, config)
    for path in (config_path or paths.config, found.claude_limits, found.claude_limits_legacy,
                 found.claude_sessions / '*/*.jsonl', found.claude_desktop / '*/*/local_*.json',
                 found.codex_rollouts / '*/*/*/rollout-*.jsonl'):
        print(f'  {path}')
    if config.modules['codex_wrapper']:
        print(f'  {found.codex_wrapper}/*.json')
        print('  Event paths referenced by those wrapper files')
    if config.modules['codex_chats']:
        for path in (found.codex_home / 'state_5.sqlite', found.codex_home / '.codex-global-state.json',
                     found.codex_home / 'sessions', found.codex_home / 'archived_sessions'):
            print(f'  {path}')
        print(f'  {found.codex_home}/state_5.sqlite-wal and state_5.sqlite-shm (when present)')
        print('  Rollout paths referenced by the Codex database')
    if config.modules['forecast'] or config.modules['routing']:
        print(f'  {config.limits_history}')
    if config.modules['burn_rate'] or config.modules['scenarios'] and config.modules['forecast']:
        print(f'  {found.claude_sessions}/*/*/subagents/*.jsonl')
    if config.modules['context_reset'] and config.modules['codex_chats']:
        print(f'  {paths.data}/reset/reset-results.json (or legacy {paths.data}/reset-results.json)')
        print(f'  {found.codex_home}/goals_1.sqlite (on context reset)')
        print(f'  {found.codex_home}/config.toml and the bundled Codex executable (on context reset)')
        print('  The Codex process may read additional paths it controls.')
    print('  Any paths read by the previous statusLine command are controlled by that command.')


def write_plan(paths, config, demo=False):
    print('Application writes:')
    if demo:
        print('  None in demo mode.')
        return
    if config.modules['forecast'] or config.modules['routing']:
        print(f'  {config.limits_history} (quota history)')
    if config.modules['routing']:
        print(f'  {config.routing_file} (routing)')
    if config.modules['context_reset'] and config.modules['codex_chats']:
        print(f'  {paths.data}/reset/reset-results.json (on context reset)')
        print('  A context reset also runs Codex, which may update its own data.')
    print(f'  {paths.data}/claude-limits.json (when the installed statusLine receives limits)')
    print('  The previous statusLine command may write its own files.')


def installation_plan(paths, config, args, state, original):
    print(f'Installer reads: {paths.settings}, {paths.config}, {paths.state}, {paths.backup}')
    print('Installation files:')
    if state is None:
        if original is not None:
            print(f'  create backup: {paths.backup}')
        print(f'  create: {paths.state}')
        print(f'  {"change" if original is not None else "create"}: {paths.settings}')
        if not planned_config(paths).exists():
            print(f'  create: {paths.config}')
    else:
        print('  Existing installation is retained.')
        if state.get('agent_sha'):
            agent, label = owned_agent(paths, state)
            if label == LEGACY_LABEL:
                print(f'  replace and reload: {agent} -> {paths.agent}')
                print(f'  change: {paths.state}')
    print(f'  statusLine cache when Claude runs: {paths.data}/claude-limits.json')
    if args.launch_agent and (state is None or not state.get('agent_sha')):
        print(f'  create and load: {paths.agent}')
        if state is not None:
            print(f'  change: {paths.state}')
    read_plan(paths, config)
    write_plan(paths, config)


def check_agent_available(paths):
    if sys.platform != 'darwin':
        raise InstallError('--launch-agent requires macOS')
    if paths.agent.exists() or paths.agent.is_symlink():
        raise InstallError(f'LaunchAgent already exists: {paths.agent}')
    result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{LABEL}'], capture_output=True)
    if result.returncode == 0:
        raise InstallError(f'LaunchAgent label is already in use: {LABEL}')


def add_agent(paths, state, port):
    check_agent_available(paths)
    plist = plistlib.dumps({'Label': LABEL, 'ProgramArguments': [sys.executable, str(ROOT / 'murmuratio'),
                                                                  'run', '--port', str(port)],
                            'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True})
    create_new(paths.agent, plist)
    try:
        state['agent_sha'] = sha(plist)
        save_state(paths, state)
        result = subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(paths.agent)], capture_output=True)
        if result.returncode:
            raise InstallError('LaunchAgent could not be loaded; the statusLine installation remains in place')
    except (InstallError, OSError):
        if paths.agent.is_file() and not paths.agent.is_symlink() and sha(paths.agent.read_bytes()) == sha(plist):
            paths.agent.unlink()
        state.pop('agent_sha', None)
        save_state(paths, state)
        raise


def owned_agent(paths, state):
    matches = []
    for path, label in ((paths.agent, LABEL), (paths.legacy_agent, LEGACY_LABEL)):
        if path.is_symlink():
            raise InstallError(f'Refusing to use LaunchAgent symlink: {path}')
        if path.is_file() and sha(path.read_bytes()) == state['agent_sha']:
            matches.append((path, label))
    if len(matches) != 1:
        raise InstallError('LaunchAgent was changed or removed; installation record was retained')
    return matches[0]


def migrate_agent(paths, state):
    old, label = owned_agent(paths, state)
    if label == LABEL:
        return
    check_agent_available(paths)
    try:
        plist = plistlib.loads(old.read_bytes())
        arguments = plist['ProgramArguments']
        if plist.get('Label') != LEGACY_LABEL or not isinstance(arguments, list):
            raise ValueError('invalid legacy LaunchAgent')
        matches = [i for i, arg in enumerate(arguments) if isinstance(arg, str) and Path(arg).name == 'agent-inspector']
        if len(matches) != 1:
            raise ValueError('legacy CLI not found')
        arguments[matches[0]] = str(ROOT / 'murmuratio')
        plist['Label'] = LABEL
        updated = plistlib.dumps(plist)
    except (ValueError, KeyError, TypeError, plistlib.InvalidFileException) as exc:
        raise InstallError('Cannot migrate the existing LaunchAgent; it was retained') from exc
    create_new(paths.agent, updated)
    target = f'gui/{os.getuid()}'
    was_loaded = subprocess.run(['launchctl', 'print', f'{target}/{LEGACY_LABEL}'], capture_output=True).returncode == 0
    booted_new = False
    stopped_old = False
    try:
        if was_loaded:
            if subprocess.run(['launchctl', 'bootout', f'{target}/{LEGACY_LABEL}'], capture_output=True).returncode:
                raise InstallError('Could not unload the old LaunchAgent; it was retained')
            stopped_old = True
        if subprocess.run(['launchctl', 'bootstrap', target, str(paths.agent)], capture_output=True).returncode:
            raise InstallError('Could not load the new LaunchAgent; the old one was retained')
        booted_new = True
        previous_sha = state['agent_sha']
        state['agent_sha'] = sha(updated)
        try:
            save_state(paths, state)
        except Exception:
            state['agent_sha'] = previous_sha
            raise
    except Exception as exc:
        if booted_new:
            if subprocess.run(['launchctl', 'bootout', f'{target}/{LABEL}'], capture_output=True).returncode:
                raise InstallError('Could not stop the new LaunchAgent after migration failed; both plist files were retained') from exc
        if stopped_old:
            if subprocess.run(['launchctl', 'bootstrap', target, str(old)], capture_output=True).returncode:
                paths.agent.unlink(missing_ok=True)
                raise InstallError('Could not restart the old LaunchAgent after migration failed; its plist was retained') from exc
        paths.agent.unlink(missing_ok=True)
        raise
    old.unlink()
    print(f'Migrated LaunchAgent: {LABEL}')


def validate_install(paths, args):
    state = load_state(paths)
    original, settings = settings_bytes(paths.settings)
    config_path = planned_config(paths)
    if config_path.is_symlink():
        raise InstallError(f'Refusing to use symlink: {config_path}')
    config = Config.load(paths.home, config_path)
    if state:
        if state.get('uninstall') is not None:
            raise InstallError('Uninstallation is incomplete; run uninstall again first')
        if not isinstance(settings.get('statusLine'), dict) or settings['statusLine'].get('command') != state['command']:
            raise InstallError('The installed statusLine was changed; restore it before reinstalling')
        if state.get('agent_sha') and owned_agent(paths, state)[1] == LEGACY_LABEL:
            check_agent_available(paths)
    else:
        old_command(settings)
        if paths.backup.exists() or paths.backup.is_symlink():
            raise InstallError(f'Backup already exists: {paths.backup}')
    if args.launch_agent and (not state or not state.get('agent_sha')):
        check_agent_available(paths)
    return state, original, settings, config


def install(paths, args):
    state, original, settings, config = validate_install(paths, args)
    if args.dry_run:
        installation_plan(paths, config, args, state, original)
        return
    if state is None:
        command = wrapper_command(old_command(settings))
        status = dict(settings['statusLine']) if isinstance(settings.get('statusLine'), dict) else {'type': 'command'}
        status['command'] = command
        new_settings = patch_status(original if original is not None else b'{}\n', json.dumps(status, ensure_ascii=False))
        state = {'version': 1, 'settings_existed': original is not None, 'command': command,
                 'installed_settings_sha': sha(new_settings), 'created_config': not paths.config.exists(),
                 'backup_sha': sha(original) if original is not None else None}
        # The original is backed up before the settings file is touched.
        backup_created = state_created = config_created = False
        try:
            if original is not None:
                create_new(paths.backup, original)
                backup_created = True
            create_new(paths.state, (json.dumps(state, indent=2) + '\n').encode())
            state_created = True
            if state['created_config']:
                create_new(paths.config, DEFAULT_CONFIG)
                config_created = True
            current, _ = settings_bytes(paths.settings)
            if current != original:
                raise InstallError('settings.json changed during installation; no settings were overwritten')
            mode = paths.settings.stat().st_mode & 0o777 if original is not None else 0o600
            atomic_write(paths.settings, new_settings, mode)
        except Exception:
            if config_created and paths.config.exists() and paths.config.read_bytes() == DEFAULT_CONFIG:
                paths.config.unlink()
            if state_created and paths.state.exists():
                paths.state.unlink()
            if backup_created and paths.backup.exists():
                paths.backup.unlink()
            raise
        print('Installed statusLine wrapper.')
    else:
        print('Already installed.')
    if state.get('agent_sha'):
        migrate_agent(paths, state)
    if args.launch_agent and not state.get('agent_sha'):
        add_agent(paths, state, args.port)
        print(f'Loaded LaunchAgent: {LABEL}')


def remove_installation_files(paths):
    """Keep the recovery record until all other cleanup has succeeded."""
    data_with_record = paths.install.parent
    for directory in (paths.config_dir, paths.legacy_config_dir, paths.data, paths.legacy_data):
        if not directory.exists():
            continue
        if directory != data_with_record:
            shutil.rmtree(directory)
            continue
        for item in directory.iterdir():
            if item == paths.install:
                continue
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
    for item in paths.install.iterdir():
        if item == paths.state:
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
    paths.state.unlink()
    paths.install.rmdir()
    data_with_record.rmdir()


def uninstall(paths, args):
    state = load_state(paths)
    for directory in (paths.config_dir, paths.legacy_config_dir, paths.data, paths.legacy_data):
        if directory.is_symlink() or directory.exists() and not directory.is_dir():
            raise InstallError(f'Refusing to remove non-directory or symlink: {directory}')
    if state is not None and paths.install.is_symlink():
        raise InstallError(f'Refusing to use installation directory symlink: {paths.install}')
    if state is None:
        if args.dry_run:
            print('Would remove old and new configuration and data directories.')
            return
        for directory in (paths.config_dir, paths.legacy_config_dir, paths.data, paths.legacy_data):
            if directory.exists():
                shutil.rmtree(directory)
        print('Removed configuration and data.')
        return

    phase = state.get('uninstall')
    if phase is not None and (not isinstance(phase, dict) or not isinstance(phase.get('before_sha'), str)
                              or not (phase.get('after_sha') is None or isinstance(phase.get('after_sha'), str))
                              or phase.get('agent_label') not in (None, LABEL, LEGACY_LABEL)):
        raise InstallError('Invalid uninstallation record; installation data was retained')
    current, settings = settings_bytes(paths.settings)
    current_sha = sha(current) if current is not None else None
    restored_already = phase is not None and current_sha == phase['after_sha']
    if phase is not None and not restored_already and current_sha != phase['before_sha']:
        raise InstallError('settings.json changed during uninstallation; installation record was retained')

    restored = None
    backup = None
    if not restored_already:
        if current is None:
            raise InstallError('settings.json is missing; installation record and backup were retained')
        status = settings.get('statusLine')
        if not isinstance(status, dict) or status.get('command') != state['command']:
            raise InstallError('The installed statusLine was changed; installation record and backup were retained')
        if state['settings_existed']:
            if paths.backup.is_symlink():
                raise InstallError('Settings backup is a symlink; installation record was retained')
            try:
                backup = paths.backup.read_bytes()
            except FileNotFoundError as exc:
                raise InstallError('Settings backup is missing; installation record was retained') from exc
            if sha(backup) != state.get('backup_sha'):
                raise InstallError('Settings backup has changed; installation record was retained')
            json_object(backup, str(paths.backup))
        if current_sha == state['installed_settings_sha']:
            restored = backup
        else:
            restored = patch_status(current, status_value(backup) if backup is not None else None)
        after_sha = sha(restored) if restored is not None else None
        if phase is not None and after_sha != phase['after_sha']:
            raise InstallError('Uninstallation recovery data changed; installation record was retained')

    if state.get('agent_sha'):
        if phase is None:
            agent, agent_label = owned_agent(paths, state)
        else:
            agent_label = phase['agent_label']
            if agent_label not in (LABEL, LEGACY_LABEL):
                raise InstallError('Invalid LaunchAgent recovery record')
            agent = paths.agent if agent_label == LABEL else paths.legacy_agent
            if agent.is_symlink() or agent.exists() and (not agent.is_file() or sha(agent.read_bytes()) != state['agent_sha']):
                raise InstallError('LaunchAgent changed during uninstallation; installation record was retained')
            if not agent.exists():
                agent = None
    else:
        agent, agent_label = None, None

    if args.dry_run:
        print(f'Uninstaller reads: {paths.settings}, {paths.config}, {paths.state}, {paths.backup}')
        print('Uninstallation files:')
        print(f'  restore or remove: {paths.settings}')
        for directory in (paths.config_dir, paths.legacy_config_dir, paths.data, paths.legacy_data):
            print(f'  remove: {directory}')
        if agent:
            print(f'  unload and remove: {agent}')
        read_plan(paths, Config.load(paths.home))
        return
    if phase is None:
        phase = {'before_sha': current_sha, 'after_sha': after_sha, 'agent_label': agent_label}
        state['uninstall'] = phase
        save_state(paths, state)
    if agent:
        if sys.platform != 'darwin':
            raise InstallError('Cannot unload this LaunchAgent outside macOS')
        target = f'gui/{os.getuid()}/{agent_label}'
        if subprocess.run(['launchctl', 'print', target], capture_output=True).returncode == 0:
            if subprocess.run(['launchctl', 'bootout', target], capture_output=True).returncode:
                raise InstallError('Could not unload LaunchAgent; installation record was retained')
    if settings_bytes(paths.settings)[0] != current:
        raise InstallError('settings.json changed during uninstallation; installation record was retained')
    if not restored_already:
        if restored is None:
            paths.settings.unlink()
        else:
            atomic_write(paths.settings, restored, paths.settings.stat().st_mode & 0o777)
    if agent:
        agent.unlink()
    remove_installation_files(paths)
    print('Restored previous statusLine.')


def doctor(paths):
    from now_source import NowSource
    from murmuratio_app.adapters.codex_sessions import CodexSource
    config = Config.load(paths.home)
    source = NowSource(home=paths.home, config=config)
    found = source.discovery
    print(f'Claude: {found.claude_executable or "not found"}')
    print(f'Codex: {found.codex_executable or "not found"}')
    read_plan(paths, config)
    print('Adapter health:')
    desktop = source.desktop_adapter.read()
    desktop_by_id = {row['cli_session_id']: row for row in desktop.values()}
    now = time.time()
    results = [desktop, source.claude_adapter.read(now, desktop_by_id, set()),
               source.claude_limits_adapter.read(), source.codex_limits_adapter.read()]
    if config.modules['codex_wrapper']:
        results.append(source.wrapper_adapter.read(now, set()))
    if config.modules['codex_chats']:
        results.append(CodexSource(found.codex_home, lambda value: value).read())
    if config.modules['burn_rate'] or config.modules['scenarios'] and config.modules['forecast']:
        results.append(source.claude_usage_adapter.read(now - 7 * 86400, desktop_by_id, config.modules['burn_rate']))
    if config.modules['burn_rate'] or config.modules['codex_credits']:
        results.append(source.codex_meter_adapter.read(now, config.modules['codex_credits'], config.modules['burn_rate']))
    for result in results:
        age = f', age {int(result.age_seconds)}s' if result.age_seconds is not None else ''
        print(f'  {result.source}: {result.status}{age}')
    if source.claude_limits_adapter.age_seconds is None:
        print('  Claude limits cache age: unavailable')


def port_number(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('port must be an integer') from exc
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError('port must be between 1 and 65535')
    return number


def main(argv=None):
    parser = PlainArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    install_parser = commands.add_parser('install')
    install_parser.add_argument('--launch-agent', action='store_true', help='Also install and load a macOS LaunchAgent')
    install_parser.add_argument('--port', type=port_number, default=8765)
    uninstall_parser = commands.add_parser('uninstall')
    doctor_parser = commands.add_parser('doctor')
    run_parser = commands.add_parser('run')
    run_parser.add_argument('--port', type=port_number, default=8765)
    run_parser.add_argument('--config', type=Path)
    run_parser.add_argument('--demo', action='store_true')
    run_parser.add_argument('--portless', action='store_true', help=argparse.SUPPRESS)
    for item in (install_parser, uninstall_parser, doctor_parser, run_parser):
        item.add_argument('--dry-run', action='store_true', help='Show paths without making changes')
    args = parser.parse_args(argv)
    paths = Paths(Path.home())
    try:
        if args.command in ('install', 'run'):
            if args.command == 'install':
                paths.select_install()  # Refuse conflicting records before moving either directory.
                validate_install(paths, args)
                paths = Paths(paths.home)
            moved = migrate(paths, args.dry_run)
            if args.command == 'run' and args.config:
                for old, new in moved:
                    try:
                        relative = args.config.absolute().relative_to(old.absolute())
                    except ValueError:
                        continue
                    args.config = new / relative
                    break
        if args.command in ('install', 'uninstall'):
            paths.select_install()
        if args.command == 'install':
            install(paths, args)
        elif args.command == 'uninstall':
            uninstall(paths, args)
        elif args.command == 'doctor':
            if args.dry_run:
                read_plan(paths, Config.load(paths.home))
            else:
                doctor(paths)
        elif args.dry_run:
            config = Config.load(paths.home, args.config or planned_config(paths)) if not args.demo else Config()
            read_plan(paths, config, args.demo, args.config)
            write_plan(paths, config, args.demo)
        else:
            if args.portless:
                url = urlsplit(os.environ.get('PORTLESS_URL', ''))
                if (url.scheme != 'http' or not url.hostname or not url.hostname.endswith('.localhost')
                        or not url.port or url.username or url.password or url.path not in ('', '/')
                        or url.query or url.fragment):
                    raise InstallError('A local Portless URL is required')
                try:
                    args.port = port_number(os.environ.get('PORT', ''))
                except argparse.ArgumentTypeError as exc:
                    raise InstallError('A valid Portless port is required') from exc
            else:
                os.environ.pop('PORTLESS_URL', None)
            command = [sys.executable, '-B', str(ROOT / 'server.py'), '--port', str(args.port), '--data-dir', str(paths.data)]
            if args.config:
                command.extend(['--config', str(args.config)])
            if args.demo:
                command.append('--demo')
            os.execv(sys.executable, command)
    except (InstallError, OSError, ValueError) as exc:
        message = str(exc).splitlines()
        print('Error: ' + (message[0] if message else type(exc).__name__), file=sys.stderr)
        return 1
    return 0
