# Murmuratio — observable data

## Board

`murmuratio_app/discovery.py` locates installed tools and their data. `murmuratio_app/adapters/` reads local Codex run metadata and event JSONL, Claude project transcripts and desktop session metadata; `now_source.py` composes their results. Desktop `cliSessionId` maps to the transcript; titles, archive state and parent links come from desktop JSON. Sessions with a non-null `scheduledTaskId` are excluded before transcript processing, status inference and parent/orchestrator linking. They cannot appear as roots or workers, even when their transcript is recent or cached.

The adapters return typed observations with a source, timestamp and observed or inferred confidence. Board sessions, parent/action/timing/context fields, quota windows and usage share the normalized types in `murmuratio_app/schema.py`; grouping and derived quota, forecast, routing and burn calculations live separately in `murmuratio_app/grouping.py` and `murmuratio_app/metrics/`. The existing flat JSON consumed by the frontend remains unchanged for healthy sources.

When an enabled source is missing, unreadable or incompatible, `/api/now` includes its `unavailable` or `unsupported` status in `sources`; valid observations from other sources still render. Malformed individual files do not hide valid files from the same source. Unknown fields are ignored. Disabled optional sources are not read. Codex chat state reports the same source status if its database cannot be read; an incompatible rollout can still return its valid events.

Claude status follows saved activity and completed turns. Codex status additionally checks its recorded PID. Status is a local inference, not proof that a network request is active. Model, action and context values come from saved events; unavailable values remain unknown. Source files are read without modification.

Claude plan limits come from `~/.claude/tools/claude-limits.json`; Codex limits come from saved rollout events. These snapshots can be stale. The existing derived `limits-history.jsonl` and `routing.json` files record quota history and routing without changing model settings. No model API or account API is queried.

## Codex chats

`inspector/adapters/codex_sessions.py` opens `state_5.sqlite` read-only with `query_only` for metadata and indexes only selected JSONL rollouts. Client paths are never accepted; rollout paths must remain under sessions or archived_sessions. The in-memory LRU holds three indexes. Polling appends complete records; unfinished JSONL lines wait for another read, and replaced files are reindexed.

- `token_usage_record.usage` describes one saved call; `thread_token_usage` is cumulative. Response IDs deduplicate calls. Legacy `event_msg.token_count` is a fallback, without counting repeated cumulative snapshots as calls.
- `turn_context.model/effort` describes saved requested settings. Explicit model reroutes are shown when recorded. Hidden backend model versions cannot be inferred.
- `compacted` begins a context segment from `replacement_history` and base instructions. Fresh-window reset markers remain distinct from compactions. Encrypted summaries and binary media are not estimated.
- Context history keeps up to 100 usage samples. Before/after compaction uses adjacent measured inputs, which can include unrelated changes. After compaction, the UI waits for a fresh measurement.
- Block estimates use character counts divided by four; they are not model tokenization, an exact request reconstruction, or evidence of wasted tokens. Cache is part of input and reasoning is part of output.
- Events page in batches of 120 with 16,000-character previews; full text is fetched by known event ID. Malformed complete records are reported. Missing local rollouts may remain listed but unavailable.

## Privacy and reset boundary

HTTP binds to loopback, checks Host/Origin/Sec-Fetch-Site and forwarded peers, and does not enable CORS. Mutations require a same-origin local page, JSON and an action header. CSP restricts scripts to local assets and images to approved embedded data. API responses are no-store; access logging is disabled. Known secret formats are masked; arbitrary secrets in chat text cannot be recognized reliably. HTML remains text.

Normal inspection does not copy chat content or invoke models. Browser-local state contains selection, filters, hidden IDs and layout preferences. Codex reset remains an explicitly confirmed experimental operation with version checks, process-local tool disabling and recovery settings saved under `.inspector-data/reset-results.json`. Its private directory/file protections are independent of any recording facility. `reset_chat_history.py` is a separate Codex-only command-line recovery tool and remains available; it is not invoked by the Board.
