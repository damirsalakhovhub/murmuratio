"""Adapters for Claude's cached limits and Codex rollout limits."""
import json
from pathlib import Path
import time
from murmuratio_app.adapters.base import Adapter, AbsentSource, UnavailableSource, UnsupportedSource
from murmuratio_app.adapters.limits import limit_window, rollout_limits
from murmuratio_app.common import timestamp


CLAUDE_LIMITS_STALE_SECONDS = 30 * 60


class ClaudeLimitsAdapter(Adapter):
    name = 'claude_limits'

    def __init__(self, path, legacy_path=None, clock=time.time):
        super().__init__()
        self.path = Path(path)
        self.paths = [self.path] + ([Path(legacy_path)] if legacy_path is not None else [])
        self.clock = clock
        self.age_seconds = None
        self.stale = False

    def collect(self):
        self.age_seconds = None
        self.stale = False
        candidates, invalid, unreadable = [], False, False
        for path in self.paths:
            try:
                data = json.loads(path.read_text())
                limits = data.get('rate_limits') if isinstance(data, dict) else None
                if not isinstance(limits, dict) or (limits and not any(k in limits for k in ('five_hour', 'seven_day'))):
                    raise ValueError('Unknown limits format')
                at = timestamp(data['at']) if 'at' in data else path.stat().st_mtime
                if at is None:
                    raise ValueError('Invalid cache timestamp')
                windows, malformed = [], False
                for key, minutes in (('five_hour', 300), ('seven_day', 10080)):
                    window = limit_window(limits.get(key), minutes, 'used_percentage')
                    if window:
                        windows.append(window)
                    elif key in limits and limits[key] is not None:
                        malformed = True
                candidates.append((at, windows, malformed))
            except FileNotFoundError:
                continue
            except (ValueError, TypeError, UnicodeError):
                invalid = True
            except OSError:
                unreadable = True
        usable = [candidate for candidate in candidates if candidate[1]]
        if not usable:
            if candidates:
                at, _, malformed = max(candidates, key=lambda item: item[0])
                self.age_seconds = max(0, self.clock() - at)
                invalid = invalid or malformed
            if invalid:
                raise UnsupportedSource()
            if unreadable:
                raise UnavailableSource()
            if not candidates:
                raise AbsentSource()
            return
        at, windows, malformed = max(usable, key=lambda item: item[0])
        now = self.clock()
        self.age_seconds = max(0, now - at)
        self.stale = (self.age_seconds > CLAUDE_LIMITS_STALE_SECONDS
                      or any(window['resets_at'] is not None and window['resets_at'] <= now for window in windows))
        if invalid or malformed:
            self.unsupported()
        elif unreadable:
            self.unavailable()
        elif self.stale:
            self.status = 'stale'
        for window in windows:
            yield self.observation(window, at)


class CodexLimitsAdapter(Adapter):
    name = 'codex_limits'

    def __init__(self, root, clock=time.time):
        super().__init__()
        self.root = Path(root)
        self.clock = clock
        self.age_seconds = None
        self.stale = False

    def collect(self):
        self.age_seconds = None
        self.stale = False
        files = []
        root = self.root.resolve()
        for path in self.files('*/*/*/rollout-*.jsonl'):
            try:
                if not path.resolve().is_relative_to(root):
                    self.unsupported()
                    continue
                files.append((path.stat().st_mtime, path))
            except OSError:
                self.unavailable()
        for _, path in sorted(files, reverse=True)[:5]:
            try:
                with path.open('rb') as handle:
                    handle.seek(max(0, path.stat().st_size - 400000))
                    content = handle.read().decode('utf-8', 'ignore')
                lines = content.splitlines()
            except OSError:
                self.unavailable()
                continue
            parsed, recognized = False, False
            complete = lines if content.endswith('\n') else lines[:-1]
            known = {'session_meta', 'event_msg', 'turn_context', 'response_item', 'token_usage_record', 'compacted'}
            for line in reversed(complete):
                try:
                    candidate = json.loads(line)
                    if isinstance(candidate, dict):
                        parsed = True
                        if candidate.get('type') in known or '"rate_limits"' in line:
                            recognized = True
                            break
                except (ValueError, TypeError):
                    continue
            if complete and (not parsed or not recognized):
                self.unsupported()
            for i, line in enumerate(reversed(lines)):
                if '"rate_limits"' not in line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    # A trailing partial append is expected and will be retried.
                    if i != 0 or content.endswith('\n'):
                        self.unsupported()
                    continue
                windows = rollout_limits(data)
                if windows:
                    at = timestamp(data.get('timestamp')) if isinstance(data, dict) else None
                    now = self.clock()
                    self.age_seconds = max(0, now - at) if at is not None else None
                    self.stale = (self.age_seconds is None or self.age_seconds > CLAUDE_LIMITS_STALE_SECONDS
                                  or any(window['resets_at'] is not None and window['resets_at'] <= now for window in windows))
                    if self.stale and self.status == 'available':
                        self.status = 'stale'
                    for window in windows:
                        yield self.observation(window, at)
                    return
                stack = [data]
                while stack:
                    item = stack.pop()
                    if isinstance(item, dict):
                        if 'rate_limits' in item and item['rate_limits'] is not None:
                            self.unsupported()
                        stack.extend(item.values())
                    elif isinstance(item, list):
                        stack.extend(item)
