(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const number = value => Number.isFinite(value) ? value.toLocaleString('en-US', {maximumFractionDigits: 1}) : '—';
  const moment = at => Number.isFinite(at) ? new Date(at * 1000).toLocaleString('en-GB', {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }) : '—';
  const weekday = at => {
    const d = new Date(at * 1000);
    return `${d.toLocaleDateString('en-US', {weekday: 'short'})} ${d.getDate()}`;
  };
  const monthDay = at => new Date(at * 1000).toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
  const month = at => new Date(at * 1000).toLocaleDateString('en-US', {month: 'long'});
  const compact = value => !Number.isFinite(value) ? '—' : value >= 1000 ? `${Math.round(value / 1000)}k` : number(value);
  const svg = (name, attrs) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  };
  const el = (name, className, text) => {
    const node = document.createElement(name);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  // View switch: Timeline or Rings, remembered per browser.
  const VIEW_KEY = 'murmuratio.calculator.view';
  function showView(view) {
    for (const button of document.querySelectorAll('.calculator-switch [data-view]')) {
      button.setAttribute('aria-checked', String(button.dataset.view === view));
      button.tabIndex = button.dataset.view === view ? 0 : -1;
    }
    for (const panel of document.querySelectorAll('[data-view-panel]')) panel.hidden = panel.dataset.viewPanel !== view;
    if (view === 'timeline') layoutLabels();
  }
  let initial = 'timeline';
  try { if (localStorage.getItem(VIEW_KEY) === 'rings') initial = 'rings'; } catch {}
  const viewButtons = [...document.querySelectorAll('.calculator-switch [data-view]')];
  for (const button of viewButtons) {
    button.addEventListener('click', () => {
      try { localStorage.setItem(VIEW_KEY, button.dataset.view); } catch {}
      showView(button.dataset.view);
    });
    button.addEventListener('keydown', event => {
      const step = {ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1}[event.key];
      if (!step && event.key !== 'Home' && event.key !== 'End') return;
      event.preventDefault();
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? viewButtons.length - 1
        : (viewButtons.indexOf(button) + step + viewButtons.length) % viewButtons.length;
      viewButtons[index].click();
      viewButtons[index].focus();
    });
  }
  showView(initial);

  // Labels sit at their time; overlapping ones move to the next lane, up to the lane's limit,
  // and beyond it the less important one is left out (its dot or boundary stays).
  function layoutLabels() {
    const track = $('calc-track');
    const width = track.getBoundingClientRect().width;
    if (!width) return;
    for (const [lane, limit] of [[$('calc-above'), 2], [$('calc-below'), 1]]) {
      const rows = [];
      const labels = [...lane.children].sort((a, b) => (b.dataset.priority || 0) - (a.dataset.priority || 0) || a.dataset.x - b.dataset.x);
      for (const label of labels) {
        const w = label.getBoundingClientRect().width, x = Number(label.dataset.x) * width;
        const left = Math.max(0, Math.min(width - w, x - w / 2)), right = left + w;
        let row = rows.findIndex(spans => spans.every(([a, b]) => right + 10 <= a || b + 10 <= left));
        if (row < 0 && rows.length < limit) { row = rows.length; rows.push([]); }
        label.style.visibility = row < 0 ? 'hidden' : '';
        if (row < 0) continue;
        rows[row].push([left, right]);
        label.style.left = `${left / width * 100}%`;
        label.dataset.row = row;
      }
      // The first lane is always the one next to the track.
      for (const label of labels) {
        const row = Number(label.dataset.row || 0);
        label.style.top = `${(lane.id === 'calc-above' ? rows.length - 1 - row : row) * 20}px`;
      }
      lane.style.height = `${Math.max(1, rows.length) * 20}px`;
    }
  }
  new ResizeObserver(layoutLabels).observe($('calc-track'));

  function renderTimeline(c, now) {
    const t = c.timeline, above = $('calc-above'), below = $('calc-below'), track = $('calc-track');
    above.replaceChildren(); below.replaceChildren(); track.replaceChildren();
    $('calc-strip').hidden = !t;
    if (t) {
      const span = t.end - t.start, x = at => Math.max(0, Math.min(1, (at - t.start) / span));
      const label = (lane, text, at, className, priority = 0) => {
        const node = el('span', `calc-label${className ? ' ' + className : ''}`, text);
        node.dataset.x = x(at); node.dataset.priority = priority; lane.append(node);
      };
      const sameMonth = at => new Date(at * 1000).getMonth() === new Date(t.start * 1000).getMonth();
      const date = at => sameMonth(at) ? weekday(at) : monthDay(at);
      label(below, date(t.start), t.start, '', 1);
      const left = (c.extra_resets || []).length;
      $('calc-resets-head').hidden = !t.expires_at;
      if (t.expires_at) {
        $('calc-resets-label').textContent = `${left} reset${left === 1 ? '' : 's'} for ${month(t.expires_at - 1)}`;
        $('calc-resets-end').textContent = monthDay(t.expires_at);
      }
      for (const w of t.windows) {
        const block = el('div', 'calc-block');
        block.style.left = `${x(w.start) * 100}%`;
        block.style.width = `${(x(w.end) - x(w.start)) * 100}%`;
        if (w.current && Number.isFinite(c.used_percent)) {
          block.classList.add('current');
          const fill = el('i', 'calc-fill');
          fill.style.width = `${Math.min(100, c.used_percent)}%`;
          const value = el('span', 'calc-block-value', `${number(c.used_percent)}%`);
          // Past 80% the value sits inside the fill, at its end.
          if (c.used_percent > 80) {
            value.classList.add('on-fill');
            value.style.right = `calc(${Math.max(0, 100 - c.used_percent)}% + 10px)`;
          }
          block.append(fill);
          if (x(w.end) - x(w.start) > 0.12) block.append(value);
        }
        track.append(block);
        // The deadline's date already ends the head row above the strip.
        if (w.end !== t.expires_at) label(below, date(w.end), w.end, '', w.ends === 'natural' ? 1 : 0);
      }
      let pressedNow = false;
      for (const reset of t.resets) {
        const dot = el('i', `calc-dot ${reset.status}`);
        dot.style.left = `${x(reset.at) * 100}%`;
        track.append(dot);
        // A press planned within the hour is the «Today» marker itself.
        const pressNow = reset.status === 'planned' && Math.abs(reset.at - now) < 3600;
        label(above, reset.status === 'used' ? 'Reset used' : reset.status === 'unplanned' ? 'Unused reset'
          : pressNow ? 'Reset now' : 'Reset', reset.at, pressNow ? 'strong' : '', pressNow ? 2 : 0);
        pressedNow ||= pressNow;
      }
      if (now >= t.start && now <= t.end) {
        const today = el('i', 'calc-today');
        today.style.left = `${x(now) * 100}%`;
        track.append(today);
        if (!pressedNow) label(above, 'Today', now, 'strong', 2);
      }
    }
    // A balance that expires within two months is labelled by the month it is kept for.
    const near = Number.isFinite(c.credits_expire_at) && c.credits_expire_at - now <= 62 * 86400;
    $('calc-credits-label').textContent = `${compact(c.credits_balance)} ${near ? `for ${month(c.credits_expire_at - 1)}` : 'left'}`;
    $('calc-credits-end').textContent = Number.isFinite(c.credits_expire_at) ? monthDay(c.credits_expire_at) : '—';
    const share = c.credits_start_balance ? c.credits_balance / c.credits_start_balance : 0;
    $('calc-credits-fill').style.width = `${Math.max(0, Math.min(1, share || 0)) * 100}%`;
    if (!$('calc-timeline').hidden) layoutLabels();
  }

  function ring(target, fraction, value, title, until) {
    const r = 42, length = 2 * Math.PI * r;
    const picture = svg('svg', {viewBox: '0 0 100 100', 'aria-hidden': 'true'});
    picture.append(svg('circle', {cx: 50, cy: 50, r, class: 'track'}));
    const share = Math.max(0, Math.min(1, fraction || 0));
    if (share > 0) picture.append(svg('circle', {cx: 50, cy: 50, r, class: 'arc',
      'stroke-dasharray': `${share * length} ${length}`, transform: 'rotate(-90 50 50)'}));
    const dial = el('div', 'calc-ring-dial');
    dial.append(picture, el('span', 'calc-ring-value', value));
    target.replaceChildren(dial, el('span', 'calc-ring-title', title), el('span', 'calc-ring-until', until));
  }

  function renderRings(c) {
    const t = c.timeline;
    ring($('ring-week'), (c.used_percent || 0) / 100,
      Number.isFinite(c.used_percent) ? `${number(c.used_percent)}%` : '—', 'Week used',
      Number.isFinite(c.next_reset) ? `until ${weekday(c.next_reset)}` : '');
    ring($('ring-credits'), c.credits_start_balance ? c.credits_balance / c.credits_start_balance : 0,
      compact(c.credits_balance), 'Credits left',
      Number.isFinite(c.credits_expire_at) ? `until ${monthDay(c.credits_expire_at)}` : '');
    const left = (c.extra_resets || []).length, total = c.resets_total || 0;
    ring($('ring-resets'), total ? left / total : 0, `${left}/${total}`, 'Resets left',
      t?.expires_at ? `until ${monthDay(t.expires_at)}` : '');
  }

  async function load() {
    try {
      const response = await fetch('/api/now', {cache: 'no-store'});
      if (!response.ok) throw new Error();
      const data = await response.json(), c = data.calculator;
      if (!c) throw new Error();
      $('week-used').textContent = Number.isFinite(c.used_percent) ? `${number(c.used_percent)}%` : '—';
      $('week-left').textContent = Number.isFinite(c.remaining_percent) ? `${number(c.remaining_percent)}%` : '—';
      const days = Math.max(0, Math.ceil((c.next_reset - data.now) / 86400));
      $('week-reset').textContent = moment(c.next_reset) + (Number.isFinite(c.next_reset) ? `, in ${days} day${days === 1 ? '' : 's'}` : '');
      $('credits-left').textContent = number(c.credits_balance);
      $('credits-expiry').textContent = moment(c.credits_expire_at);
      const resets = c.extra_resets || [];
      $('resets-left').textContent = number(resets.length);
      for (const row of $('manual-resets').querySelectorAll('[data-expiry]')) row.remove();
      for (const at of resets) {
        const row = document.createElement('div'); row.dataset.expiry = '';
        const label = document.createElement('dt'); label.textContent = 'Expires at';
        const expiry = document.createElement('dd'); expiry.textContent = moment(at);
        row.append(label, expiry); $('manual-resets').append(row);
      }
      $('mode-title').textContent = c.mode?.title || '—';
      $('mode-reason').textContent = c.mode?.reason || '';
      document.querySelector('.calculator-view').dataset.mode = Number.isFinite(c.used_percent) ? c.mode?.id || '' : '';
      renderTimeline(c, data.now);
      renderRings(c);
      $('calculator-error').hidden = true;
    } catch {
      $('calculator-error').textContent = 'Live data unavailable.';
      $('calculator-error').hidden = false;
    }
  }
  void load();
  setInterval(load, 10000);
})();
