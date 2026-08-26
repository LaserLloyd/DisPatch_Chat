// Thin REST client.

// Called when any non-auth request comes back 401 (the lock kicked in, e.g.
// the session idle-expired server-side). main.js registers a handler that
// shows the lock screen.
let onLocked = null;
export function setOnLocked(fn) { onLocked = fn; }

async function j(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let body = null;
    try { body = await r.json(); } catch {}
    // If the server has dropped us to Safe Mode (401 Locked, or a 403/flag that
    // says decoy/locked), tell the app to reconcile. Exclude /api/auth so a
    // wrong-PIN 401 doesn't recurse.
    const downgraded = r.status === 401 || (body && (body.locked || body.decoy));
    if (downgraded && onLocked && !url.startsWith('/api/auth')) {
      try { onLocked(); } catch {}
    }
    let detail = (body && (body.detail || body.error)) || r.statusText;
    // FastAPI HTTPException detail can be an object (some endpoints send
    // {errors:{key:msg}} or {detail, journal_tail}) — flatten it so toasts
    // never show "[object Object]".
    if (detail && typeof detail === 'object') {
      if (detail.errors && typeof detail.errors === 'object') {
        detail = Object.entries(detail.errors).map(([k, v]) => `${k}: ${v}`).join('; ');
      } else if (typeof detail.detail === 'string') {
        detail = detail.detail;
      } else {
        try { detail = JSON.stringify(detail); } catch { detail = String(detail); }
      }
    }
    const err = new Error(`${r.status}: ${detail}`);
    err.status = r.status;
    throw err;
  }
  return r.status === 204 ? null : r.json();
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

// Client-side ceiling on a single upload — mirrors the server's pre-buffer hard
// max so we never push a multi-GB body the server will just reject.
const UPLOAD_HARD_MAX = 4 * 1024 * 1024 * 1024;   // 4GB

// XHR-based upload so we get real upload-progress events (fetch cannot report
// them). Resolves with the parsed JSON body; rejects with an Error carrying
// `.status`, and triggers the lock handler on a 401/decoy downgrade (mirrors j()).
function xhrUpload(url, file, onProgress, fields) {
  return new Promise((resolve, reject) => {
    if (file.size > UPLOAD_HARD_MAX) {
      reject(new Error(`File too large (max ${Math.floor(UPLOAD_HARD_MAX / (1024 ** 3))}GB)`));
      return;
    }
    const fd = new FormData();
    fd.append('file', file);
    for (const [k, v] of Object.entries(fields || {})) {
      if (v != null) fd.append(k, v);
    }
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    if (onProgress && xhr.upload) {
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    }
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) { resolve(body); return; }
      const downgraded = xhr.status === 401 || (body && (body.locked || body.decoy));
      if (downgraded && onLocked && !url.startsWith('/api/auth')) {
        try { onLocked(); } catch {}
      }
      let detail = (body && (body.detail || body.error)) || xhr.statusText || 'Upload failed';
      if (detail && typeof detail === 'object') {
        try { detail = JSON.stringify(detail); } catch { detail = String(detail); }
      }
      const err = new Error(`${xhr.status}: ${detail}`);
      err.status = xhr.status;
      reject(err);
    };
    xhr.onerror = () => reject(new Error('Network error during upload'));
    xhr.onabort = () => reject(new Error('Upload aborted'));
    xhr.send(fd);
  });
}

