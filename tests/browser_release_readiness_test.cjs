// Audit regressions against the real board markup, stylesheet and view.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const root = path.resolve(__dirname, '..');

(async () => {
  const browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
    args: ['--disable-background-networking', '--disable-sync', '--disable-extensions']});
  try {
    for (const width of [375, 1280]) for (const theme of ['light', 'dark']) {
      const page = await browser.newPage();
      await page.setViewport({width, height: 900, deviceScaleFactor: width === 375 ? 2 : 1});
      await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: theme}]);
      const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8')
        .replace(/<script[^>]*><\/script>/g, '').replace(/<link[^>]*>/g, '');
      await page.setContent(html);
      await page.addStyleTag({path: path.join(root, 'web/style.css')});
      await page.addScriptTag({path: path.join(root, 'web/now.js')});
      const result = await page.evaluate(() => {
        let clock = Date.now();
        Date.now = () => clock;
        const timers = [], interval = window.setInterval;
        window.setInterval = (callback, delay) => {
          timers.push({callback, delay});
          return interval(callback, delay);
        };
        const node = (tag, name = '', value) => {
          const el = document.createElement(tag); el.className = name;
          if (value !== undefined) el.textContent = value;
          return el;
        };
        const view = createNowView({node, text: (el, value) => { el.textContent = value; }});
        view.configure({modules: {}});
        const now = Date.now() / 1000;
        const worker = {id: 'example', kind: 'claude', project: 'Example', folder: 'example',
          status: 'working', task: 'Review example', started: now - 300, last_at: now};
        const snapshot = {now, snapshot_at: now - 180, refresh_failed: true, agents: [worker], completed: [],
          limits: {claude: [{window_minutes: 300, used_percent: 10, resets_at: now + 3600}]},
          sources: {codex_sessions: {status: 'absent'}, codex_limits: {status: 'absent'}}};
        view.render(snapshot);
        const stale = document.querySelector('#now-freshness');
        const link = document.querySelector('a[href="/calculator"]');
        const checks = {
          age: stale && !stale.hidden && /^Data is 3 min old$/.test(stale.textContent),
          header: Boolean(stale?.closest('.source-bar')),
          noAbsentLimits: !document.querySelector('.now-gauge[data-tool="codex"]'),
          noEmptyCalculator: link.hidden,
          noOverflow: document.documentElement.scrollWidth <= innerWidth,
        };
        view.render({...snapshot, snapshot_at: now, refresh_failed: false,
          calculator: {used_percent: 0}, limits: {codex: [{window_minutes: 10080, used_percent: 0, resets_at: now + 86400}]},
          agents: [{...worker, kind: 'codex'}], sources: {claude_sessions: {status: 'absent'}}});
        checks.recovered = stale?.hidden === true;
        checks.codexCalculator = !link.hidden;
        checks.noClaudeLimits = !document.querySelector('.now-gauge[data-tool="claude"]');
        view.render({...snapshot, snapshot_at: now - 5, refresh_failed: true});
        checks.twoIntervals = stale?.hidden === true;
        view.render({...snapshot, snapshot_at: now - 7});
        checks.afterTwoIntervals = stale?.hidden === false;
        view.render({...snapshot, snapshot_at: now, refresh_failed: false});
        view.error();
        clock += 7000;
        const timer = timers.find(timer => timer.delay === 3000);
        timer?.callback();
        checks.disconnectedAge = Boolean(timer) && stale?.hidden === false;
        view.render(snapshot);
        document.querySelector('#now-board').dispatchEvent(new Event('pointerenter'));
        view.render({...snapshot, snapshot_at: clock / 1000, agents: [{...worker, id: 'queued'}]});
        checks.pausedAge = stale?.hidden === false && Boolean(document.querySelector('[data-agent-id="example"]'));
        document.querySelector('#now-board').dispatchEvent(new Event('pointerleave'));
        checks.appliedAge = stale?.hidden === true && Boolean(document.querySelector('[data-agent-id="queued"]'));
        view.render(snapshot);
        return checks;
      });
      assert.deepEqual(Object.entries(result).filter(([, ok]) => !ok).map(([key]) => key), [], `${width}px ${theme}`);
      const artifacts = path.join(root, '.murmuratio-data/release-readiness');
      fs.mkdirSync(artifacts, {recursive: true});
      await page.screenshot({path: path.join(artifacts, `${width}-${theme}.png`), fullPage: true});
      await page.close();
    }
    console.log('Release readiness: snapshot age/recovery, two refresh intervals, single-provider limits and calculator; 375/1280px light/dark passed.');
  } finally { await closeBrowser(browser); }
})().catch(error => { console.error(error); process.exitCode = 1; });
