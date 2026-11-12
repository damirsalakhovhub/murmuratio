"""Discover local source locations without opening or modifying their data."""
from dataclasses import dataclass
import os
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
    claude_limits_legacy: Path | None
    agents_registry: Path
    integration_registry: Path

    @classmethod
    def at(cls, home, config, codex_home=None):
        home = Path(home)
        codex = Path(codex_home or os.environ.get('CODEX_HOME') or home / '.codex').expanduser()
        return cls(
            claude_executable=which('claude'), codex_executable=which('codex'),
            claude_sessions=home / '.claude/projects',
            claude_desktop=home / 'Library/Application Support/Claude/claude-code-sessions',
            codex_home=codex, codex_rollouts=codex / 'sessions',
            codex_wrapper=config.wrapper_runs or home / '.local/share/murmuratio/codex-runs',
            claude_limits=home / '.local/share/murmuratio/claude-limits.json',
            # An extra limits cache, for example one an older status line writes.
            claude_limits_legacy=config.claude_limits_extra,
            # Launch wrappers append who each agent is; the board reads it, never writes it.
            agents_registry=config.agents_registry or home / '.claude/tools/agents-registry.jsonl',
            integration_registry=home / '.local/share/murmuratio/agents.jsonl',
        )
