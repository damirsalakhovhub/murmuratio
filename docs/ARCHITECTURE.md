# Architecture

`murmuratio` enters `murmuratio_app/cli.py`. The loopback server in `server.py` serves `web/` and the read-only `/api/now` snapshot. `statusline_wrapper.py` is the stable callback used by installed Claude Code settings; implementation lives in the package. The runtime uses the Python standard library and requires Python 3.11 or newer on macOS.

## Sources and failure isolation

`discovery.py` finds local sources, using `CODEX_HOME` when set. Adapters normalize saved records into observations defined in `schema.py`. `now.py` merges them with grouping and metrics. Unknown fields are ignored; malformed records and unreadable files do not discard healthy observations from other records/files. Source problems carry fixed diagnostic codes rather than exception text or private paths.

Claude sessions and native subagent transcripts are incrementally summarized. A subagent gets its own worker card, exact parent link, recorded model/effort and brief. Its usage is classified as worker usage even when its parent is a personal chat. Codex discovery reads rollouts independently of SQLite; the session index and a read-only state database provide titles. Both `name` and `title` database schemas are supported. Old rollouts receive a cheap launch/tail check; unfinished workers stay discoverable while quiet.

Complete JSONL records are consumed incrementally. In-place rewrites invalidate cached summaries. Shared Claude/Codex summary readers buffer records up to 8 MiB. The Codex board reader uses 512 KiB read chunks and buffers records up to 64 MiB; larger records are skipped. Codex quota reads use a 400,000-byte tail and discard its leading record fragment. Incomplete final records are retried. Rejected summary records invalidate context until a new usage measurement. Codex board records too large to interpret also invalidate confirmed lifecycle state. Card briefs cap request/reply text, file and commit lists; the unused full-text `last_action` field is not exposed.

Snapshots are computed off request threads. A request waits briefly for a new state, then can return the last complete snapshot. `snapshot_at` is the snapshot's Unix time; `refresh_failed` identifies a failed refresh. A slow in-progress refresh also supplies `stale` and `stale_seconds`. The browser polls every three seconds and shows one header line, `Data is N min old`, after two refresh intervals. The age continues to update during connection failure or a reading pause; successful refresh clears it.

Quota history is read only for forecast, routing or burn-rate modules. Read/write failures are reported without aborting the board snapshot. An unreadable history is not overwritten with a partial replacement. Optional usage scans run in the background and retain their last usable result.

## Cards and visibility

Grouping uses recorded parent identifiers first. Claude Bash results can identify a launched Codex thread exactly; a unique folder/time match is inferred and marked. Ambiguous matches remain standalone. Registry records can set column, role and parent. Titles use this priority: Desktop, registry, wrapper, subagent description, meaningful task text (Claude's first prompt or Codex's latest user task). Machine preambles and launch metadata do not name cards.

Named or registered orchestrators remain visible without children under the same provider policy. Other lone top-level chats are hidden; they appear while they have unfinished children or children finished today. Identified Codex workers and recorded child sessions remain eligible independently. An empty board means no eligible agents, not a chat-history view or proof that providers are unavailable. Native Claude subagents need their parent card. Finished workers stay on the visible tree for 30 minutes, with ancestors retained while children are active; today's completed work contributes to totals.

Cards show recorded model and effort. Context uses current saved input usage and a recorded or model-family context window. Unknown usage stays unknown. Unfinished cards at or above `[board] context_high_percent` (default 80) signal attention; finished cards do not. Compaction invalidates prior context until new usage arrives. The web product has Board and, when Codex quota/credit data is available, Calculator. There is no chat tab or context-reset operation. Demo has deterministic board fixtures and hides Calculator.

## Configuration and writes

All modules default to off: `forecast`, `scenarios`, `routing`, `codex_credits`, `burn_rate`, `codex_wrapper`, `branding`. Scenarios require forecast. `[board] mark_guessed` defaults to false. Project column mappings and ordering live under `[projects]`; workday bounds live under `[work_hours]`.

The default data directory is `~/.local/share/murmuratio/`. `[paths] codex_runs`, `routing`, `limits_history`, `agents_registry` and `claude_limits` can select compatible existing locations. The extra Claude limits path supplements the normal status-line cache; the newest usable sample wins. `./murmuratio run --dry-run` lists active reads, writes and atomic replacement paths, including registry/subagent metadata reads and routing-history reads/writes.

Forecast and routing maintain `limits-history.jsonl`; burn-rate calculations can read it. Routing writes `routing.json` every minute, including when no browser is open, and appends changed advice to `routing-history.jsonl` beside quota history. Cold credit/live-limit scans delay publishing partial routing. Stale limits retain their last known age; expired windows leave forecasts and routing. Browser preferences remain in local storage.

Routing keeps code on Codex while its forecast fits quota, and design on Claude. Policy lives under `[routing]`: `credits_expire` defaults to `"12-31"`; `codex_resets` can list account reset expiry timestamps. `credits_reserve_months` and `credits_start` configure reservations. Tables `codex_tiers`, `claude_tiers`, `surplus_boost` and `saving_boost` configure launcher tier labels. Default tier labels are `"default"`, boost maps are empty and no reservation calendar is enabled. `surplus_effort` defaults to unset and `surplus_title` to `"Surplus: run Fast"`. Calculator displays the server-computed mode and account reset/credit information; it never performs a reset.

The installer preserves Claude settings and the previous status-line command. A LaunchAgent starts immediately when installed, so it is an alternative to a foreground server. Uninstall keeps recovery information until cache cleanup succeeds and can resume after a cache-access failure. See [README](../README.md) for each lifecycle and [SECURITY](../SECURITY.md) for the local-account trust boundary and optional Codex network read.
