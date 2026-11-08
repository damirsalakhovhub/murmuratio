"""Quota, forecast, credit and burn calculations on normalized readings."""
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
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


# Typical credits per hour of Codex work, used until a spend is measured.
DEFAULT_CREDITS_PER_HOUR = 400


def credits_expiry(now: float, month: int, day: int):
    """End of the next local expiry day (the day itself still counts)."""
    today = datetime.fromtimestamp(now).date()
    for year in range(today.year, today.year + 5):
        try:
            end = datetime.combine(date(year, month, day) + timedelta(days=1), datetime.min.time()).timestamp()
        except ValueError:  # 02-29 outside a leap year
            continue
        if end > now:
            return end
    return None


def credit_month(now: float, expire_at: float):
    """The current credit month: (start, end, months from its start to the expiry, this one included).

    The balance is split evenly over the months left: each month's part expires at its end."""
    today = datetime.fromtimestamp(now)
    start = datetime(today.year, today.month, 1)
    end = datetime(today.year + today.month // 12, today.month % 12 + 1, 1)
    last = datetime.fromtimestamp(expire_at - 1)
    months = (last.year - start.year) * 12 + last.month - start.month + 1
    return start.timestamp(), min(end.timestamp(), expire_at), max(1, months)


def balance_at(points: Sequence[CreditReading], at: float):
    """Balance last seen before a time, else the first one seen after it."""
    points = sorted(points)
    before = [b for t, b in points if t < at]
    return before[-1] if before else (points[0][1] if points else None)


def credit_spent_per_day(points: Sequence[CreditReading], now: float, days: float = 3):
    """Average credits spent per day over the last days. Stale parallel balances count only new lows."""
    cutoff = now - days * 86400
    points = sorted(p for p in points if p[0] <= now)
    before = [b for at, b in points if at < cutoff]
    window = [(at, b) for at, b in points if at >= cutoff]
    if not window:
        return 0.0
    low, spent = (before[-1] if before else None), 0.0
    for _, balance in window:
        if low is not None and balance < low:
            spent += low - balance
        low = balance if low is None else min(low, balance)
    first = cutoff if before else window[0][0]
    return spent / max(1, min(days, (now - first) / 86400))


def codex_credit_pool(codex, credits, now, expire_at, work_day):
    """Add Codex credit fields to its weekly stats; return this week's share left in weekly %.

    Credits are spent only after the Codex week hits 100%. The balance is split evenly over the
    months to its expiry (each month's part is due by month end), and this month's part over the
    Codex weeks left in the month. Credits convert to weekly % as hours of credit use times % per
    work hour; until a spend is measured the typical rate is used, and an idle Codex counts at an
    even weekly pace."""
    if not credits or not credits.get('balance'):
        return 0
    balance, measured = credits['balance'], credits.get('per_hour')
    per_hour = measured or DEFAULT_CREDITS_PER_HOUR
    percent_per_credit = None
    if codex['days_left'] and codex['remaining'] is not None:
        percent_per_credit = (codex['burn_per_day'] or 100 / 7) / work_day / per_hour
        codex['credits_percent'] = balance * percent_per_credit
    if expire_at is None or expire_at <= now:
        return 0
    _, month_end, months = credit_month(now, expire_at)
    opening = credits.get('month_start_balance') or balance
    month_left = max(0, balance - opening * (months - 1) / months)
    pool = codex['credits'] = dict(
        balance=balance, expires_at=expire_at, per_hour=per_hour, per_hour_measured=bool(measured),
        days_left=(expire_at - now) / 86400, burn_per_day=credits.get('spent_per_day') or 0,
        month_ends_at=month_end, month_left=month_left, month_pool=opening / months)
    reset = codex['resets_at']
    if reset is None or reset <= now:
        return 0
    # This week counts whole; the weeks after it until month end count by their part in the month.
    # Credits spent since this week began count against its share.
    weeks = 1 + max(0, month_end - reset) / (7 * 86400)
    spent = max(0, (credits.get('week_start_balance') or balance) - balance)
    share = (month_left + min(spent, balance)) / weeks
    share_left = max(0, min(month_left, share - spent))
    share_percent = share_left * percent_per_credit if percent_per_credit else None
    pool.update(weeks_left_in_month=weeks, week_share=share, week_share_left=share_left,
                week_share_percent=share_percent)
    if share_percent:
        allowance = (codex['remaining'] + share_percent) / codex['days_left']
        codex.update(allowance_per_day=allowance, headroom=allowance / max(codex['burn_per_day'], 1))
    return share_percent or 0


def quota_routing(limits: Mapping[str, Sequence[LimitWindow]], history: Sequence[QuotaHistory],
                  now: float, credits=None, work_day=12, credits_expire_at=None):
    """Use it or lose it: steer work so each weekly quota reaches zero at its own reset.

    A week whose pace to zero (allowance_per_day) is clearly above its burn needs more work. Codex's
    week includes this week's credit share (see codex_credit_pool)."""
    stats = {tool: weekly_pace(tool, next((w for w in limits.get(tool, [])
                                         if w['window_minutes'] == 10080), None), history, now)
             for tool in ('claude', 'codex')}
    extra = dict(claude=0, codex=codex_credit_pool(stats['codex'], credits, now, credits_expire_at, work_day))
    # A week that cannot cover one more work day (or the rest of the week, if less) is gone.
    exhausted = {tool: s['remaining'] is not None and s['days_left'] is not None and
                 s['remaining'] + extra[tool] < max(1, (s['burn_per_day'] or 0) * min(1, s['days_left']))
                 for tool, s in stats.items()}
    c, x = ((0 if exhausted[t] else stats[t]['headroom']) if stats[t]['headroom'] is not None else None
            for t in ('claude', 'codex'))
    leader, code, design = None, 'codex', 'claude'
    tiers = dict(codex=dict(simple='luna', hard='sol'), claude=dict(simple='Sonnet', hard='Opus'))
    share_left = stats['codex'].get('credits', {}).get('week_share_left')
    share = f" Codex's week includes {share_left:.0f} credits after its limit hits 100%." if extra['codex'] else ''
    if c is None and x is None:
        reason = 'Weekly quota data is unavailable, so default routing is retained.'
    elif c is None or x is None:
        leader = 'codex' if c is None else 'claude'
        code = design = leader
        reason = (f'Only {leader.title()} has valid weekly quota data, so it leads available work' +
                  (' with design limited to ready specifications.' if leader == 'codex' else '.'))
    elif c >= 1.2 and x >= 1.2 and stats['claude']['resets_at'] < stats['codex']['resets_at']:
        # Both under pace: code goes to the quota lost sooner, or Claude's would burn unused.
        leader, code = 'claude', 'claude'
        reason = ('Both tools have sufficient headroom; Claude leads code and design until its weekly reset, '
                  'which comes first, so its quota is not lost.' + share)
    elif c >= 1.2 and x >= 1.2:
        leader = 'default'
        reason = 'Both tools have sufficient headroom, so Codex leads code and Claude leads design.' + share
    elif c >= 1.2:
        leader, code = 'claude', 'claude'
        reason = ('Claude leads code and design with Sonnet for simple work and Opus for hard work: its weekly '
                  'quota is under pace and is lost at reset, while Codex ' +
                  ('has almost no quota or credit share left this week.' if exhausted['codex'] else 'is over pace.'))
    elif x >= 1.2:
        leader, design = 'codex', 'codex'
        reason = ('Codex leads code and design from ready specifications: its weekly quota is under pace '
                  'and is lost at reset, while Claude ' +
                  ('has almost no included quota left.' if exhausted['claude'] else 'is over pace.') + share)
    else:
        leader = 'codex' if x >= c else 'claude'
        reason = f'{leader.title()} leads because it has more headroom.'
        if c < 1 and x < 1:
            tiers = dict(codex=dict(simple='luna', hard='luna'), claude=dict(simple='Sonnet', hard='Sonnet'))
            reason = f'{leader.title()} has more headroom, but both tools need cheaper tiers and non-urgent work deferred.'
        elif x < 1:
            code = 'claude'
            reason = 'Claude leads code because Codex is over pace.'
        elif c < 1:
            design = 'codex'
            reason = 'Codex leads code and design from ready specifications because Claude is over pace.'
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
