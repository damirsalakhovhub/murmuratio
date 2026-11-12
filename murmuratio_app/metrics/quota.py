"""Quota, forecast, credit and burn calculations on normalized readings."""
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from math import ceil
from murmuratio_app.config import RoutingPolicy
from murmuratio_app.schema import CodexBurnReading, CreditReading, LimitWindow, QuotaHistory

# Local workload conversion used by the quota forecast and credit routing.
DEFAULT_CREDITS_PER_PERCENT = 250.0
FAST_PERCENT_FACTOR = 2.5


class WorkClock:
    """Counts the configured work hours between two times (every day, local time)."""

    def __init__(self, start=9, end=21):
        self.start, self.end = start, end
        self.per_day = end - start

    def _spans(self, a, b):
        """Work spans of each local day from a to b, in order."""
        day = datetime.fromtimestamp(a).date()
        while True:
            base = datetime.combine(day, datetime.min.time())
            ws = (base + timedelta(hours=self.start)).timestamp()
            we = (base + timedelta(hours=self.end)).timestamp()
            if ws >= b:
                return
            lo, hi = max(a, ws), min(b, we)
            if hi > lo:
                yield lo, hi
            day += timedelta(days=1)

    def hours(self, a, b):
        if b <= a:
            return 0.0
        return sum(hi - lo for lo, hi in self._spans(a, b)) / 3600

    def forward(self, a, hours, limit):
        """The earliest time t <= limit with hours(a, t) >= hours, else None."""
        need = hours * 3600
        if need <= 0:
            return a
        for lo, hi in self._spans(a, limit):
            if hi - lo >= need:
                return lo + need
            need -= hi - lo
        return None

    def back(self, end, hours, floor):
        """The latest time t >= floor with hours(t, end) >= hours, else None."""
        need = hours * 3600
        if need <= 0:
            return end
        spans = list(self._spans(floor, end))
        for lo, hi in reversed(spans):
            if hi - lo >= need:
                return hi - need
            need -= hi - lo
        return None


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


def balance_since(points: Sequence[CreditReading], at: float):
    """First balance observed in a window; a debit across its boundary is unassignable."""
    return next((balance for when, balance in sorted(points) if when >= at), None)


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


def codex_credit_pool(codex, credits, now, expire_at, work_day, policy=None):
    """Add Codex credit fields to its weekly stats; return this week's share left in weekly %.

    Reserve credits on the configured calendar, then spread the balance over weeks until
    expiry. Convert credit hours to weekly percentage using measured or typical rates."""
    policy = policy or RoutingPolicy()
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
    reserved_until = policy.reserve_until(now)
    deferred = reserved_until is not None
    budget = 0 if deferred else balance
    pool = codex['credits'] = dict(
        balance=balance, expires_at=expire_at, per_hour=per_hour, per_hour_measured=bool(measured),
        days_left=(expire_at - now) / 86400, burn_per_day=credits.get('spent_per_day') or 0,
        month_ends_at=expire_at, month_left=budget, month_pool=budget)
    if deferred:
        pool['reserved_until'] = reserved_until
    reset = codex['resets_at']
    if reset is None or reset <= now:
        return 0
    # This week counts whole; the weeks after it count by their part before expiry.
    # Credits spent since this week began count against its share.
    weeks = 1 + max(0, expire_at - reset) / (7 * 86400)
    spent = max(0, (credits.get('week_start_balance') or balance) - balance)
    share = 0 if deferred else (balance + min(spent, balance)) / weeks
    share_left = max(0, min(budget, share - spent))
    share_percent = share_left * percent_per_credit if percent_per_credit is not None else None
    pool.update(weeks_left_in_month=weeks, week_share=share, week_share_left=share_left,
                week_share_percent=share_percent)
    pool['credits_plan'] = credits_plan(codex['used_percent'], reset, share, spent, now, policy.credit_days_before_reset)
    if share_percent:
        allowance = (codex['remaining'] + share_percent) / codex['days_left']
        codex.update(allowance_per_day=allowance, headroom=allowance / max(codex['burn_per_day'], 1))
    return share_percent or 0


