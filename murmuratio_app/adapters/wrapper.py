"""Optional adapter for local Codex wrapper run files."""
import json
from pathlib import Path
from murmuratio_app.adapters.base import Adapter
from murmuratio_app.adapters.settings import recorded_effort
from murmuratio_app.adapters.brief import brief_view
from murmuratio_app.adapters.activity import working_seconds
from murmuratio_app.adapters.parsing import consume_codex
from murmuratio_app.adapters.shared import model_context_window, CODEX_DEAD_SECONDS, codex_status
from murmuratio_app.common import active_today, one_line, timestamp
from murmuratio_app.naming import TASK_ID, name_fields

MAX_EVENT_LINE = 256 * 1024


class CodexWrapperAdapter(Adapter):
    name = 'codex_wrapper'

    def __init__(self, root, summary, summary_cache, codex_cwd, codex_context, project, alive,
                 runner=None, has_rollout=lambda thread, run_id: False):
        super().__init__()
        self.root = Path(root)
        self.summary, self.summary_cache, self.codex_cwd, self.codex_context = summary, summary_cache, codex_cwd, codex_context
        self.project, self.alive = project, alive
        self.runner = runner or self.run
        self.has_rollout = has_rollout
        self.brief_cache = {}

    def brief_text(self, run):
        path = run.get('brief')
        if not isinstance(path, str) or not path:
            return ''
        try:
            st = Path(path).stat()
            signature = (st.st_mtime_ns, st.st_size)
            cached = self.brief_cache.get(path)
            if cached is None or cached[0] != signature:
                with open(path, encoding='utf-8', errors='replace') as stream:
                    cached = (signature, stream.read(256 * 1024))
                self.brief_cache[path] = cached
            return cached[1]
        except OSError:
            return ''

    @staticmethod
    def _thread_of(path):
        try:
            run = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(run, dict):
            return None
        thread = run.get('thread') if isinstance(run.get('thread'), str) else None
        if not thread and run.get('mode') == 'resume' and isinstance(run.get('target'), str):
            thread = run['target']
        if not thread and isinstance(run.get('events'), str) and run['events']:
            try:
                with open(run['events'], encoding='utf-8', errors='replace') as handle:
                    first = json.loads(handle.readline() or '{}')
                if isinstance(first, dict) and first.get('type') == 'thread.started' and isinstance(first.get('thread_id'), str):
                    thread = first['thread_id']
            except (OSError, ValueError):
                pass
        return thread

    @staticmethod
    def claude_session(run):
        """The Claude session a claude-task run started: recorded by newer launchers, else its first event."""
        if not isinstance(run, dict) or run.get('tool') != 'claude':
            return None
        if isinstance(run.get('session_id'), str) and run['session_id']:
            return run['session_id']
        if not isinstance(run.get('events'), str) or not run['events']:
            return None
        try:
            with open(run['events'], encoding='utf-8', errors='replace') as handle:
                for _ in range(16):
                    line = handle.readline(MAX_EVENT_LINE)
                    if not line:
                        break
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and isinstance(event.get('session_id'), str) and event['session_id']:
                        return event['session_id']
        except OSError:
            pass
        return None

    def run(self, path, now, seen=None):
        run = json.loads(path.read_text())
        if not isinstance(run, dict) or run.get('status') not in ('running', 'done', 'blocked'):
            return None
        started, ended = timestamp(run.get('started')), timestamp(run.get('ended'))
        finished_at = ended or path.stat().st_mtime
        run_id = one_line(run.get('run_id')) or path.stem
        finished = run['status'] in ('done', 'blocked')
        old = finished and not active_today(finished_at, now) and now - finished_at >= 1800
        if old and now - finished_at > 86400 and not self.has_rollout(run.get('thread'), run_id):
            return None
        data, last_at = {}, None
        if isinstance(run.get('events'), str) and run['events']:
            try:
                events = Path(run['events'])
                data, last_at = self.summary(events, consume_codex)
                if self.summary_cache[events].unreadable():
                    self.unsupported()
            except FileNotFoundError:
                # Temporary event logs may be removed after the run. The saved
                # wrapper metadata is still a usable observation.
                pass
            except OSError as exc:
                self.unavailable(exc)
        thread = run.get('thread') or data.get('thread')
        if not isinstance(thread, str):
            thread = None
        if not thread and run.get('mode') == 'resume':
            thread = run.get('target')
        if old and not self.has_rollout(thread, run_id):
            return None
        cwd = run.get('cwd')
        if not cwd and isinstance(run.get('target'), str) and run['target'].startswith('/'):
            cwd = run['target']
        if not cwd and thread:
            cwd = self.codex_cwd(thread)
        cwd = cwd if isinstance(cwd, str) else one_line(run.get('from_cwd'))
        tier = run.get('tier')
        model = data['model'] if 'model' in data else one_line(run.get('model'))
        context = self.codex_context(thread, seen)
        who = 'Codex' + (' ' + tier if model and isinstance(tier, str) and bool(tier) else (' ' + model if model else ''))
        last_at = data.get('last_at') or last_at or ended or started
        status = codex_status(run, now, last_at, self.alive(run['pid']) if type(run.get('pid')) is int and run['pid'] > 0 else None)
        dead_expired = (run['status'] == 'running' and status == 'crashed' and last_at is not None
                        and now - last_at > CODEX_DEAD_SECONDS)
        completed = finished or dead_expired
        if completed and ended is None:
            ended = last_at or path.stat().st_mtime
        naming = name_fields(titles=[('wrapper', run.get('title'))])
        if naming['title_source'] is None or TASK_ID.fullmatch(naming['task']):
            naming = name_fields(titles=[('wrapper', run.get('title'))], text=self.brief_text(run))
            if naming['title_source'] is None:
                naming = name_fields(titles=[('wrapper', run.get('task_id'))])
        return dict(id='codex:' + path.stem, kind='codex', who=who, model=model or None,
                    effort=recorded_effort(data, run, keys=('model_reasoning_effort', 'reasoning_effort', 'effort')), thread=thread,
                    _effort_recorded=any(key in source for source in (data, run)
                                         for key in ('model_reasoning_effort', 'reasoning_effort', 'effort')),
                    cwd=cwd, folder=Path(cwd).name if cwd else '—', project=self.project(cwd),
                    **naming,
                    status=status, last_at=last_at,
                    current_action=data.get('current_action', ''), started=started,
                    parent_session=one_line(run.get('parent_session')),
                    parent_app_session=one_line(run.get('parent_app_session')),
                    from_cwd=one_line(run.get('from_cwd')),
                    seconds=working_seconds(data, now, status, started, last_at),
                    context_tokens=data.get('context_tokens'), completed=completed,
                    context_used=context.get('context_tokens', data.get('context_tokens')),
                    context_window=model_context_window(context if context.get('context_window') else data, model),
                    brief=brief_view(data, cwd),
                    ended=ended, verdict=one_line(run.get('verdict')), head=one_line(run.get('head')),
                    _wrapper=True, _pid_known=type(run.get('pid')) is int and run['pid'] > 0,
                    _dead_expired=dead_expired,
                    _run_id=run_id, _claude_session=self.claude_session(run))

    def collect(self, now, seen):
        found = False
        for path in self.files('*.json'):
            try:
                raw = json.loads(path.read_text())
                found = True
                if not isinstance(raw, dict) or raw.get('status') not in ('running', 'done', 'blocked'):
                    self.unsupported()
                    continue
                agent = self.runner(path, now, seen)
                if agent:
                    yield self.observation(agent, agent.get('last_at') or path.stat().st_mtime, 'inferred')
                if isinstance(raw.get('events'), str):
                    seen.add(Path(raw['events']))
            except FileNotFoundError:
                continue
            except OSError as exc:
                self.unavailable(exc)
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError, RecursionError):
                self.unsupported()
        if not found and self.status == 'available':
            self.status = 'absent'
