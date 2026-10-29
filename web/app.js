import { mountTour, esc } from '/viewer/tour.js';

const $ = (sel) => document.querySelector(sel);
const token = document.querySelector('meta[name=app-token]').content;
const EXAMPLES = [
  '2BHK, about 90 sqm, 30 lakh rupees, warm scandinavian, no TV in the living room',
  '3BHK 120 sqm, budget $60,000, dark academia with lots of books and brass',
  '1BHK 55 sqm, 12 lakh, japandi, calm and minimal',
];
const STEPS = ['Waiting for a GPU', 'Starting the GPU machine', 'Installing software', 'Downloading model weights',
  'Loading the model', 'Rendering rooms', 'Finishing up'];

let config = {};
let current = null;        // { manifest, summary } of the project being reviewed
let tour = null;           // handle returned by mountTour
let pollTimer = null;

// --- helpers -------------------------------------------------------------------------------------------------
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-App-Token': token }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}
const money = (n, cur) => `${cur} ${Math.round(n).toLocaleString()}`;
function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.hidden = !message;
  if (message) el.scrollIntoView({ block: 'nearest' });
}
function busy(button, on, label) {
  button.disabled = on;
  button.dataset.label ??= button.textContent;
  button.innerHTML = on ? `<span class="spin"></span> ${esc(label)}` : esc(button.dataset.label);
}

// --- create form ---------------------------------------------------------------------------------------------
function initForm() {
  $('#examples').innerHTML = EXAMPLES.map((t, i) => `<button type="button" class="example" data-i="${i}">${esc(t.slice(0, 38))}…</button>`).join('');
  $('#examples').addEventListener('click', (e) => {
    const b = e.target.closest('.example');
    if (b) $('#brief').value = EXAMPLES[+b.dataset.i];
  });
  document.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    $('#tab-brief').hidden = tab.dataset.tab !== 'brief';
    $('#tab-details').hidden = tab.dataset.tab !== 'details';
    $('#plan-hint').textContent = tab.dataset.tab === 'brief'
      ? 'Planning is cheap (a fraction of a cent) and needs no GPU.'
      : 'Fixed rules, no AI: instant and free.';
  }));
  $('#currency').innerHTML = (config.currencies || ['USD']).map((c) => `<option>${esc(c)}</option>`).join('');
  $('#create').addEventListener('submit', async (e) => {
    e.preventDefault();
    toast('');
    const brief = !$('#tab-brief').hidden;
    const body = brief ? { brief: $('#brief').value } : {
      budget: Number($('#budget').value), currency: $('#currency').value, area_sqm: Number($('#area').value),
      bhk: $('#bhk').value, style: $('#style').value,
    };
    busy($('#plan-btn'), true, brief ? 'The agents are planning…' : 'Planning…');
    try {
      showPlan(await api('/api/plan', body));
      loadProjects();
    } catch (err) {
      toast(`Could not plan: ${err.message}`);
    } finally {
      busy($('#plan-btn'), false);
    }
  });
}