PLAN_BAND = 0.1


def credits_plan(used_percent, reset, target, spent, now, credit_days=2):
    """Compare credit spend with its prorated weekly budget, independently of quota use.

    Allow one hour of the week's budget (at least one credit) near zero, and 10% thereafter.
    credit_days_start remains the suggested quota-to-credits transition, for compatibility;
    it no longer makes a small early credit debit an automatic overspend."""
    week_start, credit_start = reset - 7 * 86400, reset - credit_days * 86400
    expected = target * min(1, max(0, (now - week_start) / (7 * 86400)))
    tolerance = max(1.0, target / (7 * 24), expected * PLAN_BAND)
    status = 'under' if spent < expected - tolerance else 'over' if spent > expected + tolerance else 'on'
    return dict(target_per_week=target, spent_this_week=spent, expected_by_now=expected,
                credit_days_start=credit_start, status=status)


def _hit(start, used, pace, work, limit, target=99):
    """When use started at `used` reaches the target at the forecast pace, before `limit`."""
    need = (target - used) / pace if pace > 0 else None
    if used >= target:
        return start
    if need is None:
        return None
    hit = work.forward(start, need * work.per_day, limit) if work else start + need * 86400
    return hit if hit is not None and hit < limit else None


def _opportunities(cursor, deadlines, pace, work, target=99):
    """Projected presses, one per deadline in order, each in a fresh window from `cursor`."""
    hits = []
    for deadline in deadlines:
        hit = _hit(cursor, 0, pace, work, min(deadline, cursor + 7 * 86400), target)
        if hit is None:
            break
        hits.append(hit)
        cursor = hit  # pressing here restarts seven days, rather than keeping Monday
    return hits


def _press_plan(used, now, reset, deadlines, pace, work, reserved, surplus=False):
    """Plan at the selected mode's pace; every press starts a fresh seven days.

    Surplus aims for 100% with three days left. If that is unreachable, the reservation's
    last-chance exception still applies, even below 99% on the final usable day.
    None marks a reset that cannot be used after the preceding planned press."""
    plan, left, start, end = [], list(deadlines), now, reset
    fallback = None
    while left:
        last = None
        if reserved and surplus:
            last = work.back(left[0], 1 / 3600, start) if work else left[0] - 1
        hit = _hit(start, used, pace, work, min(end, left[0]),
                   target=100 if surplus and used < 99 else 99)
        if hit is not None and (end - hit >= 3 * 86400 or
                                (reserved and len(_opportunities(end, left, pace, work,
                                                                100 if surplus else 99)) < len(left))):
            plan.append(hit)
            if end - hit < 3 * 86400:
                fallback = hit
            left.pop(0)
            start, used, end = hit, 0, hit + 7 * 86400
        elif end >= left[0] or (reserved and surplus and (last is None or last < end)):
            # Use the final available instant, never the reservation end or two resets at once.
            if last is not None and last > start:
                plan.append(last)
                fallback = last
                start, used, end = last, 0, last + 7 * 86400
            else:
                plan.append(None)
            left.pop(0)
        else:
            start, used, end = end, 0, end + 7 * 86400
    return plan, fallback


def calculator_timeline(reset, deadlines, plan, now, used_reset=False):
    """Seven-day windows from this week's start to the last reset deadline, for the calculator.

    Windows end at a natural reset, at a planned press, or at the timeline's end."""
    if reset is None:
        return None
    start = reset - 7 * 86400
    end = max([reset, *deadlines])
    resets = ([dict(at=start, status='used')] if used_reset else []) + [
        dict(at=at, status='planned', expires_at=deadline) if at is not None
        else dict(at=deadline, status='unplanned', expires_at=deadline)
        for at, deadline in zip(plan, deadlines)]
    presses = sorted(at for at in plan if at is not None)
    windows, cursor, cursor_end = [], start, reset
    while cursor < end:
        press = next((at for at in presses if cursor < at < cursor_end), None)
        stop = min(press or cursor_end, end)
        ends = 'manual' if press is not None and stop == press else 'natural' if stop == cursor_end else 'end'
        windows.append(dict(start=cursor, end=stop, ends=ends, current=cursor <= now < stop))
        cursor, cursor_end = stop, stop + 7 * 86400
    return dict(start=start, end=end, expires_at=max(deadlines) if deadlines else None,
                windows=windows, resets=resets)


