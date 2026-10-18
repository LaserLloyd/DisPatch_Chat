// REST calls for the 3.0 settings surfaces: settings store, connections, characters, starter packs,
// the first-run wizard, backups and plugin manifests.
//
// They live here, not in api.js, on purpose. api.js is imported at one `?v=` by about a dozen
// modules (assets.test.js enforces a single version per module), so adding to it forces a version
// cascade through files other workstreams are editing at the same time. Folding these in is a
// mechanical change for the next api.js bump; until then this file keeps the same contract:
//
//   - a failed call throws an Error whose message is "<status>: <detail>" and which also carries
//     `status`, `body`, and the 3.0 envelope's `code` / `fix` / `docs` (api.js errorExtras),
//   - a 401 (the session ran out) calls the handler main.js registers with setOnLocked(),
//   - nothing here may hang: every request is aborted after a generous timeout.

import { errorExtras } from './api.js?v=34';

let onLocked = null;
export function setOnLocked(fn) { onLocked = fn; }

const JSON_HEADERS = { 'Content-Type': 'application/json' };
const DEFAULT_TIMEOUT_MS = 60_000;

const body = (o) => JSON.stringify(o == null ? {} : o);
const enc = encodeURIComponent;

function flattenDetail(detail) {
  if (detail && typeof detail === 'object') {
    if (detail.errors && typeof detail.errors === 'object') {
      return Object.entries(detail.errors).map(([k, v]) => `${k}: ${v}`).join('; ');
    }
    if (typeof detail.detail === 'string') return detail.detail;
    if (typeof detail.message === 'string') return detail.message;
    try { return JSON.stringify(detail); } catch { return String(detail); }
  }
  return detail;
}

/** Turn a failed fetch Response into the Error every caller of this module handles. Pure. */
export async function failure(r, url = '') {
  let b = null;
  try { b = await r.clone().json(); } catch { /* not JSON */ }
  const extras = errorExtras(b, r.headers);
  const detail = flattenDetail((b && (b.detail || b.error)) || r.statusText);
  const err = new Error(`${r.status}: ${typeof detail === 'string' ? detail : r.statusText}`);
  err.status = r.status;
  err.body = b;
  // The per-key refusals of PATCH /api/settings and the permission list of a 409 plugin approval.
  if (b && b.errors && typeof b.errors === 'object') err.errors = b.errors;
  if (b && Array.isArray(b.permissions)) err.permissions = b.permissions;
  if (b && Array.isArray(b.requested)) err.requested = b.requested;
  Object.assign(err, extras);
  if (r.status === 401 && extras.code !== 'signin_required' && onLocked && !url.startsWith('/api/auth')) {
    try { onLocked(); } catch { /* ignore */ }
  }
  return err;
}

async function request(url, opts = {}) {
  let signal = opts.signal;
  let timer = null;
  if (!signal && typeof AbortController === 'function') {
    const ctl = new AbortController();
    signal = ctl.signal;
    timer = setTimeout(() => ctl.abort(), opts.timeoutMs || DEFAULT_TIMEOUT_MS);
  }
  const { timeoutMs: _t, ...fetchOpts } = opts;
  let r;
  try {
    r = await fetch(url, { ...fetchOpts, signal });
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (!r.ok) throw await failure(r, url);
  return r;
}

async function j(url, opts) {
  const r = await request(url, opts);
  return r.status === 204 ? null : r.json();
}

const post = (url, o, extra) => j(url, { method: 'POST', headers: JSON_HEADERS, body: body(o), ...extra });
const patch = (url, o) => j(url, { method: 'PATCH', headers: JSON_HEADERS, body: body(o) });
const del = (url) => j(url, { method: 'DELETE' });

function query(params) {
  const q = Object.entries(params || {}).filter(([, v]) => v != null && v !== '')
    .map(([k, v]) => `${enc(k)}=${enc(v)}`).join('&');
  return q ? `?${q}` : '';
}

/** GET a file the server sends as an attachment; returns { blob, name }. */
async function download(url, opts) {
  const r = await request(url, opts);
  const cd = r.headers.get('content-disposition') || '';
  const m = /filename="?([^";]+)"?/i.exec(cd);
  return { blob: await r.blob(), name: m ? m[1] : 'download' };
}

