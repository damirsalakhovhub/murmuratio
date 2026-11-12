'use strict';
const BOARD_REFRESH_MS = 3000;
function sessionTitleDisplay(title = '', source = null) {
  if (source === 'launcher') return title;
  return title.replace(/^↳ */, '→ ');
}

function codexCreditDisplay(credit) {
  const month = Number.isFinite(credit.month_left), reserved = Number.isFinite(credit.reserved_until);
  const monthName = (month || reserved) && new Date((reserved ? credit.reserved_until : credit.month_ends_at - 1) * 1000)
    .toLocaleDateString('en-GB', {month: 'long'});
  return {month, reserved, monthName, value: reserved || !month ? credit.balance : credit.month_left,
    label: reserved ? `credits for ${monthName}` : month ? `credits left in ${monthName}` : 'credits'};
}

function modelLabel(agent, detail = false) {
  const raw = typeof agent.model === 'string' ? agent.model.trim() : '';
  let label = raw || 'model unknown';
  const claude = raw.replace(/^claude-/, '').replace(/-(?:\d{8}|latest)$/, '')
    .match(/^(opus|sonnet|haiku|fable)(?:-(\d+(?:[-.]\d+)*))?$/i);
  const codex = raw.match(/^gpt-\d+(?:\.\d+)*-(sol|terra|luna|astra)$/i);
  if (claude) label = claude[1][0].toUpperCase() + claude[1].slice(1) + (claude[2] ? ' ' + claude[2].replace(/-/g, '.') : '');
  else if (codex) label = codex[1][0].toUpperCase() + codex[1].slice(1);
  const effort = typeof agent.effort === 'string' ? agent.effort.trim() : '';
  return label + (effort ? ' ' + effort : detail ? ' effort unknown' : '');
}

