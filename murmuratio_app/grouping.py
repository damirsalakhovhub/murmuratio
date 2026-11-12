"""Relationships and retention for normalized board sessions."""
from pathlib import Path
import os
from murmuratio_app.common import active_today
from murmuratio_app.naming import name_fields, orchestrator_name
from murmuratio_app.schema import DesktopSession, Session
from murmuratio_app.agent_registry import card_id, identity, process_matches, TERMINAL


def integration_cards(agents, registry, now, project):
    """Join owned snapshots by provider identity; expose launches even without a transcript."""
    by_identity = {}
    for agent in agents:
        provider_id = (agent.get('thread') if agent['kind'] == 'codex' else
                       agent['id'].removeprefix('claude:').split(':agent-', 1)[0])
        by_identity[(agent['kind'], provider_id, agent.get('subagent_id'))] = agent
    for record in registry.values():
        if 'provider_id' not in record:
            continue
        key = identity(record)
        agent = by_identity.get(key) if record['provider_id'] else None
        if agent is None:
            cwd = record.get('cwd') or ''
            agent = dict(id=card_id(record['kind'], record['provider_id'] or record['id'], record['subagent_id']),
                         kind=record['kind'], thread=record['provider_id'] if record['kind'] == 'codex' else None,
                         who=record['kind'].title(), cwd=cwd, folder=Path(cwd).name or '—',
                         project=project(cwd) if cwd else 'Unknown', task=record.get('name') or 'Untitled',
                         status='unknown', started=record['started_at'], ended=None, last_at=record['started_at'],
                         seconds=0, current_action='', parent_id=None, parent_session='',
                         context_tokens=None, context_used=None, model=None, effort=None, completed=False)
            if record['subagent_id']:
                agent['subagent_id'] = record['subagent_id']
                agent['parent_session'] = record['provider_id']
            agents.append(agent)
            by_identity[key] = agent
        agent.update(registered=True, role=record['role'], registry_status=record['status'],
                     model_requested=record['model_requested'], effort_requested=record['effort_requested'],
                     exit_code=record['exit_code'], _registry_via=record['via'])
        agent['_explicit_nested_orchestrator'] = record['role'] == 'orchestrator' and bool(record['parent_id'])
        if (record['via'] == 'manual' and record['role'] == 'orchestrator'
                and record['status'] == 'running' and record['ended_at'] is None
                and agent.get('status') == 'done'):
            agent.update(completed=True, ended=agent.get('last_at'))
        agent['model'] = agent.get('model') or record['model']
        # A transcript's explicit unknown belongs to the current turn too.
        if not agent.get('_effort_recorded') and agent.get('effort') is None:
            agent['effort'] = record['effort']
        # The launcher's process result is authoritative even when a provider's final event is missing.
        if record['via'] == 'launcher' or record['ended_at'] is not None:
            status = record['status']
            if status in ('starting', 'running'):
                matches = process_matches(record)
                starting_grace = status == 'starting' and now - (record['started_at'] or 0) < 10
                if matches is False or matches is None and not starting_grace:
                    status = 'unknown'
            agent.update(registry_status=status, status={'starting': 'working', 'running': 'working',
                         'done': 'done', 'failed': 'crashed', 'interrupted': 'crashed', 'unknown': 'unknown'}[status],
                         completed=status in TERMINAL, ended=record['ended_at'])
            agent['last_at'] = record['ended_at'] or agent.get('last_at') or record['started_at']
    return agents


