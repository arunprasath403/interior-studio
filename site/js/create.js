import { api, ApiError, clearCode, getCode, setCode } from './api.js';
import { esc, homeTitle, levelLabel, money, roomTitle, sqft } from './format.js';

const STYLES = [
  ['scandinavian', 'Scandinavian', 'Light woods, soft whites and calm greys', ['#efe7da', '#c9a97a', '#8a9a8c']],
  ['japandi', 'Japandi', 'Natural oak, linen and quiet earth tones', ['#e8dfd0', '#a98e6d', '#4b4a46']],
  ['modern minimalist', 'Modern minimal', 'Clean lines, warm whites and matte black', ['#f1efec', '#cfc8bf', '#2b2b2b']],
  ['industrial', 'Industrial', 'Exposed brick, steel and leather', ['#b5654a', '#4a4e52', '#2a2a2a']],
  ['dark academia', 'Dark academia', 'Deep greens, walnut and brass', ['#24402f', '#5c3b27', '#b89147']],
];
const EXAMPLES = [
  '2 bedrooms, about 90 sq m, 30 lakh rupees, warm Scandinavian style, no TV in the living room',
  '3 bedroom home, 120 sq m, $60,000 budget, dark academia with lots of books and brass',
  '1 bedroom flat, 55 sq m, 12 lakh, calm and minimal Japandi',
];
const WAIT_MESSAGES = {
  403: 'That invitation code does not look right. Please check it and try again.',
  429: 'We have reached today\'s limit for new designs. Please come back tomorrow.',
};

const defaultCurrency = () => ((navigator.language || '').endsWith('-IN') ? 'INR' : 'USD');
let draft = { mode: 'words', brief: '', budget: '', currency: defaultCurrency(), area: '', unit: 'sqft', bedrooms: '2', style: 'scandinavian' };

export function renderCreate(root) {
  showForm(root);
  return null;
}

