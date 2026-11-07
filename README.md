# murmuratio

Murmuratio is a local board for Claude Code and Codex on your Mac. When Claude starts Codex workers in hidden chats, the board shows them under their orchestrator, grouped by project, with their tasks, models, status, context use and usage limits.

Requires macOS and Python 3.11 or newer.

```sh
git clone https://github.com/damirsalakhovhub/murmuratio.git
cd murmuratio
./murmuratio install                  # add --launch-agent to start at login
./murmuratio run              # open http://127.0.0.1:8765/
./murmuratio uninstall
```

The installer wraps the Claude Code `statusLine` in `~/.claude/settings.json`, preserving its previous command and backing up the original settings. It creates `~/.config/murmuratio/config.toml` and `~/.local/share/murmuratio/`. Uninstall restores the previous status line, preserves unrelated settings changes, and removes both the current and old `agent-inspector` configuration and data directories. Keep the cloned folder while installed. Use `./murmuratio install --dry-run` to inspect the changes first.

On install or run, old `agent-inspector` configuration and data directories move to the new names if the corresponding destination does not exist. Existing destinations are never overwritten. The old `./agent-inspector` command remains as a deprecated alias for one release.

Murmuratio reads local Claude and Codex files, serves only on `127.0.0.1`, and sends nothing. An existing status line is a separate command and may do its own work.

## Optional modules

All modules are off by default. Set any to `true` under `[modules]` in the config file and restart the board:

- `forecast`: estimates when usage limits will be reached.
- `scenarios`: compares usage scenarios; requires `forecast`.
- `routing`: suggests which tool to use based on available limits.
- `codex_credits`: shows Codex credit usage.
- `burn_rate`: shows token usage rates.
- `codex_chats`: opens saved Codex chats.
- `context_reset`: enables experimental Codex context reset; requires `codex_chats` and launches Codex, whose own network activity is outside Murmuratio's control.
- `codex_wrapper`: reads local Codex wrapper records.
- `branding`: shows additional board graphics.

For a board with sample data, run `./murmuratio run --demo`. Demo reads no personal session data; the first run can still migrate old Murmuratio directories. License: [MIT](LICENSE).
