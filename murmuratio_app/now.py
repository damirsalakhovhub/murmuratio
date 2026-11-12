"""Session snapshots and local quota history for Claude and Codex routing."""
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time
from murmuratio_app.config import Config
from murmuratio_app.naming import name_fields
from murmuratio_app.common import active_today, local_day, pid_alive
from murmuratio_app.grouping import board_parents, board_sessions, retain_agents, link_parents as group_parents, merge_codex_sources, integration_cards
from murmuratio_app.health import assignment_health, limits_health
from murmuratio_app.history import (append_routing_history, read_routing_history, read_limit_history,
                                   limit_history_additions, replace_text)
from murmuratio_app.metrics.quota import (DEFAULT_CREDITS_PER_PERCENT, balance_at, balance_since,
                                        calculator_timeline, codex_mode, credits_expiry,
                                        manual_reset_started, quota_routing, week_forecast)
from murmuratio_app.metrics.usage import burn_by_model as calculate_burn, claude_usage_totals, credit_totals
from murmuratio_app.adapters.claude import ClaudeDesktopAdapter, ClaudeSessionsAdapter
from murmuratio_app.adapters.wrapper import CodexWrapperAdapter
from murmuratio_app.adapters.codex_board import CodexBoardAdapter, HISTORY_SECONDS
from murmuratio_app.adapters.usage import ClaudeUsageAdapter, CodexMeterAdapter
from murmuratio_app.adapters.quota_sources import ClaudeLimitsAdapter, CodexLimitsAdapter, app_server_limits
from murmuratio_app.discovery import Discovery
from murmuratio_app.adapters.shared import JsonlSummary, model_context_window
from murmuratio_app.adapters.base import read_error_reason
from murmuratio_app.adapters.registry import AgentsRegistryAdapter


LIVE_LIMITS_SECONDS = 300
REQUEST_SECONDS = 1.0       # longest a request waits for a new board state
FIRST_REQUEST_SECONDS = 5.0  # right after start there is no earlier state to show


