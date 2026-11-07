"""Quota, forecast, credit and burn calculations on normalized readings."""
from collections.abc import Mapping, Sequence
from murmuratio_app.schema import CodexBurnReading, CreditReading, LimitWindow, QuotaHistory

def weekly_pace(tool: str, window: LimitWindow | None, history: Sequence[QuotaHistory], now: float):
    """Percentage points per day, within the latest reset segment and three days."""
    if not window:
        return dict(used_percent=None, remaining=None, resets_at=None, days_left=None,
                    allowance_per_day=None, burn_per_day=None, headroom=None)
    used, reset = window['used_percent'], window.get('resets_at')
    points = sorted((r for r in history if r['tool'] == tool and r['window'] == 'week'
                     and r['at'] <= now), key=lambda r: r['at'])
    points.append(dict(at=now, used_percent=used, resets_at=reset))
    segment = []
    for point in points:
        if segment and (point['used_percent'] < segment[-1]['used_percent'] or
                        (point.get('resets_at') is not None and segment[-1].get('resets_at') is not None
                         and abs(point['resets_at'] - segment[-1]['resets_at']) > 3600)):  # Claude's reset time jitters by milliseconds
            segment = []
        segment.append(point)
    cutoff = now - 3 * 86400
    first = segment[0]
    for point in segment[1:]:
        if point['at'] <= cutoff:
            first = point
            continue
        if first['at'] < cutoff:
            fraction = (cutoff - first['at']) / (point['at'] - first['at'])
            first = dict(at=cutoff, used_percent=first['used_percent'] +
                         fraction * (point['used_percent'] - first['used_percent']))
        break
    elapsed = (now - first['at']) / 86400
    burn = max(0, (used - first['used_percent']) / elapsed) if elapsed > 0 else 0
    days = max(0, (reset - now) / 86400) if reset is not None else None
    remaining = 100 - used
    allowance = remaining / days if days is not None and days > 0 else None
    return dict(used_percent=used, remaining=remaining, resets_at=reset, days_left=days,
                allowance_per_day=allowance, burn_per_day=burn,
                headroom=allowance / max(burn, 1) if allowance is not None else None)


def quota_routing(limits: Mapping[str, Sequence[LimitWindow]], history: Sequence[QuotaHistory],
                  now: float, credits=None, work_day=12):
    stats = {tool: weekly_pace(tool, next((w for w in limits.get(tool, [])
                                         if w['window_minutes'] == 10080), None), history, now)
             for tool in ('claude', 'codex')}
    # Codex credits are spent after its limit hits 100%, so they add to its remaining week.
    # Hours of credit use (balance / credits per hour) times % per work hour (12 work hours a day).
    x = stats['codex']
    if (credits and credits.get('balance') and credits.get('per_hour') and x['burn_per_day']
            and x['days_left'] and x['remaining'] is not None):
        extra = credits['balance'] / credits['per_hour'] * x['burn_per_day'] / work_day
        allowance = (x['remaining'] + extra) / x['days_left']
        x.update(credits_percent=extra, allowance_per_day=allowance, headroom=allowance / max(x['burn_per_day'], 1))
    c, x = stats['claude']['headroom'], stats['codex']['headroom']
    leader, code, design = None, 'codex', 'claude'
    tiers = dict(codex=dict(simple='luna', hard='sol'), claude=dict(simple='Sonnet', hard='Opus'))
    if c is None and x is None:
        reason = 'Weekly quota data is unavailable, so default routing is retained.'
    elif c is None or x is None:
        leader = 'codex' if c is None else 'claude'
        code = design = leader
        reason = (f'Only {leader.title()} has valid weekly quota data, so it leads available work' +
                  (' with design limited to ready specifications.' if leader == 'codex' else '.'))
    elif c >= 1.2 and x >= 1.2:
        leader = 'default'
        reason = 'Both tools have sufficient headroom, so Codex leads code and Claude leads design.'
    else:
        leader = 'codex' if x >= c else 'claude'
        reason = f'{leader.title()} leads because it has more weekly headroom.'
        if x < 1 and c >= 1.2:
            code = 'claude'
            reason = 'Claude leads code with Sonnet for simple work and Opus for hard work because Codex is over pace.'
        elif c < 1 and x >= 1.2:
            design = 'codex'
            reason = 'Codex leads code and design from ready specifications because Claude is over pace.'
        elif c < 1 and x < 1:
            tiers = dict(codex=dict(simple='luna', hard='luna'), claude=dict(simple='Sonnet', hard='Sonnet'))
            reason = f'{leader.title()} has more headroom, but both tools need cheaper tiers and non-urgent work deferred.'
    return dict(at=now, leader=leader, code_to=code, design_to=design, tiers=tiers,
                **stats, reason=reason)


def week_forecast(tool: str, window: LimitWindow | None, history: Sequence[QuotaHistory], now: float):
    """This week's usage line and a straight forecast at the week's average pace."""
    if not window or window.get('resets_at') is None:
        return None
    reset, used = window['resets_at'], window['used_percent']
    start = reset - 7 * 86400
    points = [[row['at'], row['used_percent']] for row in history
              if row['tool'] == tool and row['window'] == 'week' and start <= row['at'] < now
              and row.get('resets_at') is not None and abs(row['resets_at'] - reset) < 3600]
    points.append([now, used])
    elapsed = (now - start) / 86400
    pace = used / elapsed if elapsed >= 0.25 else None
    days_left = max(0, (reset - now) / 86400)
    projected = used + pace * days_left if pace is not None else None
    hits = now + (100 - used) / pace * 86400 if pace and projected is not None and projected > 100 else None
    return dict(start=start, resets_at=reset, used_percent=used, points=points, pace_per_day=pace,
                projected_at_reset=projected, hits_100_at=hits)


def codex_burn(readings: Sequence[CodexBurnReading], start: float, reset: float):
    """Weekly % gained per hour and model. Parallel chats report stale readings, so only new highs count."""
    hours, high = {}, None
    for at, used, resets_at, model in sorted(readings):
        if at < start or resets_at is None or abs(resets_at - reset) > 3600:
            continue
        if high is not None and used > high:
            hour = int(at // 3600) * 3600
            hours.setdefault(hour, {})
            hours[hour][model] = hours[hour].get(model, 0) + used - high
        high = used if high is None else max(high, used)
    return hours


def credit_spend(points: Sequence[CreditReading]):
    """Credits per hour while they were being spent.

    Parallel rollouts report slightly stale balances, so only new lows count. A drop counts only when the
    previous low was seen within 30 minutes; longer gaps have no known duration and are left out.
    Drops less than 30 minutes apart form one stretch; a stretch counts at least 5 minutes."""
    spent, spans, low, low_at = 0.0, [], None, None
    for at, balance in points:
        if low is None or balance > low:
            low, low_at = (balance, at) if low is None else (low, low_at)
            continue
        if balance == low:
            low_at = at
            continue
        if at - low_at <= 1800:
            spent += low - balance
            if spans and low_at - spans[-1][1] <= 1800:
                spans[-1][1] = at
            else:
                spans.append([low_at, at])
        low, low_at = balance, at
    seconds = float(sum(max(300, b - a) for a, b in spans))
    return (spent / (seconds / 3600) if seconds else None), spent, seconds
