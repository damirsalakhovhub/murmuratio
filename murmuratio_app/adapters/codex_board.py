"""Bounded, incremental board summaries of saved Codex sessions."""
import json
from pathlib import Path
import re
import sqlite3
import subprocess

from murmuratio_app.adapters.base import Adapter
from murmuratio_app.adapters.activity import record_activity, working_seconds
from murmuratio_app.adapters.parsing import consume_codex
from murmuratio_app.adapters.brief import brief_view
from murmuratio_app.adapters.shared import CODEX_DEAD_SECONDS, JsonlPrefix, context_number, current_action, model_context_window
from murmuratio_app.common import one_line, timestamp
from murmuratio_app.naming import display_name, name_fields, orchestrator_name

HISTORY_SECONDS = 24 * 60 * 60
MAX_RECORD_BYTES = 512 * 1024
# Multimodal messages and compaction records can legitimately contain tens of
# megabytes of embedded images/history. Keep a hard limit for damaged input.
MAX_BUFFERED_BYTES = 64 * 1024 * 1024
IMAGE_DATA = re.compile(rb'([,{]\s*"image_url"\s*:\s*")data:image/[a-zA-Z0-9.+-]+;base64,[a-zA-Z0-9+/=]+(")(?=\s*[,}])')
AGENT_THREAD_SOURCES = ('agent_created_thread', 'agent_forked_thread', 'subagent')


def interactive_session(data):
    """Use launch metadata, never task text, to recognize a person's chat."""
    source, originator = data.get('source'), data.get('originator')
    # Codex Desktop records threads that an agent opened (for example a chat persona starting a
    # worker) with the same source and originator as a person's chat; only thread_source differs.
    if data.get('thread_source') in AGENT_THREAD_SOURCES:
        return False
    if source == 'exec' or originator == 'codex_exec' or isinstance(source, dict) and 'subagent' in source:
        return False
    return source in ('cli', 'tui', 'vscode') or originator in ('Codex Desktop', 'codex_cli_rs', 'codex-tui')


def thread_names(database):
    """Read-only thread titles from the Codex state database; none when unreadable."""
    try:
        if not database.is_file():
            return {}
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
            column = next((name for name in ('name', 'title') if name in columns), None)
            if 'id' not in columns or column is None:
                return {}
            rows = connection.execute(f"SELECT id, {column} FROM threads WHERE {column} IS NOT NULL AND {column} != ''").fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return {}
    return {thread: one_line(name) for thread, name in rows
            if isinstance(thread, str) and isinstance(name, str) and one_line(name)}


def index_names(path):
    """Thread titles set by thread/name/set, as Codex appends them to session_index.jsonl; the last one wins."""
    names = {}
    try:
        with path.open('rb') as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if (isinstance(record, dict) and isinstance(record.get('id'), str)
                        and isinstance(record.get('thread_name'), str) and one_line(record['thread_name'])):
                    names[record['id']] = one_line(record['thread_name'])
    except OSError:
        return {}
    return names


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
        source = data['source']
        subagent = source.get('subagent') if isinstance(source, dict) else None
        spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
        if isinstance(spawn, dict) and isinstance(spawn.get('parent_thread_id'), str):
            data['_parent_thread'] = spawn['parent_thread_id']
        data['originator'] = payload.get('originator')
        data['thread_source'] = payload.get('thread_source')
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
            data.pop('_unsupported_lifecycle', None)
            data['turn_state'] = 'working'
            data['turn_started'] = at
            data.setdefault('turns', []).append({'at': at, 'cwd': data.get('cwd', '')})
            del data['turns'][:-32]
            if context_number(payload.get('model_context_window'), positive=True):
                data['context_window'] = payload['model_context_window']
        elif event in ('task_complete', 'turn_complete'):
            data.pop('_unsupported_lifecycle', None)
            data['turn_state'] = 'blocked' if payload.get('error') else 'done'
            data['ended'] = at
        elif event in ('turn_aborted', 'task_aborted'):
            data.pop('_unsupported_lifecycle', None)
            data['turn_state'], data['ended'] = 'blocked', at
        elif isinstance(event, str) and event.startswith(('task_', 'turn_')) and event != 'turn_diff':
            data['_unsupported_lifecycle'] = True
            data['turn_state'] = 'unknown'
        elif event == 'user_message':
            task, source = display_name(text=payload.get('message', payload.get('text')))
            if source:
                data['task'] = task
                data['task_raw'] = payload.get('message', payload.get('text'))
    elif kind == 'response_item':
        if payload.get('type') == 'message' and payload.get('role') == 'user':
            content = payload.get('content')
            text = content if isinstance(content, str) else '\n'.join(p.get('text', '') for p in content or [] if isinstance(p, dict))
            task, source = display_name(text=text)
            if source:
                data['task'] = task
                data['task_raw'] = text
        elif payload.get('type') in ('function_call', 'custom_tool_call'):
            data['current_action'] = current_action(payload.get('name', ''))
    elif kind == 'token_usage_record':
        usage = payload.get('usage')
        if isinstance(usage, dict) and context_number(usage.get('input_tokens')):
            data['context_tokens'] = usage['input_tokens']
            data.pop('_context_invalidated', None)


