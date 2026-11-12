"""Fail-open Claude lifecycle hook and journaled, reversible optional hook installation."""
import base64
import json
from pathlib import Path
import shlex
import sys
import time

from murmuratio_app.agent_registry import AgentRegistry, registry_path, single_line

EVENTS = ('SessionStart', 'SubagentStart', 'SessionEnd')


def claude_hook(home):
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        event = payload.get('hook_event_name')
        session = single_line(payload.get('session_id'), 'session_id', 200)
        if event == 'SubagentStart':
            agent = single_line(payload.get('agent_id'), 'agent_id', 200)
            print(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'additionalContext':
                f'Murmuratio identity: session_id={session}, agent_id={agent}. '
                'Use these IDs with --id/--subagent-id when registering, and --parent-id/--parent-subagent-id when spawning.'}}))
            return 0
        if event not in ('SessionStart', 'SessionEnd'):
            return 0
        updates = dict(kind='claude', provider_id=session, via='hook')
        for field, value in (('cwd', payload.get('cwd')), ('name', payload.get('title')), ('model', payload.get('model'))):
            if isinstance(value, str) and value.strip():
                updates[field] = single_line(value, field)
        effort = payload.get('effort')
        if isinstance(effort, dict):
            effort = effort.get('level')
        if isinstance(effort, str) and effort.strip():
            updates['effort'] = single_line(effort, 'effort')
        updates.update(status='done' if event == 'SessionEnd' else 'running',
                       ended_at=time.time() if event == 'SessionEnd' else None)
        AgentRegistry(home).write(updates, finish_hook=event == 'SessionEnd')
    except Exception:
        pass  # Hook errors must never fail or delay a provider session beyond its hook timeout.
    return 0


def journal_path(paths):
    return paths.install / 'integrations.json'


def hook_entry(command):
    return {'hooks': [{'type': 'command', 'command': command, 'timeout': 2}]}


def command_text():
    from murmuratio_app.cli import ROOT
    return f'{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / "murmuratio"))} hook claude 2>/dev/null || true'


def skill_path(paths):
    return paths.home / '.claude/skills/murmuratio/SKILL.md'


def rendered_template(relative):
    from murmuratio_app.cli import ROOT
    invocation = f'{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / "murmuratio"))}'
    return (ROOT / 'integrations' / relative).read_text().replace('@M@', invocation).encode()


def skill_plan(paths, state):
    from murmuratio_app.cli import InstallError, sha
    target = skill_path(paths)
    for path in (target, target.parent, target.parent.parent):
        if path.is_symlink():
            raise InstallError('Murmuratio skill path is a symlink; existing files were retained')
    if any(path.exists() and not path.is_dir() for path in (target.parent, target.parent.parent)):
        raise InstallError('Murmuratio skill directory is not a directory; existing files were retained')
    owned = state.get('skill') if state else None
    if target.exists() and (not owned or not target.is_file() or sha(target.read_bytes()) != owned.get('sha')):
        raise InstallError('Murmuratio skill exists or was edited; existing files and recovery data were retained')
    content = rendered_template('claude/skills/murmuratio/SKILL.md')
    if owned:
        if owned.get('sha') != sha(content):
            raise InstallError('Installed skill template changed; uninstall before reinstalling')
        return owned, content
    created = [str(path.relative_to(paths.home)) for path in (target.parent, target.parent.parent) if not path.exists()]
    return {'sha': sha(content), 'created_dirs': created}, content


def install_skill(paths, state, dry_run=False):
    from murmuratio_app.cli import InstallError, create_new, sha
    if dry_run:
        print(f'  install optional Claude orchestration skill: {skill_path(paths)}')
    elif not skill_path(paths).exists():
        content = rendered_template('claude/skills/murmuratio/SKILL.md')
        if sha(content) != state['skill']['sha']:
            raise InstallError('Skill template changed during installation; recovery journal was retained')
        create_new(skill_path(paths), content)


def print_codex_block():
    print('Codex AGENTS.md block (paste manually):')
    print(rendered_template('codex/AGENTS.murmuratio.md').decode(), end='')


def member(raw, key):
    from murmuratio_app.cli import status_value
    return status_value(raw, key)


def patch_member(raw, key, replacement):
    from murmuratio_app.cli import patch_status
    return patch_status(raw, replacement, key)


