---
name: murmuratio
description: Register orchestration and launch workers for the local board.
---
When coordinating, register this Claude session:
@M@ agent register --kind claude --id "$CLAUDE_CODE_SESSION_ID" --role orchestrator --name "<project> orchestrator"
For a native subagent, use the session and agent IDs supplied by SubagentStart and add --subagent-id <agent-id>.
Launch a worker:
@M@ spawn --to <claude|codex> --title "<title>" --model "<model>" --effort "<level>" --parent-kind claude --parent-id "$CLAUDE_CODE_SESSION_ID" [--parent-subagent-id <agent-id>] BRIEF_FILE
Omit the bracketed option for a root; use the hook-provided IDs for a subagent.
If an identity is unavailable, stop and request it.
