"""Adapters for Claude Desktop metadata and Claude Code transcripts."""
import json
from pathlib import Path
from murmuratio_app.adapters.base import Adapter
from murmuratio_app.adapters.activity import working_seconds
from murmuratio_app.adapters.parsing import consume_claude, consume_claude_sidechain
from murmuratio_app.adapters.shared import claude_context_window, claude_label, claude_status
from murmuratio_app.common import one_line
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
                                      archived=raw.get('isArchived'), scheduled_task_id=raw.get('scheduledTaskId'),
                                      parent_session=one_line(parent.get('sessionId')),
                                      has_parent=bool(parent.get('sessionId')))
                    cached = (signature, normalized)
                    self.cache[path] = cached
                yield self.observation(cached[1], st.st_mtime)
            except OSError:
                self.unavailable()
            except (ValueError, TypeError, UnicodeError):
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
                               model=model, cwd=cwd, folder=Path(cwd).name or '—', project=self.project(cwd),
                               task=one_line(meta.get('title')) or data.get('first_user') or 'Untitled',
                               title_source='desktop' if one_line(meta.get('title')) else 'first_user',
                               status=status, last_action=data.get('last_action', ''),
                               current_action=data.get('current_action', ''), started=started,
                               turn_started=data.get('turn_started'), ended=finished_at if status == 'done' else None,
                               waiting_since=finished_at if data.get('waiting_since') is not None else None,
                               app_session=meta.get('app_session'),
                               parent_session=parent_session,
                               last_at=activity,
                               seconds=working_seconds(data, now, status, started, data.get('last_at')),
                               context_tokens=data.get('context_tokens'), context_used=data.get('context_tokens'),
                               context_window=claude_context_window(data, model),
                               _codex_launches=list(data.get('_codex_launches', [])), _codex_pending=pending)
                if status == 'working' and now - activity > 120:
                    session['quiet'] = True
                sessions[path.stem] = session
            except OSError:
                self.unavailable()
            except (ValueError, TypeError, OverflowError, AttributeError):
                self.unsupported()
        for path in self.files('*/*/subagents/*.jsonl'):
            parent = sessions.get(path.parent.parent.name)
            if not parent:
                continue
            try:
                if now - path.stat().st_mtime > 86400:
                    continue
                seen.add(path)
                data, _ = self.summary(path, consume_claude_sidechain)
                parent['_codex_launches'].extend(data.get('_codex_launches', []))
            except OSError:
                self.unavailable()
            except (ValueError, TypeError, OverflowError, AttributeError):
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