def apply_integrations(agents, registry):
    records = {identity(r): r for r in registry.values() if 'provider_id' in r and r.get('provider_id')}
    by_identity = {}
    owned_ids = set()
    for agent in agents:
        provider = agent.get('thread') if agent['kind'] == 'codex' else agent['id'].removeprefix('claude:').split(':agent-', 1)[0]
        by_identity[(agent['kind'], provider, agent.get('subagent_id'))] = agent
    for agent in agents:
        provider = agent.get('thread') if agent['kind'] == 'codex' else agent['id'].removeprefix('claude:').split(':agent-', 1)[0]
        record = records.get((agent['kind'], provider, agent.get('subagent_id')))
        if not record:
            # A Codex launch can fail before reporting a provider ID.
            record = next((r for r in registry.values() if 'provider_id' in r and not r['provider_id']
                           and agent['id'] == card_id(r['kind'], r['id'])), None)
        if not record:
            continue
        owned_ids.add(agent['id'])
        agent['registered'] = True
        agent.pop('guessed', None)
        if record.get('name'):
            if record['via'] == 'launcher':
                agent.update(task=record['name'], title_source='launcher')
            else:
                agent.update(name_fields(titles=[('registry', record['name'])]))
        if record['role'] is not None:
            agent['orchestrator'] = record['role'] == 'orchestrator'
            if agent['kind'] == 'codex':
                agent['codex_role'] = record['role']
        if record['parent_id']:
            parent = by_identity.get((record['parent_kind'], record['parent_id'], record['parent_subagent_id']))
            exact_id = card_id(record['parent_kind'], record['parent_id'], record['parent_subagent_id'])
            agent.update(parent_id=parent['id'] if parent is not None and parent is not agent else exact_id,
                         parent_session=record['parent_id'], parent_app_session='', link='exact')
            if parent is not None and parent is not agent:
                agent['project'] = parent['project']
        elif record['subagent_id']:
            agent.update(parent_id=card_id(record['kind'], record['provider_id']), link='exact')
    return owned_ids

def board_parents(agents, now, candidates=None):
    """Parents with an unfinished child or a child finished on the current local day."""
    aliases = {alias: agent['id'] for agent in (candidates if candidates is not None else agents)
               for alias in (agent['id'], agent['id'].split(':', 1)[-1], agent.get('app_session'), agent.get('thread')) if alias}
    return {parent for agent in agents if (
        not (agent.get('completed') or agent.get('status') in ('done', 'crashed'))
        or active_today(agent.get('ended') or agent.get('last_at'), now))
        if (parent := (agent.get('parent_id') or aliases.get(agent.get('parent_session'))
                       or aliases.get(agent.get('parent_app_session')))) and parent != agent['id']}


def board_orchestrator(agent):
    """Registry coordinators and named top-level Desktop coordinators stay visible."""
    return bool(agent.get('orchestrator') and (agent.get('registered') or agent.get('codex_role') == 'orchestrator') or (
        agent.get('kind') == 'claude' and agent.get('app_session') and not agent.get('parent_id')
        and orchestrator_name(agent.get('task'))))


def board_worker(agent):
    """Provider launch metadata identifies a worker even when its parent is unknown."""
    return agent.get('kind') == 'codex' and agent.get('codex_role') == 'worker'


def board_sessions(agents, now, children=None):
    """Keep coordinators, recorded workers, and chats that currently host agents."""
    parents = board_parents(agents if children is None else children, now, agents)
    return [agent for agent in agents if (
        board_orchestrator(agent) or board_worker(agent)
        or any(agent.get(key) for key in ('parent_id', 'parent_session', 'parent_app_session'))
        or agent['id'] in parents)]


def retain_agents(agents: list[Session], now: float) -> list[Session]:
    """Keep live work, waiting Desktop roots and recent workers, with active ancestors."""
    by_id = {agent['id']: agent for agent in agents}
    keep = {agent['id'] for agent in agents if agent.get('board_source') == 'codex_rollout'
            or agent['status'] in ('working', 'hung')
            or (agent['status'] == 'blocked' and agent.get('current_action') == 'waiting for approval')
            or (agent.get('kind') == 'claude' and agent.get('app_session')
                and agent.get('orchestrator') and agent['status'] == 'waiting')
            or (agent.get('kind') != 'claude' and active_today(agent.get('last_at'), now))
            or now - (agent.get('ended') or agent.get('waiting_since') or agent.get('last_at') or 0) < 1800}
    keep.update(agent['id'] for agent in agents if agent.get('registered') and agent.get('orchestrator')
                and not agent.get('completed'))
    pending = [agent['id'] for agent in agents if agent['id'] in keep and (
        agent['status'] in ('working', 'hung') or
        agent['status'] == 'blocked' and agent.get('current_action') == 'waiting for approval')]
    visited = set(pending)
    while pending:
        parent = by_id[pending.pop()].get('parent_id')
        if parent in by_id and parent not in visited:
            keep.add(parent)
            visited.add(parent)
            pending.append(parent)
    return [agent for agent in agents if agent['id'] in keep
            and (not agent.get('subagent_id') or agent.get('parent_id') in keep)]


