import {heading, panel} from '../components/common/heading.js';
import {entries, records} from '../components/common/records.js';
import {date, esc} from '../utils/dom.js';
import {topic} from '../constants/project.js';
import {sourceLabels, storageModeLabels, storageVersionLabels} from '../constants/workflow.js';

function storageBrief(state) {
  const storage = state.storage;
  if (!storage) return '';
  const canSwitch = state.session.role==='operator' ? storage.can_switch_to : [];
  const switches = canSwitch.map(mode=>`<button class="secondary" data-switch="${esc(mode)}">切换到${esc(storageModeLabels[mode] || mode)}</button>`).join('');
  const migrate = state.session.role==='operator'
    ? `<button data-migrate ${storage.pending_cases?'':'class="secondary"'}>执行逐单迁移</button>` : '';
  const readiness = storage.cutover_ready
    ? '<p class="note">全部档案已逐单入库，可以切换到新端受理。</p>'
    : `<p class="note">还有 ${esc(storage.pending_cases)} 份档案、${esc(storage.pending_orders)} 张工单待迁移。</p>`;
  return `<aside class="brief storage-brief"><h2>存储切换</h2>
    <dl><dt>当前模式</dt><dd>${esc(storageModeLabels[storage.mode] || storage.mode)}</dd>
    <dt>回执以哪端为准</dt><dd>${esc(sourceLabels[storage.authoritative_source] || storage.authoritative_source)}</dd>
    <dt>正在接单</dt><dd>${esc(storage.accepting_sources.map(x=>sourceLabels[x] || x).join('、'))}</dd>
    <dt>整批 / 逐单档案</dt><dd>${esc(storage.cases_by_version.v1 || 0)} / ${esc(storage.cases_by_version.v2 || 0)}</dd>
    <dt>逐单记录</dt><dd>${esc(storage.total_orders)}</dd></dl>
    ${readiness}${state.session.role==='operator'?`<div class="actions">${migrate}${switches}</div>`:''}</aside>`;
}

export function casesPage(state, identity) {
  const {spec, session, cases, runs} = state;
  const selected = cases.find(row=>row.id===identity) || cases[0];
  const operator = session.role==='operator';
  const rows = cases.map(row=>[`<a href="#cases/${row.id}" ${row.id===selected?.id?'aria-current="true"':''}>${esc(row.title)}</a>`, esc(storageVersionLabels[row.storage_version] || row.storage_version || 'v1'), esc(row.author), esc(date(row.created_at))]);
  const detail = selected
    ? `<aside class="brief"><h2>${esc(selected.title)}</h2><p>${esc(spec.guidance)}</p><p class="note">该档案记录 ${runs.filter(row=>row.case_id===selected.id).length} 次执行。</p>${operator?`<a href="#editor/${selected.id}">复制为新档案</a>`:''}</aside>`
    : `<aside class="brief"><h2>${esc(spec.customer)}</h2></aside>`;
  const input = selected ? Object.entries(selected.payload).map(([key, value])=>{
    const label = spec.field_labels[key] || key;
    const rows = Array.isArray(value) && value.every(x=>x && typeof x === 'object' && !Array.isArray(x));
    return panel(label, rows ? entries(value.slice(0,10), spec.field_labels) + (value.length>10?`<p class="note">显示前 10 条，共 ${value.length} 条。</p>`:'') : `<pre>${esc(value)}</pre>`);
  }).join('') : '';
  return `${heading(topic.modules[0], spec.summary, operator?'<a class="button" href="#editor">建立档案</a>':'')}
    <div class="spread"><section aria-label="档案目录">${records([spec.entity_labels[0], '存储版本', '经办', '建立时间'], rows, '尚未建立档案。经办员可以从演示资料建立第一份。')}</section>
    ${detail}</div>
    ${storageBrief(state)}
    ${input ? `<div class="inspection">${input}</div>`:''}`;
}

export function bindCases(state, api, refresh, fail) {
  document.querySelectorAll('[data-switch]').forEach(button=>button.addEventListener('click', async()=>{
    button.disabled = true;
    try {
      await api('/storage/switch', {method:'POST', data:{mode:button.dataset.switch}});
      await refresh();
    } catch (error) { fail(error); button.disabled = false; }
  }));
  document.querySelector('[data-migrate]')?.addEventListener('click', async event=>{
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await api('/storage/migrate', {method:'POST'});
      await refresh();
    } catch (error) { fail(error); button.disabled = false; }
  });
}
