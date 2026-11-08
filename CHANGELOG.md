# Changelog

## 0.2.0-beta

Board

- The Codex chats view uses the board's look, with one shared header, real chat titles and a phone layout.
- Charts can be hidden and shown; board columns can be hidden and grow from 260 to 600 px when there are few.
- Notices sit in the page flow; charts stay readable at phone width.
- Uncertain, stale and paused states are shown as such, and unreadable sources name the reason.

Correctness

- Claude one-shot sessions no longer leave ghost cards, and their durations are right; idle Desktop orchestrators stay on the board.
- Codex Desktop threads started by an agent are workers; chats a person opened stay hidden unless `[board] show_codex_chats = true`.
- Codex lifecycle records parse whatever their JSON member order; in-place rewrites of session logs reset cached summaries.
- Board updates are guarded against losing cards, and timestamps outside the supported range are rejected.
- An unfinished Codex worker stays on the board however long it is quiet, instead of leaving after 24 hours; old rollouts are checked cheaply, so startup stays fast with months of history. The Codex chats tab lists every saved chat.

Limits and routing

- A stale quota sample keeps its last known values, marked with their age, in the header, forecast and routing. Only a window past its reset is dropped.
- Optional `[limits] codex_live = true` reads Codex limits from Codex itself every 5 minutes (`codex app-server`, one `account/rateLimits/read`, no model call).
- Routing aims to use each weekly quota by its reset: a week under pace gets code and design, and when both are, code goes to the one that resets first.
- Codex credits are split by month until they expire (`[routing] credits_expire`, default `"12-31"`) and by week within the month; this week's share adds to Codex's weekly capacity. The header shows credits left this month.
- `routing.json` keeps its fields and adds `last_known_age_seconds` and a `codex.credits` block. It is not rewritten until the first credit scan and live read have finished after a start.

Changed defaults

- The routing file, quota history and wrapper records default to `~/.local/share/murmuratio/` instead of `~/.claude/tools/`. Set `routing`, `limits_history` and `codex_runs` under `[paths]` to keep the old locations.
- `~/.claude/tools/claude-limits.json` is no longer read by default; set `claude_limits` under `[paths]` to read an extra Claude limits cache.
- With no configuration every optional module is off, also when the server is started directly.
- The migration from the pre-release product name is removed.

Tools and tests

- `./murmuratio check-update --url <board>` compares this checkout with a running board without writing anything.
- Browser checks run on macOS in CI, including the real board, a release smoke test and 1,000-session scale checks. A bug report form is included.

## 0.1.0-beta

- Local Claude Code and Codex board with optional usage forecasts, routing and chat inspection.
- Installer, status-line integration, optional macOS LaunchAgent and sample-data mode.
