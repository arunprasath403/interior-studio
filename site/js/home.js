import { api } from './api.js';
import { esc, homeTitle, money, bedroomsLabel } from './format.js';

const STEPS = [
  ['1', 'Tell us about your home', 'Share your budget, the size and the style you love, in your own words or with a few simple choices.'],
  ['2', 'We design every room', 'Each room is planned, furnished and priced to stay within your budget, then brought to life as a realistic 360° view.'],
  ['3', 'Walk through it', 'Look around, step from room to room, and tap any piece to see what it costs. Share the link with anyone.'],
];
const PROMISES = [
  ['Stays within your budget', 'Every piece is priced, and the total never goes over what you set.'],
  ['Every room, beautifully designed', 'Realistic lighting, materials and furniture, with a consistent look from room to room.'],
  ['Easy to share', 'Send one link to family or your builder and they can walk through it too.'],
];

function homeCard(home) {
  const image = home.cover_url
    ? `<img src="${esc(home.cover_url)}" alt="" loading="lazy">`
    : '<div class="ph" aria-hidden="true"></div>';
  return `
    <a class="home-card" href="#/tour/${esc(home.id)}">
      <div class="home-photo">${image}</div>
      <div class="home-body">
        <h3>${esc(homeTitle(home))}</h3>
        <p class="meta">${bedroomsLabel(home.bedrooms)} · ${Math.round(home.area_sqm)} m² · ${home.rooms} rooms</p>
        <p class="meta">Budget ${esc(money(home.budget, home.currency))}</p>
        <span class="link">Walk through this home →</span>
      </div>
    </a>`;
}

async function activeBanner(slot) {
  let active;
  try { active = JSON.parse(localStorage.getItem('studio.active') || 'null'); } catch { active = null; }
  if (!active) return;
  try {
    const job = await api.job(active.jobId);
    if (job.state === 'done') {
      slot.innerHTML = `<div class="banner"><span>Your walkthrough is ready.</span><a class="btn small" href="#/tour/${esc(job.project_id)}">Open it</a></div>`;
    } else if (['waiting', 'running'].includes(job.state)) {
      slot.innerHTML = `<div class="banner"><span>Your walkthrough is being created.</span><a class="btn small" href="#/studio/${esc(job.id)}">See progress</a></div>`;
    } else {
      localStorage.removeItem('studio.active');
    }
  } catch { /* the banner is a nicety; the page works without it */ }
}

export async function renderHome(root, scrollTo) {
  root.innerHTML = `
    <div id="banner-slot"></div>
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">Your home, designed before it's built</p>
        <h1>See your home before you build it.</h1>
        <p class="lede">Tell us your budget, size and style. We design and price every room, then turn it into a 360° walkthrough you can explore and share.</p>
        <div class="actions">
          <a class="btn" href="#/create">Design your home</a>
          <a class="btn ghost" href="#/gallery">Explore sample homes</a>
        </div>
        <ul class="proof"><li>Every item priced</li><li>Stays within your budget</li><li>Ready in minutes</li></ul>
      </div>
      <figure class="hero-visual" id="hero-visual" aria-hidden="true"><div class="ph"></div></figure>
    </section>

    <section class="section" id="gallery" aria-labelledby="gallery-title">
      <div class="section-head">
        <h2 id="gallery-title">Homes designed in the studio</h2>
        <p>Open any of these to look around, room by room.</p>
      </div>
      <div class="cards" id="cards" aria-busy="true"><div class="card-skeleton"></div><div class="card-skeleton"></div></div>
    </section>

    <section class="section tint" id="how" aria-labelledby="how-title">
      <div class="section-head"><h2 id="how-title">How it works</h2><p>From a few details to a home you can walk through.</p></div>
      <ol class="steps-row">
        ${STEPS.map(([n, title, text]) => `<li><span class="num">${n}</span><h3>${title}</h3><p>${text}</p></li>`).join('')}
      </ol>
    </section>

    <section class="section" aria-label="Why people use it">
      <div class="promises">
        ${PROMISES.map(([title, text]) => `<div class="promise"><h3>${title}</h3><p>${text}</p></div>`).join('')}
      </div>
    </section>

    <section class="cta-band">
      <h2>Ready to see yours?</h2>
      <p>It starts with a few details about your home.</p>
      <a class="btn light" href="#/create">Design your home</a>
    </section>`;

  activeBanner(root.querySelector('#banner-slot'));
  try {
    const { homes } = await api.gallery();
    const cards = root.querySelector('#cards');
    cards.removeAttribute('aria-busy');
    cards.innerHTML = homes.length ? homes.map(homeCard).join('')
      : '<p class="muted">Sample homes are on their way. Design your own in the meantime.</p>';
    const hero = homes.find((h) => h.cover_url);
    if (hero) {
      root.querySelector('#hero-visual').innerHTML = `
        <img src="${esc(hero.cover_url)}" alt="">
        <figcaption><strong>${esc(homeTitle(hero))}</strong><span>${bedroomsLabel(hero.bedrooms)} · ${esc(money(hero.budget, hero.currency))} · ${hero.rooms} rooms</span></figcaption>`;
    }
  } catch (err) {
    root.querySelector('#cards').innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
  if (scrollTo) document.getElementById(scrollTo)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  return null;
}
