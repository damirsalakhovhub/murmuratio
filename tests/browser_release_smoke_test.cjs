// Release smoke: real CLI, isolated HOME and browser, and a stubbed launchctl.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const {isolatedEnv, reservePort, runCli, startServer, stopServer, success} = require('./cli_test_helpers.cjs');

function stageClaude(home, id) {
  const project = path.join(home, '.claude/projects/synthetic');
  const desktop = path.join(home, 'Library/Application Support/Claude/claude-code-sessions/a/b');
  fs.mkdirSync(project, {recursive: true});
  fs.mkdirSync(desktop, {recursive: true});
  const record = {type: 'assistant', timestamp: Date.now() / 1000, cwd: '/synthetic/project',
    message: {stop_reason: 'tool_use', model: 'claude-sonnet-5-5', content: [
      {type: 'tool_use', name: 'Bash', id: 'synthetic-tool', input: {command: 'work'}}]}};
  fs.writeFileSync(path.join(project, `${id}.jsonl`), `${JSON.stringify(record)}\n`);
  fs.writeFileSync(path.join(desktop, `local_${id}.json`), JSON.stringify({cliSessionId: id}));
}

function settingsAt(home) { return path.join(home, '.claude/settings.json'); }

function seedSettings(home) {
  const settings = settingsAt(home);
  fs.mkdirSync(path.dirname(settings), {recursive: true});
  const original = Buffer.from('{\n  "statusLine" : { "type" : "command", "command" : "cat", "padding": 4 },\n  "theme":"dark", "unrelated":[1,2]\n}\n');
  fs.writeFileSync(settings, original);
  return original;
}

function assertWrappedCommand(home, env) {
  const command = JSON.parse(fs.readFileSync(settingsAt(home))).statusLine.command;
  const input = '{"synthetic":true}\n';
  const result = spawnSync('/bin/sh', ['-c', command], {env, input, encoding: 'utf8', timeout: 15000});
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, input);
}

function assertPlainFailure(result, pattern) {
  assert.notEqual(result.status, 0);
  assert.equal(result.stdout, '');
  const lines = result.stderr.trimEnd().split('\n');
  assert.equal(lines.length, 1, result.stderr);
  assert.match(lines[0], /^Error: /);
  assert.match(lines[0], pattern);
}

async function checkBoard(page, url, id) {
  await page.goto(url, {waitUntil: 'domcontentloaded'});
  const selector = `[data-agent-id="claude:${id}"]`;
  await page.waitForSelector(selector);
  const snapshot = await page.evaluate(async () => {
    const response = await fetch('/api/now');
    return {status: response.status, data: await response.json()};
  });
  assert.equal(snapshot.status, 200);
  assert(snapshot.data.agents.some(agent => agent.id === `claude:${id}`));
  assert.equal(await page.$eval(`${selector} .now-word`, element => element.textContent), 'Running');
}

async function main() {
  const homes = [fs.mkdtempSync(path.join(os.tmpdir(), 'murmuratio-install-')),
    fs.mkdtempSync(path.join(os.tmpdir(), 'murmuratio-late-claude-'))];
  let browser, running, occupied;
  try {
    const [home, late] = homes;
    const env = isolatedEnv(home);
    const original = seedSettings(home);
    stageClaude(home, 'install-smoke');
    success(runCli(env, 'install'));
    assertWrappedCommand(home, env);
    const installed = fs.readFileSync(settingsAt(home));

    // The CLI must fail without touching the status line or unrelated settings.
    occupied = await reservePort();
    const occupiedPort = occupied.address().port;
    assertPlainFailure(runCli(env, 'run', '--port', String(occupiedPort)), /address already in use/i);
    assert.deepEqual(fs.readFileSync(settingsAt(home)), installed);
    const config = path.join(home, '.config/murmuratio/config.toml');
    const validConfig = fs.readFileSync(config);
    fs.writeFileSync(config, '[modules\n');
    assertPlainFailure(runCli(env, 'run'), /expected|invalid/i);
    assert.deepEqual(fs.readFileSync(settingsAt(home)), installed);
    fs.writeFileSync(config, validConfig);

    running = await startServer(env);
    assert.notEqual(Number(new URL(running.url).port), occupiedPort);
    await new Promise(resolve => occupied.close(resolve)); occupied = null;
    browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
      userDataDir: path.join(home, 'browser'),
      args: ['--no-sandbox', '--disable-background-networking', '--disable-sync', '--disable-extensions']});
    const page = await browser.newPage();
    await checkBoard(page, running.url, 'install-smoke');
    assert.deepEqual(fs.readFileSync(settingsAt(home)), installed);
    await stopServer(running.server); running = null;
    success(runCli(env, 'uninstall'));
    assert.deepEqual(fs.readFileSync(settingsAt(home)), original, 'unchanged settings restore exact bytes');
    assert(!fs.existsSync(env.MURMURATIO_LAUNCHCTL_LOG), 'default install must not invoke launchctl');

    // Unrelated user edits survive; macOS LaunchAgent calls are made only to the stub.
    success(runCli(env, 'install', ...(process.platform === 'darwin' ? ['--launch-agent'] : [])));
    const edited = fs.readFileSync(settingsAt(home), 'utf8').replace('"theme":"dark"', '"theme":"light", "new":true');
    fs.writeFileSync(settingsAt(home), edited);
    success(runCli(env, 'uninstall'));
    const restored = fs.readFileSync(settingsAt(home), 'utf8');
    assert.equal(JSON.parse(restored).theme, 'light');
    assert.equal(JSON.parse(restored).new, true);
    assert.deepEqual(JSON.parse(restored).statusLine, JSON.parse(original).statusLine);
    assert(restored.includes('{ "type" : "command", "command" : "cat", "padding": 4 }'));
    if (process.platform === 'darwin') {
      const calls = fs.readFileSync(env.MURMURATIO_LAUNCHCTL_LOG, 'utf8').trim().split('\n');
      assert(calls.every(call => ['print', 'bootstrap', 'bootout'].includes(call)));
      assert.deepEqual(calls.filter(call => call !== 'print'), ['bootstrap', 'bootout']);
    }

    // A first install without Claude can be rerun after Claude appears, without Codex.
    const lateEnv = isolatedEnv(late);
    success(runCli(lateEnv, 'install'));
    assert(!fs.existsSync(path.join(late, '.claude')));
    const lateOriginal = seedSettings(late);
    stageClaude(late, 'claude-only');
    success(runCli(lateEnv, 'install'));
    assertWrappedCommand(late, lateEnv);
    assert(!fs.existsSync(lateEnv.CODEX_HOME));
    running = await startServer(lateEnv);
    await checkBoard(page, running.url, 'claude-only');
    await stopServer(running.server); running = null;
    success(runCli(lateEnv, 'uninstall'));
    assert.deepEqual(fs.readFileSync(settingsAt(late)), lateOriginal);
    assert(!fs.existsSync(lateEnv.MURMURATIO_LAUNCHCTL_LOG));
    console.log('Release smoke: install, startup failures, real API/browser, restoration and Claude-only reinstall passed');
  } finally {
    if (browser) await closeBrowser(browser);
    if (running) await stopServer(running.server);
    if (occupied) await new Promise(resolve => occupied.close(resolve));
    for (const home of homes) fs.rmSync(home, {recursive: true, force: true});
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
