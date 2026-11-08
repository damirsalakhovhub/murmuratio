"""Usage and model-burn calculations over normalized source measurements."""
from collections.abc import Sequence
from datetime import datetime
from murmuratio_app.metrics.quota import balance_at, codex_burn, credit_spend, credit_spent_per_day
from murmuratio_app.schema import ClaudeUsageBuckets, CodexMeter, Usage

USAGE_WEIGHTS = dict(input_tokens=1, cache_creation_input_tokens=1.25, cache_read_input_tokens=0.1, output_tokens=5)


def add_usage(data, usage: Usage, include_models=True):
    ids = data.setdefault('ids', set())
    if usage['message_id'] in ids:
        return
    ids.add(usage['message_id'])
    at = usage['at']
    if at is None:
        return
    cost = sum(weight * usage[key] for key, weight in USAGE_WEIGHTS.items())
    hour = int(at // 3600) * 3600
    buckets = data.setdefault('hours', {})
    buckets[hour] = buckets.get(hour, 0) + cost
    if include_models:
        by_model = data.setdefault('models', {}).setdefault(hour, {})
        model = usage['model']
        by_model[model] = by_model.get(model, 0) + cost


def claude_usage_totals(readings: Sequence[ClaudeUsageBuckets], since: float, include_split: bool):
    totals, models = dict(worker=0.0, chat=0.0), {}
    for row in readings:
        for hour, by_model in row['models'].items():
            if hour >= since - 3600:
                slot = models.setdefault(hour, {})
                for model, cost in by_model.items():
                    slot[model] = slot.get(model, 0) + cost
        if include_split:
            totals[row['kind']] += sum(cost for hour, cost in row['hours'].items() if hour >= since - 3600)
    total = totals['worker'] + totals['chat']
    return (totals['worker'] / total if total else None), models


def credit_totals(readings: Sequence[CodexMeter], include_credits: bool, now: float | None = None):
    points, usage, latest = [], [], None
    for row in readings:
        points.extend(row['points'])
        usage.extend(row['usage'])
        candidate = row['latest']
        if candidate and (latest is None or candidate[0] > latest[0]):
            latest = candidate
    points.sort()
    per_hour, spent, seconds = credit_spend(points) if include_credits else (None, 0, 0)
    value = dict(balance=latest[1], at=latest[0], per_hour=per_hour, sample_credits=spent,
                 sample_hours=seconds / 3600) if latest else None
    if value and include_credits and now is not None:
        value['spent_per_day'] = credit_spent_per_day(points, now)
        month = datetime.fromtimestamp(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        value['month_start_balance'] = balance_at(points, month)
    return value, usage, points


def burn_by_model(forecast, claude_models, codex_readings):
    out = {}
    claude = forecast.get('claude')
    if claude and claude_models:
        total = sum(c for hour, by in claude_models.items() if hour >= claude['start'] - 3600 for c in by.values())
        scale = claude['used_percent'] / total if total else 0
        out['claude'] = dict(start=claude['start'], hours=sorted(
            [hour, {m: c * scale for m, c in by.items()}] for hour, by in claude_models.items() if hour >= claude['start'] - 3600))
    codex = forecast.get('codex')
    if codex:
        hours = codex_burn(codex_readings, codex['start'], codex['resets_at'])
        out['codex'] = dict(start=codex['start'], hours=sorted([hour, by] for hour, by in hours.items()))
    return out