export const w5 = {
  // ---- settings store
  settingsSchema: () => j('/api/settings/schema'),
  settings: (params) => j(`/api/settings${query(params)}`),
  patchSettings: (set, reset) => patch('/api/settings', { ...(set && Object.keys(set).length ? { set } : {}),
    ...(reset && reset.length ? { reset } : {}) }),
  settingsDefaults: () => j('/api/settings/defaults'),

  // ---- connections
  connections: () => j('/api/connections'),
  createConnection: (o) => post('/api/connections', o),
  patchConnection: (id, o) => patch(`/api/connections/${enc(id)}`, o),
  deleteConnection: (id) => del(`/api/connections/${enc(id)}`),
  testConnection: (id) => post(`/api/connections/${enc(id)}/test`),
  testConnectionForm: (o) => post('/api/connections/test', o),
  discoverConnections: (o) => post('/api/connections/discover', o || {}),
  adoptConnections: () => post('/api/connections/adopt'),

  // ---- characters and starter packs
  characters: () => j('/api/characters'),
  allCharacters: () => j('/api/characters/all'),
  createCharacter: (o) => post('/api/characters', o),
  patchCharacter: (id, o) => patch(`/api/characters/${enc(id)}`, o),
  personalities: () => j('/api/characters/personas'),
  starterPacks: () => j('/api/starter-packs'),
  installPack: (id, o) => post(`/api/starter-packs/${enc(id)}/install`, o || {}),

  // ---- first-run wizard
  wizard: () => j('/api/wizard'),
  wizardDetect: (o) => post('/api/wizard/detect', o || {}),
  wizardConnection: (o) => post('/api/wizard/connection', o),
  wizardFamily: (o) => post('/api/wizard/family', o),
  wizardNetwork: (o) => post('/api/wizard/network', o),
  wizardStep: (step, status) => post(`/api/wizard/step/${enc(step)}`, { status }),
  wizardFinish: () => post('/api/wizard/finish'),
  setup: (o) => post('/api/auth/setup', o),

  // ---- backups, support bundle, per-user data
  backups: () => j('/api/backups'),
  createBackup: (o) => post('/api/backups', o || {}),
  verifyBackup: (id, passphrase) => post(`/api/backups/${enc(id)}/verify`, passphrase ? { passphrase } : {}),
  restoreBackup: (id, passphrase) => post(`/api/backups/${enc(id)}/restore`, { confirm: true, ...(passphrase ? { passphrase } : {}) }),
  deleteBackup: (id) => del(`/api/backups/${enc(id)}`),
  pendingRestore: () => j('/api/backups/pending'),
  cancelPendingRestore: () => del('/api/backups/pending'),
  supportBundle: (includePrompts) => download('/api/support-bundle', {
    method: 'POST', headers: JSON_HEADERS, body: body({ include_prompts: !!includePrompts }) }),
  exportUser: (id) => download(`/api/users/${enc(id)}/export`),
  eraseUser: (id) => del(`/api/users/${enc(id)}`),
  people: () => j('/api/people'),

  // ---- plugins
  pluginManifests: () => j('/api/plugins/manifests'),
  setExtensionEnabled: (id, enabled, approve) => j(`/api/plugins/extensions/${enc(id)}`, {
    method: 'PUT', headers: JSON_HEADERS, body: body(approve ? { enabled, approve } : { enabled }) }),
};

/** Save a Blob the way a user would ("Save as"): an object URL and a click on a throwaway link. */
export function saveBlob(blob, name, doc = document) {
  const url = URL.createObjectURL(blob);
  const a = doc.createElement('a');
  a.href = url;
  a.download = name;
  a.rel = 'noopener';
  doc.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** The one sentence to show for a failed call: the envelope's fix beats a bare status line. */
export function errorText(e) {
  if (!e) return '';
  if (e.errors && typeof e.errors === 'object') {
    return Object.entries(e.errors).map(([k, v]) => `${k}: ${v}`).join('; ');
  }
  const msg = String(e.message || e).replace(/^\d{3}:\s*/, '');
  return e.fix && !msg.includes(e.fix) ? `${msg} ${e.fix}` : msg;
}
