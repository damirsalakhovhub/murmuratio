"""Cache Claude's reported quota and pass the original input to the user's status line."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def cache_limits(raw):
    try:
        data = json.loads(raw)
        limits = data.get('rate_limits') if isinstance(data, dict) else None
        if not isinstance(limits, dict):
            return
        selected = {}
        for key in ('five_hour', 'seven_day'):
            window = limits.get(key)
            if not isinstance(window, dict):
                continue
            used = window.get('used_percentage')
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
                continue
            selected[key] = {'used_percentage': used}
            reset = window.get('resets_at')
            if not isinstance(reset, bool) and isinstance(reset, (int, float)) and math.isfinite(reset):
                selected[key]['resets_at'] = reset
        if not selected:
            return
        directory = Path.home() / '.local/share/murmuratio'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as stream:
                name = stream.name
                json.dump({'at': time.time(), 'rate_limits': selected}, stream, allow_nan=False)
            os.replace(name, directory / 'claude-limits.json')
            name = None
        finally:
            if name:
                os.unlink(name)
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError):
        # Caching must never interrupt the original status line.
        pass


def main():
    raw = sys.stdin.buffer.read()
    cache_limits(raw)
    if len(sys.argv) > 1 and sys.argv[1]:
        return subprocess.run(sys.argv[1], shell=True, input=raw).returncode
    print('Claude')
    return 0
