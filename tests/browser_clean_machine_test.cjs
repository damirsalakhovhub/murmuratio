// Clean machine: install, run and uninstall in a fresh HOME with Claude only, Codex only and neither.
// Each setup must show its own sessions and limits, explain what is missing and leave nothing behind.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const {isolatedEnv, runCli, startServer, stopServer, success} = require('./cli_test_helpers.cjs');

const now = Date.now() / 1000;
const write = (file, text) => { fs.mkdirSync(path.dirname(file), {recursive: true}); fs.writeFileSync(file, text); };
const jsonl = rows => rows.map(row => JSON.stringify(row)).join('\n') + '\n';

function stageClaude(home) {
  write(path.join(home, '.claude/projects/-work-atlas/clean-claude.jsonl'), jsonl([
    {type: 'user', timestamp: new Date((now - 120) * 1000).toISOString(), cwd: '/work/atlas', sessionId: 'clean-claude',
      message: {role: 'user', content: 'Review the release notes'}},
    {type: 'assistant', timestamp: new Date((now - 60) * 1000).toISOString(), cwd: '/work/atlas', sessionId: 'clean-claude',
      message: {role: 'assistant', model: 'claude-sonnet-5-5', stop_reason: 'tool_use',
        content: [{type: 'tool_use', name: 'Bash', id: 'tool-1', input: {command: 'git log'}}]}}]));
  write(path.join(home, '.claude/tools/agents-registry.jsonl'), jsonl([{id: 'clean-claude', kind: 'claude', role: 'worker', parent_id: 'fixture-launcher'}]));
  write(path.join(home, '.claude/settings.json'), '{\n  "theme": "dark"\n}\n');
}

function stageCodex(home) {
  const started = new Date((now - 90) * 1000).toISOString();
  write(path.join(home, '.codex/sessions/2026/11/08/rollout-2026-11-08T10-00-00-clean-codex.jsonl'), jsonl([
    {type: 'session_meta', timestamp: started, payload: {id: 'clean-codex', timestamp: started, cwd: '/work/harbor',
      source: 'exec', originator: 'codex_exec', parent_session: 'fixture-launcher'}},
    {type: 'event_msg', timestamp: started, payload: {type: 'task_started', model_context_window: 200000}},
    {type: 'turn_context', timestamp: started, payload: {model: 'gpt-6.1-sol', cwd: '/work/harbor'}},
    {type: 'response_item', timestamp: started, payload: {type: 'message', role: 'user',
      content: [{type: 'input_text', text: 'Run the test suite'}]}},
    {type: 'event_msg', timestamp: new Date((now - 30) * 1000).toISOString(), payload: {type: 'token_count',
      info: {model_context_window: 200000, last_token_usage: {input_tokens: 42000}},
      rate_limits: {primary: {used_percent: 40, window_minutes: 10080, resets_at: Math.round(now + 3 * 86400)},
        secondary: {used_percent: 12, window_minutes: 300, resets_at: Math.round(now + 3600)}}}}]));
}

function tree(home) {
  const out = [];
  const walk = dir => {
    for (const name of fs.readdirSync(dir).sort()) {
      const full = path.join(dir, name), rel = path.relative(home, full);
      if (['bin', 'browser', 'launchctl.log', 'launchctl.state'].includes(rel.split(path.sep)[0])) continue;
      const stat = fs.lstatSync(full);
      if (stat.isDirectory()) { out.push(rel + '/'); walk(full); } else out.push(`${rel} ${fs.readFileSync(full, 'utf8').length}`);
    }
  };
  walk(home);
  return out;
}