// --- step 1: the form -------------------------------------------------------------------------------------------
function showForm(root, message = '') {
  const currencies = ['INR', 'USD', 'EUR', 'GBP', 'AED', 'CAD', 'AUD', 'SGD'];
  root.innerHTML = `
    <section class="create">
      <div class="create-head">
        <p class="eyebrow">Step 1 of 2</p>
        <h1>Tell us about your home</h1>
        <p class="lede">Describe it in your own words, or choose the details yourself. We'll turn it into a room-by-room design plan.</p>
      </div>
      <form id="home-form" class="panel" novalidate>
        <div class="seg" role="tablist" aria-label="How would you like to start?">
          <button type="button" role="tab" data-mode="words" aria-selected="${draft.mode === 'words'}">Describe it</button>
          <button type="button" role="tab" data-mode="details" aria-selected="${draft.mode === 'details'}">Choose details</button>
        </div>

        <div id="pane-words" ${draft.mode === 'words' ? '' : 'hidden'}>
          <label for="brief">What are you looking for?</label>
          <textarea id="brief" rows="5" maxlength="2000" placeholder="${esc(EXAMPLES[0])}">${esc(draft.brief)}</textarea>
          <p class="fine">Mention your budget, the size and the number of bedrooms. Tell us anything you'd like included or left out.</p>
          <div class="chips" aria-label="Examples">${EXAMPLES.map((e, i) => `<button type="button" class="chip" data-example="${i}">${esc(e.split(',').slice(0, 3).join(','))}…</button>`).join('')}</div>
        </div>

        <div id="pane-details" ${draft.mode === 'details' ? '' : 'hidden'}>
          <div class="field-row">
            <div><label for="budget">Budget</label><input id="budget" type="number" min="100" inputmode="decimal" placeholder="3000000" value="${esc(draft.budget)}"></div>
            <div><label for="currency">Currency</label><select id="currency">${currencies.map((c) => `<option ${c === draft.currency ? 'selected' : ''}>${c}</option>`).join('')}</select></div>
          </div>
          <div class="field-row">
            <div><label for="area">Size of the home</label><input id="area" type="number" min="1" inputmode="decimal" placeholder="1000" value="${esc(draft.area)}"></div>
            <div><label for="unit">Measured in</label><select id="unit"><option value="sqft" ${draft.unit === 'sqft' ? 'selected' : ''}>square feet</option><option value="sqm" ${draft.unit === 'sqm' ? 'selected' : ''}>square metres</option></select></div>
          </div>
          <label for="bedrooms">Bedrooms</label>
          <select id="bedrooms">${[1, 2, 3, 4, 5].map((n) => `<option ${String(n) === draft.bedrooms ? 'selected' : ''}>${n}</option>`).join('')}</select>
          <fieldset class="styles"><legend>Style</legend>
            ${STYLES.map(([value, label, text, swatch]) => `
              <label class="style-option"><input type="radio" name="style" value="${value}" ${value === draft.style ? 'checked' : ''}>
                <span class="style-card"><span class="swatch" aria-hidden="true">${swatch.map((c) => `<i style="background:${c}"></i>`).join('')}</span>
                <strong>${label}</strong><small>${text}</small></span></label>`).join('')}
          </fieldset>
        </div>

        <p class="error" id="form-error" role="alert" ${message ? '' : 'hidden'}>${esc(message)}</p>
        <button class="btn wide" id="submit" type="submit">Create my design plan</button>
        <p class="fine center">Takes a few seconds. No payment needed.</p>
      </form>
    </section>

    <dialog id="code-dialog" aria-labelledby="code-title">
      <form method="dialog" id="code-form">
        <h2 id="code-title">Creating a new design is by invitation</h2>
        <p>While the studio is in preview, new designs need an invitation code. You can still explore every sample home without one.</p>
        <label for="code">Invitation code</label>
        <input id="code" type="password" autocomplete="off" required>
        <p class="error" id="code-error" role="alert" hidden></p>
        <div class="dialog-actions"><button type="button" class="btn ghost" id="code-cancel">Cancel</button><button class="btn" type="submit">Continue</button></div>
      </form>
    </dialog>`;

  const $ = (sel) => root.querySelector(sel);
  const form = $('#home-form');
  const dialog = $('#code-dialog');

  const readForm = () => {
    draft = {
      ...draft,
      brief: $('#brief').value,
      budget: $('#budget').value, currency: $('#currency').value, area: $('#area').value, unit: $('#unit').value,
      bedrooms: $('#bedrooms').value, style: form.querySelector('input[name=style]:checked')?.value || draft.style,
    };
  };

  form.querySelectorAll('[role=tab]').forEach((tab) => tab.addEventListener('click', () => {
    readForm();
    draft.mode = tab.dataset.mode;
    form.querySelectorAll('[role=tab]').forEach((t) => t.setAttribute('aria-selected', String(t === tab)));
    $('#pane-words').hidden = draft.mode !== 'words';
    $('#pane-details').hidden = draft.mode !== 'details';
  }));
  form.querySelectorAll('[data-example]').forEach((chip) => chip.addEventListener('click', () => { $('#brief').value = EXAMPLES[+chip.dataset.example]; }));

  const showError = (text) => { const el = $('#form-error'); el.textContent = text; el.hidden = !text; };

  async function submit() {
    readForm();
    let body;
    if (draft.mode === 'words') {
      if (draft.brief.trim().length < 10) return showError('Please tell us a little more about the home you have in mind.');
      body = { brief: draft.brief };
    } else {
      const budget = Number(draft.budget), area = Number(draft.area);
      if (!(budget >= 100)) return showError('Please enter your budget.');
      if (!(area > 0)) return showError('Please enter the size of your home.');
      body = { budget, currency: draft.currency, area_sqm: draft.unit === 'sqft' ? area / 10.764 : area, bhk: draft.bedrooms, style: draft.style };
    }
    showError('');
    const button = $('#submit');
    button.disabled = true;
    button.innerHTML = '<span class="spin"></span> Designing your plan…';
    try {
      const data = await api.plan(body);
      showPlan(root, data);
    } catch (err) {
      button.disabled = false;
      button.textContent = 'Create my design plan';
      if (err.status === 403) { clearCode(); askForCode(); return; }
      showError(friendlyPlanError(err));
    }
  }

  function askForCode() {
    $('#code-error').hidden = true;
    if (!dialog.open) dialog.showModal();
    $('#code').focus();
  }
  $('#code-cancel').addEventListener('click', () => dialog.close());
  $('#code-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const code = $('#code').value.trim();
    try {
      await api.checkCode(code);
      setCode(code);
      dialog.close();
      submit();
    } catch (err) {
      const el = $('#code-error');
      el.textContent = WAIT_MESSAGES[err.status] || err.message;
      el.hidden = false;
    }
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (!getCode()) { readForm(); askForCode(); return; }
    submit();
  });
}

