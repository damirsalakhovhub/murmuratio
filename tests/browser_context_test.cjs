// Sanitized live cards served on a separate loopback port, with real server context calculations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'murmuratio-context-'));
const fixture = path.join(home, 'snapshot.json');
const payload = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/context-signals.json')));
fs.writeFileSync(fixture, JSON.stringify(payload));
const artifacts = path.join(os.tmpdir(), 'murmuratio-context-verification');
const program = `import json,sys
from http.server import ThreadingHTTPServer
from murmuratio_app.config import Config
from murmuratio_app.now import context_signal
from murmuratio_app.server import make_handler
class Source:
 def state(self):
  with open(sys.argv[1]) as f: value=json.load(f)
  for card in value['agents']+value['completed']: card.update(context_signal(card, Config.context_high_percent))
  return value
server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(now=Source()))
print('http://127.0.0.1:'+str(server.server_port),flush=True)
server.serve_forever()`;
const service = spawn('python3', ['-B', '-c', program, fixture], {cwd: path.join(__dirname, '..'),
  env: {...process.env, HOME: home, CODEX_HOME: path.join(home, 'codex'), PORTLESS_URL: ''}, stdio: ['ignore', 'pipe', 'pipe']});
(async () => {
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Context server did not start')), 10000);
      service.once('error', reject);
      service.once('exit', code => reject(new Error(`Context server exited ${code}`)));
      service.stdout.on('data', chunk => { const match = String(chunk).match(/http:\/\/127\.0\.0\.1:\d+/); if (match) {clearTimeout(timer); resolve(match[0]);} });
    });
    browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
      args: ['--disable-background-networking', '--disable-sync', '--disable-extensions']});
    const page = await browser.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url, {waitUntil: 'networkidle0'});
    await page.waitForSelector('[data-agent-id="high"] .now-context.high');
    assert.equal(await page.$eval('[data-agent-id="high"] .now-context', e => e.textContent), 'context 84%');
    assert.equal(await page.$eval('[data-agent-id="full"] .now-context', e => e.textContent), 'context 100%');
    assert.equal(await page.$eval('[data-agent-id="below"] .now-context', e => e.textContent), '');
    assert.equal(await page.$eval('[data-agent-id="unknown"] .now-context', e => e.hidden), true);
    assert.equal(await page.$eval('[data-agent-id="finished"] .now-context', e => e.classList.contains('high')), false);
    assert.match(await page.$eval('.now-counts', e => e.textContent), /3 context high/);
    assert.match(await page.$eval('#now-summary', e => e.textContent), /3 needs attention/);
    assert.doesNotMatch(await page.$eval('.now-counts', e => e.textContent), /needs attention/);
    fs.mkdirSync(artifacts, {recursive: true});
    for (const theme of ['light', 'dark']) {
      await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: theme}]);
      for (const [width, dpr] of [[375, 2], [1280, 1]]) {
        await page.setViewport({width, height: 1000, deviceScaleFactor: dpr});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${theme} ${width}px no overflow`);
        assert(await page.$$eval('.now-context.high', es => es.every(e => {
          const expected = getComputedStyle(document.body).getPropertyValue('--now-orange').trim();
          const probe = document.createElement('span'); probe.style.color = expected; e.append(probe);
          const color = getComputedStyle(probe).color; probe.remove();
          const ring = e.querySelector('.now-context-fill').getBoundingClientRect(), label = e.lastElementChild.getBoundingClientRect();
          return getComputedStyle(e).color === color && getComputedStyle(e.querySelector('.now-context-fill')).stroke === color
            && label.left >= ring.right && label.right <= e.closest('.now-card').getBoundingClientRect().right;
        })), 'Warning token and ring/label layout');
        await page.screenshot({path: path.join(artifacts, `${theme}-${width}.png`), fullPage: true});
      }
    }
    // A card returning below threshold loses its label and all warning counts on the next poll.
    for (const card of payload.agents) card.context_used = card.id === 'unknown' ? null : 79;
    fs.writeFileSync(fixture, JSON.stringify(payload));
    await page.waitForFunction(() => !document.querySelector('.now-context.high'));
    assert.doesNotMatch(await page.$eval('.now-counts', e => e.textContent), /context high/);
    assert.match(await page.$eval('#now-summary', e => e.textContent), /0 needs attention/);
    assert.deepEqual(errors, []);
    console.log('Context browser checks passed: server signals, threshold/counters, unknown/finished, live clearing, 375/1280px light/dark.');
  } finally {
    if (browser) await closeBrowser(browser);
    service.kill(); service.stdout.destroy(); service.stderr.destroy();
    fs.rmSync(home, {recursive: true, force: true});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
