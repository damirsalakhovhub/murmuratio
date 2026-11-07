"""Normalize Claude and Codex source records."""
import math
from pathlib import Path
import re
import shlex
from murmuratio_app.common import one_line, timestamp
from murmuratio_app.adapters.shared import (CLAUDE_TURN_FINISHED, current_action, content_action, context_number)
from murmuratio_app.adapters.limits import limit_window
from murmuratio_app.adapters.activity import start_turn, record_activity, end_turn

from murmuratio_app.metrics.usage import USAGE_WEIGHTS, add_usage


def codex_launch(command, cwd, at):
    """Record only identity, location and time, never the shell command itself."""
    if not isinstance(command, str):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()')
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    executable = next((index for index, token in enumerate(tokens) if Path(token).name == 'codex'), None)
    if executable is None:
        return None
    end = next((index for index in range(executable + 1, len(tokens))
                if tokens[index] and all(c in ';&|()' for c in tokens[index])), len(tokens))
    execute = next((index for index in range(executable + 1, end) if tokens[index] == 'exec'), None)
    if execute is None:
        return None
    launch_cwd = cwd
    for index, token in enumerate(tokens):
        target = None
        if token == 'cd' and index < executable and index + 1 < len(tokens):
            target = tokens[index + 1]
        elif token in ('-C', '--cd') and executable < index < end and index + 1 < end:
            target = tokens[index + 1]
        elif token.startswith('--cd=') and executable < index < end:
            target = token[5:]
        if target:
            launch_cwd = str(Path(launch_cwd) / target) if launch_cwd and not Path(target).is_absolute() else target
    return {'at': at, 'cwd': str(Path(launch_cwd)) if launch_cwd else '', 'thread': None}


def recorded_thread(value):
    if isinstance(value, list):
        return next((found for item in value if (found := recorded_thread(item))), None)
    if isinstance(value, dict):
        thread = value.get('thread_id') or value.get('threadId')
        if isinstance(thread, str) and thread:
            return thread
        return recorded_thread(value.get('content') or value.get('text'))
    if not isinstance(value, str):
        return None
    match = re.search(r'(?i)\bthread[_ -]?id["\']?\s*[:=]\s*["\']?([a-z0-9_-]+)', value[:131072])
    if match:
        return match.group(1)
    match = re.search(r'rollout-[^\s"\']*?([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl', value[:131072], re.I)
    return match.group(1) if match else None

def consume_usage(data, record, include_models=True):
    """Normalize an assistant message before passing it to the usage metric."""
    message = record.get('message')
    if record.get('type') != 'assistant' or not isinstance(message, dict) or not isinstance(message.get('usage'), dict):
        return
    raw = message['usage']
    usage = {}
    for key in USAGE_WEIGHTS:
        value = raw.get(key)
        if value is None:
            usage[key] = 0
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            usage[key] = value
        else:
            raise ValueError('Unsupported usage measurement')
    usage.update(at=timestamp(record.get('timestamp')), message_id=message.get('id') or record.get('uuid'),
                 model=message.get('model') if isinstance(message.get('model'), str) else 'other')
    add_usage(data, usage, include_models)


def consume_credits(data, record):
    """Keep the Codex credit balance each time it changes in a rollout."""
    payload = record.get('payload')
    limits = payload.get('rate_limits') if isinstance(payload, dict) else None
    credits = limits.get('credits') if isinstance(limits, dict) else None
    if not isinstance(credits, dict):
        return
    try:
        balance = float(credits.get('balance'))
    except (TypeError, ValueError):
        return
    at = timestamp(record.get('timestamp'))
    if at is None or not math.isfinite(balance):
        return
    points, last = data.setdefault('points', []), data.get('latest')
    if not points or points[-1][1] != balance:
        # Keep when the old balance was last seen, so a spend starts there and not at its first sighting.
        if last and points and points[-1] != last:
            points.append(last)
        points.append((at, balance))
    data['latest'] = (at, balance)


def consume_codex_meter(data, record, include_credits=True, include_burn=True):
    """Collect only enabled Codex credit and per-model usage measurements."""
    if include_credits:
        consume_credits(data, record)
    if not include_burn:
        return
    payload = record.get('payload')
    if not isinstance(payload, dict):
        return
    if record.get('type') == 'turn_context' and isinstance(payload.get('model'), str):
        data['model'] = payload['model']
        return
    window = limit_window((payload.get('rate_limits') or {}).get('primary')) if isinstance(payload.get('rate_limits'), dict) else None
    at = timestamp(record.get('timestamp'))
    if not window or at is None or window['window_minutes'] != 10080:
        return
    usage = data.setdefault('usage', [])
    if not usage or usage[-1][1] != window['used_percent']:
        usage.append((at, window['used_percent'], window['resets_at'], data.get('model') or 'other'))


