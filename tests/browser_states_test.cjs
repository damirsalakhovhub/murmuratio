// Focused board state regressions in a real browser, without provider data.
const assert = require('node:assert/strict');
const path = require('node:path');
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');

(async () => {
  const browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
    args: ['--disable-background-networking', '--disable-sync', '--disable-extensions']});
  try {
    const page = await browser.newPage();
    await page.emulateTimezone('America/Los_Angeles');
    await page.setContent('<div id="now-board"></div><div id="now-summary"></div><div id="now-limits"></div>' +
      '<p id="now-error" hidden></p><div id="now-forecast"></div><button id="charts-toggle"></button>' +
      '<button id="columns-toggle"></button><div id="columns-menu" hidden></div><span id="columns-hidden-count"></span>');
    await page.addScriptTag({path: process.argv[2] || path.join(__dirname, '../web/now.js')});
    const results = await page.evaluate(async () => {
      const node = (tag, name, value) => {
        const element = document.createElement(tag);
        if (name) element.className = name;
        if (value != null) element.textContent = value;
        return element;
      };
      const text = (element, value) => { element.textContent = value; };
      const originalTimeout = window.setTimeout;
      window.setTimeout = (callback, delay, ...args) => originalTimeout(callback, delay === 10000 ? 10 : delay, ...args);
      const view = window.createNowView({node, text});
      view.configure({modules: {}, project_order: []});
      const now = Date.UTC(2026, 10, 7) / 1000 + 60;
      const agent = (id, extra = {}) => ({id, project: 'Test', kind: 'codex', task: id,
        folder: 'test', started: now - 300, last_at: now - 10, status: 'working', ...extra});
      const snapshot = {now, local_day: {start: now - 60, end: now - 60 + 86400},
        agents: [agent('unknown', {status: 'unknown', seconds: null}), agent('quiet', {quiet: true, seconds: null})],
        completed: [], limits: {codex: [{window_minutes: 300, used_percent: 10, resets_at: now + 3600}]},
        sources: {codex_limits: {status: 'stale'}}};
      view.render(snapshot);
      const card = id => document.querySelector(`[data-agent-id="${id}"]`);
      const checks = {
        R2: card('unknown').querySelector('.now-word').textContent === 'Unknown',
        T1: card('unknown').querySelectorAll('.now-time span').length === 1,
        S1: /quiet/i.test(card('quiet').querySelector('.now-current')?.textContent || ''),
        R4: /stale/i.test(document.querySelector('[data-tool="codex"]').textContent),
      };
      view.render({now, agents: [], completed: [], sources: {codex_sessions: {status: 'unsupported'}}});
      checks.R3 = document.querySelector('.now-empty').hidden && !document.querySelector('#now-error').hidden
        && /format not recognized/.test(document.querySelector('#now-error').textContent);
      view.render({now, agents: [], completed: [], sources: {
        codex_wrapper: {status: 'unavailable', reason: 'permission_denied'}}});
      checks.R3_reason = document.querySelector('#now-error').textContent === "Codex wrapper logs can't be read: permission denied.";
      view.render({now, agents: [], completed: [], sources: {codex_wrapper: {status: 'absent'}}});
      checks.R3_absent = document.querySelector('#now-error').hidden && !document.querySelector('.now-empty').hidden;
      view.render({now, local_day: snapshot.local_day, agents: [], completed: [agent('midnight', {
        status: 'done', completed: true, ended: now - 360})]});
      checks.T2 = /0 done today/.test(document.querySelector('#now-summary').textContent);
      document.querySelector('#now-board').dispatchEvent(new Event('pointerenter'));
      view.render({...snapshot, agents: [agent('latest')]});
      await new Promise(resolve => originalTimeout(resolve, 30));
      const paused = /paused/i.test(document.querySelector('#now-error').textContent);
      view.error();
      const disconnected = /Reconnecting/.test(document.querySelector('#now-error').textContent);
      checks.R8 = paused && disconnected && /paused/i.test(document.querySelector('#now-error').textContent);
      return checks;
    });
    const failures = Object.entries(results).filter(([, passed]) => !passed).map(([name]) => name);
    assert.deepEqual(failures, [], `Board state regressions: ${failures.join(', ')}`);
    console.log(`Board state regressions passed: ${Object.keys(results).join(', ')}`);
  } finally {
    await closeBrowser(browser);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
