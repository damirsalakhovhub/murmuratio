# murmuratio — verification

Finalized 2026-11-07 on `main`.

- The new CLI migrates legacy configuration and data without replacing existing destinations, preserves previous statusLine settings and migrates an owned LaunchAgent. The deprecated command delegates to the new CLI. Uninstall removes both sets of directories and can resume after interrupted cleanup.
- Isolated-home tests cover both destinations, partial migrations, conflicts, dry runs, explicit old configuration paths, statusLine continuity, LaunchAgent recovery, and cleanup failures. Independent reviewers also exercised the previous installer and checked test integrity.
- `python3 -B -m unittest discover`: 159 passed, 0 failed. The browser suite's three scenarios passed with isolated demo data; they cover module settings, Board behavior, chat pagination, responsive sizes, DPR1/DPR2 and reduced motion.
- Rendered the demo Board and the landing page at desktop and mobile sizes. Verified the copied agent link and installation commands. JavaScript syntax checks and `git diff --check` passed.
- Tracked files were checked for local user paths and personal project names. The MIT license and public credit and GitHub links are retained.

No OS restart or login test was performed.
