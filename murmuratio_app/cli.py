"""Install, remove, diagnose and run Murmuratio."""
import argparse
import base64
import binascii
import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from murmuratio_app import __version__
from murmuratio_app.discovery import Discovery
from murmuratio_app.common import PlainArgumentParser
from murmuratio_app.config import Config, MODULES

ROOT = Path(__file__).resolve().parent.parent
LABEL = 'org.murmuratio.app'
DEFAULT_CONFIG = ('# Optional modules are disabled by default.\n[modules]\n' +
                  ''.join(f'{name} = false\n' for name in MODULES) +
                  '\n').encode()


class InstallError(Exception):
    pass


class Paths:
    def __init__(self, home):
        self.home = Path(home)
        self.settings = self.home / '.claude/settings.json'
        self.config_dir = self.home / '.config/murmuratio'
        self.config = self.config_dir / 'config.toml'
        self.data = self.home / '.local/share/murmuratio'
        self.install = self.data / 'install'
        self.backup = self.install / 'settings.json.backup'
        self.state = self.install / 'installation.json'
        self.agent = self.home / f'Library/LaunchAgents/{LABEL}.plist'


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


def status_value(raw, property_name='statusLine'):
    text = raw.decode('utf-8')
    entries, _ = status_spans(text)
    return next((text[start:end] for key, _, start, end, _ in entries if key == property_name), None)


def patch_status(raw, replacement, property_name='statusLine'):
    text = raw.decode('utf-8')
    entries, closing = status_spans(text)
    for index, (key, start, value_start, value_end, previous_comma) in enumerate(entries):
        if key != property_name:
            continue
        if replacement is not None:
            return (text[:value_start] + replacement + text[value_end:]).encode()
        if index + 1 < len(entries):
            return (text[:start] + text[entries[index + 1][1]:]).encode()
        return (text[:previous_comma if previous_comma is not None else start] + text[value_end:]).encode()
    if replacement is None:
        return raw
    addition = (', ' if entries else '') + json.dumps(property_name) + ': ' + replacement
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


def file_identity(path):
    if path.is_symlink():
        return None
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino] if path.is_file() else None


def sole_owned_file(path, identity):
    if not identity or file_identity(path) != identity:
        return False
    try:
        return path.stat().st_nlink == 1
    except FileNotFoundError:
        return False


def recover_hook_install(paths, state, dry_run=False):
    """Resolve an interrupted statusLine install without touching changed settings."""
    phase = state.get('hook_install') if state else None
    if phase is None:
        return state
    if (not isinstance(phase, dict) or not isinstance(phase.get('after_sha'), str)
            or not (phase.get('before_sha') is None or isinstance(phase.get('before_sha'), str))):
        raise InstallError('Invalid statusLine installation record; installation data was retained')
    current, settings = settings_bytes(paths.settings)
    current_sha = sha(current) if current is not None else None
    status = settings.get('statusLine')
    installed = isinstance(status, dict) and status.get('command') == state['command']
    result = dict(state)
    if current_sha == phase['after_sha'] or installed:
        result.pop('hook_install', None)
    elif current_sha == phase['before_sha']:
        if (not dry_run and sole_owned_file(paths.backup, state.get('backup_identity')) and
                sha(paths.backup.read_bytes()) == state.get('backup_sha')):
            paths.backup.unlink()
        result.update(hook_installed=False, settings_existed=False, command='', installed_settings_sha='', backup_sha=None)
        for key in ('hook_install', 'backup_identity', 'backup_inline'):
            result.pop(key, None)
    else:
        raise InstallError('settings.json changed during statusLine installation; installation record was retained')
    if not dry_run:
        save_state(paths, result)
    return result


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
                 found.claude_sessions / '*/*.jsonl', found.claude_sessions / '*/*/subagents/*.jsonl',
                 found.claude_sessions / '*/*/subagents/*.meta.json', found.agents_registry,
                 found.integration_registry,
                 found.claude_desktop / '*/*/local_*.json',
                 found.codex_rollouts / '*/*/*/rollout-*.jsonl'):
        if path is not None:
            print(f'  {path}')
    if config.modules['codex_wrapper']:
        print(f'  {found.codex_wrapper}/*.json')
        print('  Event paths referenced by those wrapper files')
        print('  Brief paths referenced by those wrapper files')
    for path in (found.codex_home / 'session_index.jsonl', found.codex_home / 'state_5.sqlite'):
        print(f'  {path}')
    print(f'  {found.codex_home}/state_5.sqlite-wal and state_5.sqlite-shm (when present)')
    print('  Rollout paths referenced by the Codex database (sessions/ or archived_sessions/)')
    if config.modules['forecast'] or config.modules['routing'] or config.modules['burn_rate']:
        print(f'  {config.limits_history}')
    if config.modules['routing']:
        print(f'  {config.limits_history.parent / "routing-history.jsonl"}')
    if config.codex_live_limits:
        print(f'  {config.codex_cli or "codex on PATH"} app-server, every 5 minutes (account/rateLimits/read;'
              ' Codex contacts OpenAI and reads paths it controls)')
    print('  Any paths read by the previous statusLine command are controlled by that command.')