def merge_handoffs(agents, aliases):
    """Replace a same-title orchestrator by its successor and redirect its children."""
    claude = [agent for agent in agents if agent['kind'] == 'claude']
    by_id = {agent['id']: agent for agent in claude}
    replaced = {}

    def successor(agent):
        while agent['id'] in replaced:
            agent = replaced[agent['id']]
        return agent

    for agent in sorted(claude, key=lambda a: a['started'] or 0):
        old = by_id.get(agent.get('parent_id'))
        old = successor(old) if old else None
        if (old and old is not agent and not agent.get('subagent_id')
                and not (agent.get('_explicit_nested_orchestrator') or old.get('_explicit_nested_orchestrator'))
                and orchestrator_name(agent['task']) and agent['task'] == old['task']):
            replaced[old['id']] = agent
            agent['parent_id'], agent['parent_session'] = old['parent_id'], old['parent_session']
    for agent in claude:
        if agent.get('parent_id') in replaced:
            agent['parent_id'] = successor(by_id[agent['parent_id']])['id']
    for alias, agent in aliases.items():
        aliases[alias] = successor(agent)
    return [agent for agent in agents if agent['id'] not in replaced]


def link_claude_parents(agents, aliases):
    """Notes sessions inherit their starter's project, independent of file order."""
    def project(agent, seen):
        if agent['project'] or agent['id'] in seen:
            return agent['project']
        parent = aliases.get(agent.get('parent_session'))
        if parent:
            agent['project'] = project(parent, seen | {agent['id']})
        return agent['project']

    for agent in agents:
        project(agent, set())
        parent = aliases.get(agent.get('parent_session'))
        agent['parent_id'] = parent['id'] if parent and parent is not agent and parent['project'] == agent['project'] else None
        if agent.get('subagent_id') and agent['parent_id']:
            agent['link'] = 'exact'


def inherit_starter_folders(agents, aliases):
    by_id = {agent['id']: agent for agent in agents}

    def folder(agent, seen):
        if 'side-session-notes' not in Path(agent['cwd']).parts and not agent['folder'].startswith('local_'):
            return agent['folder']
        fallback = agent['project'] if agent['project'] and not agent['project'].startswith('local_') else '—'
        if agent['id'] in seen:
            return fallback
        parent = (by_id.get(agent.get('parent_id')) or aliases.get(agent.get('parent_session'))
                  or aliases.get(agent.get('parent_app_session')))
        agent['folder'] = folder(parent, seen | {agent['id']}) if parent else fallback
        return agent['folder']

    for agent in agents:
        folder(agent, set())


def mark_codex_worker(agent):
    """A linked chat's completed turn is a worker completion, not an idle chat."""
    if agent.get('codex_role') == 'chat' and agent['status'] == 'waiting' and agent.get('waiting_since') is not None:
        agent.update(status='done', completed=True, ended=agent.pop('waiting_since'))
    agent['codex_role'] = 'worker'


