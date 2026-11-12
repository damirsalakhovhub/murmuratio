"""Compare a running local board with this checkout without writing snapshots."""
import http.client
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from murmuratio_app.common import active_today
from murmuratio_app.config import Config
from murmuratio_app.discovery import Discovery
from murmuratio_app.grouping import board_orchestrator, board_worker
from murmuratio_app.now import NowSource
from murmuratio_app.server import sanitize


BOARD_SOURCES = ('claude_desktop', 'claude_sessions', 'codex_sessions', 'codex_wrapper', 'agents_registry')
MAX_RESPONSE = 32 * 1024 * 1024


class CheckError(ValueError):
    pass


def read_board(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'http' or not (host in ('localhost', '127.0.0.1') or host.endswith('.localhost'))
            or parsed.username is not None or parsed.password is not None or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment):
        raise CheckError('Board URL must be a local HTTP origin')
    try:
        port = parsed.port or 80
        # Never follow a redirect or resolve an arbitrary hostname. The original Host
        # header still lets a local reverse proxy select the named board.
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        connection.request('GET', '/api/now', headers={'Host': parsed.netloc})
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE + 1)
        if response.status != 200 or len(body) > MAX_RESPONSE:
            raise CheckError('Running board did not return a usable /api/now response')
        return json.loads(body)
    except (OSError, http.client.HTTPException, UnicodeError, json.JSONDecodeError) as exc:
        raise CheckError('Could not read the running board') from exc
    finally:
        if 'connection' in locals():
            connection.close()


def archived_from_disk(home):
    """Verify archive flags independently of the candidate Desktop adapter."""
    root = Path(home) / 'Library/Application Support/Claude/claude-code-sessions'
    records = {}
    try:
        if not root.exists():
            return set()
        with os.scandir(root) as accounts:
            for account in accounts:
                if account.name.startswith('.') or not account.is_dir():
                    continue
                with os.scandir(account.path) as projects:
                    for project in projects:
                        if project.name.startswith('.') or not project.is_dir():
                            continue
                        with os.scandir(project.path) as files:
                            for entry in files:
                                if not entry.name.startswith('local_') or not entry.name.endswith('.json'):
                                    continue
                                with open(entry.path, encoding='utf-8') as stream:
                                    data = json.load(stream)
                                if not isinstance(data, dict) or not isinstance(data.get('cliSessionId'), str):
                                    raise CheckError('Cannot verify Claude Desktop archive metadata')
                                records.setdefault(data['cliSessionId'], []).append(data.get('isArchived') is True)
    except (OSError, ValueError, UnicodeError) as exc:
        raise CheckError('Cannot verify Claude Desktop archive metadata') from exc
    # Conflicting records do not establish whether a session is archived.
    return {'claude:' + key for key, values in records.items() if all(values)}


def candidate(home, config_path, codex_home, at):
    config = Config.load(home, config_path)
    # These modules are unrelated to card discovery and can write quota history,
    # routing files or start background samplers. Keep wrapper/chat discovery on.
    for name in ('forecast', 'routing', 'burn_rate', 'codex_credits'):
        config.modules[name] = False
    config.codex_live_limits = False
    codex_path = Discovery.at(home, config, codex_home).codex_home
    source = NowSource(home=home, config=config, codex_home=codex_path,
                       sanitize=sanitize, clock=lambda: at)
    archived_before = archived_from_disk(home)
    state = source.state()
    # A claude-task run file names the Claude session it started; that session's card replaces the run's.
    wrapper_threads, claude_tasks = {}, {}
    if config.modules['codex_wrapper']:
        cards = {card['id']: card for card in state['agents'] + state['completed'] if card.get('kind') == 'codex'}
        try:
            paths = list(source.wrapper_adapter.root.glob('*.json'))
        except OSError:
            paths = []
        for path in paths:
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(record, dict) or record.get('status') not in ('running', 'done', 'blocked'):
                continue
            card_id = 'codex:' + path.stem
            card = cards.get(card_id)
            if card:
                thread = source.wrapper_adapter._thread_of(path)
                if thread and thread == card.get('thread'):
                    wrapper_threads[card_id] = thread
            session = source.wrapper_adapter.claude_session(record)
            if session:
                claude_tasks[card_id] = 'claude:' + session
    archived = archived_before & archived_from_disk(home)
    return state, archived, wrapper_threads, claude_tasks


