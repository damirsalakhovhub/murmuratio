# Murmuratio — design

The application has two tabs: **Board** and **Codex chats**. Native tab buttons support arrow, Home and End navigation. The selected tab and chat persist locally. Content from session files is rendered as text.

Board uses the accepted English project columns and agent cards. Cards retain status, action, timers, context rings, ancestry and the approved Claude/Codex icons. Quota gauges and builder leader icons retain their existing routing semantics. No visual changes to these components are part of the current cleanup. Scheduled desktop sessions are excluded before card construction and parent/orchestrator assignment.

Codex keeps the MDS foundation snapshot in `web/style.css`, native system type, semantic colors, controls, disclosures and responsive behavior. The sidebar preserves pinned chats, custom sections, projects and recents. Search, archive/subagent filters, per-browser hiding and 60-row paging remain. The 200–600px sidebar resizes by pointer or keyboard; narrow screens stack vertically.

Selected chats retain the Context, Spend, Model and Compactions sections. At 900px they use a full-width context block above three columns; from 560px they use a 2×2 grid; narrower widths use one column. Each section owns its 10px gap and 20px inset (15px narrow). Disclosures retain open state during polling. The event stream loads 120 events at a time and expands full payloads on demand.

The page header is a vertical group with its own token gap. It contains the project name and connection error; the obsolete recording control has been removed. Buttons retain existing sizing. Codex reset controls and their confirmation dialog are preserved.

DOM text and native SVG graphics remain sharp at their displayed size. Browser verification covers wide/narrow layouts at DPR1/DPR2, themes, keyboard navigation and Board viewport scaling. Current results and limitations are in `VERIFICATION.md`.
