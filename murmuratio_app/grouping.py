"""Relationships and retention for normalized board sessions."""
from pathlib import Path
from murmuratio_app.common import active_today
from murmuratio_app.schema import DesktopSession, Session

def retain_agents(agents: list[Session], now: float) -> list[Session]:
    """Keep today's activity, running work and unarchived orchestrators, with ancestors."""
    by_id = {agent['id']: agent for agent in agents}
    keep = {agent['id'] for agent in agents if agent['status'] in ('working', 'hung')
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


def link_parents(agents: list[Session], desktop: dict[str, DesktopSession], project_for, clock) -> list[Session]:
    claude = [a for a in agents if a['kind'] == 'claude']
    aliases = {alias: a for a in claude for alias in (a['id'].removeprefix('claude:'), a.get('app_session')) if alias}
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
    for agent in agents:
        if agent['kind'] != 'codex':
            continue
        parent = aliases.get(agent.get('parent_session')) or aliases.get(agent.get('parent_app_session'))
        if not parent:
            project = project_for(agent['from_cwd']) if agent.get('from_cwd') else agent['project']
            candidates = roots.get(project, [])
            parent = max(candidates, key=lambda a: (a['cwd'] == agent['cwd'], a['last_at'] or 0), default=None)
        agent['parent_id'] = parent['id'] if parent else None
        agent['orchestrator'] = False
        if parent:
            agent['project'] = parent['project']
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
            agent['seconds'] = max(0, clock() - agent['started']) if agent['started'] is not None else None
        if agent['orchestrator'] and agent['status'] == 'waiting' and agent['id'] in working_ancestors:
            agent['status'] = 'idle'
    return agents