def write_plan(paths, config, demo=False):
    print('Application writes:')
    if demo:
        print('  None in demo mode.')
        return
    if config.modules['forecast'] or config.modules['routing']:
        print(f'  {config.limits_history} (quota history)')
        print(f'  {config.limits_history.with_suffix(config.limits_history.suffix + ".tmp")} (atomic replacement)')
    if config.modules['routing']:
        print(f'  {config.routing_file} (routing)')
        print(f'  {config.routing_file.with_suffix(config.routing_file.suffix + ".tmp")} (atomic replacement)')
        print(f'  {config.limits_history.parent / "routing-history.jsonl"} (changed routing advice)')
    print(f'  {paths.data}/claude-limits.json (when the installed statusLine receives limits)')
    print('  The previous statusLine command may write its own files.')


def installation_plan(paths, config, args, state, original):
    print(f'Installer reads: {paths.settings}, {paths.config}, {paths.state}, {paths.backup}')
    print('Installation files:')
    if state is None:
        if original is not None:
            print(f'  create backup: {paths.backup}')
        print(f'  create: {paths.state}')
        if paths.settings.parent.is_dir():
            print(f'  {"change" if original is not None else "create"}: {paths.settings}')
        else:
            print('  Claude statusLine skipped: Claude directory not found')
        if not paths.config.exists():
            print(f'  create: {paths.config}')
    else:
        print('  Existing installation is retained.')
        if not state.get('hook_installed', True) and paths.settings.parent.is_dir():
            if original is not None:
                print(f'  save settings backup in: {paths.state}')
            print(f'  {"change" if original is not None else "create"}: {paths.settings}')
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
    if paths.agent.is_symlink():
        raise InstallError(f'Refusing to use LaunchAgent symlink: {paths.agent}')
    if not paths.agent.is_file() or sha(paths.agent.read_bytes()) != state['agent_sha']:
        raise InstallError('LaunchAgent was changed or removed; installation record was retained')
    return paths.agent, LABEL


def validate_install(paths, args):
    state = recover_hook_install(paths, load_state(paths), args.dry_run)
    original, settings = settings_bytes(paths.settings)
    config_path = paths.config
    if config_path.is_symlink():
        raise InstallError(f'Refusing to use symlink: {config_path}')
    config = Config.load(paths.home, config_path)
    if state:
        if state.get('uninstall') is not None:
            raise InstallError('Uninstallation is incomplete; run uninstall again first')
        if state.get('hook_installed', True) and (not isinstance(settings.get('statusLine'), dict) or settings['statusLine'].get('command') != state['command']):
            raise InstallError('The installed statusLine was changed; restore it before reinstalling')
        if not state.get('hook_installed', True) and paths.settings.parent.is_dir():
            old_command(settings)
    else:
        old_command(settings)
        if paths.backup.exists() or paths.backup.is_symlink():
            raise InstallError(f'Backup already exists: {paths.backup}')
    if args.launch_agent and (not state or not state.get('agent_sha')):
        check_agent_available(paths)
    return state, original, settings, config


