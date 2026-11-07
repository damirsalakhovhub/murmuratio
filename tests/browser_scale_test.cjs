// Deterministic saved histories through the actual CLI, API and browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {performance} = require('node:perf_hooks');
const {spawnSync} = require('node:child_process');
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const {isolatedEnv, startServer, stopServer} = require('./cli_test_helpers.cjs');

const oldCount = 1000, activeCount = 150, doneCount = 25, chatCount = 25;
const recentCount = activeCount + doneCount + chatCount;
const id = index => `recent-${String(index).padStart(3, '0')}`;
const event = (type, timestamp, fields = {}) => ({type: 'event_msg', timestamp, payload: {type, ...fields}});

function stage(home) {
  const directory = path.join(home, '.codex/sessions/2026/11/07');
  fs.mkdirSync(directory, {recursive: true});
  const now = Date.now() / 1000;
  let size = 0;
  function write(name, records, modified) {
    const file = path.join(directory, `rollout-${name}.jsonl`);
    const contents = records.map(record => JSON.stringify(record)).join('\n') + '\n';
    fs.writeFileSync(file, contents);
    fs.utimesSync(file, modified, modified);
    size += Buffer.byteLength(contents);
  }
  const oldPadding = 'x'.repeat(10000), recentPadding = 'y'.repeat(8000);
  for (let index = 0; index < oldCount; index++) {
    const name = `old-${String(index).padStart(4, '0')}`;
    write(name, [
      {type: 'session_meta', timestamp: now - 90000, payload: {id: name, source: 'exec', cwd: '/synthetic/old'}},
      event('task_started', now - 90000),
      {type: 'response_item', timestamp: now - 90000, payload: {type: 'function_call_output', output: oldPadding}},
    ], now - 90000);
  }
  for (let index = 0; index < recentCount; index++) {
    const name = id(index), chat = index >= activeCount + doneCount;
    const cwd = `/synthetic/project-${index % 5}`;
    const records = [
      {type: 'session_meta', timestamp: now - 60, payload: {id: name, source: chat ? 'cli' : 'exec', cwd}},
      event('task_started', now - 59),
      event('user_message', now - 58, {message: `Synthetic task ${name}`}),
      {type: 'response_item', timestamp: now - 57, payload: {type: 'function_call_output', output: recentPadding}},
    ];
    if (index >= activeCount && !chat) records.push(event('task_complete', now));
    write(name, records, now);
  }
  return {directory, size};
}

async function api(url) {
  const started = performance.now();
  const response = await fetch(`${url}/api/now`);
  assert.equal(response.status, 200);
  const body = await response.json();
  return {body, ms: performance.now() - started};
}

function cards(snapshot) {
  return new Map([...snapshot.agents, ...snapshot.completed].map(agent => [agent.id, agent]));
}

function withoutClock(agent) {
  const result = {...agent};
  delete result.seconds; // A running turn's duration advances naturally.
  return result;
}

function checkSnapshot(snapshot) {
  assert.equal(snapshot.agents.length, activeCount);
  assert.equal(snapshot.completed.length, doneCount);
  assert.deepEqual(snapshot.codex_chats, {hidden: chatCount, running: chatCount});
  const actual = cards(snapshot);
  const expected = Array.from({length: activeCount + doneCount}, (_, index) => `codex:${id(index)}`);
  assert.deepEqual([...actual.keys()].sort(), expected.sort());
  assert.deepEqual([...new Set([...actual.values()].map(agent => agent.project))].sort(),
    ['project-0', 'project-1', 'project-2', 'project-3', 'project-4']);
  for (let index = 0; index < activeCount + doneCount; index++) {
    const agent = actual.get(`codex:${id(index)}`);
    assert.equal(agent.task, `Synthetic task ${id(index)}`);
    assert.equal(agent.status, index < activeCount ? 'working' : 'done');
    assert.equal(agent.project, `project-${index % 5}`);
  }
}