function friendlyPlanError(err) {
  if (err instanceof ApiError && err.status === 422) {
    return `We need a little more detail: ${String(err.message).replace(/^the brief does not say:\s*/i, '')}.`;
  }
  return WAIT_MESSAGES[err.status] || err.message;
}

// --- step 2: the plan --------------------------------------------------------------------------------------------
function showPlan(root, data) {
  const { manifest, summary } = data;
  const meta = manifest.metadata;
  const nodes = manifest.topology.nodes;
  const planned = money(meta.spent, meta.currency);
  root.innerHTML = `
    <section class="create plan">
      <div class="create-head">
        <p class="eyebrow">Step 2 of 2</p>
        <h1>Here's your design plan</h1>
        <p class="lede">${esc(homeTitle(summary))} · ${Math.round(meta.total_area_sqm)} m² (${sqft(meta.total_area_sqm).toLocaleString()} sq ft) · ${esc(levelLabel(meta.tier))} level</p>
      </div>
      <div class="panel">
        <div class="budget">
          <div><span class="muted">Your budget</span><strong>${esc(money(meta.total_budget, meta.currency))}</strong></div>
          <div><span class="muted">Planned spend</span><strong>${esc(planned)}</strong></div>
        </div>
        <div class="bar" role="img" aria-label="Planned spend is ${Math.round(meta.spent / meta.total_budget * 100)} percent of your budget"><span style="width:${Math.min(100, meta.spent / meta.total_budget * 100)}%"></span></div>
        ${meta.notes ? `<p class="note">Your notes: ${esc(meta.notes)}</p>` : ''}
        <div class="rooms">
          ${nodes.map((n) => {
            const items = n.hotspots.filter((h) => h.type === 'product_tag');
            return `<article class="room-card">
              <header><h3>${esc(roomTitle(n))}</h3><span>${esc(money(n.allocated_budget, meta.currency))}</span></header>
              <p class="meta">${n.area_sqm} m²</p>
              ${n.description ? `<p>${esc(n.description)}</p>` : ''}
              <details><summary>What's included (${items.length})</summary>
                <ul>${items.map((h) => `<li><span>${esc(h.payload.title)}</span><span>${esc(money(h.payload.cost, meta.currency))}</span></li>`).join('')}</ul>
              </details>
            </article>`;
          }).join('')}
        </div>
        <p class="error" id="start-error" role="alert" hidden></p>
        <div class="plan-actions">
          <button class="btn" id="start" type="button">Bring it to life</button>
          <button class="btn ghost" id="edit" type="button">Change my details</button>
        </div>
        <p class="fine">This takes about ${summary.estimated_minutes} minutes. You can keep this page open and watch each room come together.</p>
      </div>
    </section>`;

  root.querySelector('#edit').addEventListener('click', () => showForm(root));
  root.querySelector('#start').addEventListener('click', async (e) => {
    const button = e.currentTarget;
    const error = root.querySelector('#start-error');
    error.hidden = true;
    button.disabled = true;
    button.innerHTML = '<span class="spin"></span> Getting started…';
    try {
      const job = await api.start(summary.id);
      try { localStorage.setItem('studio.active', JSON.stringify({ jobId: job.id, projectId: summary.id })); } catch { /* optional */ }
      location.hash = `#/studio/${job.id}`;
    } catch (err) {
      button.disabled = false;
      button.textContent = 'Bring it to life';
      error.textContent = err.status === 403 ? WAIT_MESSAGES[403] : err.message;
      error.hidden = false;
      if (err.status === 403) clearCode();
    }
  });
  window.scrollTo(0, 0);
}