def install(paths, args):
    integrations = getattr(args, 'integrations', False)
    if integrations:
        from murmuratio_app.hooks import preflight_integrations
        preflight_integrations(paths)
    state, original, settings, config = validate_install(paths, args)
    create_claude = (integrations or getattr(args, 'statusline', False)) and not paths.settings.parent.exists()
    if args.dry_run:
        installation_plan(paths, config, args, state, original)
        if integrations:
            from murmuratio_app.hooks import install_integrations
            install_integrations(paths, dry_run=True)
        return
    if create_claude:
        if state is not None:
            created_dirs = set(state.get('created_dirs', []))
            created_dirs.add('.claude')
            state['created_dirs'] = sorted(created_dirs)
            save_state(paths, state)
        paths.settings.parent.mkdir(mode=0o700, parents=True)
    if state is None:
        use_hook = paths.settings.parent.is_dir()
        command = wrapper_command(old_command(settings)) if use_hook else ''
        status = dict(settings['statusLine']) if isinstance(settings.get('statusLine'), dict) else {'type': 'command'}
        status['command'] = command
        new_settings = patch_status(original if original is not None else b'{}\n', json.dumps(status, ensure_ascii=False)) if use_hook else None
        # Folders this install creates, so uninstall can remove them again when they are empty.
        created = sorted({str(folder.relative_to(paths.home)) for top in (paths.install, paths.config_dir)
                          for folder in (top, *top.parents) if folder.is_relative_to(paths.home)
                          and folder != paths.home and not folder.exists()})
        if create_claude:
            created.append('.claude')
        state = {'version': 1, 'settings_existed': original is not None, 'command': command, 'created_dirs': created,
                 'installed_settings_sha': sha(new_settings) if use_hook else '', 'created_config': not paths.config.exists(),
                 'backup_sha': sha(original) if original is not None else None, 'hook_installed': use_hook}
        if use_hook:
            state['hook_install'] = {'before_sha': sha(original) if original is not None else None,
                                     'after_sha': sha(new_settings)}
        # The original is backed up before the settings file is touched.
        backup_created = state_created = config_created = False
        try:
            if original is not None:
                create_new(paths.backup, original)
                backup_created = True
                state['backup_identity'] = file_identity(paths.backup)
            create_new(paths.state, (json.dumps(state, indent=2) + '\n').encode())
            state_created = True
            if state['created_config']:
                create_new(paths.config, DEFAULT_CONFIG)
                config_created = True
                state['created_config_identity'] = file_identity(paths.config)
                save_state(paths, state)
            if use_hook:
                current, _ = settings_bytes(paths.settings)
                if current != original:
                    raise InstallError('settings.json changed during installation; no settings were overwritten')
                mode = paths.settings.stat().st_mode & 0o777 if original is not None else 0o600
                atomic_write(paths.settings, new_settings, mode)
                state.pop('hook_install', None)
                save_state(paths, state)
        except Exception:
            current = settings_bytes(paths.settings)[0] if use_hook else None
            if use_hook and current != original:
                # The hook may have been installed; retain its journal and backup.
                raise
            if config_created and sole_owned_file(paths.config, state.get('created_config_identity')) and paths.config.read_bytes() == DEFAULT_CONFIG:
                paths.config.unlink()
            if state_created and paths.state.exists():
                paths.state.unlink()
            if backup_created and sole_owned_file(paths.backup, state.get('backup_identity')):
                paths.backup.unlink()
            raise
        print('Installed statusLine wrapper.' if use_hook else 'Installed without a Claude statusLine.')
    else:
        if not state.get('hook_installed', True) and paths.settings.parent.is_dir():
            command = wrapper_command(old_command(settings))
            status = dict(settings['statusLine']) if isinstance(settings.get('statusLine'), dict) else {'type': 'command'}
            status['command'] = command
            updated = patch_status(original if original is not None else b'{}\n', json.dumps(status, ensure_ascii=False))
            state.update(hook_installed=True, settings_existed=original is not None, command=command,
                         installed_settings_sha=sha(updated), backup_sha=sha(original) if original is not None else None)
            if original is not None:
                state['backup_inline'] = base64.b64encode(original).decode('ascii')
            state['hook_install'] = {'before_sha': sha(original) if original is not None else None,
                                     'after_sha': sha(updated)}
            try:
                if settings_bytes(paths.settings)[0] != original:
                    raise InstallError('settings.json changed during installation; no settings were overwritten')
                save_state(paths, state)
                mode = paths.settings.stat().st_mode & 0o777 if original is not None else 0o600
                atomic_write(paths.settings, updated, mode)
                state.pop('hook_install', None)
                save_state(paths, state)
            except Exception:
                try:
                    recover_hook_install(paths, load_state(paths))
                except (InstallError, OSError):
                    pass
                raise
            print('Installed statusLine wrapper.')
        else:
            print('Already installed.')
    if args.launch_agent and not state.get('agent_sha'):
        add_agent(paths, state, args.port)
        print(f'Loaded LaunchAgent: {LABEL}')
    if integrations:
        from murmuratio_app.hooks import install_integrations
        install_integrations(paths)


