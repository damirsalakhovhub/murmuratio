// Deterministic browser responses against an isolated local fixture server.
const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, '.inspector-data/now-verification');
const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'inspector-board-'));
const fixtureConfig = path.join(fixtureRoot, 'config.toml');
fs.writeFileSync(fixtureConfig, '[modules]\nforecast = true\nscenarios = true\nrouting = true\ncodex_credits = true\nburn_rate = true\ncodex_chats = true\ncontext_reset = true\ncodex_wrapper = true\nbranding = true\n');
const service = spawn('python3', ['-B', '-c', 'from murmuratio_app.server import main; main()', '--port', '0', '--config', fixtureConfig,
  '--codex-home', path.join(fixtureRoot, 'codex'), '--data-dir', path.join(fixtureRoot, 'data')],
  {cwd: root, env: {...process.env, HOME: fixtureRoot, PORTLESS_URL: ''}, stdio: ['ignore', 'pipe', 'pipe']});

(async () => {
  let browser;
  try {
    const base = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Fixture server did not start')), 10000);
      service.once('error', error => { clearTimeout(timer); reject(error); });
      service.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture server exited: ${code}`)); });
      service.stdout.on('data', chunk => {
        const match = String(chunk).match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
    });
    browser = await puppeteer.launch({executablePath: chromePath(), headless: true,
      args: ['--disable-background-networking', '--disable-sync', '--disable-extensions']});
    fs.mkdirSync(artifacts, {recursive: true});
    const errors = [];
    const track = page => {
      page.on('pageerror', error => errors.push(error.message));
      page.on('console', message => { if (/content security policy/i.test(message.text())) errors.push(message.text()); });
    };
    const at = (h, m) => new Date(2026, 10, 5, h, m).getTime() / 1000, now = at(18, 40);
    const item = (id, project, changes = {}) => ({id, project, kind: 'claude', who: 'Claude Opus 5.5', model: 'claude-opus-5-5',
      folder: 'dev', task: id, started: at(18, 0), last_at: at(18, 30), status: 'working', ...changes});
    const atlas = item('atlas', 'Atlas', {orchestrator: true, status: 'waiting', waiting_since: at(18, 31), task: 'Review implementation'});
    const gameplay = item('gameplay', 'Atlas', {kind: 'codex', model: 'gpt-6.1-terra', folder: 'gameplay', parent_id: 'atlas',
      started: at(8, 34), seconds: 360, task: 'Update the dashboard component', current_action: 'editing dashboard.py',
      thread: 'fixture-thread', board_source: 'codex_rollout', link: 'inferred', context_tokens: 987654, context_used: 49, context_window: 100});
    const icons = item('icons', 'Atlas', {parent_id: 'atlas', folder: 'interfaces', status: 'done', task: 'Review the layout',
      started: at(17, 51), ended: at(18, 18), seconds: 27 * 60});
    const northstar = item('northstar', 'Northstar', {orchestrator: true, status: 'idle', last_at: at(18, 9), task: 'Prepare reference materials'});
    const arts = item('arts', 'Northstar', {parent_id: 'northstar', folder: 'arts', started: at(18, 9), current_action: 'generating 4 images',
      task: 'Prepare sample artwork', context_used: 80, context_window: 100});
    const harbor = item('harbor', 'Harbor', {orchestrator: true, status: 'idle', last_at: at(18, 28), task: 'Coordinate project'});
    const inspector = item('inspector', 'Harbor', {kind: 'codex', model: 'gpt-6.1-sol', folder: 'inspector', parent_id: 'harbor',
      started: at(18, 27), current_action: 'tests', task: 'Test the board', context_used: 50, context_window: 100});
    const done = item('done', 'Harbor', {kind: 'codex', model: 'gpt-6.1-sol', folder: 'inspector', parent_id: 'harbor', status: 'done', completed: true,
      task: 'Fix display labels', started: at(18, 22), ended: at(18, 27), seconds: 300, head: 'a1b2c3d', verdict: 'REPORT DUMP '.repeat(200)});
    let payload = {now, agents: [gameplay, icons, arts, inspector, atlas, northstar, harbor], completed: [done]}, polls = 0, fail = false;
    const page = await browser.newPage(); track(page);
    await page.evaluateOnNewDocument(() => localStorage.setItem('inspector.source', 'now'));
    await page.setViewport({width: 1280, height: 1000, deviceScaleFactor: 1});
    await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: 'light'}]);
    await page.setRequestInterception(true);
    page.on('request', request => {
      if (new URL(request.url()).pathname === '/api/now') {
        polls++; return request.respond({status: fail ? 503 : 200, contentType: 'application/json', body: JSON.stringify(payload)});
      }
      return request.continue();
    });
    await page.goto(base, {waitUntil: 'networkidle0'});
    await page.waitForSelector('[data-agent-id="gameplay"]');
    const bitmap = await page.evaluate(async () => {
      const image = new Image(); image.src = document.querySelector('#now-codex image').getAttribute('href');
      await image.decode(); return {width: image.naturalWidth, height: image.naturalHeight};
    });
    assert(bitmap.width >= 36 && bitmap.height >= 36, 'Codex icon decodes at adequate DPR2 resolution');
    assert.equal(await page.$eval('[data-agent-id="gameplay"] .now-task', e => e.getAttribute('href')), '/?codex=fixture-thread');
    assert.equal(await page.$eval('[data-agent-id="gameplay"]', e => e.dataset.parentId), 'atlas');
    assert.equal(await page.$eval('[data-agent-id="gameplay"]', e => e.title), 'Parent link inferred from working directory and launch time');
    assert.equal(await page.title(), 'Murmuratio — Board');
    assert.equal(await page.$eval('html', e => e.lang), 'en');
    await page.focus('#tab-now'); await page.keyboard.press('ArrowRight');
    assert.equal(await page.$eval('#tab-codex', e => e.getAttribute('aria-selected')), 'true');
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.$eval('#tab-now', e => e.getAttribute('aria-selected')), 'true');
    const value = (id, selector) => page.$eval(`[data-agent-id="${id}"] ${selector}`, e => e.textContent);
    const parts = (id, selector) => page.$$eval(`[data-agent-id="${id}"] ${selector} > span`, es => es.map(e => e.textContent));
    const ids = project => page.$$eval(`[data-project="${project}"] .now-card`, es => es.map(e => e.dataset.agentId));
    assert.deepEqual(await parts('atlas', '.now-time'), ['Last reply 18:31']);
    assert.deepEqual(await parts('gameplay', '.now-time'), ['08:34', '6′']);
    assert.deepEqual(await parts('icons', '.now-time'), ['17:51', '27′']);
    assert.equal(await value('northstar', '.now-time'), 'Last reply 18:09');
    assert.equal(await value('gameplay', '.now-model'), 'GPT-6.1 Terra');
    assert.equal(await value('atlas', '.now-model'), 'Opus 5.5');
    for (const [id, percent, level] of [['gameplay', '49%', 'low'], ['inspector', '50%', 'mid'], ['arts', '80%', 'high']]) {
      assert.equal(await value(id, '.now-context'), percent);
      assert.equal(await page.$eval(`[data-agent-id="${id}"] .now-context`, e => e.dataset.level), level);
    }
    assert.equal(await page.$eval('[data-agent-id="atlas"] .now-context', e => e.hidden), true);
    // Narrow cards may hide footer values; each footer item keeps a hover tooltip that gives them back.
    assert.deepEqual(await page.$$eval('[data-agent-id="gameplay"] .now-footer > *', es => es.map(e => e.hasAttribute('data-tip'))), [true, true, true]);
    assert.deepEqual(await page.$$eval('#now-summary > span', es => es.map(e => e.textContent)), ['3 running', '0 needs attention', '2 done today']);
    assert.deepEqual(await page.$$eval('[data-project="Atlas"] .now-counts > span', es => es.map(e => e.textContent)), ['1 running', '1 done']);
    assert.equal(await page.$eval('#now-view', e => /987654|REPORT DUMP|Context|Now/.test(e.textContent)), false);
    assert.equal(await page.$eval('[data-agent-id="atlas"]', e => getComputedStyle(e).boxShadow.includes('1.5px')), false);
    assert.equal(await value('atlas', '.now-word'), 'Turn finished');
    assert.equal(await page.$$eval('.now-tree path', es => es.length), 3);
    assert.deepEqual(await ids('Atlas'), ['atlas', 'gameplay', 'icons']);
    assert.equal(await page.$eval('.source-bar', e => e.getBoundingClientRect().top), 0);
    assert.equal(await page.$eval('.source-bar', e => getComputedStyle(e).position), 'sticky');
    await page.screenshot({path: path.join(artifacts, 'v4-fixture-light.png'), fullPage: true});

    // Polling continues, but neither text nor nodes change while the pointer is on the board.
    await page.evaluate(() => { window.firstCard = document.querySelector('[data-agent-id="gameplay"]'); });
    await page.hover('[data-agent-id="gameplay"]');
    const before = await page.$eval('#now-board', e => e.innerHTML), priorPolls = polls;
    const extra = item('extra', 'Atlas', {parent_id: 'atlas', task: '<script>window.pwned=1</script>', current_action: 'searching files'});
    payload = {now: now + 60, agents: [extra, {...gameplay, status: 'waiting', waiting_since: at(18, 40)}, harbor, northstar, atlas, arts, inspector], completed: [done, icons]};
    await page.waitForResponse(r => new URL(r.url()).pathname === '/api/now');
    assert(polls > priorPolls);
    assert.equal(await page.$eval('#now-board', e => e.innerHTML), before);
    // Selecting text keeps the queued update paused after the pointer leaves.
    await page.evaluate(() => {
      const range = document.createRange(); range.selectNodeContents(document.querySelector('[data-agent-id="gameplay"] .now-task'));
      getSelection().removeAllRanges(); getSelection().addRange(range);
    });
    await page.mouse.move(5, 5);
    assert.deepEqual(await parts('gameplay', '.now-action'), ['Running', 'editing dashboard.py']);
    payload = {...payload, agents: [{...gameplay, current_action: 'latest action'}, extra, harbor, northstar, atlas, arts, inspector]};
    await page.waitForResponse(r => new URL(r.url()).pathname === '/api/now');
    assert.equal(await page.$('[data-agent-id="extra"]'), null);
    await page.evaluate(() => getSelection().removeAllRanges());
    await page.waitForFunction(() => document.querySelector('[data-agent-id="gameplay"] .now-current').textContent === 'latest action');
    assert.deepEqual(await ids('Atlas'), ['atlas', 'gameplay', 'extra', 'icons']);
    assert.equal(await page.evaluate(() => window.firstCard === document.querySelector('[data-agent-id="gameplay"]')), true);
    assert.equal(await page.evaluate(() => window.pwned), undefined);

    // Completion moves a worker under the Finished heading; omission (server dropped it) removes it. New roots/children append to the column.
    const lateRoot = item('late-root', 'Atlas', {orchestrator: true, status: 'idle'});
    payload = {now: now + 120, agents: [lateRoot, {...extra, task: 'New task'}, inspector, harbor, arts, northstar, atlas],
      completed: [{...gameplay, status: 'done', completed: true, ended: now + 120, seconds: 480, head: 'abcdef12345'}]};
    await page.waitForFunction(() => document.querySelector('[data-agent-id="gameplay"]').classList.contains('done'));
    assert.deepEqual(await ids('Atlas'), ['atlas', 'extra', 'late-root', 'gameplay']);
    // Finished heading shows a dim count; one click hides the list, the next shows it.
    const finishedTitle = '[data-project="Atlas"] .now-finished-title';
    assert.equal(await page.$eval(finishedTitle + ' .now-finished-count', e => e.textContent), '1');
    await page.click(finishedTitle);
    assert.equal(await page.$eval('[data-project="Atlas"] .now-finished .now-items', e => e.hidden), true);
    await page.click(finishedTitle);
    assert.equal(await page.$eval('[data-project="Atlas"] .now-finished .now-items', e => e.hidden), false);
    await page.mouse.move(5, 5);
    assert.deepEqual(await parts('gameplay', '.now-time'), ['08:34', '8′']);
    assert.deepEqual(await parts('gameplay', '.now-action'), ['Done']);
    const grandchild = item('grandchild', 'Atlas', {parent_id: 'extra'});
    payload = {...payload, agents: [grandchild, ...payload.agents].reverse()};
    await page.waitForSelector('[data-agent-id="grandchild"]');
    assert.deepEqual(await ids('Atlas'), ['atlas', 'extra', 'late-root', 'grandchild', 'gameplay']);
    assert.equal(await page.$eval('[data-agent-id="grandchild"]', e => e.style.marginInlineStart), '42px');
    assert.equal(await page.$eval('.now-tree path', e => e.getAttribute('d').includes(' Q ')), true);

    // Blocked orchestrators and workers awaiting approval need the owner.
    payload = {...payload, agents: payload.agents.map(a => ['atlas', 'extra'].includes(a.id) ? {...a, status: 'blocked', ...(a.id === 'extra' ? {current_action: 'waiting for approval'} : {})} : a),
      limits: {claude: [{window_minutes: 10080, used_percent: 25, resets_at: now + 172800},
        {window_minutes: 300, used_percent: 80, resets_at: now + 3600}],
      codex: [{window_minutes: 10080, used_percent: 75, resets_at: now + 86400}]},
      routing: {code_to: 'claude', claude: {allowance_per_day: 10, burn_per_day: 20},
        codex: {allowance_per_day: 10, burn_per_day: 0}},
      forecast: {claude: {start: now - 5 * 86400, resets_at: now + 2 * 86400, used_percent: 25, points: [[now - 86400, 20], [now, 25]],
        pace_per_day: 5, projected_at_reset: 35, hits_100_at: null},
        codex: {start: now - 6 * 86400, resets_at: now + 86400, used_percent: 95, points: [[now, 95]],
        pace_per_day: 50, projected_at_reset: 125, hits_100_at: now + 43200, credits: {balance: 60000, per_hour: 400, sample_hours: 0.2}}},
      credits: {balance: 60000, per_hour: 400, sample_hours: 0.2},
      burn: {claude: {start: now - 5 * 86400, hours: [[Math.floor(now / 3600) * 3600, {'claude-opus-5-5': 1.2, 'claude-fable-5-1': 0.8}]]},
        codex: {start: now - 6 * 86400, hours: [[Math.floor(now / 3600) * 3600 - 3600, {'gpt-6.1-sol': 1}]]}}};
    await page.waitForSelector('[data-agent-id="atlas"].need');
    // Forecast sits between the header and the columns: full width, 500px tall, one verdict per tool.
    await page.waitForSelector('#now-forecast:not([hidden]) .now-forecast-chart');
    const fc = await page.evaluate(() => {
      const f = document.getElementById('now-forecast').getBoundingClientRect(), b = document.getElementById('now-board').getBoundingClientRect();
      return {height: f.height, sameWidth: Math.abs(f.width - b.width) < 1, above: f.bottom <= b.top,
        verdicts: [...document.querySelectorAll('.now-forecast-verdict')].map(e => e.textContent),
        lines: document.querySelectorAll('.now-forecast-chart .actual').length, projected: document.querySelectorAll('.now-forecast-chart .projected').length};
    });
    assert.equal(fc.height, 500); assert(fc.sameWidth); assert(fc.above);
    assert.match(fc.verdicts[0], /^Claudefits, 35% at reset/); assert.match(fc.verdicts[1], /^Codexlimit .*, then credits$/);
    assert.equal(await page.$eval('.now-credits', e => e.textContent), '60.0kcredits');
    assert.equal(fc.lines, 2); assert.equal(fc.projected, 2);
    assert.equal(await page.$$eval('.now-forecast-chart .reset', es => es.length), 2);
    assert.equal(await page.$$eval('.now-forecast-chart .reset-mark', es => es.length), 2);
    // Scenarios recompute the verdicts and the answers.
    const facts = () => page.$$eval('.now-forecast-fact-label', es => es.map(e => e.textContent));
    assert.deepEqual(await facts(), ['Work split today', 'Codex credits', 'To last until reset', 'Work hours']);
    const chartHeight = await page.$eval('.now-forecast-chart', e => e.getBoundingClientRect().height);
    await page.click('.now-forecast-scenario[data-scenario="claude"]');
    await page.waitForFunction(() => document.querySelector('.now-forecast-scenario[data-scenario="claude"]').getAttribute('aria-checked') === 'true');
    assert.match(await page.$eval('.now-forecast-verdict[data-tool="codex"]', e => e.textContent), /fits, 95% at reset/);
    await page.click('.now-forecast-scenario[data-scenario="rebalance"]');
    await page.waitForFunction(() => document.querySelectorAll('.now-forecast-fact-label').length === 5);
    assert.deepEqual(await facts(), ['Work split', 'Best split result', 'Codex credits', 'To last until reset', 'Work hours']);
    assert.equal(await page.$eval('#now-forecast', e => e.getBoundingClientRect().height), 500);
    assert.equal(await page.$eval('.now-forecast-chart', e => e.getBoundingClientRect().height), chartHeight);
    await page.click('.now-forecast-scenario[data-scenario="now"]');
    // Burn rate view: one chart per tool, bars stacked by model; the block keeps its height.
    await page.click('.now-forecast-scenario[data-option="burn"]');
    await page.waitForSelector('.now-burn-chart');
    assert.deepEqual(await page.$$eval('.now-burn-chart', es => es.map(e => e.dataset.tool)), ['claude', 'codex']);
    assert.deepEqual(await page.$$eval('.now-burn-chart[data-tool="claude"] rect[data-model]', es => es.map(e => e.dataset.model)), ['fable', 'opus']);
    assert.deepEqual(await page.$$eval('.now-burn-legend .now-forecast-key', es => es.map(e => e.textContent)), ['Fable', 'Opus', 'Sol']);
    assert.equal(await page.$eval('#now-forecast', e => e.getBoundingClientRect().height), 500);
    await page.click('.now-forecast-scenario[data-option="week"]');
    await page.waitForFunction(() => document.querySelectorAll('.now-burn-chart[data-tool="codex"] .now-burn-bar').length === 8);
    await page.click('.now-forecast-scenario[data-option="today"]');
    await page.click('.now-forecast-scenario[data-option="forecast"]');
    await page.waitForSelector('.now-forecast-chart .reset');
    assert.deepEqual(await page.$$eval('.now-card.need', es => es.map(e => e.dataset.agentId)), ['atlas', 'extra']);
    assert.equal(await value('extra', '.now-word'), 'Blocked');
    assert.match(await page.$eval('#now-summary', e => e.textContent), /2 needs attention/);
    assert.equal(await page.$eval('[data-agent-id="atlas"]', e => getComputedStyle(e).boxShadow.includes('1.5px')), true);
    assert.equal(await page.$$eval('.now-gauge-logo', es => es.length), 2, 'One logo per tool');
    assert.deepEqual(await page.$$eval('.now-gauge[data-tool="claude"] .now-gauge-win', es => es.map(e => e.textContent)), ['5h', 'week']);
    assert.equal(await page.$eval('.now-gauge-pct', e => e.textContent), '80%');
    assert(await page.$$eval('.now-gauge-pct', es => es.every(e => getComputedStyle(e).display === 'none')));
    assert.deepEqual(await page.$$eval('.now-gauge-reset', es => es.map(e => e.textContent)), ['1h', '2d ↑', '24h']);
    // Tooltips: the first waits, the next one shows at once.
    await page.hover('.now-gauge[data-tool="claude"] .now-gauge-wrap');
    assert.equal(await page.$eval('.now-tip', e => e.hidden), true);
    await page.waitForFunction(() => !document.querySelector('.now-tip').hidden, {timeout: 2000});
    assert.match(await page.$eval('.now-tip', e => e.textContent), /^Claude 5-hour limitUsed\d+%Time passed\d+%/);
    await page.hover('.now-gauge-pace');
    assert.equal(await page.$eval('.now-tip', e => e.hidden), false);
    assert.match(await page.$eval('.now-tip', e => e.textContent), /^Faster than the weekly paceSpending/);
    await page.mouse.move(5, 300);
    await page.waitForFunction(() => document.querySelector('.now-tip').hidden);
    assert.deepEqual(await page.$$eval('.now-gauge', es => es.map(e => e.dataset.level)), ['crit', 'ok', 'crit']);
    assert.deepEqual(await page.$$eval('.now-gauge', es => es.map(e => e.querySelectorAll('.time-track').length)), [5, 7, 7]);
    const timeArc = await page.$eval('.time-arc', e => ({dash: e.getAttribute('stroke-dasharray'), offset: e.getAttribute('stroke-dashoffset')}));
    assert.equal(timeArc.dash, '17 83');
    assert.equal(timeArc.offset, '-1.5');
    assert.equal(await page.$('.now-dig'), null);
    assert.equal(await page.$eval('.now-gauge-logo use[href="#now-claude-builder"]', e => e.getAttribute('href')), '#now-claude-builder');
    assert(await page.$$eval('#now-summary, .now-counts, .now-action, .now-time, .now-limits', es =>
      es.every(e => !/[·•|]/.test(e.textContent))), 'Board values use spacing instead of separator characters');
    assert(await page.$$eval('.now-gauge-ring', es => es.every(e => e.getBoundingClientRect().width === 32)));
    assert(await page.$$eval('.now-gauge-logo', es => es.every(e => e.getBoundingClientRect().width === 32)));
    assert(await page.$$eval('.now-time, .now-context, .now-counts, .now-summary, .now-limits', es =>
      es.every(e => getComputedStyle(e).fontFamily.includes('ui-monospace'))));
    assert(await page.$$eval('.now-model', es => es.every(e => {
      const role = getComputedStyle(e.previousElementSibling), model = getComputedStyle(e);
      return role.fontFamily === model.fontFamily && role.fontSize === model.fontSize && model.opacity === '0.7';
    })));
    assert.equal(await page.$('#now-updated'), null);
    assert(await page.$$eval('.now-card', es => es.every(e => getComputedStyle(e).borderRadius === '5px')));
    // Board grid: every line height, padding, gap and radius on the board is a multiple of 5px.
    const offGrid = await page.$$eval('.now-project, .now-project *', es => es.flatMap(e => {
      if (e.closest('svg')) return [];
      const s = getComputedStyle(e), bad = [];
      for (const prop of ['lineHeight', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'rowGap', 'columnGap', 'borderTopLeftRadius']) {
        const v = parseFloat(s[prop]);
        if (Number.isFinite(v) && s[prop].endsWith('px') && v % 5 && !(prop === 'borderTopLeftRadius' && s[prop] === '50%')) bad.push(`${e.className} ${prop} ${s[prop]}`);
      }
      return bad;
    }));
    assert.deepEqual(offGrid, []);
    assert(await page.$$eval('.now-task', es => es.every(e => {
      const s = getComputedStyle(e); return s.fontWeight === '400' && s.whiteSpace === 'nowrap' && s.textOverflow === 'ellipsis';
    })));
    assert(await page.$$eval('.now-card', es => es.every(e =>
      getComputedStyle(e.querySelector('.now-word')).color === getComputedStyle(e.querySelector('.now-dot')).backgroundColor)));
    payload = {...payload, routing: {...payload.routing, code_to: 'codex', claude: {allowance_per_day: 20, burn_per_day: 10}},
      limits: {claude: [{window_minutes: 300, used_percent: 40, resets_at: payload.now + 3.5 * 3600},
        {window_minutes: 10080, used_percent: 50, resets_at: payload.now + 86400}],
        codex: [{window_minutes: 10080, used_percent: 39, resets_at: payload.now + 86400}]}};
    await page.waitForFunction(() => document.querySelector('.now-gauge-pct').textContent === '40%');
    assert.deepEqual(await page.$$eval('.now-gauge', es => es.map(e => e.dataset.level)), ['crit', 'warn', 'ok']);
    assert.deepEqual(await page.$$eval('.now-gauge-reset', es => es.map(e => e.textContent)), ['4h', '1d ↓', '1d']);
    assert.equal(await page.$eval('.now-gauge-logo use[href="#now-codex-builder"]', e => e.getAttribute('href')), '#now-codex-builder');
    await page.screenshot({path: path.join(artifacts, 'final-rings-light.png'), fullPage: true});
    await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: 'dark'}]);
    await page.screenshot({path: path.join(artifacts, 'final-rings-dark.png'), fullPage: true});
    for (const width of [390, 320]) {
      await page.setViewport({width, height: 1000, deviceScaleFactor: 2});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px header rings reflow`);
    }
    await page.screenshot({path: path.join(artifacts, 'final-rings-narrow.png'), fullPage: true});
    await page.setViewport({width: 1280, height: 1000, deviceScaleFactor: 1});

    // The finished card leaves at exactly 30 minutes; today's done count remains.
    payload = {...payload, now: now + 120 + 1799, limits: {}};
    await page.waitForFunction(() => !document.querySelector('.now-gauge'));
    assert(await page.$('[data-agent-id="gameplay"]'));
    payload = {...payload, now: now + 120 + 1800};
    await page.waitForFunction(() => !document.querySelector('[data-agent-id="gameplay"]'));
    assert.match(await page.$eval('[data-project="Atlas"] .now-counts', e => e.textContent), /1 done/);

    // A handoff updates already-rendered workers without adding a generation.
    const successor = item('successor', 'Atlas', {orchestrator: true, status: 'waiting', task: atlas.task});
    payload = {...payload, agents: payload.agents.filter(a => a.id !== 'atlas').map(a =>
      a.parent_id === 'atlas' ? {...a, parent_id: successor.id} : a.id === 'grandchild' ? {...a, status: 'waiting'} : a).concat(successor)};
    await page.waitForSelector('[data-agent-id="successor"]');
    assert.equal(await page.$('[data-agent-id="atlas"]'), null);
    assert.equal((await ids('Atlas'))[0], 'successor', 'The successor replaces its predecessor above existing workers');
    assert.equal(await page.$eval('[data-agent-id="extra"]', e => e.dataset.parentId), 'successor');
    assert.equal(await page.$eval('[data-agent-id="grandchild"]', e => e.style.marginInlineStart), '42px');
    assert.equal(await value('successor', '.now-word'), 'Turn finished');
    assert.deepEqual(await page.$$eval('.now-card.need', es => es.map(e => e.dataset.agentId)), ['extra']);
    await page.waitForFunction(() => getComputedStyle(document.querySelector('[data-project="Atlas"]')).opacity === '0.5');
    payload = {...payload, agents: payload.agents.map(a => a.id === 'successor' ? {...a, status: 'working', turn_started: at(19, 10)} : a)};
    await page.waitForFunction(() => document.querySelector('[data-agent-id="successor"] .now-time').textContent.startsWith('19:10'));

    for (const [width, dpr] of [[1280, 1], [780, 2], [390, 2], [320, 2]]) {
      await page.setViewport({width, height: 1000, deviceScaleFactor: dpr});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px has no horizontal scroll`);
      assert(await page.$$eval('.now-identity svg', es => es.every(e => e.getBoundingClientRect().width === 15)), 'Native SVG icons');
    }
    await page.screenshot({path: path.join(artifacts, 'v4-narrow.png'), fullPage: true});
    await page.setViewport({width: 1280, height: 1000, deviceScaleFactor: 2});
    await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: 'dark'}, {name: 'prefers-reduced-motion', value: 'reduce'}]);
    assert.equal(await page.$eval('body', e => getComputedStyle(e).backgroundColor), 'rgb(0, 0, 0)');
    assert.equal(await page.$eval('[data-agent-id="inspector"] .now-dot', e => getComputedStyle(e).animationName), 'none');
    await page.screenshot({path: path.join(artifacts, 'v4-dark.png'), fullPage: true});
    // At a scaled viewport the DOM text/icons remain crisp and the columns reflow.
    await page.$eval('body', e => { e.style.zoom = '1.25'; });
    const scaled = await page.evaluate(() => ({width: document.documentElement.scrollWidth, viewport: innerWidth,
      columns: getComputedStyle(document.querySelector('#now-board')).gridTemplateColumns.split(' ').length}));
    assert(scaled.width <= scaled.viewport + 1, 'Scaled compact layout has no overflow beyond pixel rounding');
    assert.equal(scaled.columns, 3, 'Compact 260px columns fit at 125% scale');
    await page.$eval('body', e => { e.style.zoom = ''; });
    // Finished ancestors remain while a nested worker is running, even beyond 30 minutes.
    const oldRoot = item('old-root', 'Retention', {status: 'done', ended: now - 3600});
    const oldParent = item('old-parent', 'Retention', {status: 'done', ended: now - 3600, parent_id: oldRoot.id});
    const nested = item('nested', 'Retention', {parent_id: oldParent.id});
    const beforeAncestors = payload;
    payload = {...payload, agents: [...payload.agents, nested, oldParent, oldRoot]};
    await page.waitForSelector('[data-agent-id="nested"]');
    assert.deepEqual(await ids('Retention'), ['old-root', 'old-parent', 'nested']);
    assert.equal(await page.$eval('[data-agent-id="nested"]', e => e.style.marginInlineStart), '42px');
    payload = beforeAncestors;
    await page.waitForFunction(() => !document.querySelector('[data-project="Retention"]'));
    const retainedCount = await page.$$eval('.now-card', es => es.length);
    fail = true;
    await page.waitForSelector('#now-error:not([hidden])');
    assert.equal(await page.$eval('#now-error', e => e.textContent), 'Source unavailable. Reconnecting…');
    assert.equal(await page.$$eval('.now-card', es => es.length), retainedCount);
    fail = false; payload = {now, agents: [], completed: []};
    await page.waitForSelector('#now-error[hidden]').catch(async error => {
      console.error('Reconnect diagnostic', {polls, ...(await page.evaluate(() => ({hidden: document.hidden,
        selection: getSelection().toString(), hoveringBoard: document.querySelector('#now-board').matches(':hover'),
        summary: document.querySelector('#now-summary').textContent, error: document.querySelector('#now-error').textContent})))});
      throw error;
    });
    assert.equal(await page.$$eval('.now-card', es => es.length), 0, 'Cards omitted by the server leave the board');
    assert.equal(await page.$eval('.now-empty', e => e.textContent), 'Murmuratio reads local Claude Code and Codex sessions. None were found.');
    assert.equal(await page.$eval('.now-empty', e => e.hidden), false);
    for (const [width, dpr] of [[1280, 1], [390, 2]]) {
      await page.setViewport({width, height: 1000, deviceScaleFactor: dpr});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px empty board reflows`);
      await page.screenshot({path: path.join(artifacts, `empty-${width}.png`), fullPage: true});
    }
    const standalone = item('standalone', 'Codex project', {kind: 'codex', model: 'gpt-6.1-sol',
      status: 'done', completed: true, ended: now - 1799, board_source: 'codex_rollout'});
    payload = {now, agents: [], completed: [standalone]};
    await page.waitForSelector('[data-agent-id="standalone"]');
    assert.equal(await page.$eval('.now-empty', e => e.hidden), true);
    assert.equal(await page.$eval('[data-agent-id="standalone"]', e => e.hasAttribute('title')), false);
    await page.screenshot({path: path.join(artifacts, 'codex-only.png'), fullPage: true});
    payload = {...payload, now: now + 1};
    await page.waitForFunction(() => !document.querySelector('[data-agent-id="standalone"]'));
    assert.match(await page.$eval('#now-summary', e => e.textContent), /1 done today/);
    // A Codex-app-only user sees one quiet count, with no project or false empty-state copy.
    payload = {now, agents: [], completed: [], codex_chats: {hidden: 3, running: 2}};
    await page.waitForFunction(() => document.querySelector('#now-summary').textContent.includes('Your Codex chats: 2'));
    assert.equal(await page.$$eval('.now-project', es => es.length), 0);
    assert.equal(await page.$eval('.now-empty', e => e.hidden), true);
    for (const theme of ['light', 'dark']) {
      await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: theme}]);
      for (const [width, dpr] of [[1280, 1], [375, 2]]) {
        await page.setViewport({width, height: 1000, deviceScaleFactor: dpr});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px chat count reflows`);
        await page.screenshot({path: path.join(artifacts, `codex-chat-count-${theme}-${width}.png`), fullPage: true});
      }
    }
    payload = {...payload, codex_chats: {hidden: 3, running: 0}};
    await page.waitForFunction(() => !document.querySelector('#now-summary').textContent.includes('Your Codex chats:'));
    const stopped = ['waiting', 'idle', 'blocked', 'hung', 'crashed'].map(status => item(status, 'Working time', {
      kind: 'codex', status, started: at(8, 0), last_at: now - 60, seconds: 125}));
    payload = {now, agents: stopped, completed: []};
    await page.waitForSelector('[data-agent-id="crashed"]');
    for (const agent of stopped) assert.deepEqual(await parts(agent.id, '.now-time'), ['08:00', '2′']);
    assert.deepEqual(errors, []);
    console.log('Final Board browser checks passed: fixture project parents, SVG tree elbows, exact time strings, stable ordering, in-place completion and source removal, pointer + selection pause/latest queue, English UI, no report/context dumps, safe text, Codex links, keyboard tabs, 1280/780/390/320px DPR1/2, 125% scale, light/dark, reduced motion, reconnect.');
  } finally {
    if (browser) await closeBrowser(browser);
    if (service.exitCode === null) service.kill();
    service.stdout.destroy(); service.stderr.destroy();
    fs.rmSync(fixtureRoot, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
