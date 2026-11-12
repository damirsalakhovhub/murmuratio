When coordinating, register with @M@ agent register --kind codex --id "$CODEX_THREAD_ID" --role orchestrator --name "<project> orchestrator".
Launch with @M@ spawn --to <claude|codex> --title "<title>" --model "<model>" --effort "<level>" --parent-kind codex --parent-id "$CODEX_THREAD_ID" BRIEF_FILE.
If CODEX_THREAD_ID is unavailable, request the current thread ID; do not use an inherited Claude ID.