def board_view(state, at=None, allow_unhealthy=False):
    """Project the real API response to the cards retained by the board UI."""
    if not isinstance(state, dict) or not isinstance(state.get('now'), (int, float)) or not math.isfinite(state['now']):
        raise CheckError('Board response has no valid clock')
    if not isinstance(state.get('agents'), list) or not isinstance(state.get('completed'), list):
        raise CheckError('Board response has no valid card lists')
    issues = state.get('sources', {})
    if not isinstance(issues, dict):
        raise CheckError('Board response has invalid source health')
    health = {}
    for name in BOARD_SOURCES:
        issue = issues.get(name, {})
        if not isinstance(issue, dict):
            raise CheckError('Board response has invalid source health')
        status = issue.get('status', 'available')
        if status not in ('available', 'absent') and not (
                allow_unhealthy and status in ('unavailable', 'unsupported')):
            raise CheckError(f'Board source {name} is unavailable or unsupported')
        reason = issue.get('reason') if status in ('unavailable', 'unsupported') else None
        if reason is not None and not isinstance(reason, str):
            raise CheckError('Board response has invalid source health')
        health[name] = (status, reason)
    at = state['now'] if at is None else at
    by_id = {}
    for card in state['agents'] + state['completed']:
        if (not isinstance(card, dict) or not all(isinstance(card.get(k), str) and card[k]
                                                  for k in ('id', 'status'))
                or not isinstance(card.get('project'), str)):
            raise CheckError('Board response has an invalid card')
        if card['id'] in by_id:
            raise CheckError('Board response has duplicate card IDs')
        by_id[card['id']] = card
    def finished(card):
        needs_you = not card.get('completed') and card['status'] == 'blocked' and (
            card.get('orchestrator') or card.get('current_action') == 'waiting for approval')
        return (card.get('completed') or card['status'] in ('done', 'crashed') or
                (not card.get('orchestrator') and card['status'] == 'blocked' and not needs_you))
    keep = set()
    for card in by_id.values():
        ended = card.get('ended') if card.get('ended') is not None else card.get('last_at')
        ended = ended if isinstance(ended, (int, float)) and math.isfinite(ended) else 0
        if card.get('has_eligible_children') is True or not finished(card) or at - ended < 1800:
            keep.add(card['id'])
    pending = list(keep)
    while pending:
        parent = by_id[pending.pop()].get('parent_id')
        if isinstance(parent, str) and parent in by_id and parent not in keep:
            keep.add(parent)
            pending.append(parent)
    aliases = {alias: card['id'] for card in by_id.values()
               for alias in (card['id'], card['id'].split(':', 1)[-1], card.get('app_session'), card.get('thread')) if alias}
    eligible_parents = {parent for card in by_id.values() if (
        not (card.get('completed') or card['status'] in ('done', 'crashed'))
        or active_today(card.get('ended') or card.get('last_at'), at))
        if (parent := (card.get('parent_id') or aliases.get(card.get('parent_session'))
                       or aliases.get(card.get('parent_app_session')))) and parent != card['id']}
    # Reduce the response immediately. The snapshot cannot retain task text or paths.
    fields = ('id', 'project', 'status', 'orchestrator', 'completed', 'parent_id',
              'kind', 'codex_role', 'thread', 'ended', 'last_at', 'turn_started', 'registered',
              'parent_session', 'parent_app_session')
    return {key: {**{field: by_id[key].get(field) for field in fields},
                  'orchestrator_exception': board_orchestrator(by_id[key]),
                  'has_eligible_children': key in eligible_parents or by_id[key].get('has_eligible_children') is True}
            for key in keep}, health


def signature(cards):
    return {key: (card['project'], card['status'], bool(card.get('orchestrator')),
                  bool(card.get('completed'))) for key, card in cards.items()}


def describe(value):
    return json.dumps(value, ensure_ascii=True)


