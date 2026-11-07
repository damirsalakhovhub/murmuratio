"""Session snapshots and local quota history for Claude and Codex routing."""
import json
from pathlib import Path
import threading
import time
from murmuratio_app.config import Config
from murmuratio_app.common import local_day, pid_alive
from murmuratio_app.grouping import retain_agents, link_parents as group_parents, merge_codex_sources
from murmuratio_app.history import read_limit_history, limit_history_additions, replace_text
from murmuratio_app.metrics.quota import quota_routing, week_forecast
from murmuratio_app.metrics.usage import burn_by_model as calculate_burn, claude_usage_totals, credit_totals
from murmuratio_app.adapters.claude import ClaudeDesktopAdapter, ClaudeSessionsAdapter
from murmuratio_app.adapters.wrapper import CodexWrapperAdapter
from murmuratio_app.adapters.codex_board import CodexBoardAdapter, HISTORY_SECONDS
from murmuratio_app.adapters.usage import ClaudeUsageAdapter, CodexMeterAdapter
from murmuratio_app.adapters.quota_sources import ClaudeLimitsAdapter, CodexLimitsAdapter
from murmuratio_app.discovery import Discovery
from murmuratio_app.adapters.shared import JsonlSummary
from murmuratio_app.adapters.base import read_error_reason


def web_build():
    """Changes whenever a page file changes, so open pages reload themselves."""
    web = Path(__file__).resolve().parent.parent / 'web'
    try:
        return str(max(path.stat().st_mtime_ns for path in web.iterdir() if path.is_file()))
    except (OSError, ValueError):
        return ''


