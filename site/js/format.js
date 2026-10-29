// Everyday wording and number formats, so no technical terms ever reach the page.
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export function money(amount, currency = 'USD') {
  try {
    return new Intl.NumberFormat(currency === 'INR' ? 'en-IN' : 'en-US', {
      style: 'currency', currency, maximumFractionDigits: 0,
    }).format(amount);
  } catch {
    return `${currency} ${Math.round(amount).toLocaleString()}`;
  }
}

export const cap = (s) => String(s || '').replace(/\b\w/g, (c) => c.toUpperCase());
export const sqft = (m2) => Math.round(m2 * 10.764);
export const bedroomCount = (bhk) => parseInt(String(bhk), 10) || 1;
export const bedroomsLabel = (bhk) => { const n = bedroomCount(bhk); return `${n} bedroom${n === 1 ? '' : 's'}`; };

export function homeTitle(summary) {
  if (summary.title) return summary.title;
  const n = bedroomCount(summary.bedrooms);
  return `${cap(summary.style)} ${n}-bedroom home`;
}

const LEVELS = { economy: 'Essential', standard: 'Comfort', premium: 'Premium', luxury: 'Luxury' };
export const levelLabel = (level) => LEVELS[level] || cap(level);

const ROOM_WORDS = { living: 'living room', kitchen: 'kitchen', master_bedroom: 'master bedroom', bedroom: 'bedroom', bathroom: 'bathroom' };
export function roomWord(room) {
  if (room.kind === 'bedroom' && /\d/.test(room.name || '')) return String(room.name).toLowerCase();
  return ROOM_WORDS[room.kind] || String(room.name || 'room').toLowerCase();
}
export const designing = (room) => `Designing the ${roomWord(room)}`;
// "living room" -> "Living room". Accepts a manifest room ({kind, room_name}) or a job room ({kind, name}).
export function roomTitle(room) {
  const text = roomWord({ kind: room.kind, name: room.room_name || room.name });
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function minutesLabel(seconds) {
  const m = Math.max(1, Math.round(seconds / 60));
  return m === 1 ? 'about a minute' : `about ${m} minutes`;
}

let toastTimer;
export function toast(message) {
  const el = document.getElementById('toast');
  clearTimeout(toastTimer);
  el.textContent = message;
  el.hidden = !message;
  if (message) toastTimer = setTimeout(() => { el.hidden = true; }, 4500);
}
