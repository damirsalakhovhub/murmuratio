"""Persistence of local derived quota history and routing."""
import json
from murmuratio_app.common import timestamp

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
