"""Bounded, incremental board summaries of saved Codex sessions."""
import json
from pathlib import Path
import re
import subprocess

from murmuratio_app.adapters.base import Adapter, UnavailableSource
from murmuratio_app.adapters.activity import record_activity, working_seconds
from murmuratio_app.adapters.parsing import consume_codex
from murmuratio_app.adapters.shared import CODEX_DEAD_SECONDS, context_number, current_action
from murmuratio_app.common import one_line, timestamp

HISTORY_SECONDS = 24 * 60 * 60
MAX_RECORD_BYTES = 512 * 1024
MAX_META_BYTES = 8 * 1024 * 1024
TYPE = re.compile(rb'"type"\s*:\s*"([^"]+)"')
USER_ROLE = re.compile(rb'"role"\s*:\s*"user"')
RECORD_TIMESTAMP = re.compile(rb'"timestamp"\s*:\s*("[^"\\]*(?:\\.[^"\\]*)*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)')


def interactive_session(data):
    """Use launch metadata, never task text, to recognize a person's chat."""
    source, originator = data.get('source'), data.get('originator')
    if source == 'exec' or originator == 'codex_exec' or isinstance(source, dict) and 'subagent' in source:
        return False
    return source in ('cli', 'tui', 'vscode') or originator in ('Codex Desktop', 'codex_cli_rs', 'codex-tui')


