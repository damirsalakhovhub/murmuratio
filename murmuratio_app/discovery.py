"""Discover local source locations without opening or modifying their data."""
from dataclasses import dataclass
from pathlib import Path
from shutil import which


@dataclass(frozen=True)
class Discovery:
    claude_executable: str | None
    codex_executable: str | None
    claude_sessions: Path
    claude_desktop: Path
    codex_home: Path
    codex_rollouts: Path
    codex_wrapper: Path
    claude_limits: Path
    claude_limits_legacy: Path

    @classmethod
    def at(cls, home, config):
        home = Path(home)
        codex = home / '.codex'
        return cls(
            claude_executable=which('claude'), codex_executable=which('codex'),
            claude_sessions=home / '.claude/projects',
            claude_desktop=home / 'Library/Application Support/Claude/claude-code-sessions',
            codex_home=codex, codex_rollouts=codex / 'sessions',
            codex_wrapper=config.wrapper_runs or home / '.claude/tools/codex-runs',
            claude_limits=home / '.local/share/murmuratio/claude-limits.json',
            claude_limits_legacy=home / '.claude/tools/claude-limits.json',
        )
