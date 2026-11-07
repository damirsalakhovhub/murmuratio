# Board rules

These are the target behaviors for saved local data. Status and time are estimates,
not proof that a process is alive. Tests are being added for the outstanding gaps.

## Cards

- **B1 Discovery.** Show eligible saved Claude sessions and Codex workers in their
  project. Skip known archived and scheduled Claude sessions. Codex discovery
  scans saved rollouts modified within 24 hours; unsaved or older quiet sessions
  may be missing, even when still running.
- **B2 Identity.** One observed session has one card. Merge wrapper and rollout
  observations only on a shared identity; distinct runs may belong to one thread.
  Never merge unrelated sessions because values from different ID namespaces match.
- **B3 Codex chats.** Exec workers, native subagents and Claude-linked runs are
  cards. Interactive chats are hidden by default; their running count is shown.
  The board setting can show them as cards.
- **B4 Claude subagents.** Agent-tool sidechains do not make separate cards.
- **B5 Tree.** Use recorded parent links, or a unique cwd/time match marked inferred.
  Ambiguous links stay standalone. Do not expose a hidden chat to place a child.
- **B6 Handoff.** A linked same-title Claude orchestrator successor replaces its
  predecessor and takes its children. Other handoffs are not inferred from title alone.

## Status

- **S1 Running.** Show the state supported by saved turns. Label quiet work; silence
  alone never means approval. A quiet Claude turn can be stale or already stopped.
- **S2 Attention.** Mark recognizable approvals, failures and blocked work using
  source evidence. Do not guess approval from silence or claim an unobserved wait.
- **S3 Finished turn.** An idle orchestrator or interactive chat can show Turn
  finished; completed workers show Done. Distinguish errors and cancellations.
- **S4 Stalled.** A discovered unfinished Codex turn with no file update for over
  10 minutes is stalled. After over 30 minutes without an event, if no rollout
  writer is found, mark it as possibly stopped. A failed probe is not proof of death.
  Do not call a normally finished chat crashed for being idle.
- **S5 Retention.** A finished worker remains visible for 30 minutes, including
  across midnight. Keep discoverable ancestors while a child remains active.

## Time

- **T1 Duration.** A worker shows the sum of its known turn intervals, excluding
  gaps. A running orchestrator shows its current turn. A finished turn does not
  grow. Omit an unknown duration rather than displaying an invented zero.
- **T2 Clock.** Times and the daily done count use local time; the count resets at
  local midnight. Server and browser must agree on the date used for retention.

## Robustness

- **R1 Damaged data.** Isolate malformed or incomplete records and files; healthy
  sessions remain visible. Retry partial writes. If a large record cannot be
  interpreted safely, do not retain an earlier state as if it were confirmed.
- **R2 Format drift.** Extra fields and JSON member order do not change recognized
  bounded records. An unrecognized task/turn lifecycle is visibly unsupported;
  unknown future formats may require an adapter update.
- **R3 Source health.** An absent optional provider is normal. An existing unreadable
  or unsupported source is identified as a problem, not as an empty board.
- **R4 Freshness.** A quota sample older than 30 minutes, or past its reset, is
  stale. Show that state and exclude it from forecasts and routing. Unknown age
  is not fresh. Forecasts are estimates based on the remaining fresh data.
- **R5 Writes.** Normal inspection does not change vendor sessions or settings.
  Enabled history/routing modules write only to documented paths. Experimental
  reset can change Codex data only after explicit confirmation.
- **R6 Install.** Preserve existing settings and user edits. Restore the original
  status line on uninstall; restore exact settings bytes when they have not changed.
  Report conflicts instead of overwriting changed files; document retained data.
- **R7 Privacy.** Normal inspection stays local. Diagnostic sharing is optional;
  do not include raw transcripts, prompts, tokens or paths by default.
- **R8 Live view.** Show connection failure and when rendering has been paused
  for over 10 seconds. Resume with the newest snapshot after recovery or reading.
- **R9 Options.** Optional modules start off and do no module-specific reads or
  writes while disabled. The chat-card setting is independent of the chat viewer.

## How changes land

1. For changed user behavior, update the relevant rule and its evidence together.
2. Reproduce a bug at the smallest useful layer; add a regression with the fix.
3. Use staged provider-format data through the real API and browser for key paths.
4. CI runs Python 3.11 and 3.13 and the browser suite from a clean checkout.
5. Before release, require green checks and the [fresh-home install, board and
   uninstall smoke](../tests/README.md#release-smoke); check changed optional
   modules and document known limits.
6. Collect privacy-reviewed diagnostics only when a reporter chooses to share them.
7. Before restarting a live board after a code change, run the synthetic corpus
   test and `./murmuratio check-update` against its local URL. For board UI
   changes, also run `node tests/browser_real_board_test.cjs`. Restart only
   after the applicable checks pass.
