"""Optional adapter for local Codex wrapper run files."""
import json
from pathlib import Path
from murmuratio_app.adapters.base import Adapter, UnavailableSource
from murmuratio_app.adapters.activity import working_seconds
from murmuratio_app.adapters.parsing import consume_codex
from murmuratio_app.adapters.shared import CODEX_DEAD_SECONDS, codex_status
from murmuratio_app.common import active_today, one_line, timestamp


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

    def run(self, path, now, seen=None):
        run = json.loads(path.read_text())
        if not isinstance(run, dict) or run.get('status') not in ('running', 'done'):
            return None
        started, ended = timestamp(run.get('started')), timestamp(run.get('ended'))
        finished_at = ended or path.stat().st_mtime
        run_id = one_line(run.get('run_id')) or path.stem
        old = run['status'] == 'done' and not active_today(finished_at, now)
        if old and now - finished_at > 86400 and not self.has_rollout(run.get('thread'), run_id):
            return None
        data, last_at = {}, None
        if isinstance(run.get('events'), str) and run['events']:
            try:
                events = Path(run['events'])
                data, last_at = self.summary(events, consume_codex)
                if self.summary_cache[events].malformed:
                    self.unsupported()
            except OSError:
                self.unavailable()
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
        model = one_line(run.get('model'))
        context = self.codex_context(thread, seen)
        who = 'Codex' + (' ' + tier if tier in ('sol', 'terra', 'luna') else (' ' + model if model else ''))
        last_at = data.get('last_at') or last_at or ended or started
        status = codex_status(run, now, last_at, self.alive(run['pid']) if type(run.get('pid')) is int and run['pid'] > 0 else None)
        dead_expired = (run['status'] == 'running' and status == 'crashed' and last_at is not None
                        and now - last_at > CODEX_DEAD_SECONDS)
        completed = run['status'] == 'done' or dead_expired
        if completed and ended is None:
            ended = last_at or path.stat().st_mtime
        return dict(id='codex:' + path.stem, kind='codex', who=who, model=model, thread=thread,
                    cwd=cwd, folder=Path(cwd).name if cwd else '—', project=self.project(cwd),
                    task=one_line(run.get('title')) or 'Untitled',
                    status=status,
                    last_action=data.get('last_action', ''), last_at=last_at,
                    current_action=data.get('current_action', ''), started=started,
                    parent_session=one_line(run.get('parent_session')),
                    parent_app_session=one_line(run.get('parent_app_session')),
                    from_cwd=one_line(run.get('from_cwd')),
                    seconds=working_seconds(data, now, status, started, last_at),
                    context_tokens=data.get('context_tokens'), completed=completed,
                    context_used=context.get('context_tokens', data.get('context_tokens')),
                    context_window=context.get('context_window'),
                    ended=ended, verdict=one_line(run.get('verdict')), head=one_line(run.get('head')),
                    _wrapper=True, _pid_known=type(run.get('pid')) is int and run['pid'] > 0,
                    _dead_expired=dead_expired,
                    _run_id=run_id)

    def collect(self, now, seen):
        if not self.root.is_dir():
            raise UnavailableSource()
        for path in self.root.glob('*.json'):
            try:
                raw = json.loads(path.read_text())
                if not isinstance(raw, dict) or raw.get('status') not in ('running', 'done'):
                    self.unsupported()
                    continue
                agent = self.runner(path, now, seen)
                if agent:
                    yield self.observation(agent, agent.get('last_at') or path.stat().st_mtime, 'inferred')
                if isinstance(raw.get('events'), str):
                    seen.add(Path(raw['events']))
            except OSError:
                self.unavailable()
            except (ValueError, TypeError, OverflowError, AttributeError):
                self.unsupported()
