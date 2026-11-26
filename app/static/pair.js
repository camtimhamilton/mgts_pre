const $ = id => document.getElementById(id);
const names = {mentor:"Наставник",intern:"Стажёр",pair:"Пара",hr:"HR",comment:"Комментарий о работе за период",attention:"Ситуации для внимания",difficulties:"Сложности за период",barriers:"Что мешает работать и развиваться"};
const indicesNames={x:"По оценкам наставника",y:"По ответам стажёра",z:"Риск пары"};
const bands = {green:"Низкий",yellow:"Внимание",red:"Высокий",gray:"Мало данных"};
const states = {missing:"Анкета отсутствует",future:"Будущий период",insufficient_data:"Недостаточное покрытие",partial:"Частичный анализ",completed:"Анкета получена"};
const modes = {GLOBAL:"Общая аналитика",PAIR_MONTH:"Пара за месяц",PAIR_RANGE:"Пара за период"};
const percent = n => n == null ? "—" : Math.round(n*100)+"%";
const number = n => n == null ? "—" : new Intl.NumberFormat("ru-RU",{maximumFractionDigits:2}).format(n);
const stamp = s => s ? new Date(s).toLocaleString("ru-RU",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "—";
const globalScope = {type:"GLOBAL"};
function element(tag, text, cls) {
  const el=document.createElement(tag);
  if(text!=null) el.textContent=text;
  if(cls) el.className=cls;
  return el;
}
function button(title,handler,cls) {
  const b=element("button",title,cls); b.type="button"; b.addEventListener("click",handler);return b;
}
function disclosure(label,...children) {
  const d=element("details");d.append(element("summary",label),...children);return d;
}
function table(headers,rows) {
  const wrapper=element("div",null,"data-wrap"), t=element("table",null,"data"), h=t.createTHead().insertRow();
  headers.forEach(v=>h.append(element("th",v)));const body=t.createTBody();
  for(const row of rows){const tr=body.insertRow();for(const value of row){
    const td=tr.insertCell();if(value instanceof Node)td.append(value);else td.textContent=value??"—";
  }}wrapper.append(t);return wrapper;
}
function notice(text,error=false){$("message").replaceChildren(...(text?[element("div",text,error?"notice error":"notice")]:[]));}
function conn(text,state){$("connection").textContent=text;$("connection").dataset.state=state;}
async function api(path,body,signal){
  const response=await fetch("/api/v2/"+path,{method:body===undefined?"GET":"POST",
    headers:body===undefined?{}:{"Content-Type":"application/json"},body:JSON.stringify(body),signal});
  if(!response.ok){const e=new Error(response.status===409?"Исходные данные изменились. Восстановите актуальный сценарий.":"Сервис аналитики недоступен.");e.status=response.status;throw e;}
  return response.json();
}
function readDraft(){try{return JSON.parse(sessionStorage.getItem("pulse-pair-session"))||{};}catch{return {};}}
const draft=readDraft();
let selected=draft.selected??[], scope=draft.scope??globalScope, mode=scope.type, base=draft.base, method=draft.method;
let scenarioId=draft.scenarioId??null,scenarioName=draft.scenarioName??"Рабочий сценарий";
let savedBaseId=draft.savedBaseId??null;
let previewFact=false;
let data=null,anchor=null,seq=0,controller=null,busy=false,revision=null,dialogSeq=0,recalculating=false;
function remember(){sessionStorage.setItem("pulse-pair-session",JSON.stringify({selected,scope,base,method,scenarioId,scenarioName,savedBaseId}));}
function same(a,b){return a.type===b.type&&a.pair_id==b.pair_id&&a.start==b.start&&a.end==b.end;}
function selectedHere(r){return selected.some(s=>s.action_id===r.action_id && same(s.scope,r.scope));}
function belongs(row,month){return scope.type==="GLOBAL"||(row.pair_id===scope.pair_id&&month>=scope.start&&month<=scope.end);}
function title(s){if(s.type==="GLOBAL")return modes.GLOBAL;const p=data?.matrix.find(p=>p.pair_id===s.pair_id);
  return (p?p.mentor_id+" — "+p.intern_id:s.pair_id)+` (М${s.start}${s.start===s.end?"":"–М"+s.end})`;}
function dialog(titleText,...children){dialogSeq++;$("dialog-title").textContent=titleText;$("dialog-body").replaceChildren(...children);if(!$("dialog").open)$("dialog").showModal();}
$("close").onclick=()=>$("dialog").close();$("dialog").onclose=()=>dialogSeq++;
function swatch(left,right){const span=element("span",null,"swatch");span.append(element("i",null,"half "+left));if(right)span.append(element("i",null,"half "+right));span.title=[bands[left],bands[right]].filter(Boolean).join(" / ");return span;}
function analysisLabel(sides){
  if(!sides.length)return "Данных нет";
  if(sides.some(s=>s.text_analysis_status==="error"))return "Ошибка AI-анализа · доступны оценки";
  const ai=sides.filter(s=>s.text_mode==="deepseek"&&s.text_analysis_status==="completed").length;
  if(ai===sides.length)return "Полный AI-анализ";
  if(ai)return `Частичный AI-анализ · ${ai} из ${sides.length} анкет`;
  return "Предварительный расчёт по оценкам";
}
async function refresh(){
  const id=++seq;controller?.abort();controller=new AbortController();conn("Анализ выполняется…","loading");
  try{
    const result=await api("analytics",{scope,selected:previewFact?[]:selected,expected_data_version:selected.length?base:undefined,expected_methodology_version:selected.length?method:undefined},controller.signal);
    if(seq!==id)return;
    if(selected.length&&method&&result.methodology_cache_version!==method){await invalidate();return;}
    data=result;base=result.data_version;method=result.methodology_cache_version;remember();render();conn("Данные загружены","ready");
  }catch(e){
    if(id!==seq||e.name==="AbortError")return;
    if(e.status===409&&selected.length){
      const previous=scenarioId||savedBaseId;
      if(previous){
        try{
          const saved=await api("scenarios/"+encodeURIComponent(previous)+"/restore",{},controller.signal);
          if(id!==seq)return;
          if(saved.data_version!==base && saved.state.selected.length===selected.length &&
             selected.every(a=>saved.state.selected.some(b=>a.action_id===b.action_id&&same(a.scope,b.scope)))){
            base=saved.data_version;remember();await refresh();return;
          }
        }catch{if(id!==seq)return;}
      }
      await invalidate();return;
    }
    data=null;$("sections").replaceChildren();$("metrics").replaceChildren();$("matrix").replaceChildren();
    $("summary-extra").replaceChildren();$("refresh-time").textContent="";$("scope-title").textContent=modes[mode];
    $("view-label").textContent="Ошибка анализа";
    conn("Нет связи с аналитикой","error");notice("Не удалось получить актуальные индексы. Проверьте, запущен ли сервис аналитики.",true);
    $("message").append(button("Повторить",()=>{notice("");refresh();}));
  }
}
async function invalidate(){selected=[];scenarioId=null;savedBaseId=null;previewFact=false;remember();await refresh();notice("Анкеты или методология обновились. Предыдущий сценарий доступен в истории. Выберите мероприятия по актуальным данным.");}
function setMode(next){if(busy||recalculating)return;mode=next;anchor=null;
  if(next==="GLOBAL")scope=globalScope;
  else {const row=data?.matrix.find(x=>x.pair_id===scope.pair_id)??data?.matrix[0];if(!row){mode="GLOBAL";return;}
    const month=scope.start??1;scope={type:next,pair_id:row.pair_id,start:month,end:month};}
  notice("");remember();refresh();
}
document.querySelectorAll("[data-mode]").forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
function pick(row,month){if(mode==="GLOBAL"||busy||recalculating)return;
  if(mode==="PAIR_RANGE"&&anchor&&anchor.pair_id===row.pair_id){scope={type:mode,pair_id:row.pair_id,start:Math.min(anchor.month,month),end:Math.max(anchor.month,month)};anchor=null;}
  else{scope={type:mode,pair_id:row.pair_id,start:month,end:month};anchor=mode==="PAIR_RANGE"?{pair_id:row.pair_id,month}:null;}
  notice("");remember();refresh();
}
function matrix(){
  const t=$("matrix");t.replaceChildren();const head=t.createTHead().insertRow();
  for(const h of ["Пара", "М1","М2","М3","М4","М5","М6"])head.append(element("th",h));
  const body=t.createTBody();
  for(const row of [...data.matrix].sort((a,b)=>a.intern_id.localeCompare(b.intern_id,"ru",{numeric:true}))){const tr=body.insertRow(), th=element("th",row.mentor_id+" · "+row.intern_id);th.scope="row";th.append(element("small",row.department));tr.append(th);
    for(const m of row.months){const cell=button("",()=>pick(row,m.month),"cell");
      cell.dataset.pair=row.pair_id;cell.dataset.month=m.month;cell.dataset.active=mode!=="GLOBAL"&&belongs(row,m.month);
      cell.disabled=mode==="GLOBAL";cell.title=`${row.mentor_id} — ${row.intern_id}, М${m.month}. `+
        [[indicesNames.x,m.x],[indicesNames.y,m.y]].map(([n,s])=>`${n} ${number(s.score)} балл. · ${s.source_id?analysisLabel([s]):"Анкета отсутствует"}`).join(". ");
      const attention=[m.x,m.y].some(s=>s.critical_flags.length||s.factors.some(f=>f.severity===1));
      if(attention){cell.dataset.attention="true";cell.title+=" · Есть сигнал внимания";}
      cell.setAttribute("aria-label",cell.title);cell.append(element("i",null,"half "+m.x.band),element("i",null,"half "+m.y.band));tr.insertCell().append(cell);
    }
  }
}
function quoteLabel(q){return q.kind==="rating"?"Оценка "+q.value+"/5":q.quote;}
async function openSource(q){
  dialog("Исходная анкета",element("p","Загрузка…"));const id=dialogSeq;
  try{
    const source=await api("source/"+encodeURIComponent(q.source_id));if(id!==dialogSeq)return;
    const r=source.record;const wrap=element("div");
    const labels=Object.fromEntries(data.dynamics.filter(d=>d.role===r.role).map(d=>[d.factor_id,d.name]));
    for(const [k,v] of Object.entries({...r.ratings,...r.texts})){
      const div=element("div",null,"answer");div.append(element("strong",labels[k]??names[k]??k));const p=element("p");
      if(k===q.question_id){const str=String(v), quote=q.quote??str, pos=str.indexOf(quote);
        if(pos>=0)p.append(str.slice(0,pos),element("mark",quote),str.slice(pos+quote.length));else p.textContent=str;
      }else p.textContent=v;div.append(p);wrap.append(div);
    }
    dialog(names[r.role]+" · "+r.intern_id+" · М"+r.month,element("p",r.department+" · версия анкеты "+r.version,"muted"),wrap);
  }catch{if(id===dialogSeq)dialog("Исходная анкета",element("p","Источник временно недоступен."));}
}
function sourceButton(q){const value=quoteLabel(q)??"Ответ";const short=value.length>105?value.slice(0,103)+"…":value;
  const b=button(`${q.kind==="rating"?short:"«"+short+"»"} · ${names[q.role]} · М${q.month}`,()=>openSource(q),"quote");b.title=value;return b;}
function showIndex(key){
  const i=data.indices,section=data.sections[{x:"mentor",y:"intern",z:"pair"}[key]],wrap=element("div");
  wrap.append(element("p",`Фактически ${number(i.actual[key])} · в сценарии ${number(i.scenario[key])} · ${data.methodology.version}`));
  wrap.append(element("p",`Пригодных парных наблюдений ${i.eligible_count} из ${i.observation_count}. Для средних X, Y и Z используется один набор наблюдений.`, "muted spaced"));
  if(key==="z")wrap.append(element("h3",`Z = (${number(i.x)} + ${number(i.y)}) / 2 = ${number(i.z)}`));
  else wrap.append(element("h3",key.toUpperCase()+" = сумма вкладов факторов"),element("p","Вклад фактора = вес × максимальная подтверждённая выраженность.","muted"));
  if(key!=="z" && i.eligible_count>1) {
    const contributions=i.factor_contributions[key];
    wrap.append(table(["Фактор","Средний вклад","После мероприятий"],contributions.map(f=>[f.name,number(f.contribution),number(f.scenario_contribution)])),
      element("strong","Сумма средних вкладов · "+number(i[key])));
  }
  for(const row of data.matrix) for(const cell of row.months){if(!belongs(row,cell.month))continue;
    const label=`${row.mentor_id} — ${row.intern_id}, М${cell.month}`;
    if(key==="z"){const details=disclosure(`${label} · X ${number(cell.x.score)} · Y ${number(cell.y.score)} · Z ${number(cell.z)}`);
      for(const side of ["x","y"])for(const f of cell[side].factors.filter(f=>f.contribution>0)){
        details.append(disclosure(f.name+" · "+number(f.contribution),element("p",`Вес ${f.weight} · ${f.rule_id}. ${f.activation_rule}`),...f.evidence.map(sourceButton)));
      }
      wrap.append(details);continue;}
    const s=cell[key], rows=s.factors.map(f=>[f.name,f.rating??"—",f.weight,number(f.severity),number(f.contribution),number(f.scenario_contribution)]);
    const details=disclosure(label+" · "+number(s.score),element("p",`Покрытие ${percent(s.coverage)} · ${states[s.status]??s.status}`),table(["Фактор","1–5","Вес","Степень","Факт","Сценарий"],rows));
    for(const f of s.factors){const d=disclosure(f.name,element("p",f.activation_rule),element("p",`${f.rule_id} · ${f.source} · ${f.rule_status} · ${f.validation_status}`,"muted"));f.evidence.forEach(q=>d.append(sourceButton(q)));details.append(d);}
    if(data.indices.observation_count===1)details.open=true;wrap.append(details);
  }
  if(selected.length&&!previewFact)wrap.append(element("h3","Выбранные мероприятия"),...selected.map(x=>element("p",(data.recommendations.find(r=>r.action_id===x.action_id)?.title??x.action_id)+" · "+title(x.scope))));
  dialog(section.title+" · расчёт индекса",wrap);
}
function dyn(key){const series=data.dynamics.filter(d=>d.role===key);const rows=series.map(d=>[element("span",d.name),...(d.values.map((v,index)=>{
  const change=d.changes.find(c=>c.month===index+1);const t=element("span",number(v)+(change?(change.direction==="improvement"?" ↑":" ↓"):""),change?"change":"");
  t.title=d.pair_id;return t;}))]);if(scope.type==="GLOBAL")series.forEach((d,i)=>rows[i][0].append(element("small"," · "+(data.matrix.find(p=>p.pair_id===d.pair_id)?.intern_id??d.pair_id))));
  return disclosure("Динамика исходных оценок · шкала 1–5",table(["Показатель","М1","М2","М3","М4","М5","М6"],rows));
}
function effect(d){return ["x","y","z"].map(k=>k.toUpperCase()+" "+number(d[k])).join(" · ");}
async function changeChoices(next,name=scenarioName,snapshot=false){
  if(busy||recalculating||!data)return false;busy=true;conn("Сохранение плана…","loading");
  document.querySelectorAll(".rec input").forEach(c=>c.disabled=true);
  try{const result=await api("scenarios",{scenario_id:snapshot?null:scenarioId,name:snapshot?name:"Рабочий сценарий",scope,selected:next,expected_data_version:base, ...(method?{expected_methodology_version:method}:{})});
    if(!snapshot)scenarioId=result.scenario_id;scenarioName=name;selected=next;previewFact=false;remember();notice("");await refresh();return true;
  }catch(e){notice(e.message,true);if(e.status===409)await invalidate();else if(data){render();conn("Сбой сохранения","error");}return false;}finally{busy=false;}
}
function section(key){
  const s=data.sections[key],i=data.indices,k={mentor:"x",intern:"y",pair:"z"}[key];
  const box=element("article",null,"section");box.dataset.section=key;
  const details=disclosure("Подробнее",element("p",s.characteristic),element("p","Покрытие "+percent(s.coverage),"muted"));
  const active=data.summary.top_factors.filter(f=>key==="pair"||f.role===key);
  const summary=key==="pair"
    ? `Расхождения оценок: ${data.discrepancies.length} ${data.discrepancies.length===1?"случай":"случ."}.`
    : active.length ? "Темы для уточнения по ответам." : s.evidence.length?"По этим ответам нет отдельных сигналов внимания.":"Ответов пока нет.";
  box.append(element("h3",key==="pair"?"Взаимодействие пары":indicesNames[k]),element("p",summary,"characteristic"));
  if(active.length&&key!=="pair"){
    const tags=element("ul",null,"factor-tags");
    for(const f of active.slice(0,3))tags.append(element("li",f.name));
    box.append(tags);
  }
  if(s.history_summary?.periods_available>1){
    const t=s.history_summary;
    if(t.trend.warning)details.append(element("p",t.trend.warning,"muted"));
  }
  const texts=s.evidence.filter(q=>q.quote);if(texts.length){
    const qlist=element("div",null,"source-list");texts.forEach(q=>qlist.append(sourceButton(q)));
    details.append(disclosure(`Цитаты из анкет (${texts.length})`,qlist));
  }
  if(s.claims.length){const cl=element("div",null,"source-list"),refs=new Map(s.evidence.map(q=>[q.id,q]));for(const c of s.claims){const p=element("div",c.text);for(const ref of c.evidence_refs||[])if(refs.has(ref))p.append(" ",sourceButton(refs.get(ref)));cl.append(p);}details.append(disclosure("Основания характеристики",cl));}
  if(s.recommendations.length){
    const list=element("ul",null,"recs"), discuss=element("ul",null,"recs");
    const ordered=[...s.recommendations].sort((a,b)=>Number(selectedHere(b))-Number(selectedHere(a)) || (b.delta[k]??0)-(a.delta[k]??0));
    for(const r of ordered){const actionable=r.assumption!=="no_score_change"&&(selectedHere(r)||r.delta[k]>0||r.additional_delta[k]>0);
      const li=element("li"),l=element(actionable?"label":"div",null,"rec"),chk=document.createElement("input");
      chk.type="checkbox";chk.checked=selectedHere(r)&&!previewFact;chk.disabled=previewFact;chk.dataset.action=r.action_id;
      chk.onchange=()=>{const next=selected.filter(a=>!(a.action_id===r.action_id&&same(a.scope,r.scope)));if(chk.checked)next.push({action_id:r.action_id,scope:r.scope});changeChoices(next);};
      if(actionable)l.append(chk);
      l.append(element("span",r.title));if(actionable&&r.delta[k]>0)l.append(element("span","−"+number(r.delta[k])+" балл.","effect"));
      li.append(l);(actionable?list:discuss).append(li);
      details.append(disclosure(r.title,element("p",r.rationale),element("p","Ответственный — "+r.responsible.split(" · ").map(role=>names[role]??role).join(", ")),element("p",r.completion_criteria+" · "+r.resolution_criteria),element("p","Потенциал: "+effect(r.delta)+". Дополнительно: "+effect(r.additional_delta),"rec-meta")));
    }
    const count=list.children.length;
    if(count)box.append(element("h4","Что можно улучшить"),element("p","Выберите мероприятия, чтобы оценить возможное снижение риска при устранении выявленных проблем.","subtle"),list);
    if(count>3){const rest=element("ul",null,"recs");while(list.children.length>3)rest.prepend(list.lastElementChild);box.append(disclosure("Все рекомендации ("+count+")",rest));}
    if(discuss.children.length)box.append(disclosure("Что обсудить",discuss));
  }
  if(key!=="pair")details.append(dyn(key));
  else if(data.discrepancies.length)details.append(disclosure("Расхождения оценок",...data.discrepancies.map(d=>element("p",`М${d.month} · ${data.dynamics.find(f=>f.factor_id===d.aspect)?.name??"Ясность задач"} · наставник ${d.mentor_rating}/5, стажёр ${d.intern_rating}/5. ${d.explanation}`))));
  if(key==="pair" && data.semantic_comparisons?.length) {
    const types={AGREEMENT:"Согласие",CONTRADICTION:"Противоречие",AWARENESS_GAP:"Требуется уточнение осведомлённости",COMPLEMENTARY:"Дополняющие наблюдения",NOT_COMPARABLE:"Нет сопоставимых оснований"};
    const items=data.semantic_comparisons.filter(c=>c.evidence.length);
    if(items.length){const entries=element("div",null,"source-list");
      for (const c of items){const p=element("p","М"+c.month+" · "+types[c.type]+". "+c.explanation);for(const q of c.evidence)p.append(sourceButton(q));entries.append(p);}
      details.append(disclosure("Сопоставление текстов",entries));}
  }
  if(key!=="pair"){
    const sides=data.matrix.flatMap(row=>row.months.filter(m=>belongs(row,m.month)).map(m=>m[k]));
    const entries=element("div",null,"source-list");
    for(const side of sides)for(const v of side.signals??[]){
      const factor=side.factors.find(f=>f.id===v.factor_id);
      const neural=v.method==="local_neural_topic_matching";
      const p=element("p",(factor?.name??v.factor_id)+" · "+(neural?"Гипотеза локальной нейромодели, без баллов":({accepted:"Принято правилом",review:"Требует уточнения",rejected:"Отклонено"}[v.validation_status]??v.validation_status)));
      if(v.validation_status!=="rejected")p.append(" ",sourceButton(v));entries.append(p);
    }
    if(entries.childElementCount)details.append(disclosure("Интерпретация комментариев",entries));
  }
  box.append(details);
  return box;
}
function trend(){
  const row=data.matrix.find(r=>r.pair_id===scope.pair_id);
  const entries=scope.type==="GLOBAL"?data.index_dynamics:row?.months.filter(m=>
    scope.type==="PAIR_MONTH"||m.month>=scope.start&&m.month<=scope.end).map(m=>({month:m.month,...m.scenario}))??[];
  const chart=element("div",null,"trend");
  chart.append(element("h3",scope.type==="PAIR_MONTH"?"История пары":"Динамика риска"));
  const graph=element("div",null,"trend-grid");
  const max=Math.max(0,...entries.flatMap(m=>[m.x??0,m.y??0,m.z??0]));
  const scale=Math.max(10,Math.ceil(max/10)*10);
  for(const m of entries){
    const date="М"+m.month, bars=element("div",null,"trend-month");
    bars.dataset.active=String(scope.type==="PAIR_MONTH"&&scope.start===m.month);
    const label=date+" · "+["x","y","z"].map(k=>indicesNames[k]+" "+number(m[k])+" балл.").join(" · ");bars.setAttribute("role","img");bars.setAttribute("aria-label",label);bars.title=label;
    const plot=element("div",null,"trend-bars");
    for(const k of ["x","y","z"]){
      const bar=element("i",null,"trend-"+k+(m[k]==null?" unavailable":""));
      bar.style.setProperty("--value",((m[k]??0)*100/scale)+"%");plot.append(bar);
    }
    bars.append(plot,element("small",date));graph.append(bars);
  }
  const legend=element("div",null,"chart-legend");
  for(const k of ["x","y","z"]){const item=element("span");item.append(element("i",null,"trend-"+k),element("small",indicesNames[k]));legend.append(item);}
  legend.append(element("small",`Шкала 0–${scale} баллов`));chart.append(graph,legend);
  return chart;
}
function render(){
  document.querySelectorAll("[data-mode]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.mode===mode)));
  $("hint").textContent=mode==="GLOBAL"?"Все пары за шесть контрольных месяцев.":mode==="PAIR_MONTH"?"Выберите ячейку пары и месяца.":anchor?"Выберите конец диапазона в этой же строке.":"Выберите первый и последний месяцы одной пары.";
  matrix();const i=data.indices;$("scope-title").textContent=title(scope);
  const cells=data.matrix.flatMap(r=>r.months.filter(m=>belongs(r,m.month)));
  const present=cells.flatMap(m=>[m.x,m.y]).filter(s=>s.source_id);
  $("refresh-time").textContent=data.updated_at?"Анализ от "+stamp(data.updated_at):"";
  $("view-label").textContent=analysisLabel(present);
  $("compare").hidden=!selected.length;$("compare").textContent=previewFact?"Показать план улучшений":"Показать текущие баллы";
  $("reset").hidden=!selected.length;
  const textMode=data.matrix.flatMap(r=>r.months).flatMap(m=>[m.x,m.y]).find(s=>s.nlp_audit?.mode)?.nlp_audit.mode;
  $("methodology").textContent=`Методика ${data.methodology.version} · порог покрытия ${percent(data.methodology.min_coverage)}. Зона внимания от ${data.methodology.thresholds.yellow}, высокая от ${data.methodology.thresholds.red} баллов. Тексты: ${({local_encoder:"локальная нейромодель и правила",local_rules:"локальные правила",deepseek:"DeepSeek"}[textMode]??"нет наблюдений")}.`;
  const cards=["z","x","y"];
  $("metrics").replaceChildren(...cards.map(k=>{
    const b=button("",()=>showIndex(k),"metric");b.dataset.index=k;
    const value=element("div",null,"metric-value");
    value.append(swatch(k==="y"?i.bands.y:i.bands.x,k==="z"?i.bands.y:null),element("b",number(i[k])));
    b.append(element("span",indicesNames[k]),value);
    if(selected.length&&!previewFact&&i.scenario[k]!=null&&i.actual[k]!=null){
      b.append(element("small","После улучшений, условно"),
        element("small",`Сейчас ${number(i.actual[k])} · снижение ${number(i.actual[k]-i.scenario[k])} балл.`));
    }else b.append(element("small","Баллы из 100"));
    return b;
  }));
  const stats=element("div"),counter=element("div",null,"counters"),distribution=element("div",null,"distribution");
  const both=cells.filter(m=>m.x.source_id&&m.y.source_id).length;
  const attention=cells.filter(m=>[m.x,m.y].some(side=>side.actual_score>=30||side.critical_flags.length)).length;
  for(const [label,value] of [["Анкеты",`${i.received_surveys}/${i.expected_surveys}`],["Месяцы с обеими анкетами",`${both}/${i.observation_count}`],["Наблюдения от 30 баллов или с критическим сигналом",attention],["Критические случаи",data.summary.critical_cases.length]]){
    const item=element("span");item.append(element("b",value),element("small",label));counter.append(item);
  }
  for(const [color,value] of Object.entries(i.risk_distribution)){
    const item=element("span");item.append(swatch(color),element("span",bands[color]+" "+value));distribution.append(item);
  }
  stats.append(table(["Месяц","Наставник, баллы","Стажёр, баллы","Риск пары, баллы","Пар в расчёте"],data.index_dynamics.map(m=>["М"+m.month,number(m.x),number(m.y),number(m.z),m.eligible_count])));
  const top=data.summary.top_factors.slice(0,5);
  if(top.length)stats.append(element("strong","Что требует внимания"),...top.map(f=>element("p",names[f.role]+" · "+f.name+" · "+f.observation_count+" набл.")));
  const list=element("div",null,"source-list");
  for(const p of data.patterns)list.append(element("p",`${data.matrix.find(r=>r.pair_id===p.pair_id)?.intern_id??""} · ${names[p.role]??""} · М${(p.months??[p.month]).join(", М")} · ${data.dynamics.find(d=>d.factor_id===p.factor_id&&d.role===p.role)?.name??p.factor_id}. ${p.explanation}`));
  stats.append(list);for(const st of ["mentor","intern"]){const a=i.side_statistics[st];stats.append(element("p",`${names[st]} · по всем пригодным анкетам ${number(a.mean)} (${a.count})`));}
  $("summary-extra").replaceChildren(counter,trend(),...(scope.type==="GLOBAL"?[distribution]:[]),disclosure("Подробная динамика",stats));
  $("sections").replaceChildren(...["pair","mentor","intern"].map(section));
  if(data.summary.critical_cases.length)$("summary-extra").prepend(element("p","Требуется конфиденциальное уточнение · "+data.summary.critical_cases.map(c=>(data.matrix.find(p=>p.pair_id===c.pair_id)?.intern_id??c.pair_id)+" М"+c.month).join(", "),"flag"));
  if(!data.matrix.length)notice("История пока пуста. Загрузите обе книги командой импорта из README.");
  else if(present.some(s=>s.text_analysis_status==="error"))notice("Текстовый анализ завершился с ошибкой. Откройте исходные ответы или повторите пересчёт.",true);
  else notice("");
}
async function scenarios(){
  dialog("План улучшений",element("p","Загрузка…"));const id=dialogSeq;
  try{const items=await api("scenarios");if(id!==dialogSeq)return;
    const pane=element("div"),actions=element("div",null,"actions"),input=element("input");input.value=scenarioName;input.maxLength=140;input.setAttribute("aria-label","Название сценария");
    const action=(label,fn)=>button(label,async()=>{try{await fn();}catch(e){notice(e.message,true);$("dialog").close();}});
    actions.append(input,action("Сохранить",async()=>{if(await changeChoices(selected,input.value.trim()||"Сохранённый сценарий",true))scenarios();}),
      action("Сбросить область",async()=>{await changeChoices(selected.filter(a=>!same(a.scope,scope)));$("dialog").close();}),
      action("Сбросить весь сценарий",async()=>{await changeChoices([]);$("dialog").close();}));
    pane.append(element("p",`Отмечено мероприятий: ${selected.length}. Изменения моделируют полное устранение факторов, включая исторические периоды.`),actions);
    const list=element("div",null,"saved");
    for(const item of items){const row=element("div",null,"actions");const restore=action("Восстановить",async()=>{
      const v=await api("scenarios/"+encodeURIComponent(item.scenario_id)+"/restore",{});selected=v.state.selected;scope=v.state.scope;mode=scope.type;scenarioId=null;savedBaseId=v.scenario_id;scenarioName=v.name;base=v.data_version;method=v.result.methodology_cache_version;anchor=null;remember();$("dialog").close();await refresh();});
      const stale=item.stale||(method&&item.methodology_cache_version!==method);
      restore.disabled=!!stale;row.append(element("span",item.name+" · "+stamp(item.updated_at)+(stale?" · устарел":"")),restore,action("История",async()=>{
        const v=await api("scenarios/"+encodeURIComponent(item.scenario_id));const labels={created:"Создан",updated:"Сохранён",selected:"Выбор",removed:"Снятие",restored:"Восстановлен",invalidated:"Новые данные"};
        dialog("История · "+v.name,table(["Время","Событие","Мероприятие"],v.history.map(h=>[stamp(h.created_at),labels[h.kind]??h.kind,h.state.selection?.action_id??"—"])));}));list.append(row);}
    pane.append(list);dialog("План улучшений",pane);
  }catch{if(id===dialogSeq)dialog("План улучшений",element("p","История сохранённых планов недоступна."));}
}
$("manage").onclick=scenarios;
$("compare").onclick=()=>{if(!busy){previewFact=!previewFact;refresh();}};
$("reset").onclick=()=>changeChoices([]);
const formErrors={
  wrong_form_id:"ID формы не соответствует настройкам.",
  intern_pair_not_registered:"Сначала нужна анкета наставника или запись о паре.",
  delivery_id_content_conflict:"Ответ изменился без нового номера редакции.",
  response_id_required:"Не передан ID ответа.",
  invalid_rpc_method:"Укажите метод submit.",
  invalid_or_conflicting_survey:"Проверьте параметры анкеты и соответствие пары.",
};
function updateStatus(text){$("update-result").textContent=text;$("update-result").hidden=false;}
async function formStatus(showDetails=false){
  const result=await api("forms/status");
  if(showDetails){
    const rows=result.forms.map(f=>{
      const cacheKey="pulse-yandex-seen-"+f.role;
      const seen=Number(localStorage.getItem(cacheKey)||0);
      localStorage.setItem(cacheKey,String(f.accepted));
      return [names[f.role],f.last_error?formErrors[f.last_error] :
        f.last_success?"Ответ получен":f.configured?"Ожидаем первый ответ":"ID формы не задан",
        stamp(f.last_success),Math.max(0,f.accepted-seen),f.accepted];
    });
    dialog("Яндекс Формы",element("p","Новые анкеты поступают автоматически. Обновление показывает принятые ответы и проверяет доставку."),
      table(["Анкета","Статус","Последний приём","Новых с прошлого просмотра","Всего принято"],rows));
  }
  $("update").title=result.forms.map(f=>names[f.role]+": "+(f.last_error?"ошибка":f.last_success?"ответ получен":"ожидаем доставку")).join(" · ");
}
$("update").onclick=async()=>{
  if(busy||recalculating)return;
  $("update").disabled=true;
  try{await refresh();await formStatus(true);}
  catch{updateStatus("Не удалось проверить доставку из Яндекс Форм.");}
  finally{$("update").disabled=false;}
};
$("recalculate").onclick=async()=>{
  if(busy||recalculating)return;
  recalculating=true;$("recalculate").disabled=true;controller?.abort();
  conn("Анализ выполняется…","loading");$("view-label").textContent="Анализ выполняется";
  updateStatus("Пересчитываем сохранённые анкеты…");
  const progress=element("progress");progress.setAttribute("aria-label","Пересчёт выполняется");
  $("update-result").append(" ",progress);
  try{
    const result=await api("recalculate",{});
    const sides=result.matrix.flatMap(r=>r.months.flatMap(m=>[m.x,m.y])).filter(s=>s.source_id);
    await refresh();
    updateStatus(`Пересчёт завершён. Анкет: ${sides.length}. ${analysisLabel(sides)}.`);
  }catch{updateStatus("Пересчёт не завершён. Проверьте сервис или попробуйте снова.");await refresh();}
  finally{recalculating=false;$("recalculate").disabled=false;}
};
async function poll(){try{const r=await api("revision");if(revision!==null&&revision!==r.revision&&!busy&&!recalculating){revision=r.revision;await refresh();await formStatus();}else if(revision===null)revision=r.revision;}catch{/* Keep the visible last status until an analysis is requested. */}}
await poll();await refresh();setInterval(poll,2500);
formStatus().catch(()=>{});