async function setup(browser, name, stage) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), `murmuratio-clean-${name}-`));
  let running;
  try {
    const env = isolatedEnv(home);
    stage(home);
    const before = tree(home);
    success(runCli(env, 'install'));
    if (fs.existsSync(path.join(home, '.claude/settings.json'))) {
      // Claude's status line passes its limits through the installed wrapper.
      const command = JSON.parse(fs.readFileSync(path.join(home, '.claude/settings.json'))).statusLine.command;
      const input = JSON.stringify({rate_limits: {five_hour: {used_percentage: 20, resets_at: now + 3600},
        seven_day: {used_percentage: 55, resets_at: now + 4 * 86400}}});
      const result = spawnSync('/bin/sh', ['-c', command], {env, input, encoding: 'utf8', timeout: 15000});
      assert.equal(result.status, 0, result.stderr);
    }
    running = await startServer(env);
    const page = await browser.newPage();
    await page.goto(running.url, {waitUntil: 'networkidle0'});
    await page.waitForSelector('.now-card, .now-empty:not([hidden])');
    const state = await page.evaluate(async () => (await fetch('/api/now')).json());
    const cards = await page.$$eval('.now-card', es => es.map(e => e.dataset.agentId));
    const gauges = await page.$$eval('.now-gauge[aria-label*="% used"]', es => es.map(e => e.getAttribute('aria-label')));
    const unavailable = await page.$$eval('.now-gauge[aria-label$="limits unavailable"]', es => es.map(e => e.getAttribute('aria-label')));
    const calculator = await page.$eval('a[href="/calculator"]', el => !el.hidden && getComputedStyle(el).display !== 'none');
    const empty = await page.$eval('.now-empty', e => e.hidden ? '' : e.textContent);
    const notice = await page.$eval('#now-error', e => e.hidden ? '' : e.textContent);
    await page.close();
    await stopServer(running.server); running = null;
    success(runCli(env, 'uninstall'));
    return {state, cards, gauges, unavailable, calculator, empty, notice, left: tree(home).filter(line => !before.includes(line))};
  } finally {
    if (running) await stopServer(running.server);
    fs.rmSync(home, {recursive: true, force: true});
  }
}

async function main() {
  const browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
    args: ['--no-sandbox', '--disable-background-networking', '--disable-sync', '--disable-extensions']});
  const results = {};
  try {
    const claude = await setup(browser, 'claude', stageClaude);
    assert.deepEqual(claude.cards, ['claude:clean-claude'], 'Claude only: its session is a card');
    assert.deepEqual(claude.gauges.map(g => g.split(' ').slice(0, 2).join(' ')), ['Claude 5h', 'Claude week'], 'Claude only: its limits');
    assert.deepEqual(claude.unavailable, [], 'Absent Codex limits are hidden');
    assert.equal(claude.calculator, false, 'Claude-only has no empty Codex calculator');
    assert.equal(claude.notice, '', 'Claude only: a missing Codex is not an error');
    assert.deepEqual(claude.left, [], 'Claude only: uninstall leaves nothing');
    results['Claude only'] = 'PASS';

    const chat = await setup(browser, 'claude-chat', home => {
      stageClaude(home);
      fs.unlinkSync(path.join(home, '.claude/tools/agents-registry.jsonl'));
    });
    assert.deepEqual(chat.cards, [], 'A plain terminal chat stays hidden');
    assert.deepEqual(chat.state.agents, []);
    assert.equal(chat.empty, 'Your Claude Code and Codex chats appear on the board once they start agents.');
    assert.equal(chat.notice, '');
    assert.deepEqual(chat.left, []);
    results['Claude terminal chat'] = 'PASS';

    const codex = await setup(browser, 'codex', stageCodex);
    assert.deepEqual(codex.cards, ['codex:clean-codex'], 'Codex only: its worker is a card');
    assert.deepEqual(codex.gauges.map(g => g.split(' ').slice(0, 2).join(' ')), ['Codex 5h', 'Codex week'], 'Codex only: its limits');
    assert.deepEqual(codex.unavailable, [], 'Absent Claude limits are hidden');
    assert.equal(codex.calculator, true, 'Codex weekly data enables the calculator');
    assert.equal(codex.notice, '', 'Codex only: a missing Claude is not an error');
    assert.deepEqual(codex.left, [], 'Codex only: uninstall leaves nothing');
    results['Codex only'] = 'PASS';

    const neither = await setup(browser, 'neither', () => {});
    assert.deepEqual(neither.cards, []);
    assert.deepEqual(neither.gauges, []);
    assert.deepEqual(neither.unavailable, []);
    assert.equal(neither.calculator, false);
    assert.match(neither.empty, /Claude Code/);
    assert.match(neither.empty, /Codex/);
    assert.equal(neither.empty, 'Your Claude Code and Codex chats appear on the board once they start agents.');
    assert.equal(neither.notice, '', 'Neither: nothing is reported as broken');
    assert.deepEqual(neither.left, [], 'Neither: uninstall leaves nothing');
    results.Neither = 'PASS';
  } finally {
    await closeBrowser(browser);
    console.log('Clean machine: ' + Object.entries(results).map(([k, v]) => `${k} ${v}`).join(', '));
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