def changes(before, after):
    if before is None:
        return 'appeared'
    if after is None:
        return 'disappeared'
    result = []
    for field, label in (('project', 'project'), ('status', 'status'),
                         ('orchestrator', 'orchestrator'), ('completed', 'completed')):
        old, new = before.get(field), after.get(field)
        if field in ('orchestrator', 'completed'):
            old, new = bool(old), bool(new)
        if old != new:
            result.append(f'{label} {describe(old)} to {describe(new)}')
    return '; '.join(result)


def reason_for_removal(card, before, archived, now):
    if card['id'] in archived:
        return 'archived in Claude Desktop'
    if card.get('has_eligible_children'):
        return None
    if (not card.get('orchestrator_exception') and not board_worker(card)
            and not any(card.get(key) for key in ('parent_id', 'parent_session', 'parent_app_session'))):
        return 'top-level session has no active or today-finished children'
    # An ancestor can remain visible for a child; age alone cannot justify losing it.
    for other in before.values():
        parent, visited = other.get('parent_id'), set()
        while isinstance(parent, str) and parent in before and parent not in visited:
            if parent == card['id']:
                return None
            visited.add(parent)
            parent = before[parent].get('parent_id')
    if card.get('orchestrator'):
        return None
    ended = card.get('ended') if card.get('ended') is not None else card.get('last_at')
    if (card.get('status') in ('done', 'crashed') or card.get('completed')) and isinstance(ended, (int, float)) and now - ended >= 1800:
        return 'finished more than 30 minutes ago'
    last_at = card.get('last_at')
    if card.get('kind') == 'claude' and card.get('status') == 'working' and isinstance(last_at, (int, float)) and now - last_at >= 86400:
        return 'unfinished Claude session has been inactive for 24 hours'
    return None


def reason_for_change(card_id, before, after, recovered_sources=()):
    """Explain a changed card only by evidence in the candidate's own relationships."""
    old, new = before[card_id], after[card_id]
    if ('codex_sessions' in recovered_sources and old.get('kind') == new.get('kind') == 'codex'
            and old.get('status') == 'unknown' and new.get('status') == 'working'
            and isinstance(old.get('thread'), str) and old['thread']
            and isinstance(old.get('turn_started'), (int, float)) and not isinstance(old['turn_started'], bool)
            and math.isfinite(old['turn_started'])
            and old['turn_started'] == new.get('turn_started')
            and all(old.get(field) == new.get(field) for field in
                    ('project', 'orchestrator', 'completed', 'codex_role', 'thread', 'parent_id',
                     'parent_session', 'parent_app_session'))):
        return 'the same Codex turn is readable after the source recovered'
    same_activity = (old.get('status'), bool(old.get('completed'))) == (new.get('status'), bool(new.get('completed')))
    promoted = (new.get('orchestrator') and not new.get('completed') and old.get('status') in ('done', 'waiting')
                and new.get('status') in ('waiting', 'idle'))
    if new.get('registered') and not old.get('registered') and (same_activity or promoted):
        return 'now placed by the agents registry'
    if (new.get('kind') == 'codex' and new.get('codex_role') == 'orchestrator' and new.get('orchestrator')
            and old.get('codex_role') != 'orchestrator'):
        return 'now a Codex orchestrator, recognised by its thread title'
    if (old['status'] == 'waiting' and new['status'] == 'idle' and bool(old.get('orchestrator'))
            and bool(new.get('orchestrator')) and (old['project'], bool(old.get('completed'))) ==
            (new['project'], bool(new.get('completed')))):
        working = [key for key, card in after.items() if card['status'] == 'working' and card.get('parent_id') == card_id
                   and before.get(key, {}).get('parent_id') != card_id]
        if working:
            return f'idle while its newly nested worker {describe(sorted(working)[0])} works'
    parent = after.get(new.get('parent_id')) if isinstance(new.get('parent_id'), str) else None
    if (old['project'] != new['project'] and old.get('parent_id') != new.get('parent_id') and parent
            and parent['project'] == new['project'] and (old['status'], bool(old.get('orchestrator'))) ==
            (new['status'], bool(new.get('orchestrator')))):
        return f'now nested under {describe(new["parent_id"])} and takes its project'
    return None


