"""Shared local time, text and process helpers."""
import argparse
from datetime import datetime
import math
import os
from pathlib import Path


class PlainArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Error: ' + ' '.join(message.split()) + '\n')


def timestamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) if math.isfinite(value) else None
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def one_line(value):
    return ' '.join(value.split()) if isinstance(value, str) else ''


def active_today(at, now):
    return at is not None and datetime.fromtimestamp(at).date() == datetime.fromtimestamp(now).date()


def pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
