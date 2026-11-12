"""Adapters for Claude Desktop metadata and Claude Code transcripts."""
import json
from pathlib import Path
from murmuratio_app.adapters.base import Adapter
from murmuratio_app.adapters.settings import recorded_effort
from murmuratio_app.adapters.brief import brief_view
from murmuratio_app.adapters.activity import working_seconds
from murmuratio_app.adapters.parsing import consume_claude, consume_claude_sidechain
from murmuratio_app.adapters.shared import model_context_window, claude_label, claude_status
from murmuratio_app.common import one_line
from murmuratio_app.naming import name_fields
from murmuratio_app.grouping import codex_launch_matches


class ClaudeDesktopAdapter(Adapter):
    name = 'claude_desktop'

    def __init__(self, root):
        super().__init__()
        self.root = Path(root)
        self.cache = {}

    def collect(self):
        seen = set()
        for path in self.files('*/*/local_*.json'):
            seen.add(path)
            try:
                st = path.stat()
                signature = (st.st_ino, st.st_size, st.st_mtime_ns)
                cached = self.cache.get(path)
                if cached is None or cached[0] != signature:
                    raw = json.loads(path.read_text())
                    if not isinstance(raw, dict) or not isinstance(raw.get('cliSessionId'), str):
                        self.unsupported()
                        continue
                    parent = raw.get('spawnedFrom')
                    parent = parent if isinstance(parent, dict) else {}
                    normalized = dict(cli_session_id=raw['cliSessionId'], app_session=path.stem,
                                      title=raw.get('title'), model=raw.get('model'), cwd=raw.get('cwd'),
                                      effort=recorded_effort(raw, raw.get('sessionSettings')),
                                      archived=raw.get('isArchived'), scheduled_task_id=raw.get('scheduledTaskId'),
                                      parent_session=one_line(parent.get('sessionId')),
                                      has_parent=bool(parent.get('sessionId')))
                    cached = (signature, normalized)
                    self.cache[path] = cached
                yield self.observation(cached[1], st.st_mtime)
            except OSError:
                self.unavailable()
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
                self.unsupported()
        self.cache = {p: v for p, v in self.cache.items() if p in seen}


