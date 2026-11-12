"""Two health scores for the board header: limits spending and agent assignments.

A score is 0..100. 100 means nothing for the owner to look at; the reason names the worst part."""
GOOD_LOW, GOOD_HIGH = 0.85, 1.05  # share of the week's capacity used by reset that counts as on plan
ASSIGN_WINDOW = 86400
NAMES = {'claude': 'Claude', 'codex': 'Codex'}


def _usage_score(ratio):
    if ratio is None:
        return None
    if ratio > GOOD_HIGH:
        return max(0, round(100 - (ratio - GOOD_HIGH) * 200))
    if ratio < GOOD_LOW:
        return max(0, round(100 - (GOOD_LOW - ratio) * 150))
    return 100


def limits_health(routing, forecast):
    """How the week's spending lands at reset: running out early and leaving quota unused both cost."""
    if not routing:
        return None
    parts = []
    for tool in ('claude', 'codex'):
        stats, week = routing.get(tool) or {}, (forecast or {}).get(tool) or {}
        if tool == 'codex' and (routing.get('codex_mode') or {}).get('available'):
            mode = routing['codex_mode']
            ratio = mode['demand'] / mode['available']  # counts manual resets and this week's credits
        elif week.get('projected_at_reset') is not None:
            ratio = week['projected_at_reset'] / 100
        else:
            ratio = None
        parts.append(dict(tool=tool, score=_usage_score(ratio), ratio=ratio, used_percent=stats.get('used_percent'),
                          resets_at=stats.get('resets_at'), hits_100_at=week.get('hits_100_at')))
    known = [p for p in parts if p['score'] is not None]
    if not known:
        return dict(score=None, reason='No limit data yet', parts=parts)
    worst = min(known, key=lambda p: p['score'])
    if worst['score'] == 100:
        reason = 'On plan'
    elif worst['ratio'] > GOOD_HIGH:
        reason = f"{NAMES[worst['tool']]} runs out before reset"
    else:
        reason = f"{NAMES[worst['tool']]} leaves {round((1 - worst['ratio']) * 100)}% unused at reset"
    return dict(score=worst['score'], reason=reason, parts=parts)


def _advice_at(history, at):
    advice = None
    for row in history:
        if row['at'] > at:
            break
        advice = row
    return advice


def assignment_health(agents, history, now):
    """Workers started in the last day against the routing advice at their start.

    A worker on a provider that took neither code nor design that moment is off advice.
    Workers started before the advice log began are not judged."""
    if not history:
        return dict(score=None, reason='No routing advice recorded yet', judged=0, wrong=[], since=None)
    since = history[0]['at']
    judged, wrong = 0, []
    for agent in agents:
        started = agent.get('started')
        if (agent.get('orchestrator') or agent.get('codex_role') == 'chat' or agent.get('kind') not in NAMES
                or not started or started < since or now - started > ASSIGN_WINDOW):
            continue
        advice = _advice_at(history, started)
        if advice is None:
            continue
        judged += 1
        if agent['kind'] not in (advice['code_to'], advice['design_to']):
            wrong.append(dict(id=agent['id'], task=agent.get('task') or agent.get('who') or agent['id'],
                              project=agent.get('project'), kind=agent['kind'], started=started,
                              advice=advice['code_to']))
    if not judged:
        return dict(score=100, reason='No workers started in the last day', judged=0, wrong=[], since=since)
    score = round(100 * (judged - len(wrong)) / judged)
    reason = f'{len(wrong)} of {judged} against advice' if wrong else f'All {judged} by advice'
    return dict(score=score, reason=reason, judged=judged, wrong=wrong, since=since)
