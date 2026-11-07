// Optional-module combinations against the real demo server and browser.
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const fixture = JSON.parse(fs.readFileSync(path.join(root, 'fixtures/demo.json'), 'utf8'));
const brandedIndex = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8').replace(
  '  <!-- murmuratio-branding -->', fs.readFileSync(path.join(root, 'web/branding.html'), 'utf8').trimEnd());
const disabled = Object.fromEntries(['forecast', 'scenarios', 'routing', 'codex_credits', 'burn_rate',
  'codex_chats', 'context_reset', 'codex_wrapper', 'branding'].map(key => [key, false]));
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'inspector-demo-'));
const service = spawn('python3', ['-B', '-c', 'from murmuratio_app.server import main; main()', '--demo', '--port', '0', '--config', '/unreadable/personal.toml'],
  {cwd: root, env: {...process.env, HOME: temp, PORTLESS_URL: ''}, stdio: ['ignore', 'pipe', 'pipe']});
let browser;
(async () => {
  const base = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Demo did not start')), 10000);
    service.once('error', error => { clearTimeout(timer); reject(error); });
    service.once('exit', code => { clearTimeout(timer); reject(new Error(`Demo exited ${code}`)); });
    service.stdout.on('data', chunk => {
      const match = String(chunk).match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
  });
  browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
    args: ['--disable-background-networking', '--disable-sync', '--disable-extensions']});
  const errors = [];
  async function pageWith(config, width = 1280) {
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewport({width, height: 1000, deviceScaleFactor: width < 500 ? 2 : 1});
    await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: 'light'}]);
    if (config) {
      await page.setRequestInterception(true);
      page.on('request', request => {
        const pathname = new URL(request.url()).pathname;
        if (pathname === '/api/config') return request.respond({status: 200, contentType: 'application/json', body: JSON.stringify(config)});
        if (pathname === '/api/now') return request.respond({status: 200, contentType: 'application/json', body: JSON.stringify(fixture)});
        if (pathname === '/' && config.modules.branding) return request.respond({status: 200, contentType: 'text/html', body: brandedIndex});
        return request.continue();
      });
    }
    await page.goto(base, {waitUntil: 'networkidle0'});
    await page.waitForSelector('.now-card');
    return page;
  }
  const demo = await pageWith();
  const first = await demo.evaluate(async () => (await (await fetch('/api/now')).json()));
  const second = await demo.evaluate(async () => (await (await fetch('/api/now')).json()));
  assert.deepEqual(first, second, 'demo data is deterministic');
  const demoConfig = await demo.evaluate(async () => (await (await fetch('/api/config')).json()));
  assert.deepEqual(demoConfig.modules, disabled, 'demo matches a fresh configuration');
  assert(['forecast', 'routing', 'burn', 'credits'].every(key => !(key in first)));
  assert.equal(await demo.$$eval('.now-card.need', es => es.length), 1, 'worker approval requires attention');
  assert.equal(await demo.$eval('#now-summary', e => e.textContent.includes('1 needs attention')), true);
  fs.mkdirSync(path.join(root, '.inspector-data'), {recursive: true});
  await demo.screenshot({path: path.join(root, '.inspector-data/opensource-demo.png'), fullPage: true});
  await demo.close();
  const emptyBurn = await browser.newPage();
  await emptyBurn.evaluateOnNewDocument(() => localStorage.setItem('now-block-view', 'burn'));
  await emptyBurn.setRequestInterception(true);
  emptyBurn.on('request', request => {
    const pathname = new URL(request.url()).pathname;
    if (pathname === '/api/config') return request.respond({status: 200, contentType: 'application/json', body: JSON.stringify({modules: {...disabled, forecast: true, burn_rate: true}, project_order: [], work_hours: [9, 21]})});
    if (pathname === '/api/now') return request.respond({status: 200, contentType: 'application/json', body: JSON.stringify({...fixture, burn: {}})});
    return request.continue();
  });
  await emptyBurn.goto(base, {waitUntil: 'networkidle0'});
  await emptyBurn.waitForSelector('#now-forecast:not([hidden]) [data-option="forecast"]');
  assert.equal(await emptyBurn.$eval('#now-forecast', e => e.textContent.includes('No usage recorded this week yet')), true);
  await emptyBurn.click('[data-option="forecast"]');
  await emptyBurn.waitForSelector('.now-forecast-chart .reset');
  await emptyBurn.close();

  const retry = await browser.newPage();
  let configAttempts = 0;
  await retry.setRequestInterception(true);
  retry.on('request', request => {
    if (new URL(request.url()).pathname === '/api/config' && ++configAttempts === 1)
      request.respond({status: 503, contentType: 'application/json', body: '{"error":"unavailable"}'});
    else request.continue();
  });
  await retry.goto(base, {waitUntil: 'networkidle0'});
  await retry.waitForSelector('.now-card', {timeout: 10000});
  assert(configAttempts >= 2, 'configuration is retried after an initial failure');
  assert.equal(await retry.$eval('#now-error', e => e.hidden), true);
  await retry.close();

  const oldApproval = await browser.newPage();
  const aged = {...first, agents: first.agents.map(a => a.id === 'approval' ? {...a, last_at: first.now - 7200} : a)};
  await oldApproval.setRequestInterception(true);
  oldApproval.on('request', request => new URL(request.url()).pathname === '/api/now'
    ? request.respond({status: 200, contentType: 'application/json', body: JSON.stringify(aged)}) : request.continue());
  await oldApproval.goto(base, {waitUntil: 'networkidle0'});
  await oldApproval.waitForSelector('[data-agent-id="approval"].need');
  await oldApproval.close();

  const config = {modules: disabled, project_order: [], work_hours: [9, 21]};
  const publicPage = await pageWith(config, 390);
  assert.equal(await publicPage.$eval('#now-forecast', e => e.hidden), true);
  assert.equal(await publicPage.$eval('.source-tabs', e => e.hidden), true);
  assert.equal(await publicPage.$('.now-credits'), null);
  assert.equal(await publicPage.$('.now-gauge-logo'), null);
  assert.equal(await publicPage.$('.now-identity svg'), null);
  assert.equal(await publicPage.$eval('[data-agent-id="codex"] .now-task', e => e.hasAttribute('href')), false);
  assert.equal(await publicPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await publicPage.close();

  const forecastConfig = {modules: {...disabled, forecast: true}, project_order: ['Northstar', 'Atlas'], work_hours: [8, 18]};
  const forecast = await pageWith(forecastConfig);
  await forecast.waitForSelector('#now-forecast:not([hidden]) .now-forecast-chart');
  assert.equal(await forecast.$('[data-scenario]'), null);
  assert.equal(await forecast.$('[data-option="burn"]'), null);
  assert.equal(await forecast.$('.now-credits'), null);
  assert.equal(await forecast.$eval('#now-forecast', e => e.textContent.includes('Codex credits')), false);
  assert.equal(await forecast.$eval('#now-forecast', e => e.textContent.includes('8:00–18:00')), true);
  assert.deepEqual(await forecast.$$eval('.now-project', es => es.map(e => e.dataset.project)), ['Northstar', 'Atlas']);
  assert.equal(await forecast.$('.now-forecast-value svg'), null);
  assert.equal(await forecast.$('.reset-mark'), null);
  await forecast.close();

  const changing = {modules: {...disabled, forecast: true, branding: true}, project_order: [], work_hours: [9, 21]};
  const togglePage = await pageWith(changing);
  assert.notEqual(await togglePage.$('.now-gauge-logo'), null);
  changing.modules.branding = false;
  await togglePage.waitForFunction(() => !document.querySelector('.now-gauge-logo'), {timeout: 10000});
  assert.equal(await togglePage.$('.now-identity svg'), null);
  await togglePage.close();

  const burn = await pageWith({modules: {...disabled, burn_rate: true, branding: true}, project_order: [], work_hours: [9, 21]});
  await burn.waitForSelector('#now-forecast:not([hidden]) .now-burn-chart');
  assert.equal(await burn.$('[data-option="forecast"]'), null);
  assert.equal(await burn.$('[data-scenario]'), null);
  assert.equal(await burn.$('.now-gauge-logo use[href$="-builder"]'), null);
  await burn.close();
  assert.deepEqual(errors, []);
  assert.deepEqual(fs.readdirSync(temp), [], 'demo left HOME untouched');
  console.log('PASS: deterministic demo, approval count, public layout, independent forecast/scenarios/routing/credits/burn/branding, project order and work hours');
})().finally(async () => {
  if (browser) await closeBrowser(browser);
  if (service.exitCode === null) { service.kill(); }
  service.stdout.destroy(); service.stderr.destroy();
  fs.rmSync(temp, {recursive: true, force: true});
}).catch(error => { console.error(error); process.exitCode = 1; });