class ClaudeSessionsAdapter(Adapter):
    name = 'claude_sessions'

    def __init__(self, root, summary, cache, project):
        super().__init__()
        self.root = Path(root)
        self.summary, self.cache, self.project = summary, cache, project

    def collect(self, now, desktop, seen, active_codex=None):
        sessions = {}
        for path in self.files('*/*.jsonl'):
            try:
                modified = path.stat().st_mtime
                meta = desktop.get(path.stem, {})
                if meta.get('archived') or meta.get('scheduled_task_id') is not None:
                    continue
                seen.add(path)
                data, modified = self.summary(path, consume_claude)
                cached = self.cache.get(path)
                if cached and (cached.malformed or cached.record_types and not cached.record_types.intersection({'user', 'assistant'})):
                    self.unsupported()
                if not data:
                    continue
                parent_session = meta.get('parent_session', '')
                spawned = bool(parent_session)
                activity = min(modified, data.get('last_at') or modified)
                status = claude_status(activity, data.get('last_turn'), now, spawned or not meta)
                pending = not status and bool(active_codex)
                if pending:
                    status = 'waiting'
                if not status:
                    continue
                cwd = data.get('cwd') or meta.get('cwd') or ''
                started = data.get('started')
                model = data.get('model') or one_line(meta.get('model'))
                finished_at = min(activity, data.get('waiting_since') or activity)
                session = dict(id='claude:' + path.stem, kind='claude', who=claude_label(model),
                               model=model or None, effort=recorded_effort(data, meta), _effort_recorded='effort' in data, cwd=cwd, folder=Path(cwd).name or '—', project=self.project(cwd),
                               **name_fields(titles=[('desktop', meta.get('title'))], text=data.get('first_user_raw', data.get('first_user'))),
                               status=status,
                               current_action=data.get('current_action', ''), started=started,
                               turn_started=data.get('turn_started'), ended=finished_at if status == 'done' else None,
                               waiting_since=finished_at if data.get('waiting_since') is not None else None,
                               app_session=meta.get('app_session'),
                               parent_session=parent_session,
                               last_at=activity,
                               seconds=working_seconds(data, now, status, started, data.get('last_at')),
                               context_tokens=data.get('context_tokens'), context_used=data.get('context_tokens'),
                               context_window=model_context_window(data, model),
                               brief=brief_view(data, cwd),
                               _codex_launches=list(data.get('_codex_launches', [])), _codex_pending=pending)
                if status == 'working' and now - activity > 120:
                    session['quiet'] = True
                sessions[path.stem] = session
            except OSError:
                self.unavailable()
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError, RecursionError):
                self.unsupported()
        children = {}
        for path in self.files('*/*/subagents/agent-*.jsonl'):
            parent = sessions.get(path.parent.parent.name)
            if not parent:
                continue
            try:
                if now - path.stat().st_mtime > 86400:
                    continue
                seen.add(path)
                data, modified = self.summary(path, consume_claude_sidechain)
                parent['_codex_launches'].extend(data.get('_codex_launches', []))
                if not data:
                    continue
                meta = {}
                try:
                    meta = json.loads(path.with_suffix('.meta.json').read_text())
                    if not isinstance(meta, dict):
                        meta = {}
                        self.unsupported()
                except FileNotFoundError:
                    pass
                except OSError:
                    self.unavailable()
                except (ValueError, UnicodeError):
                    self.unsupported()
                activity = min(modified, data.get('last_at') or modified)
                if now - activity >= 86400:
                    continue
                status = 'done' if meta.get('finished') is True else claude_status(
                    activity, data.get('last_turn'), now, spawned=True)
                model = data.get('model') or one_line(meta.get('model'))
                cwd = data.get('cwd') or parent['cwd']
                finished_at = min(activity, data.get('waiting_since') or activity)
                child = dict(id=parent['id'] + ':' + path.stem, kind='claude',
                             subagent_id=path.stem.removeprefix('agent-'),
                             agent_type=one_line(meta.get('agentType')), who=claude_label(model), model=model or None, effort=recorded_effort(data, meta), _effort_recorded='effort' in data,
                             cwd=cwd, folder=parent['folder'], project=parent['project'],
                             **name_fields(titles=[('description', meta.get('description'))], text=data.get('first_user_raw', data.get('first_user'))),
                             status=status, completed=status == 'done',
                             current_action=data.get('current_action', ''), started=data.get('started'),
                             turn_started=data.get('turn_started'), ended=finished_at if status == 'done' else None,
                             waiting_since=finished_at if data.get('waiting_since') is not None else None,
                             app_session=None, parent_session=path.parent.parent.name,
                             parent_id=parent['id'], link='exact', last_at=activity,
                             seconds=working_seconds(data, now, status, data.get('started'), data.get('last_at')),
                             context_tokens=data.get('context_tokens'), context_used=data.get('context_tokens'),
                             context_window=model_context_window(data, model), brief=brief_view(data, cwd))
                if status == 'working' and now - activity > 120:
                    child['quiet'] = True
                children.setdefault(path.parent.parent.name, []).append(child)
            except OSError:
                self.unavailable()
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError, RecursionError):
                self.unsupported()
        for session_id, session in sessions.items():
            if session['_codex_pending'] and not any(
                    agent.get('parent_session') == session_id or
                    agent.get('parent_app_session') == session.get('app_session') and session.get('app_session') or
                    any(codex_launch_matches(agent, call, exact=True) for call in session['_codex_launches'])
                    for agent in active_codex):
                continue
            # Status and some context fallbacks are inferred from saved events.
            yield self.observation(session, session.get('last_at'), 'inferred')
            for child in children.get(session_id, []):
                yield self.observation(child, child.get('last_at'), 'inferred')
