// A synthetic quota snapshot exercises chart preferences in a real browser.
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const {fixtureURL} = require('./fixture_startup.cjs');
const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'murmuratio-forecast-'));
const config = path.join(home, 'config.toml');
fs.writeFileSync(config, '[modules]\nforecast=true\nrouting=true\ncodex_credits=true\nbranding=true\n');
const snapshot = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/forecast-board.json')));
const service = spawn('python3', ['-B', '-c', 'from murmuratio_app.server import main; main()', '--port', '0', '--config', config],
  {cwd: root, env: {...process.env, HOME: home, CODEX_HOME: path.join(home, 'codex'), PORTLESS_URL: ''}, stdio: ['ignore', 'pipe', 'pipe']});
(async () => {
  let browser;
  try {
    const base = await fixtureURL(service);
    browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
      args: ['--disable-background-networking', '--disable-sync', '--disable-extensions']});
    const artifacts = path.join(root, '.murmuratio-data/forecast-preference');
    fs.mkdirSync(artifacts, {recursive: true});
    for (const width of [375, 1280]) for (const theme of ['light', 'dark']) {
      const context = await browser.createBrowserContext();
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.setViewport({width, height: 900, deviceScaleFactor: width === 375 ? 2 : 1});
      await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: theme}, {name: 'prefers-reduced-motion', value: 'reduce'}]);
      await page.setRequestInterception(true);
      page.on('request', request => new URL(request.url()).pathname === '/api/now'
        ? request.respond({status: 200, contentType: 'application/json', body: JSON.stringify(snapshot)}) : request.continue());
      const ready = () => page.waitForSelector('#charts-toggle:not([hidden])');
      await page.goto(base, {waitUntil: 'networkidle0'}); await ready();
      assert.equal(await page.$eval('#now-forecast', e => e.hidden), true, 'fresh browser starts with charts collapsed');
      assert.equal(await page.$eval('#charts-toggle', e => e.getAttribute('aria-pressed')), 'false');
      const header = () => page.evaluate(() => ({limits: document.querySelector('#now-limits').textContent,
        counts: document.querySelector('#now-summary').textContent,
        visible: [...document.querySelectorAll('#now-summary, .now-gauge, .now-health-row')].every(e => e.getBoundingClientRect().height > 0)}));
      const before = await header(); assert(before.visible);
      assert.equal(await page.$$eval('.now-health-row', es => es.length), 2);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({path: path.join(artifacts, `${width}-${theme}-collapsed.png`), fullPage: true});
      await page.click('#charts-toggle');
      await page.waitForSelector('#now-forecast:not([hidden]) .now-forecast-chart');
      assert.equal(await page.evaluate(() => localStorage.getItem('now-charts')), 'on');
      assert.deepEqual(await header(), before, 'limits, pace bars and counters survive expanding');
      await page.reload({waitUntil: 'networkidle0'}); await ready();
      assert.equal(await page.$eval('#now-forecast', e => e.hidden), false, 'explicit open survives reload');
      await page.$eval('#charts-toggle', e => e.focus()); await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelector('#now-forecast').hidden);
      await page.reload({waitUntil: 'networkidle0'}); await ready();
      assert.equal(await page.$eval('#now-forecast', e => e.hidden), true, 'explicit collapse survives reload');
      assert.equal(await page.evaluate(() => localStorage.getItem('now-charts')), 'off');
      assert.deepEqual(await header(), before);
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log('Forecast preferences: collapsed default, one-click open, both choices persist, header unchanged, keyboard; 375/1280px light/dark passed.');
  } finally {
    if (browser) await closeBrowser(browser);
    service.kill(); service.stdout.destroy(); service.stderr.destroy();
    fs.rmSync(home, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