def rollout_writers(paths):
    """Find open writers in one bounded macOS query; failure is not proof of death."""
    names = {str(path.resolve()): path for path in paths}
    try:
        result = subprocess.run(['/usr/sbin/lsof', '-nP', '-Fpan', '--', *names],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof returns 1 when some or all requested files have no open descriptors.
    if result.returncode not in (0, 1) or result.stderr.strip():
        return None
    writers, access = set(), None
    for line in result.stdout.splitlines():
        if line.startswith(('p', 'f')):
            access = None
        elif line.startswith('a'):
            access = line[1:]
        elif line.startswith('n') and access in ('w', 'u') and line[1:] in names:
            writers.add(names[line[1:]])
    return writers


def _task_text(content):
    if isinstance(content, str):
        content = [{'text': content}]
    if not isinstance(content, list):
        return ''
    for part in content:
        text = part.get('text') if isinstance(part, dict) else None
        if not isinstance(text, str):
            continue
        text = text.strip()
        if (not text or text.startswith(('<environment_context>', '<permissions instructions>',
                                        '<collaboration_mode>', '# AGENTS.md instructions',
                                        '<user_instructions>', '<system>'))):
            continue
        return one_line(text[:1000])[:240]
    return ''


def consume_rollout(data, record):
    payload = record.get('payload')
    if not isinstance(payload, dict):
        return
    kind, at = record.get('type'), timestamp(record.get('timestamp'))
    consume_codex(data, record)
    if kind == 'session_meta':
        thread = payload.get('id') or payload.get('session_id')
        if isinstance(thread, str):
            data['thread'] = thread
        data['started'] = timestamp(payload.get('timestamp')) or at
        if isinstance(payload.get('cwd'), str):
            data['cwd'] = payload['cwd']
        data['source'] = payload.get('source')
        data['originator'] = payload.get('originator')
        if isinstance(payload.get('model'), str):
            data['model'] = payload['model']
        for field in ('parent_session', 'parent_app_session', 'run_id'):
            if isinstance(payload.get(field), str):
                data[field] = payload[field]
        if context_number(payload.get('context_window'), positive=True):
            data['context_window'] = payload['context_window']
    elif kind == 'turn_context':
        if isinstance(payload.get('model'), str):
            data['model'] = payload['model']
        if isinstance(payload.get('cwd'), str):
            data['cwd'] = payload['cwd']
            if data.get('turns'):
                data['turns'][-1]['cwd'] = payload['cwd']
    elif kind == 'event_msg':
        event = payload.get('type')
        if event in ('task_started', 'turn_started'):
            data['turn_state'] = 'working'
            data['turn_started'] = at
            data.setdefault('turns', []).append({'at': at, 'cwd': data.get('cwd', '')})
            del data['turns'][:-32]
            if context_number(payload.get('model_context_window'), positive=True):
                data['context_window'] = payload['model_context_window']
        elif event in ('task_complete', 'turn_complete'):
            data['turn_state'] = 'blocked' if payload.get('error') else 'done'
            data['ended'] = at
        elif event in ('turn_aborted', 'task_aborted'):
            data['turn_state'], data['ended'] = 'blocked', at
        elif event == 'user_message':
            task = _task_text(payload.get('message', payload.get('text')))
            if task:
                data['task'] = task
    elif kind == 'response_item':
        if payload.get('type') == 'message' and payload.get('role') == 'user':
            task = _task_text(payload.get('content'))
            if task:
                data['task'] = task
        elif payload.get('type') in ('function_call', 'custom_tool_call'):
            data['current_action'] = current_action(payload.get('name', ''))
    elif kind == 'token_usage_record':
        usage = payload.get('usage')
        if isinstance(usage, dict) and context_number(usage.get('input_tokens')):
            data['context_tokens'] = usage['input_tokens']


class BoardSummary:
    """Scan recent files once, then consume only complete appended records."""
    def __init__(self):
        self.inode = None
        self.offset = 0
        self.modified = None
        self.data = {}

    def refresh(self, path):
        st = path.stat()
        reset = (self.inode != st.st_ino or st.st_size < self.offset or
                 (st.st_size == self.offset and st.st_mtime_ns != self.modified))
        if reset:
            self.data, self.offset = {}, 0
        self.inode, self.modified = st.st_ino, st.st_mtime_ns
        with path.open('rb') as stream:
            stream.seek(self.offset)
            while True:
                start = stream.tell()
                line = stream.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    self.offset = start
                    break
                if not line.endswith(b'\n'):
                    if len(line) > MAX_RECORD_BYTES:
                        prefix = line[:1024]
                        kinds = TYPE.findall(prefix)
                        pieces = [line] if kinds and kinds[0] == b'session_meta' else None
                        size = len(line)
                        while line and not line.endswith(b'\n'):
                            line = stream.readline(MAX_RECORD_BYTES + 1)
                            size += len(line)
                            if pieces is not None:
                                if size <= MAX_META_BYTES:
                                    pieces.append(line)
                                else:
                                    pieces = None
                        if line:
                            self.offset = stream.tell()
                            if pieces is not None:
                                self._consume(b''.join(pieces))
                            else:
                                self._consume_oversized_event(prefix, kinds)
                            continue
                    self.offset = start
                    break
                self.offset = stream.tell()
                self._consume(line)
        return self.data, st.st_mtime

    def _consume(self, line):
        try:
            first = TYPE.search(line, 0, 1024)
            if not first:
                return
            outer = first.group(1)
            if outer in (b'event_msg', b'response_item'):
                second = TYPE.search(line, first.end(), 1024)
                inner = second.group(1) if second else None
                if outer == b'event_msg' and inner not in (b'task_started', b'turn_started', b'task_complete',
                        b'turn_complete', b'turn_aborted', b'task_aborted', b'user_message', b'token_count'):
                    if inner != b'thread_settings_applied':
                        self._activity(line)
                    return
                if outer == b'response_item':
                    if inner not in (b'message', b'function_call', b'custom_tool_call'):
                        self._activity(line)
                        return
                    if inner == b'message' and not USER_ROLE.search(line):
                        self._activity(line)
                        return
            if outer not in (b'session_meta', b'event_msg', b'turn_context', b'response_item', b'token_usage_record'):
                self._activity(line)
                return
            record = json.loads(line)
            if isinstance(record, dict):
                consume_rollout(self.data, record)
        except (ValueError, TypeError, UnicodeError, OverflowError):
            pass

    def _activity(self, prefix):
        match = RECORD_TIMESTAMP.search(prefix, 0, 1024)
        if match:
            try:
                at = timestamp(json.loads(match.group(1)))
            except (ValueError, UnicodeError):
                return
            if at is not None:
                self.data['last_at'] = at
                record_activity(self.data, at)

    def _consume_oversized_event(self, prefix, kinds):
        # Completion records may contain a very large final message. Preserve the
        # turn boundary without retaining that message.
        if len(kinds) < 2 or kinds[0] != b'event_msg' or kinds[1] not in (
                b'task_started', b'turn_started', b'task_complete', b'turn_complete', b'turn_aborted', b'task_aborted'):
            self._activity(prefix)
            return
        match = RECORD_TIMESTAMP.search(prefix)
        try:
            at = json.loads(match.group(1)) if match else None
            consume_rollout(self.data, {'type': 'event_msg', 'timestamp': at,
                                       'payload': {'type': kinds[1].decode('ascii')}})
        except (ValueError, UnicodeError):
            pass


class CodexBoardAdapter(Adapter):
    name = 'codex_sessions'

    def __init__(self, root, project):
        super().__init__()
        self.root, self.project, self.cache = Path(root), project, {}

    def collect(self, now):
        if not self.root.is_dir():
            raise UnavailableSource()
        seen = set()
        stale = []
        root = self.root.resolve()
        for path in self.root.glob('*/*/*/rollout-*.jsonl'):
            try:
                if not path.resolve().is_relative_to(root):
                    self.unsupported()
                    continue
                if now - path.stat().st_mtime > HISTORY_SECONDS:
                    continue
                seen.add(path)
                data, modified = self.cache.setdefault(path, BoardSummary()).refresh(path)
                thread = data.get('thread')
                if not thread:
                    match = re.search(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$', path.stem, re.I)
                    thread = match.group(1) if match else None
                if not thread:
                    continue
                cwd = data.get('cwd', '')
                started = data.get('started') or data.get('turn_started') or modified
                state = data.get('turn_state')
                chat = interactive_session(data) and not (data.get('parent_session') or data.get('parent_app_session'))
                status = ('hung' if state == 'working' and now - modified > 600 else state or 'waiting')
                if status == 'done' and chat:
                    status = 'waiting'
                end = data.get('ended') if status in ('done', 'blocked') else None
                observation = self.observation(dict(
                    id='codex:' + thread, kind='codex', thread=thread,
                    who='Codex', model=data.get('model', ''), cwd=cwd,
                    folder=Path(cwd).name if cwd else '—',
                    project=self.project(cwd) if cwd else 'Unknown',
                    task=data.get('task') or 'Untitled', status=status,
                    started=started, turn_started=data.get('turn_started'), ended=end,
                    waiting_since=data.get('ended') if status == 'waiting' else None,
                    last_at=data.get('last_at') or modified,
                    seconds=working_seconds(data, now, status, started, data.get('last_at')),
                    _stopped_seconds=working_seconds(data, now, 'hung', started, data.get('last_at')),
                    completed=status == 'done', current_action=data.get('current_action', ''),
                    context_tokens=data.get('context_tokens'), context_used=data.get('context_tokens'),
                    context_window=data.get('context_window'),
                    parent_session=data.get('parent_session', ''),
                    parent_app_session=data.get('parent_app_session', ''),
                    board_source='codex_rollout', codex_role='chat' if chat else 'worker', _rollout=True,
                    _run_id=data.get('run_id', ''), _turns=list(data.get('turns', []))),
                    data.get('last_at') or modified, 'inferred')
                agent = observation.value
                if not agent['completed'] and now - agent['last_at'] > CODEX_DEAD_SECONDS:
                    stale.append((path, observation))
                else:
                    yield observation
            except OSError:
                self.unavailable()
        if stale:
            writers = rollout_writers([path for path, _ in stale])
            if writers is None:
                self.unavailable()
            for path, observation in stale:
                agent = observation.value
                if writers is not None and path not in writers:
                    agent.update(status='crashed', completed=True, ended=agent['last_at'],
                                 seconds=agent['_stopped_seconds'],
                                 verdict='CRASHED: no live process and no activity for over 30 minutes')
                    agent.pop('waiting_since', None)
                yield observation
        self.cache = {path: value for path, value in self.cache.items() if path in seen}
