# Architecture

`murmuratio` calls the CLI in `murmuratio_app/cli.py`. The server in `murmuratio_app/server.py` serves the local interface in `web/` and its JSON API. `statusline_wrapper.py` is the stable callback path used by installed Claude Code settings; its implementation lives in the package.

Discovery finds local sources. Adapters convert saved records into observations defined in `schema.py`; `now.py` combines them with grouping and metrics for the board. Missing or incompatible sources are reported without hiding healthy sources. Status and usage are inferred from saved data, and optional sources are read only when enabled.

The default board reads recent JSONL rollouts independently of SQLite. It scans relevant records on the first read and incrementally consumes subsequent complete records. Claude Bash results can identify a Codex thread exactly; unique cwd and launch-time matches are inferred. Optional Codex chat metadata comes from read-only SQLite; event indexes use JSONL rollouts restricted to the Codex session directories. The server keeps a small in-memory index cache and pages event text. Context reset is a separate, explicitly confirmed operation. See [SECURITY.md](../SECURITY.md) for the read, write and network boundaries.