class BoardSummary:
    """Scan recent files once, then consume only complete appended records."""
    def __init__(self):
        self.inode = None
        self.offset = 0
        self.modified = None
        self.size = None
        self.prefix = JsonlPrefix()
        self.data = {}

    def refresh(self, path):
        st = path.stat()
        reset = (self.inode != st.st_ino or st.st_size < self.offset or
                 (st.st_size == self.offset and st.st_mtime_ns != self.modified))
        with path.open('rb') as stream:
            if (not reset and st.st_size > self.offset and
                    (st.st_mtime_ns != self.modified or st.st_size != self.size)):
                reset = not self.prefix.matches(stream, self.offset)
            if reset:
                self.data, self.offset = {}, 0
                self.prefix.reset()
            self.inode, self.modified, self.size = st.st_ino, st.st_mtime_ns, st.st_size
            stream.seek(self.offset)
            while True:
                start = stream.tell()
                line = stream.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    self.offset = start
                    break
                if not line.endswith(b'\n'):
                    if len(line) > MAX_RECORD_BYTES:
                        pieces = [line]
                        size = len(line)
                        while line and not line.endswith(b'\n'):
                            line = stream.readline(MAX_RECORD_BYTES + 1)
                            size += len(line)
                            if pieces is not None:
                                if size <= MAX_BUFFERED_BYTES:
                                    pieces.append(line)
                                else:
                                    pieces = None
                        if line:
                            self.offset = stream.tell()
                            if pieces is not None:
                                self._consume(b''.join(pieces))
                            else:
                                self._uncertain_record()
                            continue
                    self.offset = start
                    break
                self.offset = stream.tell()
                self._consume(line)
            self.prefix.extend(stream, self.offset)
        return self.data, st.st_mtime

    def _consume(self, line):
        try:
            # Images are never used by the board. Drop only complete, unescaped
            # data URLs in image_url fields before decoding large JSON records.
            if len(line) > 8 * 1024 * 1024:
                line = IMAGE_DATA.sub(rb'\1\2', line)
            record = json.loads(line)
            if not isinstance(record, dict):
                self._uncertain_record()
                return
            if record.get('type') in ('session_meta', 'event_msg', 'turn_context', 'response_item', 'token_usage_record', 'compacted'):
                consume_rollout(self.data, record)
            else:
                at = timestamp(record.get('timestamp'))
                if at is not None:
                    self.data['last_at'] = at
                    record_activity(self.data, at)
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, OverflowError, RecursionError):
            self._uncertain_record()

    def _uncertain_record(self):
        # A discarded record may have contained a turn or compaction boundary.
        # Only this session becomes uncertain; never show stale context as current.
        self.data.update(_unsupported_lifecycle=True, turn_state='unknown',
                         context_tokens=None, _context_invalidated=True)

LIFECYCLE = (b'"task_started"', b'"turn_started"', b'"task_complete"', b'"turn_complete"',
             b'"turn_aborted"', b'"task_aborted"')
TAIL_BYTES = 256 * 1024


def open_worker(path, orchestrators=()):
    """Keep a known orchestrator or a worker whose last turn never finished.

    Reads the first record for the launch metadata and the file's tail for the last lifecycle event.
    A tail with no lifecycle event counts as open, so a long quiet turn is never missed."""
    with path.open('rb') as stream:
        first = stream.readline(MAX_RECORD_BYTES + 1)
        size = stream.seek(0, 2)
        stream.seek(max(0, size - TAIL_BYTES))
        tail = stream.read()
    data = {}
    try:
        record = json.loads(first)
        if isinstance(record, dict) and record.get('type') == 'session_meta':
            consume_rollout(data, record)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, OverflowError, RecursionError):
        return True
    if data.get('thread') in orchestrators:
        return True
    if interactive_session(data) and not (data.get('parent_session') or data.get('parent_app_session')):
        return False
    for line in reversed(tail.split(b'\n')[1:] if size > TAIL_BYTES else tail.split(b'\n')):
        if not any(marker in line for marker in LIFECYCLE):
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        payload = record.get('payload') if isinstance(record, dict) else None
        event = payload.get('type') if isinstance(payload, dict) else None
        if event in ('task_started', 'turn_started'):
            return True
        if event in ('task_complete', 'turn_complete', 'turn_aborted', 'task_aborted'):
            return False
    return True


