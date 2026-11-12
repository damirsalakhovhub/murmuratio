"""Facts about an agent's latest turn for the card panel, read from records already consumed.

Only what the transcript shows: the request that opened the turn, the agent's latest text, commits
git printed, files the agent wrote, and a pending question or approval. Nothing is inferred.
"""
from collections import Counter
from pathlib import Path
import re

REPLY_CHARS = 1500
REQUEST_CHARS = 300
COMMITS_KEPT = 8
FILES_KEPT = 200
# `git commit` prints "[branch abc1234] subject"; a root commit adds "(root-commit)".
COMMIT_LINE = re.compile(r'^\[[^\]\s]+(?: \(root-commit\))? ([0-9a-f]{7,40})\] (.+)$', re.M)
PATCH_FILE = re.compile(r'^\*\*\* (?:Add|Update) File: (.+)$', re.M)
REMINDER = re.compile(r'<(system-reminder|local-command-stdout|command-[a-z-]+)>.*?</\1>', re.S)
MACHINE_PREFIXES = ('<environment_context>', '<permissions instructions>', '<collaboration_mode>',
                    '# AGENTS.md instructions', '<user_instructions>', '<system>', '<turn_aborted>',
                    '[Request interrupted', '<codex_delegation>')
CLAUDE_WRITE_TOOLS = ('Write', 'Edit', 'MultiEdit', 'NotebookEdit')
CLAUDE_ASKS = {'AskUserQuestion': 'question', 'ExitPlanMode': 'plan'}


def _texts(content):
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out = []
    for part in content:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and isinstance(part.get('text'), str):
            out.append(part['text'])
    return out


def request_text(content):
    """The owner's words without machine context; empty when the message holds only context."""
    text = REMINDER.sub('', '\n'.join(_texts(content))).strip()
    if not text or text.startswith(MACHINE_PREFIXES):
        return ''
    # Launchers put marker tags such as <stall-guard-worker .../> before the task.
    text = re.sub(r'^(?:\s*<[A-Za-z][\w.:-]*(?:\s[^<>]*)?/>)+', '', text).strip()
    line = ' '.join(text.split())
    return line[:REQUEST_CHARS]


def _start(data, request):
    data['brief'] = {'request': request, 'reply': '', 'commits': [], 'files': Counter(), 'asks': {}}


def _brief(data):
    if 'brief' not in data:
        _start(data, '')
    return data['brief']


def _reply(data, text):
    text = text.strip() if isinstance(text, str) else ''
    if text:
        _brief(data)['reply'] = text[:REPLY_CHARS + 1]


def _commits(data, output):
    if not isinstance(output, str) or '] ' not in output:
        return
    commits = _brief(data)['commits']
    for sha, subject in COMMIT_LINE.findall(output[:65536]):
        if not any(sha.startswith(known) or known.startswith(sha) for known, _ in commits):
            commits.append((sha[:7], ' '.join(subject.split())[:160]))
    del commits[:-COMMITS_KEPT]


def _file(data, path):
    if not isinstance(path, str) or not path:
        return
    path = path.removeprefix('file://')
    files = _brief(data)['files']
    if path in files or len(files) < FILES_KEPT:
        files[path] += 1


def consume_claude_brief(data, record, kind, message, tool_result):
    content = message.get('content')
    if kind == 'user' and not tool_result:
        if record.get('isMeta') or record.get('isCompactSummary'):
            return
        request = request_text(content)
        if request:
            _start(data, request)
        return
    if not isinstance(content, list):
        if kind == 'assistant' and isinstance(content, str):
            _reply(data, content)
        return
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get('type') == 'text' and kind == 'assistant':
            _reply(data, part.get('text'))
        elif part.get('type') == 'tool_use':
            inputs = part.get('input') if isinstance(part.get('input'), dict) else {}
            name = part.get('name') if isinstance(part.get('name'), str) else ''
            if name in CLAUDE_WRITE_TOOLS:
                _file(data, inputs.get('file_path') or inputs.get('notebook_path'))
            if name in CLAUDE_ASKS and isinstance(part.get('id'), str):
                _brief(data)['asks'][part['id']] = CLAUDE_ASKS[name]
        elif part.get('type') == 'tool_result':
            if 'brief' in data and isinstance(part.get('tool_use_id'), str):
                data['brief']['asks'].pop(part['tool_use_id'], None)
            for text in _texts(part.get('content')):
                _commits(data, text)


