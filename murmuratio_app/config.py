"""Optional local configuration for Murmuratio."""
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
import tomllib

DATA = '~/.local/share/murmuratio'
MODULES = ('forecast', 'scenarios', 'routing', 'codex_credits', 'burn_rate',
           'codex_wrapper', 'branding')


@dataclass
class RoutingPolicy:
    credits_reserve_months: tuple = ()
    credits_start: tuple | None = None
    codex_tiers: dict = field(default_factory=lambda: dict(simple='default', hard='default'))
    claude_tiers: dict = field(default_factory=lambda: dict(simple='default', hard='default'))
    surplus_boost: dict = field(default_factory=dict)
    saving_boost: dict = field(default_factory=dict)
    surplus_title: str = 'Surplus: run Fast'
    surplus_effort: str | None = None
    credit_days_before_reset: int = 2

    @classmethod
    def load(cls, raw):
        policy = cls()
        months = raw.get('credits_reserve_months', [])
        if not isinstance(months, list) or any(type(v) is not int or not 1 <= v <= 12 for v in months):
            raise ValueError('routing.credits_reserve_months must contain month numbers from 1 to 12')
        policy.credits_reserve_months = tuple(months)
        start = raw.get('credits_start', '')
        if start:
            try:
                month, day = (int(part) for part in start.split('-'))
                date(2024, month, day)
            except (AttributeError, ValueError):
                raise ValueError('routing.credits_start must be "MM-DD"') from None
            policy.credits_start = (month, day)
        elif not isinstance(start, str) or months:
            raise ValueError('routing.credits_start is required when reserving credits')
        for name in ('codex_tiers', 'claude_tiers', 'surplus_boost', 'saving_boost'):
            value = raw.get(name, getattr(policy, name))
            if (not isinstance(value, dict) or any(not isinstance(k, str) or not k or
                    not isinstance(v, str) or not v for k, v in value.items())):
                raise ValueError(f'routing.{name} must map non-empty strings to strings')
            if name.endswith('_tiers') and set(value) != {'simple', 'hard'}:
                raise ValueError(f'routing.{name} requires simple and hard')
            setattr(policy, name, dict(value))
        title = raw.get('surplus_title', policy.surplus_title)
        if not isinstance(title, str) or not title:
            raise ValueError('routing.surplus_title must be a non-empty string')
        policy.surplus_title = title
        effort = raw.get('surplus_effort', '')
        if effort not in ('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise ValueError('routing.surplus_effort is invalid')
        policy.surplus_effort = effort or None
        days = raw.get('credit_days_before_reset', policy.credit_days_before_reset)
        if type(days) is not int or not 0 <= days <= 7:
            raise ValueError('routing.credit_days_before_reset must be an integer from 0 to 7')
        policy.credit_days_before_reset = days
        return policy

    def reserve_until(self, now):
        today = datetime.fromtimestamp(now)
        if today.month not in self.credits_reserve_months or self.credits_start is None:
            return None
        month, day = self.credits_start
        for year in range(today.year, today.year + 5):
            try:
                start = datetime(year, month, day).timestamp()
            except ValueError:
                continue
            if start > now:
                return start
        return None

    def spend_from(self, now):
        today = datetime.fromtimestamp(now)
        if self.credits_start is None or today.month != self.credits_start[0]:
            return None
        try:
            start = datetime(today.year, *self.credits_start).timestamp()
        except ValueError:
            return None
        return start if start <= now else None


@dataclass
class Config:
    modules: dict = field(default_factory=lambda: {name: False for name in MODULES})
    project_order: list = field(default_factory=list)
    project_mappings: list = field(default_factory=list)
    project_fallback_mappings: list = field(default_factory=list)
    inherit_folders: list = field(default_factory=list)
    fallback_project: str = 'Unknown'
    work_hours: tuple = (9, 21)
    wrapper_runs: Path | None = None
    routing_file: Path | None = None
    limits_history: Path | None = None
    credits_expire: tuple = (12, 31)
    codex_live_limits: bool = False
    codex_cli: Path | None = None
    claude_limits_extra: Path | None = None
    codex_resets: tuple = ()
    agents_registry: Path | None = None
    mark_guessed: bool = False
    context_high_percent: int = 80
    routing_policy: RoutingPolicy = field(default_factory=RoutingPolicy)

    @classmethod
    def load(cls, home, path=None):
        home = Path(home)
        path = Path(path) if path is not None else home / '.config/murmuratio/config.toml'
        try:
            with path.open('rb') as stream:
                raw = tomllib.load(stream)
        except FileNotFoundError:
            raw = {}
        modules = raw.get('modules', {})
        projects = raw.get('projects', {})
        hours = raw.get('work_hours', {})
        paths = raw.get('paths', {})
        board = raw.get('board', {})
        routing = raw.get('routing', {})
        limits = raw.get('limits', {})
        if not all(isinstance(value, dict) for value in (modules, projects, hours, paths, board, routing, limits)):
            raise ValueError('Config sections must be tables')
        mark_guessed = board.get('mark_guessed', False)
        if not isinstance(mark_guessed, bool):
            raise ValueError('board.mark_guessed must be a boolean')
        context_high_percent = board.get('context_high_percent', cls.context_high_percent)
        if type(context_high_percent) is not int or not 1 <= context_high_percent <= 100:
            raise ValueError('board.context_high_percent must be an integer from 1 to 100')
        codex_live = limits.get('codex_live', False)
        if not isinstance(codex_live, bool):
            raise ValueError('limits.codex_live must be a boolean')
        for name in MODULES:
            if name in modules and not isinstance(modules[name], bool):
                raise ValueError(f'modules.{name} must be a boolean')
        enabled = {name: modules.get(name, False) for name in MODULES}
        order, inherit = projects.get('order', []), projects.get('inherit_folders', [])
        mappings = projects.get('mappings', [])
        fallback = projects.get('fallback', 'Unknown')
        if (not isinstance(order, list) or not all(isinstance(v, str) for v in order)
                or not isinstance(inherit, list) or not all(isinstance(v, str) for v in inherit)
                or not isinstance(mappings, list) or not isinstance(fallback, str) or not fallback):
            raise ValueError('Invalid project settings')
        def local_path(value):
            if not isinstance(value, str) or not value:
                raise ValueError('Paths must be non-empty strings')
            if value == '~':
                return home
            if value.startswith('~/'):
                return home / value[2:]
            return Path(value)
        parsed, fallbacks = [], []
        for item in mappings:
            if (not isinstance(item, dict) or not isinstance(item.get('name'), str)
                    or not item['name'] or not isinstance(item.get('worktrees', False), bool)
                    or not isinstance(item.get('after_inherit', False), bool)):
                raise ValueError('Invalid project mapping')
            target = fallbacks if item.get('after_inherit', False) else parsed
            target.append((local_path(item.get('path')), item['name'], item.get('worktrees', False)))
        start, end = hours.get('start', 9), hours.get('end', 21)
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= 24):
            raise ValueError('work_hours must satisfy 0 <= start < end <= 24')
        expire = routing.get('credits_expire', '12-31')
        try:
            month, day = (int(part) for part in expire.split('-'))
            date(2024, month, day)  # A leap year, so 02-29 is accepted.
        except (AttributeError, ValueError):
            raise ValueError('routing.credits_expire must be "MM-DD"') from None
        # Expiry times of the Codex full resets the account has, each with its UTC offset.
        resets, codex_resets = routing.get('codex_resets', []), []
        for value in resets if isinstance(resets, list) else [None]:
            try:
                moment = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                moment = None
            if moment is None or moment.tzinfo is None:
                raise ValueError('routing.codex_resets must be a list of ISO times with a UTC offset')
            codex_resets.append(moment.timestamp())
        codex_resets = tuple(sorted(codex_resets))
        return cls(modules=enabled, mark_guessed=mark_guessed,
                   routing_policy=RoutingPolicy.load(routing),
                   context_high_percent=context_high_percent, credits_expire=(month, day),
                   codex_resets=codex_resets,
                   codex_live_limits=codex_live,
                   codex_cli=local_path(paths['codex_cli']) if 'codex_cli' in paths else None,
                   claude_limits_extra=local_path(paths['claude_limits']) if 'claude_limits' in paths else None,
                   project_order=order, project_mappings=parsed, project_fallback_mappings=fallbacks,
                   inherit_folders=inherit, fallback_project=fallback, work_hours=(start, end),
                   wrapper_runs=local_path(paths.get('codex_runs', f'{DATA}/codex-runs')),
                   routing_file=local_path(paths.get('routing', f'{DATA}/routing.json')),
                   limits_history=local_path(paths.get('limits_history', f'{DATA}/limits-history.jsonl')),
                   agents_registry=local_path(paths.get('agents_registry', '~/.claude/tools/agents-registry.jsonl')))

    def frontend(self):
        return {'modules': self.modules, 'project_order': self.project_order, 'work_hours': self.work_hours}
