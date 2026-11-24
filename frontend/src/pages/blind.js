import {api, store} from '../api.js';
import {badge, pageTitle, sectionTitle} from '../components/ledger.js';
import {h, number, when} from '../presentation.js';

function sealForm() {
  const collectible = store.batches.filter(batch => batch.state !== 'reference');
  return `<details class="entry-form" ${store.blindOverview.some(item =>
    item.validations.some(v => v.state === 'sealed')) ? '' : 'open'}><summary>封存新一轮盲验</summary>
  <form data-action="blindSeal">
    <label>配方<select name="recipe_code" required>${store.recipes.map(recipe =>
      `<option value="${h(recipe.code)}">${h(recipe.code)} · ${h(recipe.product)}</option>`).join('')}</select></label>
    <label class="wide">见证批名单（可多选，须已冻结曲线；已认证参考批不得入选）
      <select name="witness_batches" multiple size="4" required>${collectible.map(batch =>
        `<option value="${h(batch.code)}">${h(batch.code)} · ${h(batch.recipe_code)} · ${h(batch.state)}</option>`).join('')}</select>
      <small>按住 Ctrl / ⌘ 多选；名单封存后训练参考与见证曲线同时冻结。</small></label>
    <label class="wide">封存说明<textarea name="note" rows="2" minlength="4" required></textarea></label>
    <button class="primary">封存盲验</button></form></details>`;
}

function observedForm(witness) {
  const rows = [0, 1].map(index => `<div class="pair-row">
    <input name="specimen_${index}" placeholder="试片编号 TRAY-W${index + 1}" required>
    <input name="before_mm_${index}" type="number" step="0.00001" min="0.00001" max="200" placeholder="冻干前 mm" required>
    <input name="after_mm_${index}" type="number" step="0.00001" min="0.00001" max="200" placeholder="冻干后 mm" required>
  </div>`).join('');
  return `<details class="entry-form"><summary>登记 ${h(witness.batch_code)} 的实测（预测已锁定）</summary>
  <form data-action="blindObserved" data-witness="${witness.id}">
    <div class="wide pair-grid">${rows}</div>
    <label class="wide">实测说明<textarea name="note" rows="2" minlength="4" required></textarea></label>
    <button class="primary">登记实测并核对冻结曲线</button></form></details>`;
}

function witnessTable(witnesses, role, state) {
  const staff = role !== 'viewer';
  return `<table><thead><tr><th>见证批次</th><th>冻结曲线</th><th>预测状态</th><th>预测 %</th>
    <th>实测 %</th><th>误差 pp</th><th>判定</th></tr></thead><tbody>
    ${witnesses.map(witness => {
      const fail = witness.within_tolerance === false;
      return `<tr ${fail ? 'class="row-fail"' : ''}><th class="mono">${h(witness.batch_code)}</th>
      <td class="mono">${h(witness.source_name)}<small><br>SHA-256 ${h((witness.curve_checksum || '').slice(0, 12))}…</small></td>
      <td>${badge(witness.job_state)}${witness.error ? `<br><small class="warn-text">${h(witness.error)}</small>` : ''}</td>
      <td class="mono">${witness.result ? number(witness.result.prediction_pct) : '—'}</td>
      <td class="mono">${witness.observed_pct == null ? '—' : number(witness.observed_pct)}</td>
      <td class="mono ${fail ? 'warn-text' : ''}">${witness.error_pp == null ? '—' : (witness.error_pp > 0 ? '+' : '') + number(witness.error_pp)}</td>
      <td>${witness.within_tolerance == null ? '待核对' : witness.within_tolerance ? '达标（≤2 pp）' : '超差作废'}</td></tr>`;
    }).join('')}</tbody></table>
    ${staff && state === 'sealed' ? witnesses.map(witness => {
      if (witness.job_state === 'idle' || witness.job_state === 'failed')
        return `<button class="primary compact" data-action="blindPredict" data-witness="${witness.id}">锁定 ${h(witness.batch_code)} 预测</button>`;
      if (witness.job_state === 'predicted') return observedForm(witness);
      if (witness.job_state === 'queued' || witness.job_state === 'running')
        return `<p class="empty">${h(witness.batch_code)} 预测计算中，可刷新查看。</p>`;
      return '';
    }).join('') : ''}`;
}