class NowSource:
    def __init__(self, home=None, sanitize=lambda value: value, codex=None, clock=time.time, alive=pid_alive, config=None, codex_home=None):
        self.home = Path(home) if home is not None else Path.home()
        self.config = config if config is not None else Config.load(self.home)
        self.sanitize, self.codex, self.clock, self.alive = sanitize, codex, clock, alive
        self.cache = {}
        self.lock = threading.Lock()
        self.last_snapshot = None
        self.last_snapshot_at = 0
        self.usage_lock = threading.Lock()
        self.usage_cache = {}
        self.usage_running = set()
        paths = Discovery.at(self.home, self.config, codex_home)
        self.discovery = paths
        self.desktop_adapter = ClaudeDesktopAdapter(paths.claude_desktop)
        self.claude_adapter = ClaudeSessionsAdapter(paths.claude_sessions, self.summary, self.cache, self.project)
        self.wrapper_adapter = CodexWrapperAdapter(paths.codex_wrapper, self.summary, self.cache, self.codex_cwd,
                                                    self.codex_context, self.project, self.alive,
                                                    runner=lambda *args: self.run(*args),
                                                    has_rollout=self.has_codex_rollout)
        self.codex_board_adapter = CodexBoardAdapter(paths.codex_rollouts, self.project)
        self.claude_limits_adapter = ClaudeLimitsAdapter(paths.claude_limits, paths.claude_limits_legacy, clock)
        self.codex_limits_adapter = CodexLimitsAdapter(paths.codex_rollouts, clock)
        self.claude_usage_adapter = ClaudeUsageAdapter(paths.claude_sessions)
        self.codex_meter_adapter = CodexMeterAdapter(paths.codex_rollouts)
        self.source_statuses = {}
        self.source_reasons = {}
        self.board_by_thread = {}

    def _read_adapter(self, adapter, *args):
        result = adapter.read(*args)
        if result.status != 'available' or adapter.name not in self.source_statuses:
            self.source_statuses[adapter.name] = result.status
            if result.reason:
                self.source_reasons[adapter.name] = result.reason
        return result.values()

    def _isolate(self, name, default, action):
        try:
            value = action()
            self.source_statuses.setdefault(name, 'available')
            return value
        except Exception as exc:
            # One failing source must not prevent the other sources from rendering.
            self.source_statuses[name] = 'unavailable'
            self.source_reasons[name] = read_error_reason(exc)
            return default

    def _background_usage(self, name, key, now, collect):
        """Return the last complete snapshot while one refresh runs off the request path."""
        def refresh():
            try:
                status, value = collect()
            except Exception:
                status, value = 'unavailable', None
            with self.usage_lock:
                previous = self.usage_cache.get(name)
                if value is None and previous and previous['key'] == key:
                    value = previous['value']
                self.usage_cache[name] = {'key': key, 'at': now, 'status': status, 'value': value}
                self.usage_running.discard(name)

        with self.usage_lock:
            cached = self.usage_cache.get(name)
            interval = 600 if cached and cached['status'] == 'available' else 30
            if (name not in self.usage_running and
                    (not cached or cached['key'] != key or now - cached['at'] >= interval)):
                self.usage_running.add(name)
                threading.Thread(target=refresh, name='inspector-' + name, daemon=True).start()
            return cached if cached and cached['key'] == key else None

    def _collect_codex_usage(self, now, credits, burn):
        result = self.codex_meter_adapter.read(now, credits, burn)
        if result.status in ('absent', 'unavailable') and not result.observations:
            return result.status, None
        value, readings = credit_totals(result.values(), credits)
        return result.status, (value, readings)

    def _collect_claude_usage(self, since, desktop, split, burn):
        result = self.claude_usage_adapter.read(since, desktop, burn)
        if result.status in ('absent', 'unavailable') and not result.observations:
            return result.status, None
        return result.status, claude_usage_totals(result.values(), since, split)

    def project(self, cwd):
        path = self.home if cwd == '~' else self.home / cwd[2:] if cwd.startswith('~/') else Path(cwd)
        def match(mappings):
            for prefix, name, worktrees in mappings:
                if (path == prefix or prefix in path.parents
                        or (worktrees and str(path).startswith(str(prefix) + '-worktrees/'))):
                    return name
            return None
        explicit = match(self.config.project_mappings)
        if explicit:
            return explicit
        if any(part in path.parts for part in self.config.inherit_folders):
            return ''
        return match(self.config.project_fallback_mappings) or path.name or self.config.fallback_project

    def summary(self, path, consume):
        return self.cache.setdefault(path, JsonlSummary()).refresh(path, consume)

    def codex_context(self, thread, seen=None):
        """Read the latest normalized per-turn context for a Codex thread."""
        if thread in self.board_by_thread:
            agent = self.board_by_thread[thread]
            return {key: agent[key] for key in ('context_tokens', 'context_window') if agent.get(key) is not None}
        if not thread or not self.codex:
            return {}
        rows = self._read_adapter(self.codex, 'context', thread, self.summary, seen, self.cache)
        return rows[0] if rows else {}

    def codex_cwd(self, thread):
        if thread in self.board_by_thread and self.board_by_thread[thread].get('cwd'):
            return self.board_by_thread[thread]['cwd']
        if not thread or not self.codex:
            return None
        rows = self._read_adapter(self.codex, 'cwd', thread)
        return rows[0]['cwd'] if rows else None

    def burn_by_model(self, now):
        return calculate_burn(getattr(self, 'forecast', {}) or {}, getattr(self, 'claude_models', {}),
                              getattr(self, 'codex_readings', []))

    def desktop_sessions(self):
        return {row['cli_session_id']: row for row in self._read_adapter(self.desktop_adapter)}

    def codex_titles(self):
        try:
            return self.sanitize(self.wrapper_adapter.titles()) if self.sanitize else self.wrapper_adapter.titles()
        except Exception:
            return {}

    def run(self, path, now, seen=None):
        return self.wrapper_adapter.run(path, now, seen)

    def has_codex_rollout(self, thread, run_id):
        return ((isinstance(thread, str) and thread in self.board_by_thread)
                or any(agent.get('_run_id') == run_id for agent in self.board_by_thread.values()))

    def claude_limits(self):
        return self._read_adapter(self.claude_limits_adapter)

    def codex_limits(self):
        return self._read_adapter(self.codex_limits_adapter)

    def routing(self, limits, now, credits=None):
        modules = self.config.modules
        path = self.config.limits_history
        history, prune = read_limit_history(path, now) if modules['forecast'] or modules['routing'] else ([], False)
        additions = limit_history_additions(limits, history, now) if modules['forecast'] or modules['routing'] else []
        history.extend(additions)
        self.forecast = {tool: week_forecast(tool, next((w for w in limits.get(tool, []) if w['window_minutes'] == 10080), None),
                                             history, now) for tool in ('claude', 'codex')} if modules['forecast'] or modules['burn_rate'] else {}
        if modules['forecast'] or modules['routing']:
            path.parent.mkdir(parents=True, exist_ok=True)
            if prune:
                replace_text(path, ''.join(json.dumps(row, allow_nan=False) + '\n' for row in history))
            elif additions:
                with path.open('a') as stream:
                    stream.writelines(json.dumps(row, allow_nan=False) + '\n' for row in additions)
        if not modules['routing']:
            return None
        routing = quota_routing(limits, history, now, credits, self.config.work_hours[1] - self.config.work_hours[0])
        target = self.config.routing_file
        target.parent.mkdir(parents=True, exist_ok=True)
        replace_text(target, json.dumps(routing, indent=2, allow_nan=False) + '\n')
        return routing

    def state(self):
        requested_at = time.monotonic_ns()
        with self.lock:
            if self.last_snapshot is not None and self.last_snapshot_at >= requested_at:
                return self.last_snapshot
            now, agents, completed, seen = self.clock(), [], [], set()
            self.source_statuses = {}
            self.source_reasons = {}
            rollouts = self._isolate('codex_sessions', [], lambda: self._read_adapter(self.codex_board_adapter, now))
            self.board_by_thread = {}
            for agent in rollouts:
                previous = self.board_by_thread.get(agent['thread'])
                if previous is None or (agent.get('last_at') or 0) > (previous.get('last_at') or 0):
                    self.board_by_thread[agent['thread']] = agent
            runs = []
            if self.config.modules['codex_wrapper']:
                runs = self._isolate('codex_wrapper', [], lambda: self._read_adapter(self.wrapper_adapter, now, seen))
            for agent in merge_codex_sources(runs, rollouts):
                (completed if agent['completed'] else agents).append(agent)
            desktop = self._isolate('claude_desktop', {}, self.desktop_sessions)
            active_codex = [agent for agent in agents if agent['kind'] == 'codex' and agent['status'] in ('working', 'hung')]
            claude = self._isolate('claude_sessions', [], lambda: self._read_adapter(self.claude_adapter, now, desktop, seen, active_codex))
            agents.extend(claude)
            for path in set(self.cache) - seen:
                del self.cache[path]
            linked = self.link_parents(agents + completed, desktop)
            hidden_chats = [a for a in linked if a.get('codex_role') == 'chat' and not self.config.show_codex_chats]
            hidden_running = sum(not a.get('completed') and a['status'] in ('working', 'hung') for a in hidden_chats)
            linked = [a for a in linked if a.get('codex_role') != 'chat' or self.config.show_codex_chats]
            hidden_ids = {a['id'] for a in hidden_chats}
            for agent in linked:
                if agent.get('parent_id') in hidden_ids:
                    agent['parent_id'] = None
                    agent.pop('link', None)
            agents = retain_agents([a for a in linked if not a.get('completed')], now)
            completed = [a for a in linked if a.get('completed') and (a['kind'] != 'codex'
                         or now - (a.get('ended') or a.get('last_at') or 0) < HISTORY_SECONDS)]
            agents.sort(key=lambda a: (a['status'] != 'working', -(a['last_at'] or 0)))
            completed.sort(key=lambda a: -(a['ended'] or 0))
            limits = {'codex': self._isolate('codex_limits', [], self.codex_limits),
                      'claude': self._isolate('claude_limits', [], self.claude_limits)}
            modules = self.config.modules
            credits, self.codex_readings = None, []
            if modules['codex_credits'] or modules['burn_rate']:
                include_credits, include_burn = modules['codex_credits'], modules['burn_rate']
                snapshot = self._background_usage('codex_meter', (include_credits, include_burn), now,
                    lambda: self._collect_codex_usage(now, include_credits, include_burn))
                if snapshot:
                    self.source_statuses['codex_meter'] = snapshot['status']
                    if snapshot['value'] is not None:
                        credits, self.codex_readings = snapshot['value']
            if not modules['codex_credits']:
                credits = None
            self.forecast = {}
            routing_limits = {kind: [] if getattr(self, kind + '_limits_adapter').stale else windows
                              for kind, windows in limits.items()}
            routing = self._isolate('quota_history', None, lambda: self.routing(routing_limits, now, credits)) if modules['forecast'] or modules['routing'] or modules['burn_rate'] else None
            forecast = getattr(self, 'forecast', {})
            codex_week = forecast.get('codex')
            if codex_week and modules['codex_credits']:
                codex_week['credits'] = credits
            claude_week = forecast.get('claude')
            self.claude_models = {}
            include_split = bool(modules['scenarios'] and modules['forecast'])
            if claude_week and (include_split or modules['burn_rate']):
                since, include_burn = claude_week['start'], modules['burn_rate']
                snapshot = self._background_usage('claude_usage', (since, include_split, include_burn), now,
                    lambda: self._collect_claude_usage(since, desktop, include_split, include_burn))
                if snapshot:
                    self.source_statuses['claude_usage'] = snapshot['status']
                    if snapshot['value'] is not None:
                        worker_share, self.claude_models = snapshot['value']
                        if include_split:
                            claude_week['worker_share'] = worker_share
            state = {'agents': agents, 'completed': completed, 'now': now, 'local_day': local_day(now), 'limits': limits, 'build': web_build(),
                     'codex_chats': {'hidden': len(hidden_chats), 'running': hidden_running}}
            if modules['routing']:
                state['routing'] = routing
            if modules['forecast']:
                state['forecast'] = forecast
            if modules['burn_rate']:
                state['burn'] = self.burn_by_model(now)
            if modules['codex_credits']:
                state['credits'] = credits
            issues = {name: {'status': status} for name, status in self.source_statuses.items() if status != 'available'}
            for name, issue in issues.items():
                if self.source_reasons.get(name):
                    issue['reason'] = self.source_reasons[name]
            if 'claude_limits' in issues:
                issues['claude_limits']['age_seconds'] = self.claude_limits_adapter.age_seconds
                if self.claude_limits_adapter.stale and issues['claude_limits']['status'] != 'stale':
                    issues['claude_limits']['stale'] = True
            if 'codex_limits' in issues:
                issues['codex_limits']['age_seconds'] = self.codex_limits_adapter.age_seconds
                if self.codex_limits_adapter.stale and issues['codex_limits']['status'] != 'stale':
                    issues['codex_limits']['stale'] = True
            if issues:
                state['sources'] = issues
            self.last_snapshot = self.sanitize(state)
            self.last_snapshot_at = time.monotonic_ns()
            return self.last_snapshot

    def link_parents(self, agents, desktop):
        return group_parents(agents, desktop, self.clock)