def remove_installation_files(paths):
    """Keep the recovery record until all other cleanup has succeeded."""
    state = load_state(paths)
    # The status line's limits cache is only a copy of what Claude reports.
    cache = paths.data / 'claude-limits.json'
    if cache.is_file() and not cache.is_symlink():
        cache.unlink()
    identity = state.get('created_config_identity')
    if state['created_config'] and identity:
        if sole_owned_file(paths.config, identity) and paths.config.read_bytes() == DEFAULT_CONFIG:
            paths.config.unlink()
    if (state.get('hook_installed', True) and state.get('settings_existed') and
            sole_owned_file(paths.backup, state.get('backup_identity')) and
            sha(paths.backup.read_bytes()) == state.get('backup_sha')):
        paths.backup.unlink()
    paths.state.unlink()
    created = [paths.home / name for name in state.get('created_dirs', [])
               if isinstance(name, str) and name and not Path(name).is_absolute() and '..' not in Path(name).parts]
    for directory in (paths.install, paths.install.parent, paths.config_dir,
                      *sorted(created, key=lambda path: len(path.parts), reverse=True)):
        try:
            directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise


def uninstall(paths, args, _skip_integrations=False):
    state = load_state(paths)
    for directory in (paths.config_dir, paths.data):
        if directory.is_symlink() or directory.exists() and not directory.is_dir():
            raise InstallError(f'Refusing to remove non-directory or symlink: {directory}')
    if state is not None and paths.install.is_symlink():
        raise InstallError(f'Refusing to use installation directory symlink: {paths.install}')
    if not _skip_integrations:
        from murmuratio_app.agent_registry import registry_path
        from murmuratio_app.hooks import journal_path, uninstall_integrations
        if journal_path(paths).exists() or registry_path(paths.home).exists():
            # Validate the statusLine backup, recovery state and LaunchAgent before
            # removing any integration hooks or metadata.
            from contextlib import redirect_stdout
            import io
            with redirect_stdout(io.StringIO()):
                uninstall(paths, argparse.Namespace(dry_run=True), _skip_integrations=True)
        uninstall_integrations(paths, getattr(args, 'keep_integration_data', False), args.dry_run)
    if state is None:
        print('No installation record; existing files retained.')
        return

    state = recover_hook_install(paths, state, args.dry_run)

    phase = state.get('uninstall')
    if phase is not None and (not isinstance(phase, dict) or not (phase.get('before_sha') is None or isinstance(phase.get('before_sha'), str))
                              or not (phase.get('after_sha') is None or isinstance(phase.get('after_sha'), str))
                              or phase.get('agent_label') not in (None, LABEL)):
        raise InstallError('Invalid uninstallation record; installation data was retained')
    use_hook = state.get('hook_installed', True)
    current, settings = settings_bytes(paths.settings) if use_hook else (None, {})
    current_sha = sha(current) if current is not None else None
    restored_already = not use_hook or phase is not None and current_sha == phase['after_sha']
    if phase is not None and not restored_already and current_sha != phase['before_sha']:
        raise InstallError('settings.json changed during uninstallation; installation record was retained')

    restored = None
    backup = None
    after_sha = None
    if not restored_already:
        if current is None:
            raise InstallError('settings.json is missing; installation record and backup were retained')
        status = settings.get('statusLine')
        if not isinstance(status, dict) or status.get('command') != state['command']:
            raise InstallError('The installed statusLine was changed; installation record and backup were retained')
        if state['settings_existed']:
            if 'backup_inline' in state:
                try:
                    backup = base64.b64decode(state['backup_inline'], validate=True)
                except (ValueError, TypeError, binascii.Error) as exc:
                    raise InstallError('Settings backup is invalid; installation record was retained') from exc
            else:
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
            if agent_label != LABEL:
                raise InstallError('Invalid LaunchAgent recovery record')
            agent = paths.agent
            if agent.is_symlink() or agent.exists() and (not agent.is_file() or sha(agent.read_bytes()) != state['agent_sha']):
                raise InstallError('LaunchAgent changed during uninstallation; installation record was retained')
            if not agent.exists():
                agent = None
    else:
        agent, agent_label = None, None

    if args.dry_run:
        print(f'Uninstaller reads: {paths.settings}, {paths.config}, {paths.state}, {paths.backup}')
        print('Uninstallation files:')
        if use_hook:
            print(f'  restore or remove: {paths.settings}')
        print(f'  remove owned installation records: {paths.install}')
        print(f'  remove unchanged installer-created config: {paths.config}')
        print(f'  remove statusLine limits cache: {paths.data / "claude-limits.json"}')
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
    if use_hook and settings_bytes(paths.settings)[0] != current:
        raise InstallError('settings.json changed during uninstallation; installation record was retained')
    if not restored_already:
        if restored is None:
            paths.settings.unlink()
        else:
            atomic_write(paths.settings, restored, paths.settings.stat().st_mode & 0o777)
    if agent:
        agent.unlink()
    remove_installation_files(paths)
    print('Restored previous statusLine.' if use_hook else 'Removed installation records.')