class CodexBoardAdapter(Adapter):
    name = 'codex_sessions'

    def __init__(self, root, project):
        super().__init__()
        self.root, self.project, self.cache = Path(root), project, {}
        # Old rollouts: (mtime_ns, size, orchestrator ids) -> whether they remain eligible.
        self.old = {}
        # Rollouts found with no writer, by (mtime_ns, size): crashed until the file changes.
        self.crashed = {}
        self.index = (None, {})

    def names(self):
        """Thread titles: the session index, overridden by the state database when it is readable."""
        path = self.root.parent / 'session_index.jsonl'
        try:
            st = path.stat()
            key = (st.st_ino, st.st_size, st.st_mtime_ns)
        except OSError:
            key = None
        if key != self.index[0]:
            self.index = (key, index_names(path) if key else {})
        return {**self.index[1], **thread_names(self.root.parent / 'state_5.sqlite')}

    def collect(self, now, registry=None):
        seen, old_seen = set(), set()
        stale = []
        root = self.root.resolve()
        names = self.names()
        orchestrators = frozenset({thread for thread, name in names.items() if orchestrator_name(name)}
                                 | {record.get('provider_id') or thread for thread, record in (registry or {}).items()
                                    if record.get('kind') == 'codex' and record.get('role') == 'orchestrator'})
        for path in self.files('*/*/*/rollout-*.jsonl'):
            try:
                if not path.resolve().is_relative_to(root):
                    continue
                st = path.stat()
                if now - st.st_mtime > HISTORY_SECONDS:
                    # Old unfinished workers and named/registered orchestrators stay.
                    key = (st.st_mtime_ns, st.st_size, orchestrators)
                    known = self.old.get(path)
                    if not known or known[0] != key:
                        known = self.old[path] = (key, open_worker(path, orchestrators))
                    old_seen.add(path)
                    if not known[1]:
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
                linked = data.get('parent_session') or data.get('parent_app_session') or data.get('_parent_thread')
                # A Codex app orchestrator can be opened by a tool or by its predecessor's handoff, so its
                # launch metadata looks like a worker's; its thread title is the reliable role. A thread
                # title set in the app or by thread/name/set also names the card better than the last message.
                orchestrator = (not linked and data.get('source') != 'exec' and data.get('originator') != 'codex_exec'
                                and orchestrator_name(names.get(thread)))
                chat = orchestrator or interactive_session(data) and not (data.get('parent_session') or data.get('parent_app_session'))
                status = ('hung' if state == 'working' and now - modified > 600 else state or 'waiting')
                if status == 'done' and chat:
                    status = 'waiting'
                end = data.get('ended') if status in ('done', 'blocked') else None
                observation = self.observation(dict(
                    id='codex:' + thread, kind='codex', thread=thread,
                    who='Codex', model=data.get('model') or None, effort=data.get('effort'), cwd=cwd,
                    folder=Path(cwd).name if cwd else '—',
                    project=self.project(cwd) if cwd else 'Unknown',
                    **name_fields(titles=[('desktop', names.get(thread))], text=data.get('task_raw', data.get('task'))), status=status,
                    started=started, turn_started=data.get('turn_started'), ended=end,
                    waiting_since=data.get('ended') if status == 'waiting' else None,
                    last_at=data.get('last_at') or modified,
                    seconds=working_seconds(data, now, status, started, data.get('last_at')),
                    _stopped_seconds=working_seconds(data, now, 'hung', started, data.get('last_at')),
                    completed=status == 'done', current_action=data.get('current_action', ''),
                    context_tokens=data.get('context_tokens'), context_used=data.get('context_tokens'),
                    context_window=model_context_window(data, data.get('model')), _context_invalidated=data.get('_context_invalidated', False),
                    brief=brief_view(data, cwd),
                    parent_session=data.get('parent_session', ''),
                    parent_app_session=data.get('parent_app_session', ''),
                    board_source='codex_rollout',
                    codex_role='orchestrator' if orchestrator else 'chat' if chat else 'worker', _rollout=True,
                    _run_id=data.get('run_id', ''), _parent_thread=data.get('_parent_thread'),
                    _turns=list(data.get('turns', []))),
                    data.get('last_at') or modified, 'inferred')
                agent = observation.value
                if state not in ('done', 'blocked', 'unknown') and now - agent['last_at'] > CODEX_DEAD_SECONDS:
                    stale.append((path, observation, (st.st_mtime_ns, st.st_size)))
                else:
                    yield observation
            except OSError as exc:
                self.unavailable(exc)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                # A single unrecognized rollout must not poison siblings.
                continue
        if stale:
            unknown = [path for path, _, key in stale if self.crashed.get(path) != key]
            writers = rollout_writers(unknown) if unknown else set()
            if writers is None:
                self.unavailable()
            for path, observation, key in stale:
                agent = observation.value
                crashed = self.crashed.get(path) == key or (writers is not None and path not in writers)
                if crashed:
                    # A file with no writer stays crashed until it changes.
                    self.crashed[path] = key
                    if path in self.old:
                        self.old[path] = (self.old[path][0], False)
                    agent.update(status='crashed', completed=True, ended=agent['last_at'],
                                 seconds=agent['_stopped_seconds'],
                                 verdict='CRASHED: no live process and no activity for over 30 minutes')
                    agent.pop('waiting_since', None)
                yield observation
        self.cache = {path: value for path, value in self.cache.items() if path in seen}
        self.old = {path: value for path, value in self.old.items() if path in old_seen}
        self.crashed = {path: key for path, key in self.crashed.items() if path in seen}
