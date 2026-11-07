# Murmuratio: instructions for an AI agent

You were given this link by a person. Read it, then do the steps below.
Talk to the person in their language. Keep it short.

## 1. Tell the person what this is (3-4 sentences)

Murmuratio is a small local web page for macOS that shows every Claude Code and Codex
agent running on this Mac, grouped by project. When Claude orchestrates and starts
Codex workers in hidden chats, Codex does not show them; Murmuratio does: each worker
under the orchestrator that started it, its model, status, how full its context is,
and the Claude and Codex usage limits. It only reads local files, serves on
127.0.0.1 and sends nothing anywhere. Open source, MIT.

## 2. Check whether it is useful for them

Ask or check, then say plainly if it is worth installing:
- Useful if they use Claude Code and Codex together, especially with Claude starting
  Codex workers, or if they run many agent sessions at once.
- Not useful if they use only one chat at a time, or are not on macOS.
- Requires macOS and Python 3.11+ (`python3 --version`).

Do not install anything until the person says yes.

## 3. What installing changes (tell them before you start)

- Adds a Claude Code `statusLine` command to `~/.claude/settings.json` so the board can
  read Claude limits. The previous settings file is backed up and restored on uninstall.
  If they already have a statusLine, it keeps working: it is wrapped, not replaced.
- Creates `~/.config/murmuratio/config.toml` and
  `~/.local/share/murmuratio/`.
- Old `agent-inspector` directories move to the new names if those destinations do not exist.
- Optional, only if they ask: a LaunchAgent that starts the board at login.

You can show the exact list first: `./murmuratio install --dry-run`.

## 4. Install (after a yes)

```sh
git clone https://github.com/damirsalakhovhub/murmuratio.git
cd murmuratio
./murmuratio install                 # add --launch-agent to start at login
./murmuratio run        # serves the board
```

Then tell them to open http://127.0.0.1:8765/ (a different port: `run --port 9000`).
If something looks wrong: `./murmuratio doctor`.
To see sample data: `./murmuratio run --demo`. A first run can migrate old directories.

## 5. Remove

```sh
./murmuratio uninstall
```

Restores their previous statusLine and removes both the old and new configuration and data directories. Then delete the cloned folder.
