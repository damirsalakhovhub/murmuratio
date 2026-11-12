// Real provider files, real server and real browser; no request interception.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');

async function main() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'murmuratio-board-'));
  const project = path.join(home, '.claude/projects/synthetic');
  fs.mkdirSync(project, {recursive: true});
  const record = {type: 'assistant', timestamp: Date.now() / 1000, cwd: '/synthetic/project',
    message: {stop_reason: 'tool_use', model: 'claude-sonnet-5-5', content: [
      {type: 'tool_use', name: 'Bash', id: 'synthetic-tool', input: {command: 'work'}}]}};
  fs.writeFileSync(path.join(project, 'missing-start.jsonl'), JSON.stringify(record) + '\n');
  const desktop = path.join(home, 'Library/Application Support/Claude/claude-code-sessions/a/b');
  fs.mkdirSync(desktop, {recursive: true});
  fs.writeFileSync(path.join(desktop, 'local_missing-start.json'), JSON.stringify({cliSessionId: 'missing-start'}));
  const fixture = fs.readFileSync(path.join(__dirname, 'fixtures/adapters/claude-oneshot.jsonl'), 'utf8')
    .trim().split('\n').map(line => JSON.parse(line));
  const finished = Date.parse(fixture[3].timestamp);
  const observed = Date.now();
  const shift = (records, age = 60000) => records.map(r => ({...r, ...(r.timestamp
    ? {timestamp: new Date(Date.parse(r.timestamp) + observed - finished - age).toISOString()} : {})}));
  fs.writeFileSync(path.join(project, 'synthetic-oneshot.jsonl'), shift(fixture).map(r => JSON.stringify(r)).join('\n') + '\n');
  fs.writeFileSync(path.join(project, 'old-oneshot.jsonl'), shift(fixture, 46 * 3600 * 1000)
    .map(r => JSON.stringify({...r, sessionId: 'old-oneshot'})).join('\n') + '\n');
  fs.writeFileSync(path.join(project, 'idle-orchestrator.jsonl'), shift(fixture, 2 * 3600 * 1000)
    .map(r => JSON.stringify({...r, sessionId: 'idle-orchestrator'})).join('\n') + '\n');
  fs.writeFileSync(path.join(desktop, 'local_idle-orchestrator.json'), JSON.stringify({
    cliSessionId: 'idle-orchestrator', title: 'Sample orchestrator'}));
  const expected = {'claude:missing-start': 'Running', 'claude:synthetic-oneshot': 'Done',
    'claude:idle-orchestrator': 'Turn finished'};
  const codex = path.join(home, 'codex/sessions/2026/11/07');
  fs.mkdirSync(codex, {recursive: true});
  for (const name of ['Alpha', 'Beta', 'Gamma']) {
    const prefix = name.toLowerCase(), cwd = '/synthetic/' + name;
    for (const [suffix, age, desktopFields, word] of [
      ['root', 7200000, {title: 'Coordinator'}, 'Turn finished'],
      ['working', 60000, {spawnedFrom: {sessionId: 'not-on-the-board'}}, 'Running'],
      ['finished', 600000, {spawnedFrom: {sessionId: prefix + '-root'}}, 'Done'],
      ['cli', 300000, null, 'Done'],
    ]) {
      const id = prefix + '-' + suffix;
      const records = shift(fixture, age).slice(0, suffix === 'working' ? 3 : undefined)
        .map(r => ({...r, sessionId: id, ...(r.cwd ? {cwd} : {})}));
      fs.writeFileSync(path.join(project, id + '.jsonl'), records.map(r => JSON.stringify(r)).join('\n') + '\n');
      if (desktopFields) fs.writeFileSync(path.join(desktop, 'local_' + id + '.json'),
        JSON.stringify({cliSessionId: id, ...desktopFields}));
      expected['claude:' + id] = word;
    }
    const id = prefix + '-chat', timestamp = observed / 1000;
    fs.writeFileSync(path.join(codex, 'rollout-' + id + '.jsonl'), [
      {type: 'session_meta', timestamp, payload: {id, timestamp, cwd, source: 'cli'}},
      {type: 'event_msg', timestamp, payload: {type: 'task_started'}},
    ].map(r => JSON.stringify(r)).join('\n') + '\n');
  }
  // Lifecycle fixtures represent launched agents; coordinators are explicitly registered.
  const registry = path.join(home, '.claude/tools/agents-registry.jsonl');
  fs.mkdirSync(path.dirname(registry), {recursive: true});
  fs.writeFileSync(registry, Object.keys(expected).map(id => {
    const name = id.slice('claude:'.length), root = name === 'idle-orchestrator' || name === 'missing-start' || name.endsWith('-root');
    return JSON.stringify({id: name, kind: 'claude', role: root ? 'orchestrator' : 'worker',
      parent_id: root ? null : name.endsWith('-finished') ? name.replace(/-finished$/, '-root') : 'fixture-launcher'});
  }).join('\n') + '\n');
  const env = {...process.env, HOME: home, CODEX_HOME: path.join(home, 'codex')};
  delete env.PORTLESS_URL;
  const server = spawn(process.env.PYTHON || 'python3', ['-B', '-c',
    'from murmuratio_app.server import main; main()', '--port', '0'],
  {cwd: path.join(__dirname, '..'), env, stdio: ['ignore', 'pipe', 'pipe']});
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Server did not start')), 10000);
      server.once('error', reject);
      server.once('exit', code => reject(new Error(`Server exited: ${code}`)));
      server.stdout.on('data', chunk => {
        const match = String(chunk).match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
    });
    browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
      userDataDir: path.join(home, 'browser'),
      args: ['--no-sandbox', '--disable-background-networking', '--disable-sync', '--disable-extensions']});
    const page = await browser.newPage();
    await page.goto(url, {waitUntil: 'domcontentloaded'});
    const selector = '[data-agent-id="claude:missing-start"]';
    await page.waitForSelector(selector);
    const snapshot = await page.evaluate(async () => (await fetch('/api/now')).json());
    assert.equal(snapshot.agents[0].turn_started, null);
    assert.deepEqual(await page.$$eval(`${selector} .now-time > span`, items => items.map(e => e.textContent)), ['—']);
    const oneshot = '[data-agent-id="claude:synthetic-oneshot"]';
    await page.waitForSelector(oneshot);
    assert.equal(await page.$eval(`${oneshot} .now-word`, e => e.textContent), 'Done', 'S3: one-shot is finished');
    assert.equal(await page.$eval(`${oneshot} .now-who`, e => e.textContent), 'project', 'S3: CLI is not an orchestrator');
    assert.equal(await page.$eval(`${oneshot} .now-time > span:last-child`, e => e.textContent), '1′', 'T1: saved active time');
    assert.equal(await page.$('[data-agent-id="claude:old-oneshot"]'), null, 'S5: old completion is absent');
    const idle = '[data-agent-id="claude:idle-orchestrator"]';
    await page.waitForSelector(idle);
    assert.equal(await page.$eval(`${idle} .now-word`, e => e.textContent), 'Turn finished', 'S3: Desktop root waits for a person');
    await page.waitForSelector('[data-agent-id="claude:gamma-cli"]');
    const actual = await page.$$eval('#now-board [data-agent-id]', cards => Object.fromEntries(
      cards.map(card => [card.dataset.agentId, card.querySelector('.now-word').textContent])));
    assert.deepEqual(actual, expected, 'B1/S3/S5: exact visible cards and statuses');
    assert.deepEqual((await page.$$eval('#now-board .now-project', groups => groups.map(group => group.dataset.project))).sort(),
      ['Alpha', 'Beta', 'Gamma', 'project'], 'B1: exact visible projects');
    for (let poll = 0; poll < 3; poll++) {
      await page.waitForResponse(r => new URL(r.url()).pathname === '/api/now');
      assert.equal(await page.$$eval(oneshot, items => items.length), 1, 'B2: one DOM card across real polls');
    }
    console.log('Real board: unknown turn start has no duration');
    console.log('B2/S3/S5/T1: real one-shot completion, retention, classification and repeated polls passed');
    console.log('S3: an idle Desktop orchestrator remains visible after 30 minutes');
  } finally {
    if (browser) await closeBrowser(browser);
    if (server.exitCode === null && server.signalCode === null) {
      const closed = new Promise(resolve => server.once('close', resolve));
      server.kill();
      await closed;
    }
    fs.rmSync(home, {recursive: true, force: true});
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