async function main() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'murmuratio-scale-'));
  let browser, running, sampler;
  try {
    const env = isolatedEnv(home);
    const corpus = stage(home);
    assert(corpus.size >= 10 * 1024 * 1024 && corpus.size <= 20 * 1024 * 1024);
    running = await startServer(env);
    let peakRss = 0;
    const sample = () => {
      const result = spawnSync('ps', ['-o', 'rss=', '-p', String(running.server.pid)], {encoding: 'utf8'});
      if (result.status === 0) peakRss = Math.max(peakRss, Number(result.stdout.trim()) || 0);
    };
    sample();
    sampler = setInterval(sample, 100);

    const cold = await api(running.url);
    checkSnapshot(cold.body);
    const warm = await api(running.url);
    checkSnapshot(warm.body);

    browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
      userDataDir: path.join(home, 'browser'),
      args: ['--no-sandbox', '--disable-background-networking', '--disable-sync', '--disable-extensions']});
    const page = await browser.newPage();
    const renderStarted = performance.now();
    await page.goto(running.url, {waitUntil: 'domcontentloaded'});
    await page.waitForFunction(count => document.querySelectorAll('[data-agent-id]').length === count,
      {}, activeCount + doneCount);
    const renderMs = performance.now() - renderStarted;
    const rendered = await page.$$eval('[data-agent-id]', elements => elements.map(element => element.dataset.agentId));
    assert.deepEqual(rendered.sort(), [...cards(warm.body).keys()].sort());
    const statuses = await page.$$eval('[data-agent-id]', elements => Object.fromEntries(
      elements.map(element => [element.dataset.agentId, element.querySelector('.now-word').textContent])));
    const summary = await page.$eval('#now-summary', element => element.textContent);
    const doneToday = warm.body.completed.filter(agent => agent.ended >= warm.body.local_day.start
      && agent.ended < warm.body.local_day.end).length;
    assert(summary.includes(`${activeCount} running`));
    assert(summary.includes(`${doneToday} done today`));
    assert(summary.includes(`Your Codex chats: ${chatCount}`));

    const target = `codex:${id(0)}`;
    const file = path.join(corpus.directory, `rollout-${id(0)}.jsonl`);
    fs.appendFileSync(file, `${JSON.stringify(event('task_complete', Date.now() / 1000))}\n`);
    const appended = await api(running.url);
    const before = cards(warm.body), after = cards(appended.body);
    assert.deepEqual([...after.keys()].sort(), [...before.keys()].sort());
    assert.equal(appended.body.agents.length, activeCount - 1);
    assert.equal(appended.body.completed.length, doneCount + 1);
    assert.deepEqual(appended.body.codex_chats, warm.body.codex_chats);
    assert.equal(after.get(target).status, 'done');
    for (const [key, value] of before) {
      if (key !== target) assert.deepEqual(withoutClock(after.get(key)), withoutClock(value), key);
    }
    await page.waitForFunction(id => {
      const card = [...document.querySelectorAll('[data-agent-id]')].find(element => element.dataset.agentId === id);
      return card?.querySelector('.now-word')?.textContent === 'Done';
    }, {}, target);
    const updatedStatuses = await page.$$eval('[data-agent-id]', elements => Object.fromEntries(
      elements.map(element => [element.dataset.agentId, element.querySelector('.now-word').textContent])));
    statuses[target] = 'Done';
    assert.deepEqual(updatedStatuses, statuses);
    const updatedSummary = await page.$eval('#now-summary', element => element.textContent);
    const doneAfter = appended.body.completed.filter(agent => agent.ended >= appended.body.local_day.start
      && agent.ended < appended.body.local_day.end).length;
    assert(updatedSummary.includes(`${activeCount - 1} running`));
    assert(updatedSummary.includes(`${doneAfter} done today`));
    sample();
    console.log(`Scale: ${oldCount} old + ${recentCount} recent, ${(corpus.size / 1048576).toFixed(1)} MiB, ${activeCount + doneCount} cards, ${chatCount} hidden chats`);
    console.log(`Scale timings: cold API ${cold.ms.toFixed(0)} ms, warm API ${warm.ms.toFixed(0)} ms, append API ${appended.ms.toFixed(0)} ms, first render ${renderMs.toFixed(0)} ms, sampled server peak RSS ${(peakRss / 1024).toFixed(1)} MiB`);
  } finally {
    if (sampler) clearInterval(sampler);
    if (browser) await closeBrowser(browser);
    if (running) await stopServer(running.server);
    fs.rmSync(home, {recursive: true, force: true});
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
