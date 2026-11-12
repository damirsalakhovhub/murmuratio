# Tests

From the repository root, run the standard-library Python tests:

```sh
python3 -B -m unittest discover -s tests -v
```

CI runs both Python versions in UTC. Fixed-clock fixtures scope their own timezone;
calendar regressions also cover UTC, Asia/Almaty and America/New_York, including DST.
To repeat the CI timezone locally, prefix the command with `TZ=UTC`.

The browser tests use Node.js 22.12 or newer, `puppeteer-core` and an existing Chrome or Chromium installation. They use isolated fixture data. No browser is downloaded by the test dependency.

```sh
npm ci --prefix tests
npm run test:browser --prefix tests
```

The clean-machine test installs, runs and uninstalls in a fresh home with Claude only, Codex only and neither, and checks that nothing is left behind. The real-board test stages synthetic session files and exercises the server and browser without intercepting API requests.

Set `CHROME_PATH` to the browser executable if it is not found automatically. Screenshots are written to the ignored `.murmuratio-data/` directory.

The forecast fixture allows 30 seconds for cold startup. A failure includes the
child command, elapsed time, stdout and stderr; startup error handling is tested
before the browser scenarios run.

## Release smoke

After installing test dependencies and selecting Chrome as above, run:

```sh
npm run test:smoke --prefix tests
```

This exercises the actual CLI, the default board and API in a browser, and
uninstallation in temporary homes. It checks preservation of existing and
edited Claude settings, a later Claude-only install, occupied port and invalid
config errors. On macOS, LaunchAgent commands use a stub, never the real
`launchctl`. Check any changed optional modules separately before release.

The browser suite also runs `npm run test:scale --prefix tests`, which generates
1,000 old and 200 recent sessions and checks the API, cards and live refresh.
It prints API, first-render and sampled server-memory measurements; timings are
informational and are not pass/fail thresholds.
