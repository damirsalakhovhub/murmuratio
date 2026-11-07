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
  const linkedCodex = new URLSearchParams(location.search).get('codex');
  let modules = {}, configKey = '';
  let source = linkedCodex ? 'codex' : read('inspector.source', 'now');
  if (!['now', 'codex'].includes(source)) source = 'now';
  let selected = linkedCodex || read('inspector.selected.' + source);
  if (linkedCodex) save('inspector.selected.codex', linkedCodex);
  let hiddenIds; try { hiddenIds = JSON.parse(read('inspector.hidden.codex', '[]')); } catch { hiddenIds = []; }
  const hiddenChats = new Set(Array.isArray(hiddenIds) ? hiddenIds.filter(id => typeof id === 'string') : []);
  let chatLimit = 60;
  let nextBefore = null;
  let sessionRevision = null;
  const codexEvents = new Map();
  let allCollapsed = read('inspector.collapsed') === 'true';
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
  try { groupPreferences=JSON.parse(read('inspector.sidebarGroups', '{}')); } catch { groupPreferences={}; }
  if (!groupPreferences || Array.isArray(groupPreferences) || typeof groupPreferences !== 'object') groupPreferences={};
  let sidebarSearching = false;
  const turns = new Map();
  const packets = new Map();
  const unread = new Set();
  const pretty = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  const parse = value => { try { return JSON.parse(value); } catch { return value; } };
  const numbers = new Intl.NumberFormat('ru-RU');
  const isCount = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const count = value => isCount(value) ? numbers.format(value) : 'нет данных';
  function tokenText(usage) {
    const u = usage || {};
    if (![u.input_tokens, u.output_tokens, u.total_tokens].some(isCount)) return 'Статистика ещё не получена';
    const directions = `→ ${count(u.input_tokens)} ← ${count(u.output_tokens)}`;
    return `${directions} токенов`;
  }

  const panels = window.createInspectorPanels({node, count});

  async function api(path, options = {}) {
    const response = await fetch(path, {cache: 'no-store', credentials: 'same-origin', ...options});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Источник недоступен');
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
        else {group.open=!group.open;groupPreferences[key]=group.open;save('inspector.sidebarGroups',JSON.stringify(groupPreferences));}
        renderChats(lastSessions);
      });
      sidebarGroups.set(key,group);
    }
    text(group.summary,label);group.summary.title=label;
    group.details.open=searching ? group.searchOpen : group.open;
    return group;
  }
  function renderChats(sessions) {
    lastSessions=sessions;
    const hidden=hiddenChats,showingHidden=$('show-hidden').checked;
    text($('hidden-chats-label'),`Скрытые ${sessions.filter(s=>hidden.has(s.id)).length}`);
    moreChats.hidden=true;
    const query=$('chat-search').value.trim().toLocaleLowerCase();
    const searching=Boolean(query);
    if(searching && !sidebarSearching)for(const g of sidebarGroups.values())g.searchOpen=true;
    sidebarSearching=searching;
    const projectNames=new Map((lastSidebar.projects||[]).map(p=>[p.id,p.name]));
    const filtered=sessions.filter(s=>hidden.has(s.id)===showingHidden &&
      ((showingHidden || $('show-archive').checked || !s.archived) && (showingHidden || $('show-agents').checked || !s.is_agent) &&
      (!query || `${s.name} ${s.cwd||''} ${projectNames.get(s.project_id)||''}`.toLocaleLowerCase().includes(query))));
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
          save('inspector.hidden.'+source,JSON.stringify([...set]));renderChats(lastSessions);
          const remaining=[...$('chat-list').querySelectorAll('.chat-visibility')].filter(button=>button.getClientRects().length);
          (remaining[Math.min(Math.max(position,0),remaining.length-1)] || $('show-hidden')).focus();
        });
        row.append(info,visibility);el._row=row;el._project=project;el._visibility=visibility;
      }
      const matches=scope.filter(item=>item.name===s.name).map(item=>item.id).sort((a,b)=>String(a).localeCompare(String(b)));
      const suffix=matches.length>1 ? ` (${matches.indexOf(s.id)+1})` : '';
      text(el,(s.name||'Без названия')+suffix);el.classList.add('codex-chat');
      el.title=[s.name,s.cwd].filter(Boolean).join(' — ');
      if(s.id===selected)el.setAttribute('aria-current','true');else el.removeAttribute('aria-current');
      text(el._project,[s.cwd?.split('/').filter(Boolean).at(-1),s.archived?'Архив':'',s.is_agent?'Субагент':''].filter(Boolean).join(' '));
      el._project.title=s.cwd||'';
      const action=showingHidden?'Вернуть':'Скрыть';el._visibility.title=action;
      el._visibility.setAttribute('aria-label',`${action} чат «${s.name||'Без названия'}»`);
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
    text($('chat-count'),`${filtered.length-(recent.length-shownRecent.length)} из ${filtered.length}`);
    if(!filtered.length && (searching || showingHidden))groups.push(node('p','muted','Чаты не найдены.'));
    order($('chat-list'),groups);
    for(const [key,el] of chats)if(!ids.has(el._sessionId))chats.delete(key);
  }
  function describe(event) {
    const blocks = event.blocks || [];
    const raw = blocks.map(b => (b.text || '') + (b.truncated ? `\n\n[Показаны первые 16 000 символов из ${count(b.characters)}]` : '')).join('\n\n');
    if (event.kind === 'model_record') {
      // One usage line per saved model record, not per tool or repeated answer part.
      return {label: '→ Обращение к модели', content: '',
        tokens: tokenText(event.usage), provenance: 'По сохранённым данным Codex'};
    }
    if (event.kind === 'tool_call') {
      const call = parse(raw);
      return {label: '→ ' + (typeof call === 'object' && call?.name ? call.name : 'Инструмент'),
        content: typeof call === 'object' && call ? pretty(call.arguments ?? call) : raw};
    }
    if (event.kind === 'tool_result') {
      const result = parse(raw);
      const value = result?.value ?? result;
      const content = Array.isArray(value?.content)
        ? value.content.map(part => part.type === 'text' ? part.text : pretty(part)).join('\n\n')
        : pretty(value);
      return {label: event.status === 'Ошибка' ? '← Ошибка инструмента' : '← Результат инструмента', content};
    }
    const labels = {user: 'Вы', assistant: '← Ответ модели', context: '→ Контекст', error: 'Ошибка',
      compaction: 'Сжатие контекста', context_reset: 'Сброс контекста', model_change: 'Смена модели', notification: 'Служебное событие'};
    return {label: labels[event.kind] || 'Событие', content: raw};
  }
  function requestDetails(event, details) {
    const body = node('div', 'request-details-body');
    const u = event.usage || {};
    body.append(node('p', '', `Вход ${count(u.input_tokens)} кеш ${count(u.cache_read_tokens)} выход ${count(u.output_tokens)} reasoning ${count(u.reasoning_tokens)}.`));
    body.append(node('p', '', `Модель в записи хода: ${event.inference?.requestedModel || 'неизвестна'}. Модель в usage: ${event.inference?.resolvedModel || 'не сообщена'}.`));
    body.append(node('p', 'muted', 'Кеш включён во вход, reasoning — в выход. Сохранённые сообщения не восстанавливают точный сетевой запрос. Разбор крупных блоков доступен в «Что занимает место».'));
    details.append(body);
  }
  function packet(event) {
    const description = describe(event);
    let view = packets.get(event.id);
    if (!view) {
      const root = node('article', 'packet');
      view = {root, signature: '', body: null}; packets.set(event.id, view);
    }
    const signature = JSON.stringify([description, event.kind === 'model_record' ? event.blocks : null]);
    // Refresh the metadata used on next expansion without duplicating whole histories per call.
    view.event = event;
    if (signature === view.signature) return {root: view.root, changed: false};
    const wasOpen = view.body?.open;
    const whyOpen = view.why?.open;
    const long = description.content.length > 900;
    const heading = node('p', 'packet-label', description.label);
    const body = node('pre', 'packet-text', description.content);
    if (event.kind === 'error' || event.status === 'Ошибка') heading.classList.add('error');
    view.root.replaceChildren();
    if (long) {
      const details = node('details', 'packet-content');
      const summary = node('summary', '', description.label);
      if (heading.classList.contains('error')) summary.classList.add('error');
      details.open = wasOpen ?? false;
      details.append(summary, body); view.root.append(details); view.body = details;
    } else {
      view.root.append(heading);
      if (description.content) view.root.append(body);
      view.body = null;
    }
    if ((event.blocks || []).some(b=>b.truncated)) {
      const expand = node('button','control button','Показать полный текст'); expand.type='button';
      const sid=selected, version=generation;
      expand.addEventListener('click',async()=>{
        expand.disabled=true;
        try {
          const result=await api('/api/codex/event?id='+encodeURIComponent(sid)+'&event='+encodeURIComponent(event.id));
          if(version!==generation)return;
          body.textContent=result.text;expand.remove();
        } catch {text(expand,'Не удалось загрузить. Повторить');expand.disabled=false;}
      });
      view.root.append(expand);
    }
    if (description.tokens) {
      const usage = node('p', 'token-count packet-tokens', `${description.tokens} ${description.provenance}`);
      usage.title = '→ Вход модели: весь контекст, включая кеш. ← Выход модели. Не размер отдельного сообщения или результата инструмента.';
      view.root.append(usage);
      const why = node('details', 'request-details');
      why.append(node('summary', '', 'Откуда токены?'));
      let ready = false;
      why.addEventListener('toggle', () => {
        if (why.open && !ready) { requestDetails(view.event, why); ready = true; }
      });
      // Late usage updates keep the top-level explanation expanded.
      if (whyOpen) { requestDetails(event, why); ready = true; }
      why.open = whyOpen ?? false;
      view.root.append(why); view.why = why;
    }
    view.root.classList.toggle('user-packet', event.kind === 'user');
    view.signature = signature;
    return {root: view.root, changed: true};
  }
  function nearBottom() {
    const feed = $('feed');
    return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 70;
  }
  function bottom() {
    $('feed').scrollTop = $('feed').scrollHeight;
    unread.clear(); $('new-events').hidden = true;
  }
  function render(data) {
    lastAnalysis = data.analysis;
    $('reset-context').hidden = !modules.context_reset || !selected;
    $('event-log').hidden = false;
    panels.render(data, source);
    text($('session-title'), data.session.name || 'Без названия');
    $('collapse-all').hidden = false;
    const feed = $('feed');
    const atBottom = nearBottom();
    const top = feed.scrollTop;
    const anchor = [...feed.querySelectorAll('.packet')].find(el => el.getBoundingClientRect().bottom > feed.getBoundingClientRect().top);
    const anchorTop = anchor?.getBoundingClientRect().top;
    const grouped = new Map();
    for (const event of data.events || []) {
      const key = event.group_id || 'unlinked';
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(event);
    }
    const nodes = [];
    const seen = new Set();
    let changed = false;
    for (const [key, events] of grouped) {
      let turn = turns.get(key);
      if (!turn) {
        const root = node('section', 'turn');
        const user = node('div', 'user-messages');
        const answers = node('details', 'answers');
        const summary = node('summary', '', 'Ответ');
        const body = node('div', 'answer-packets');
        answers.open = !allCollapsed;
        answers.append(summary, body); root.append(user, answers);
        turn = {root, user, answers, summary, body}; turns.set(key, turn);
      }
      const userNodes = [], responseNodes = [];
      for (const event of events) {
        seen.add(event.id);
        const result = packet(event);
        if (result.changed) { changed = true; unread.add(event.id); }
        (event.kind === 'user' ? userNodes : responseNodes).push(result.root);
      }
      order(turn.user, userNodes); order(turn.body, responseNodes);
      turn.answers.hidden = !responseNodes.length;
      const failed = events.some(e => e.kind === 'error' || e.status === 'Ошибка');
      text(turn.summary, failed ? 'Ответ есть ошибка' : 'Ответ');
      nodes.push(turn.root);
    }
    order(feed, nodes);
    for (const key of turns.keys()) if (!grouped.has(key)) turns.delete(key);
    for (const id of packets.keys()) if (!seen.has(id)) packets.delete(id);
    if (!loaded || atBottom) bottom();
    else {
      feed.scrollTop = anchor?.isConnected ? top + anchor.getBoundingClientRect().top - anchorTop : top;
      if (changed) $('new-events').hidden = false;
    }
    text($('event-log-summary'), `События чата ${count(data.event_count)}`);
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
      if (sessionRevision === data.revision) { text($('session-title'), data.session.name || 'Без названия'); return; }
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
      text($('session-error'), 'Не удалось прочитать чат. Повторяем подключение…'); $('session-error').hidden = false;
    }
  }
  function select(id) {
    if (selected === id && loaded) return;
    selected = id; generation++; loaded = false; lastAnalysis = null; save('inspector.selected.' + source, id);
    $('reset-context').hidden = true;
    $('reset-result').hidden = true;
    codexEvents.clear(); nextBefore = null; sessionRevision = null;
    $('analysis').hidden = true; $('older-events').hidden = true;
    $('event-log').open = false;
    $('event-log').hidden = true;
    turns.clear(); packets.clear(); unread.clear(); $('feed').replaceChildren();
    $('new-events').hidden = true; $('session-error').hidden = true;
    text($('session-title'), lastSessions.find(s => s.id === id)?.name || 'Чат');
    renderChats(lastSessions); void loadSession(); if (modules.context_reset && source === 'codex' && id) void pollReset();
  }
  async function poll() {
    if (busy) return;
    busy = true;
    const version = generation;
    try {
      const currentConfig = await api('/api/config');
      if (JSON.stringify(currentConfig) !== configKey) { location.reload(); return; }
      const state = await api(source === 'now' ? '/api/now' : '/api/codex/state');
      if (version !== generation) return;
      $('connection-error').hidden = true;
      if (source === 'now') { nowView.render(state); return; }
      lastSidebar=state.sidebar||{};renderChats(state.sessions || []);
      await loadSession();
      if (modules.context_reset && source === 'codex' && selected) await pollReset();
    } catch {
      if (version !== generation) return;
      if (source === 'now') { nowView.error(); return; }
      text($('connection-error'), 'Нет связи с источником. Повторяем подключение…'); $('connection-error').hidden = false;
    } finally { busy = false; }
  }
  function syncSource() {
    document.body.classList.toggle('now-mode', source === 'now');
    document.documentElement.lang = source === 'now' ? 'en' : 'ru';
    document.title = source === 'now' ? 'Murmuratio — Board' : 'Murmuratio';
    for (const name of ['now','codex']) {
      const tab = $('tab-'+name);tab.setAttribute('aria-selected', String(name===source)); tab.tabIndex = name===source ? 0 : -1;
    }
    $('now-view').hidden=source!=='now';
    $('workspace').hidden=source==='now';
    if (source==='now') return;
    $('workspace').setAttribute('aria-labelledby', 'tab-codex');
    $('reset-context').hidden=!modules.context_reset || !selected || !loaded;
    $('event-log').open=false;
  }
  async function switchSource(name) {
    if (source === name || (name === 'codex' && !modules.codex_chats)) return;
    source=name;$('show-hidden').checked=false;save('inspector.source',name);generation++;loaded=false;selected='';
    chats.clear();lastSessions=[];lastSidebar={};turns.clear();packets.clear();codexEvents.clear();
    $('chat-list').replaceChildren();$('feed').replaceChildren();$('analysis').hidden=true;
    $('collapse-all').hidden=true;$('older-events').hidden=true;moreChats.hidden=true;
    $('new-events').hidden=true;$('session-error').hidden=true;syncSource();
    if (source==='now') { void poll(); return; }
    select(read('inspector.selected.'+source));
    text($('session-title'), selected ? 'Загрузка…' : 'Выберите чат');
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
      text(note, 'Не открывайте Codex до окончания операции.');
      return;
    }
    if (job.reset_applied === true && job.before?.context_limit > 0 && isCount(job.before?.input_tokens)) {
      if (!Number.isInteger(job.reset_event_index)) {
        line.classList.add('error');text(line,'Новое окно создано, но сравнение недоступно');
        text(note,[job.error,job.restore_error,job.storage_error].filter(Boolean).join(' ') || 'Проверьте состояние чата в Codex.');
        return;
      }
      const before=job.before, marker=(lastAnalysis?.compactions||[]).find(c=>c.mode==='reset' && c.event_index===job.reset_event_index);
      const after=marker?.after_input, limit=marker?.after_limit;
      const percent=(n,total)=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1}).format(100*n/total)+'%';
      const original=`${count(before.input_tokens)} токенов ${percent(before.input_tokens,before.context_limit)}`;
      if (!isCount(after) || !(limit > 0)) {
        text(line, `До: ${original} После: ждём запрос`);
        text(note, 'Откройте Codex и отправьте тот же короткий вопрос на прежней модели без инструментов.');
      } else if (before.model===marker.after_model && before.context_limit===limit) {
        const delta=after-before.input_tokens;
        text(line, `${count(before.input_tokens)} → ${count(after)} токенов ${percent(before.input_tokens,limit)} → ${percent(after,limit)} окна ${delta>0?'+':'−'}${count(Math.abs(delta))}`);
        text(note, 'Первый запрос после смены окна. Для сравнения вопрос и инструменты должны быть одинаковыми.');
      } else {
        text(line, `До: ${original} После: ${count(after)} токенов ${percent(after,limit)}`);
        text(note, 'Модель или размер окна изменились; разницу напрямую сравнивать нельзя.');
      }
      if (job.error || job.restore_error || job.storage_error) {
        line.classList.add('error');text(note,[job.error,job.restore_error,job.storage_error].filter(Boolean).join(' '));
      }
      return;
    }
    if (job.before && job.after && isCount(job.context_limit) && job.context_limit > 0) {
      const before=job.before.input_tokens, after=job.after.input_tokens;
      const percent=n=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1}).format(100*n/job.context_limit)+'%';
      const delta=after-before;
      text(line, `Astra ${count(before)} → ${count(after)} токенов ${percent(before)} → ${percent(after)} окна ${delta>0?'+':'−'}${count(Math.abs(delta))}`);
      text(note, 'Одинаковые контрольные запросы без вызова инструментов. Состав инструментов и рабочая модель могут отличаться.');
      if (job.restore_error) { line.classList.add('error'); text(note, job.restore_error); }
      return;
    }
    line.classList.add('error');
    text(line, job.reset_applied === true ? 'Новое окно создано, замер не завершён' : job.reset_applied === null ? 'Результат операции неизвестен' : 'Сброс не выполнен');
    text(note, [job.error,job.restore_error,job.storage_error].filter(Boolean).join(' ') || 'Проверьте состояние чата в Codex.');
  }
  async function pollReset() {
    if (!modules.context_reset || source !== 'codex' || !selected) return;
    const id=selected, seq=++resetStatusSequence;
    try {
      const status=await api('/api/codex/reset/status?id='+encodeURIComponent(id));
      if (id!==selected || source!=='codex' || seq!==resetStatusSequence) return;
      const job=status.job || {}, running=job.state==='running';
      renderResetResult(running ? job : (status.result || job));
      text($('reset-context'),running?'Сброс выполняется…':'Сбросить контекст');
      if ($('reset-dialog').open && resetDialogId===id) {
        $('reset-cancel').textContent=running || resetDialogStarted ? 'Закрыть' : 'Отмена';
        $('reset-confirm').disabled=resetStarting || running || resetDialogStarted || !status.readiness?.ready;
        text($('reset-dialog-status'),running ? job.stage+'…' : resetDialogStarted ? (job.state==='completed' ? 'Готово. Откройте Codex и повторите короткий вопрос.' : (job.error || job.restore_error || 'Операция не завершена.')) : (resetActionError || (status.readiness?.ready ? 'Всё готово к созданию окна.' : status.readiness?.reason || 'Проверяем состояние чата…')));
      }
    } catch {
      if (id===selected && $('reset-dialog').open && resetDialogId===id) {
        $('reset-confirm').disabled=true;
        text($('reset-dialog-status'),'Не удалось проверить состояние. Повторяем…');
      }
    }
  }
  $('reset-context').addEventListener('click',()=>{
    if (source!=='codex' || !selected) return;
    resetDialogId=selected;resetDialogStarted=false;resetStarting=false;resetActionError='';
    text($('reset-chat-name'),$('session-title').textContent);
    const last=lastAnalysis?.points?.at(-1);
    $('reset-compaction').hidden=!isCount(last?.input) || !!lastAnalysis?.pending_compaction;
    if (!$('reset-compaction').hidden) text($('reset-compaction'),`Последний замер: ${count(last.input)} токенов${last.model?' '+last.model:''}.`);
    $('reset-confirm').disabled=true;
    text($('reset-dialog-status'),'Проверяем состояние чата…');
    $('reset-dialog').showModal();$('reset-cancel').focus();void pollReset();
  });
  $('reset-cancel').addEventListener('click',()=>$('reset-dialog').close());
  $('reset-confirm').addEventListener('click',async()=>{
    if (resetStarting || resetDialogId!==selected) return;
    resetStarting=true;resetActionError='';$('reset-confirm').disabled=true;
    const id=resetDialogId, options={method:'POST',headers:{'Content-Type':'application/json','X-Inspector-Action':'1'}};
    try {
      text($('reset-dialog-status'),'Подтверждаем состояние чата…');
      const ready=await api('/api/codex/reset/prepare',{...options,body:JSON.stringify({id})});
      if (!ready.ready) throw new Error(ready.reason || 'Чат не готов к сбросу.');
      await api('/api/codex/reset/start',{...options,body:JSON.stringify({id,token:ready.token})});
      resetDialogStarted=true;
    } catch (error) { resetActionError=error.message || 'Не удалось запустить сброс.';text($('reset-dialog-status'),resetActionError); }
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
    if(el.type==='checkbox')el.checked=read('inspector.'+id)==='true';else el.value=read('inspector.'+id);
    el.addEventListener('input',()=>{save('inspector.'+id,el.type==='checkbox'?String(el.checked):el.value);chatLimit=60;renderChats(lastSessions);});
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
    } catch { if(version===generation){text($('session-error'),'Не удалось загрузить ранние события. Повторите.');$('session-error').hidden=false;} }
    finally{$('older-events').disabled=false;}
  });
  // The separator owns only sidebar geometry; it never changes source data.
  const splitter = $('sidebar-resizer'), workspace = $('workspace');
  let preferredWidth = Number(read('inspector.sidebarWidth', '280')) || 280;
  let dragging = null;
  function sidebarBounds() {
    return {min: 200, max: Math.max(200, Math.min(600, workspace.clientWidth - 420))};
  }
  function resizeSidebar(width, persist = false) {
    const {min,max} = sidebarBounds();
    const actual = Math.round(Math.max(min, Math.min(max, width)));
    workspace.style.setProperty('--sidebar-width', actual + 'px');
    splitter.setAttribute('aria-valuemin', String(min));splitter.setAttribute('aria-valuemax', String(max));
    splitter.setAttribute('aria-valuenow', String(actual));splitter.setAttribute('aria-valuetext', actual + ' пикселей');
    workspace.style.setProperty('--workspace-top', Math.round(workspace.getBoundingClientRect().top + scrollY) + 'px');
    if (persist) { preferredWidth=actual;save('inspector.sidebarWidth', String(actual)); }
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
    const collapse = [...turns.values()].some(turn => !turn.answers.hidden && turn.answers.open);
    allCollapsed = collapse; save('inspector.collapsed', String(collapse));
    for (const turn of turns.values()) turn.answers.open = !collapse;
    text($('collapse-all'), collapse ? 'Развернуть все ответы' : 'Свернуть все ответы');
  });
  text($('collapse-all'), allCollapsed ? 'Развернуть все ответы' : 'Свернуть все ответы');
  $('new-events').addEventListener('click', bottom);
  $('feed').addEventListener('scroll', () => { if (nearBottom()) { unread.clear(); $('new-events').hidden = true; } }, {passive: true});
  async function start() {
    try {
      const config = await api('/api/config');
      modules = config.modules || {}; configKey = JSON.stringify(config); nowView.configure(config);
    } catch { nowView.error(); setTimeout(start, 3000); return; }
    if (!modules.codex_chats) source = 'now';
    document.querySelector('.source-tabs').hidden = !modules.codex_chats;
    syncSource(); void poll();
    setInterval(() => { if (!document.hidden) void poll(); }, 3000);
  }
  void start();
})();
