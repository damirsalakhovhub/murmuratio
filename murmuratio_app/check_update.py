"""Compare a running local board with this checkout without writing snapshots."""
import http.client
import json
import math
import os
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from murmuratio_app.adapters.codex_sessions import CodexSource
from murmuratio_app.config import Config
from murmuratio_app.discovery import Discovery
from murmuratio_app.now import NowSource
from murmuratio_app.server import sanitize


BOARD_SOURCES = ('claude_desktop', 'claude_sessions', 'codex_sessions', 'codex_wrapper')
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


def chats_hidden_on_disk(home, config_path):
    """Check the persisted board setting independently of the candidate parser."""
    path = Path(config_path) if config_path else Path(home) / '.config/murmuratio/config.toml'
    try:
        with path.open('rb') as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        raise CheckError('Cannot verify the Codex chat setting') from exc
    board = data.get('board', {})
    if not isinstance(board, dict) or not isinstance(board.get('show_codex_chats', False), bool):
        raise CheckError('Cannot verify the Codex chat setting')
    return not board.get('show_codex_chats', False)


def candidate(home, config_path, codex_home, at):
    config = Config.load(home, config_path)
    # These modules are unrelated to card discovery and can write quota history,
    # routing files or start background samplers. Keep wrapper/chat discovery on.
    for name in ('forecast', 'routing', 'burn_rate', 'codex_credits'):
        config.modules[name] = False
    codex_path = Discovery.at(home, config, codex_home).codex_home
    codex = CodexSource(codex_path, sanitize, include_reset=False) if config.modules['codex_chats'] else None
    source = NowSource(home=home, config=config, codex=codex, codex_home=codex_path,
                       sanitize=sanitize, clock=lambda: at)
    archived_before = archived_from_disk(home)
    hidden_before = chats_hidden_on_disk(home, config_path)
    state = source.state()
    archived = archived_before & archived_from_disk(home)
    hidden = hidden_before and chats_hidden_on_disk(home, config_path)
    return state, archived, not hidden


def board_view(state, at=None):
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
        if status not in ('available', 'absent'):
            raise CheckError(f'Board source {name} is unavailable or unsupported')
        health[name] = status
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
        if not finished(card) or at - ended < 1800:
            keep.add(card['id'])
    pending = list(keep)
    while pending:
        parent = by_id[pending.pop()].get('parent_id')
        if isinstance(parent, str) and parent in by_id and parent not in keep:
            keep.add(parent)
            pending.append(parent)
    # Reduce the response immediately. The snapshot cannot retain task text or paths.
    fields = ('id', 'project', 'status', 'orchestrator', 'completed', 'parent_id',
              'kind', 'codex_role', 'ended', 'last_at')
    return {key: {field: by_id[key].get(field) for field in fields} for key in keep}, health


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


def reason_for_removal(card, before, archived, show_chats, now):
    if card['id'] in archived:
        return 'archived in Claude Desktop'
    if card.get('kind') == 'codex' and card.get('codex_role') == 'chat' and not show_chats:
        return 'interactive Codex chats are hidden by the current setting'
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


def check_update(url, home, config_path=None, codex_home=None):
    """Return nonzero for unstable, unhealthy or unexplained board changes."""
    old = read_board(url)
    before, before_health = board_view(old)
    at = old['now']
    del old
    new, archived, show_chats = candidate(home, config_path, codex_home, at)
    after, after_health = board_view(new, at)
    del new
    latest = read_board(url)
    latest_cards, latest_health = board_view(latest, at)
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
    if before_health != after_health:
        raise CheckError('Board source availability changed; restart blocked')
    old_sig, new_sig = signature(before), signature(after)
    blocked = False
    for project in sorted({card['project'] for card in before.values()} - {card['project'] for card in after.values()}):
        print(f'Project {describe(project)} disappears')
    for card_id in sorted(removed):
        card = before[card_id]
        reason = reason_for_removal(card, before, archived, show_chats, at)
        print(f'Removed {describe(card["project"])} {describe(card_id)} status {describe(card["status"])}: {reason or "unexplained; restart blocked"}')
        blocked |= reason is None
    for card_id in sorted(old_sig.keys() & new_sig.keys()):
        if old_sig[card_id] != new_sig[card_id]:
            print(f'Changed {describe(card_id)}: {changes(before[card_id], after[card_id])}; unexplained; restart blocked')
            blocked = True
    for card_id in sorted(new_sig.keys() - old_sig.keys()):
        print(f'Added {describe(after[card_id]["project"])} {describe(card_id)}')
    if blocked:
        return 1
    print(f'Board comparison passed: {len(before)} visible cards before, {len(after)} after.')
    return 0