def merge_codex_sources(wrappers, rollouts):
    """Unify observations only when a recorded thread or run identifier agrees."""
    groups = []
    for agent in wrappers + rollouts:
        keys = {(namespace, value) for namespace, value in (
            ('thread', agent.get('thread')), ('run', agent.get('_run_id')))
            if isinstance(value, str) and value}
        matching = [group for group in groups if group['keys'] & keys]
        if not matching:
            matching = [{'keys': set(), 'items': []}]
            groups.append(matching[0])
        group = matching[0]
        for other in matching[1:]:
            group['keys'].update(other['keys'])
            group['items'].extend(other['items'])
            groups.remove(other)
        group['keys'].update(keys)
        group['items'].append(agent)
    result = []
    for group in groups:
        items = group['items']
        wrapped = [item for item in items if item.get('_wrapper')]
        native = max((item for item in items if item.get('_rollout')),
                     key=lambda item: item.get('last_at') or 0, default=None)
        if not wrapped:
            result.append(native)
            continue
        agent = dict(max(wrapped, key=lambda item: (item.get('started') or 0, item.get('last_at') or 0)))
        if native:
            native = dict(native)
            mark_codex_worker(native)
            # The wrapper can exit while its worker still owns the rollout.
            if agent.get('_dead_expired') and not native.get('completed'):
                agent['completed'], agent['ended'] = False, None
            agent['_rollout'], agent['_turns'] = True, native.get('_turns', [])
            agent['_parent_thread'] = native.get('_parent_thread')
            agent['board_source'] = 'codex_rollout'
            if native.get('brief'):
                # The rollout holds every tool output; its request is the wrapper preamble plus the brief,
                # whose first line is already the card title, so the panel leaves it out.
                agent['brief'] = {**native['brief'], 'request': ''}
            titles = [(item.get('title_source'), item.get('task')) for item in (agent, native)
                      if item.get('task') != 'Untitled']
            agent.update(name_fields(titles=titles))
            native_start = native.get('turn_started') or native.get('started')
            same_run = (native_start is not None and native_start >= (agent.get('started') or 0) - 5 and
                        (not agent.get('completed') or native_start <= (agent.get('ended') or agent.get('last_at') or 0) + 5))
            if same_run:
                for key in ('model', 'effort'):
                    agent[key] = native.get(key)
                agent['_effort_recorded'] = native.get('_effort_recorded', False)
            for key in ('thread', 'model', 'cwd', 'folder', 'project', 'context_used',
                        'context_tokens', 'context_window', 'turn_started'):
                if key == 'model' and not same_run:
                    continue
                value = native.get(key)
                if key in ('context_used', 'context_tokens') and native.get('_context_invalidated'):
                    # Unknown after compaction must replace the wrapper's older measurement too.
                    agent[key] = value
                    continue
                if value is not None and value != '' and (not agent.get(key) or key in (
                        'context_used', 'context_tokens', 'context_window', 'turn_started')):
                    agent[key] = value
            # A completed wrapper stays terminal even if rollout timestamps are newer.
            native_start = native.get('turn_started')
            if native.get('completed') and native['status'] == 'crashed':
                native_start = native_start or native.get('last_at')
            if (not agent.get('_pid_known') and not agent.get('completed') and native_start and
                    (not agent.get('started') or native_start >= agent['started'] - 5)):
                for key in ('status', 'completed', 'ended', 'last_at', 'current_action', 'waiting_since'):
                    agent[key] = native.get(key)
            agent['seconds'] = native['seconds'] if agent['status'] == 'working' else native['_stopped_seconds']
        agent['codex_role'] = 'worker'
        result.append(agent)
    return result


def codex_launch_matches(agent, call, exact=False):
    if call.get('thread'):
        if call['thread'] != agent.get('thread'):
            return False
        if exact:
            return True
    if not call.get('cwd') or call.get('at') is None:
        return False
    turns = agent.get('_turns') or [{'at': agent.get('started'), 'cwd': agent.get('cwd')}]
    return any(turn.get('at') is not None and turn.get('cwd')
               and os.path.normpath(turn['cwd']) == os.path.normpath(call['cwd'])
               and -5 <= turn['at'] - call['at'] <= 120 for turn in turns)


def active_at(chat, cwd, at):
    """Whether a Codex chat's turn in this folder was open at this moment."""
    turns = chat.get('_turns') or [{'at': chat.get('started'), 'cwd': chat.get('cwd')}]
    before = [turn for turn in turns if turn.get('at') is not None and turn['at'] <= at + 5]
    if not before or not before[-1].get('cwd'):
        return False
    if os.path.normpath(before[-1]['cwd']) != os.path.normpath(cwd):
        return False
    later = any(turn.get('at') is not None and turn['at'] > at + 5 for turn in turns)
    ended = chat.get('ended') or chat.get('waiting_since')
    return (chat.get('last_at') or 0) >= at - 5 and (later or not ended or ended >= at - 5)


