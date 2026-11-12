# Changelog

## 0.3.0-beta — 2026-11-12

### Added

- Optional exact agent links in both cross-provider and same-provider orchestration directions, with explicit caller and native Claude subagent identities.
- Locked, complete metadata snapshots in a private local registry, alongside the unchanged legacy registry and wrapper readers.
- `agent register` and `spawn`, with brief input on stdin, literal launcher titles, separate requested/effective model settings, process identity and exit status.
- Opt-in Claude lifecycle hooks and orchestration skill, journaled installation/removal, and a manually pasted Codex instructions block.
- Four-direction stub launches verified through the board API, cancellation, concurrent writes, hook failures, and byte-preserving settings recovery tests.

## 0.2.0-beta — 2026-11-12

### Removed

- The Codex chat tab and experimental context-reset controls. The board reads saved sessions and does not change conversations or reset context.
- Rune development and synchronization from the active release workflow; the web board is the supported interface.
- Unused full assistant-text `last_action` payloads and unrelated naming fixture content.

### Added

- Native Claude subagent cards with exact parent links, brief panels and worker usage classification.
- Recorded model and effort on cards, plus an unfinished-context attention signal at 80%, configurable with `[board] context_high_percent`.
- Snapshot timestamps and refresh-failure state, with one quiet header age line after two refresh intervals.
- Fault isolation for damaged records, unreadable files and quota history; bounded large-record reads.
- Both Codex thread-title schemas, complete dry-run path inventory and resumable uninstall cache cleanup.
- Local-account trust-boundary documentation and separate foreground/LaunchAgent lifecycle instructions.
- Regression tests, fresh-home install/uninstall smoke and 1,000-session scale checks.

### Changed

- Named or registered Claude and Codex orchestrators stay visible without children. Other lone chats are hidden until they host unfinished agents or agents finished today; an empty board means no eligible agents.
- Naming priority is Desktop title, registry name, wrapper title, subagent description, then meaningful task text (Claude's first prompt or Codex's latest user task).
- Finished workers remain visible for 30 minutes; unfinished Codex workers stay discoverable while quiet. Source failures preserve healthy cards and identify unreadable input.
- Single-provider setups hide the absent provider's limits and unavailable Calculator entry. Demo keeps deterministic board data and hides Calculator.
- Charts start collapsed and can be shown independently; hidden columns and chart preferences stay in browser storage.
- All seven optional modules and live Codex limit reads default to off. Quota history is read only for modules that need it. Derived data defaults to `~/.local/share/murmuratio/`.
- Stale quota values retain their age; known resets bring optional live-limit reads forward. Routing policy, tier labels and credit reservations are configurable.
