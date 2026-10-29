import { api } from './api.js';
import { designing, esc, minutesLabel, toast } from './format.js';

const POLL_MS = 3000;
const SECONDS_PER_ROOM = 40;
const STUDIO_SECONDS = 270;

function buildSteps(job) {
  const phase = job.phase;
  const studioReady = ['rooms', 'finishing', 'done'].includes(phase);
  const steps = [{ label: 'Planning your home', note: 'Rooms laid out and priced', state: 'done' }];
  steps.push({
    label: 'Getting the design studio ready',
    note: phase === 'queue' ? "The studio is busy right now. We'll begin as soon as there's space."
      : studioReady ? 'Ready' : 'Setting up your design space. This usually takes a few minutes.',
    state: studioReady ? 'done' : 'active',
  });
  job.rooms.forEach((room, i) => {
    const done = i < job.rooms_done || phase === 'done';
    const active = studioReady && phase !== 'done' && i === job.rooms_done;
    steps.push({
      label: designing(room),
      note: done ? 'Done' : active ? 'Choosing the finishes, lighting and furniture…' : 'Up next',
      state: done ? 'done' : active ? 'active' : 'todo',
    });
  });
  steps.push({
    label: 'Adding the finishing touches',
    note: phase === 'done' ? 'Done' : phase === 'finishing' ? 'Almost there…' : 'Last step',
    state: phase === 'done' ? 'done' : phase === 'finishing' ? 'active' : 'todo',
  });
  return steps;
}

function progressPercent(job) {
  switch (job.phase) {
    case 'queue': return 4;
    case 'studio': return 18;
    case 'rooms': return Math.round(30 + 62 * (job.rooms_done / Math.max(1, job.rooms_total)));
    case 'finishing': return 96;
    case 'done': return 100;
    default: return 0;
  }
}

function etaText(job) {
  const remainingRooms = Math.max(0, job.rooms_total - job.rooms_done);
  switch (job.phase) {
    case 'queue': return 'Waiting for a free space in the studio';
    case 'studio': return `${minutesLabel(STUDIO_SECONDS + remainingRooms * SECONDS_PER_ROOM)} to go`;
    case 'rooms': return remainingRooms ? `${minutesLabel(remainingRooms * SECONDS_PER_ROOM + 15)} to go` : 'Almost there';
    case 'finishing': return 'Almost there';
    default: return '';
  }
}

export async function renderStudio(root, jobId) {
  root.innerHTML = `
    <section class="studio">
      <p class="eyebrow">Your walkthrough</p>
      <h1 id="s-title">Creating your walkthrough</h1>
      <p class="lede" id="s-sub">You can keep this page open. Each room appears below as soon as it's ready.</p>
      <div class="panel" id="s-panel">
        <div class="progress">
          <div class="bar"><span id="s-bar"></span></div>
          <div class="progress-meta"><span id="s-pct">0%</span><span id="s-eta"></span></div>
        </div>
        <ol class="stages" id="stages" aria-live="polite"></ol>
        <div class="previews" id="previews"></div>
        <div class="studio-actions"><button class="btn ghost danger" id="stop" type="button">Stop</button></div>
      </div>
    </section>`;

  const $ = (sel) => root.querySelector(sel);
  let timer = null, closed = false, failures = 0, previewsFor = -1, confirmStop = false, redirect = null;

  $('#stop').addEventListener('click', async (e) => {
    const button = e.currentTarget;
    if (!confirmStop) {
      confirmStop = true;
      button.textContent = 'Click again to stop';
      setTimeout(() => { confirmStop = false; button.textContent = 'Stop'; }, 4000);
      return;
    }
    button.disabled = true;
    try { await api.cancel(jobId); } catch (err) { toast(err.message); button.disabled = false; }
  });

  async function showPreviews(job) {
    if (job.rooms_done === previewsFor) return;
    previewsFor = job.rooms_done;
    if (!job.rooms_done) return;
    try {
      const { images } = await api.project(job.project_id);
      $('#previews').innerHTML = job.rooms.map((room) => images[room.id]
        ? `<figure class="preview"><img src="${esc(images[room.id])}" alt="${esc(room.name)}"><figcaption>${esc(room.name)}</figcaption></figure>`
        : `<figure class="preview empty"><figcaption>${esc(room.name)}</figcaption></figure>`).join('');
    } catch { /* previews are a bonus */ }
  }

  function finish(job) {
    $('#stop').hidden = true;
    const panel = $('#s-panel');
    if (job.state === 'done') {
      $('#s-title').textContent = 'Your walkthrough is ready';
      $('#s-sub').textContent = 'Taking you there now…';
      panel.insertAdjacentHTML('beforeend', `<div class="studio-actions"><a class="btn" href="#/tour/${esc(job.project_id)}">Open your walkthrough</a></div>`);
      redirect = setTimeout(() => { if (!closed) location.hash = `#/tour/${job.project_id}`; }, 2500);
      try { localStorage.removeItem('studio.active'); } catch { /* ignore */ }
    } else {
      const cancelled = job.state === 'cancelled';
      $('#s-title').textContent = cancelled ? 'You stopped this walkthrough' : "We couldn't finish this one";
      $('#s-sub').textContent = cancelled ? 'Nothing was charged. You can start again any time.' : (job.error || 'Please try again.');
      panel.insertAdjacentHTML('beforeend', '<div class="studio-actions"><a class="btn" href="#/create">Start again</a></div>');
      try { localStorage.removeItem('studio.active'); } catch { /* ignore */ }
    }
  }

  async function poll() {
    if (closed) return;
    try {
      const job = await api.job(jobId);
      failures = 0;
      const pct = progressPercent(job);
      $('#s-bar').style.width = `${pct}%`;
      $('#s-pct').textContent = job.state === 'done' ? 'Done' : `${pct}%`;
      $('#s-eta').textContent = etaText(job);
      $('#stages').innerHTML = buildSteps(job).map((s) => `
        <li class="stage ${s.state}"><span class="mark" aria-hidden="true"></span>
          <div><strong>${esc(s.label)}</strong><small>${esc(s.note)}</small></div></li>`).join('');
      showPreviews(job);
      if (['done', 'failed', 'cancelled'].includes(job.state)) { finish(job); return; }
    } catch (err) {
      failures += 1;
      if (err.status === 404) { $('#s-title').textContent = "We couldn't find that walkthrough"; $('#stop').hidden = true; return; }
      if (failures === 3) toast('Having trouble reaching the studio. We\'ll keep trying.');
    }
    timer = setTimeout(poll, POLL_MS);
  }
  poll();

  return () => { closed = true; clearTimeout(timer); clearTimeout(redirect); };
}