def codex_launcher(agent, codex, claimed):
    """The Codex chat that ran a wrapper, when the wrapper's recorded Claude parent cannot have.

    The wrapper records the Claude session from its environment first. A Codex app started from a
    Claude session inherits that session's variables, so its own launches name an unrelated Claude chat.
    The Claude chat was in another folder; one Codex chat had an open turn in the launch folder."""
    origin, started = agent.get('from_cwd'), agent.get('started')
    if not agent.get('_wrapper') or not origin or started is None:
        return None
    if claimed and claimed.get('cwd') and os.path.normpath(claimed['cwd']) == os.path.normpath(origin):
        return None
    hosts = [chat for chat in codex if chat is not agent and chat.get('codex_role') in ('chat', 'orchestrator')
             and active_at(chat, origin, started)]
    return hosts[0] if len(hosts) == 1 else None


def link_codex(agents, aliases, launches):
    codex = [agent for agent in agents if agent['kind'] == 'codex']
    calls = [(aliases.get(alias), call) for alias, call in launches if aliases.get(alias)]
    threads = {agent.get('thread'): agent for agent in codex if agent.get('thread')}
    for agent in codex:
        if agent.get('codex_role') == 'orchestrator':
            agent['parent_id'] = None
            continue
        native_parent = agent.get('_parent_thread')
        if native_parent:
            agent['parent_session'] = native_parent
            parent = threads.get(native_parent)
            agent['parent_id'] = parent['id'] if parent is not agent and parent else None
            if agent['parent_id']:
                agent['link'] = 'exact'
            else:
                agent['_link_ambiguous'] = True
            continue
        # A launcher run from a Codex thread records that thread as its parent session.
        host = threads.get(agent.get('parent_session')) if agent.get('_wrapper') else None
        if host is not None and host is not agent:
            agent['parent_id'], agent['link'] = host['id'], 'exact'
            continue
        parent = aliases.get(agent.get('parent_session')) or aliases.get(agent.get('parent_app_session'))
        launcher = codex_launcher(agent, codex, parent)
        if launcher:
            agent['parent_id'], agent['link'] = launcher['id'], 'inferred'
            continue
        if parent:
            agent['parent_id'], agent['link'] = parent['id'], 'exact'
            continue
        exact = {parent['id']: parent for parent, call in calls
                 if call.get('thread') and call['thread'] == agent.get('thread')}
        if len(exact) == 1:
            agent['parent_id'], agent['link'] = next(iter(exact)), 'exact'
        else:
            agent['parent_id'] = None
            if len(exact) > 1:
                agent['_link_ambiguous'] = True

    candidates = {agent['id']: [index for index, (_, call) in enumerate(calls) if codex_launch_matches(agent, call)]
                  for agent in codex}
    for agent in codex:
        if (agent.get('parent_id') or agent.get('_link_ambiguous') or agent.get('codex_role') == 'orchestrator'
                or len(candidates[agent['id']]) != 1):
            continue
        index = candidates[agent['id']][0]
        if sum(index in options for options in candidates.values()) != 1:
            continue
        parent = calls[index][0]
        agent['parent_id'], agent['link'] = parent['id'], 'inferred'
    # Do not let malformed native parent cycles hide every card.
    by_id = {agent['id']: agent for agent in codex}
    for agent in codex:
        seen, current = {agent['id']}, agent
        while current.get('parent_id') in by_id:
            if current['parent_id'] in seen:
                agent['parent_id'] = None
                agent.pop('link', None)
                break
            seen.add(current['parent_id'])
            current = by_id[current['parent_id']]
    for agent in codex:
        agent['orchestrator'] = agent.get('codex_role') == 'orchestrator'
        if agent['orchestrator']:
            continue
        if agent.get('parent_id'):
            mark_codex_worker(agent)
        parent = by_id.get(agent.get('parent_id')) or next(
            (parent for parent in aliases.values() if parent['id'] == agent.get('parent_id')), None)
        if parent:
            agent['project'] = parent['project']


