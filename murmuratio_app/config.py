"""Optional local configuration for Murmuratio."""
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import tomllib

DATA = '~/.local/share/murmuratio'
MODULES = ('forecast', 'scenarios', 'routing', 'codex_credits', 'burn_rate',
           'codex_chats', 'context_reset', 'codex_wrapper', 'branding')


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
    show_codex_chats: bool = False
    credits_expire: tuple = (12, 31)
    codex_live_limits: bool = False
    codex_cli: Path | None = None
    claude_limits_extra: Path | None = None

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
        show_codex_chats = board.get('show_codex_chats', False)
        if not isinstance(show_codex_chats, bool):
            raise ValueError('board.show_codex_chats must be a boolean')
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
        return cls(modules=enabled, show_codex_chats=show_codex_chats, credits_expire=(month, day),
                   codex_live_limits=codex_live,
                   codex_cli=local_path(paths['codex_cli']) if 'codex_cli' in paths else None,
                   claude_limits_extra=local_path(paths['claude_limits']) if 'claude_limits' in paths else None,
                   project_order=order, project_mappings=parsed, project_fallback_mappings=fallbacks,
                   inherit_folders=inherit, fallback_project=fallback, work_hours=(start, end),
                   wrapper_runs=local_path(paths.get('codex_runs', f'{DATA}/codex-runs')),
                   routing_file=local_path(paths.get('routing', f'{DATA}/routing.json')),
                   limits_history=local_path(paths.get('limits_history', f'{DATA}/limits-history.jsonl')))

    def frontend(self):
        return {'modules': self.modules, 'project_order': self.project_order, 'work_hours': self.work_hours}