export const api = {
  health: () => j('/api/health'),

  // Auth / lock
  authStatus: () => j('/api/auth/status'),
  unlock: (pin, remember) => j('/api/auth/unlock', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ pin, remember: !!remember }) }),
  lock: () => j('/api/auth/lock', { method: 'POST' }),
  rememberConfig: (days) => j('/api/auth/remember-config', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ days }) }),
  forgetDevices: () => j('/api/auth/forget-devices', { method: 'POST' }),
  setupPin: (newPin, currentPin) => j('/api/auth/setup', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ new_pin: newPin, current_pin: currentPin }) }),
  recover: (code) => j('/api/auth/recover', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ code }) }),

  bots: () => j('/api/bots'),
  allBots: () => j('/api/bots/all'),
  saveOrder: (bots) => j('/api/bots/order', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ bots }) }),

  threads: (botId) => j(`/api/threads?bot_id=${encodeURIComponent(botId)}`),
  createThread: (botId) => j('/api/threads', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ bot_id: botId }) }),
  messages: (tid, beforeId) => j(`/api/threads/${tid}/messages?limit=200${beforeId ? `&before_id=${beforeId}` : ''}`),
  rename: (tid, title) => j(`/api/threads/${tid}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify({ title }) }),
  pin: (tid, pinned) => j(`/api/threads/${tid}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify({ pinned }) }),
  archive: (tid) => j(`/api/threads/${tid}`, { method: 'DELETE' }),
  remove: (tid) => j(`/api/threads/${tid}?hard=true`, { method: 'DELETE' }),

  deleteMessage: (mid) => j(`/api/messages/${mid}`, { method: 'DELETE' }),
  // Takes either a row op ({index, checked, list}) or a whole array. The row
  // op is what the widget sends: the server merges it, so a request cannot
  // carry a stale view of rows it does not mention.
  updateChecklist: (mid, body) => j(`/api/messages/${mid}/checklist`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(Array.isArray(body) ? { checked: body } : body) }),

  upload: (file, onProgress) => xhrUpload('/api/upload', file, onProgress),

  // One-way drop — the locked side's own upload path. Stores the file and posts
  // a text-only notice into `threadId` (or the default safe bot's daily thread).
  drop: (file, threadId, onProgress) =>
    xhrUpload('/api/drop', file, onProgress, { thread_id: threadId }),

  uploadAvatar: (botId, file, cropX, cropY, cropSize) => {
    const fd = new FormData();
    fd.append('file', file);
    const q = new URLSearchParams({ crop_x: cropX, crop_y: cropY, crop_size: cropSize });
    return j(`/api/bots/${encodeURIComponent(botId)}/avatar?${q}`, { method: 'POST', body: fd });
  },

  markRead: (tid) => j(`/api/threads/${tid}/read`, { method: 'POST' }),
  unread: () => j('/api/unread'),

  // Search · recovery · transcript bridge
  search: (q, botId) => j(`/api/search?q=${encodeURIComponent(q)}${botId ? `&bot_id=${encodeURIComponent(botId)}` : ''}`),
  recoverThread: (tid) => j('/api/recover/transcript', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ thread_id: tid }) }),
  recoverAll: () => j('/api/recover/transcript', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ all: true }) }),
  ocSessions: (botId) => j(`/api/openclaw/sessions?bot_id=${encodeURIComponent(botId)}`),
  ocTranscript: ({ botId, threadId, sessionKey }) => {
    const p = new URLSearchParams({ bot_id: botId });
    if (threadId) p.set('thread_id', threadId);
    if (sessionKey) p.set('session_key', sessionKey);
    return j(`/api/openclaw/transcript?${p}`);
  },
  importSession: (botId, sessionKey, title) => j('/api/openclaw/import', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ bot_id: botId, session_key: sessionKey, title }) }),
  exportUrl: (fmt) => `/api/export?format=${encodeURIComponent(fmt)}`,

  files: () => j('/api/files'),
  uploadFile: (file, onProgress) => xhrUpload('/api/files', file, onProgress),
  deleteFile: (fid) => j(`/api/files/${fid}`, { method: 'DELETE' }),
  wipeFiles: (beforeIso) => j('/api/files/wipe', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ before: beforeIso }) }),

  // Reaction images (ephemeral overlay pack)
  reactions: () => j('/api/reactions'),
  fireReaction: (body) => j('/api/reactions/fire', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }),
  reactionImageUrl: (id) => `/api/reactions/${encodeURIComponent(id)}/image`,
  addReaction: (file, fields) => {
    const fd = new FormData();
    fd.append('file', file);
    for (const [k, v] of Object.entries(fields || {})) fd.append(k, v);
    return j('/api/reactions', { method: 'POST', body: fd });
  },
  generateReaction: (body) => j('/api/reactions/generate', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }),
  patchReaction: (id, body) => j(`/api/reactions/${encodeURIComponent(id)}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(body) }),
  deleteReaction: (id) => j(`/api/reactions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  reactionSettings: (values) => j('/api/reactions/settings', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ values }) }),
  reseedReactions: () => j('/api/reactions/reseed', { method: 'POST' }),
  reactionPool: (values) => j('/api/reactions/pool', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ values }) }),
  refillReactionPool: (replace) => j(`/api/reactions/pool/refill${replace ? '?replace=true' : ''}`, { method: 'POST' }),

  // Avatar pools (one-shot face/full pairs new threads draw)
  avatarPools: () => j('/api/avatar-pool'),
  avatarPoolConfig: (botId, values) => j(`/api/avatar-pool/${encodeURIComponent(botId)}`, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ values }) }),
  avatarPoolRefill: (botId) => j(`/api/avatar-pool/${encodeURIComponent(botId)}/refill`, { method: 'POST' }),
  avatarPoolPrompts: (botId) => j(`/api/avatar-pool/${encodeURIComponent(botId)}/prompts`),
  saveAvatarPoolPrompts: (botId, prompts) => j(`/api/avatar-pool/${encodeURIComponent(botId)}/prompts`, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ prompts }) }),

  // Coding terminal (full-session only)
  terminalStatus: () => j('/api/terminal/status'),
  terminalAction: (action) => j(`/api/terminal/${action}`, { method: 'POST' }),
  terminalOptions: (opts) => j('/api/terminal/options', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(opts) }),
  terminalModels: () => j('/api/terminal/models'),
  // DeepSeek Harness (full-session only)
  harnessStatus: () => j('/api/harness/status'),
  harnessAction: (action) => j(`/api/harness/${action}`, { method: 'POST' }),
  harnessSetModel: (provider, model) => j('/api/harness/model', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ provider, model }) }),
  harnessJobs: () => j('/api/harness/jobs'),
  harnessSubmitJob: (task, cwd) => j('/api/harness/jobs', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ task, cwd }) }),
  harnessCancelJob: () => j('/api/harness/jobs/cancel', { method: 'POST' }),
};
