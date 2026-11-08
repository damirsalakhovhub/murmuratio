'use strict';
// Turns saved Codex events into a readable activity view: what was asked, what the agent did, what it replied.
// Raw records stay one click away; model requests only feed the per-turn usage line.
window.createActivity = ({node, count}) => {
  const finite = n => typeof n === 'number' && Number.isFinite(n);
  const short = n => {
    if (!finite(n)) return '—';
    const f = new Intl.NumberFormat('en-US', {maximumFractionDigits: 1});
    return n >= 1e6 ? f.format(n / 1e6) + 'M' : n >= 1e3 ? f.format(n / 1e3) + 'K' : count(n);
  };
  const clock = ms => Number.isFinite(ms) ? new Date(ms).toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'}) : '—';
  const minutes = ms => `${Math.floor(Math.max(0, ms || 0) / 60000)}′`;
  const seconds = s => s < 1 ? '<1s' : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}′`;
  const timeOf = e => Date.parse(e?.time || '');
  const parse = v => { try { return JSON.parse(v); } catch { return v; } };
  const pretty = v => typeof v === 'string' ? v : JSON.stringify(v, null, 2);
  const base = p => String(p).trim().split('/').filter(Boolean).at(-1) || String(p).trim();
  const plural = (n, word, many = word + 's') => `${n} ${n === 1 ? word : many}`;

  // ---------- Markdown: escaped text only; links limited to http(s). ----------
  function inline(text, parent) {
    const pattern = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\s][^*\n]*\*)|(\[[^\]\n]+\]\(<?([^)>\n]+)>?\))/g;
    let last = 0, m;
    while ((m = pattern.exec(text))) {
      if (m.index > last) parent.append(text.slice(last, m.index));
      if (m[1]) parent.append(node('code', '', m[1].slice(1, -1)));
      else if (m[2]) parent.append(node('strong', '', m[2].slice(2, -2)));
      else if (m[3]) parent.append(node('em', '', m[3].slice(1, -1)));
      else {
        const label = m[4].slice(1, m[4].indexOf(']')), target = m[5].trim();
        if (/^https?:\/\//.test(target)) { const a = node('a', '', label); a.href = target; a.target = '_blank'; a.rel = 'noopener noreferrer'; parent.append(a); }
        else { const span = node('span', 'md-file', label); span.title = target; parent.append(span); }
      }
      last = pattern.lastIndex;
    }
    if (last < text.length) parent.append(text.slice(last));
    return parent;
  }
  function markdown(source, className = '') {
    const root = node('div', 'md ' + className);
    const lines = String(source || '').replace(/\r\n/g, '\n').split('\n');
    let i = 0, para = [];
    const flush = () => { if (para.length) { root.append(inline(para.join(' '), node('p'))); para = []; } };
    while (i < lines.length) {
      const line = lines[i];
      const fence = /^\s*```/.exec(line);
      if (fence) {
        flush(); const body = []; i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) body.push(lines[i++]);
        i++; root.append(node('pre', 'md-code', body.join('\n'))); continue;
      }
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) { flush(); root.append(inline(heading[2], node('p', 'md-heading'))); i++; continue; }
      const item = /^\s*(?:[-*+]|(\d+)[.)])\s+(.*)$/.exec(line);
      if (item) {
        flush(); const ordered = Boolean(item[1]); const list = node(ordered ? 'ol' : 'ul');
        if (ordered && item[1] !== '1') list.start = Number(item[1]);
        while (i < lines.length) {
          const next = /^\s*(?:[-*+]|(\d+)[.)])\s+(.*)$/.exec(lines[i]);
          if (!next || Boolean(next[1]) !== ordered) break;
          const li = inline(next[2], node('li')); i++;
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*(?:[-*+]|\d+[.)])\s+/.test(lines[i])) { li.append(' '); inline(lines[i++].trim(), li); }
          list.append(li);
        }
        root.append(list); continue;
      }
      if (/^\s*>\s?/.test(line)) { flush(); root.append(inline(line.replace(/^\s*>\s?/, ''), node('blockquote'))); i++; continue; }
      if (!line.trim()) { flush(); i++; continue; }
      para.push(line.trim()); i++;
    }
    flush();
    return root;
  }
  const plain = s => String(s || '').replace(/[*_`#>]+/g, '').replace(/\s+/g, ' ').trim();

  // ---------- Understanding tool calls ----------
  function unquote(literal) {
    const q = literal[0], body = literal.slice(1, -1);
    if (q === '"') { try { return JSON.parse(literal); } catch { return body; } }
    return body.replace(/\\n/g, '\n').replace(/\\(['`\\])/g, '$1');
  }
  function shortCommand(cmd) {
    let c = String(cmd).trim();
    c = c.replace(/^cd\s+\S+\s*&&\s*/, '');
    const first = c.split('\n').find(x => x.trim()) || c;
    return first.trim() + (c.includes('\n') ? ' …' : '');
  }
  function patchFiles(text) {
    const files = [];
    for (const m of String(text).matchAll(/\*\*\* (?:Update|Add|Delete) File: ([^\n\\"]+)/g)) files.push(m[1].trim());
    return [...new Set(files)];
  }
  function toolName(n) { return String(n).replace(/^mcp__/, '').replace(/__/g, ' ').replace(/_/g, ' '); }
  // Returns {verb, target, more, code, kind, commands, files}.
  function describeCall(raw) {
    const call = parse(raw);
    const name = typeof call === 'object' && call ? call.name : '';
    let args = typeof call === 'object' && call ? call.arguments ?? call.input ?? '' : raw;
    const argObject = typeof args === 'string' ? parse(args) : args;
    const code = typeof args === 'string' ? args : pretty(args);
    const out = {verb: 'Used', target: name ? toolName(name) : 'a tool', more: 0, code, kind: 'other', commands: [], files: []};
    const commandsIn = text => [...String(text).matchAll(/\bcmd\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)/g)].map(m => unquote(m[1]));
    if (name === 'exec' || (!name && typeof args === 'string')) {
      const js = String(args);
      out.commands = commandsIn(js);
      out.files = patchFiles(js);
      const inner = [...new Set([...js.matchAll(/tools\.(\w+)\(/g)].map(m => m[1]))];
      if (out.files.length) Object.assign(out, {verb: 'Edited', target: out.files.map(base).join(', '), kind: 'edit', more: out.commands.length});
      else if (out.commands.length) Object.assign(out, {verb: 'Ran', target: shortCommand(out.commands[0]), kind: 'command', more: out.commands.length - 1});
      else if (inner.includes('view_image')) Object.assign(out, {verb: 'Viewed', target: 'an image', kind: 'look'});
      else if (inner.includes('web__run')) Object.assign(out, {verb: 'Searched', target: 'the web', kind: 'web'});
      else if (inner.some(t => t.startsWith('mcp__chrome_devtools'))) Object.assign(out, {verb: 'Browser', target: inner.filter(t => t.startsWith('mcp__chrome')).map(t => t.replace('mcp__chrome_devtools__', '').replace(/_/g, ' ')).join(', '), kind: 'browser'});
      else if (inner.some(t => t.startsWith('mcp__codex_app'))) Object.assign(out, {verb: 'Codex app', target: inner.filter(t => t.startsWith('mcp__codex_app')).map(t => t.replace('mcp__codex_app__', '').replace(/_/g, ' ')).join(', '), kind: 'other'});
      else if (inner.includes('write_stdin')) Object.assign(out, {verb: 'Checked', target: 'a running command', kind: 'wait'});
      else Object.assign(out, {verb: 'Ran', target: (js.split('\n').find(l => l.trim()) || 'a script').trim(), kind: 'script'});
      return out;
    }
    const a = typeof argObject === 'object' && argObject ? argObject : {};
    if (name === 'exec_command' || name === 'shell' || name === 'local_shell') {
      const cmd = Array.isArray(a.cmd ?? a.command) ? (a.cmd ?? a.command).join(' ') : a.cmd ?? a.command ?? '';
      out.commands = [String(cmd)];
      return Object.assign(out, {verb: 'Ran', target: shortCommand(cmd), kind: 'command'});
    }
    if (name === 'apply_patch') { out.files = patchFiles(code); return Object.assign(out, {verb: 'Edited', target: out.files.map(base).join(', ') || 'files', kind: 'edit'}); }
    if (name === 'js') return Object.assign(out, {verb: 'Browser', target: a.title || 'script', kind: 'browser'});
    if (name === 'sleep') return Object.assign(out, {verb: 'Waited', target: finite(a.duration_ms) ? seconds(a.duration_ms / 1000) : '', kind: 'wait'});
    if (name === 'wait' || name === 'write_stdin') return Object.assign(out, {verb: 'Checked', target: 'a running command', kind: 'wait'});
    if (name === 'spawn_agent') return Object.assign(out, {verb: 'Started agent', target: a.task_name || a.agent_type || '', kind: 'agent'});
    if (name === 'send_message') return Object.assign(out, {verb: 'Messaged agent', target: a.target || '', kind: 'agent'});
    if (name === 'view_image') return Object.assign(out, {verb: 'Viewed', target: a.path ? base(a.path) : 'an image', kind: 'look'});
    if (name === 'update_plan') return Object.assign(out, {verb: 'Updated', target: 'the plan', kind: 'other'});
    return out;
  }
  // Codex wraps every script result; pull out wall time, exit codes and the plain outputs.
  function describeResult(raw, status) {
    const text = String(raw || '');
    const res = {seconds: null, failed: status === 'Error', output: text};
    const wall = /Wall time ([\d.]+) seconds/.exec(text);
    if (wall) res.seconds = Number(wall[1]);
    if (/^Script (failed|error)/i.test(text)) res.failed = true;
    const split = text.split(/\nOutput:\n\n?/);
    let body = split.length > 1 ? split.slice(1).join('\nOutput:\n') : text;
    body = body.replace(/^Warning: truncated output \(original token count: \d+\)\n(Total output lines: \d+\n)?\n?/m, '');
    const parts = [];
    let structured = false;
    for (const line of body.split('\n')) {
      if (!line.startsWith('{"i":') && !line.startsWith('{"result"')) { if (structured && line.trim()) parts.push(line); continue; }
      const item = parse(line);
      if (typeof item !== 'object' || !item?.result) { parts.push(line); continue; }
      structured = true;
      const r = item.result;
      if (r.status === 'rejected') { res.failed = true; parts.push(pretty(r.reason ?? r)); continue; }
      const v = r.value ?? r;
      if (finite(v?.exit_code) && v.exit_code !== 0) res.failed = true;
      parts.push((finite(v?.exit_code) && v.exit_code !== 0 ? `Exit code ${v.exit_code}\n` : '') + (typeof v?.output === 'string' ? v.output : pretty(v)));
    }
    if (structured) res.output = parts.join('\n\n').trim();
    else {
      const parsed = parse(body);
      const value = parsed?.value ?? parsed;
      res.output = Array.isArray(value?.content) ? value.content.map(p => p.type === 'text' ? p.text : pretty(p)).join('\n\n') : pretty(value).trim();
    }
    return res;
  }

  // ---------- Building a turn ----------
  const setupPrefix = /^\s*(# AGENTS\.md instructions|<environment_context>|<user_instructions>|<INSTRUCTIONS>|<permissions instructions>)/;
  function analyse(events, textOf) {
    const turn = {prompt: null, setup: [], items: [], requests: 0, input: 0, output: 0, start: NaN, end: NaN, failed: false, aborted: false};
    let work = null;
    const openCalls = [];
    const closeWork = () => { work = null; };
    const workGroup = e => { if (!work) { work = {type: 'work', key: e.id, steps: [], start: timeOf(e), end: timeOf(e)}; turn.items.push(work); } return work; };
    for (const e of events) {
      const t = timeOf(e);
      if (Number.isFinite(t)) { if (!(t >= turn.start)) turn.start = t; if (!(t <= turn.end)) turn.end = t; }
      const raw = textOf(e);
      if (e.kind === 'model_record') {
        turn.requests++;
        if (finite(e.usage?.input_tokens)) turn.input += e.usage.input_tokens;
        if (finite(e.usage?.output_tokens)) turn.output += e.usage.output_tokens;
        continue;
      }
      if (e.kind === 'context' || (e.kind === 'user' && setupPrefix.test(raw))) { turn.setup.push(e); continue; }
      if (e.kind === 'user') {
        closeWork();
        if (!turn.prompt && !turn.items.length) turn.prompt = e;
        else turn.items.push({type: 'prompt', event: e});
        continue;
      }
      if (e.kind === 'assistant') { closeWork(); turn.items.push({type: 'reply', event: e}); continue; }
      if (e.kind === 'tool_call') {
        const step = {type: 'tool', key: e.id, call: e, result: null, info: describeCall(raw), outcome: null};
        const g = workGroup(e); g.steps.push(step); if (Number.isFinite(t)) g.end = t;
        openCalls.push(step); continue;
      }
      if (e.kind === 'tool_result') {
        let step = openCalls.shift();
        if (!step) {
          step = {type: 'tool', key: e.id, call: null, result: null, info: {verb: 'Tool output', target: '', more: 0, code: '', kind: 'other', commands: [], files: []}, outcome: null};
          workGroup(e).steps.push(step);
        }
        step.result = e; step.outcome = describeResult(raw, e.status);
        if (step.outcome.failed) turn.failed = true;
        if (work && Number.isFinite(t)) work.end = t;
        continue;
      }
      if (e.kind === 'notification' && /reasoning/i.test(e.title || '')) {
        const g = workGroup(e); g.steps.push({type: 'thought', key: e.id, event: e}); if (Number.isFinite(t)) g.end = t; continue;
      }
      if (e.kind === 'error') turn.failed = true;
      if (e.kind === 'notification' && /abort/i.test(e.title || '')) turn.aborted = true;
      closeWork();
      turn.items.push({type: 'marker', event: e});
    }
    return turn;
  }
  function workSummary(group) {
    const tools = group.steps.filter(s => s.type === 'tool');
    const commands = tools.reduce((n, s) => n + (s.info.kind === 'command' || s.info.kind === 'edit' ? s.info.commands.length : 0), 0);
    const scripts = tools.filter(s => s.info.kind === 'script').length;
    const files = [...new Set(tools.flatMap(s => s.info.files))];
    const kinds = k => tools.filter(s => s.info.kind === k).length;
    const parts = [];
    if (commands || scripts) parts.push('ran ' + [commands && plural(commands, 'command'), scripts && plural(scripts, 'script')].filter(Boolean).join(' and '));
    if (files.length) parts.push('edited ' + (files.length === 1 ? base(files[0]) : plural(files.length, 'file')));
    if (kinds('browser')) parts.push('used the browser');
    if (kinds('web')) parts.push('searched the web');
    if (kinds('look')) parts.push('viewed ' + plural(kinds('look'), 'image'));
    if (kinds('agent')) parts.push('messaged agents');
    if (kinds('wait')) parts.push('waited');
    if (kinds('other')) parts.push(plural(kinds('other'), 'other step'));
    if (!parts.length) parts.push(tools.length ? plural(tools.length, 'step') : 'thought');
    const text = parts.join(', ');
    return {text: text[0].toUpperCase() + text.slice(1), failed: tools.filter(s => s.outcome?.failed).length, running: tools.some(s => !s.result)};
  }

  // ---------- Rendering ----------
  // `ui` carries per-feed state: open folds, loaded full texts, and the full-text loader.
  function fold(ui, key, className, open) {
    const d = node('details', className); d.dataset.key = key;
    d.open = ui.open.has(key) ? ui.open.get(key) : open;
    d.addEventListener('toggle', () => ui.open.set(key, d.open));
    return d;
  }
  function fullButton(ui, event, onText) {
    if (!(event?.blocks || []).some(b => b.truncated) || ui.full.has(event.id)) return null;
    const button = node('button', 'control button step-more', 'Show full text'); button.type = 'button';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { const text = await ui.load(event.id); ui.full.set(event.id, text); onText(text); button.remove(); }
      catch { button.textContent = 'Could not load. Retry'; button.disabled = false; }
    });
    return button;
  }
  function renderStep(step, ui, textOf, live) {
    if (step.type === 'thought') {
      const li = node('li', 'step step--thought packet');
      const p = node('p', 'step-thought', plain(textOf(step.event)));
      p.addEventListener('click', () => p.classList.toggle('expanded'));
      li.append(p);
      return li;
    }
    const li = node('li', 'step packet');
    li.dataset.kind = step.info.kind;
    const failed = step.outcome?.failed;
    if (failed) li.classList.add('failed');
    const d = fold(ui, 'step:' + step.key, 'step-fold', false);
    const summary = node('summary', 'step-head');
    const target = node('span', 'step-target', step.info.target);
    summary.append(node('span', 'step-verb', step.info.verb), target);
    if (step.info.more > 0) summary.append(node('span', 'step-extra', `+${step.info.more}`));
    const meta = node('span', 'step-meta');
    if (failed) meta.append(node('span', 'step-failed', 'Failed'));
    else if (!step.result && step.call) meta.append(node('span', live ? 'step-running' : '', live ? 'Running' : 'No result'));
    if (finite(step.outcome?.seconds)) meta.append(node('span', '', seconds(step.outcome.seconds)));
    summary.append(meta);
    d.append(summary);
    let body = null;
    const build = () => {
      if (body) return;
      body = node('div', 'step-body');
      if (step.info.commands.length > 1 || (step.info.commands.length && step.info.kind === 'edit')) {
        const list = node('ul', 'step-commands');
        for (const c of step.info.commands) list.append(node('li', '', shortCommand(c)));
        body.append(node('p', 'step-label', 'Commands'), list);
      }
      if (step.call) {
        const pre = node('pre', 'step-code', ui.full.get(step.call.id) ?? step.info.code);
        body.append(node('p', 'step-label', step.call && /^exec$|^$/.test(parse(textOf(step.call))?.name ?? '') ? 'Script' : 'Call'), pre);
        const more = fullButton(ui, step.call, text => { pre.textContent = describeCall(text).code; });
        if (more) body.append(more);
      }
      if (step.result) {
        const pre = node('pre', 'step-code step-output', step.outcome.output || 'No output');
        body.append(node('p', 'step-label', failed ? 'Output, failed' : 'Output'), pre);
        const more = fullButton(ui, step.result, text => { pre.textContent = describeResult(text, step.result.status).output || 'No output'; });
        if (more) body.append(more);
      }
      d.append(body);
    };
    if (d.open) build();
    d.addEventListener('toggle', () => { if (d.open) build(); });
    li.append(d);
    return li;
  }
  function renderTurn(key, events, ui, options) {
    const textOf = e => ui.full.get(e.id) ?? (e.blocks || []).map(b => b.text || '').join('\n\n');
    const t = analyse(events, textOf);
    const root = node('section', 'turn');
    root.dataset.turn = key;
    if (t.failed) root.classList.add('failed');
    const live = options.live;
    const promptText = t.prompt ? textOf(t.prompt) : '';
    const promptLines = promptText.trim().split('\n');
    const turnFold = fold(ui, 'turn:' + key, 'turn-fold', !options.collapsed);
    const head = node('summary', 'turn-head');
    const title = node('span', 'turn-title', t.prompt ? plain(promptLines[0]) || 'Message' : options.first ? 'Earlier in this turn' : 'Agent work');
    const meta = node('span', 'turn-meta');
    if (Number.isFinite(t.start)) meta.append(node('span', '', clock(t.start)));
    if (Number.isFinite(t.start) && Number.isFinite(t.end)) meta.append(node('span', '', minutes(t.end - t.start)));
    if (t.failed) meta.append(node('span', 'step-failed', 'Has errors'));
    if (t.aborted) meta.append(node('span', '', 'Stopped'));
    head.append(title, meta);
    turnFold.append(head);
    const body = node('div', 'turn-body');
    if (t.prompt) {
      const rest = promptLines.slice(1).join('\n').trim();
      if (rest) {
        const prompt = markdown(rest, 'turn-prompt');
        body.append(prompt);
        const more = fullButton(ui, t.prompt, () => options.rerender());
        if (more) body.append(more);
      }
    }
    if (t.setup.length) {
      const setup = fold(ui, 'setup:' + key, 'setup-fold packet', false);
      setup.append(node('summary', 'quiet-head', `Setup: ${t.setup.map(e => /tool/i.test(e.title || '') ? 'tool definitions' : 'instructions').filter((x, i, a) => a.indexOf(x) === i).join(' and ')}`));
      let built = false;
      setup.addEventListener('toggle', () => {
        if (!setup.open || built) return; built = true;
        for (const e of t.setup) { const d = node('details', 'setup-item'); d.append(node('summary', '', e.title || 'Context'), node('pre', 'step-code', textOf(e))); setup.append(d); }
      });
      body.append(setup);
    }
    const lastWork = t.items.filter(i => i.type === 'work').at(-1);
    for (const item of t.items) {
      if (item.type === 'reply') {
        const reply = markdown(textOf(item.event), 'reply packet');
        body.append(reply);
        const more = fullButton(ui, item.event, () => options.rerender());
        if (more) body.append(more);
      } else if (item.type === 'prompt') {
        const p = node('div', 'packet user-packet inline-prompt');
        p.append(node('p', 'inline-prompt-label', 'You'), markdown(textOf(item.event)));
        body.append(p);
      } else if (item.type === 'marker') {
        const e = item.event;
        const names = {compaction: 'Context compacted', context_reset: 'Context reset', model_change: 'Model changed', error: 'Error'};
        const label = names[e.kind] || e.title || 'Event';
        const raw = plain(textOf(e));
        const m = node('p', 'marker packet' + (e.kind === 'error' ? ' error' : ''), label + (raw && e.kind !== 'compaction' && raw.length < 140 && raw.toLowerCase() !== label.toLowerCase() ? `: ${raw}` : ''));
        body.append(m);
      } else {
        const sum = workSummary(item);
        const isLive = live && item === lastWork && sum.running;
        const tools = item.steps.filter(s => s.type === 'tool');
        if (!tools.length) {
          for (const s of item.steps) body.append(node('p', 'thought packet', plain(textOf(s.event))));
          continue;
        }
        const g = fold(ui, 'work:' + item.key, 'work-fold', isLive);
        const gh = node('summary', 'work-head');
        gh.append(node('span', 'work-text', sum.text));
        const gm = node('span', 'work-meta');
        if (sum.failed) gm.append(node('span', 'step-failed', `${sum.failed} failed`));
        if (isLive) gm.append(node('span', 'step-running', 'Running'));
        if (Number.isFinite(item.start) && Number.isFinite(item.end) && item.end - item.start >= 1000) gm.append(node('span', '', seconds((item.end - item.start) / 1000)));
        gh.append(gm);
        g.append(gh);
        const list = node('ol', 'steps');
        const fill = () => { if (!list.childElementCount) for (const s of item.steps) list.append(renderStep(s, ui, textOf, isLive)); };
        if (g.open) fill();
        g.addEventListener('toggle', () => { if (g.open) fill(); });
        g.append(list);
        body.append(g);
      }
    }
    if (t.requests) {
      const usage = node('p', 'turn-usage');
      usage.append(node('span', '', plural(t.requests, 'model request')), node('span', '', `${short(t.input)} input`), node('span', '', `${short(t.output)} output`));
      usage.title = 'Input counts all context sent with each request, cache included. Output includes reasoning.';
      body.append(usage);
    }
    turnFold.append(body);
    root.append(turnFold);
    root._turn = t;
    return root;
  }
  // What the chat is doing now, in Board words.
  function status(events, textOf) {
    const last = events.at(-1);
    const lastTime = timeOf(last);
    const age = Date.now() - lastTime;
    const busyKinds = new Set(['tool_call', 'tool_result', 'model_record', 'notification']);
    const aborted = last?.kind === 'notification' && /abort/i.test(last.title || '');
    const running = !aborted && Number.isFinite(age) && (age < 60000 || (age < 600000 && busyKinds.has(last.kind)));
    let detail = '';
    if (running) {
      for (let i = events.length - 1; i >= 0; i--) {
        const e = events[i];
        if (e.kind === 'tool_call') { const info = describeCall(textOf(e)); detail = `${info.verb} ${info.target}`; break; }
        if (e.kind === 'notification' && /reasoning/i.test(e.title || '')) { detail = plain(textOf(e)); break; }
        if (e.kind === 'assistant') { detail = 'Writing a reply'; break; }
      }
    }
    let start = NaN;
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].group_id !== last?.group_id) break;
      const t = timeOf(events[i]); if (Number.isFinite(t)) start = t;
    }
    return {state: aborted ? 'stopped' : running ? 'running' : 'waiting', word: aborted ? 'Stopped' : running ? 'Running' : 'Turn finished', detail, start, lastTime};
  }
  return {renderTurn, status, markdown, describeCall, describeResult, clock, minutes, short};
};
