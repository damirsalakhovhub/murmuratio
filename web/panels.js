'use strict';
window.createInspectorPanels = ({node, count}) => {
  let signature='';
  const finite=n=>typeof n==='number' && Number.isFinite(n);
  const date=s=>s ? new Date(s).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—';
  const short=n=>{
    if(!finite(n))return '—';
    const sign=n<0?'−':'', magnitude=Math.abs(n);
    const f=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1});
    return sign+(magnitude>=1e6 ? f.format(magnitude/1e6)+' млн' : magnitude>=1e3 ? f.format(magnitude/1e3)+' тыс.' : count(magnitude));
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
    svg.setAttribute('aria-label','Входной контекст по обращениям; разрыв — сжатие');svg.classList.add('context-chart');
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

    const context=card('context','Контекст');
    const percentage=finite(u.input_tokens)&&a.context_limit>0 ? Math.round(100*u.input_tokens/a.context_limit)+'%' : '—';
    const main=node('div','context-main');
    main.append(value('',a.pending_compaction||a.pending_context?'—':percentage, a.pending_context?'Ждём замер':a.pending_compaction?'После сжатия ждём замер':`${short(u.input_tokens)} из ${short(a.context_limit)} токенов`));
    const points=(a.points||[]).filter(p=>finite(p.input));
    main.append(chart(points));context.append(main);
    context.append(node('p','metric-caption',`Последний запрос ${date(a.last_at)}`));
    const composition=stack();
    composition.append(node('p','muted','Оценка текста после сжатия. Полный сетевой запрос и зашифрованное резюме недоступны.'));
    for(const c of a.categories||[]){const row=node('div','breakdown-row');row.append(node('span','',c.name),node('span','',`≈ ${short(c.estimated_tokens)}`));composition.append(row);}
    for(const [i,b] of (a.largest_blocks||[]).entries()){
      const body=stack();body.append(node('pre','packet-text',b.preview),node('p','muted',`${count(b.characters)} символов первые 2 000 показаны${b.duplicates>1?' повторов '+b.duplicates:''}`));
      composition.append(fold('block-'+i,`${b.title} ≈ ${short(b.estimated_tokens)}`,body));
    }
    composition.append(node('p','muted','≈ символы / 4. Большой объём или повтор не доказывает бесполезность.'));
    const exact=stack();
    exact.append(node('p','muted',`Вход ${count(u.input_tokens)} из ${count(a.context_limit)} токенов. Замер прошлого обращения, не следующего запроса.`));
    exact.append(node('p','muted',`График: последние ${points.length} из ${count(a.point_count)} обращений, слева ранние; разрыв — сжатие. Вертикальная шкала от 0 до ${count(Math.max(0,...points.map(p=>p.input)))}.`));
    for(const p of points.slice().reverse())exact.append(node('p','token-count',`${date(p.time)} ${count(p.input)} вход ${count(p.output)} выход`));
    const contextDetails=node('div','metric-details');contextDetails.append(fold('composition','Состав контекста',composition),fold('history','История запросов',exact));context.append(contextDetails);

    const spend=card('spend','Расход');
    const numbers=node('div','metric-pair');numbers.append(value('Вход',short(t.input_tokens)),value('Выход',short(t.output_tokens)));spend.append(numbers);
    spend.append(node('p','metric-caption',`За весь чат ${count(a.point_count)} обращений`));
    const costs=stack();
    for(const [label,v] of [['Вход',t.input_tokens],['Из него кеш',t.cache_read_tokens],['Выход',t.output_tokens],['Из него reasoning',t.reasoning_tokens]]){
      const row=node('div','breakdown-row');row.append(node('span','',label),node('span','',count(v)));costs.append(row);
    }
    costs.append(node('p','muted','Кеш входит во вход; reasoning — в выход. Повторная отправка истории увеличивает расход, но не заполняет окно повторно. Это токены, не денежное списание.'));
    costs.append(node('p','muted',`Последнее обращение: ${count(u.input_tokens)} вход ${count(u.cache_read_tokens)} кеш ${count(u.output_tokens)} выход ${count(u.reasoning_tokens)} reasoning.`));
    for(const key of ['primary','secondary']){const v=a.rate_limits?.[key];if(v)costs.append(node('p','muted',`Снимок лимита аккаунта: ${count(v.used_percent)}% за ${count(v.window_minutes)} мин. Сброс ${date(v.resets_at*1000)}. Может устареть.`));}
    spend.append(fold('costs','Детали расхода',costs));

    const model=card('model','Модель');
    const raw=a.model||'';
    // Only format an observed public ID, never infer a version from a display name.
    const match=/^gpt-(\d+(?:\.\d+)?)-(astra|sol|terra|luna)$/.exec(raw);
    const display=match ? match[2][0].toUpperCase()+match[2].slice(1)+' '+match[1] : raw||'—';
    model.append(node('p','metric-number model-number',display));
    model.append(node('p','metric-caption',a.effort ? `Reasoning ${a.effort}` : 'Режим reasoning неизвестен'));
    const modelDetails=stack();modelDetails.append(node('p','',raw||'Модель не записана'),node('p','muted','Модель из записи хода. Скрытая версия неизвестна; явные перенаправления — в событиях.'));
    model.append(fold('model','Идентификатор',modelDetails));

    const compact=card('compaction','Сжатия');
    const allComps=a.compactions||[],comps=allComps.filter(c=>c.mode!=='reset'),last=comps.at(-1);
    const sinceReset=allComps.at(-1)?.mode==='reset';
    const compactMain=node('div','metric-pair');
    compactMain.append(value('После сжатия',short(last?.after_input),last?.after_limit>0 && finite(last?.after_input) ? `${Math.round(100*last.after_input/last.after_limit)}% окна` : last ? 'Ждём замер' : 'Сжатий ещё нет'));
    compactMain.append(value(sinceReset?'Обращений до сброса':'Обращений с тех пор',last?count(last.responses_after):'—',`Всего сжатий ${count(a.compaction_count)}`));
    compact.append(compactMain);
    const measured=comps.filter(c=>finite(c.after_input));
    const trend=chart(measured.map(c=>({input:c.after_input,segment:0})));
    trend.querySelector('svg')?.setAttribute('aria-label','Контекст после сжатий, от ранних к поздним');
    compact.append(trend);
    if(finite(last?.after_delta))compact.append(node('p','metric-caption',`К предыдущему сжатию: ${last.after_delta>0?'+':''}${short(last.after_delta)} токенов`));
    const recent=comps.slice(-3);
    const growing=recent.length===3 && recent.every(c=>finite(c.after_input)) && recent.slice(1).every(c=>finite(c.after_delta)&&c.after_delta>0);
    const high=last?.after_limit>0 && last.after_input/last.after_limit>=0.7;
    compact.append(node('p','metric-caption',sinceReset ? 'Данные сжатий относятся к периоду до сброса' : !last ? 'Нет данных для оценки' : !finite(last.after_input) ? 'После последнего сжатия пока нет замера' : high ? 'Мало свободного места после сжатия — рассмотрите новый чат' : growing ? 'Остаток растёт три сжатия подряд — проверьте историю' : 'По объёму явного сигнала для смены чата нет'));
    const compactDetails=stack();
    const table=node('table','compaction-table'),head=node('tr');
    for(const title of ['Сжатие','После','Δ','Обращения до следующего'])head.append(node('th','',title));
    const thead=node('thead');thead.append(head);table.append(thead);
    const tbody=node('tbody');
    for(const c of comps.slice().reverse()){
      const row=node('tr');
      for(const label of [`#${c.number} ${date(c.time)}`,`${short(c.after_input)}${c.after_limit>0&&finite(c.after_input)?' '+Math.round(100*c.after_input/c.after_limit)+'%':''}`,finite(c.after_delta)?(c.after_delta>0?'+':'')+short(c.after_delta):'—',`${count(c.responses_after)}${c.interval_complete?'':' пока'}`])row.append(node('td','',label));
      tbody.append(row);
    }
    table.append(tbody);const scroll=node('div','compaction-table-scroll');scroll.append(table);compactDetails.append(scroll);
    compactDetails.append(node('p','muted','После — вход первого записанного обращения после сжатия, вместе с инструкциями и новыми данными; не размер резюме. Δ сравнивает соседние сжатия только при одинаковой модели и лимите. Пропущенный замер — не ноль.'));
    compactDetails.append(node('p','muted','Обращения — записанные вызовы модели, не сообщения пользователя. Последний интервал ещё идёт. График показывает доступные замеры; шкала от нуля, слева ранние.'));
    compactDetails.append(node('p','muted','Подсказка — эвристика инспектора: остаток от 70% окна или рост три сжатия подряд. Это не правило OpenAI и не оценка сохранности решений. Смена модели, инструкций и объёма отчётов влияет на результат.'));
    compact.append(fold('compactions','История сжатий',compactDetails));
    if(a.malformed_records)compact.append(node('p','error',`Пропущено повреждённых записей: ${count(a.malformed_records)}`));
    root.replaceChildren(context,spend,model,compact);
    for(const d of root.querySelectorAll('details'))d.open=open.has(d.dataset.fold);
    if(focus)root.querySelector(`details[data-fold="${focus}"] > summary`)?.focus({preventScroll:true});
  }
  return {render};
};
