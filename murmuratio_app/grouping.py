"""Relationships and retention for normalized board sessions."""
from pathlib import Path
import os
from murmuratio_app.common import active_today
from murmuratio_app.schema import DesktopSession, Session

def retain_agents(agents: list[Session], now: float) -> list[Session]:
    """Keep today's activity, running work and unarchived orchestrators, with ancestors."""
    by_id = {agent['id']: agent for agent in agents}
    keep = {agent['id'] for agent in agents if agent.get('board_source') == 'codex_rollout'
            or agent['status'] in ('working', 'hung')
            or (agent['status'] == 'blocked' and agent.get('current_action') == 'waiting for approval')
            or agent.get('orchestrator') or active_today(agent.get('last_at'), now)}
    pending = list(keep)
    while pending:
        parent = by_id[pending.pop()].get('parent_id')
        if parent in by_id and parent not in keep:
            keep.add(parent)
            pending.append(parent)
    return [agent for agent in agents if agent['id'] in keep]


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
        if old and old is not agent and 'orchestrator' in agent['task'].lower() and agent['task'] == old['task']:
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
        keys = {value for value in (agent.get('thread'), agent.get('_run_id'),
                                    agent['id'].removeprefix('codex:')) if isinstance(value, str) and value}
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
            agent['board_source'] = 'codex_rollout'
            for key in ('thread', 'model', 'cwd', 'folder', 'project', 'task', 'context_used',
                        'context_tokens', 'context_window', 'turn_started'):
                value = native.get(key)
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


def link_codex(agents, aliases, launches):
    codex = [agent for agent in agents if agent['kind'] == 'codex']
    calls = [(aliases.get(alias), call) for alias, call in launches if aliases.get(alias)]
    for agent in codex:
        parent = aliases.get(agent.get('parent_session')) or aliases.get(agent.get('parent_app_session'))
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
        if agent.get('parent_id') or agent.get('_link_ambiguous') or len(candidates[agent['id']]) != 1:
            continue
        index = candidates[agent['id']][0]
        if sum(index in options for options in candidates.values()) != 1:
            continue
        parent = calls[index][0]
        agent['parent_id'], agent['link'] = parent['id'], 'inferred'
    for agent in codex:
        agent['orchestrator'] = False
        if agent.get('parent_id'):
            mark_codex_worker(agent)
        parent = next((parent for parent in aliases.values() if parent['id'] == agent.get('parent_id')), None)
        if parent:
            agent['project'] = parent['project']


def link_parents(agents: list[Session], desktop: dict[str, DesktopSession], clock) -> list[Session]:
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
    active_parents = {agent.get('parent_id') for agent in agents if agent['kind'] == 'codex'
                      and agent['status'] in ('working', 'hung')}
    removed = {agent['id'] for agent in claude if agent.get('_codex_pending') and agent['id'] not in active_parents}
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
        agent['orchestrator'] = not agent['parent_id'] and (not agent['parent_session'] or (
            agent['parent_session'] in known_parents and 'orchestrator' in agent['task'].lower()))
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
    working_ancestors = set()
    for child in agents:
        if child['status'] != 'working':
            continue
        parent_id = child.get('parent_id')
        while parent_id in by_id:
            working_ancestors.add(parent_id)
            parent_id = by_id[parent_id].get('parent_id')
    for agent in claude:
        if agent['orchestrator'] and agent['status'] == 'done':
            agent['status'] = 'waiting'
            agent['ended'] = None
        if agent['orchestrator'] and agent['status'] == 'waiting' and agent['id'] in working_ancestors:
            agent['status'] = 'idle'
    for agent in agents:
        for key in ('_codex_launches', '_codex_pending', '_link_ambiguous', '_wrapper', '_pid_known', '_dead_expired', '_rollout', '_run_id', '_turns', '_stopped_seconds'):
            agent.pop(key, None)
    return agents
