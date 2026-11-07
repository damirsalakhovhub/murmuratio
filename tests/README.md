# Tests

From the repository root, run the standard-library Python tests:

```sh
python3 -B -m unittest discover -s tests -v
```

The browser tests use Node.js 18 or newer, `puppeteer-core` and an existing Chrome or Chromium installation. They use isolated fixture data. No browser is downloaded by the test dependency.

```sh
npm ci --prefix tests
npm run test:browser --prefix tests
```

Set `CHROME_PATH` to the browser executable if it is not found automatically. Screenshots are written to the ignored `.inspector-data/` directory.
