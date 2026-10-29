// Talks to the studio's server. Creating things needs an invitation code, which is remembered on this device.
const BASE = (window.STUDIO_API || '').replace(/\/$/, '');
const CODE_KEY = 'studio.code';

export const getCode = () => { try { return localStorage.getItem(CODE_KEY) || ''; } catch { return ''; } };
export const setCode = (code) => { try { localStorage.setItem(CODE_KEY, code); } catch { /* private mode: it just will not be remembered */ } };
export const clearCode = () => { try { localStorage.removeItem(CODE_KEY); } catch { /* ignore */ } };

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function call(path, { method = 'GET', body, code } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (code !== undefined) headers['X-Access-Code'] = code;
  let res;
  try {
    res = await fetch(`${BASE}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, 'We could not reach the studio. Please check your connection and try again.');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data.error || 'Something went wrong. Please try again.');
  return data;
}

export const api = {
  gallery: () => call('/api/gallery'),
  project: (id) => call(`/api/projects/${encodeURIComponent(id)}`),
  job: (id) => call(`/api/jobs/${encodeURIComponent(id)}`),
  checkCode: (code) => call('/api/access', { method: 'POST', body: {}, code }),
  plan: (body) => call('/api/plan', { method: 'POST', body, code: getCode() }),
  start: (projectId) => call(`/api/projects/${encodeURIComponent(projectId)}/render`, { method: 'POST', body: { confirm: true }, code: getCode() }),
  cancel: (jobId) => call(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', body: {}, code: getCode() }),
};
