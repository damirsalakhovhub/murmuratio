# Security

Murmuratio is beta software. Security support is best effort, with no SLA or guaranteed response time. There is no bug bounty program.

## Report a vulnerability

Use **Report a vulnerability** on the [GitHub Security page](https://github.com/damirsalakhovhub/murmuratio/security) to send a private report. A repository administrator must enable private vulnerability reporting for this option to appear. If it is unavailable, contact a maintainer privately and do not publish sensitive details in an issue.

## Local data

The board reads Claude project transcripts, Claude desktop session metadata, Codex rollout files and locally cached usage limits. It queries local open-file information with macOS `lsof` to check inactive Codex rollout writers. With optional modules enabled, it also reads Codex chat metadata and archived rollouts, wrapper records, usage history and reset settings. Chat text can appear in the local browser; masking recognizes some secret formats but cannot detect every secret. Source errors may leave a board partially populated, and status and usage are estimates based on saved data.

When `~/.claude` exists, the installer backs up and changes `~/.claude/settings.json`; it does not create the directory otherwise. It creates installation records and, if absent, a default configuration, and can create a LaunchAgent. Uninstall restores the status line and removes its records, an unchanged installer-created configuration, and its LaunchAgent; other files are retained. The installed status line writes a quota cache. Forecast and routing can write `~/.local/share/murmuratio/limits-history.jsonl` and `~/.local/share/murmuratio/routing.json`, or paths configured by the user. Context reset writes recovery state under the data directory.

The HTTP server binds to loopback, checks local hosts and origins, and does not log request contents. Normal inspection makes no model or account API requests. The optional `[limits] codex_live = true` setting starts `codex app-server` every 5 minutes for one read-only `account/rateLimits/read` request; Codex then contacts OpenAI with the signed-in account. Murmuratio never reads Codex credentials itself. An existing status-line command runs separately. An explicitly confirmed experimental context reset starts Codex, which can modify its own data and use the network. Browser preferences stay in local browser storage. Use `./murmuratio run --dry-run` to inspect the active read and write paths.