def consume_codex(data, record):
    at = timestamp(record.get('timestamp'))
    event = record.get('payload') if record.get('type') == 'event_msg' else record
    event_type = event.get('type') if isinstance(event, dict) else None
    if event_type in ('task_started', 'turn_started', 'turn.started'):
        start_turn(data, at)
    if at is not None and event_type != 'thread_settings_applied':
        data['last_at'] = at
        # Resume setup can arrive hours after the previous turn's last work.
        if record.get('type') not in ('session_meta', 'turn_context'):
            record_activity(data, at)
    if event_type in ('task_complete', 'turn_complete', 'turn.completed',
                      'turn_aborted', 'task_aborted', 'turn.failed'):
        end_turn(data, at)
    if record.get('type') == 'thread.started':
        data['thread'] = record.get('thread_id')
    item = record.get('item')
    if isinstance(item, dict):
        action = item.get('command') or item.get('text') or item.get('message')
        if one_line(action):
            data['last_action'] = one_line(action)
        if item.get('type') == 'command_execution':
            data['current_action'] = current_action('command', item.get('command'))
        elif item.get('type') == 'file_change':
            data['current_action'] = 'editing files'
        elif item.get('type') == 'mcp_tool_call':
            data['current_action'] = current_action('mcp__' + one_line(item.get('tool')), server=item.get('server', ''))
    # exec turn.completed usage is cumulative spend, not current context.
    if record.get('type') == 'token_usage_record':
        usage = record.get('usage')
        if isinstance(usage, dict):
            data['context_tokens'] = usage.get('input_tokens')
    event = record.get('payload') if record.get('type') == 'event_msg' else record
    if isinstance(event, dict) and event.get('type') == 'token_count':
        info = event.get('info')
        if isinstance(info, dict):
            usage = info.get('last_token_usage')
            if isinstance(usage, dict) and context_number(usage.get('input_tokens')):
                # Codex input_tokens already includes cached_input_tokens.
                data['context_tokens'] = usage['input_tokens']
            if context_number(info.get('model_context_window'), positive=True):
                data['context_window'] = info['model_context_window']


def consume_claude(data, record):
    kind = record.get('type')
    if kind not in ('user', 'assistant'):
        return
    message = record.get('message')
    if record.get('isSidechain'):
        return
    if not isinstance(message, dict):
        raise ValueError('Unsupported Claude message')
    at = timestamp(record.get('timestamp'))
    if at is not None:
        data.setdefault('started', at)
        data['last_at'] = at
    if isinstance(record.get('cwd'), str):
        data['cwd'] = record['cwd']
    action = content_action(message)
    if action:
        data['last_action'] = action
    content = message.get('content')
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'tool_use':
                inputs = part.get('input')
                inputs = inputs if isinstance(inputs, dict) else {}
                data['current_action'] = current_action(part.get('name', ''), inputs.get('file_path') or inputs.get('command'), part.get('server_name', ''))
                if part.get('name') == 'Bash':
                    launch = codex_launch(inputs.get('command'), data.get('cwd', ''), at)
                    if launch:
                        launch['tool_id'] = part.get('id')
                        data.setdefault('_codex_launches', []).append(launch)
                        del data['_codex_launches'][:-256]
            elif isinstance(part, dict) and part.get('type') == 'tool_result':
                tool_id = part.get('tool_use_id')
                launch = next((item for item in reversed(data.get('_codex_launches', []))
                               if tool_id and item.get('tool_id') == tool_id), None)
                if launch:
                    launch['thread'] = recorded_thread(part.get('content')) or launch['thread']
    tool_result = isinstance(content, list) and any(isinstance(p, dict) and p.get('type') == 'tool_result' for p in content)
    if kind == 'user' and not tool_result:
        if at is not None and not record.get('isMeta'):
            data['turn_started'] = at
            start_turn(data, at)
        texts = [content] if isinstance(content, str) else [p.get('text', '') for p in content or [] if isinstance(p, dict) and p.get('type') == 'text']
        first_line = next((line.strip() for text in texts if isinstance(text, str) for line in text.splitlines() if line.strip()), '')
        if first_line:
            data.setdefault('first_user', first_line)
    data['last_turn'] = message.get('stop_reason') if kind == 'assistant' else 'user'
    record_activity(data, at)
    if kind == 'assistant' and message.get('stop_reason') in CLAUDE_TURN_FINISHED and at is not None:
        data['waiting_since'] = at
        end_turn(data, at)
    if kind == 'assistant':
        model = message.get('model')
        if isinstance(model, str) and not model.startswith('<'):
            if model != data.get('model'):
                data.pop('context_window', None)
            data['model'] = model
        usage = message.get('usage')
        for source in (usage, message, record):
            if not isinstance(source, dict):
                continue
            window = source.get('model_context_window', source.get('context_window'))
            if context_number(window, positive=True):
                data['context_window'] = window
                break
        if isinstance(usage, dict):
            values = [usage.get(k, 0) for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')]
            if 'input_tokens' in usage and all(isinstance(v, int) and v >= 0 for v in values):
                data['context_tokens'] = sum(values)


def consume_claude_sidechain(data, record):
    """Read a subagent transcript under its owning Claude session."""
    consume_claude(data, {**record, 'isSidechain': False})