def array_spans(text):
    decoder = json.JSONDecoder()
    pos, entries, previous = text.index('[') + 1, [], None
    while True:
        while text[pos].isspace():
            pos += 1
        if text[pos] == ']':
            return entries, pos
        start = pos
        value, pos = decoder.raw_decode(text, pos)
        entries.append((value, start, pos, previous))
        while text[pos].isspace():
            pos += 1
        if text[pos] == ']':
            return entries, pos
        previous, pos = pos, pos + 1


def append_hooks(raw, command):
    from murmuratio_app.cli import InstallError
    hooks = member(raw, 'hooks')
    if hooks is not None and not isinstance(json.loads(hooks), dict):
        raise InstallError('settings.json hooks must be an object; no integrations were installed')
    hooks_raw = (hooks or '{}').encode()
    for event in EVENTS:
        array = member(hooks_raw, event)
        if array is not None and not isinstance(json.loads(array), list):
            raise InstallError(f'settings.json {event} hooks must be an array; no integrations were installed')
        array = array or '[]'
        entries, closing = array_spans(array)
        updated = array[:closing] + (', ' if entries else '') + json.dumps(hook_entry(command)) + array[closing:]
        hooks_raw = patch_member(hooks_raw, event, updated)
    return patch_member(raw, 'hooks', hooks_raw.decode())


def strip_hooks(raw, before, command):
    from murmuratio_app.cli import InstallError
    hooks = member(raw, 'hooks')
    if hooks is None or not isinstance(json.loads(hooks), dict):
        raise InstallError('Installed hooks changed; settings and recovery backup were retained')
    hooks_raw = hooks.encode()
    old_hooks = member(before, 'hooks') if before else None
    for event in EVENTS:
        array = member(hooks_raw, event)
        if array is None or not isinstance(json.loads(array), list):
            raise InstallError('Installed hooks changed; settings and recovery backup were retained')
        entries, _ = array_spans(array)
        index = next((i for i in range(len(entries) - 1, -1, -1) if entries[i][0] == hook_entry(command)), None)
        if index is None:
            raise InstallError('Installed hooks changed; settings and recovery backup were retained')
        _, start, end, comma = entries[index]
        if index + 1 < len(entries):
            cleaned = array[:start] + array[entries[index + 1][1]:]
        else:
            cleaned = array[:comma if comma is not None else start] + array[end:]
        prior = member(old_hooks.encode(), event) if old_hooks else None
        if not json.loads(cleaned) and prior is None:
            cleaned = None
        hooks_raw = patch_member(hooks_raw, event, cleaned)
    # Restore the exact original hook bytes when only unrelated settings changed.
    if json.loads(hooks_raw) == (json.loads(old_hooks) if old_hooks else {}):
        return patch_member(raw, 'hooks', old_hooks)
    return patch_member(raw, 'hooks', hooks_raw.decode())


def read_journal(paths):
    from murmuratio_app.cli import InstallError, json_object, sha
    path = journal_path(paths)
    if path.is_symlink():
        raise InstallError('Integration journal is a symlink; recovery data was retained')
    try:
        state = json_object(path.read_bytes(), 'Integration journal')
    except FileNotFoundError:
        return None
    if (state.get('version') != 1 or not isinstance(state.get('command'), str)
            or state.get('phase') not in ('install', 'installed', 'uninstall')
            or not isinstance(state.get('after_sha'), str)):
        raise InstallError('Invalid integration journal; recovery data was retained')
    try:
        before = base64.b64decode(state['before'], validate=True) if state['before'] is not None else None
        if state.get('before_sha') != (sha(before) if before is not None else None):
            raise ValueError('Backup checksum changed')
        if before is not None:
            json_object(before, 'Integration settings backup')
    except (KeyError, ValueError, TypeError):
        raise InstallError('Invalid integration settings backup; recovery data was retained') from None
    return state, before


def preflight_integrations(paths):
    from murmuratio_app.cli import InstallError, settings_bytes
    original, _ = settings_bytes(paths.settings)
    append_hooks(original or b'{}\n', command_text())
    journal = read_journal(paths)
    if journal and journal[0]['phase'] == 'uninstall':
        raise InstallError('Integration uninstallation is incomplete; run uninstall again first')
    skill_plan(paths, journal[0] if journal else None)


