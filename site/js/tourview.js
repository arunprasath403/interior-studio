import { api } from './api.js';
import { esc, homeTitle, levelLabel, money, roomTitle, toast } from './format.js';
import { mountTour } from './tour.js';

export async function renderTour(root, projectId) {
  document.body.classList.add('immersive');
  root.innerHTML = '<div class="tour-loading"><span class="spin big"></span><p>Opening the walkthrough…</p></div>';

  let data;
  try {
    data = await api.project(projectId);
  } catch (err) {
    document.body.classList.remove('immersive');
    root.innerHTML = `<section class="notice"><h1>We couldn't open that home</h1><p>${esc(err.message)}</p><a class="btn" href="#/">Back to the studio</a></section>`;
    return null;
  }
  const { manifest, summary, images } = data;
  const meta = manifest.metadata;
  if (!Object.keys(images).length) {
    document.body.classList.remove('immersive');
    root.innerHTML = `<section class="notice"><h1>This home isn't ready yet</h1><p>Its rooms are still being created. Please check back in a few minutes.</p><a class="btn" href="#/">Back to the studio</a></section>`;
    return null;
  }

  const byId = Object.fromEntries(manifest.topology.nodes.map((n) => [n.room_id, n]));
  root.innerHTML = `
    <section class="tour">
      <div class="viewer" id="viewer"></div>
      <div class="tour-top">
        <a class="pill" href="#/" aria-label="Back to the studio">← Back</a>
        <div class="tour-title"><strong>${esc(homeTitle(summary))}</strong><small>${Math.round(meta.total_area_sqm)} m² · ${esc(levelLabel(meta.tier))} level</small></div>
        <span class="grow"></span>
        <button class="pill" id="share" type="button">Share</button>
        <button class="pill solid" id="toggle" type="button" aria-expanded="false" aria-controls="drawer">Your design</button>
      </div>
      <nav class="room-bar" id="room-bar" aria-label="Rooms"></nav>
      <aside class="drawer" id="drawer" hidden aria-label="Your design">
        <button class="close" id="close" type="button" aria-label="Close">×</button>
        <h2>Your design</h2>
        <div class="budget">
          <div><span class="muted">Budget</span><strong>${esc(money(meta.total_budget, meta.currency))}</strong></div>
          <div><span class="muted">Planned spend</span><strong>${esc(money(meta.spent, meta.currency))}</strong></div>
        </div>
        <div class="bar"><span style="width:${Math.min(100, meta.spent / meta.total_budget * 100)}%"></span></div>
        <h3>Rooms</h3><div id="room-list"></div>
        <h3 id="items-title">In this room</h3><ul class="items" id="item-list"></ul>
      </aside>
    </section>`;

  const $ = (sel) => root.querySelector(sel);
  const tour = mountTour({
    container: $('#viewer'), manifest, images,
    onRoomChange: (roomId) => {
      const room = byId[roomId];
      root.querySelectorAll('[data-room]').forEach((el) => el.classList.toggle('active', el.dataset.room === roomId));
      $('#items-title').textContent = `In the ${roomTitle(room).toLowerCase()}`;
      $('#item-list').innerHTML = room.hotspots.filter((h) => h.type === 'product_tag').map((h) => `
        <li><button type="button" data-item="${esc(h.id)}"><span>${esc(h.payload.title)}<small>${esc(h.payload.material)}</small></span><b>${esc(money(h.payload.cost, meta.currency))}</b></button></li>`).join('');
    },
  });

  $('#room-bar').innerHTML = tour.rooms.map((n) => `<button type="button" class="pill room" data-room="${esc(n.room_id)}">${esc(roomTitle(n))}</button>`).join('');
  $('#room-list').innerHTML = tour.rooms.map((n) => `
    <button type="button" class="room-row" data-room="${esc(n.room_id)}"><span>${esc(roomTitle(n))}<small>${n.area_sqm} m²</small></span><b>${esc(money(n.allocated_budget, meta.currency))}</b></button>`).join('');
  root.querySelectorAll('[data-room]').forEach((el) => el.addEventListener('click', () => tour.goToRoom(el.dataset.room)));
  $('#item-list').addEventListener('click', (e) => { const b = e.target.closest('[data-item]'); if (b) tour.goToItem(b.dataset.item); });

  const drawer = $('#drawer'), toggle = $('#toggle');
  const setDrawer = (open) => { drawer.hidden = !open; toggle.setAttribute('aria-expanded', String(open)); };
  toggle.addEventListener('click', () => setDrawer(drawer.hidden));
  $('#close').addEventListener('click', () => setDrawer(false));
  const onKey = (e) => { if (e.key === 'Escape') setDrawer(false); };
  document.addEventListener('keydown', onKey);

  $('#share').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(location.href); toast('Link copied. Anyone with it can walk through this home.'); }
    catch { toast('Copy the address from your browser to share this home.'); }
  });

  return () => { document.removeEventListener('keydown', onKey); tour.destroy(); };
}