def manual_reset_started(history, reset, tool='codex'):
    """Whether the window ending at `reset` began with a manual press: a reading from before
    its start promised a reset later than that start, in another window (the provider's
    deadline drifts by up to an hour within one window)."""
    start = reset - 7 * 86400
    return any(row['tool'] == tool and row['window'] == 'week' and row['at'] < start - 3600
               and row.get('resets_at') is not None and start + 3600 < row['resets_at'] < reset - 2 * 3600
               for row in history)


def codex_mode(forecast, resets, now, credit_share=0, work_hours=None, policy=None):
    """One calculator/routing decision using the configured credit and reset calendar.

    Each manual reset restarts the seven-day window for subsequent opportunities.
    """
    policy = policy or RoutingPolicy()
    result = dict(id='saving', title='Saving', reason='Weekly limit unavailable.',
                  boost={}, fast=False, effort=None, reset_share=0, headroom=None,
                  demand=None, available=None, reset_reminder=None, reset_plan=[], reset_deadlines=[])
    if not forecast:
        return result
    used, reset = forecast['used_percent'], forecast['resets_at']
    days = max(0, (reset - now) / 86400)
    reserve_end = policy.reserve_until(now)
    reserved = reserve_end is not None
    reserve_label = datetime.fromtimestamp(now).strftime('%B')
    spend_label = datetime.fromtimestamp(reserve_end).strftime('%B') if reserved else ''
    usable = sorted(at for at in resets if at > now)
    deadlines = [min(at, reserve_end) for at in usable] if reserved else usable
    pace = forecast['pace_per_day']
    work = WorkClock(*work_hours) if work_hours is not None else None
    # Could all remaining resets still be used after waiting for the natural reset?
    opportunities = len(_opportunities(reset, deadlines, pace, work))
    last_chance = reserved and opportunities < len(usable)
    reset_share = sum(100 / max(1, (deadline - now) / (7 * 86400)) for deadline in deadlines)
    available = 100 - used + reset_share + (0 if reserved else credit_share)
    demand = max(0, forecast['projected_at_reset'] - used)
    # A future reset share cannot fund work in an exhausted window today.
    if reserved and used >= 100:
        available = 0
    surplus = demand < available - 1e-7
    plan_pace = pace * FAST_PERCENT_FACTOR if surplus else pace
    press_now = bool(usable) and used >= 99 and (days >= 3 or last_chance)
    if press_now:
        plan, fallback = _press_plan(0, now, now + 7 * 86400, deadlines[1:], plan_pace, work, reserved, surplus)
        plan.insert(0, now)
    else:
        plan, fallback = _press_plan(used, now, reset, deadlines, plan_pace, work, reserved, surplus)
    result.update(reset_plan=plan,
                  reset_deadlines=deadlines)
    if press_now:
        reason = (f'Week at {used:g}%; this is the last chance to use a reset in {reserve_label}.' if last_chance and days < 3
                  else f'Week at {used:g}%, reset {datetime.fromtimestamp(reset):%A}, in {ceil(days)} days.')
        result.update(id='reset', title='Press a reset now', reason=reason,
                      reset_reminder=dict(expires_at=usable[0], available=len(usable)))
        return result
    end = datetime.fromtimestamp(reset)
    reason = f'Week at {used:g}%, pace reaches {forecast["projected_at_reset"]:.0f}% by {end:%a} {end.day}'
    if surplus and reserved and usable:
        reason += f', with {len(usable)} reset{"s" if len(usable) != 1 else ""} to use in {reserve_label}'
    elif not surplus and reserved:
        reason += f'; credits kept for {spend_label}'
    if surplus and usable:
        first = next((at for at in plan if at is not None), None)
        if fallback is not None:
            last = datetime.fromtimestamp(fallback)
            reason += (f'. Cannot reach 100% with three days left even at ~{plan_pace:.0f}%/day'
                       f'; last chance {last:%a} {last.day}')
        elif first is not None:
            press = datetime.fromtimestamp(first)
            reason += f'. To press a reset on {press:%a} {press.day}, burn ~{plan_pace:.0f}%/day'
        elif reserved:
            reason += (f'. Cannot reach 100% with three days left even at ~{plan_pace:.0f}%/day'
                       '; no work time remains before the last-chance deadline')
    result.update(id='surplus' if surplus else 'saving',
                  title=policy.surplus_title if surplus else 'Saving',
                  reason=reason + '.', boost=dict(policy.surplus_boost if surplus else policy.saving_boost),
                  fast=surplus, effort=policy.surplus_effort if surplus else None, reset_share=reset_share,
                  headroom=available / max(1e-9, demand), demand=demand, available=available)
    return result