def doctor(paths):
    from murmuratio_app.now import NowSource
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
    results = [desktop, source.claude_adapter.read(now, desktop_by_id, set()), source.codex_board_adapter.read(now),
               source.claude_limits_adapter.read(), source.codex_limits_adapter.read()]
    if config.modules['codex_wrapper']:
        results.append(source.wrapper_adapter.read(now, set()))
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
    parser.add_argument('--version', action='version', version=f'murmuratio {__version__}')
    commands = parser.add_subparsers(dest='command', required=True)
    agent_parser = commands.add_parser('agent', help='Register an explicit orchestration identity')
    agent_commands = agent_parser.add_subparsers(dest='agent_command', required=True)
    register_parser = agent_commands.add_parser('register')
    register_parser.add_argument('--kind', choices=('claude', 'codex'), required=True)
    register_parser.add_argument('--id', required=True)
    register_parser.add_argument('--role', choices=('orchestrator',), required=True)
    register_parser.add_argument('--name', required=True)
    register_parser.add_argument('--subagent-id')
    spawn_parser = commands.add_parser('spawn', help='Launch a worker with an exact parent link')
    spawn_parser.add_argument('--to', choices=('claude', 'codex'), required=True)
    spawn_parser.add_argument('--title', required=True)
    spawn_parser.add_argument('--model', required=True)
    spawn_parser.add_argument('--effort', required=True)
    spawn_parser.add_argument('--parent-kind', choices=('claude', 'codex'), required=True)
    spawn_parser.add_argument('--parent-id', required=True)
    spawn_parser.add_argument('--parent-subagent-id')
    spawn_parser.add_argument('--cwd', type=Path)
    spawn_parser.add_argument('--sandbox', choices=('read-only', 'workspace-write'))
    spawn_parser.add_argument('--permission-mode', choices=('manual', 'acceptEdits', 'dontAsk', 'plan'))
    spawn_parser.add_argument('brief_file', type=Path)
    hook_parser = commands.add_parser('hook', help='Handle an optional provider lifecycle event')
    hook_parser.add_argument('provider', choices=('claude',))
    install_parser = commands.add_parser('install')
    install_parser.add_argument('--integrations', action='store_true', help='Install optional Claude hooks and orchestration skill')
    install_parser.add_argument('--statusline', action='store_true', help='Install the statusLine even if Claude is not set up yet')
    install_parser.add_argument('--launch-agent', action='store_true', help='Also install and load a macOS LaunchAgent')
    install_parser.add_argument('--port', type=port_number, default=8765)
    uninstall_parser = commands.add_parser('uninstall')
    uninstall_parser.add_argument('--keep-integration-data', action='store_true', help='Keep integration registry metadata')
    doctor_parser = commands.add_parser('doctor')
    check_parser = commands.add_parser('check-update', help='Compare this code with a running local board')
    check_parser.add_argument('--url', default='http://127.0.0.1:8765', help='Running local board URL')
    check_parser.add_argument('--config', type=Path, help='Config used by the running board')
    check_parser.add_argument('--codex-home', type=Path, help='Codex home used by the running board')
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
        if args.command == 'agent':
            from murmuratio_app.agent_registry import AgentRegistry
            record = AgentRegistry(paths.home).register(args.kind, args.id, args.name, args.subagent_id)
            print(json.dumps(record, allow_nan=False))
        elif args.command == 'hook':
            from murmuratio_app.hooks import claude_hook
            return claude_hook(paths.home)
        elif args.command == 'spawn':
            from murmuratio_app.spawn import spawn
            return spawn(paths.home, args)
        elif args.command == 'install':
            install(paths, args)
        elif args.command == 'uninstall':
            uninstall(paths, args)
        elif args.command == 'check-update':
            from murmuratio_app.check_update import check_update
            return check_update(args.url, paths.home, args.config, args.codex_home)
        elif args.command == 'doctor':
            if args.dry_run:
                read_plan(paths, Config.load(paths.home))
            else:
                doctor(paths)
        elif args.dry_run:
            config = Config.load(paths.home, args.config or paths.config) if not args.demo else Config()
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
            from murmuratio_app.server import main as serve
            command = ['--port', str(args.port)]
            if args.config:
                command.extend(['--config', str(args.config)])
            if args.demo:
                command.append('--demo')
            serve(command)
    except (InstallError, OSError, ValueError) as exc:
        message = str(exc).splitlines()
        print('Error: ' + (message[0] if message else type(exc).__name__), file=sys.stderr)
        return 1
    return 0
