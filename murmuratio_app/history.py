"""Persistence of local derived quota history and routing."""
import json
from murmuratio_app.common import timestamp


def read_routing_history(path):
    """Valid advice in time order; incomplete or malformed records do not affect the board."""
    if path is None:
        return []
    rows = []
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(row, dict):
                    continue
                at = timestamp(row.get('at'))
                if at is not None and all(row.get(key) in ('claude', 'codex') for key in ('code_to', 'design_to')):
                    rows.append(dict(at=at, code_to=row['code_to'], design_to=row['design_to']))
    except FileNotFoundError:
        pass
    return sorted(rows, key=lambda row: row['at'])


def routing_history_addition(history, routing):
    """A new row only when the advice changed, so the file stays small."""
    row = dict(at=routing['at'], code_to=routing['code_to'], design_to=routing['design_to'])
    last = history[-1] if history else None
    if last and all(last[key] == row[key] for key in ('code_to', 'design_to')):
        return None
    return row


def append_routing_history(path, routing):
    addition = routing_history_addition(read_routing_history(path), routing)
    if path is not None and addition:
        with path.open('ab+') as stream:
            if stream.tell():
                stream.seek(-1, 2)
                if stream.read(1) != b'\n':
                    stream.write(b'\n')
            stream.write((json.dumps(addition, allow_nan=False) + '\n').encode('utf-8'))


def read_limit_history(path, now):
    """Load valid observations from the last 21 days and flag rows to prune."""
    history, prune = [], False
    try:
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    at = timestamp(row.get('at'))
                    used = row.get('used_percent')
                    if (row.get('tool') not in ('claude', 'codex') or row.get('window') not in ('5h', 'week')
                            or at is None or not now - 21 * 86400 <= at <= now
                            or isinstance(used, bool) or not isinstance(used, (int, float))
                            or not 0 <= used <= 100):
                        prune = True
                        continue
                    history.append(dict(at=at, tool=row['tool'], window=row['window'],
                                        used_percent=used, resets_at=timestamp(row.get('resets_at'))))
                except (ValueError, TypeError, AttributeError):
                    prune = True
    except FileNotFoundError:
        pass
    history.sort(key=lambda row: row['at'])
    return history, prune


def limit_history_additions(limits, history, now):
    """Record only changed usage or reset times, once per tool/window."""
    latest = {(row['tool'], row['window']): row for row in history}
    additions = []
    for tool, windows in limits.items():
        for window in windows:
            name = {300: '5h', 10080: 'week'}.get(window['window_minutes'])
            if not name:
                continue
            row = dict(at=now, tool=tool, window=name, used_percent=window['used_percent'],
                       resets_at=window['resets_at'])
            previous = latest.get((tool, name))
            if not previous or any(row[key] != previous[key] for key in ('used_percent', 'resets_at')):
                additions.append(row)
                latest[tool, name] = row
    return additions


def replace_text(path, value):
    """Publish a complete local snapshot so readers never see a partial write."""
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(value)
    temporary.replace(path)
