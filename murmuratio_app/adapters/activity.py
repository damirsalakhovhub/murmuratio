"""Incremental working time, excluding gaps between saved turns."""


def start_turn(data, at):
    if at is None:
        return
    # A new prompt also closes a turn whose completion was not saved.
    end_turn(data, data.get('_active_last_at'))
    data.setdefault('_active_seconds', 0)
    data['_active_started'] = data['_active_last_at'] = at


def record_activity(data, at):
    if at is not None and data.get('_active_started') is not None:
        data['_active_last_at'] = max(data['_active_last_at'], at)


def end_turn(data, at):
    started = data.pop('_active_started', None)
    if started is not None:
        stop = at if at is not None else data.get('_active_last_at', started)
        data['_active_seconds'] += max(0, stop - started)


def working_seconds(data, now, status, started, last_at):
    if '_active_seconds' not in data:
        # Without turn boundaries only the saved activity interval is known.
        return max(0, last_at - started) if started is not None and last_at is not None else None
    seconds = data['_active_seconds']
    current = data.get('_active_started')
    if current is not None:
        stop = now if status == 'working' else data.get('_active_last_at', current)
        seconds += max(0, stop - current)
    return seconds
