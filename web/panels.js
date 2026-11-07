'use strict';
window.createInspectorPanels = ({node, count}) => {
  let signature='';
  const finite=n=>typeof n==='number' && Number.isFinite(n);
  const date=s=>s ? new Date(s).toLocaleString('en-GB',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—';
  const short=n=>{
    if(!finite(n))return '—';
    const sign=n<0?'−':'', magnitude=Math.abs(n);
    const f=new Intl.NumberFormat('en-US',{maximumFractionDigits:1});
    return sign+(magnitude>=1e6 ? f.format(magnitude/1e6)+'M' : magnitude>=1e3 ? f.format(magnitude/1e3)+'K' : count(magnitude));
  };
  const stack=()=>node('div','analysis-body');
  function fold(id,title,body){const d=node('details','analysis-fold');d.dataset.fold=id;d.append(node('summary','',title),body);return d;}
  function card(id,title){const c=node('section','metric-block');c.dataset.metric=id;c.append(node('h3','metric-title',title));return c;}
  function value(label,number,caption){const v=node('div','metric-value-group');if(label)v.append(node('p','metric-label',label));v.append(node('p','metric-number',number));if(caption)v.append(node('p','metric-caption',caption));return v;}
  function chart(points){
    const root=node('div','trend-preview');
    if(!points.length)return root;
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('viewBox','0 0 600 90');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('role','img');
    svg.setAttribute('aria-label','Input context by request; gaps mark compaction');svg.classList.add('context-chart');
    const max=Math.max(1,...points.map(p=>p.input)), groups=[];
    points.forEach((p,i)=>{if(!i||points[i-1].segment!==p.segment)groups.push([]);groups.at(-1).push([5+i*590/Math.max(1,points.length-1),85-p.input/max*80]);});
    for(const g of groups){const line=document.createElementNS(svg.namespaceURI,'polyline');line.setAttribute('points',g.map(x=>x.join(',')).join(' '));svg.append(line);}
    const first=points[0],last=points.at(-1);
    if(points.length===1){const dot=document.createElementNS(svg.namespaceURI,'circle');dot.setAttribute('cx','5');dot.setAttribute('cy','5');dot.setAttribute('r','2');svg.append(dot);}
    const labels=node('div','chart-labels');labels.append(node('span','',short(first.input)),node('span','',short(last.input)));
    root.append(svg,labels);return root;
  }
  function render(data,source){
    const root=document.getElementById('analysis');root.hidden=source!=='codex';
    if(source!=='codex'){signature='';return;}
    const a=data.analysis||{},u=a.last_usage||{},t=data.session.totals||{};
    const next=JSON.stringify([data.session.id,a,t]);if(next===signature)return;
    const open=new Set(root.dataset.session===data.session.id ? [...root.querySelectorAll('details[open]')].map(x=>x.dataset.fold) : []);
    const focus=root.contains(document.activeElement) ? document.activeElement.closest('details')?.dataset.fold : null;
    root.dataset.session=data.session.id;signature=next;

    const context=card('context','Context');
    const percentage=finite(u.input_tokens)&&a.context_limit>0 ? Math.round(100*u.input_tokens/a.context_limit)+'%' : '—';
    const main=node('div','context-main');
    main.append(value('',a.pending_compaction||a.pending_context?'—':percentage, a.pending_context?'Waiting for measurement':a.pending_compaction?'Waiting for measurement after compaction':`${short(u.input_tokens)} of ${short(a.context_limit)} tokens`));
    const points=(a.points||[]).filter(p=>finite(p.input));
    main.append(chart(points));context.append(main);
    context.append(node('p','metric-caption',`Last request ${date(a.last_at)}`));
    const composition=stack();
    composition.append(node('p','muted','Text estimate after compaction. The full network request and encrypted summary are unavailable.'));
    for(const c of a.categories||[]){const row=node('div','breakdown-row');row.append(node('span','',c.name),node('span','',`≈ ${short(c.estimated_tokens)}`));composition.append(row);}
    for(const [i,b] of (a.largest_blocks||[]).entries()){
      const body=stack();body.append(node('pre','packet-text',b.preview),node('p','muted',`${count(b.characters)} characters, first 2,000 shown${b.duplicates>1?' repeats '+b.duplicates:''}`));
      composition.append(fold('block-'+i,`${b.title} ≈ ${short(b.estimated_tokens)}`,body));
    }
    composition.append(node('p','muted','≈ characters / 4. Size or repetition alone does not mean content is useless.'));
    const exact=stack();
    exact.append(node('p','muted',`Input ${count(u.input_tokens)} of ${count(a.context_limit)} tokens. Measures the previous request, not the next one.`));
    exact.append(node('p','muted',`Chart: latest ${points.length} of ${count(a.point_count)} requests, oldest on the left; gaps mark compaction. Vertical scale from 0 to ${count(Math.max(0,...points.map(p=>p.input)))}.`));
    for(const p of points.slice().reverse())exact.append(node('p','token-count',`${date(p.time)} ${count(p.input)} input ${count(p.output)} output`));
    const contextDetails=node('div','metric-details');contextDetails.append(fold('composition','Context composition',composition),fold('history','Request history',exact));context.append(contextDetails);

    const spend=card('spend','Usage');
    const numbers=node('div','metric-pair');numbers.append(value('Input',short(t.input_tokens)),value('Output',short(t.output_tokens)));spend.append(numbers);
    spend.append(node('p','metric-caption',`${count(a.point_count)} requests across the chat`));
    const costs=stack();
    for(const [label,v] of [['Input',t.input_tokens],['Including cache',t.cache_read_tokens],['Output',t.output_tokens],['Including reasoning',t.reasoning_tokens]]){
      const row=node('div','breakdown-row');row.append(node('span','',label),node('span','',count(v)));costs.append(row);
    }
    costs.append(node('p','muted','Cache is included in input; reasoning is included in output. Resending history increases usage without filling the window again. These are tokens, not monetary charges.'));
    costs.append(node('p','muted',`Last request: ${count(u.input_tokens)} input ${count(u.cache_read_tokens)} cache ${count(u.output_tokens)} output ${count(u.reasoning_tokens)} reasoning.`));
    for(const key of ['primary','secondary']){const v=a.rate_limits?.[key];if(v)costs.append(node('p','muted',`Account limit snapshot: ${count(v.used_percent)}% over ${count(v.window_minutes)} min. Resets ${date(v.resets_at*1000)}. May be outdated.`));}
    spend.append(fold('costs','Usage details',costs));

    const model=card('model','Model');
    const raw=a.model||'';
    // Only format an observed public ID, never infer a version from a display name.
    const match=/^gpt-(\d+(?:\.\d+)?)-(astra|sol|terra|luna)$/.exec(raw);
    const display=match ? match[2][0].toUpperCase()+match[2].slice(1)+' '+match[1] : raw||'—';
    model.append(node('p','metric-number model-number',display));
    model.append(node('p','metric-caption',a.effort ? `Reasoning ${a.effort}` : 'Reasoning effort unknown'));
    const modelDetails=stack();modelDetails.append(node('p','',raw||'Model not recorded'),node('p','muted','Model from the turn record. The hidden version is unknown; explicit reroutes appear in events.'));
    model.append(fold('model','Identifier',modelDetails));

    const compact=card('compaction','Compactions');
    const allComps=a.compactions||[],comps=allComps.filter(c=>c.mode!=='reset'),last=comps.at(-1);
    const sinceReset=allComps.at(-1)?.mode==='reset';
    const compactMain=node('div','metric-pair');
    compactMain.append(value('After compaction',short(last?.after_input),last?.after_limit>0 && finite(last?.after_input) ? `${Math.round(100*last.after_input/last.after_limit)}% of window` : last ? 'Waiting for measurement' : 'No compactions yet'));
    compactMain.append(value(sinceReset?'Requests before reset':'Requests since',last?count(last.responses_after):'—',`Total compactions ${count(a.compaction_count)}`));
    compact.append(compactMain);
    const measured=comps.filter(c=>finite(c.after_input));
    const trend=chart(measured.map(c=>({input:c.after_input,segment:0})));
    trend.querySelector('svg')?.setAttribute('aria-label','Context after compactions, oldest to newest');
    compact.append(trend);
    if(finite(last?.after_delta))compact.append(node('p','metric-caption',`Since previous compaction: ${last.after_delta>0?'+':''}${short(last.after_delta)} tokens`));
    const recent=comps.slice(-3);
    const growing=recent.length===3 && recent.every(c=>finite(c.after_input)) && recent.slice(1).every(c=>finite(c.after_delta)&&c.after_delta>0);
    const high=last?.after_limit>0 && last.after_input/last.after_limit>=0.7;
    compact.append(node('p','metric-caption',sinceReset ? 'Compaction data is from before the reset' : !last ? 'No data to assess' : !finite(last.after_input) ? 'No measurement since the last compaction' : high ? 'Little space left after compaction; consider a new chat' : growing ? 'Remaining context grew over three compactions; review history' : 'Context size gives no clear reason to switch chats'));
    const compactDetails=stack();
    const table=node('table','compaction-table'),head=node('tr');
    for(const title of ['Compaction','After','Δ','Requests until next'])head.append(node('th','',title));
    const thead=node('thead');thead.append(head);table.append(thead);
    const tbody=node('tbody');
    for(const c of comps.slice().reverse()){
      const row=node('tr');
      for(const label of [`#${c.number} ${date(c.time)}`,`${short(c.after_input)}${c.after_limit>0&&finite(c.after_input)?' '+Math.round(100*c.after_input/c.after_limit)+'%':''}`,finite(c.after_delta)?(c.after_delta>0?'+':'')+short(c.after_delta):'—',`${count(c.responses_after)}${c.interval_complete?'':' so far'}`])row.append(node('td','',label));
      tbody.append(row);
    }
    table.append(tbody);const scroll=node('div','compaction-table-scroll');scroll.append(table);compactDetails.append(scroll);
    compactDetails.append(node('p','muted','After is the input of the first recorded request after compaction, including instructions and new data, not the summary size. Δ compares consecutive compactions only with the same model and limit. A missing measurement is not zero.'));
    compactDetails.append(node('p','muted','Requests are recorded model calls, not user messages. The latest interval is still open. The chart shows available measurements from zero, oldest on the left.'));
    compactDetails.append(node('p','muted','This hint is an inspector heuristic: at least 70% of the window remains used, or usage grows over three compactions. It is not an OpenAI rule or a measure of retained decisions. Model, instruction and report size changes affect the result.'));
    compact.append(fold('compactions','Compaction history',compactDetails));
    if(a.malformed_records)compact.append(node('p','error',`Skipped malformed records: ${count(a.malformed_records)}`));
    root.replaceChildren(context,spend,model,compact);
    for(const d of root.querySelectorAll('details'))d.open=open.has(d.dataset.fold);
    if(focus)root.querySelector(`details[data-fold="${focus}"] > summary`)?.focus({preventScroll:true});
  }
  return {render};
};