def link_claude_tasks(agents):
    """A claude-task run and the Claude session it started are one worker: keep the session's card,
    nested under the Codex thread the run records as its parent, and drop the run's duplicate card."""
    sessions = {agent['id'].removeprefix('claude:'): agent for agent in agents if agent['kind'] == 'claude'}
    threads = {agent['thread']: agent for agent in agents if agent['kind'] == 'codex' and agent.get('thread')}
    merged = set()
    for run in agents:
        worker = sessions.get(run.get('_claude_session')) if run['kind'] == 'codex' else None
        if not worker:
            continue
        worker.update(name_fields(titles=[(item.get('title_source'), item.get('task'))
                                          for item in (worker, run) if item.get('task') != 'Untitled']))
        if not worker.get('_effort_recorded') and worker.get('effort') is None:
            worker['effort'] = run.get('effort')
        if not worker.get('model'):
            worker['model'] = run.get('model')
        merged.add(run['id'])
        parent = threads.get(run.get('parent_session'))
        if parent and not worker.get('parent_id') and not worker.get('app_session'):
            worker['parent_id'], worker['link'], worker['project'] = parent['id'], 'exact', parent['project']
    return [agent for agent in agents if agent['id'] not in merged] if merged else agents


def registry_keys(agent):
    """The registry ids a card answers to: a Claude session id (or its Desktop alias), a Codex thread id."""
    if agent['kind'] == 'claude':
        return [key for key in (agent['id'].removeprefix('claude:'), agent.get('app_session')) if key]
    if agent['kind'] == 'codex':
        return [agent['thread']] if agent.get('thread') else [agent['id'].removeprefix('codex:')]
    return []


def column_key(name):
    """Case-insensitive, hyphen/underscore = space: «northstar-app», «Northstar_App» and «Northstar App» are one column."""
    return ' '.join(str(name).replace('-', ' ').replace('_', ' ').lower().split())


def registry_column(project, columns):
    """The board column for a registry project: a card stem («northstar-app») matched to a known column
    («Northstar App»); an unknown stem is title-cased, never a second column for a known project."""
    key = column_key(project)
    for column in columns:
        if column and column_key(column) == key:
            return column
    return ' '.join(word[:1].upper() + word[1:] for word in key.split())


