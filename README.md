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

Before restarting a running board after a code change, compare the checkout to the live board:

```sh
./murmuratio check-update --url http://127.0.0.1:8765
```

Restart only if the command exits successfully. Use the running board's local URL and, if applicable, its `--config` and `--codex-home` values. The check reports changes to visible card IDs, projects, statuses and orchestrator roles. Unexplained losses, changed statuses or roles, unhealthy sources and an unstable live board block the restart. Retry if the live board changes while comparing. Confirmed archived sessions, hidden chats and expired workers may leave. No snapshot files are written, and task text is not printed.

The source files can change during comparison: two matching live snapshots reduce this risk but do not freeze them. The check compares API data and the board's card-retention rules; it does not verify browser rendering.

## What it reads, writes and sends

Reads by default:

- Claude Code transcripts and subagent records: `~/.claude/projects/`. Claude desktop session metadata: `~/Library/Application Support/Claude/claude-code-sessions/`.
- Saved Codex sessions and usage-limit events: `~/.codex/sessions/`. Exec workers, Codex subagents and Claude-linked workers appear without a wrapper or optional module. Interactive app and CLI chats are hidden from projects by default; running chats appear as `Your Codex chats: N` in the header. `CODEX_HOME` selects the Codex data directory.
- Local open-file information from macOS `lsof`, to check whether an inactive Codex rollout still has a writer.
- Claude limits cache: `~/.local/share/murmuratio/claude-limits.json`, plus an optional extra cache set as `claude_limits` under `[paths]`. The newest usable snapshot is selected.
- With `codex_wrapper`: compatible run records in `~/.local/share/murmuratio/codex-runs/` (set `codex_runs` under `[paths]`) and the event files they reference. Murmuratio does not create wrapper records.
- With `codex_chats`: `~/.codex/state_5.sqlite` and its WAL/SHM files, `~/.codex/.codex-global-state.json`, and referenced rollouts under `sessions/` or `archived_sessions/` in the Codex data directory.

Writes:

- Configuration: `~/.config/murmuratio/config.toml`, created by install only if absent.
- Installation records and settings backups: `~/.local/share/murmuratio/install/`.
- When `~/.claude` exists: `~/.claude/settings.json`, to wrap `statusLine` while preserving the previous command. The wrapper writes the Claude limits cache above.
- With `--launch-agent`: `~/Library/LaunchAgents/org.murmuratio.app.plist`.
- Optional forecast and routing read and write `~/.local/share/murmuratio/limits-history.jsonl`; routing also writes `~/.local/share/murmuratio/routing.json`. These paths and the wrapper directory are configurable.
- Optional context reset also reads `goals_1.sqlite` and `config.toml` in the Codex data directory, keeps recovery state at `~/.local/share/murmuratio/reset/reset-results.json`, and can change Codex data.
- Browser preferences stay in local browser storage.

`./murmuratio run --dry-run` lists the active paths.

Sends: nothing during normal use. With `[limits] codex_live = true`, Codex itself reads your account's limits from OpenAI every 5 minutes. The HTTP server listens only on `127.0.0.1` and makes no model or account API calls. The previous status-line command and an explicitly confirmed experimental context reset can run software with its own network activity. See [SECURITY.md](SECURITY.md).

## Accuracy limits

- Claude native subagents are included in their parent turn and do not have separate cards.
- An unfinished Codex worker stays on the board however long it is quiet; finished and crashed ones leave. Rollouts older than a day are checked by their first record and last lifecycle event and reread only when they change, so startup stays fast with months of history. The Codex chats tab lists every saved chat from Codex's own index, newest first. Ephemeral sessions without saved rollouts are invisible.
- Launch classification uses `session_meta.source` (`exec` or a `subagent` object for workers; `cli`, `tui` or `vscode` for interactive chats), with `originator` as a fallback. Codex Desktop threads whose `thread_source` says an agent created or forked them are workers. A Claude parent link or wrapper record identifies a worker regardless of the interactive launch metadata. Finished cards leave the board after 30 minutes; their recent history still contributes to today's totals.
- Claude-to-Codex parent links use recorded identifiers when available. Otherwise, a unique working-directory and launch-time match within about two minutes is marked inferred and may be wrong. Indirect or delayed launches may be missed.
- Very large records may be read partially or skipped. Codex board reads buffer records up to 8 MiB; larger turn boundaries receive limited handling.
- Status and context are inferred from saved data. Quiet unfinished Claude turns remain working for up to 24 hours after their last saved activity; silence alone never means approval is needed. Usage-limit numbers are snapshots and may be stale.
- A completed wrapper record wins over unfinished rollout state. Codex threads with no rollout writer and no activity for over 30 minutes move to completed as crashed; a live wrapper process keeps its current status. If the process check fails, the board preserves the inferred status.
- Only macOS is supported.

## Optional modules

All modules are off by default. Set them to `true` under `[modules]` in `~/.config/murmuratio/config.toml`, then restart:

- `forecast`: estimates usage limits; `scenarios` compares forecasts and requires `forecast`.
- `routing`: writes a suggestion of which tool should take code and which design, so neither weekly quota is left unused at its reset. A tool whose remaining quota would last past its reset at the current pace gets more work; when both would, code goes to the one that resets first; a tool that cannot cover another work day gets less. Other scripts can read the suggestion from `routing.json`. Codex credits, which are spent only after the Codex weekly limit reaches 100%, are planned per month until they expire (`[routing] credits_expire = "MM-DD"`, default `"12-31"`) and per week within the month; this week's share counts as extra Codex capacity.
- `codex_credits`: shows the credit balance left this month; `burn_rate` shows token usage rates.
- `codex_chats`: shows saved chats; `context_reset` enables experimental resets and requires `codex_chats`.
- `codex_wrapper`: reads wrapper records; `branding` adds board graphics.

To show interactive Codex chats as full board cards, add this separate section and restart:

```toml
[board]
show_codex_chats = true
```

This setting is independent of the `codex_chats` module, which enables the saved-chat browser tab.

Codex writes its limits to session logs only while it works, so a quiet Codex shows its last known values with their age. To keep them current, add this section and restart:

```toml
[limits]
codex_live = true
```

Every 5 minutes Murmuratio then starts `codex app-server` and sends one `account/rateLimits/read` request, the read the Codex app uses to show remaining limits and credits. It makes no model call and changes nothing, but Codex contacts OpenAI with your signed-in account. The `codex` command is found on `PATH`, in `/opt/homebrew/bin` or `/usr/local/bin`; set `codex_cli` under `[paths]` to choose another.

## Uninstall

```sh
./murmuratio uninstall
```

This restores the previous status line and removes files the installer owns, the status line's limits cache and the folders the installer created when they are empty. Existing or edited configuration and other data, such as routing and history files, are kept. Older installations without file ownership records may leave their configuration or backup in place.

## Disclaimer

Murmuratio is not affiliated with or endorsed by Anthropic or OpenAI. Claude, Claude Code and Codex are trademarks of their respective owners.

Normal inspection uses only local data and sends no data off your machine. Existing status-line commands and the optional context reset can run software with its own network activity, as described above. To the extent permitted by applicable law, the authors are not liable for any loss, damage, cost (including API or subscription usage), data exposure or downtime arising from use of Murmuratio. Usage-limit and context numbers are estimates and must not be relied on for billing or decisions.

## More

- [CHANGELOG.md](CHANGELOG.md)
- [SECURITY.md](SECURITY.md)
- [CONTRIBUTING.md](CONTRIBUTING.md) and [tests/README.md](tests/README.md) for development checks
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [MIT license](LICENSE)
