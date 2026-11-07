"""Normalize quota windows from local source formats."""
import math
from murmuratio_app.common import timestamp
from murmuratio_app.schema import LimitWindow

def limit_window(window, minutes=None, percent_key='used_percent') -> LimitWindow | None:
    """Normalize the two local quota formats, rejecting malformed measurements."""
    if not isinstance(window, dict):
        return None
    used, minutes = window.get(percent_key), minutes or window.get('window_minutes')
    if (isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100
            or isinstance(minutes, bool) or not isinstance(minutes, (int, float))
            or not math.isfinite(minutes) or minutes <= 0):
        return None
    return dict(window_minutes=minutes, used_percent=used, resets_at=timestamp(window.get('resets_at')))


def rollout_limits(record) -> list[LimitWindow]:
    """Find quota windows in a rollout event's nested payload."""
    stack = [record]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            limits = item.get('rate_limits')
            if isinstance(limits, dict):
                windows = [limit_window(limits.get(key)) for key in ('primary', 'secondary')]
                if any(windows):
                    return [window for window in windows if window]
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return []
