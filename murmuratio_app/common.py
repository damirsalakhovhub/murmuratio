"""Shared local time, text and process helpers."""
import argparse
from datetime import datetime, timedelta
import math
import os


class PlainArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Error: ' + ' '.join(message.split()) + '\n')


def timestamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                return None
            result = float(value)
            datetime.fromtimestamp(result)
            return result
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def one_line(value):
    return ' '.join(value.split()) if isinstance(value, str) else ''


def active_today(at, now):
    try:
        return at is not None and datetime.fromtimestamp(at).date() == datetime.fromtimestamp(now).date()
    except (ValueError, TypeError, OverflowError, OSError):
        return False


def local_day(now):
    today = datetime.fromtimestamp(now).date()
    return {'start': datetime.combine(today, datetime.min.time()).timestamp(),
            'end': datetime.combine(today + timedelta(days=1), datetime.min.time()).timestamp()}


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