def apply_registry(agents, registry, mark_guessed=False, columns=()):
    """Column, role, title and parent recorded by the launch wrappers replace the heuristics.

    Cards missing from the registry keep their heuristic placement; with mark_guessed those that
    started at or after the registry's coverage start (earliest record started_at) carry guessed=True
    so the board can say so; earlier cards are simply unmarked, and no dated records mark nothing. Registered cards carry registered=True.
    columns: the board's known column names, so a registry project stem maps onto them."""
    if registry is None:
        return
    integrations = {key: r for key, r in registry.items() if 'provider_id' in r}
    registry = {key: r for key, r in registry.items() if 'provider_id' not in r}
    columns = list(dict.fromkeys([*columns, *(agent.get('project') for agent in agents)]))
    # A Claude app id (local_…) stands for the CLI session whose record carries it as app_id.
    by_app = {record['app_id']: record for record in registry.values() if record.get('app_id')}
    cards = {}
    for agent in agents:
        for key in registry_keys(agent):
            cards.setdefault((agent['kind'], key), []).append(agent)

    def card_for(key, kind=None):
        # A thread can have several wrapper runs: the live, most recent one stands for it.
        keys = {key}
        if key in by_app:
            keys.add(by_app[key]['id'])
        if registry.get(key, {}).get('app_id'):
            keys.add(registry[key]['app_id'])
        found = [agent for (card_kind, card_key), group in cards.items() if card_key in keys
                 and (kind is None or card_kind == kind) for agent in group]
        return max(found, key=lambda a: (not a.get('completed'), a.get('last_at') or 0), default=None)

    def record_for(agent):
        for key in registry_keys(agent):
            record = registry.get(key) or by_app.get(key)
            if record and record.get('kind') == agent['kind']:
                return record
        return None

    starts = [record['started_at'] for record in registry.values()
              if isinstance(record.get('started_at'), (int, float))]
    coverage_start = min(starts) if starts else None
    registered = {}
    for agent in agents:
        record = record_for(agent)
        if record:
            registered[agent['id']] = (agent, record)
    for agent, record in registered.values():
        agent['registered'] = True
        agent.pop('guessed', None)
        if record.get('name'):
            agent.update(name_fields(titles=[(agent.get('title_source'), agent['task']), ('registry', record['name'])]))
        if record.get('role') == 'orchestrator':
            # An orchestrator is always a column root; a handoff predecessor is only named.
            agent['orchestrator'], agent['parent_id'], agent['completed'] = True, None, False
            agent.pop('link', None)
            agent.pop('follows', None)
            if record.get('parent_id'):
                parent = card_for(record['parent_id'])
                predecessor = registry.get(record['parent_id']) or by_app.get(record['parent_id']) or {}
                name = predecessor.get('name') or (parent or {}).get('task')
                if name and parent is not agent:
                    agent['follows'] = name
            if agent['kind'] == 'codex':
                agent['codex_role'] = 'orchestrator'
        else:
            if record.get('role'):
                agent['orchestrator'] = False
                if agent['kind'] == 'codex' and record['role'] == 'worker':
                    agent['codex_role'] = 'worker'
            parent = card_for(record['parent_id']) if record.get('parent_id') else None
            if record.get('parent_id') and not agent.get('parent_session'):
                agent['parent_session'] = record['parent_id']
            if parent is not None and parent is not agent:
                agent['parent_id'], agent['link'] = parent['id'], 'exact'
            # Missing or unresolved registry parents keep the heuristic relationship.
        if record.get('project'):
            agent['project'] = registry_column(record['project'], columns)
    owned_ids = apply_integrations(agents, integrations)
    by_id = {agent['id']: agent for agent in agents}
    # Registry parents can form a cycle; break it rather than hide a column.
    for agent in agents:
        seen, current = {agent['id']}, agent
        while current.get('parent_id') in by_id:
            if current['parent_id'] in seen:
                agent['parent_id'] = None
                agent.pop('link', None)
                break
            seen.add(current['parent_id'])
            current = by_id[current['parent_id']]
    # Resolve the whole chain, independent of card order, after cutting cycles.
    for agent, record in registered.values():
        if record.get('project'):
            continue
        current = agent
        while current['id'] in registered and not registered[current['id']][1].get('project'):
            parent = by_id.get(current.get('parent_id'))
            if parent is None:
                break
            current = parent
        agent['project'] = current['project']
    for agent in agents:
        if agent['id'] not in owned_ids:
            continue
        seen, current = {agent['id']}, agent
        while current.get('parent_id') in by_id and current['parent_id'] not in seen:
            current = by_id[current['parent_id']]
            seen.add(current['id'])
        agent['project'] = current['project']
    # A guessed card nested under a registered ancestor follows that ancestor's column.
    for agent in agents:
        if agent['id'] in registered or agent['id'] in owned_ids:
            continue
        seen, current = {agent['id']}, by_id.get(agent.get('parent_id'))
        while current is not None and current['id'] not in seen:
            if current['id'] in registered:
                agent['project'] = current['project']
                break
            seen.add(current['id'])
            current = by_id.get(current.get('parent_id'))
        # Subagents have an exact parent from their transcript path and no registry entry.
        if (mark_guessed and not agent.get('subagent_id')
                and coverage_start is not None and isinstance(agent.get('started'), (int, float))
                and agent['started'] >= coverage_start):
            agent['guessed'] = True