def install_integrations(paths, dry_run=False):
    from murmuratio_app.cli import InstallError, atomic_write, settings_bytes, sha
    path = journal_path(paths)
    current, _ = settings_bytes(paths.settings)
    found = read_journal(paths)
    if found:
        state, before = found
        state['skill'], _ = skill_plan(paths, state)
        if sha(current or b'') == state['after_sha']:
            if dry_run:
                install_skill(paths, state, dry_run=True)
            else:
                # Journal the skill before a retry creates its file.
                atomic_write(path, (json.dumps(state, indent=2) + '\n').encode())
                install_skill(paths, state)
                state['phase'] = 'installed'
                atomic_write(path, (json.dumps(state, indent=2) + '\n').encode())
            print_codex_block()
            return
        if state['phase'] != 'install' or current != before:
            raise InstallError('Integration settings changed; installed hooks and backup were retained')
    else:
        before = current
        state = dict(version=1, command=command_text(), phase='install',
                     before=base64.b64encode(before).decode() if before is not None else None,
                     before_sha=sha(before) if before is not None else None)
        state['skill'], _ = skill_plan(paths, state)
    updated = append_hooks(before or b'{}\n', state['command'])
    state['after_sha'] = sha(updated)
    if dry_run:
        print(f'  append optional Claude hooks: {paths.settings}')
        print(f'  journal owned integration edits: {path}')
        install_skill(paths, state, dry_run=True)
        print_codex_block()
        return
    atomic_write(path, (json.dumps(state, indent=2) + '\n').encode())
    if settings_bytes(paths.settings)[0] != before:
        raise InstallError('settings.json changed during integration installation; backup was retained')
    mode = paths.settings.stat().st_mode & 0o777 if before is not None else 0o600
    atomic_write(paths.settings, updated, mode)
    install_skill(paths, state)
    state['phase'] = 'installed'
    atomic_write(path, (json.dumps(state, indent=2) + '\n').encode())
    print('Installed optional Claude hooks and orchestration skill.')
    print_codex_block()


def uninstall_integrations(paths, keep_data=False, dry_run=False):
    from murmuratio_app.cli import InstallError, atomic_write, settings_bytes, sha
    path, data = journal_path(paths), registry_path(paths.home)
    if not keep_data and (data.is_symlink() or data.exists() and not data.is_file()):
        raise InstallError('Integration registry changed type; settings and recovery data were retained')
    found = read_journal(paths)
    if found:
        state, before = found
        target = skill_path(paths)
        if state.get('skill'):
            for candidate in (target, target.parent, target.parent.parent):
                if candidate.is_symlink():
                    raise InstallError('Installed skill path changed; hooks and recovery data were retained')
            if target.exists() and (not target.is_file() or sha(target.read_bytes()) != state['skill'].get('sha')):
                raise InstallError('Installed skill was edited; hooks and recovery data were retained')
        current, _ = settings_bytes(paths.settings)
        current_sha = sha(current or b'')
        if state['phase'] == 'uninstall':
            if current_sha not in (state['restore_sha'], state['uninstall_before_sha']):
                raise InstallError('settings.json changed during integration removal; backup was retained')
            restored = base64.b64decode(state['restore']) if state['restore'] is not None else None
        elif current == before or current_sha == state['after_sha']:
            restored = before
        else:
            if current is None:
                raise InstallError('settings.json is missing; integration backup was retained')
            restored = strip_hooks(current, before, state['command'])
        if dry_run:
            print(f'  remove owned Claude hooks: {paths.settings}')
            if state.get('skill'):
                print(f'  remove owned Claude skill: {target}')
        else:
            state.update(phase='uninstall', restore_sha=sha(restored or b''), uninstall_before_sha=current_sha,
                         restore=base64.b64encode(restored).decode() if restored is not None else None)
            atomic_write(path, (json.dumps(state, indent=2) + '\n').encode())
            if settings_bytes(paths.settings)[0] != current:
                raise InstallError('settings.json changed during integration removal; backup was retained')
            if restored is None:
                paths.settings.unlink(missing_ok=True)
            else:
                mode = paths.settings.stat().st_mode & 0o777 if current is not None else 0o600
                atomic_write(paths.settings, restored, mode)
            if state.get('skill'):
                target.unlink(missing_ok=True)
                for name in state['skill'].get('created_dirs', []):
                    if name not in ('.claude/skills/murmuratio', '.claude/skills'):
                        continue
                    try:
                        (paths.home / name).rmdir()
                    except OSError:
                        pass
    if not keep_data:
        if dry_run:
            print(f'  remove owned integration metadata: {data}')
        else:
            data.unlink(missing_ok=True)
    if found and not dry_run:
        path.unlink()
        print('Removed optional Claude hooks and skill.')