def quota_routing(limits: Mapping[str, Sequence[LimitWindow]], history: Sequence[QuotaHistory],
                  now: float, credits=None, work_day=12, credits_expire_at=None, codex_resets=(),
                  pace_priors=None, credits_per_percent=None, work_hours=None, policy=None):
    """Prefer Codex for code and Claude for design, with Claude tiers pinned by task kind.

    Code moves to Claude only when Codex's forecast exceeds its quota, reset share and credits."""
    policy = policy or RoutingPolicy()
    stats = {tool: weekly_pace(tool, next((w for w in limits.get(tool, [])
                                         if w['window_minutes'] == 10080), None), history, now)
             for tool in ('claude', 'codex')}
    usable_resets = sorted(at for at in codex_resets if at > now)
    codex_credit_pool(stats['codex'], credits, now, credits_expire_at, work_day, policy)
    # A week that cannot cover one more work day (or the rest of the week, if less) is gone.
    claude = stats['claude']
    exhausted = (claude['remaining'] is not None and claude['days_left'] is not None and
                 claude['remaining'] < max(1, (claude['burn_per_day'] or 0) * min(1, claude['days_left'])))
    code, design = 'codex', 'codex' if exhausted else 'claude'
    tiers = dict(codex=dict(policy.codex_tiers), claude=dict(policy.claude_tiers))
    share_left = stats['codex'].get('credits', {}).get('week_share_left')
    # Boost/Fast spends the resources still available before reset. A credit-plan
    # comparison is a budget diagnostic, not evidence that the remaining week is short.
    week = next((w for w in limits.get('codex', []) if w['window_minutes'] == 10080), None)
    cpp = credits_per_percent or DEFAULT_CREDITS_PER_PERCENT
    credit_pace = (credits.get('spent_per_day') or 0) / cpp if credits else None
    forecast = week_forecast('codex', week, history, now, (pace_priors or {}).get('codex'), work_hours,
                             credit_pace, cpp)
    mode = codex_mode(forecast, usable_resets, now, (share_left or 0) / cpp, work_hours, policy)
    if mode['available'] is not None:
        stats['codex'].update(headroom=mode['headroom'], allowance_per_day=mode['available'] / stats['codex']['days_left'])
        if mode['available'] <= 0 or mode['demand'] > mode['available']:
            code = 'claude'
    if forecast and forecast['used_percent'] >= 100 and policy.reserve_until(now) is not None:
        code = 'claude'  # wait for the provider to confirm a reset before spending again
        design = 'claude'
    leader = code if code == design else 'default'
    code_reason = ("Claude takes code because Codex's projected demand exceeds its weekly quota, manual reset share and this week's credit share"
                   if code == 'claude' else 'Codex keeps code')
    if exhausted and design == 'codex':
        reason = code_reason + ', while Codex takes design because Claude is nearly exhausted.'
    elif exhausted:
        reason = code_reason + ', while Claude keeps design because Codex has no included allowance left.'
    else:
        reason = (code_reason + f', while Claude keeps design with {tiers["claude"]["simple"]} for simple work and {tiers["claude"]["hard"]} for hard work.'
                  if policy.claude_tiers != RoutingPolicy().claude_tiers else code_reason + ', while Claude keeps design.')
    return dict(at=now, leader=leader, code_to=code, design_to=design, tiers=tiers,
                **stats, reason=reason, codex_mode=mode, codex_boost=mode['boost'], codex_fast=mode['fast'],
                codex_effort=mode['effort'], codex_reset_reminder=mode['reset_reminder'])


