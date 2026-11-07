# Murmuratio 0.1.0-beta

First public beta release. Murmuratio is a local board for Claude Code and Codex on macOS. It shows orchestrators and their workers grouped by project, with their tasks, models, inferred status, context and usage limits.

## What it does

- Reads saved Claude Code and Codex sessions from your machine and groups them by project.
- Links workers to the orchestrator that started them, including Codex runs started from Claude Code.
- Shows Claude and Codex usage limits in the header.
- Serves the board on `127.0.0.1` only. Normal use sends nothing off the machine and makes no model or account API calls.
- Optional modules, off by default: usage forecasts, scenarios, routing suggestions, Codex credits, burn rate, saved Codex chats, experimental context reset, wrapper records, board graphics.

## Requirements

- macOS
- Python 3.11 or newer (standard library only)

## Install

```sh
git clone https://github.com/damirsalakhovhub/murmuratio.git && cd murmuratio
./murmuratio install   # --dry-run to preview, --launch-agent to start at login
./murmuratio run       # http://127.0.0.1:8765/
```

`./murmuratio run --demo` shows sample data without installing. `./murmuratio uninstall` restores the status line and removes installed files.

## Known limits

- macOS only.
- Codex discovery covers saved sessions modified in the last 24 hours. Ephemeral sessions are invisible.
- Without recorded identifiers, Claude-to-Codex links are inferred from working directory and launch time, and may be wrong.
- Status and context are inferred from saved data. Usage-limit numbers are snapshots and may be stale.
- Very large records may be read partially or skipped.

See the [README](../README.md) for the full list of paths it reads and writes.
