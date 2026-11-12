# Security

Murmuratio is beta software. Security support is best effort, with no SLA or guaranteed response time. There is no bug bounty program.

## Report a vulnerability

Use **Report a vulnerability** on the [GitHub Security page](https://github.com/damirsalakhovhub/murmuratio/security) to send a private report. A repository administrator must enable private vulnerability reporting for this option to appear. If it is unavailable, contact a maintainer privately and do not publish sensitive details in an issue.

## Local data

Murmuratio never edits Codex conversations or settings. Live limits start the Codex CLI, which may update its own state files, and SQLite readers may create WAL/SHM sidecar files.

The board reads Claude project transcripts, Claude desktop session metadata, Codex rollout files and locally cached usage limits. It queries local open-file information with macOS `lsof` to check inactive Codex rollout writers. It also reads Codex thread metadata and referenced archived rollouts. With optional modules enabled, it reads wrapper records and usage history. Chat text can appear in the local browser; masking recognizes some secret formats but cannot detect every secret. Source errors may leave a board partially populated, and status and usage are estimates based on saved data.

When `~/.claude` exists, the installer backs up and changes `~/.claude/settings.json`; it does not create the directory otherwise. It creates installation records and, if absent, a default configuration, and can create a LaunchAgent. Uninstall restores the status line and removes its records, an unchanged installer-created configuration, its LaunchAgent and the status-line limits cache; other files are retained. Cleanup can be retried if cache removal fails. The installed status line writes a quota cache. Forecast and routing can write `~/.local/share/murmuratio/limits-history.jsonl` and `~/.local/share/murmuratio/routing.json`, or paths configured by the user. Routing also appends changed advice to `routing-history.jsonl` beside the quota history.

The HTTP server binds only to loopback (`127.0.0.1`) and has no access token. **Any process or account on the same Mac can read `/api/now`, including session text. Do not run Murmuratio on shared machines.** Host and origin checks limit browser access but do not authenticate local processes or accounts. The server does not log request contents. Normal inspection makes no model or account API requests. The optional `[limits] codex_live = true` setting starts `codex app-server` every 5 minutes for one read-only `account/rateLimits/read` request; Codex then contacts OpenAI with the signed-in account. Murmuratio never reads Codex credentials itself. An existing status-line command runs separately. Browser preferences stay in local browser storage. Use `./murmuratio run --dry-run` to inspect the active read and write paths.