def wrapper_replacement(card_id, card, after, wrapper_threads):
    """Find a candidate wrapper card that continues the removed native Codex thread."""
    prefix = 'codex:'
    if card.get('kind') != 'codex' or not card_id.startswith(prefix):
        return None
    thread = card_id[len(prefix):]
    if card.get('thread') != thread:
        return None
    for candidate_id, candidate in after.items():
        if (candidate_id != card_id and candidate.get('kind') == 'codex' and
                wrapper_threads.get(candidate_id) == thread and candidate.get('thread') == thread):
            return candidate_id
    return None


def check_update(url, home, config_path=None, codex_home=None):
    """Return nonzero for unstable, unhealthy or unexplained board changes."""
    old = read_board(url)
    before, before_health = board_view(old, allow_unhealthy=True)
    at = old['now']
    del old
    new, archived, wrapper_threads, claude_tasks = candidate(home, config_path, codex_home, at)
    after, after_health = board_view(new, at, allow_unhealthy=True)
    del new
    latest = read_board(url)
    latest_cards, latest_health = board_view(latest, at, allow_unhealthy=True)
    del latest
    if signature(before) != signature(latest_cards) or before_health != latest_health:
        first, last = signature(before), signature(latest_cards)
        for card_id in sorted(first.keys() | last.keys()):
            if first.get(card_id) != last.get(card_id):
                print(f'Live board changed {describe(card_id)}: {changes(before.get(card_id), latest_cards.get(card_id))}')
        raise CheckError('Running board changed during comparison; retry before restarting')
    removed = before.keys() - after.keys()
    if removed and (any(before[key] != latest_cards[key] for key in removed) or
                    any(before[key].get('parent_id') != latest_cards[key].get('parent_id') for key in before)):
        raise CheckError('Card evidence changed during comparison; retry before restarting')
    for name, (status, reason) in before_health.items():
        new_status, new_reason = after_health[name]
        if status in ('available', 'absent'):
            if status != new_status:
                raise CheckError(f'Board source {name} availability changed; restart blocked')
        elif new_status == 'absent' or (new_status != 'available' and (status, reason) != (new_status, new_reason)):
            raise CheckError(f'Board source {name} changed while unhealthy; restart blocked')
    for name, (status, _) in before_health.items():
        if status in ('unavailable', 'unsupported'):
            print(f'No baseline for source {name} ({status}).')
    recovered_sources = {name for name, health in before_health.items()
                         if health == ('unsupported', 'unrecognized_format') and after_health[name][0] == 'available'}
    old_sig, new_sig = signature(before), signature(after)
    blocked = False
    for project in sorted({card['project'] for card in before.values()} - {card['project'] for card in after.values()}):
        print(f'Project {describe(project)} disappears')
    for card_id in sorted(removed):
        card = before[card_id]
        replacement = wrapper_replacement(card_id, card, after, wrapper_threads)
        if replacement:
            print(f'Re-keyed {describe(card["project"])} {describe(card_id)} as {describe(replacement)}')
            continue
        session = claude_tasks.get(card_id)
        if session and session in after:
            print(f'Merged {describe(card["project"])} {describe(card_id)} into its Claude session card {describe(session)}')
            continue
        reason = reason_for_removal(card, before, archived, at)
        print(f'Removed {describe(card["project"])} {describe(card_id)} status {describe(card["status"])}: {reason or "unexplained; restart blocked"}')
        blocked |= reason is None
    for card_id in sorted(old_sig.keys() & new_sig.keys()):
        if old_sig[card_id] != new_sig[card_id]:
            reason = reason_for_change(card_id, before, after, recovered_sources)
            print(f'Changed {describe(card_id)}: {changes(before[card_id], after[card_id])}; {reason or "unexplained; restart blocked"}')
            blocked |= reason is None
    for card_id in sorted(new_sig.keys() - old_sig.keys()):
        print(f'Added {describe(after[card_id]["project"])} {describe(card_id)}')
    if blocked:
        return 1
    print(f'Board comparison passed: {len(before)} visible cards before, {len(after)} after.')
    return 0
