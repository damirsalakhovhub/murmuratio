"""The optional integrations' single, locked metadata writer (no prompts or replies)."""
import fcntl
import calendar
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import time
import uuid

FIELDS = ('v', 'id', 'kind', 'provider_id', 'subagent_id', 'role', 'name',
          'parent_kind', 'parent_id', 'parent_subagent_id', 'cwd', 'started_at',
          'ended_at', 'status', 'exit_code', 'pid', 'pid_start', 'model_requested',
          'effort_requested', 'model', 'effort', 'via')
STATUSES = {'starting', 'running', 'done', 'failed', 'interrupted', 'unknown'}
TERMINAL = {'done', 'failed', 'interrupted'}


def registry_path(home):
    return Path(home) / '.local/share/murmuratio/agents.jsonl'


def single_line(value, label, limit=1024):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 or c in '\u0085\u2028\u2029' for c in value)):
        raise ValueError(f'{label} must be a non-empty single line (at most {limit} characters)')
    return value


def identity(record):
    return record['kind'], record.get('provider_id'), record.get('subagent_id')


def card_id(kind, provider_id, subagent_id=None):
    return f'{kind}:{provider_id}' + (f':agent-{subagent_id}' if subagent_id else '')


def validate(record):
    if set(record) != set(FIELDS) or record['v'] != 1:
        raise ValueError('Invalid integration snapshot')
    if record['kind'] not in ('claude', 'codex') or record['status'] not in STATUSES:
        raise ValueError('Invalid provider or status')
    if record['role'] not in (None, 'orchestrator', 'worker') or record['via'] not in ('manual', 'launcher', 'hook'):
        raise ValueError('Invalid role or source')
    for key in ('id', 'provider_id', 'subagent_id', 'parent_id', 'parent_subagent_id'):
        if record[key] is not None:
            single_line(record[key], key, 200)
    if not record['id']:
        raise ValueError('A record ID is required')
    for key in ('name', 'cwd', 'model_requested', 'effort_requested', 'model', 'effort'):
        if record[key] is not None:
            single_line(record[key], key)
    if record['parent_kind'] not in (None, 'claude', 'codex'):
        raise ValueError('Invalid parent provider')
    if bool(record['parent_id']) != bool(record['parent_kind']):
        raise ValueError('Parent ID and provider must be supplied together')
    if record['parent_subagent_id'] and (record['parent_kind'] != 'claude' or not record['parent_id']):
        raise ValueError('A parent subagent requires a Claude session')
    if record['subagent_id'] and (record['kind'] != 'claude' or not record['provider_id']):
        raise ValueError('A subagent requires a Claude session')
    for key in ('started_at', 'ended_at', 'pid_start'):
        if record[key] is not None and (type(record[key]) not in (int, float) or not math.isfinite(record[key])
                                        or not 0 <= record[key] < 1e11):
            raise ValueError('Invalid timestamp')
    for key in ('pid', 'exit_code'):
        if record[key] is not None and type(record[key]) is not int:
            raise ValueError(f'Invalid {key}')
    if record['pid'] is not None and record['pid'] <= 0:
        raise ValueError('Invalid pid')
    return record


def process_start(pid):
    """A PID alone cannot establish that this is still the launched process."""
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True,
                                text=True, timeout=1, env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'})
        if result.returncode or not result.stdout.strip():
            return None
        return calendar.timegm(time.strptime(result.stdout.strip(), '%a %b %d %H:%M:%S %Y'))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def process_matches(record):
    if not record.get('pid') or not record.get('pid_start'):
        return None
    observed = process_start(record['pid'])
    return observed == record['pid_start'] if observed else False


class AgentRegistry:
    def __init__(self, home):
        self.path = registry_path(home)

    def write(self, changes, record_id=None, *, finish_hook=False):
        """Lock read/merge/append as one operation; every line is a complete snapshot."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+b') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Registry must be a regular, unshared file')
            fcntl.flock(stream, fcntl.LOCK_EX)
            os.fchmod(stream.fileno(), 0o600)
            records = {}
            tail = b''
            for line in stream:
                tail = line
                if not line.endswith(b'\n'):
                    continue
                try:
                    record = validate(json.loads(line))
                    records.pop(record['id'], None)
                    records[record['id']] = record
                except (ValueError, TypeError, KeyError, UnicodeError):
                    continue
            previous = records.get(record_id)
            superseded = None
            target_id = record_id
            if changes.get('provider_id'):
                identity_source = dict(previous or {})
                identity_source.update(changes)
                key = identity(identity_source)
                matches = [r for r in records.values() if identity(r) == key and r.get('id') != record_id]
                explicit = next((r for r in reversed(matches)
                                 if r.get('via') in ('manual', 'hook') and r.get('role')), None)
                if previous and previous.get('via') == 'launcher' and explicit:
                    superseded = dict(previous, status='unknown', ended_at=time.time(), role=None, name=None,
                                      parent_kind=None, parent_id=None, parent_subagent_id=None)
                    previous, target_id = explicit, explicit['id']
                elif previous is None:
                    previous = next(iter(reversed(matches)), None)
            previous = previous or {}
            if finish_hook and (previous.get('via') == 'launcher' or previous.get('status') in TERMINAL):
                return previous
            changes = dict(changes)
            if previous.get('via') == 'launcher' and changes.get('via') in ('manual', 'hook'):
                source = changes.pop('via')
                if source == 'hook':
                    for key in ('name', 'role', 'status', 'ended_at', 'exit_code'):
                        changes.pop(key, None)
            record = dict.fromkeys(FIELDS)
            record.update(v=1, id=target_id or previous.get('id') or 'run:' + str(uuid.uuid4()),
                          started_at=time.time(), status='running')
            record.update(previous)
            record.update(changes)
            validate(record)
            # A killed append must not swallow the next valid snapshot.
            if tail and not tail.endswith(b'\n'):
                stream.seek(-len(tail), os.SEEK_END)
                stream.truncate()
            rows = ([superseded] if superseded else []) + [record]
            stream.write(''.join(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n'
                                 for row in rows).encode())
            stream.flush()
            os.fsync(stream.fileno())
            return record

    def register(self, kind, provider_id, name, subagent_id=None):
        single_line(provider_id, 'id', 200)
        single_line(name, 'name')
        return self.write(dict(kind=kind, provider_id=provider_id, subagent_id=subagent_id,
                               role='orchestrator', name=name, cwd=str(Path.cwd()), via='manual'))