def context_signal(agent, threshold):
    """Normalize the current context once, after all provider observations are merged."""
    used, window = agent.get('context_used'), agent.get('context_window')
    known = (type(used) in (int, float) and math.isfinite(used) and used >= 0
             and type(window) in (int, float) and math.isfinite(window) and window > 0)
    percent = min(100, used / window * 100) if known else None
    finished = agent.get('completed') or agent.get('status') in ('done', 'crashed')
    level = ('finished' if finished else 'high') if percent is not None and percent >= threshold else (
        'mid' if percent is not None and percent >= 50 else 'low')
    return {'context_percent': int(percent + .5) if percent is not None else None,
            'context_level': level if known else None,
            'context_high': bool(percent is not None and percent >= threshold
                                 and not finished)}


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
        self.refresh_failed = False
        self.refresh_lock = threading.Lock()
        self.refreshing = None  # Event of the running off-request refresh
        self.usage_lock = threading.Lock()
        self.usage_cache = {}
        self.usage_running = set()
        paths = Discovery.at(self.home, self.config, codex_home)
        self.discovery = paths
        if self.codex is None:
            from murmuratio_app.adapters.codex_sessions import CodexSource
            self.codex = CodexSource(paths.codex_home)
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
        self.registry_adapter = AgentsRegistryAdapter(paths.agents_registry)
        self.integration_registry_adapter = AgentsRegistryAdapter(paths.integration_registry)
        self.integration_registry_adapter.name = 'integration_registry'
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

    def _background_usage(self, name, key, now, collect, interval=None):
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
            interval = interval or (600 if cached and cached['status'] == 'available' else 30)
            if (name not in self.usage_running and
                    (not cached or cached['key'] != key or now - cached['at'] >= interval)):
                self.usage_running.add(name)
                threading.Thread(target=refresh, name='murmuratio-' + name, daemon=True).start()
            return cached if cached and cached['key'] == key else None

    def _collect_codex_usage(self, now, credits, burn):
        result = self.codex_meter_adapter.read(now, credits, burn)
        if result.status in ('absent', 'unavailable') and not result.observations:
            return result.status, None
        value, readings, points = credit_totals(result.values(), credits, now)
        return result.status, (value, readings, points)

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
            return {key: agent.get(key) for key in ('context_tokens', 'context_window')
                    if agent.get(key) is not None or key == 'context_tokens' and agent.get('_context_invalidated')}
        if not thread or not self.codex:
            return {}
        # This is a lookup for one wrapper thread, not the board-wide rollout
        # source. A damaged archived rollout must not mark all sessions unhealthy.
        result = self.codex.read('context', thread, self.summary, seen, self.cache)
        if result.status != 'available':
            return {'context_tokens': None}
        rows = result.values()
        return rows[0] if rows else {}

    def codex_cwd(self, thread):
        if thread in self.board_by_thread and self.board_by_thread[thread].get('cwd'):
            return self.board_by_thread[thread]['cwd']
        if not thread or not self.codex:
            return None
        rows = self.codex.read('cwd', thread).values()
        return rows[0]['cwd'] if rows else None

    def burn_by_model(self, now):
        return calculate_burn(getattr(self, 'forecast', {}) or {}, getattr(self, 'claude_models', {}),
                              getattr(self, 'codex_readings', []))

    def desktop_sessions(self):
        return {row['cli_session_id']: row for row in self._read_adapter(self.desktop_adapter)}

    def run(self, path, now, seen=None):
        return self.wrapper_adapter.run(path, now, seen)

    def has_codex_rollout(self, thread, run_id):
        return ((isinstance(thread, str) and thread in self.board_by_thread)
                or any(agent.get('_run_id') == run_id for agent in self.board_by_thread.values()))

    def claude_limits(self):
        return self._read_adapter(self.claude_limits_adapter)

    def codex_limits(self):
        return self._read_adapter(self.codex_limits_adapter)

    def _collect_codex_live(self):
        command = self.config.codex_cli or shutil.which('codex', path=os.pathsep.join(
            [os.environ.get('PATH', ''), '/opt/homebrew/bin', '/usr/local/bin']))
        if not command:
            return 'absent', None
        return 'available', app_server_limits(str(command), clock=self.clock)

    def routing(self, limits, now, credits=None, ages=None, write=True):
        modules = self.config.modules
        path = self.config.limits_history
        history, prune = self._isolate('quota_history', ([], False), lambda: read_limit_history(path, now)) if path else ([], False)
        self.limit_history = history
        # History gets only fresh samples: a stale one would be stamped with the wrong time.
        fresh = {tool: windows for tool, windows in limits.items() if tool not in (ages or {})}
        additions = limit_history_additions(fresh, history, now) if modules['forecast'] or modules['routing'] else []
        history.extend(additions)
        credit_pace = (credits.get('spent_per_day') or 0) / DEFAULT_CREDITS_PER_PERCENT if credits else None
        self.forecast = {tool: week_forecast(tool, next((w for w in limits.get(tool, []) if w['window_minutes'] == 10080), None),
                                             history, now, None, self.config.work_hours,
                                             credit_pace if tool == 'codex' else None)
                         for tool in ('claude', 'codex')} if modules['forecast'] or modules['burn_rate'] else {}
        def save_history():
            path.parent.mkdir(parents=True, exist_ok=True)
            if prune:
                replace_text(path, ''.join(json.dumps(row, allow_nan=False) + '\n' for row in history))
            elif additions:
                with path.open('a') as stream:
                    stream.writelines(json.dumps(row, allow_nan=False) + '\n' for row in additions)
        if path and (modules['forecast'] or modules['routing']) and self.source_statuses.get('quota_history') == 'available':
            self._isolate('quota_history', None, save_history)
        for tool, age in (ages or {}).items():
            if self.forecast.get(tool):
                self.forecast[tool]['last_known_age_seconds'] = age
        if not modules['routing']:
            return None
        routing = quota_routing(limits, history, now, credits, self.config.work_hours[1] - self.config.work_hours[0],
                                credits_expiry(now, *self.config.credits_expire),
                                self.config.codex_resets, work_hours=self.config.work_hours, policy=self.config.routing_policy)
        for tool, age in (ages or {}).items():
            if routing[tool]['used_percent'] is not None:
                routing[tool]['last_known_age_seconds'] = age
        if write:
            target = self.config.routing_file
            target.parent.mkdir(parents=True, exist_ok=True)
            replace_text(target, json.dumps(routing, indent=2, allow_nan=False) + '\n')
            self._isolate('routing_history', None, lambda: append_routing_history(self.routing_history_path(), routing))
        return routing

    def routing_history_path(self):
        path = self.config.limits_history
        return path.parent / 'routing-history.jsonl' if path else None

    def snapshot(self):
        """The board state for a request, never waiting longer than REQUEST_SECONDS.

        The state is computed off the request thread. A request waits a moment for a fresh one;
        if that takes longer (a slow disk holds the process), it gets the last
        complete state marked stale. None means no state has been computed yet."""
        with self.refresh_lock:
            done = self.refreshing
            if done is None:
                done = self.refreshing = threading.Event()
                threading.Thread(target=self._refresh, args=(done,), name='murmuratio-board', daemon=True).start()
        done.wait(REQUEST_SECONDS if self.last_snapshot is not None else FIRST_REQUEST_SECONDS)
        snapshot, finished = self.last_snapshot, self.last_snapshot_at
        if snapshot is None:
            return None
        snapshot = dict(snapshot, refresh_failed=self.refresh_failed)
        if done.is_set():
            return snapshot
        return dict(snapshot, stale=True, stale_seconds=round((time.monotonic_ns() - finished) / 1e9, 1))

    def _refresh(self, done):
        try:
            self.state()
        except Exception:
            self.refresh_failed = True  # retain the last complete state and expose the failure
        finally:
            with self.refresh_lock:
                self.refreshing = None
            done.set()

    def state(self):
        requested_at = time.monotonic_ns()
        with self.lock:
            if self.last_snapshot is not None and self.last_snapshot_at >= requested_at:
                return self.last_snapshot
            now, agents, completed, seen = self.clock(), [], [], set()
            self.source_statuses = {}
            self.source_reasons = {}
            registry = self._isolate('agents_registry', {}, self.registry)
            rollouts = self._isolate('codex_sessions', [], lambda: self._read_adapter(self.codex_board_adapter, now, registry))
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
            linked = self.link_parents(integration_cards(agents + completed, registry, now, self.project), desktop, registry)
            for agent in linked:
                agent.pop('has_eligible_children', None)
                agent['context_window'] = model_context_window(agent, agent.get('model'))
                agent.update(context_signal(agent, self.config.context_high_percent))
                agent['model'] = agent.get('model') or None
                agent['effort'] = agent.get('effort') or None
                if agent.get('title_source') != 'launcher':
                    agent.update(name_fields(titles=[(agent.get('title_source'), agent.get('task'))], project=agent.get('project', '')))
            completed = [a for a in linked if a.get('completed') and (a['kind'] != 'codex'
                         or now - (a.get('ended') or a.get('last_at') or 0) < HISTORY_SECONDS)]
            agents = retain_agents([a for a in linked if not a.get('completed')], now)
            retained = agents + completed
            retained_ids = {a['id'] for a in retained}
            children = retained + [a for a in linked if a['id'] not in retained_ids
                                   and (a.get('completed') or a['status'] in ('done', 'crashed'))
                                   and active_today(a.get('ended') or a.get('last_at'), now)]
            parents = board_parents(children, now, linked)
            # A chat can wait longer than an ordinary worker while its children remain on the board.
            retained.extend(a for a in linked if a['id'] in parents and a['id'] not in retained_ids
                            and not any(a.get(key) for key in ('parent_id', 'parent_session', 'parent_app_session')))
            visible = board_sessions(retained, now, children)
            for agent in visible:
                if agent['id'] in parents and not any(agent.get(key) for key in ('parent_id', 'parent_session', 'parent_app_session')):
                    agent['has_eligible_children'] = True
            agents = [a for a in visible if not a.get('completed')]
            completed = [a for a in visible if a.get('completed')]
            retained_ids = {a['id'] for a in agents + completed}
            completed = [a for a in completed if not a.get('subagent_id') or a.get('parent_id') in retained_ids]
            agents.sort(key=lambda a: (a['status'] != 'working', -(a['last_at'] or 0)))
            completed.sort(key=lambda a: -(a['ended'] or 0))
            live = None
            if self.config.codex_live_limits:
                # Ask every five minutes, or at the next known reset. An old cached
                # percentage must not hide the new week until the normal poll is due.
                interval = LIVE_LIMITS_SECONDS
                with self.usage_lock:
                    previous = self.usage_cache.get('codex_live_limits')
                if previous and previous['value']:
                    resets = [w['resets_at'] for w in previous['value'][1] if w.get('resets_at') is not None]
                    if resets:
                        due = min(resets) - previous['at']
                        interval = max(1, min(interval, due)) if due > 0 else 30  # retry a delayed provider reset
                snapshot = self._background_usage('codex_live_limits', 'live', now, self._collect_codex_live, interval)
                if snapshot:
                    self.source_statuses['codex_live_limits'] = snapshot['status']
                    live = snapshot['value']
                    if live is not None:
                        self.codex_limits_adapter.live = live[:2]
            limits = {'codex': self._isolate('codex_limits', [], self.codex_limits),
                      'claude': self._isolate('claude_limits', [], self.claude_limits)}
            modules = self.config.modules
            credits, self.codex_readings, credit_points = None, [], []
            if modules['codex_credits'] or modules['burn_rate']:
                include_credits, include_burn = modules['codex_credits'], modules['burn_rate']
                snapshot = self._background_usage('codex_meter', (include_credits, include_burn), now,
                    lambda: self._collect_codex_usage(now, include_credits, include_burn))
                if snapshot:
                    self.source_statuses['codex_meter'] = snapshot['status']
                    if snapshot['value'] is not None:
                        credits, self.codex_readings, credit_points = snapshot['value']
            if not modules['codex_credits']:
                credits = None
            elif credits is not None:
                credits = dict(credits)  # the scan's cached value stays unchanged
            if modules['codex_credits'] and live and live[2] is not None and live[0] > ((credits or {}).get('at') or 0):
                # A newer balance from the live read; the spend rate stays the scan's.
                credits = {**(credits or {'per_hour': None, 'sample_credits': 0.0, 'sample_hours': 0.0}),
                           'balance': live[2], 'at': live[0]}
            self.forecast = {}
            # A stale sample stays as the last known value, marked with its age; only a window
            # past its reset is dropped, because its percentage no longer applies.
            routing_limits = {kind: [w for w in windows if w['resets_at'] is None or w['resets_at'] > now]
                              for kind, windows in limits.items()}
            ages = {kind: getattr(self, kind + '_limits_adapter').age_seconds
                    for kind in limits if getattr(self, kind + '_limits_adapter').stale}
            codex_reset = next((w['resets_at'] for w in routing_limits['codex'] if w['window_minutes'] == 10080), None)
            # Only debits observed within this week belong to its plan. A gap across
            # the reset cannot establish which week paid for the balance change.
            week_start = balance_since(credit_points, codex_reset - 7 * 86400) if credits and codex_reset is not None else None
            if week_start is not None:
                credits['week_start_balance'] = week_start  # credits spent this week count against its share
            # Right after start the credit scan and the live Codex read are still running: routing from
            # those partial inputs is shown but not written, so orchestrators keep the previous file.
            with self.usage_lock:
                warming = ((modules['codex_credits'] and 'codex_meter' not in self.usage_cache)
                           or (self.config.codex_live_limits and 'codex_live_limits' not in self.usage_cache))
            routing = self._isolate('quota_history', None, lambda: self.routing(routing_limits, now, credits, ages, not warming)) if modules['forecast'] or modules['routing'] or modules['burn_rate'] else None
            month = (routing or {}).get('codex', {}).get('credits')
            if credits and month:
                credits.update(month_left=month['month_left'], month_ends_at=month['month_ends_at'],
                               week_share_left=month.get('week_share_left'), credits_plan=month.get('credits_plan'))
                if 'reserved_until' in month:
                    credits['reserved_until'] = month['reserved_until']
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
            state = {'agents': agents, 'completed': completed, 'now': now, 'local_day': local_day(now), 'limits': limits, 'build': web_build(), 'refresh_failed': False, 'snapshot_at': now}
            if modules['routing']:
                state['routing'] = routing
                if routing:
                    state['health'] = dict(
                        limits=limits_health(routing, forecast),
                        assignments=assignment_health(agents + completed, self._isolate(
                            'routing_history', [], lambda: read_routing_history(self.routing_history_path())), now))
            if modules['forecast']:
                state['forecast'] = forecast
            if modules['burn_rate']:
                state['burn'] = self.burn_by_model(now)
            if modules['codex_credits']:
                state['credits'] = credits
            week = next((w for w in routing_limits['codex'] if w['window_minutes'] == 10080), {})
            mode = (routing or {}).get('codex_mode')
            history = getattr(self, 'limit_history', []) if any(modules[k] for k in ('forecast', 'routing', 'burn_rate')) else []
            if mode is None:
                projection = week_forecast('codex', week, history, now, work_hours=self.config.work_hours)
                mode = codex_mode(projection, self.config.codex_resets, now, work_hours=self.config.work_hours, policy=self.config.routing_policy)
            else:
                history = getattr(self, 'limit_history', [])
            reset = week.get('resets_at')
            used_reset = bool(reset) and manual_reset_started(history, reset)
            left = [at for at in self.config.codex_resets if at > now]
            balance = (credits or {}).get('balance')
            spend_from = self.config.routing_policy.spend_from(now)
            start_balance = balance_at(credit_points, spend_from) if balance is not None and spend_from and credit_points else balance
            state['calculator'] = dict(used_percent=week.get('used_percent'), next_reset=week.get('resets_at'),
                                     remaining_percent=100 - week['used_percent'] if week else None,
                                     extra_resets=left,
                                     resets_total=max(len(self.config.codex_resets), len(left) + used_reset),
                                     credits_balance=balance,
                                     credits_start_balance=max(start_balance, balance) if balance is not None else None,
                                     timeline=calculator_timeline(reset, mode.get('reset_deadlines', []),
                                                                  mode.get('reset_plan', []), now, used_reset),
                                     credits_per_percent=DEFAULT_CREDITS_PER_PERCENT,
                                     credits_expire_at=credits_expiry(now, *self.config.credits_expire),
                                     mode=mode)
            issues = {name: {'status': status} for name, status in self.source_statuses.items() if status != 'available'}
            for name, issue in issues.items():
                if self.source_reasons.get(name):
                    issue['reason'] = self.source_reasons[name]
            for adapter in (self.claude_limits_adapter, self.codex_limits_adapter):
                if adapter.name in issues:
                    issues[adapter.name]['age_seconds'] = adapter.age_seconds
                    if adapter.stale and issues[adapter.name]['status'] != 'stale':
                        issues[adapter.name]['stale'] = True
            if self.registry_adapter.malformed and self.source_statuses.get('agents_registry') == 'available':
                issues['agents_registry'] = {'status': 'available', 'malformed': self.registry_adapter.malformed}
            if issues:
                state['sources'] = issues
            self.refresh_failed = False
            self.last_snapshot = self.sanitize(state)
            self.last_snapshot_at = time.monotonic_ns()
            return self.last_snapshot

    def registry(self):
        """Registry id to its latest record; empty when the file does not exist yet."""
        legacy = {record['id']: record for record in self._read_adapter(self.registry_adapter)}
        owned = self.integration_registry_adapter.read()
        if owned.status not in ('available', 'absent'):
            self.source_statuses['integration_registry'] = owned.status
            self.source_reasons['integration_registry'] = owned.reason
        if owned.status == 'available' and self.source_statuses.get('agents_registry') == 'absent':
            self.source_statuses['agents_registry'] = 'available'
        legacy.update({'integration:' + r['id']: r for r in owned.values()})
        return legacy

    def link_parents(self, agents, desktop, registry=None):
        config = self.config
        columns = [*config.project_order, *(name for _, name, _ in config.project_mappings),
                   *(name for _, name, _ in config.project_fallback_mappings), config.fallback_project]
        return group_parents(agents, desktop, self.clock, registry if registry is not None else {},
                             config.mark_guessed, columns)
