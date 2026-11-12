"""Adapters for Claude's cached limits and Codex rollout limits."""
import json
import math
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time
from murmuratio_app.adapters.base import Adapter, AbsentSource, UnavailableSource, UnsupportedSource
from murmuratio_app.adapters.limits import limit_window, rollout_limits
from murmuratio_app.adapters.shared import JsonlSummary
from murmuratio_app.common import timestamp
from murmuratio_app import __version__


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
        self.live = None  # (at, windows) from app_server_limits, when enabled

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
        readable, unreadable, sample = False, False, None
        known = {'session_meta', 'event_msg', 'turn_context', 'response_item', 'token_usage_record', 'compacted'}
        for _, path in sorted(files, reverse=True)[:5]:
            summary = JsonlSummary()
            try:
                data = summary.refresh_tail(path, self.consume, 400000)
            except OSError as exc:
                self.unavailable(exc)
                continue
            if data.get('sample'):
                readable = True
                sample = data['sample']
                break
            if summary.unreadable(known) or data.get('invalid_limits'):
                unreadable = True
            elif summary.readable_types.intersection(known):
                readable = True
        if unreadable and not readable:
            self.unsupported()
        if sample or self.live:
            yield from self.emit(*(sample or self.live))

    @staticmethod
    def consume(data, record):
        windows = rollout_limits(record)
        if windows:
            data['sample'] = (timestamp(record.get('timestamp')), windows)
            return
        stack = [record]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if 'rate_limits' in item and item['rate_limits'] is not None:
                    data['invalid_limits'] = True
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)

    def emit(self, at, windows):
        """Yield the newest sample: the rollout's, or a newer live read from Codex itself."""
        if self.live and (at is None or self.live[0] > at):
            at, windows = self.live
        now = self.clock()
        self.age_seconds = max(0, now - at) if at is not None else None
        self.stale = (self.age_seconds is None or self.age_seconds > CLAUDE_LIMITS_STALE_SECONDS
                      or any(window['resets_at'] is not None and window['resets_at'] <= now for window in windows))
        if self.stale and self.status == 'available':
            self.status = 'stale'
        for window in windows:
            yield self.observation(window, at)


def app_server_limits(command, timeout=15, clock=time.time):
    """Read the account's current Codex limits the way the Codex app does.

    Starts `codex app-server` and sends one `account/rateLimits/read` request: a read of the
    usage status, with no model call and no write. Returns (at, windows, credits balance or None)."""
    if os.sep in command:
        command = os.path.abspath(command)  # the server runs in a temporary directory
    try:
        process = subprocess.Popen([command, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, cwd=tempfile.gettempdir())
    except OSError:
        raise UnavailableSource() from None
    deadline, sent = time.monotonic() + timeout, False
    try:
        def send(message):
            process.stdin.write(json.dumps(message).encode() + b'\n')
            process.stdin.flush()
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'murmuratio', 'version': __version__}}})
        def messages():
            # Raw reads: a buffered readline can hide a second line from select.
            pending = b''
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    return
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    return
                pending += chunk
                if len(pending) > 4 * 1024 * 1024:
                    return
                *lines, pending = pending.split(b'\n')
                for line in lines:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        for message in messages():
            if not isinstance(message, dict):
                continue
            if message.get('id') in (1, 2) and 'error' in message:
                break  # for example not signed in
            if message.get('id') == 1 and not sent:
                send({'method': 'initialized'})
                send({'id': 2, 'method': 'account/rateLimits/read', 'params': None})
                sent = True
            elif message.get('id') == 2:
                result = message.get('result')
                limits = result.get('rateLimits') if isinstance(result, dict) else None
                if not isinstance(limits, dict):
                    raise UnsupportedSource()
                windows = []
                for key in ('primary', 'secondary'):
                    item = limits.get(key)
                    if isinstance(item, dict):
                        window = limit_window(dict(used_percent=item.get('usedPercent'),
                                                   window_minutes=item.get('windowDurationMins'),
                                                   resets_at=item.get('resetsAt')))
                        if window:
                            windows.append(window)
                if not windows:
                    raise UnsupportedSource()
                credits = limits.get('credits')
                try:
                    balance = float(credits.get('balance')) if isinstance(credits, dict) else None
                except (TypeError, ValueError):
                    balance = None
                return clock(), windows, balance if balance is not None and math.isfinite(balance) else None
        raise UnavailableSource()
    except (OSError, ValueError):
        raise UnavailableSource() from None
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
