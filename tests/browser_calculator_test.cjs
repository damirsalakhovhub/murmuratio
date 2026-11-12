// The real HTTP handler and snapshots, with provider readings in a temporary home.
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const fixture = `
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from murmuratio_app.config import Config
from murmuratio_app.now import NowSource
from murmuratio_app.server import LocalHTTPServer, make_handler
from tests.test_calculator import at
from tests.routing_fixture import example_policy
with TemporaryDirectory() as home:
    config=Config.load(home)
    config.routing_policy=example_policy()
    config.modules.update(routing=True,codex_credits=True)
    resets=(at('2026-12-01T00:51'),at('2026-12-02T03:26'))
    states={}
    for name,now,used,end,extra in (
        ('surplus',at('2026-11-10T08:35'),10,at('2026-11-16T12:51'),resets),
        ('saving',at('2026-11-10T08:35'),95,at('2026-11-16T12:51'),()),
        ('reset',at('2026-11-10T08:35'),99,at('2026-11-16T12:51'),resets),
        ('last-chance',at('2026-11-30T12:00'),99,at('2026-12-01T12:51'),resets),
        ('missing',at('2026-11-10T08:35'),None,None,())):
        config.codex_resets=extra
        source=NowSource(home,config=config,clock=lambda:now)
        source.codex_limits=lambda: [] if used is None else [dict(window_minutes=10080,used_percent=used,resets_at=end)]
        source.usage_cache['codex_meter']=dict(key=(True,False),at=now,status='available',value=(dict(balance=48000.34,at=now),[],[]))
        states[name]=source.state()
    selected=['surplus']
    source=SimpleNamespace(state=lambda:states.get(selected[0]))
    base=make_handler(now=source,config=config)
    class Handler(base):
        def do_GET(self):
            if self.path.startswith('/fixture/'):
                selected[0]=self.path.removeprefix('/fixture/')
                self.send_response(204);self.end_headers();return
            super().do_GET()
    server=LocalHTTPServer(('127.0.0.1',0),Handler)
    print('http://127.0.0.1:'+str(server.server_port),flush=True)
    try: server.serve_forever()
    finally: server.server_close()
`;
async function run() {
  const service = spawn(process.env.PYTHON || 'python3', ['-B', '-c', fixture], {
    cwd: root, env: {...process.env, TZ: 'Asia/Almaty'}, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser, logs = '';
  service.stderr.on('data', chunk => { logs += chunk; });
  try {
    const base = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(logs || 'Fixture timeout')), 10000);
      service.once('error', reject);
      service.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${logs}`)); });
      service.stdout.once('data', chunk => { clearTimeout(timer); resolve(String(chunk).trim()); });
    });
    browser = await puppeteer.launch({executablePath: chromePath(), headless: true, args: ['--no-sandbox']});
    const page = await browser.newPage(), errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.emulateTimezone('Asia/Almaty');
    const artifacts = path.join(root, '.murmuratio-data/calculator');
    fs.mkdirSync(artifacts, {recursive: true});
    for (const state of ['surplus', 'saving', 'reset', 'last-chance', 'missing']) {
      await fetch(`${base}/fixture/${state}`);
      await page.goto(`${base}/calculator`, {waitUntil: 'networkidle0'});
      await page.waitForFunction(() => document.querySelector('#week-used').textContent !== '—' ||
        document.querySelector('#mode-reason').textContent.includes('unavailable'));
      const api = await (await fetch(`${base}/api/now`)).json();
      assert.equal(await page.$eval('#mode-title', e => e.textContent), api.calculator.mode.title);
      assert.equal(await page.$eval('#mode-reason', e => e.textContent), api.calculator.mode.reason);
      assert.equal(await page.$$eval('input, form, canvas, #add-reset, polyline, path', es => es.length), 0);
      // Views draw the same values as the numbers: blocks per window, rings with the live values.
      if (api.calculator.timeline)
        assert.equal(await page.$$eval('#calc-track .calc-block', es => es.length), api.calculator.timeline.windows.length);
      assert.equal(await page.$eval('#ring-resets .calc-ring-value', e => e.textContent),
        `${api.calculator.extra_resets.length}/${api.calculator.resets_total}`);
      assert.equal(await page.$eval('#calc-credits-label', e => e.textContent), '48k for December');
      assert.equal(await page.$eval('#credits-left', e => e.textContent), '48,000.3');
      assert.match(await page.$eval('#credits-expiry', e => e.textContent), /1 Jan 2027, 00:00/);
      assert.equal(await page.$eval('#resets-left', e => e.textContent), String(api.calculator.extra_resets.length));
      if (state === 'surplus') {
        assert.equal(await page.$eval('#ring-week .calc-ring-value', e => e.textContent), '10%');
        assert.equal(await page.$eval('#ring-week .calc-ring-until', e => e.textContent), 'until Mon 16');
        assert.equal(await page.$eval('#calc-resets-label', e => e.textContent), '2 resets for November');
        assert.equal(await page.$eval('#calc-resets-end', e => e.textContent), 'Dec 1');
        assert.equal(await page.$eval('.calc-block.current .calc-block-value', e => e.textContent), '10%');
        assert.equal(await page.$eval('.calculator-view', e => e.dataset.mode), 'surplus');
        assert.equal(await page.$eval('#week-used', e => e.textContent), '10%');
        assert.equal(await page.$eval('#week-left', e => e.textContent), '90%');
        assert.match(await page.$eval('#week-reset', e => e.textContent), /16 Nov 2026, 12:51, in 7 days/);
        assert.deepEqual(await page.$$eval('[data-expiry] dd', es => es.map(e => e.textContent)),
          ['1 Dec 2026, 00:51', '2 Dec 2026, 03:26']);
      }
      for (const theme of ['light', 'dark']) {
        await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: theme}]);
        for (const [width, dpr] of [[1280, 1], [375, 2]]) {
          await page.setViewport({width, height: 1000, deviceScaleFactor: dpr});
          await page.evaluate(() => document.fonts.ready);
          await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
          assert(await page.evaluate(() => [...document.fonts].some(font => font.family === 'Geist' && font.status === 'loaded')), 'local Geist loaded');
          const layout = await page.evaluate(() => ({viewport: innerWidth, scroll: document.documentElement.scrollWidth,
            blocks: [...document.querySelectorAll('.calculator-section')].map(e => {
              const r = e.getBoundingClientRect(); return {top:r.top,bottom:r.bottom};
            }), text: document.querySelector('main').innerText}));
          assert(layout.scroll <= layout.viewport, `${state} ${theme} ${width}: overflow`);
          for (let i = 1; i < layout.blocks.length; i++) assert(layout.blocks[i].top > layout.blocks[i-1].bottom);
          assert(!/[·•|]/.test(layout.text));
          // Timeline labels in one lane never overlap.
          const overlaps = await page.evaluate(() => [...document.querySelectorAll('.calc-lane')].some(lane => {
            const boxes = [...lane.children].filter(e => e.style.visibility !== 'hidden').map(e => e.getBoundingClientRect());
            return boxes.some((a, i) => boxes.some((b, j) => i < j && a.top === b.top && a.left < b.right && b.left < a.right));
          }));
          assert(!overlaps, `${state} ${theme} ${width}: timeline labels overlap`);
          if (['surplus', 'reset', 'saving'].includes(state))
            await page.screenshot({path: path.join(artifacts, `${state}-${theme}-${width}.png`), fullPage: true});
        }
      }
    }
    await fetch(`${base}/fixture/surplus`);
    await page.goto(`${base}/calculator`, {waitUntil: 'networkidle0'});
    // The view switch is remembered across loads.
    assert.equal(await page.$eval('#calc-rings', e => e.hidden), true);
    await page.click('[data-view="rings"]');
    await page.reload({waitUntil: 'networkidle0'});
    assert.equal(await page.$eval('#calc-rings', e => e.hidden), false);
    assert.equal(await page.$eval('#calc-timeline', e => e.hidden), true);
    assert.equal(await page.$eval('[data-view="rings"]', e => e.getAttribute('aria-checked')), 'true');
    for (const theme of ['light', 'dark']) {
      await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: theme}]);
      for (const [width, deviceScaleFactor] of [[1280, 1], [375, 2]]) {
        await page.setViewport({width, height: 1000, deviceScaleFactor});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({path: path.join(artifacts, `rings-${theme}-${width}.png`), fullPage: true});
      }
    }
    await page.click('[data-view="timeline"]');
    // The radio switch has one tab stop and supports keyboard selection.
    await page.focus('[data-view="timeline"]');
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.$eval('#calc-rings', e => e.hidden), false);
    assert.equal(await page.$eval('[data-view="rings"]', e => e === document.activeElement && e.tabIndex === 0), true);
    assert.equal(await page.$eval('[data-view="timeline"]', e => e.tabIndex), -1);
    await page.keyboard.press('Home');
    assert.equal(await page.$eval('#calc-timeline', e => e.hidden), false);
    // Exercise the page's actual periodic refresh, including failure and recovery.
    await fetch(`${base}/fixture/unavailable`);
    await page.waitForFunction(() => !document.querySelector('#calculator-error').hidden, {timeout: 12000});
    await fetch(`${base}/fixture/reset`);
    await page.waitForFunction(() => document.querySelector('#calculator-error').hidden &&
      document.querySelector('#mode-title').textContent === 'Press a reset now', {timeout: 12000});
    assert.deepEqual(errors, []);
    console.log('Calculator: 5 states, 2 themes, 375px/DPR2 and 1280px/DPR1, views/switch/refresh/recovery/navigation/font passed.');
  } finally {
    if (browser) await closeBrowser(browser);
    service.kill('SIGTERM');
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