// --- plan review ---------------------------------------------------------------------------------------------
function showPlan(data) {
  current = data;
  const { manifest, summary } = data;
  const meta = manifest.metadata;
  const nodes = manifest.topology.nodes;
  const designed = meta.design_source === 'agent';
  $('#plan-body').innerHTML = `
    <div class="plan-head">${esc(meta.bhk)} · ${meta.total_area_sqm} m² · ${esc(meta.aesthetic_theme)}</div>
    <div class="badges">
      <span class="badge ${designed ? 'good' : ''}">${designed ? 'Designed by AI agents' : 'Rules only'}</span>
      <span class="badge">${esc(meta.tier)} tier</span>
      <span class="badge">${nodes.length} rooms to render</span>
    </div>
    <div class="row"><span>Budget</span><span>${money(meta.total_budget, meta.currency)}</span></div>
    <div class="row"><span>Planned spend</span><span>${money(meta.spent, meta.currency)}</span></div>
    <div class="bar"><span style="width:${Math.min(100, meta.spent / meta.total_budget * 100)}%"></span></div>
    ${meta.notes ? `<p class="muted">Your notes: ${esc(meta.notes)}</p>` : ''}
    ${nodes.map((n) => `
      <div class="room">
        <h4><span>${esc(n.room_name)}</span><span class="muted">${money(n.allocated_budget, meta.currency)}</span></h4>
        ${n.description ? `<p>${esc(n.description)}</p>` : ''}
        <details><summary>${n.hotspots.filter((h) => h.type === 'product_tag').length} items · ${n.area_sqm} m²</summary>
          <ul>${n.hotspots.filter((h) => h.type === 'product_tag').map((h) =>
            `<li><span>${esc(h.payload.title)}</span><span class="muted">${Math.round(h.payload.cost).toLocaleString()}</span></li>`).join('')}</ul>
        </details>
      </div>`).join('')}`;

  const rendered = summary.rooms_rendered === summary.rooms;
  $('#gpu-box').hidden = rendered;
  $('#open-btn').hidden = summary.rooms_rendered === 0;
  $('#open-btn').textContent = rendered ? 'Open the tour' : `Open the tour (${summary.rooms_rendered} of ${summary.rooms} rooms)`;
  $('#gpu-estimate').textContent = ` about $${summary.gpu_estimate.usd.toFixed(2)} and ${summary.gpu_estimate.minutes} minutes`;
  $('#confirm-text').textContent = config.mode === 'mock'
    ? 'Mock mode: nothing runs on AWS and nothing is charged.'
    : `Start a GPU instance (${config.instance_types.join(' or ')}) on AWS, up to about $${config.gpu_hourly}/hour, billed per second. It shuts itself down when finished.`;
  $('#confirm').checked = false;
  $('#render-btn').disabled = true;
  $('#plan').hidden = false;
  $('#plan').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('#confirm').addEventListener('change', () => { $('#render-btn').disabled = !$('#confirm').checked; });
$('#open-btn').addEventListener('click', () => current && openTour(current.summary.id));
$('#render-btn').addEventListener('click', async () => {
  toast('');
  busy($('#render-btn'), true, 'Starting…');
  try {
    const { job } = await api(`/api/projects/${encodeURIComponent(current.summary.id)}/render`, { confirm: true });
    watchJob(job);
  } catch (err) {
    toast(`Could not start the render: ${err.message}`);
  } finally {
    busy($('#render-btn'), false);
    $('#render-btn').disabled = !$('#confirm').checked;
  }
});

// --- job progress ---------------------------------------------------------------------------------------------
function watchJob(job) {
  clearInterval(pollTimer);
  $('#job').hidden = false;
  $('#job').scrollIntoView({ behavior: 'smooth', block: 'start' });
  renderJob(job);
  pollTimer = setInterval(async () => {
    try {
      const { job: fresh } = await api(`/api/jobs/${job.id}`);
      renderJob(fresh);
      refreshThumbs(fresh.project_id);
      if (['done', 'failed', 'cancelled'].includes(fresh.state)) {
        clearInterval(pollTimer);
        finishJob(fresh);
      }
    } catch (err) {
      toast(`Lost contact with the server: ${err.message}`);
      clearInterval(pollTimer);
    }
  }, 2000);
}

function stepIndex(stage) {
  const i = STEPS.findIndex((s) => stage.startsWith(s) || (s === 'Rendering rooms' && stage.startsWith('Rendering')));
  return i < 0 ? 0 : i;
}

function renderJob(job) {
  const idx = stepIndex(job.stage);
  const finished = job.state === 'done';
  $('#job-body').innerHTML = `
    <ul class="steps">${STEPS.map((s, i) => `<li class="${finished || i < idx ? 'done' : i === idx && job.state === 'running' ? 'active' : ''}">${esc(s)}</li>`).join('')}</ul>
    <div><strong>${esc(job.stage)}</strong> <span class="muted">· ${Math.floor(job.elapsed / 60)}m ${job.elapsed % 60}s</span></div>
    <div class="job-detail">${esc(job.detail || '')}${job.attempts > 1 && job.stage === 'Waiting for a GPU' ? ` (attempt ${job.attempts})` : ''}</div>
    <div class="bar"><span style="width:${job.rooms_total ? job.rooms_done / job.rooms_total * 100 : 0}%"></span></div>
    <div class="muted">${job.rooms_done} of ${job.rooms_total} rooms rendered</div>
    ${job.error ? `<div class="job-error">${esc(job.error)}</div>` : ''}`;
  $('#cancel-btn').hidden = !['queued', 'running'].includes(job.state);
  $('#cancel-btn').onclick = async () => {
    try { await api(`/api/jobs/${job.id}/cancel`, {}); } catch (err) { toast(err.message); }
  };
  if (!$('#thumbs').children.length && current) refreshThumbs(job.project_id);
}

async function refreshThumbs(projectId) {
  try {
    const { manifest, summary } = await api(`/api/projects/${encodeURIComponent(projectId)}`);
    const done = new Set(summary.rendered);
    $('#thumbs').innerHTML = manifest.topology.nodes.map((n) => `
      <div class="thumb">${done.has(n.room_id)
        ? `<img alt="${esc(n.room_name)} panorama" src="/p/${encodeURIComponent(projectId)}/${esc(n.asset_url)}">`
        : esc(n.room_name)}</div>`).join('');
  } catch { /* the next poll will try again */ }
}

async function finishJob(job) {
  loadProjects();
  if (job.state === 'done') {
    toast('');
    await openTour(job.project_id);
  } else if (job.state === 'failed') {
    toast(`The render failed. ${job.error || ''}`);
  }
}

// --- tour -------------------------------------------------------------------------------------------------------
async function openTour(projectId) {
  toast('');
  try {
    const { manifest, summary } = await api(`/api/projects/${encodeURIComponent(projectId)}`);
    if (!summary.rooms_rendered) throw new Error('this project has no rendered rooms yet');
    tour?.destroy();
    $('#empty').hidden = true;
    $('#tour-wrap').hidden = false;
    const meta = manifest.metadata;
    $('#tour-title').textContent = `${meta.bhk} · ${meta.total_area_sqm} m² · ${meta.aesthetic_theme}`;
    $('#tour-sub').textContent = `${meta.tier} tier · budget ${money(meta.total_budget, meta.currency)}`;
    $('#spent').textContent = money(meta.spent, meta.currency);
    $('#remaining').textContent = money(meta.remaining, meta.currency);
    $('#spent-bar').style.width = `${Math.min(100, meta.spent / meta.total_budget * 100)}%`;
    const byId = Object.fromEntries(manifest.topology.nodes.map((n) => [n.room_id, n]));
    const available = new Set(summary.rendered);
    tour = mountTour({
      container: $('#tour'), manifest, available,
      manifestUrl: new URL(`/p/${encodeURIComponent(projectId)}/manifest.json`, location.href),
      onRoomChange: (roomId) => {
        const node = byId[roomId];
        document.querySelectorAll('#rooms .row').forEach((el) => el.classList.toggle('active', el.dataset.id === roomId));
        $('#items-title').textContent = `Items in ${node.room_name}`;
        $('#items').innerHTML = node.hotspots.filter((h) => h.type === 'product_tag').map((h) =>
          `<div class="row clickable" data-marker="${esc(h.id)}"><span>${esc(h.payload.title)}<br><small>${esc(h.payload.material)}</small></span>` +
          `<span>${Math.round(h.payload.cost).toLocaleString()}</span></div>`).join('');
      },
    });
    $('#rooms').innerHTML = tour.rooms.map((n) =>
      `<div class="row clickable" data-id="${esc(n.room_id)}"><span>${esc(n.room_name)}<br><small>${n.area_sqm} m²</small></span>` +
      `<span>${Math.round(n.allocated_budget).toLocaleString()}</span></div>`).join('');
  } catch (err) {
    toast(`Could not open the tour: ${err.message}`);
  }
}
$('#rooms').addEventListener('click', (e) => { const r = e.target.closest('[data-id]'); if (r && tour) tour.setRoom(r.dataset.id); });
$('#items').addEventListener('click', (e) => { const r = e.target.closest('[data-marker]'); if (r && tour) tour.gotoMarker(r.dataset.marker); });

// --- projects ---------------------------------------------------------------------------------------------------
async function loadProjects() {
  try {
    const { projects } = await api('/api/projects');
    $('#project-list').innerHTML = projects.length ? projects.map((p) => `
      <div class="row">
        <span>${esc(p.bhk)} · ${esc(p.style)}<br><small>${p.rooms_rendered}/${p.rooms} rooms · ${esc(p.currency)} ${Math.round(p.budget).toLocaleString()}</small></span>
        <span class="actions">
          <button class="btn" data-review="${esc(p.id)}">Plan</button>
          ${p.rooms_rendered ? `<button class="btn" data-open="${esc(p.id)}">Tour</button>` : ''}
        </span>
      </div>`).join('') : '<p class="muted">None yet. Plan one above.</p>';
  } catch (err) { toast(err.message); }
}
$('#project-list').addEventListener('click', async (e) => {
  const open = e.target.closest('[data-open]'), review = e.target.closest('[data-review]');
  if (open) openTour(open.dataset.open);
  if (review) {
    try { showPlan(await api(`/api/projects/${encodeURIComponent(review.dataset.review)}`)); } catch (err) { toast(err.message); }
  }
});
$('#remote-btn').addEventListener('click', async () => {
  busy($('#remote-btn'), true, 'Looking…');
  try {
    const { projects } = await api('/api/remote');
    $('#remote').hidden = false;
    $('#remote-list').innerHTML = projects.length ? projects.map((id) => `
      <div class="row"><span><small>${esc(id)}</small></span>
        <span class="actions"><button class="btn" data-import="${esc(id)}">Import</button></span></div>`).join('')
      : '<p class="muted">Nothing new on AWS.</p>';
  } catch (err) { toast(err.message); } finally { busy($('#remote-btn'), false); }
});
$('#remote-list').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-import]');
  if (!b) return;
  busy(b, true, 'Importing…');
  try {
    const { summary } = await api('/api/import', { project_id: b.dataset.import });
    await loadProjects();
    $('#remote-btn').click();
    if (summary.rooms_rendered) openTour(summary.id);
  } catch (err) { toast(err.message); busy(b, false); }
});

// --- start ------------------------------------------------------------------------------------------------------
(async function init() {
  try {
    config = await api('/api/config');
    const mode = $('#mode');
    mode.textContent = config.mode === 'mock' ? 'Mock mode · no AWS spend' : `AWS · ${config.instance_types[0]}`;
    mode.classList.toggle('warn', config.mode === 'mock');
    initForm();
    await loadProjects();
    const { job } = await api('/api/jobs/current');
    if (job) {  // the page was reloaded while a render was running
      current = { summary: { id: job.project_id } };
      watchJob(job);
    }
  } catch (err) {
    toast(`Could not reach the server: ${err.message}`);
  }
})();
