# murmuratio — verification

Finalized 2026-11-07 on `main`.

- The new CLI migrates legacy configuration and data without replacing existing destinations, preserves previous statusLine settings and migrates an owned LaunchAgent. The deprecated command delegates to the new CLI. Uninstall removes both sets of directories and can resume after interrupted cleanup.
- Isolated-home tests cover both destinations, partial migrations, conflicts, dry runs, explicit old configuration paths, statusLine continuity, LaunchAgent recovery, and cleanup failures. Independent reviewers also exercised the previous installer and checked test integrity.
- `python3 -B -m unittest discover`: 159 passed, 0 failed. The browser suite's three scenarios passed with isolated demo data; they cover module settings, Board behavior, chat pagination, responsive sizes, DPR1/DPR2 and reduced motion.
- Rendered the demo Board and the landing page at desktop and mobile sizes. Verified the copied agent link and installation commands. JavaScript syntax checks and `git diff --check` passed.
- Tracked files were checked for local user paths and personal project names. The MIT license and public credit and GitHub links are retained.

No OS restart or login test was performed.

## English-only finalization, 2026-11-07

- Translated the remaining UI, accessible labels, API errors, reset status messages, CLI prompts, adapter labels and test data. Both tabs use English, with English number and date formats. Layout and behavior are preserved; Python AST comparison confirmed that all 11 changed Python files differ only in string values.
- User content stays as recorded. The only Cyrillic in tracked text is the legacy error-status comparison at `web/app.js:50` and its input fixture at `browser_codex_test.cjs:109`. The browser regression checks legacy and English error statuses, error styling and reply summaries.
- Full checks ran without a sandbox: `python3 -B -m unittest discover -v` passed all 159 tests with no failures or skips; `npm run test:browser` passed all three suites. The Codex suite passed again after adding the legacy-status regression and reset-dialog screenshots. JavaScript syntax checks and `git diff --check` passed.
- Inspected the rendered Codex view and reset dialog at 1440px/DPR1 and 390px/DPR2, plus the fixture Board and live Board. English text wraps within the existing layout. Screenshots are in the ignored `.inspector-data/codex-verification/` directory and `.inspector-data/english-live.png`. Safari was not checked.
- Restarted the existing `dev.local-sites.murmuratio` LaunchAgent, confirmed its active Portless route and verified `http://agent-inspector.localhost:1355/api/now` returned 200. The live page returned 200, used `lang="en"` and had no JavaScript errors.
