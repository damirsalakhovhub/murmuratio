// Provider-file states through the real board HTTP handler, without API interception.
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const {stopServer} = require('./cli_test_helpers.cjs');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const moments = Object.keys(JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/any-hour.json'))).moments);

async function run(existingBrowser) {
  const service = spawn(process.env.PYTHON || 'python3', ['-B', 'tests/any_hour_fixture.py'],
    {cwd: root, env: {...process.env, TZ: 'Asia/Almaty'}, stdio: ['ignore', 'pipe', 'pipe']});
  let browser = existingBrowser, page;
  let logs = '';
  service.stderr.on('data', chunk => { logs += chunk; });
  try {
    const base = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Any-hour fixture did not start: ${logs}`)), 10000);
      service.once('error', error => { clearTimeout(timer); reject(error); });
      service.once('exit', code => { clearTimeout(timer); reject(new Error(`Any-hour fixture exited ${code}: ${logs}`)); });
      service.stdout.on('data', chunk => {
        const match = String(chunk).match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
    });
    if (!browser) browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
      args: ['--disable-background-networking', '--disable-sync', '--disable-extensions']});
    page = await browser.newPage();
    await page.evaluateOnNewDocument(() => localStorage.setItem('now-charts', 'on'));
    await page.emulateTimezone('Asia/Almaty');
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const artifacts = path.join(root, '.murmuratio-data/any-hour-verification');
    fs.mkdirSync(artifacts, {recursive: true});
    for (const moment of moments) {
      const selected = await fetch(`${base}/fixture/${moment}`);
      assert.equal(selected.status, 204);
      await page.goto(base, {waitUntil: 'networkidle0'});
      await page.waitForSelector('.now-forecast-verdict[data-tool="codex"]');
      const state = await (await fetch(`${base}/api/now`)).json();
      const unavailable = ['empty', 'expired'].includes(moment);
      if (unavailable) {
        assert.deepEqual(await page.$$eval('.now-forecast-verdict', es => es.map(e => e.textContent)),
          ['Claudelimits unavailable', 'Codexlimits unavailable']);
        assert.equal(await page.$('.now-forecast-chart'), null);
        if (moment === 'empty') {
          assert.equal(await page.$$eval('.now-gauge', es => es.every(e => e.textContent.includes('limits unavailable'))), true);
          assert.equal(await page.$eval('.now-credits', e => e.textContent), 'Codex credits unavailable');
          assert.equal(await page.$eval('.now-empty', e => e.hidden), false);
        } else {
          assert.equal(await page.$$eval('.now-gauge-reset', es => es.every(e => e.textContent === 'reset passed')), true);
        }
      } else {
        assert.equal(await page.$$eval('.now-forecast-chart .projected', es => es.length), 2);
        assert.equal(await page.$$eval('.now-forecast-verdict', es => es.every(e => !/not enough|unavailable/i.test(e.textContent))), true);
        const serverProjection = state.forecast.codex;
        if (moment === 'fresh' || moment === 'five-minutes' || moment === 'one-hour') {
          assert.notEqual(await page.$eval('.now-credits-plan', e => e.dataset.status), 'over');
          assert.deepEqual(state.routing.codex_boost, {luna: 'sol'});
          assert.equal(await page.$eval('.now-gauge[data-tool="codex"] .now-gauge-win', e => e.textContent),
            `week ${Math.round(serverProjection.used_percent)}%`);
          assert.equal(await page.$$eval('[data-scenario]', es => es.length), 4, 'scenarios work immediately after reset');
          assert.equal(await page.$$eval('.now-forecast-fact-label', es => es.some(e => e.textContent === 'Codex credits')), true,
            'credit capacity stays available with a sparse or missing measured hourly rate');
        }
        // The browser must use the API's rate, rather than replace it with a zero live pace.
        const label = await page.$eval('.now-forecast-verdict[data-tool="codex"]', e => e.textContent);
        if (!serverProjection.hits_100_at) assert.match(label, new RegExp(`${Math.round(serverProjection.projected_at_reset)}% at reset`));
        assert.equal(await page.$$eval('.projected', es => es.some(e => /NaN|Infinity/.test(e.getAttribute('d') || ''))), false);
      }
      for (const theme of ['light', 'dark']) {
        await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: theme}]);
        for (const [width, deviceScaleFactor] of [[1280, 1], [375, 2]]) {
          await page.setViewport({width, height: 1000, deviceScaleFactor});
          await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          const layout = await page.evaluate(() => ({width: innerWidth, scroll: document.documentElement.scrollWidth,
            outside: [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > innerWidth + 1)
              .map(e => [e.tagName, e.className?.baseVal ?? e.className, e.textContent.slice(0, 80)]).slice(0, 15)}));
          assert(layout.scroll <= layout.width, `${moment} ${theme} ${width}px overflow: ${JSON.stringify(layout)}`);
          assert.equal(await page.$eval('#now-forecast', e => e.hidden), false);
          await page.waitForFunction(() => [...document.querySelectorAll('.now-forecast-verdict')]
            .every(e => e.getBoundingClientRect().height > 0));
          const clipped = await page.$$eval('#now-limits > *', es => es.filter(e => e.getBoundingClientRect().right > innerWidth).map(e => e.textContent));
          assert.deepEqual(clipped, [], `${moment} ${width}px header values remain visible`);
          if (['fresh', 'last-hour', 'empty'].includes(moment)) {
            await page.screenshot({path: path.join(artifacts, `${moment}-${theme}-${width}.png`), fullPage: true});
          }
        }
      }
      if (moment === 'fresh') {
        await page.locator('[data-scenario="codex"]').click();
        assert.match(await page.$eval('.now-forecast-verdict[data-tool="claude"]', e => e.textContent), /0% at reset/);
        await page.locator('[data-scenario="now"]').click();
      }
      await page.locator('[data-option="burn"]').click();
      if (!unavailable) {
        assert.equal(await page.$$eval('.now-burn-chart', es => es.length), 2, 'zero-use tools stay in burn view');
        if (moment === 'fresh') {
          assert.equal(await page.$$eval('.now-burn-chart', es => es.every(e => /No usage recorded/.test(e.textContent))), true);
          await page.screenshot({path: path.join(artifacts, 'fresh-burn-dark-375.png'), fullPage: true});
        }
      } else {
        assert.match(await page.$eval('#now-forecast', e => e.textContent), /No usage recorded this week yet/);
      }
      await page.locator('[data-option="forecast"]').click();
    }
    assert.deepEqual(errors, []);
    console.log('PASS: any-hour provider fixtures, routing headroom, fresh forecasts/scenarios, zero burn, unavailable states; 1280/375px DPR1/2, light/dark.');
  } finally {
    if (page) await page.close();
    if (!existingBrowser && browser) await closeBrowser(browser);
    await stopServer(service);
    service.stdout.destroy(); service.stderr.destroy();
  }
}

module.exports = run;
if (require.main === module) run().catch(error => { console.error(error); process.exitCode = 1; });