def _codex_item(data, item):
    kind = item.get('type')
    if kind in ('UserMessage', 'user_message'):
        request = request_text(item.get('content') or item.get('text'))
        if request:
            _start(data, request)
    elif kind in ('AgentMessage', 'agent_message'):
        _reply(data, '\n'.join(_texts(item.get('content'))) or item.get('text'))
    elif kind in ('CommandExecution', 'command_execution'):
        _commits(data, item.get('aggregated_output'))
    elif kind in ('FileChange', 'file_change'):
        changes = item.get('changes')
        if isinstance(changes, dict):
            for path, change in changes.items():
                if not (isinstance(change, dict) and change.get('type') == 'delete'):
                    _file(data, path)
        elif isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict) and change.get('kind') != 'delete':
                    _file(data, change.get('path'))


def consume_codex_brief(data, record):
    """Rollout records (event_msg / response_item) and `codex exec --json` events."""
    kind = record.get('type')
    payload = record.get('payload') if isinstance(record.get('payload'), dict) else {}
    event = payload.get('type')
    if kind in ('item.completed', 'item.started') and isinstance(record.get('item'), dict):
        if kind == 'item.completed':
            _codex_item(data, record['item'])
        return
    if kind == 'event_msg':
        if event == 'token_count':
            return
        asks = data['brief']['asks'] if 'brief' in data else {}
        if isinstance(event, str) and event.endswith('_approval_request'):
            _brief(data)['asks']['approval'] = 'approval'
            return
        asks.pop('approval', None)
        if event == 'item_completed' and isinstance(payload.get('item'), dict):
            _codex_item(data, payload['item'])
        elif event == 'user_message':
            request = request_text(payload.get('message'))
            if request:
                _start(data, request)
        elif event == 'agent_message':
            _reply(data, payload.get('message'))
        elif event in ('task_complete', 'turn_complete'):
            _reply(data, payload.get('last_agent_message'))
        elif event == 'exec_command_end':
            _commits(data, payload.get('aggregated_output') or payload.get('stdout'))
        elif event == 'patch_apply_end' and isinstance(payload.get('changes'), dict):
            for path in payload['changes']:
                _file(data, path)
    elif kind == 'response_item':
        if event in ('function_call', 'custom_tool_call'):
            name = payload.get('name') if isinstance(payload.get('name'), str) else ''
            if name.startswith('request_user_input') and isinstance(payload.get('call_id'), str):
                _brief(data)['asks'][payload['call_id']] = 'question'
            arguments = payload.get('input') if event == 'custom_tool_call' else payload.get('arguments')
            if name == 'apply_patch' and isinstance(arguments, str):
                for path in PATCH_FILE.findall(arguments):
                    _file(data, path.strip())
        elif event in ('function_call_output', 'custom_tool_call_output'):
            if 'brief' in data and isinstance(payload.get('call_id'), str):
                data['brief']['asks'].pop(payload['call_id'], None)
            for text in _texts(payload.get('output')):
                _commits(data, text)


def _relative(path, cwd):
    try:
        if cwd and Path(path).is_absolute():
            return str(Path(path).relative_to(cwd))
    except ValueError:
        pass
    return path


def brief_view(data, cwd=''):
    """The JSON the board sends: small, capped, and only fields that have something to show."""
    brief = data.get('brief') if isinstance(data, dict) else None
    if not brief:
        return None
    reply = brief['reply']
    files = brief['files']
    asks = list(dict.fromkeys(brief['asks'].values()))
    view = {'request': brief['request'], 'reply': reply[:REPLY_CHARS], 'reply_cut': len(reply) > REPLY_CHARS,
            'commits': [list(c) for c in brief['commits']], 'files': len(files),
            'top_files': [_relative(path, cwd) for path, _ in files.most_common(3)], 'asks': asks}
    if not any((view['request'], view['reply'], view['commits'], view['files'], view['asks'])):
        return None
    return view
