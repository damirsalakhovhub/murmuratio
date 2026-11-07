"""Incremental JSONL reading and local status/action interpretation."""
import json
from pathlib import Path
import re
from murmuratio_app.common import one_line, timestamp

def codex_status(run, now, last_event, alive):
    if run.get('status') == 'done':
        return 'blocked' if one_line(run.get('verdict')).startswith('BLOCKED') else 'done'
    if not alive:
        return 'crashed'
    activity = last_event if last_event is not None else timestamp(run.get('started'))
    return 'hung' if activity is not None and now - activity > 600 else 'working'


def claude_status(modified, last_turn, now, spawned=False):
    if now - modified >= 86400:
        return None
    if last_turn == 'end_turn':
        return 'done' if spawned else 'waiting'
    return 'working' if now - modified <= 120 else None


def claude_label(model):
    short = re.sub(r'-(?:\d{8}|latest)$', '', model)
    match = re.fullmatch(r'claude-(opus|sonnet|haiku|fable)(?:-(\d+(?:[-.]\d+)*))?', short)
    if match:
        family, version = match.groups()
        return 'Claude ' + family.title() + (' ' + version.replace('-', '.') if version else '')
    return 'Claude' + (' ' + model if model else '')


def content_action(message):
    content = message.get('content')
    if isinstance(content, str):
        return one_line(content)
    if not isinstance(content, list):
        return ''
    for part in reversed(content):
        if not isinstance(part, dict):
            continue
        if part.get('type') == 'text' and one_line(part.get('text')):
            return one_line(part['text'])
        if part.get('type') == 'tool_use':
            inputs = part.get('input')
            inputs = inputs if isinstance(inputs, dict) else {}
            detail = inputs.get('command') or inputs.get('description') or inputs.get('file_path') or ''
            return one_line(part.get('name', '')) + (': ' + one_line(detail) if detail else '')
        if part.get('type') == 'tool_result':
            return 'Tool result'
    return ''


def clean_action(value):
    value = re.sub(r'(?i)(?:(?:local|session|thread|call|agent)[_-])?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', '', one_line(value))
    value = re.sub(r'(?i)(?<![a-z0-9])[0-9a-f]{7,64}(?![a-z0-9])', '', value)
    value = re.sub(r'\b(?:local|session|thread|call|agent)[_-][a-zA-Z0-9_-]{8,}\b', '', value)
    return one_line(re.sub(r'[_-]+(?=\.|\s|$)', '', value)).strip(' _-')


def current_action(tool, detail='', server=''):
    """A short activity label, never an assistant report or a tool result dump."""
    name, detail = one_line(tool).lower(), one_line(detail)
    if name.startswith('mcp'):
        readable = clean_action(server)
        if readable and readable.lower() != 'mcp' and not readable.isdigit():
            return {'figma': 'Figma', 'browser': 'Browser', 'sessions': 'Sessions'}.get(readable.lower(), readable.replace('_', ' ').title())
        for key, label in (('figma', 'Figma'), ('browser', 'Browser'), ('cua_repl', 'Browser'), ('sessions', 'Sessions')):
            if key in name:
                return label
        return 'MCP tool'
    filename = clean_action(Path(detail).name) if detail.startswith('/') else ''
    filename = filename if filename and not filename.startswith('.') else 'files'
    if any(word in name for word in ('edit', 'write', 'patch')):
        return 'editing ' + filename
    if 'read' in name:
        return 'reading ' + filename
    if any(word in name for word in ('search', 'grep', 'glob')):
        return 'searching files'
    if any(word in name for word in ('bash', 'command', 'exec')):
        if re.search(r'\b(test\w*|pytest|unittest)\b', detail):
            return 'tests'
        if re.search(r'\b(git diff|git status)\b', detail):
            return 'reviewing changes'
        if re.search(r'\b(rg|grep|find)\b', detail):
            return 'searching files'
        if re.search(r'\b(cat|sed|head|tail)\b', detail):
            return 'reading files'
        return 'running a command'
    return clean_action(tool).replace('_', ' ')[:80] or 'Using a tool'


class JsonlSummary:
    """Keep only summary fields, and consume complete appended JSONL records."""
    def __init__(self):
        self.inode = None
        self.offset = 0
        self.modified = None
        self.data = {}
        self.malformed = 0
        self.record_types = set()

    def refresh(self, path, consume):
        st = path.stat()
        if (st.st_ino != self.inode or st.st_size < self.offset or
                (self.malformed and st.st_mtime_ns != self.modified) or
                (st.st_size == self.offset and st.st_mtime_ns != self.modified)):
            self.offset = 0
            self.data = {}
            self.malformed = 0
            self.record_types = set()
        self.inode, self.modified = st.st_ino, st.st_mtime_ns
        with path.open('rb') as stream:
            stream.seek(self.offset)
            while True:
                line = stream.readline()
                if not line or not line.endswith(b'\n'):
                    break
                self.offset = stream.tell()
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    self.malformed += 1
                    continue
                if isinstance(record, dict):
                    kind = record.get('type')
                    if kind is None or isinstance(kind, str):
                        self.record_types.add(kind)
                    else:
                        self.malformed += 1
                    try:
                        consume(self.data, record)
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        self.malformed += 1
                else:
                    self.malformed += 1
        return self.data, st.st_mtime


def context_number(value, positive=False):
    return isinstance(value, int) and not isinstance(value, bool) and value >= (1 if positive else 0)


def claude_context_window(data, model):
    if context_number(data.get('context_window'), positive=True):
        return data['context_window']
    if claude_label(model) in ('Claude Opus 5.5', 'Claude Sonnet 5.5'):
        return 1000000
    return None
