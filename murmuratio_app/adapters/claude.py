"""Adapters for Claude Desktop metadata and Claude Code transcripts."""
import json
from pathlib import Path
from murmuratio_app.adapters.base import Adapter, UnavailableSource
from murmuratio_app.adapters.parsing import consume_claude
from murmuratio_app.adapters.shared import claude_context_window, claude_label, claude_status
from murmuratio_app.common import one_line


class ClaudeDesktopAdapter(Adapter):
    name = 'claude_desktop'

    def __init__(self, root):
        super().__init__()
        self.root = Path(root)
        self.cache = {}

    def collect(self):
        if not self.root.is_dir():
            raise UnavailableSource()
        seen = set()
        for path in self.root.glob('*/*/local_*.json'):
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

    def collect(self, now, desktop, seen):
        if not self.root.is_dir():
            raise UnavailableSource()
        for path in self.root.glob('*/*.jsonl'):
            try:
                modified = path.stat().st_mtime
                meta = desktop.get(path.stem, {})
                if not meta and now - modified > 259200:
                    continue
                if meta.get('archived') or meta.get('scheduled_task_id') is not None:
                    continue
                seen.add(path)
                data, modified = self.summary(path, consume_claude)
                cached = self.cache.get(path)
                if cached and (cached.malformed or cached.record_types and not cached.record_types.intersection({'user', 'assistant'})):
                    self.unsupported()
                parent_session = meta.get('parent_session', '')
                spawned = bool(parent_session)
                status = claude_status(modified, data.get('last_turn'), now, spawned)
                if not status and meta and data.get('last_turn') == 'end_turn':
                    status = 'done' if spawned else 'waiting'
                approval = not status and meta and data.get('last_turn') == 'tool_use' and 600 < now - modified < 86400
                if approval:
                    status = 'blocked'
                if not status:
                    continue
                cwd = data.get('cwd') or meta.get('cwd') or ''
                started = data.get('started')
                end = data.get('last_at') if status == 'done' else now
                model = data.get('model') or one_line(meta.get('model'))
                session = dict(id='claude:' + path.stem, kind='claude', who=claude_label(model),
                               model=model, cwd=cwd, folder=Path(cwd).name or '—', project=self.project(cwd),
                               task=one_line(meta.get('title')) or data.get('first_user') or 'Untitled',
                               title_source='desktop' if one_line(meta.get('title')) else 'first_user',
                               status=status, last_action=data.get('last_action', ''),
                               current_action='waiting for approval' if approval else data.get('current_action', ''), started=started,
                               turn_started=data.get('turn_started'), ended=data.get('last_at') if status == 'done' else None,
                               waiting_since=data.get('waiting_since'), app_session=meta.get('app_session'),
                               parent_session=parent_session,
                               last_at=data.get('last_at') or modified,
                               seconds=max(0, (end or now) - started) if started is not None else None,
                               context_tokens=data.get('context_tokens'), context_used=data.get('context_tokens'),
                               context_window=claude_context_window(data, model))
                # Status and some context fallbacks are inferred from saved events.
                yield self.observation(session, data.get('last_at') or modified, 'inferred')
            except OSError:
                self.unavailable()
            except (ValueError, TypeError, OverflowError, AttributeError):
                self.unsupported()
