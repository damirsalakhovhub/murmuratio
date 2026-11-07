# Murmuratio

Beta software, provided "as is" without warranty of any kind. Use at your own risk. You are responsible for your data, your agents and their actions. See [LICENSE](LICENSE).

A local board for Claude Code and Codex on macOS. See orchestrators and their workers grouped by project, with their tasks, models, inferred status, context and usage limits.

![Murmuratio board with sample data: two projects, each with an orchestrator and its workers, one worker blocked waiting for approval, and Claude and Codex usage limits in the header](docs/screenshot.png)

## Install

Requires macOS and Python 3.11 or newer. The application uses only the Python standard library.

```sh
git clone https://github.com/damirsalakhovhub/murmuratio.git && cd murmuratio
./murmuratio install   # --dry-run to preview, --launch-agent to start at login
./murmuratio run       # http://127.0.0.1:8765/
```

Try it without installing: `./murmuratio run --demo` shows sample data. `./murmuratio doctor` checks local sources.

Keep the cloned folder while installed. If `~/.claude` exists, the installer backs up Claude Code settings and wraps the existing status line; otherwise it skips that step. Run install again after adding Claude to enable it.

## What it reads, writes and sends

Reads by default:

- Claude Code transcripts and subagent records: `~/.claude/projects/`. Claude desktop session metadata: `~/Library/Application Support/Claude/claude-code-sessions/`.
- Saved Codex sessions and usage-limit events: `~/.codex/sessions/`. Exec workers, Codex subagents and Claude-linked workers appear without a wrapper or optional module. Interactive app and CLI chats are hidden from projects by default; running chats appear as `Your Codex chats: N` in the header. `CODEX_HOME` selects the Codex data directory.
- Local open-file information from macOS `lsof`, to check whether an inactive Codex rollout still has a writer.
- Claude limits caches: `~/.local/share/murmuratio/claude-limits.json` and legacy `~/.claude/tools/claude-limits.json`. The newest usable snapshot is selected.
- With `codex_wrapper`: compatible run records in `~/.claude/tools/codex-runs/` and the event files they reference. Murmuratio does not create wrapper records.
- With `codex_chats`: `~/.codex/state_5.sqlite` and its WAL/SHM files, `~/.codex/.codex-global-state.json`, and referenced rollouts under `sessions/` or `archived_sessions/` in the Codex data directory.

Writes:

- Configuration: `~/.config/murmuratio/config.toml`, created by install only if absent.
- Installation records and settings backups: `~/.local/share/murmuratio/install/`.
- When `~/.claude` exists: `~/.claude/settings.json`, to wrap `statusLine` while preserving the previous command. The wrapper writes the Claude limits cache above.
- With `--launch-agent`: `~/Library/LaunchAgents/org.murmuratio.app.plist`.
- Optional forecast and routing read and write `~/.claude/tools/limits-history.jsonl`; routing also writes `~/.claude/tools/routing.json`. These paths and the wrapper directory are configurable.
- Optional context reset also reads `goals_1.sqlite` and `config.toml` in the Codex data directory, keeps recovery state at `~/.local/share/murmuratio/reset/reset-results.json`, and can change Codex data.
- Browser preferences stay in local browser storage.

`./murmuratio run --dry-run` lists the active paths.

Sends: nothing during normal use. The HTTP server listens only on `127.0.0.1` and makes no model or account API calls. The previous status-line command and an explicitly confirmed experimental context reset can run software with its own network activity. See [SECURITY.md](SECURITY.md).

## Accuracy limits

- Codex discovery covers saved sessions modified in the last 24 hours. Ephemeral sessions without saved rollouts are invisible.
- Launch classification uses `session_meta.source` (`exec` or a `subagent` object for workers; `cli`, `tui` or `vscode` for interactive chats), with `originator` as a fallback. A Claude parent link or wrapper record identifies a worker regardless of the interactive launch metadata. Finished cards leave the board after 30 minutes; their recent history still contributes to today's totals.
- Claude-to-Codex parent links use recorded identifiers when available. Otherwise, a unique working-directory and launch-time match within about two minutes is marked inferred and may be wrong. Indirect or delayed launches may be missed.
- Very large records may be read partially or skipped. Codex board reads skip most individual records larger than 512 KiB; oversized metadata and turn boundaries receive limited handling.
- Status and context are inferred from saved data. Usage-limit numbers are snapshots and may be stale.
- A completed wrapper record wins over unfinished rollout state. Codex threads with no rollout writer and no activity for over 30 minutes move to completed as crashed; a live wrapper process keeps its current status. If the process check fails, the board preserves the inferred status.
- Only macOS is supported.

## Optional modules

All modules are off by default. Set them to `true` under `[modules]` in `~/.config/murmuratio/config.toml`, then restart:

- `forecast`: estimates usage limits; `scenarios` compares forecasts and requires `forecast`.
- `routing`: suggests a tool; `codex_credits` shows credit usage; `burn_rate` shows token usage rates.
- `codex_chats`: shows saved chats; `context_reset` enables experimental resets and requires `codex_chats`.
- `codex_wrapper`: reads wrapper records; `branding` adds board graphics.

To show interactive Codex chats as full board cards, add this separate section and restart:

```toml
[board]
show_codex_chats = true
```

This setting is independent of the `codex_chats` module, which enables the saved-chat browser tab.

## Uninstall

```sh
./murmuratio uninstall
```

This restores the previous status line and removes files the installer owns. Existing or edited configuration and other data are kept. Older installations without file ownership records may leave their configuration or backup in place.

## Disclaimer

Murmuratio is not affiliated with or endorsed by Anthropic or OpenAI. Claude, Claude Code and Codex are trademarks of their respective owners.

Normal inspection uses only local data and sends no data off your machine. Existing status-line commands and the optional context reset can run software with its own network activity, as described above. To the extent permitted by applicable law, the authors are not liable for any loss, damage, cost (including API or subscription usage), data exposure or downtime arising from use of Murmuratio. Usage-limit and context numbers are estimates and must not be relied on for billing or decisions.

## More

- [CHANGELOG.md](CHANGELOG.md)
- [SECURITY.md](SECURITY.md)
- [CONTRIBUTING.md](CONTRIBUTING.md) and [tests/README.md](tests/README.md) for development checks
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [MIT license](LICENSE)
