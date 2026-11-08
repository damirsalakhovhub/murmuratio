'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const moreChats = $('more-chats');
  const node = (tag, className = '', value) => {
    const el = document.createElement(tag);
    el.className = className;
    if (value !== undefined) el.textContent = value;
    return el;
  };
  const text = (el, value) => { if (el.textContent !== value) el.textContent = value; };
  const read = (key, fallback = '') => { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } };
  const save = (key, value) => { try { localStorage.setItem(key, value); } catch { /* Private browsing. */ } };
  // Preferences saved under the earlier key prefix move over once.
  try {
    for (const key of Object.keys(localStorage)) {
      if (!key.startsWith('inspector.')) continue;
      const renamed = 'murmuratio.' + key.slice('inspector.'.length);
      if (localStorage.getItem(renamed) === null) localStorage.setItem(renamed, localStorage.getItem(key));
      localStorage.removeItem(key);
    }
  } catch { /* Private browsing. */ }
  const linkedCodex = new URLSearchParams(location.search).get('codex');
  let modules = {}, configKey = '';
  let source = linkedCodex ? 'codex' : read('murmuratio.source', 'now');
  if (!['now', 'codex'].includes(source)) source = 'now';
  let selected = linkedCodex || read('murmuratio.selected.' + source);
  if (linkedCodex) save('murmuratio.selected.codex', linkedCodex);
  let hiddenIds; try { hiddenIds = JSON.parse(read('murmuratio.hidden.codex', '[]')); } catch { hiddenIds = []; }
  const hiddenChats = new Set(Array.isArray(hiddenIds) ? hiddenIds.filter(id => typeof id === 'string') : []);
  let chatLimit = 60;
  let nextBefore = null;
  let sessionRevision = null;
  const codexEvents = new Map();
  let allCollapsed = read('murmuratio.collapsed') === 'true';
  let generation = 0;
  let busy = false;
  let loaded = false;
  let lastSessions = [];
  let lastSidebar = {};
  let lastAnalysis = null;
  let resetDialogId = '';
  let resetDialogStarted = false;
  let resetStarting = false;
  let resetActionError = '';
  let resetStatusSequence = 0;
  const chats = new Map();
  const sidebarGroups = new Map();
  let groupPreferences;
  try { groupPreferences=JSON.parse(read('murmuratio.sidebarGroups', '{}')); } catch { groupPreferences={}; }
  if (!groupPreferences || Array.isArray(groupPreferences) || typeof groupPreferences !== 'object') groupPreferences={};
  let sidebarSearching = false;
  const turns = new Map();
  const unread = new Set();
  const numbers = new Intl.NumberFormat('en-US');
  const isCount = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const count = value => isCount(value) ? numbers.format(value) : 'no data';
  const panels = window.createPanels({node, count});

  async function api(path, options = {}) {
    const response = await fetch(path, {cache: 'no-store', credentials: 'same-origin', ...options});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Source unavailable');
    return data;
  }
  function order(parent, nodes) {
    let cursor = parent.firstElementChild;
    for (const el of nodes) {
      if (el === cursor) cursor = cursor.nextElementSibling;
      else parent.insertBefore(el, cursor);
    }
    const keep = new Set(nodes);
    for (const el of [...parent.children]) if (!keep.has(el)) el.remove();
  }
  const nowView = window.createNowView({node, text});
  function sidebarGroup(key, label, collapsed, searching) {
    let group=sidebarGroups.get(key);
    if(!group){
      const details=node('details','sidebar-group');
      const summary=node('summary','sidebar-group-title');
      const body=node('div','sidebar-group-list');details.append(summary,body);
      group={details,summary,body,open: typeof groupPreferences[key]==='boolean' ? groupPreferences[key] : !collapsed,searchOpen:true};
      summary.addEventListener('click',event=>{
        event.preventDefault();
        if(sidebarSearching)group.searchOpen=!group.searchOpen;
        else {group.open=!group.open;groupPreferences[key]=group.open;save('murmuratio.sidebarGroups',JSON.stringify(groupPreferences));}
        renderChats(lastSessions);
      });
      sidebarGroups.set(key,group);
    }
    text(group.summary,label);group.summary.title=label;
    group.details.open=searching ? group.searchOpen : group.open;
    return group;
  }
  // Orchestrator-started chats carry a rules preamble as their name; the Board's task title reads better.
  const preamble = /^(#\s.*\bcard\b|You are an? .*(worker|orchestrator)|Read-only audit for the Claude orchestrator)/i;
  const chatName = s => {
    const first = String(s?.name || '').split('\n').find(line => line.trim())?.trim() || '';
    return (s?.task && (!first || preamble.test(first))) ? s.task : first || s?.task || 'Untitled';
  };
  const sessionName = data => chatName(lastSessions.find(s => s.id === data.session.id) || data.session);
  function renderChats(sessions) {
    lastSessions=sessions;
    const hidden=hiddenChats,showingHidden=$('show-hidden').checked;
    text($('hidden-chats-label'),`Hidden ${sessions.filter(s=>hidden.has(s.id)).length}`);
    moreChats.hidden=true;
    const query=$('chat-search').value.trim().toLocaleLowerCase();
    const searching=Boolean(query);
    if(searching && !sidebarSearching)for(const g of sidebarGroups.values())g.searchOpen=true;
    sidebarSearching=searching;
    const projectNames=new Map((lastSidebar.projects||[]).map(p=>[p.id,p.name]));
    const filtered=sessions.filter(s=>hidden.has(s.id)===showingHidden &&
      ((showingHidden || $('show-archive').checked || !s.archived) && (showingHidden || $('show-agents').checked || !s.is_agent) &&
      (!query || `${chatName(s)} ${s.name} ${s.cwd||''} ${projectNames.get(s.project_id)||''}`.toLocaleLowerCase().includes(query))));
    const ids=new Set(filtered.map(s=>s.id));
    function rowFor(s,placement='default',scope=filtered){
      const key=placement+':'+s.id;let el=chats.get(key);
      if(!el){
        el=node('button','chat-button');el.type='button';el.addEventListener('click',()=>select(s.id));el._sessionId=s.id;chats.set(key,el);
        const row=node('div','chat-row'),info=node('div','chat-info'),project=node('p','muted chat-project');
        info.append(el,project);
        const visibility=node('button','control button chat-visibility');visibility.type='button';
        visibility.addEventListener('click',()=>{
          const visible=[...$('chat-list').querySelectorAll('.chat-visibility')].filter(button=>button.getClientRects().length);
          const position=visible.indexOf(visibility),set=hiddenChats;
          if(set.has(s.id))set.delete(s.id);else set.add(s.id);
          save('murmuratio.hidden.'+source,JSON.stringify([...set]));renderChats(lastSessions);
          const remaining=[...$('chat-list').querySelectorAll('.chat-visibility')].filter(button=>button.getClientRects().length);
          (remaining[Math.min(Math.max(position,0),remaining.length-1)] || $('show-hidden')).focus();
        });
        row.append(info,visibility);el._row=row;el._project=project;el._visibility=visibility;
      }
      const matches=scope.filter(item=>chatName(item)===chatName(s)).map(item=>item.id).sort((a,b)=>String(a).localeCompare(String(b)));
      const suffix=matches.length>1 ? ` (${matches.indexOf(s.id)+1})` : '';
      text(el,chatName(s)+suffix);el.classList.add('codex-chat');
      el.title=[chatName(s),s.cwd].filter(Boolean).join(' — ');
      if(s.id===selected)el.setAttribute('aria-current','true');else el.removeAttribute('aria-current');
      text(el._project,[s.cwd?.split('/').filter(Boolean).at(-1),s.archived?'Archived':'',s.is_agent?'Subagent':''].filter(Boolean).join(' '));
      el._project.title=s.cwd||'';
      const action=showingHidden?'Unhide':'Hide';el._visibility.title=action;
      el._visibility.setAttribute('aria-label',`${action} chat “${chatName(s)}”`);
      el._visibility.innerHTML=showingHidden
        ? '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M9 4 4 9l5 5M4 9h10a6 6 0 0 1 0 12"/></svg>'
        : '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m3 3 18 18M10.6 10.6a2 2 0 0 0 2.8 2.8M9.8 5.3C16 3.8 21 12 21 12a18 18 0 0 1-3.3 4M6.3 6.3A19 19 0 0 0 3 12s7 11 14.7 5.7"/></svg>';
      return el._row;
    }
    const sections=Array.isArray(lastSidebar.sections)?lastSidebar.sections:[],projects=Array.isArray(lastSidebar.projects)?lastSidebar.projects:[];
    const knownSections=new Set(sections.map(s=>s.id)),knownProjects=new Set(projects.map(p=>p.id));
    const pinned=new Set(lastSidebar.legacy_pinned_ids||[]);
    const sectionItems=new Map(),projectItems=new Map(),recent=[];
    for(const s of filtered){
      if(s.section_id && knownSections.has(s.section_id)){
        if(!sectionItems.has(s.section_id))sectionItems.set(s.section_id,[]);sectionItems.get(s.section_id).push(s);
      }else if(s.is_pinned||pinned.has(s.id)){
        if(!sectionItems.has('legacy-pinned'))sectionItems.set('legacy-pinned',[]);sectionItems.get('legacy-pinned').push(s);
      }else {
        recent.push(s);
        if(s.project_id && knownProjects.has(s.project_id)){
          if(!projectItems.has(s.project_id))projectItems.set(s.project_id,[]);projectItems.get(s.project_id).push(s);
        }
      }
    }
    const groups=[];
    const legacy=sectionItems.get('legacy-pinned')||[];
    if(legacy.length){const group=sidebarGroup('section:legacy-pinned','Pinned',false,searching);order(group.body,group.details.open?legacy.map(s=>rowFor(s,'legacy-pinned',legacy)):[]);groups.push(group.details);}
    for(const sec of sections){
      const items=sectionItems.get(sec.id)||[];
      if(searching && !items.length)continue;
      items.sort((a,b)=>(a.section_position??Number.MAX_SAFE_INTEGER)-(b.section_position??Number.MAX_SAFE_INTEGER));
      const group=sidebarGroup('section:'+sec.id,sec.name,sec.collapsed,searching);
      order(group.body,group.details.open?items.map(s=>rowFor(s,'section:'+sec.id,items)):[]);groups.push(group.details);
    }
    const applicableProjects=projects.filter(p=>!searching || (projectItems.get(p.id)||[]).length);
    if(applicableProjects.length){
      const parent=sidebarGroup('projects','Projects',lastSidebar.projects_collapsed,searching),children=[];
      if(parent.details.open)for(const project of applicableProjects){
        const group=sidebarGroup('project:'+project.id,project.name,project.collapsed,searching),items=projectItems.get(project.id)||[];
        order(group.body,group.details.open?items.map(s=>rowFor(s,'project:'+project.id,items)):[]);children.push(group.details);
      }
      order(parent.body,children);groups.push(parent.details);
    }
    const shownRecent=recent.slice(0,chatLimit);
    if(!searching || recent.length){
      const group=sidebarGroup('recents','Recents',lastSidebar.recents_collapsed,searching);
      moreChats.hidden=!group.details.open || shownRecent.length>=recent.length;
      order(group.body,group.details.open?[...shownRecent.map(s=>rowFor(s,'recent',shownRecent)),moreChats]:[moreChats]);groups.push(group.details);
    }
    text($('chat-count'),`${filtered.length-(recent.length-shownRecent.length)} of ${filtered.length}`);
    if(!filtered.length && (searching || showingHidden))groups.push(node('p','muted','No chats found.'));
    order($('chat-list'),groups);
    for(const [key,el] of chats)if(!ids.has(el._sessionId))chats.delete(key);
  }
  function nearBottom() {
    const feed = $('feed');
    return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 70;
  }
  function bottom() {
    $('feed').scrollTop = $('feed').scrollHeight;
    unread.clear(); $('new-events').hidden = true;
  }
  const activity = window.createActivity({node, count});
  const ui = {open: new Map(), full: new Map(), load: async eventId => {
    const id = selected, version = generation;
    const result = await api('/api/codex/event?id=' + encodeURIComponent(id) + '&event=' + encodeURIComponent(eventId));
    if (version !== generation) throw new Error('Chat changed');
    return result.text;
  }};
  let lastData = null;
  const eventText = e => ui.full.get(e.id) ?? (e.blocks || []).map(b => b.text || '').join('\n\n');
  function renderStatus(data) {
    const events = data?.events || [];
    const st = activity.status(events, eventText);
    const line = $('session-status');
    line.dataset.state = st.state;
    const parts = [node('span', 'status-dot'), node('span', 'status-word', st.word)];
    if (st.detail) parts.push(node('span', 'status-detail', st.detail));
    line.replaceChildren(...parts);
    const meta = [], a = data?.analysis || {}, u = a.last_usage || {}, t = data?.session?.totals || {};
    // Board time format: start and minutes while running, the last event time otherwise.
    if (Number.isFinite(st.start)) meta.push(activity.clock(st.state === 'running' ? st.start : st.lastTime));
    if (st.state === 'running' && Number.isFinite(st.start)) meta.push(activity.minutes(Date.now() - st.start));
    if (isCount(u.input_tokens) && a.context_limit > 0) meta.push(`Context ${Math.round(100 * u.input_tokens / a.context_limit)}%`);
    if (isCount(t.input_tokens)) meta.push(`${activity.short(t.input_tokens + (t.output_tokens || 0))} tokens`);
    $('session-meta').replaceChildren(...meta.map(m => node('span', '', m)));
    $('session-meta').title = st.state === 'running' ? 'Started, working time' : 'Last event';
    $('session-status').hidden = $('session-meta').hidden = !events.length;
    return st;
  }
  function render(data) {
    lastData = data;
    lastAnalysis = data.analysis;
    $('reset-context').hidden = !modules.context_reset || !selected;
    $('event-log').hidden = false;
    panels.render(data, source);
    text($('session-title'), sessionName(data));
    $('collapse-all').hidden = false;
    const feed = $('feed');
    const atBottom = nearBottom();
    const top = feed.scrollTop;
    const anchor = [...feed.querySelectorAll('.turn')].find(el => el.getBoundingClientRect().bottom > feed.getBoundingClientRect().top);
    const anchorTop = anchor?.getBoundingClientRect().top;
    const grouped = new Map();
    for (const event of data.events || []) {
      const key = event.group_id || 'unlinked';
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(event);
    }
    const st = renderStatus(data);
    const nodes = [];
    let changed = false, index = 0;
    for (const [key, events] of grouped) {
      const last = index === grouped.size - 1;
      const live = last && st.state === 'running';
      const signature = JSON.stringify([live, index === 0, events.map(e => [e.id, e.status, e.usage?.input_tokens, e.usage?.output_tokens, ui.full.has(e.id), (e.blocks || []).map(b => (b.text || '').length)])]);
      let turn = turns.get(key);
      if (!turn || turn.signature !== signature) {
        const root = activity.renderTurn(key, events, ui, {live, collapsed: allCollapsed, first: index === 0,
          rerender: () => { const t = turns.get(key); if (t) t.signature = ''; if (lastData) render(lastData); }});
        turn = {root, signature}; turns.set(key, turn); changed = true;
      }
      nodes.push(turn.root);
      index++;
    }
    order(feed, nodes);
    for (const key of turns.keys()) if (!grouped.has(key)) turns.delete(key);
    if (!loaded || atBottom) bottom();
    else {
      feed.scrollTop = anchor?.isConnected ? top + anchor.getBoundingClientRect().top - anchorTop : top;
      if (changed) $('new-events').hidden = false;
    }
    text($('event-log-summary'), 'Activity');
    text($('event-count'), `${count(data.event_count)} events`);
    loaded = true;
  }
  async function loadSession() {
    if (source === 'now' || !selected) return;
    const id = selected, version = generation;
    try {
      const tail = codexEvents.size ? Math.max(...[...codexEvents.values()].map(e=>e.index)) : null;
      const data = await api('/api/codex/session?id=' + encodeURIComponent(id) + (tail !== null ? '&after='+tail : ''));
      if (version !== generation || id !== selected) return;
      $('session-error').hidden = true;
      if (sessionRevision === data.revision) { text($('session-title'), sessionName(data)); if (lastData) renderStatus(lastData); return; }
      // File rewrite/truncation invalidates earlier cached pages.
      if (sessionRevision && (sessionRevision.split(':')[0] !== data.revision.split(':')[0] || Number(sessionRevision.split(':')[1]) > Number(data.revision.split(':')[1]))) { codexEvents.clear(); sessionRevision=null; return loadSession(); }
      const first = !codexEvents.size;
      for (const e of data.events) codexEvents.set(e.id, e);
      if (first) nextBefore = data.next_before;
      sessionRevision = data.more_after ? null : data.revision;
      data.events = [...codexEvents.values()].sort((a,b)=>a.index-b.index);
      $('older-events').hidden = nextBefore === null;
      render(data);
    } catch {
      if (version !== generation) return;
      text($('session-error'), 'Could not read chat. Reconnecting…'); $('session-error').hidden = false;
    }
  }
  // On a phone the chat list folds to one line once a chat is open.
  let drawerOpen = false;
  function paintDrawer() {
    const folded = Boolean(selected) && !drawerOpen;
    document.querySelector('.chats').classList.toggle('folded', folded);
    $('chats-drawer').setAttribute('aria-expanded', String(!folded));
    text($('chats-drawer'), folded ? 'All chats' : 'Hide chat list');
  }
  $('chats-drawer').addEventListener('click', () => { drawerOpen = !drawerOpen; paintDrawer(); if (drawerOpen) $('chat-search').focus({preventScroll: true}); });
  function select(id) {
    drawerOpen = false; paintDrawer();
    if (selected === id && loaded) return;
    selected = id; generation++; loaded = false; lastAnalysis = null; save('murmuratio.selected.' + source, id);
    $('reset-context').hidden = true;
    $('reset-result').hidden = true;
    codexEvents.clear(); nextBefore = null; sessionRevision = null;
    $('analysis').hidden = true; $('older-events').hidden = true;
    $('event-log').open = true;
    $('event-log').hidden = true;
    $('session-status').hidden = $('session-meta').hidden = true; lastData = null;
    turns.clear(); ui.open.clear(); ui.full.clear(); unread.clear(); $('feed').replaceChildren();
    $('new-events').hidden = true; $('session-error').hidden = true;
    text($('session-title'), lastSessions.find(s => s.id === id) ? chatName(lastSessions.find(s => s.id === id)) : 'Chat');
    renderChats(lastSessions); void loadSession(); if (modules.context_reset && source === 'codex' && id) void pollReset();
  }
  async function poll() {
    if (busy) return;
    busy = true;
    const version = generation;
    try {
      const currentConfig = await api('/api/config');
      if (JSON.stringify(currentConfig) !== configKey) { location.reload(); return; }
      // The header (totals and limits) is shared, so Codex chats reads the Board data too.
      const [state, board] = await Promise.all([api(source === 'now' ? '/api/now' : '/api/codex/state'),
        source === 'now' ? null : api('/api/now').catch(() => null)]);
      if (version !== generation) return;
      $('connection-error').hidden = true;
      if (source === 'now') { nowView.render(state); return; }
      if (board) nowView.render(board);
      lastSidebar=state.sidebar||{};renderChats(state.sessions || []);
      await loadSession();
      if (modules.context_reset && source === 'codex' && selected) await pollReset();
    } catch {
      if (version !== generation) return;
      if (source === 'now') { nowView.error(); return; }
      text($('connection-error'), 'Source disconnected. Reconnecting…'); $('connection-error').hidden = false;
    } finally { busy = false; }
  }
  function syncSource() {
    document.body.classList.toggle('now-mode', source === 'now');
    document.documentElement.lang = 'en';
    document.title = source === 'now' ? 'Murmuratio — Board' : 'Murmuratio — Codex chats';
    text($('page-title'), source === 'now' ? 'Agents' : 'Codex chats');
    for (const name of ['now','codex']) {
      const tab = $('tab-'+name);tab.setAttribute('aria-selected', String(name===source)); tab.tabIndex = name===source ? 0 : -1;
    }
    $('now-view').hidden=source!=='now';
    $('workspace').hidden=source==='now';
    if (source==='now') return;
    $('workspace').setAttribute('aria-labelledby', 'tab-codex');
    $('reset-context').hidden=!modules.context_reset || !selected || !loaded;
    $('event-log').open=true;
  }
  async function switchSource(name) {
    if (source === name || (name === 'codex' && !modules.codex_chats)) return;
    source=name;$('show-hidden').checked=false;save('murmuratio.source',name);generation++;loaded=false;selected='';
    chats.clear();lastSessions=[];lastSidebar={};turns.clear();ui.open.clear();ui.full.clear();codexEvents.clear();lastData=null;
    $('chat-list').replaceChildren();$('feed').replaceChildren();$('analysis').hidden=true;
    $('collapse-all').hidden=true;$('older-events').hidden=true;moreChats.hidden=true;
    $('new-events').hidden=true;$('session-error').hidden=true;syncSource();
    if (source==='now') { void poll(); return; }
    select(read('murmuratio.selected.'+source));
    text($('session-title'), selected ? 'Loading…' : 'Select a chat');
    void poll();
  }
  function renderResetResult(job) {
    const section = $('reset-result');
    if (!job || !Object.keys(job).length) { section.hidden = true; return; }
    section.hidden = false;
    const line = $('reset-result-line'), note = $('reset-result-note');
    line.classList.remove('error');
    if (job.state === 'running') {
      text(line, job.stage + '…');
      text(note, 'Keep Codex closed until the operation finishes.');
      return;
    }
    if (job.reset_applied === true && job.before?.context_limit > 0 && isCount(job.before?.input_tokens)) {
      if (!Number.isInteger(job.reset_event_index)) {
        line.classList.add('error');text(line,'New window created, but comparison unavailable');
        text(note,[job.error,job.restore_error,job.storage_error].filter(Boolean).join(' ') || 'Check chat status in Codex.');
        return;
      }
      const before=job.before, marker=(lastAnalysis?.compactions||[]).find(c=>c.mode==='reset' && c.event_index===job.reset_event_index);
      const after=marker?.after_input, limit=marker?.after_limit;
      const percent=(n,total)=>new Intl.NumberFormat('en-US',{maximumFractionDigits:1}).format(100*n/total)+'%';
      const original=`${count(before.input_tokens)} tokens ${percent(before.input_tokens,before.context_limit)}`;
      if (!isCount(after) || !(limit > 0)) {
        text(line, `Before: ${original} After: waiting for a request`);
        text(note, 'Open Codex and send the same short question on the same model without tools.');
      } else if (before.model===marker.after_model && before.context_limit===limit) {
        const delta=after-before.input_tokens;
        text(line, `${count(before.input_tokens)} → ${count(after)} tokens ${percent(before.input_tokens,limit)} → ${percent(after,limit)} of window ${delta>0?'+':'−'}${count(Math.abs(delta))}`);
        text(note, 'First request after the window change. Use the same question and tools to compare.');
      } else {
        text(line, `Before: ${original} After: ${count(after)} tokens ${percent(after,limit)}`);
        text(note, 'The model or window size changed; the difference cannot be compared directly.');
      }
      if (job.error || job.restore_error || job.storage_error) {
        line.classList.add('error');text(note,[job.error,job.restore_error,job.storage_error].filter(Boolean).join(' '));
      }
      return;
    }
    if (job.before && job.after && isCount(job.context_limit) && job.context_limit > 0) {
      const before=job.before.input_tokens, after=job.after.input_tokens;
      const percent=n=>new Intl.NumberFormat('en-US',{maximumFractionDigits:1}).format(100*n/job.context_limit)+'%';
      const delta=after-before;
      text(line, `Astra ${count(before)} → ${count(after)} tokens ${percent(before)} → ${percent(after)} of window ${delta>0?'+':'−'}${count(Math.abs(delta))}`);
      text(note, 'Identical test requests without tool calls. Available tools and the working model may differ.');
      if (job.restore_error) { line.classList.add('error'); text(note, job.restore_error); }
      return;
    }
    line.classList.add('error');
    text(line, job.reset_applied === true ? 'New window created, measurement incomplete' : job.reset_applied === null ? 'Operation result unknown' : 'Reset not applied');
    text(note, [job.error,job.restore_error,job.storage_error].filter(Boolean).join(' ') || 'Check chat status in Codex.');
  }
  async function pollReset() {
    if (!modules.context_reset || source !== 'codex' || !selected) return;
    const id=selected, seq=++resetStatusSequence;
    try {
      const status=await api('/api/codex/reset/status?id='+encodeURIComponent(id));
      if (id!==selected || source!=='codex' || seq!==resetStatusSequence) return;
      const job=status.job || {}, running=job.state==='running';
      renderResetResult(running ? job : (status.result || job));
      text($('reset-context'),running?'Resetting…':'Reset context');
      if ($('reset-dialog').open && resetDialogId===id) {
        $('reset-cancel').textContent=running || resetDialogStarted ? 'Close' : 'Cancel';
        $('reset-confirm').disabled=resetStarting || running || resetDialogStarted || !status.readiness?.ready;
        text($('reset-dialog-status'),running ? job.stage+'…' : resetDialogStarted ? (job.state==='completed' ? 'Done. Open Codex and repeat the short question.' : (job.error || job.restore_error || 'Operation incomplete.')) : (resetActionError || (status.readiness?.ready ? 'Ready to create a window.' : status.readiness?.reason || 'Checking chat status…')));
      }
    } catch {
      if (id===selected && $('reset-dialog').open && resetDialogId===id) {
        $('reset-confirm').disabled=true;
        text($('reset-dialog-status'),'Could not check status. Retrying…');
      }
    }
  }
  $('reset-context').addEventListener('click',()=>{
    if (source!=='codex' || !selected) return;
    resetDialogId=selected;resetDialogStarted=false;resetStarting=false;resetActionError='';
    text($('reset-chat-name'),$('session-title').textContent);
    const last=lastAnalysis?.points?.at(-1);
    $('reset-compaction').hidden=!isCount(last?.input) || !!lastAnalysis?.pending_compaction;
    if (!$('reset-compaction').hidden) text($('reset-compaction'),`Last measurement: ${count(last.input)} tokens${last.model?' '+last.model:''}.`);
    $('reset-confirm').disabled=true;
    text($('reset-dialog-status'),'Checking chat status…');
    $('reset-dialog').showModal();$('reset-cancel').focus();void pollReset();
  });
  $('reset-cancel').addEventListener('click',()=>$('reset-dialog').close());
  $('reset-confirm').addEventListener('click',async()=>{
    if (resetStarting || resetDialogId!==selected) return;
    resetStarting=true;resetActionError='';$('reset-confirm').disabled=true;
    const id=resetDialogId, options={method:'POST',headers:{'Content-Type':'application/json','X-Murmuratio-Action':'1'}};
    try {
      text($('reset-dialog-status'),'Confirming chat status…');
      const ready=await api('/api/codex/reset/prepare',{...options,body:JSON.stringify({id})});
      if (!ready.ready) throw new Error(ready.reason || 'Chat is not ready for reset.');
      await api('/api/codex/reset/start',{...options,body:JSON.stringify({id,token:ready.token})});
      resetDialogStarted=true;
    } catch (error) { resetActionError=error.message || 'Could not start reset.';text($('reset-dialog-status'),resetActionError); }
    finally { resetStarting=false;void pollReset(); }
  });
  const sourceNames = ['now','codex'];
  for(const name of sourceNames) {
    $('tab-'+name).addEventListener('click',()=>void switchSource(name));
    $('tab-'+name).addEventListener('keydown',e=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
      e.preventDefault();const target=e.key==='Home'?'now':e.key==='End'?'codex':sourceNames[(sourceNames.indexOf(name)+1)%2];
      $('tab-'+target).focus();void switchSource(target);
    });
  }
  for(const id of ['chat-search','show-archive','show-agents']) {
    const el=$(id);
    if(el.type==='checkbox')el.checked=read('murmuratio.'+id)==='true';else el.value=read('murmuratio.'+id);
    el.addEventListener('input',()=>{save('murmuratio.'+id,el.type==='checkbox'?String(el.checked):el.value);chatLimit=60;renderChats(lastSessions);});
  }
  $('show-hidden').addEventListener('change',()=>{chatLimit=60;renderChats(lastSessions);});
  moreChats.addEventListener('click',()=>{chatLimit+=60;renderChats(lastSessions);});
  $('older-events').addEventListener('click',async()=>{
    if(nextBefore===null)return;
    const version=generation, cursor=nextBefore, id=selected;
    $('older-events').disabled=true;
    try {
      const data=await api('/api/codex/session?id='+encodeURIComponent(id)+'&before='+cursor);
      if(version!==generation)return;
      for(const e of data.events)codexEvents.set(e.id,e);
      nextBefore=data.next_before;data.events=[...codexEvents.values()].sort((a,b)=>a.index-b.index);
      // Preserve the currently visible anchor while prepending older events.
      const feed=$('feed'), height=feed.scrollHeight, top=feed.scrollTop;
      render(data);feed.scrollTop=top+feed.scrollHeight-height;
      $('older-events').hidden=nextBefore===null;
    } catch { if(version===generation){text($('session-error'),'Could not load earlier events. Try again.');$('session-error').hidden=false;} }
    finally{$('older-events').disabled=false;}
  });
  // The separator owns only sidebar geometry; it never changes source data.
  const splitter = $('sidebar-resizer'), workspace = $('workspace');
  let preferredWidth = Number(read('murmuratio.sidebarWidth', '280')) || 280;
  let dragging = null;
  function sidebarBounds() {
    return {min: 200, max: Math.max(200, Math.min(600, workspace.clientWidth - 420))};
  }
  function resizeSidebar(width, persist = false) {
    const {min,max} = sidebarBounds();
    const actual = Math.round(Math.max(min, Math.min(max, width)));
    workspace.style.setProperty('--sidebar-width', actual + 'px');
    splitter.setAttribute('aria-valuemin', String(min));splitter.setAttribute('aria-valuemax', String(max));
    splitter.setAttribute('aria-valuenow', String(actual));splitter.setAttribute('aria-valuetext', actual + ' pixels');
    workspace.style.setProperty('--workspace-top', Math.round(workspace.getBoundingClientRect().top + scrollY) + 'px');
    if (persist) { preferredWidth=actual;save('murmuratio.sidebarWidth', String(actual)); }
    return actual;
  }
  splitter.addEventListener('pointerdown', e => {
    if(e.button !== 0 || matchMedia('(max-width: 760px)').matches) return;
    e.preventDefault();dragging={id:e.pointerId,x:e.clientX,width:document.querySelector('.chats').getBoundingClientRect().width};
    splitter.setPointerCapture(e.pointerId);document.body.classList.add('resizing-sidebar');
  });
  splitter.addEventListener('pointermove', e => {
    if(dragging?.id===e.pointerId) resizeSidebar(dragging.width+e.clientX-dragging.x);
  });
  const finishResize = e => {
    if(dragging?.id!==e.pointerId)return;
    resizeSidebar(Number(splitter.getAttribute('aria-valuenow')),true);
    dragging=null;document.body.classList.remove('resizing-sidebar');
    if(splitter.hasPointerCapture(e.pointerId))splitter.releasePointerCapture(e.pointerId);
  };
  splitter.addEventListener('pointerup',finishResize);splitter.addEventListener('pointercancel',finishResize);
  splitter.addEventListener('lostpointercapture',finishResize);
  splitter.addEventListener('keydown',e=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
    e.preventDefault();const {min,max}=sidebarBounds();const current=Number(splitter.getAttribute('aria-valuenow'));
    resizeSidebar(e.key==='Home'?min:e.key==='End'?max:current+(e.key==='ArrowLeft'?-1:1)*(e.shiftKey?40:10),true);
  });
  new ResizeObserver(()=>{ if (!dragging) resizeSidebar(preferredWidth); }).observe(workspace);
  resizeSidebar(preferredWidth);
  $('collapse-all').addEventListener('click', () => {
    const folds = [...$('feed').querySelectorAll('.turn-fold')];
    const collapse = folds.some(d => d.open);
    allCollapsed = collapse; save('murmuratio.collapsed', String(collapse));
    for (const d of folds) d.open = !collapse;
    text($('collapse-all'), collapse ? 'Expand all' : 'Collapse all');
  });
  text($('collapse-all'), allCollapsed ? 'Expand all' : 'Collapse all');
  $('new-events').addEventListener('click', bottom);
  $('feed').addEventListener('scroll', () => { if (nearBottom()) { unread.clear(); $('new-events').hidden = true; } }, {passive: true});
  async function start() {
    try {
      const config = await api('/api/config');
      modules = config.modules || {}; configKey = JSON.stringify(config); nowView.configure(config);
    } catch { nowView.error(); setTimeout(start, 3000); return; }
    if (!modules.codex_chats) source = 'now';
    document.querySelector('.source-tabs').hidden = !modules.codex_chats;
    syncSource(); paintDrawer(); void poll();
    setInterval(() => { if (!document.hidden) void poll(); }, 3000);
  }
  void start();
})();