DEFAULT_PACE = 100 / 7  # Even use of a weekly allowance, until local estimates exist.
PACE_PRIOR_DAYS = 7
PACE_BLEND_DAYS = 2


def _prior_pace(tool, reset, history, expected, work):
    """Refine an expected daily use with completed weeks, weighted by their observed length."""
    groups = []
    for row in sorted(history, key=lambda r: r['at']):
        end = row.get('resets_at')
        if (row['tool'] != tool or row['window'] != 'week' or end is None
                or end > reset - 7 * 86400 + 3600 or not end - 7 * 86400 <= row['at'] < end):
            continue
        group = next((g for g in groups if abs(g['reset'] - end) < 3600), None)
        if group is None:
            group = dict(reset=end, at=row['at'], used=0)
            groups.append(group)
        group.update(at=row['at'], used=max(group['used'], row['used_percent']))
    prior = expected if expected is not None else DEFAULT_PACE
    total, days = prior * PACE_PRIOR_DAYS, float(PACE_PRIOR_DAYS)
    for group in groups:
        start = group['reset'] - 7 * 86400
        elapsed = work.hours(start, group['at']) / work.per_day if work else (group['at'] - start) / 86400
        if elapsed >= 1:
            total += group['used']
            days += elapsed
    source = 'estimate' if expected is not None else 'default'
    return total / days, ('history and ' + source) if days > PACE_PRIOR_DAYS else source


def week_forecast(tool: str, window: LimitWindow | None, history: Sequence[QuotaHistory], now: float,
                  expected_per_day=None, work_hours=None, credit_burn_per_day=None, credits_per_percent=None):
    """Project immediately from a prior, blending into live use over two work days.

    With configured work hours the API and browser share the same work-hour rate.
    Without them, daily rates and crossings use elapsed calendar time."""
    if not window or window.get('resets_at') is None or window['resets_at'] <= now:
        return None
    reset, used = window['resets_at'], window['used_percent']
    start = reset - 7 * 86400
    points = [[row['at'], row['used_percent']] for row in history
              if row['tool'] == tool and row['window'] == 'week' and start <= row['at'] < now
              and row.get('resets_at') is not None and abs(row['resets_at'] - reset) < 3600]
    points.append([now, used])
    work = WorkClock(*work_hours) if work_hours is not None else None
    prior, source = _prior_pace(tool, reset, history, expected_per_day, work)
    elapsed = work.hours(start, now) / work.per_day if work else max(0, (now - start) / 86400)
    weight = min(1.0, elapsed / PACE_BLEND_DAYS)
    pace = prior * (1 - weight) + used / max(PACE_BLEND_DAYS, elapsed)
    if used >= 100 and credit_burn_per_day is not None and credit_burn_per_day > pace:
        pace, source = credit_burn_per_day, 'live credits'
    days_left = work.hours(now, reset) / work.per_day if work else (reset - now) / 86400
    projected = used + pace * days_left
    hits = None
    if used >= 100:
        hits = now
    elif pace > 0 and projected > 100:
        hits = (work.forward(now, (100 - used) / pace * work.per_day, reset) if work
                else now + (100 - used) / pace * 86400)
    result = dict(start=start, resets_at=reset, used_percent=used, points=points, pace_per_day=pace,
                  projected_at_reset=projected, hits_100_at=hits, prior_per_day=prior, pace_source=source,
                  live_weight=weight, rate_per_hour=pace / (work.per_day if work else 24))
    if tool == 'codex':
        result['credits_per_percent'] = credits_per_percent or DEFAULT_CREDITS_PER_PERCENT
    return result


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
