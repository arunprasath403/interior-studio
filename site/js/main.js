import { renderHome } from './home.js';
import { renderCreate } from './create.js';
import { renderStudio } from './studio.js';
import { renderTour } from './tourview.js';

const main = document.getElementById('main');
let cleanup = null;

const routes = [
  [/^#\/?$/, (el) => renderHome(el)],
  [/^#\/gallery$/, (el) => renderHome(el, 'gallery')],
  [/^#\/how$/, (el) => renderHome(el, 'how')],
  [/^#\/create$/, (el) => renderCreate(el)],
  [/^#\/studio\/([a-f0-9]{10})$/, (el, jobId) => renderStudio(el, jobId)],
  [/^#\/tour\/([A-Za-z0-9_-]{3,80})$/, (el, projectId) => renderTour(el, projectId)],
];

async function route() {
  if (typeof cleanup === 'function') cleanup();
  cleanup = null;
  document.body.classList.remove('immersive');
  const hash = location.hash || '#/';
  const match = routes.map(([re, view]) => [hash.match(re), view]).find(([m]) => m);
  const [m, view] = match || [null, routes[0][1]];
  window.scrollTo(0, 0);
  cleanup = await view(main, ...(m ? m.slice(1) : []));
  document.querySelectorAll('.nav-link').forEach((a) => a.classList.toggle('active', a.getAttribute('href') === hash));
}

window.addEventListener('hashchange', route);
route();