def link_parents(agents: list[Session], desktop: dict[str, DesktopSession], clock,
                 registry=None, mark_guessed=False, columns=()) -> list[Session]:
    claude = [a for a in agents if a['kind'] == 'claude']
    aliases = {alias: a for a in claude for alias in (a['id'].removeprefix('claude:'), a.get('app_session')) if alias}
    launches = [(agent['id'].removeprefix('claude:'), call)
                for agent in claude for call in agent.get('_codex_launches', [])]
    link_claude_parents(claude, aliases)
    # Malformed/cyclic source relationships must not hide every card in a column.
    by_id = {a['id']: a for a in claude}
    for agent in claude:
        seen, current = {agent['id']}, agent
        while current.get('parent_id') in by_id:
            if current['parent_id'] in seen:
                agent['parent_id'] = None
                break
            seen.add(current['parent_id'])
            current = by_id[current['parent_id']]
    agents = merge_handoffs(agents, aliases)
    claude = [a for a in agents if a['kind'] == 'claude']
    link_codex(agents, aliases, launches)
    agents = link_claude_tasks(agents)
    active_parents = {agent.get('parent_id') for agent in agents if agent['kind'] == 'codex'
                      and agent['status'] in ('working', 'hung')}
    removed = {agent['id'] for agent in claude if agent.get('_codex_pending') and agent['id'] not in active_parents}
    removed.update(agent['id'] for agent in claude if agent.get('subagent_id') and agent.get('parent_id') in removed)
    if removed:
        agents = [agent for agent in agents if agent['id'] not in removed]
        claude = [agent for agent in claude if agent['id'] not in removed]
        aliases = {alias: agent for alias, agent in aliases.items() if agent['id'] not in removed}
        for agent in agents:
            if agent.get('parent_id') in removed:
                agent['parent_id'] = None
                agent.pop('link', None)
    by_id = {a['id']: a for a in claude}
    roots = {}
    known_parents = {alias for cli, meta in desktop.items() for alias in (cli, meta.get('app_session')) if alias}
    for agent in claude:
        # A chat whose starter is known but not on the board is a root only if it is an orchestrator;
        # other orphans (a starter that is hidden or archived) join their project's orchestrator below.
        # A standalone CLI transcript does not establish an orchestrator role.
        agent['orchestrator'] = bool(agent.get('app_session')) and not agent['parent_id'] and (not agent['parent_session'] or (
            agent['parent_session'] in known_parents and orchestrator_name(agent['task'])))
        if agent['orchestrator']:
            roots.setdefault(agent['project'], []).append(agent)
    for agent in claude:
        if agent['orchestrator'] or agent['parent_id'] or agent['parent_session'] not in known_parents:
            continue
        root = max(roots.get(agent['project'], []), key=lambda a: a['started'] or 0, default=None)
        if root:
            agent['parent_id'] = root['id']
        else:
            agent['orchestrator'] = True
            roots.setdefault(agent['project'], []).append(agent)
    inherit_starter_folders(agents, aliases)
    apply_registry(agents, registry, mark_guessed, columns)
    by_id.update({agent['id']: agent for agent in agents if agent['kind'] == 'codex'})
    working_ancestors = set()
    for child in agents:
        if child['status'] != 'working':
            continue
        parent_id = child.get('parent_id')
        while parent_id in by_id:
            working_ancestors.add(parent_id)
            parent_id = by_id[parent_id].get('parent_id')
    for agent in agents:
        if (agent.get('_registry_via') == 'manual' and agent.get('registry_status') == 'running'
                and agent.get('ended') is None and agent.get('status') == 'done'):
            agent.update(completed=True, ended=agent.get('last_at'))
        if agent.get('_registry_via') in ('manual', 'hook', 'launcher') and agent.get('registry_status') in TERMINAL:
            agent.update(status={'done': 'done', 'failed': 'crashed', 'interrupted': 'crashed'}[
                agent['registry_status']], completed=True)
        is_coordinator = agent['kind'] == 'claude' or (
            agent['kind'] == 'codex' and agent.get('orchestrator'))
        if (is_coordinator and agent.get('orchestrator') and agent['status'] == 'done'
                and agent.get('registry_status') not in TERMINAL
                and not (agent.get('_registry_via') == 'manual' and agent.get('completed'))):
            agent['status'] = 'waiting'
            agent['ended'] = None
        if is_coordinator and agent.get('orchestrator') and agent['status'] == 'waiting' and agent['id'] in working_ancestors:
            agent['status'] = 'idle'
    for agent in agents:
        for key in ('_codex_launches', '_codex_pending', '_link_ambiguous', '_wrapper', '_pid_known', '_dead_expired', '_rollout', '_run_id', '_turns', '_stopped_seconds', '_parent_thread', '_claude_session', '_effort_recorded', '_context_invalidated', '_registry_via', '_explicit_nested_orchestrator'):
            agent.pop(key, None)
    return agents