window.createNowView = ({node, text}) => {
  const board = document.getElementById('now-board');
  const summary = document.getElementById('now-summary');
  const limitsEl = document.getElementById('now-limits');
  const error = document.getElementById('now-error');
  const freshness = document.getElementById('now-freshness');
  const calculatorLink = document.querySelector('a[href="/calculator"]');
  let snapshotAt = null;
  function showFreshness() {
    if (!freshness) return;
    const age = Number.isFinite(snapshotAt) ? Math.max(0, Date.now() / 1000 - snapshotAt) : 0;
    freshness.hidden = age <= 2 * BOARD_REFRESH_MS / 1000;
    text(freshness, freshness.hidden ? '' : `Data is ${Math.floor(age / 60)} min old`);
  }
  setInterval(showFreshness, BOARD_REFRESH_MS);
  const empty = node('p', 'now-empty', 'Your Claude Code and Codex chats appear on the board once they start agents.');
  empty.hidden = true; board.append(empty);
  const groups = new Map(), cards = new Map();
  // An approval request needs the owner even when it comes from a worker.
  const needsYou = agent => !agent.completed && agent.status === 'blocked' && (agent.orchestrator || agent.current_action === 'waiting for approval');
  let modules = {}, projectOrder = [];
  const svgNS = 'http://www.w3.org/2000/svg';
  let queued = null, overBoard = false, loadedBuild = '', dayBounds = null;
  let disconnected = false, paused = false, pauseTimer = null, sourceIssue = '';
  const clock = at => Number.isFinite(at) ? new Date(at * 1000).toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'}) : '—';
  const minutes = seconds => `${Math.floor(Math.max(0, seconds || 0) / 60)}′`;
  const sameDay = (a, b) => Number.isFinite(a) && (dayBounds
    ? a >= dayBounds.start && a < dayBounds.end
    : new Date(a * 1000).toDateString() === new Date(b * 1000).toDateString());
  // One format everywhere: when it started and how long it has worked.
  function timeLine(agent, now) {
    if (agent.orchestrator) {
      return agent.status === 'working' ? (Number.isFinite(agent.turn_started)
        ? [clock(agent.turn_started), minutes(now - agent.turn_started)] : [clock(agent.turn_started)])
        : [clock(agent.waiting_since ?? agent.last_at)];
    }
    return [clock(agent.started), ...(Number.isFinite(agent.seconds) ? [minutes(agent.seconds)] : [])];
  }
  function statusText(agent) {
    switch (agent.status) {
      case 'working': return ['Running', agent.quiet ? ['Quiet', agent.current_action].filter(Boolean).join(' ') : agent.current_action];
      case 'waiting': return ['Turn finished'];
      case 'idle': return ['Idle', 'agents working'];
      case 'done': return ['Done'];
      case 'blocked': return ['Blocked', agent.current_action];
      case 'hung': return ['Stalled', 'no events for 10 min'];
      case 'crashed': return ['Stopped', 'process exited'];
      default: return ['Unknown'];
    }
  }
  function paintStatus(status, action, agent) {
    const [word, detail] = statusText(agent);
    status.dataset.status = agent.status;
    status.classList.toggle('quiet', agent.status === 'working' && Boolean(agent.quiet));
    action.replaceChildren(node('span', 'now-word', word), ...(detail ? [node('span', 'now-current', detail)] : []));
    return [word, detail];
  }
  function limitState(window, now) {
    const percent = Math.max(0, Math.min(100, Math.round(window.used_percent)));
    const span = (window.window_minutes || 0) * 60;
    const left = Number.isFinite(window.resets_at) ? Math.max(0, window.resets_at - now) : null;
    const elapsed = span && left !== null ? Math.min(1, Math.max(0, 1 - left / span)) : null;
    const pace = elapsed ? percent / 100 / Math.max(3600 / span, elapsed) : 0;
    const level = percent >= 75 || (percent >= 40 && pace >= 1.3) ? 'crit'
      : percent >= 50 || pace > 1.05 ? 'warn' : 'ok';
    const reset = left === null ? 'reset unknown' : left === 0 ? 'reset passed' : left < 3600
      ? `${Math.ceil(left / 60)}m` : left >= 86400 ? `${Math.round(left / 86400)}d` : `${Math.round(left / 3600)}h`;
    return {percent, elapsed, level, reset};
  }
  function limitRing(window, state) {
    const ring = document.createElementNS(svgNS, 'svg');
    ring.setAttribute('viewBox', '0 0 36 36'); ring.classList.add('now-gauge-ring');
    ring.setAttribute('aria-hidden', 'true');
    function circle(className, radius, attributes = {}) {
      const el = document.createElementNS(svgNS, 'circle'); el.setAttribute('class', className);
      for (const [key, value] of Object.entries({cx: 18, cy: 18, r: radius, ...attributes})) el.setAttribute(key, String(value));
      ring.append(el);
    }
    circle('track', 15.5);
    circle('arc ' + state.level, 15.5, {pathLength: 100, 'stroke-dasharray': `${state.percent} 100`});
    // The inner time ring is segmented into seven days or five hours.
    const parts = window.window_minutes >= 10080 ? 7 : Math.max(1, Math.round((window.window_minutes || 60) / 60));
    const gap = 3, slot = 100 / parts, segment = slot - gap, done = (state.elapsed ?? 0) * 100;
    for (let i = 0; i < parts; i++) {
      const start = i * slot, fill = Math.max(0, Math.min(segment, done - start));
      for (const [className, length] of [['time-track', segment], ['time-arc', fill]]) {
        if (length) circle(className, 10.5, {pathLength: 100, 'stroke-dasharray': `${length} ${100 - length}`,
          'stroke-dashoffset': -(start + gap / 2)});
      }
    }
    const wrap = node('span', 'now-gauge-wrap');
    wrap.append(ring, node('span', 'now-gauge-pct', state.percent + '%'));
    return wrap;
  }
  function limitLogo(kind, codeLeader) {
    const icon = document.createElementNS(svgNS, 'svg'), use = document.createElementNS(svgNS, 'use');
    icon.setAttribute('aria-label', kind === 'claude' ? 'Claude' : 'Codex');
    // The tool that gets the code today shows its builder instead of the logo.
    use.setAttribute('href', '#now-' + kind + (codeLeader ? '-builder' : '')); icon.classList.add('now-gauge-logo'); icon.append(use);
    if (codeLeader) icon.setAttribute('aria-label', (kind === 'claude' ? 'Claude' : 'Codex') + ' gets the code today');
    return icon;
  }
  // Age of a last known sample: 36m, 5h, 2d.
  function ageText(seconds) {
    if (!Number.isFinite(seconds)) return '';
    const m = Math.max(1, Math.round(seconds / 60));
    return m < 60 ? `${m}m` : m < 48 * 60 ? `${Math.round(m / 60)}h` : `${Math.round(m / 1440)}d`;
  }
  function paceMark(window, stats) {
    if (window.window_minutes !== 10080 || !Number.isFinite(stats?.allowance_per_day)
        || !Number.isFinite(stats?.burn_per_day) || stats.burn_per_day <= 0) return null;
    const fast = stats.burn_per_day > stats.allowance_per_day * 1.05, slow = stats.burn_per_day < stats.allowance_per_day * 0.95;
    if (!fast && !slow) return null;
    const mark = node('span', 'now-gauge-pace', fast ? '↑' : '↓');
    tip(mark, {title: fast ? 'Faster than the weekly pace' : 'Slower than the weekly pace',
      rows: [['Spending', `${Math.round(stats.burn_per_day)}% a day`], ['Allowance', `${Math.round(stats.allowance_per_day)}% a day`]]});
    return mark;
  }
  // Tooltips: the first one waits, the next ones show at once while the pointer moves between items.
  const tipEl = node('div', 'now-tip'); tipEl.setAttribute('role', 'tooltip'); tipEl.hidden = true; document.body.append(tipEl);
  let tipTarget = null, tipTimer = 0, tipWarmUntil = 0;
  // A tooltip is a title, an optional note and label/value rows.
  function tip(el, content) { el.nowTip = content; el.dataset.tip = ''; }
  function showTip(el) {
    fillTip(el.nowTip || {}); tipTarget = el;
    const r = el.getBoundingClientRect(), w = tipEl.offsetWidth;
    tipEl.style.left = Math.max(10, Math.min(innerWidth - w - 10, r.left + r.width / 2 - w / 2)) + 'px';
    tipEl.style.top = (r.bottom + 10) + 'px';
  }
  function fillTip({title, note, rows = [], body}) {
    const parts = title ? [node('div', 'now-tip-title', title)] : [];
    if (body) parts.push(...body());
    if (note) parts.push(node('div', 'now-tip-note', note));
    if (rows.length) {
      const grid = node('div', 'now-tip-rows');
      for (const [label, value] of rows) grid.append(node('span', 'now-tip-label', label), node('span', 'now-tip-value', value));
      parts.push(grid);
    }
    tipEl.replaceChildren(...parts); tipEl.hidden = false;
  }
  function hideTip() { clearTimeout(tipTimer); if (!tipEl.hidden) tipWarmUntil = performance.now() + 300; tipEl.hidden = true; tipTarget = null; }
  // Limits and board cards share one tooltip; whatever a narrow card hides comes back here.
  for (const area of [limitsEl, board]) {
    area.addEventListener('pointerover', event => {
      const el = event.target.closest?.('[data-tip]');
      if (!el || el === tipTarget) return;
      clearTimeout(tipTimer);
      if (!tipEl.hidden || performance.now() < tipWarmUntil) showTip(el);
      else tipTimer = setTimeout(() => showTip(el), 500);
    });
    area.addEventListener('pointerout', event => {
      const next = event.relatedTarget?.closest?.('[data-tip]');
      if (next && area.contains(next)) return;
      hideTip();
    });
  }
  addEventListener('scroll', hideTip, {passive: true});
  // Two bars: limits spending and agent assignments. Green means nothing to look at; details live in the tooltip.
  // One week at a glance: used, forecast to reset, where even pace would be now, and the 100% line.
  function weekBar(p, now, lvl) {
    const box = node('div', 'now-week'); box.dataset.level = lvl;
    const used = p.used_percent, projected = Number.isFinite(p.ratio) ? p.ratio * 100 : null;
    const scale = Math.min(150, Math.max(100, projected || 0, used || 0));
    const at = v => (Math.max(0, Math.min(scale, v)) / scale * 100) + '%';
    const head = node('div', 'now-week-head');
    const nums = node('span', 'now-week-nums', used === null || used === undefined ? 'no data' : `${Math.round(used)}% now`);
    if (used !== null && used !== undefined && projected !== null) {
      nums.append(', ', node('span', projected > 105 ? 'now-week-over' : '', `${Math.round(projected)}%`), ' by reset');
    }
    head.append(node('span', 'now-week-name', toolName(p.tool)), nums);
    const track = node('div', 'now-week-track');
    if (projected !== null && used !== null) {
      const ahead = node('span', 'now-week-forecast'); ahead.style.left = at(used); ahead.style.width = `calc(${at(projected)} - ${at(used)})`;
      track.append(ahead);
    }
    const fill = node('span', 'now-week-used'); fill.style.width = at(used || 0); track.append(fill);
    const full = node('span', 'now-week-full'); full.style.left = at(100); track.append(full);
    if (Number.isFinite(p.resets_at)) {
      const pace = Math.max(0, Math.min(100, (now - (p.resets_at - 7 * 86400)) / (7 * 86400) * 100));
      const tick = node('span', 'now-week-pace'); tick.style.left = at(pace); track.append(tick);
    }
    const foot = node('div', 'now-week-foot');
    const out = projected > 105 && Number.isFinite(p.hits_100_at) && p.hits_100_at < (p.resets_at || Infinity);
    foot.append(node('span', out ? 'now-week-out' : '', out ? `runs out ${when(p.hits_100_at)}` : ''),
      node('span', '', Number.isFinite(p.resets_at) ? `resets ${when(p.resets_at)}` : ''));
    box.append(head, track, foot);
    return box;
  }
  // Workers of the last day as segments: green by advice, coloured against it; offenders listed below.
  function workersBar(assign, lvl) {
    const box = node('div', 'now-week now-workers'); box.dataset.level = lvl;
    const judged = assign?.judged || 0, wrong = assign?.wrong || [];
    const head = node('div', 'now-week-head');
    const nums = node('span', 'now-week-nums', judged ? `${judged - wrong.length} of ${judged} by advice`
      : Number.isFinite(assign?.since) ? `none since ${when(assign.since)}` : 'no data');
    head.append(node('span', 'now-week-name', 'Workers'), nums);
    const track = node('div', 'now-workers-track');
    for (let i = 0; i < judged; i++) track.append(node('span', i >= judged - wrong.length ? 'now-workers-seg off' : 'now-workers-seg'));
    box.append(head, track);
    for (const w of wrong.slice(0, 6)) {
      const row = node('div', 'now-week-foot');
      row.append(node('span', 'now-workers-task', w.task), node('span', 'now-week-over', `${toolName(w.kind)}, not ${toolName(w.advice)}`));
      box.append(row);
    }
    if (wrong.length > 6) box.append(node('div', 'now-week-foot', `${wrong.length - 6} more`));
    return box;
  }
  function healthBars(health, now) {
    const wrap = node('span', 'now-health');
    const level = score => score === null || score === undefined ? 'none' : score < 35 ? 'bad' : score < 70 ? 'warn' : 'ok';
    const bar = (key, name, part, content) => {
      const row = node('span', 'now-health-row'); row.dataset.level = level(part?.score); row.dataset.tipKey = 'health-' + key;
      const track = node('span', 'now-health-track'), fill = node('span', 'now-health-fill');
      fill.style.width = Math.max(0, Math.min(100, part?.score ?? 0)) + '%'; track.append(fill);
      row.append(node('span', 'now-health-name', name), track);
      row.setAttribute('aria-label', `${name}: ${part?.reason || 'unknown'}`);
      tip(row, content); wrap.append(row);
    };
    const limits = health.limits;
    bar('limits', 'Limits', limits, {body: () => (limits?.parts || []).map(p => weekBar(p, now, level(p.score)))});
    const assign = health.assignments;
    bar('assignments', 'Agents', assign, {body: () => [workersBar(assign, level(assign?.score))]});
    return wrap;
  }
  function renderLimits(data) {
    const activeKey = tipTarget?.dataset.tipKey;
    limitsEl.replaceChildren();
    const routing = modules.routing ? data.routing : null;
    if (routing && data.health) limitsEl.append(healthBars(data.health, data.now));
    for (const kind of ['claude', 'codex']) {
      const present = [...(data.agents || []), ...(data.completed || [])].some(a => a.kind === kind)
        || (data.limits?.[kind] || []).length
        || Object.entries(data.sources || {}).some(([name, source]) => name.startsWith(kind + '_') && source.status !== 'absent');
      if (!present) continue;
      const windows = [...(data.limits?.[kind] || [])].sort((a, b) => a.window_minutes - b.window_minutes);
      if (!windows.length) {
        const item = node('span', 'now-gauge'); item.dataset.tool = kind;
        item.setAttribute('aria-label', `${toolName(kind)} limits unavailable`);
        const label = node('span', 'now-gauge-label');
        label.append(node('span', 'now-gauge-win', toolName(kind)), node('span', 'now-gauge-reset', 'limits unavailable'));
        item.append(label);
        limitsEl.append(item);
      }
      for (const [index, window] of windows.entries()) {
        const state = limitState(window, data.now), item = node('span', 'now-gauge');
        item.dataset.tool = kind; item.dataset.level = state.level;
        const source = data.sources?.[kind + '_limits'];
        const stale = source?.status === 'stale' || source?.stale === true, old = stale ? ageText(source.age_seconds) : '';
        // A stale sample stays as the last known value with its age; only an unknown age says "stale".
        const staleText = stale ? (old ? `, ${old} old` : ' stale') : '';
        item.setAttribute('aria-label', `${kind === 'claude' ? 'Claude' : 'Codex'} ${window.window_minutes >= 10080 ? 'week' : '5h'} ${state.percent}% used${stale ? (old ? `, last known ${old} ago` : ', stale') : ''}`);
        const label = node('span', 'now-gauge-label');
        const name = kind === 'claude' ? 'Claude' : 'Codex', week = window.window_minutes >= 10080;
        const span = week ? 'week' : Math.round(window.window_minutes / 60) + '-hour';
        const win = node('span', 'now-gauge-win', (week ? 'week' : Math.round(window.window_minutes / 60) + 'h') + ` ${state.percent}%` + staleText);
        if (stale) win.dataset.stale = 'true';
        tip(win, stale ? {title: `${name} ${span} window, last known value`, note: old
          ? `Sampled ${old} ago. ${name} reports limits only while it works.` : 'The sample age is unknown.'}
          : {title: `${name} ${span} window`, note: week ? 'Usage counts over a rolling week' : `Usage counts over a rolling ${span.replace('-hour', ' hours')}`});
        const resetLine = node('span', 'now-gauge-reset'), reset = node('span', '', state.reset);
        if (Number.isFinite(window.resets_at)) tip(reset, {title: `${name} ${span} reset`, rows: [['In', state.reset],
          ['At', new Date(window.resets_at * 1000).toLocaleString('en-GB', {weekday: week ? 'short' : undefined, hour: '2-digit', minute: '2-digit'})]]});
        const pace = paceMark(window, routing?.[kind]);
        resetLine.append(reset, ...(pace ? [' ', pace] : []));
        label.append(win, resetLine);
        if (!index) {
          const other = kind === 'claude' ? 'Codex' : 'Claude';
          const holder = node('span', 'now-gauge-logo-wrap');
          if (modules.branding) holder.append(limitLogo(kind, routing?.code_to === kind));
          else holder.textContent = name;
          tip(holder, routing?.code_to === kind ? {title: `${name} gets the code today`, note: 'It has more weekly quota left'}
            : routing?.code_to ? {title: name, note: `Code goes to ${other} today`} : {title: name});
          item.append(holder);
        }
        const ring = limitRing(window, state);
        tip(ring, {title: `${name} ${span} limit`, rows: [['Used', `${state.percent}%`],
          ['Time passed', `${Math.round((state.elapsed ?? 0) * 100)}%`], ...(state.reset ? [['Resets in', state.reset]] : [])]});
        item.append(ring, label); limitsEl.append(item);
        for (const [el, part] of [[win, 'win'], [reset, 'reset'], [ring, 'ring'], [item.querySelector('.now-gauge-logo-wrap'), 'logo'], [pace, 'pace']]) {
          if (el) el.dataset.tipKey = `${kind}-${window.window_minutes}-${part}`;
        }
      }
    }
    const credit = modules.codex_credits ? data.credits : null;
    if (credit && Number.isFinite(credit.balance)) {
      const short = v => v >= 1000 ? (v / 1000).toFixed(1) + 'k' : String(Math.round(v));
      // The reserved balance and this month's spending allowance are separate values.
      const {month, reserved, monthName, value: displayed, label} = codexCreditDisplay(credit);
      const item = node('span', 'now-credits');
      const codexWeek = (data.limits?.codex || []).find(w => w.window_minutes >= 10080);
      const spending = month && !reserved && codexWeek?.used_percent >= 100 && Number.isFinite(credit.week_share_left);
      const value = node('span', 'now-credits-value', short(displayed));
      item.append(value, node('span', 'now-credits-label', label
        + (spending ? `, spending credits: ${short(credit.week_share_left)} left this week` : '')));
      if (spending) item.dataset.spending = 'true';
      // Credit spend against its prorated weekly budget, independent of quota use.
      const plan = month ? credit.credits_plan : null;
      if (plan) {
        const line = node('span', 'now-credits-plan', `this week ${short(plan.spent_this_week)} of ${short(plan.target_per_week)}, ${plan.status} plan`);
        line.dataset.status = plan.status; item.append(line);
      }
      const number = v => Math.round(v).toLocaleString('en-US');
      tip(item, {title: 'Codex credits', note: reserved ? `Reserved for ${monthName}. This month's credit plan is 0.`
        : month ? `Spent only after the weekly limit hits 100%. This month's part is due by the end of ${monthName}.`
        : 'Spent only after the weekly limit hits 100%',
        rows: [...(plan ? [['Week plan', number(plan.target_per_week)], ['Spent this week', number(plan.spent_this_week)],
          ['Expected by now', number(plan.expected_by_now)], ['Against plan', plan.status]] : []),
          ...(month ? [[reserved ? `Reserved for ${monthName}` : `Left in ${monthName}`, number(displayed)],
          ...(Number.isFinite(credit.week_share_left) ? [['This week\'s share left', Math.round(credit.week_share_left).toLocaleString('en-US')]] : [])] : []),
          ['Balance', Math.round(credit.balance).toLocaleString('en-US')],
          ...(credit.per_hour ? [['Spend', `about ${Math.round(credit.per_hour)} an hour of work`], ['Lasts', `about ${Math.round(credit.balance / credit.per_hour)} work hours`],
            ['Measured on', `${Math.round(credit.sample_hours * 60)} min of use`]] : [])]});
      item.dataset.tipKey = 'codex-credits';
      limitsEl.append(item);
    } else if (modules.codex_credits) {
      limitsEl.append(node('span', 'now-credits', 'Codex credits unavailable'));
    }
    // A reminder only: the full reset itself is started in the Codex app.
    const reminder = routing?.codex_reset_reminder, reminderEl = document.getElementById('now-reminder');
    const remind = Boolean(reminderEl && reminder && Number.isFinite(reminder.expires_at));
    if (remind) {
      const expires = new Date(reminder.expires_at * 1000).toLocaleString('en-GB', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
      const more = reminder.available > 1 ? `, ${reminder.available} available` : '';
      text(reminderEl, `Use a Codex full reset: the week is at 100% with 3 or more days to go. ` +
        `Start it in the Codex app before ${expires}${more}.`);
    }
    if (reminderEl) reminderEl.hidden = !remind;
    // A live refresh replaces the nodes; keep an open tooltip on the same item.
    const again = activeKey && limitsEl.querySelector(`[data-tip-key="${activeKey}"]`);
    if (again) showTip(again); else if (activeKey) hideTip();
  }
  // Weekly forecast: actual usage (solid), straight forecast at the week's average pace (dashed),
  // and the even pace that lands exactly on 100% at reset (faint). One % axis, shared time axis.
  const forecastEl = document.getElementById('now-forecast');
  let forecastKey = '', forecastData = null;
  const toolName = tool => tool === 'claude' ? 'Claude' : 'Codex';
  const when = at => new Date(at * 1000).toLocaleString('en-GB', {weekday: 'short', hour: '2-digit', minute: '2-digit'});
  const span = seconds => {
    const h = Math.round(Math.max(0, seconds) / 3600);
    return h >= 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : `${h}h`;
  };
  // Usage is spread across the configured local work hours; nights stay flat.
  let WORK = [9, 21], WORK_DAY = 12;
  function workHours(a, b) {
    if (!(b > a)) return 0;
    let total = 0;
    const d = new Date(a * 1000); d.setHours(0, 0, 0, 0);
    for (let day = d.getTime() / 1000; day < b; ) {
      const next = new Date(day * 1000); next.setDate(next.getDate() + 1); next.setHours(0, 0, 0, 0);
      const open = new Date(day * 1000); open.setHours(WORK[0], 0, 0, 0);
      const close = new Date(day * 1000); close.setHours(WORK[1], 0, 0, 0);
      total += Math.max(0, Math.min(b, close / 1000) - Math.max(a, open / 1000));
      day = next.getTime() / 1000;
    }
    return total / 3600;
  }
  function addWorkHours(t, hours) {
    let at = t, left = hours * 3600;
    for (let i = 0; i < 400 && left > 0; i++) {
      const open = new Date(at * 1000); open.setHours(WORK[0], 0, 0, 0);
      const close = new Date(at * 1000); close.setHours(WORK[1], 0, 0, 0);
      if (at < open / 1000) at = open / 1000;
      if (at >= close / 1000) { const n = new Date(at * 1000); n.setDate(n.getDate() + 1); n.setHours(WORK[0], 0, 0, 0); at = n / 1000; continue; }
      const step = Math.min(left, close / 1000 - at); at += step; left -= step;
    }
    return at;
  }
  // Work-hour breakpoints make the stepped lines bend in the right places.
  function workBreaks(a, b) {
    const out = [a], d = new Date(a * 1000); d.setHours(0, 0, 0, 0);
    for (let i = 0; i < 20; i++) {
      for (const h of WORK) { const t = new Date(d); t.setHours(h, 0, 0, 0); if (t / 1000 > a && t / 1000 < b) out.push(t / 1000); }
      d.setDate(d.getDate() + 1); if (d / 1000 > b) break;
    }
    out.push(b); return out;
  }
  const baseRate = (f, now) => {
    if (Number.isFinite(f.rate_per_hour)) return f.rate_per_hour;
    const h = workHours(f.start, now);
    return h >= 3 ? f.used_percent / h : Number.isFinite(f.pace_per_day) ? f.pace_per_day / WORK_DAY : 100 / 7 / WORK_DAY;
  };
  function verdict(f) {
    if (!f || !Number.isFinite(f.pace_per_day)) return {text: 'limits unavailable', level: 'ok'};
    if (Number.isFinite(f.hits_100_at) && f.cap > 100) {
      return Number.isFinite(f.credits_out_at)
        ? {text: `limit ${when(f.hits_100_at)}, credits out ${when(f.credits_out_at)}`, level: 'crit'}
        : {text: `limit ${when(f.hits_100_at)}, then credits`, level: 'warn'};
    }
    if (Number.isFinite(f.hits_100_at)) return {text: `out ${when(f.hits_100_at)}, ${span(f.resets_at - f.hits_100_at)} early`, level: 'crit'};
    const end = Math.round(f.projected_at_reset);
    return {text: `fits, ${end}% at reset`, level: end >= 90 ? 'warn' : 'ok'};
  }
  // Scenarios move worker tasks between the tools. Claude's chats (orchestrators, the owner's own chats)
  // stay on Claude whatever the split; only its worker part scales with the share of work.
  const scenarios = [['now', 'As now'], ['rebalance', 'Rebalance'], ['claude', 'All Claude'], ['codex', 'All Codex']];
  let scenario = 'now';
  try { scenario = localStorage.getItem('now-forecast-scenario') || 'now'; } catch {}
  if (!scenarios.some(([id]) => id === scenario)) scenario = 'now';
  function project(f, rate, now) {
    if (!Number.isFinite(rate)) return {...f, rate: null, pace_per_day: null, projected_at_reset: null, hits_100_at: null};
    const projected = f.used_percent + rate * workHours(now, f.resets_at);
    const hits = f.used_percent >= 100 ? now : projected > 100 && rate > 0 ? addWorkHours(now, (100 - f.used_percent) / rate) : null;
    // Codex credits extend the week past 100%: `cap` is 100% plus what the balance buys, in weekly %.
    const cap = f.cap || 100;
    const out = projected > cap && rate > 0 && cap > 100 ? addWorkHours(now, (cap - f.used_percent) / rate) : null;
    return {...f, rate, pace_per_day: rate * WORK_DAY, projected_at_reset: projected, hits_100_at: hits, credits_out_at: out};
  }
  function plan(forecast, now, share) {
    const c = forecast.claude, x = forecast.codex, rc = c && baseRate(c, now), rx = x && baseRate(x, now);
    if (!Number.isFinite(rc) || !Number.isFinite(rx)) return null;
    const s0 = Math.min(0.9, Math.max(0.1, share)), movable = Number.isFinite(c.worker_share) ? c.worker_share : 1;
    const fixedC = rc * (1 - movable), unitC = rc * movable / (1 - s0), unitX = rx / s0;
    const at = s => ({claude: project(c, fixedC + unitC * (1 - s), now), codex: project(x, unitX * s, now), share: s});
    // Fullness against each tool's capacity (Codex: limit plus credits), in % of that capacity.
    const worst = r => Math.max(r.claude.projected_at_reset, r.codex.projected_at_reset / (x.cap || 100) * 100);
    let best = at(0);
    for (let i = 1; i <= 100; i++) { const r = at(i / 100); if (worst(r) < worst(best)) best = r; }
    // «As now» keeps each tool's measured pace exactly.
    return {now: {claude: project(c, rc, now), codex: project(x, rx, now), share}, rebalance: best, claude: at(0), codex: at(1), worst, movable};
  }
  function workShare(agents, now) {
    const sec = {claude: 0, codex: 0};
    for (const a of agents) {
      if (a.orchestrator || !Number.isFinite(a.seconds) || !sameDay(a.ended ?? a.last_at, now)) continue;
      if (a.kind in sec) sec[a.kind] += a.seconds;
    }
    const total = sec.claude + sec.codex;
    return total ? sec.codex / total : 0.5;
  }
  // Answers: a plain label, then short values; a tool's value starts with that tool's icon.
  function answers(forecast, now, share) {
    const p = modules.scenarios ? plan(forecast, now, share) : null, pct = v => Math.round(v * 100) + '%';
    const out = [];
    const view = p ? p[scenario] : Object.fromEntries(Object.entries(forecast).map(([t, f]) => [t, f && project(f, baseRate(f, now), now)]));
    if (p) out.push([scenario === 'now' ? 'Work split today' : 'Work split', [[pct(1 - view.share), 'claude'], [pct(view.share), 'codex']], 'Share of worker time per tool']);
    if (p && scenario === 'rebalance') {
      const w = Math.round(p.worst(p.rebalance));
      out.push(['Best split result', [[w <= 100 ? `both fit, fuller at ${w}%` : `short by ${w - 100}%`]], 'With the best split, the fuller tool ends at this % of its capacity']);
    }
    const cr = modules.codex_credits ? forecast.codex?.credits : null;
    if (cr && forecast.codex.cap > 100) {
      const rate = baseRate(forecast.codex, now), cpp = forecast.codex.credits_per_percent;
      const hours = Number.isFinite(cpp) && rate > 0 ? cr.balance / cpp / rate : cr.per_hour > 0 ? cr.balance / cr.per_hour : null;
      out.push(['Codex credits', [[`${Math.round(cr.balance).toLocaleString('en-US')}` + (hours !== null ? `, ~${Math.round(hours)} hours` : ''), 'codex']], 'Used only after the weekly limit hits 100% at the forecast pace']);
    }
    if (p && p.movable < 1) out.push(['Claude use', [[`chats ${pct(1 - p.movable)}, tasks ${pct(p.movable)}`, 'claude']], 'Chats: orchestrators and your own chats, they stay on Claude. Tasks: worker sessions, they can move to Codex']);
    const budget = ['claude', 'codex'].filter(t => forecast[t] && workHours(now, forecast[t].resets_at) > 0)
      .map(t => [`${Math.round((100 - forecast[t].used_percent) / workHours(now, forecast[t].resets_at) * WORK_DAY)}%/day`, t]);
    if (budget.length) out.push(['To last until reset', budget, 'Most each tool can spend per work day and still last to its reset']);
    out.push(['Work hours', [[`${WORK[0]}:00–${WORK[1]}:00`]], 'Usage is expected only in these hours; nights stay flat']);
    return {view, rows: out, scenarios: Boolean(p)};
  }
  // The block has two views: the weekly forecast and the burn rate by model.
  let blockView = 'forecast', burnPeriod = 'today', burnData = {};
  try { blockView = localStorage.getItem('now-block-view') === 'burn' ? 'burn' : 'forecast'; burnPeriod = localStorage.getItem('now-burn-period') === 'week' ? 'week' : 'today'; } catch {}
  function segmented(options, current, label, onPick) {
    const group = node('div', 'now-forecast-scenarios'); group.setAttribute('role', 'radiogroup'); group.setAttribute('aria-label', label);
    for (const [id, text] of options) {
      const b = node('button', 'now-forecast-scenario', text); b.type = 'button';
      b.setAttribute('role', 'radio'); b.setAttribute('aria-checked', String(id === current)); b.dataset.option = id;
      b.addEventListener('click', () => onPick(id));
      group.append(b);
    }
    return group;
  }
  function rerender() { forecastKey = ''; if (forecastData) renderForecast(forecastData.forecast, forecastData.now, forecastData.share); }
  const viewSwitch = () => segmented([['forecast', 'Forecast'], ['burn', narrowChart() ? 'Burn' : 'Burn rate']], blockView, 'View', id => {
    blockView = id; try { localStorage.setItem('now-block-view', id); } catch {}
    rerender();
  });
  const MODELS = [[/fable/i, 'Fable', 'fable'], [/opus/i, 'Opus', 'opus'], [/sonnet/i, 'Sonnet', 'sonnet'], [/haiku/i, 'Haiku', 'haiku'],
    [/terra/i, 'Terra', 'terra'], [/sol/i, 'Sol', 'sol'], [/luna/i, 'Luna', 'luna']];
  const family = model => MODELS.find(([re]) => re.test(model)) || [null, 'Other', 'other'];
  function renderBurn(now) {
    const tools = ['claude', 'codex'].filter(t => burnData[t]);
    forecastEl.classList.toggle('empty', !tools.length);
    const head = node('div', 'now-forecast-head'), top = node('div', 'now-forecast-top'), bottom = node('div', 'now-forecast-bottom');
    const title = node('p', 'now-forecast-verdict');
    title.append(node('b', '', 'Weekly limit spent per ' + (burnPeriod === 'today' ? 'hour, today' : 'day, this week')));
    const period = segmented([['today', 'Today'], ['week', 'Week']], burnPeriod, 'Period', id => {
      burnPeriod = id; try { localStorage.setItem('now-burn-period', id); } catch {}
      rerender();
    });
    period.classList.add('now-burn-period');
    forecastEl.classList.toggle('narrow', narrowChart());
    top.append(title, period, ...(modules.forecast ? [viewSwitch()] : []));
    // Bins: today's hours, or the days of each tool's week.
    const day0 = new Date(now * 1000); day0.setHours(0, 0, 0, 0);
    const bins = tool => {
      if (burnPeriod === 'today') return Array.from({length: 24}, (_, h) => day0 / 1000 + h * 3600);
      const first = new Date(burnData[tool].start * 1000); first.setHours(0, 0, 0, 0);
      return Array.from({length: 8}, (_, d) => { const x = new Date(first); x.setDate(x.getDate() + d); return x / 1000; });
    };
    const seen = new Map();
    const series = Object.fromEntries(tools.map(tool => {
      const edges = bins(tool), sums = edges.map(() => ({}));
      for (const [hour, by] of burnData[tool].hours) {
        const i = edges.findLastIndex(edge => hour >= edge);
        if (i < 0 || (burnPeriod === 'today' && hour >= edges[0] + 86400)) continue;
        for (const [model, pct] of Object.entries(by)) {
          const [, name, key] = family(model); seen.set(key, name);
          sums[i][key] = (sums[i][key] || 0) + pct;
        }
      }
      return [tool, {edges, sums}];
    }));
    const legend = node('p', 'now-forecast-legend now-burn-legend');
    for (const [, name, key] of [...MODELS, [null, 'Other', 'other']]) {
      if (!seen.has(key)) continue;
      const k = node('span', 'now-forecast-key'); k.dataset.model = key; k.append(node('i', 'now-burn-swatch'), name); legend.append(k);
    }
    const note = node('p', 'now-burn-note', 'Claude per model is estimated from token cost; Codex from its meter in 1% steps');
    bottom.append(note, legend);
    head.append(top, bottom);
    forecastEl.replaceChildren(head);
    const charts = node('div', 'now-burn-charts');
    const narrow = narrowChart();
    const W = narrow ? Math.max(200, forecastEl.clientWidth - 20) : Math.max(300, (forecastEl.clientWidth - 20 - 20) / 2);
    const H = narrow ? 200 : 500 - 20 - 90 - 10, pad = {l: 40, r: 10, t: 25, b: 30};
    for (const tool of tools) {
      const {edges, sums} = series[tool];
      const totals = sums.map(s => Object.values(s).reduce((a, b) => a + b, 0));
      const top = Math.max(1, ...totals) * 1.15, step = top > 20 ? 10 : top > 8 ? 5 : top > 4 ? 2 : top > 2 ? 1 : 0.5;
      const svg = document.createElementNS(svgNS, 'svg');
      svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W); svg.setAttribute('height', H);
      svg.classList.add('now-forecast-chart', 'now-burn-chart'); svg.dataset.tool = tool; svg.setAttribute('role', 'img');
      svg.setAttribute('aria-label', `${toolName(tool)}: weekly limit spent per ${burnPeriod === 'today' ? 'hour' : 'day'}, by model`);
      const add = (tag, attrs, parent = svg) => {
        const el = document.createElementNS(svgNS, tag);
        for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
        parent.append(el); return el;
      };
      const Y = v => pad.t + (1 - v / top) * (H - pad.t - pad.b), slot = (W - pad.l - pad.r) / edges.length;
      add('text', {x: pad.l, y: 15, class: 'label'}).textContent = toolName(tool);
      if (!totals.some(v => v > 0)) add('text', {x: pad.l + 10, y: pad.t + 20, class: 'label'}).textContent = 'No usage recorded this week yet';
      for (let v = 0; v <= top; v += step) {
        add('line', {x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v), class: 'grid'});
        add('text', {x: pad.l - 10, y: Y(v) + 4, class: 'axis', 'text-anchor': 'end'}).textContent = (step < 1 ? v.toFixed(1) : v) + '%';
      }
      edges.forEach((edge, i) => {
        const x = pad.l + i * slot, label = burnPeriod === 'today'
          ? (i % 3 ? '' : String(i).padStart(2, '0'))
          : new Date(edge * 1000).toLocaleDateString('en-GB', {weekday: 'short'});
        if (label) add('text', {x: x + slot / 2, y: H - 10, class: 'axis', 'text-anchor': 'middle'}).textContent = label;
        let base = 0;
        const g = add('g', {class: 'now-burn-bar'});
        for (const [, , key] of [...MODELS, [null, 'Other', 'other']]) {
          const v = sums[i][key]; if (!v) continue;
          add('rect', {x: x + slot * 0.15, width: slot * 0.7, y: Y(base + v), height: Math.max(1, Y(base) - Y(base + v) - 1), rx: 2, 'data-model': key}, g);
          base += v;
        }
        add('rect', {x, y: pad.t, width: slot, height: H - pad.t - pad.b, class: 'hit'}, g).addEventListener('pointermove', event => {
          const rows = Object.entries(sums[i]).sort((a, b) => b[1] - a[1]).map(([key, v]) => [seen.get(key) || key, v.toFixed(1) + '%']);
          fillTip({title: burnPeriod === 'today' ? `${String(i).padStart(2, '0')}:00–${String(i + 1).padStart(2, '0')}:00` : new Date(edge * 1000).toLocaleDateString('en-GB', {weekday: 'long', day: 'numeric'}),
            note: rows.length ? `${totals[i].toFixed(1)}% of the ${toolName(tool)} week` : 'Nothing spent', rows});
          const w = tipEl.offsetWidth;
          tipEl.style.left = Math.max(10, Math.min(innerWidth - w - 10, event.clientX + 15)) + 'px';
          tipEl.style.top = (event.clientY + 15) + 'px';
        });
        g.lastChild.addEventListener('pointerleave', () => { tipEl.hidden = true; });
      });
      if (burnPeriod === 'today') {
        const x = pad.l + (now - edges[0]) / 3600 * slot;
        add('line', {x1: x, x2: x, y1: pad.t, y2: H - pad.b, class: 'now'});
      }
      charts.append(svg);
    }
    if (!tools.length) charts.append(node('p', 'now-burn-note now-burn-empty', 'No usage recorded this week yet'));
    forecastEl.append(charts);
  }
  // Charts can be put away from the header; the choice stays in this browser.
  const chartsToggle = document.getElementById('charts-toggle');
  let chartsOn = false, forecastArgs = null;
  try { chartsOn = localStorage.getItem('now-charts') === 'on'; } catch {}
  function paintChartsToggle() {
    chartsToggle.setAttribute('aria-pressed', String(chartsOn));
    chartsToggle.title = chartsOn ? 'Hide charts' : 'Show charts';
  }
  paintChartsToggle();
  chartsToggle.addEventListener('click', () => {
    chartsOn = !chartsOn; paintChartsToggle();
    try { localStorage.setItem('now-charts', chartsOn ? 'on' : 'off'); } catch {}
    forecastKey = '';
    if (chartsOn && forecastArgs) renderForecast(...forecastArgs);
    animateCharts(chartsOn);
  });
  // Height and opacity fold together, so the board below rides up with the block; gap folds too.
  let chartsMotion = null;
  function animateCharts(show) {
    const [time = '0ms', easing = 'ease'] = getComputedStyle(document.documentElement).getPropertyValue('--motion-hover').trim().split(/\s+/);
    const duration = parseFloat(time) * (time.endsWith('ms') ? 1 : 1000) || 0;
    chartsMotion?.cancel(); chartsMotion = null;
    if (show && forecastEl.hidden) return;
    if (!duration || matchMedia('(prefers-reduced-motion: reduce)').matches) {
      forecastEl.hidden = !show; requestAnimationFrame(drawTrees); return;
    }
    forecastEl.hidden = false;
    const style = getComputedStyle(forecastEl), gap = parseFloat(getComputedStyle(forecastEl.parentElement).rowGap) || 0;
    const open = {height: forecastEl.offsetHeight + 'px', opacity: 1, marginBottom: '0px', paddingTop: style.paddingTop, paddingBottom: style.paddingBottom};
    const closed = {height: '0px', opacity: 0, marginBottom: -gap + 'px', paddingTop: '0px', paddingBottom: '0px'};
    const motion = forecastEl.animate(show ? [closed, open] : [open, closed], {duration, easing});
    chartsMotion = motion;
    motion.onfinish = () => {
      if (chartsMotion !== motion) return;
      chartsMotion = null;
      if (!show) forecastEl.hidden = true;
      drawTrees();
    };
  }
  // Phone width: one control row, a shorter chart, fewer ticks; the verdicts carry the end labels.
  const narrowChart = () => forecastEl.clientWidth < 560;
  const shortScenario = {now: 'Now', rebalance: 'Rebalance', claude: 'Claude', codex: 'Codex'};
  function renderForecast(forecast, now, share = 0.5) {
    forecast = forecast || {};
    forecastArgs = [forecast, now, share];
    const tools = ['claude', 'codex'].filter(tool => forecast?.[tool]);
    if (!modules.forecast) blockView = 'burn';
    if (!modules.burn_rate) blockView = 'forecast';
    if (!modules.scenarios) scenario = 'now';
    const visible = blockView === 'burn' ? modules.burn_rate : modules.forecast;
    chartsToggle.hidden = !visible;
    if (!chartsMotion) forecastEl.hidden = !visible || !chartsOn;
    if (!visible) { forecastData = null; return; }
    if (!chartsOn) { forecastKey = ''; return; }
    forecastEl.classList.toggle('narrow', narrowChart()); // measured only once the block is shown
    forecastEl.classList.toggle('empty', !tools.length);
    const key = JSON.stringify([forecast, Math.floor(now / 300), Math.round(share * 100), scenario, forecastEl.clientWidth, blockView, burnPeriod, burnData]);
    if (key === forecastKey) return;
    if (blockView === 'burn') { forecastKey = key; forecastData = {forecast, now, share}; renderBurn(now); return; }
    if (key === forecastKey) return;
    forecastKey = key; forecastData = {forecast: forecast, now, share};
    // Credits use the default conversion even before any credit spend is measured.
    const credit = modules.codex_credits ? forecast.codex?.credits : null, rx = forecast.codex && baseRate(forecast.codex, now);
    const cpp = forecast.codex?.credits_per_percent;
    if (credit && credit.balance > 0 && Number.isFinite(cpp) && cpp > 0) {
      forecast = {...forecast, codex: {...forecast.codex, cap: 100 + credit.balance / cpp}};
    } else if (credit && credit.balance > 0 && credit.per_hour > 0 && Number.isFinite(rx)) {
      forecast = {...forecast, codex: {...forecast.codex, cap: 100 + credit.balance / credit.per_hour * rx}};
    }
    const result = answers(forecast, now, share);
    forecast = {...forecast, ...Object.fromEntries(tools.map(t => [t, result.view[t] || forecast[t]]))};
    const head = node('div', 'now-forecast-head');
    const top = node('div', 'now-forecast-top');
    const verdicts = node('div', 'now-forecast-verdicts');
    for (const tool of ['claude', 'codex']) {
      const v = verdict(forecast[tool]), item = node('p', 'now-forecast-verdict');
      const old = ageText(forecast[tool]?.last_known_age_seconds);
      item.dataset.tool = tool; item.dataset.level = v.level;
      if (old) item.dataset.stale = 'true';
      item.append(node('span', 'now-forecast-swatch'), node('b', '', toolName(tool)), node('span', '', (old ? `last known ${old} ago, ` : '') + v.text));
      verdicts.append(item);
    }
    const switcher = node('div', 'now-forecast-scenarios'); switcher.setAttribute('role', 'radiogroup'); switcher.setAttribute('aria-label', 'Scenario');
    const narrow = narrowChart();
    for (const [id, label] of scenarios) {
      const b = node('button', 'now-forecast-scenario', narrow ? shortScenario[id] || label : label); b.type = 'button';
      if (narrow) b.title = label;
      b.setAttribute('role', 'radio'); b.setAttribute('aria-checked', String(id === scenario)); b.dataset.scenario = id;
      b.addEventListener('click', () => {
        scenario = id; try { localStorage.setItem('now-forecast-scenario', id); } catch {}
        forecastKey = ''; renderForecast(forecastData.forecast, forecastData.now, forecastData.share);
      });
      switcher.append(b);
    }
    top.append(verdicts, ...(modules.scenarios && result.scenarios ? [switcher] : []), ...(modules.burn_rate && !narrow ? [viewSwitch()] : []));
    // Answers sit side by side (label over value), so the chart keeps the same height in every scenario.
    const facts = node('div', 'now-forecast-answers');
    for (const [label, value, hint] of result.rows) {
      const fact = node('div', 'now-forecast-fact'), values = node('span', 'now-forecast-fact-values');
      for (const [text, tool] of value) {
        const v = node('span', 'now-forecast-value');
        if (tool && modules.branding) {
          const icon = document.createElementNS(svgNS, 'svg'), use = document.createElementNS(svgNS, 'use');
          icon.setAttribute('aria-label', toolName(tool)); use.setAttribute('href', '#now-' + tool); icon.append(use); v.append(icon);
        } else if (tool) v.append(toolName(tool) + ' ');
        v.append(text); values.append(v);
      }
      fact.append(node('span', 'now-forecast-fact-label', label), values);
      fact.title = hint;
      facts.append(fact);
    }
    const legend = node('p', 'now-forecast-legend');
    for (const [cls, label] of [['actual', 'used'], ['projected', 'forecast'], ['even', narrow ? 'even' : 'even pace']]) {
      const k = node('span', 'now-forecast-key ' + cls); k.append(node('i'), label); legend.append(k);
    }
    const bottom = node('div', 'now-forecast-bottom'); bottom.append(facts, legend, ...(modules.burn_rate && narrow ? [viewSwitch()] : []));
    head.append(top, bottom);
    forecastEl.replaceChildren(head);
    if (!tools.length) return;
    const W = Math.max(narrow ? 200 : 300, forecastEl.clientWidth - 20), H = narrow ? 260 : 500 - 20 - 90 - 10; // fixed head height keeps the chart the same size
    const pad = narrow ? {l: 36, r: 12, t: 15, b: 30} : {l: 40, r: 120, t: 15, b: 30};
    const x0 = Math.min(...tools.map(t => forecast[t].start)), x1 = Math.max(...tools.map(t => forecast[t].resets_at));
    const X = at => pad.l + (at - x0) / (x1 - x0) * (W - pad.l - pad.r);
    const Y = pct => pad.t + (1 - Math.min(110, Math.max(0, pct)) / 110) * (H - pad.t - pad.b);
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W); svg.setAttribute('height', H);
    svg.classList.add('now-forecast-chart'); svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', tools.map(t => `${toolName(t)}: ${verdict(forecast[t]).text}`).join('. '));
    const add = (tag, attrs, parent = svg) => {
      const el = document.createElementNS(svgNS, tag);
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
      parent.append(el); return el;
    };
    for (const pct of [0, 25, 50, 75, 100]) {
      add('line', {x1: pad.l, x2: W - pad.r, y1: Y(pct), y2: Y(pct), class: pct === 100 ? 'limit' : 'grid'});
      add('text', {x: pad.l - 10, y: Y(pct) + 4, class: 'axis', 'text-anchor': 'end'}).textContent = pct + '%';
    }
    const day = new Date(x0 * 1000); day.setHours(24, 0, 0, 0);
    // Day labels need about 30px ("Mon") or 50px ("Mon 3"); skip days until they fit.
    const perDay = (W - pad.l - pad.r) * 86400 / (x1 - x0), withDate = perDay >= 60, every = Math.max(1, Math.ceil((withDate ? 50 : 30) / perDay));
    for (let at = day.getTime() / 1000, i = 0; at < x1; at += 86400, i++) {
      add('line', {x1: X(at), x2: X(at), y1: pad.t, y2: H - pad.b, class: 'grid'});
      if (i % every || X(at + 43200) > W - pad.r) continue;
      add('text', {x: X(at + 43200), y: H - 10, class: 'axis', 'text-anchor': 'middle'}).textContent =
        new Date(at * 1000).toLocaleDateString('en-GB', withDate ? {weekday: 'short', day: 'numeric'} : {weekday: 'short'});
    }
    add('line', {x1: X(now), x2: X(now), y1: pad.t, y2: H - pad.b, class: 'now'});
    // Each tool's reset is its deadline: a vertical line in its colour, labelled at the top.
    const resetMarks = [];
    for (const tool of tools) {
      const f = forecast[tool], g = add('g', {class: 'series', 'data-tool': tool});
      add('line', {x1: X(f.resets_at), x2: X(f.resets_at), y1: pad.t, y2: H - pad.b, class: 'reset'}, g);
      resetMarks.push({tool, f});
    }
    add('text', {x: X(now), y: pad.t - 3, class: 'axis', 'text-anchor': 'middle'}).textContent = 'now';
    const labels = [];
    for (const tool of tools) {
      const f = forecast[tool], g = add('g', {class: 'series', 'data-tool': tool});
      const total = workHours(f.start, f.resets_at);
      add('path', {d: workBreaks(f.start, f.resets_at).map((t, i) => `${i ? 'L' : 'M'} ${X(t)} ${Y(100 * workHours(f.start, t) / total)}`).join(' '), class: 'even'}, g);
      const pts = f.points.filter(([at]) => at >= f.start);
      if (pts.length) add('path', {d: `M ${X(f.start)} ${Y(0)} L ${X(pts[0][0])} ${Y(pts[0][1])}`, class: 'assumed'}, g);
      add('path', {d: pts.map(([at, pct], i) => `${i ? 'L' : 'M'} ${X(at)} ${Y(pct)}`).join(' '), class: 'actual'}, g);
      if (Number.isFinite(f.pace_per_day)) {
        const endAt = Number.isFinite(f.hits_100_at) ? f.hits_100_at : f.resets_at;
        const endPct = Number.isFinite(f.hits_100_at) ? 100 : f.projected_at_reset;
        add('path', {d: workBreaks(now, endAt).map((t, i) => `${i ? 'L' : 'M'} ${X(t)} ${Y(Math.min(100, f.used_percent + f.rate * workHours(now, t)))}`).join(' '), class: 'projected'}, g);
        let markAt = endAt, markText = Number.isFinite(f.hits_100_at) ? `100% ${when(f.hits_100_at)}` : `${Math.round(endPct)}% at reset`;
        if (Number.isFinite(f.hits_100_at) && f.cap > 100) {
          // After the limit, Codex runs on credits: a thick line along 100% until they run out or the week resets.
          markAt = Number.isFinite(f.credits_out_at) ? f.credits_out_at : f.resets_at;
          add('line', {x1: X(f.hits_100_at), x2: X(markAt), y1: Y(100), y2: Y(100), class: 'credits'}, g);
          markText = Number.isFinite(f.credits_out_at) ? `credits out ${when(f.credits_out_at)}` : 'credits to reset';
        }
        add('circle', {cx: X(markAt), cy: Y(endPct), r: 4, class: 'end'}, g);
        if (!narrow) labels.push({tool, x: X(markAt) + 10, y: Y(endPct) + 4, text: markText});
      }
      add('circle', {cx: X(now), cy: Y(f.used_percent), r: 4, class: 'dot'}, g);
      const atStart = X(now) < pad.l + 100;
      labels.push({tool, x: X(now) + (atStart ? 10 : -10), y: Y(f.used_percent) - 10,
        anchor: atStart ? 'start' : 'end', text: `${toolName(tool)} ${Math.round(f.used_percent)}%`});
    }
    // Labels that would overlap move apart vertically.
    labels.sort((a, b) => a.y - b.y);
    for (let i = 1; i < labels.length; i++) {
      const prev = labels[i - 1], cur = labels[i];
      if (Math.abs(prev.x - cur.x) < 140 && cur.y - prev.y < 15) cur.y = prev.y + 15;
    }
    for (const l of labels) add('text', {x: l.x, y: l.y, class: 'label', 'data-tool': l.tool, 'text-anchor': l.anchor || 'start'}).textContent = l.text;
    const cross = add('line', {y1: pad.t, y2: H - pad.b, class: 'cross', visibility: 'hidden'});
    const hit = add('rect', {x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: H - pad.t - pad.b, class: 'hit'});
    const valueAt = (f, at) => {
      if (at < f.start || at > f.resets_at) return null;
      if (at <= now) {
        const pts = [[f.start, 0], ...f.points.filter(([t]) => t >= f.start)];
        let v = pts[0][1];
        for (const [t, pct] of pts) if (t <= at) v = pct;
        return {pct: v, kind: 'used'};
      }
      if (!Number.isFinite(f.rate)) return null;
      return {pct: Math.min(100, f.used_percent + f.rate * workHours(now, at)), kind: 'forecast'};
    };
    hit.addEventListener('pointermove', event => {
      const box = svg.getBoundingClientRect(), px = (event.clientX - box.left) * W / box.width;
      const at = x0 + (px - pad.l) / (W - pad.l - pad.r) * (x1 - x0);
      cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.setAttribute('visibility', 'visible');
      const rows = tools.map(t => [toolName(t), valueAt(forecast[t], at)]).filter(([, v]) => v)
        .map(([name, v]) => [name, `${Math.round(v.pct)}%${v.kind === 'forecast' ? ' forecast' : ''}`]);
      fillTip({title: when(at), rows});
      const w = tipEl.offsetWidth;
      tipEl.style.left = Math.max(10, Math.min(innerWidth - w - 10, event.clientX + 15)) + 'px';
      tipEl.style.top = (event.clientY + 15) + 'px';
    });
    hit.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); tipEl.hidden = true; });
    // Reset marks: the tool's icon at the foot of its reset line; details on hover.
    for (const {tool, f} of modules.branding && !narrow ? resetMarks : []) {
      // Round 24px logo, no outline.
      const mark = add('g', {class: 'reset-mark', 'data-tool': tool, transform: `translate(${X(f.resets_at) - 12} ${H - pad.b - 29})`});
      const clipId = 'now-reset-clip-' + tool, clip = add('clipPath', {id: clipId}, mark);
      add('circle', {cx: 12, cy: 12, r: 12}, clip);
      const use = add('use', {href: '#now-' + tool, x: 0, y: 0, width: 24, height: 24, 'clip-path': `url(#${clipId})`}, mark);
      use.setAttribute('aria-hidden', 'true');
      mark.addEventListener('pointerenter', () => {
        fillTip({title: `${toolName(tool)} resets in ${span(f.resets_at - now)}`,
          note: new Date(f.resets_at * 1000).toLocaleString('en-GB', {weekday: 'long', hour: '2-digit', minute: '2-digit'})});
        const r = mark.getBoundingClientRect(), w = tipEl.offsetWidth;
        tipEl.style.left = Math.max(10, Math.min(innerWidth - w - 10, r.left + r.width / 2 - w / 2)) + 'px';
        tipEl.style.top = (r.top - tipEl.offsetHeight - 10) + 'px';
      });
      mark.addEventListener('pointerleave', () => { tipEl.hidden = true; });
    }
    forecastEl.append(svg);
  }
  new ResizeObserver(() => { if (forecastData) { forecastKey = ''; renderForecast(forecastData.forecast, forecastData.now, forecastData.share); } }).observe(forecastEl);
  function retainedCards(agents, now) {
    const byId = new Map(agents.map(agent => [agent.id, agent]));
    const finished = agent => agent.completed || ['done', 'crashed'].includes(agent.status)
      || (!agent.orchestrator && agent.status === 'blocked' && !needsYou(agent));
    const keep = new Set(agents.filter(agent => agent.has_eligible_children === true || !finished(agent)
      || now - (agent.ended ?? agent.last_at ?? 0) < 1800).map(agent => agent.id));
    const pending = [...keep];
    while (pending.length) {
      const parent = byId.get(pending.pop()).parent_id;
      if (byId.has(parent) && !keep.has(parent)) { keep.add(parent); pending.push(parent); }
    }
    return new Map(agents.filter(agent => keep.has(agent.id)).map(agent => [agent.id, agent]));
  }
  // Columns can be put out of sight and brought back from the header menu; remembered in this browser.
  const hiddenColumns = new Set();
  try { const saved = JSON.parse(localStorage.getItem('now-hidden-columns') || '[]'); if (Array.isArray(saved)) saved.forEach(p => typeof p === 'string' && hiddenColumns.add(p)); } catch {}
  const columnsToggle = document.getElementById('columns-toggle'), columnsMenu = document.getElementById('columns-menu');
  const columnsCount = document.getElementById('columns-hidden-count');
  const eyeOff = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m3 3 18 18M10.6 10.6a2 2 0 0 0 2.8 2.8M9.8 5.3C16 3.8 21 12 21 12a18 18 0 0 1-3.3 4M6.3 6.3A19 19 0 0 0 3 12s7 11 14.7 5.7"/></svg>';
  function setColumnHidden(project, hidden) {
    if (hidden) hiddenColumns.add(project); else hiddenColumns.delete(project);
    try { localStorage.setItem('now-hidden-columns', JSON.stringify([...hiddenColumns])); } catch {}
    paintColumns();
    requestAnimationFrame(drawTrees);
  }
  function paintColumns() {
    let hiddenCount = 0, hiddenNeeds = 0;
    for (const [project, group] of groups) {
      const hidden = hiddenColumns.has(project);
      group.el.hidden = hidden;
      if (hidden) { hiddenCount++; hiddenNeeds += group.cards.filter(v => needsYou(v.agent)).length; }
    }
    board.style.setProperty('--cols', String(Math.max(1, groups.size - hiddenCount)));
    columnsCount.hidden = !hiddenCount;
    if (columnsCount.dataset.n !== String(hiddenCount)) {
      columnsCount.dataset.n = String(hiddenCount);
      columnsCount.replaceChildren(String(hiddenCount || ''), ...(hiddenCount ? [node('span', 'now-tool-word', ' hidden')] : []));
    }
    columnsToggle.classList.toggle('need', hiddenNeeds > 0);
    columnsToggle.title = hiddenNeeds ? `Columns: a hidden column needs attention` : 'Columns';
    if (!columnsMenu.hidden) fillColumnsMenu();
  }
  function fillColumnsMenu() {
    const rows = [node('p', 'now-menu-title', 'Columns')];
    for (const [project, group] of groups) {
      const label = node('label', 'now-menu-row'), box = node('input');
      box.type = 'checkbox'; box.checked = !hiddenColumns.has(project);
      box.addEventListener('change', () => setColumnHidden(project, !box.checked));
      const name = node('span', 'now-menu-name', project);
      label.append(box, name);
      if (group.cards.some(v => needsYou(v.agent))) label.append(node('span', 'now-menu-need', 'needs you'));
      rows.push(label);
    }
    if (groups.size === 0) rows.push(node('p', 'now-menu-empty', 'No columns yet'));
    if ([...groups.keys()].some(p => hiddenColumns.has(p))) {
      const all = node('button', 'now-menu-all', 'Show all'); all.type = 'button';
      all.addEventListener('click', () => { for (const p of groups.keys()) hiddenColumns.delete(p); setColumnHidden('', false); });
      rows.push(all);
    }
    const focused = columnsMenu.contains(document.activeElement) ? document.activeElement.closest('label')?.textContent : null;
    columnsMenu.replaceChildren(...rows);
    if (focused) [...columnsMenu.querySelectorAll('label')].find(l => l.textContent === focused)?.querySelector('input').focus();
  }
  function openColumnsMenu(open) {
    columnsMenu.hidden = !open; columnsToggle.setAttribute('aria-expanded', String(open));
    if (open) { fillColumnsMenu(); columnsMenu.querySelector('input')?.focus(); }
  }
  columnsToggle.addEventListener('click', () => openColumnsMenu(columnsMenu.hidden));
  document.addEventListener('pointerdown', event => {
    if (!columnsMenu.hidden && !columnsMenu.contains(event.target) && !columnsToggle.contains(event.target)) openColumnsMenu(false);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !columnsMenu.hidden) { openColumnsMenu(false); columnsToggle.focus(); }
  });
  function createGroup(project) {
    const el = node('section', 'now-project'); el.dataset.project = project;
    const heading = node('h2'), name = node('span', '', project), counts = node('span', 'now-counts');
    const hide = node('button', 'now-column-hide'); hide.type = 'button';
    hide.setAttribute('aria-label', `Hide column ${project}`); hide.title = 'Hide column, bring it back from the Columns menu';
    hide.innerHTML = eyeOff;
    hide.addEventListener('click', () => { setColumnHidden(project, true); columnsToggle.focus(); });
    heading.append(name, counts, hide);
    const items = node('div', 'now-items');
    const lines = document.createElementNS(svgNS, 'svg');
    lines.classList.add('now-tree'); lines.setAttribute('aria-hidden', 'true');
    // Finished workers sink under a small heading, so the owner sees who left the work.
    // One click on the heading hides the list, the next shows it; remembered per project in this browser.
    const finished = node('div', 'now-finished'), finishedItems = node('div', 'now-items');
    const title = node('h3', 'now-finished-title'), finishedCount = node('span', 'now-finished-count');
    title.append(node('span', '', 'Finished'), finishedCount);
    title.tabIndex = 0; title.setAttribute('role', 'button');
    // Finished work is collapsed until the owner opens it (owner 2026-11-11).
    const key = 'now-finished-open:' + project;
    const setOpen = open => {
      finishedItems.hidden = !open; title.setAttribute('aria-expanded', String(open));
      try { if (open) localStorage.setItem(key, '1'); else localStorage.removeItem(key); } catch {}
      requestAnimationFrame(drawTrees);
    };
    let collapsed = true;
    try { collapsed = localStorage.getItem(key) !== '1'; } catch {}
    finishedItems.hidden = collapsed; title.setAttribute('aria-expanded', String(!collapsed));
    title.addEventListener('click', () => setOpen(finishedItems.hidden));
    title.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setOpen(finishedItems.hidden); }
    });
    finished.append(title, finishedItems); finished.hidden = true;
    el.append(heading, lines, items, finished); board.append(el);
    const group = {el, counts, items, lines, finished, finishedItems, finishedCount, cards: []}; groups.set(project, group);
    return group;
  }
  function createCard(agent, group) {
    const el = node('article', 'now-card'); el.dataset.agentId = agent.id;
    const top = node('div', 'now-identity');
    if (modules.branding) {
      const icon = document.createElementNS(svgNS, 'svg'), use = document.createElementNS(svgNS, 'use');
      icon.setAttribute('aria-hidden', 'true'); use.setAttribute('href', '#now-' + agent.kind); icon.append(use); top.append(icon);
    }
    const who = node('span', 'now-who'), model = node('span', 'now-model'), guessed = node('span', 'now-guessed', '?');
    // Shown only when the board setting marks cards missing from the agents registry.
    guessed.hidden = true; guessed.setAttribute('aria-label', 'Not in the agents registry: column and parent are guessed');
    tip(guessed, {title: 'Not in the agents registry: column and parent are guessed'});
    top.append(who, model, guessed);
    const task = node('p', 'now-task');
    el.tabIndex = 0; el.setAttribute('aria-haspopup', 'dialog');
    const status = node('div', 'now-status'), dot = node('span', 'now-dot'), action = node('span', 'now-action');
    status.append(dot, action);
    const time = node('p', 'now-time'), details = node('div', 'now-details'), footer = node('div', 'now-footer');
    const context = node('span', 'now-context');
    context.hidden = true; context.setAttribute('role', 'img');
    const ring = document.createElementNS(svgNS, 'svg');
    ring.setAttribute('viewBox', '0 0 16 16'); ring.setAttribute('aria-hidden', 'true');
    const circle = className => {
      const el = document.createElementNS(svgNS, 'circle'); el.setAttribute('class', className);
      el.setAttribute('cx', '8'); el.setAttribute('cy', '8'); el.setAttribute('r', '6.5');
      return el;
    };
    const fill = circle('now-context-fill');
    fill.setAttribute('pathLength', '100'); fill.setAttribute('transform', 'rotate(-90 8 8)');
    const contextLabel = node('span');
    ring.append(circle('now-context-track'), fill); context.append(ring, contextLabel);
    footer.append(status, time, context); details.append(footer);
    el.append(top, task, details); group.items.append(el);
    const view = {el, who, model, guessed, task, status, action, time, context, contextLabel, fill, group, depth: 0, agent};
    cards.set(agent.id, view); group.cards.push(view);
    return view;
  }
  function paintCard(view, agent, now) {
    view.agent = agent;
    if (agent.link === 'inferred') view.el.title = 'Parent link inferred from working directory and launch time';
    else if (agent.link === 'exact') view.el.title = 'Parent link recorded in session data';
    else if (agent.follows) view.el.title = 'Follows ' + agent.follows;
    else view.el.removeAttribute('title');
    text(view.who, agent.orchestrator ? 'Orchestrator' : agent.agent_type || agent.folder);
    text(view.model, modelLabel(agent)); text(view.task, sessionTitleDisplay(agent.task, agent.title_source));
    view.guessed.hidden = agent.guessed !== true;
    const [word, detail] = paintStatus(view.status, view.action, agent);
    tip(view.status, {title: word || 'Status', note: detail});
    const times = timeLine(agent, now);
    view.time.replaceChildren(...times.map(value => node('span', '', value)));
    tip(view.time, times.length > 1
      ? {title: agent.status === 'working' ? 'Working' : 'Worked', rows: [['Started', times[0]], ['Duration', times[1]]]}
      : {title: agent.orchestrator ? `Last reply ${times[0]}` : times[0]});
    const known = Number.isInteger(agent.context_percent);
    view.context.hidden = !known;
    const high = known && agent.context_high === true;
    view.context.classList.toggle('high', high);
    view.contextLabel.hidden = !high;
    text(view.contextLabel, high ? `context ${agent.context_percent}%` : '');
    if (known) {
      const pct = agent.context_percent;
      view.context.setAttribute('aria-label', `Context window ${pct}% used`);
      view.context.dataset.level = agent.context_level;
      view.fill.setAttribute('stroke-dasharray', `${pct} 100`);
      const tokens = n => n >= 1e6 ? +(n / 1e6).toFixed(1) + 'M' : n >= 1000 ? Math.round(n / 1000) + 'k' : String(Math.round(n));
      tip(view.context, {title: 'Context window', rows: [['Used', Math.round(pct) + '%'],
        ['Tokens', `${tokens(agent.context_used)} of ${tokens(agent.context_window)}`]]});
    }
    view.el.classList.toggle('need', needsYou(agent));
    view.el.classList.toggle('run', agent.status === 'working');
    view.el.classList.toggle('quiet', agent.status === 'working' && Boolean(agent.quiet));
    view.el.classList.toggle('done', agent.status === 'done' || agent.completed === true);
    view.el.dataset.status = agent.status;
    if (panelView === view) fillPanel(view);
  }
  // Card panel: the latest turn as the transcript shows it. Nothing here is generated.
  const panel = node('section', 'now-panel'); panel.hidden = true;
  panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'Agent details'); panel.tabIndex = -1;
  document.body.append(panel);
  let panelView = null, panelExpanded = false;
  function panelPlainText(source) {
    return source.replace(/\r\n?/g, '\n')
      .replace(/^[ \t]*(?:`{3,}|~{3,})[^\n]*$/gm, '')
      .replace(/^[ \t]{0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/gm, '$1')
      .replace(/^[ \t]*>[ \t]?/gm, '')
      .replace(/^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?/gm, '')
      .replace(/!?\[([^\]\n]*)\]\([^)\n]*\)/g, '$1')
      .replace(/(`+)([^\n]*?)\1/g, '$2')
      .replace(/(\*{1,3}|~~)(?=\S)(.+?\S|\S)\1/g, '$2')
      .replace(/(^|[^\w])(_{1,3})(?=\S)(.+?\S|\S)\2(?=$|[^\w])/g, '$1$3');
  }
  function panelSection(label, ...children) {
    const section = node('div', 'now-panel-section');
    section.append(node('div', 'now-panel-label', label), ...children);
    return section;
  }
  function fillPanel(view) {
    const agent = view.agent, brief = agent.brief || {};
    const close = node('button', 'now-panel-close');
    close.type = 'button'; close.setAttribute('aria-label', 'Close');
    close.innerHTML = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
    close.addEventListener('click', () => closePanel(true));
    const head = node('div', 'now-panel-head'), who = node('div', 'now-panel-who');
    who.append(node('span', '', agent.orchestrator ? 'Orchestrator' : agent.agent_type || agent.folder || ''), node('span', 'now-panel-model', modelLabel(agent, true)));
    head.append(who, close);
    const state = node('div', 'now-panel-state now-status'), action = node('span', 'now-action');
    state.append(node('span', 'now-dot'), action);
    paintStatus(state, action, agent);
    const parts = [head, node('h3', 'now-panel-task', sessionTitleDisplay(agent.task || 'Untitled', agent.title_source)), state];
    if (brief.request) {
      const request = node('p', 'now-panel-request', brief.request); request.title = brief.request;
      parts.push(panelSection('Last request', request));
    }
    if (brief.reply) {
      const reply = node('p', 'now-panel-reply', panelPlainText(brief.reply) + (brief.reply_cut ? '…' : ''));
      reply.classList.toggle('open', panelExpanded);
      const label = agent.status === 'working' ? 'Latest message' : 'Final message';
      const section = panelSection(label, reply);
      parts.push(section);
      requestAnimationFrame(() => {
        if (!reply.isConnected || reply.scrollHeight <= reply.clientHeight + 1 && !panelExpanded) return;
        const more = node('button', 'now-panel-more', panelExpanded ? 'Less' : 'More');
        more.type = 'button';
        more.addEventListener('click', () => {
          panelExpanded = !panelExpanded; reply.classList.toggle('open', panelExpanded);
          text(more, panelExpanded ? 'Less' : 'More'); placePanel();
        });
        section.append(more); placePanel();
      });
    }
    if (brief.commits?.length) {
      const list = node('ul', 'now-panel-commits');
      for (const [sha, subject] of brief.commits.slice().reverse()) {
        const item = node('li', ''); item.append(node('span', 'now-panel-sha', sha), node('span', 'now-panel-subject', subject));
        item.title = subject; list.append(item);
      }
      parts.push(panelSection(brief.commits.length === 1 ? 'Commit' : `Commits ${brief.commits.length}`, list));
    }
    if (brief.files) {
      const list = node('ul', 'now-panel-files');
      for (const path of brief.top_files || []) { const item = node('li', '', path); item.title = path; list.append(item); }
      const rest = brief.files - (brief.top_files || []).length;
      if (rest > 0) list.append(node('li', 'now-panel-rest', `and ${rest} more`));
      parts.push(panelSection(brief.files === 1 ? 'File written' : `Files written ${brief.files}`, list));
    }
    if (!brief.request && !brief.reply && !brief.commits?.length && !brief.files) {
      parts.push(node('p', 'now-panel-empty', 'Nothing from this turn in the transcript yet.'));
    }
    panel.replaceChildren(...parts);
    placePanel();
  }
  function placePanel() {
    if (!panelView || panel.hidden) return;
    if (innerWidth <= 600) { panel.style.left = panel.style.top = ''; return; }
    const r = panelView.el.getBoundingClientRect(), w = panel.offsetWidth, h = panel.offsetHeight;
    const right = r.right + 10 + w <= innerWidth - 10;
    const left = right ? r.right + 10 : r.left - 10 - w >= 10 ? r.left - 10 - w : Math.max(10, Math.min(innerWidth - w - 10, r.left));
    const below = !right && r.left - 10 - w < 10;
    const top = below ? r.bottom + 10 : r.top;
    panel.style.left = left + 'px';
    panel.style.top = Math.max(10, Math.min(innerHeight - h - 10, top)) + 'px';
  }
  function openPanel(view) {
    if (panelView === view) { closePanel(true); return; }
    hideTip();
    panelView?.el.classList.remove('open');
    panelView = view; panelExpanded = false; view.el.classList.add('open');
    panel.hidden = false; fillPanel(view); panel.focus({preventScroll: true});
  }
  function closePanel(refocus) {
    if (!panelView) return;
    const view = panelView; panelView = null; panel.hidden = true; view.el.classList.remove('open');
    if (refocus && view.el.isConnected) view.el.focus({preventScroll: true});
    flush();
  }
  board.addEventListener('click', event => {
    const el = event.target.closest('.now-card');
    if (!el || event.target.closest('a[href], button')) return;
    const view = cards.get(el.dataset.agentId);
    if (view) openPanel(view);
  });
  board.addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const el = event.target.classList?.contains('now-card') ? event.target : null;
    const view = el && cards.get(el.dataset.agentId);
    if (view) { event.preventDefault(); openPanel(view); }
  });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && panelView) closePanel(true); });
  document.addEventListener('pointerdown', event => {
    if (panelView && !panel.contains(event.target) && !panelView.el.contains(event.target)) closePanel(false);
  });
  addEventListener('resize', placePanel);
  addEventListener('scroll', placePanel, true);
  function drawTrees() {
    for (const group of groups.values()) {
      const bounds = group.el.getBoundingClientRect(), paths = [];
      const children = new Map();
      for (const view of group.cards) {
        const parent = cards.get(view.el.dataset.parentId);
        if (!parent || parent.group !== group || !view.depth) continue;
        if (!children.has(parent)) children.set(parent, []);
        children.get(parent).push(view);
      }
      for (const [parent, kids] of children) {
        // Segments only touch end to end: overlapping strokes would double the line's brightness at joins.
        const pb = parent.el.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
        const x = Math.round((pb.left - bounds.left + 9) * dpr) / dpr;
        // Inner branches leave the trunk straight; only the last one bends.
        const at = kid => { const kb = kid.el.getBoundingClientRect(); return [kb.top - bounds.top + 20, kb.left - bounds.left]; };
        const [lastY, lastLeft] = at(kids.at(-1));
        let d = `M ${x} ${pb.bottom - bounds.top} V ${lastY - 8} Q ${x} ${lastY} ${x + 8} ${lastY} H ${lastLeft}`;
        for (const kid of kids.slice(0, -1)) {
          const [y, left] = at(kid);
          d += ` M ${x + 0.75} ${y} H ${left}`;
        }
        const path = document.createElementNS(svgNS, 'path'); path.setAttribute('d', d); paths.push(path);
      }
      group.lines.replaceChildren(...paths);
    }
  }
  function reading() { return overBoard || Boolean(document.getSelection()?.toString()); }
  function showNotice() {
    const message = [disconnected ? 'Source unavailable. Reconnecting…' : '',
      paused ? 'Updates paused while reading.' : '', sourceIssue].filter(Boolean).join(' ');
    text(error, message); error.hidden = !message;
    error.dataset.kind = disconnected || sourceIssue ? 'problem' : 'paused';
  }
  function flush() {
    if (!queued || reading()) return;
    clearTimeout(pauseTimer); pauseTimer = null; paused = false;
    const data = queued; queued = null;
    snapshotAt = data.snapshot_at; showFreshness();
    if (calculatorLink) calculatorLink.hidden = !data.calculator || !(Number.isFinite(data.calculator.used_percent) || Number.isFinite(data.calculator.credits_balance));
    dayBounds = Number.isFinite(data.local_day?.start) && Number.isFinite(data.local_day?.end) ? data.local_day : null;
    const names = {claude_sessions: 'Claude session logs', codex_sessions: 'Codex session logs', claude_desktop: 'Claude metadata',
      codex_wrapper: 'Codex wrapper logs', claude_limits: 'Claude limits', codex_limits: 'Codex limits',
      claude_usage: 'Claude usage', codex_meter: 'Codex usage', quota_history: 'Quota history'};
    const reasons = {permission_denied: 'permission denied', file_missing: 'a required file is missing',
      not_a_directory: 'the configured location is not a folder', not_a_file: 'a log location is a folder',
      unrecognized_format: 'format not recognized', read_failed: 'a file read failed', reader_failed: 'the local reader failed'};
    const problems = Object.entries(data.sources || {}).filter(([, value]) => ['unavailable', 'unsupported'].includes(value.status))
      .map(([name, value]) => `${names[name] || 'Local data'} can't be read: ${reasons[value.reason]
        || (value.status === 'unsupported' ? 'format not recognized' : 'a file read failed')}.`);
    sourceIssue = problems.join(' ');
    // A new page version on the server reloads the open page, so nobody has to press reload.
    if (data.build) {
      if (loadedBuild && loadedBuild !== data.build) { location.reload(); return; }
      loadedBuild = data.build;
    }
    // Existing cards keep their order; newly seen cards append after their parent.
    // Finished work leaves after 30 min, with ancestors kept while children are active.
    const everyone = [...(data.agents || []), ...(data.completed || [])];
    empty.hidden = everyone.length > 0 || Boolean(sourceIssue);
    const incoming = retainedCards(everyone, data.now);
    // A same-title successor takes its predecessor's place, above existing workers.
    for (const agent of incoming.values()) {
      if (!agent.orchestrator || cards.has(agent.id)) continue;
      const previous = [...cards.values()].find(view => !incoming.has(view.agent.id) && view.agent.orchestrator
        && view.agent.project === agent.project && view.agent.task === agent.task);
      if (!previous) continue;
      cards.delete(previous.agent.id); cards.set(agent.id, previous);
      previous.agent = agent;
      previous.el.dataset.agentId = agent.id;
    }
    const visiting = new Set();
    function append(agent) {
      if (cards.has(agent.id) || visiting.has(agent.id)) return;
      visiting.add(agent.id);
      const parent = incoming.get(agent.parent_id);
      if (parent && !cards.has(parent.id)) append(parent);
      const group = groups.get(agent.project) || createGroup(agent.project);
      createCard(agent, group); visiting.delete(agent.id);
    }
    const priority = project => { const i = projectOrder.indexOf(project); return i < 0 ? projectOrder.length : i; };
    for (const agent of [...incoming.values()].sort((a, b) => priority(a.project) - priority(b.project))) append(agent);
    // Cards the server no longer reports (archived chat, finished before today) leave the board.
    for (const [id, view] of cards) {
      if (incoming.has(id)) continue;
      if (panelView === view) closePanel(false);
      view.el.remove(); cards.delete(id);
      for (const group of groups.values()) group.cards = group.cards.filter(v => v !== view);
    }
    // A finished worker stays in the tree while something under it still works.
    const ended = agent => !agent.orchestrator && (agent.completed === true || ['done', 'crashed'].includes(agent.status));
    const holding = new Set();
    for (const agent of incoming.values()) {
      if (ended(agent)) continue;
      for (let id = agent.parent_id; incoming.has(id) && !holding.has(id); id = incoming.get(id).parent_id) holding.add(id);
    }
    const placed = new Set();
    function place(agent) {
      if (placed.has(agent.id)) return;
      placed.add(agent.id);
      const parent = incoming.get(agent.parent_id);
      if (parent) place(parent);
      const view = cards.get(agent.id), group = groups.get(agent.project) || createGroup(agent.project);
      if (view.group !== group) {
        view.group.cards = view.group.cards.filter(card => card !== view);
        view.group = group; group.cards.push(view); group.items.append(view.el);
      }
      const finished = ended(agent) && !holding.has(agent.id);
      const list = finished ? group.finishedItems : group.items;
      if (view.el.parentNode !== list) {
        if (finished) list.prepend(view.el); else list.append(view.el);
      }
      // A live child always sits below its parent, never above it.
      const parentView = parent && cards.get(parent.id);
      if (!finished && parentView && parentView.el.parentNode === list
          && (parentView.el.compareDocumentPosition(view.el) & Node.DOCUMENT_POSITION_PRECEDING)) parentView.el.after(view.el);
      view.depth = finished ? 0 : parent && cards.get(parent.id).group === group ? Math.min(cards.get(parent.id).depth + 1, 2) : 0;
      view.el.dataset.parentId = agent.parent_id || '';
      view.el.style.marginInlineStart = view.depth ? `${view.depth * 21}px` : '';
      paintCard(view, agent, data.now);
    }
    for (const agent of incoming.values()) place(agent);
    for (const [project, group] of groups) if (!group.cards.length) { group.el.remove(); groups.delete(project); }
    for (const group of groups.values()) {
      const count = group.finishedItems.children.length;
      group.finished.hidden = !count; text(group.finishedCount, String(count));
    }
    const counts = agents => ({running: agents.filter(a => a.status === 'working').length,
      needs: agents.filter(needsYou).length,
      context: agents.filter(a => a.context_high === true).length,
      done: agents.filter(a => (a.completed || a.status === 'done') && sameDay(a.ended ?? a.last_at, data.now)).length});
    const total = counts(everyone);
    total.needs = everyone.filter(a => needsYou(a) || a.context_high === true).length;
    burnData = modules.burn_rate ? data.burn || {} : {}; renderLimits(data); renderForecast(data.forecast, data.now, workShare(everyone, data.now));
    summary.replaceChildren();
    for (const [value, label] of [[total.running, ' running'], [total.needs, ' needs attention'], [total.done, ' done today']]) {
      const item = node('span', 'now-summary-item');
      item.append(node('b', '', String(value)), label); summary.append(item);
    }
    for (const [project, group] of groups) {
      const c = counts(everyone.filter(a => a.project === project));
      group.el.classList.toggle('quiet', !c.running);
      group.counts.replaceChildren(...[c.needs ? `${c.needs} needs attention` : '', c.running ? `${c.running} running` : '',
        c.context ? `${c.context} context high` : '',
        c.done ? `${c.done} done` : ''].filter(Boolean).map(value => node('span', '', value)));
    }
    paintColumns();
    showNotice(); drawTrees();
  }
  board.addEventListener('pointerenter', () => { overBoard = true; });
  board.addEventListener('pointerleave', () => { overBoard = false; flush(); });
  document.addEventListener('selectionchange', flush);
  new ResizeObserver(drawTrees).observe(board);
  document.fonts.ready.then(drawTrees);
  return {configure(config) { modules = config.modules || {}; projectOrder = config.project_order || [];
    WORK = config.work_hours || [9, 21]; WORK_DAY = WORK[1] - WORK[0]; },
    render(data) {
      disconnected = false; queued = data;
      if (reading() && !pauseTimer && !paused) pauseTimer = setTimeout(() => {
        pauseTimer = null;
        if (queued && reading()) { paused = true; showNotice(); }
      }, 10000);
      flush(); showNotice();
    },
    error() { disconnected = true; showNotice(); }};
};