function validationCard(entry, role) {
  const v = entry.validation;
  const supervisor = role === 'supervisor';
  return `<section class="sheet blind-card">
    ${sectionTitle(`盲验第 ${v.sealed_index} 轮 · ${h(v.recipe_code)}`, `容差 ±${number(v.tolerance_pp, 1)} 个百分点`)}
    <div class="padded blind-head">${badge(v.state)}
      <dl class="ledger-meta"><div><dt>封存人 / 时间</dt><dd>${h(v.sealed_by)} · ${when(v.sealed_at)}</dd></div>
      <div><dt>封存说明</dt><dd>${h(v.sealed_note)}</dd></div>
      <div><dt>名单指纹</dt><dd class="mono">${h(v.roster_hash)}</dd></div>
      ${v.opened_at ? `<div><dt>开通时间 / 依据</dt><dd>${when(v.opened_at)} · ${h(v.opened_note)}</dd></div>` : ''}
      ${v.voided_at ? `<div><dt>作废时间 / 原因</dt><dd>${when(v.voided_at)} · ${h(v.voided_note)}</dd></div>` : ''}</dl></div>
    ${witnessTable(entry.witnesses, role, v.state)}
    ${v.state === 'sealed' && supervisor ? `<details class="entry-form"><summary>作废本轮盲验（作废后可重开，晚到记录不再凑数）</summary>
      <form data-action="blindVoid" data-blind="${v.id}"><label class="wide">作废原因<textarea name="note" rows="2" minlength="4" required></textarea></label>
      <button class="primary danger">确认作废</button></form></details>` : ''}
    ${v.state === 'opened' ? `<div class="opening-basis"><h3>开通依据（逐批误差）</h3>
      <ol>${(v.opening_basis?.witnesses || []).map(item => `<li class="mono">${h(item.batch)}：预测 ${number(item.prediction_pct)}% → 实测 ${number(item.observed_pct)}%，误差 ${(item.error_pp > 0 ? '+' : '') + number(item.error_pp)} pp</li>`).join('')}</ol></div>` : ''}
    </section>`;
}

export function blindPage() {
  const overview = store.blindOverview;
  const current = store.blind;
  const role = store.user.role;
  return `${pageTitle('收缩率盲验', '新配方投产前封存训练参考与见证名单：先锁定预测、后登记实测，逐批按各自冻结曲线核对，误差均不超过 2 个百分点才开放生产试算。')}
  <div class="blind-layout"><aside class="sheet roster">${sectionTitle('配方盲验索引', `${overview.length} 个配方`)}
    <div class="batch-list">${overview.map(item => {
      const active = item.validations[0];
      const state = active?.state || 'idle';
      return `<a href="${active?.id ? `#blind/${active.id}` : '#blind'}" class="batch-link" ${current?.validation.id === active?.id ? 'aria-current="true"' : ''}>
        <span><b class="mono">${h(item.recipe.code)}</b><small>${h(item.recipe.product)}</small></span>
        ${item.trial_opened ? badge('opened') : active ? badge(active.state) : '<span class="badge" data-state="idle">未盲验</span>'}</a>`;
    }).join('')}</div>
    ${role === 'supervisor' ? sealForm() : '<p class="empty">仅签发主管可封存盲验名单。</p>'}</aside>
  <div class="work-detail">${current ? validationCard(current, role) :
    `<p class="empty">从左侧选择一个配方查看盲验轮次，或由主管封存新一轮盲验。</p>`}</div></div>`;
}
