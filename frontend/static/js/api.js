// Thin REST client.

// Called when any non-auth request comes back 401 (the lock kicked in, e.g.
// the session idle-expired server-side). main.js registers a handler that
// shows the lock screen.
let onLocked = null;
export function setOnLocked(fn) { onLocked = fn; }

// 3.0: a signed-in profile can be refused for POLICY rather than for lack of a
// session -- its daily time is used up (`time_limit`) or it is inside its quiet
// hours (`quiet_hours`). Those arrive as 403s with error.code set. main.js
// registers a handler that swaps the broken UI for a friendly full-screen
// notice; it is called with (code, err) for every such failure, any URL.
let onPolicyBlock = null;
export function setOnPolicyBlock(fn) { onPolicyBlock = fn; }
export const POLICY_BLOCK_CODES = ['time_limit', 'quiet_hours'];

// security.yaml `anonymous_access: none`: until somebody signs in, every API
// call except the public auth/health/static ones answers 401 with error.code
// "signin_required". That is "show the sign-in screen", not an error to
// toast, and not the session-expired path either (onLocked would drop to
// Restricted, which this install does not have).
let onSignInRequired = null;
export function setOnSignInRequired(fn) { onSignInRequired = fn; }

/** Pull the machine-readable part out of a failed response body (3.0 envelope:
 *  `{detail, error:{code,message,fix,docs}}`, plus `retry_after` on a 429 and
 *  `required_permission` on a permission_denied). Pure; exported for tests. */
export function errorExtras(body, headers) {
  const out = {};
  const env = body && typeof body.error === 'object' && body.error ? body.error : null;
  if (env) {
    if (typeof env.code === 'string') out.code = env.code;
    if (typeof env.fix === 'string' && env.fix) out.fix = env.fix;
    if (typeof env.docs === 'string' && env.docs) out.docs = env.docs;
    if (typeof env.message === 'string' && env.message) out.userMessage = env.message;
  }
  let retry = body && (body.retry_after ?? (env && env.retry_after));
  if ((retry == null || retry === '') && headers && typeof headers.get === 'function') {
    retry = headers.get('Retry-After');
  }
  const secs = Math.ceil(Number(retry));
  if (Number.isFinite(secs) && secs > 0) out.retryAfter = secs;
  if (body && typeof body.required_permission === 'string') out.requiredPermission = body.required_permission;
  else if (env && typeof env.required_permission === 'string') out.requiredPermission = env.required_permission;
  return out;
}

// Nothing here may hang forever.
//
// This wrapper had no timeout at all — no AbortController, no ceiling —
// while i18n.js time-boxes its own fetch to 4s and the WebSocket has a
// backoff. That asymmetry is what turned "the network is still settling"
// on an Android resume into "the app sits in its pre-lock state", because
// the two calls that decide whether a device is locked both come through
// here. A request that cannot finish has to become a request that failed,
// so the caller can fail CLOSED rather than wait.
//
// Generous, because some of these are real work (uploads, a board fetch on a
// slow tailnet hop). The auth path passes its own, much shorter one.
const DEFAULT_TIMEOUT_MS = 20000;

async function j(url, opts = {}) {
  // A caller-supplied signal wins: the Job Board aborts superseded requests
  // and must keep that control.
  let signal = opts.signal;
  let timer = null;
  if (!signal && typeof AbortController === 'function') {
    const ctl = new AbortController();
    signal = ctl.signal;
    const ms = opts.timeoutMs || DEFAULT_TIMEOUT_MS;
    timer = setTimeout(() => ctl.abort(), ms);
  }
  const { timeoutMs: _ignored, ...fetchOpts } = opts;
  let r;
  try {
    r = await fetch(url, { ...fetchOpts, signal });
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (!r.ok) {
    let body = null;
    try { body = await r.json(); } catch {}
    // If the server has dropped us to Safe Mode (401 Locked, or a 403/flag that
    // says decoy/locked), tell the app to reconcile. Exclude /api/auth so a
    // wrong-PIN 401 doesn't recurse.
    const extras = errorExtras(body, r.headers);
    const signinRequired = extras.code === 'signin_required';
    const downgraded = !signinRequired && (r.status === 401 || (body && (body.locked || body.decoy)));
    if (downgraded && onLocked && !url.startsWith('/api/auth')) {
      try { onLocked(); } catch {}
    }
    if (signinRequired && onSignInRequired) {
      try { onSignInRequired(); } catch {}
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
      } else if (typeof detail.message === 'string') {
        detail = detail.message;
      } else {
        try { detail = JSON.stringify(detail); } catch { detail = String(detail); }
      }
    }
    const err = new Error(`${r.status}: ${detail}`);
    err.status = r.status;
    // The parsed body rides along for callers that need more than a message —
    // Settings → Plugins reads a 422's offending row index out of it.
    err.body = body;
    // 3.0: surface error.code / error.fix / retry_after on the Error itself.
    // `detail` (and so err.message) is unchanged for every existing caller.
    Object.assign(err, extras);
    if (signinRequired) err.signInRequired = true;
    if (err.code && POLICY_BLOCK_CODES.includes(err.code) && onPolicyBlock) {
      try { onPolicyBlock(err.code, err); } catch {}
    }
    throw err;
  }
  return r.status === 204 ? null : r.json();
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

// EVERY value interpolated into a path below goes through encodeURIComponent,
// with no exceptions for "ids we generate ourselves". Half of these already
// did (uploadAvatar, the reaction and avatar-pool routes) and half did not,
// which is the shape of an invariant nobody can check by eye: an id is one
// path SEGMENT, and a value carrying `/`, `?` or `..` re-points the request at
// a different endpoint. Query values use encodeURIComponent or URLSearchParams
// for the same reason.

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
      Object.assign(err, errorExtras(body, null));
      if (err.code && POLICY_BLOCK_CODES.includes(err.code) && onPolicyBlock) {
        try { onPolicyBlock(err.code, err); } catch {}
      }
      reject(err);
    };
    xhr.onerror = () => reject(new Error('Network error during upload'));
    xhr.onabort = () => reject(new Error('Upload aborted'));
    xhr.send(fd);
  });
}

// Drop undefined keys so an optional field is ABSENT from the body rather than
// sent as null (several of these endpoints treat "absent" and "null" apart).
function compact(o) {
  const out = {};
  for (const [k, v] of Object.entries(o)) if (v !== undefined) out[k] = v;
  return out;
}

/** Body for POST /api/auth/login and /api/auth/switch. `profile` is an id, or
 *  an object {id} / {name}. Only the secret the caller actually has is sent.
 *  Pure; exported for tests. */
export function loginBody(profile, opts = {}) {
  const o = opts || {};
  const body = {};
  if (profile && typeof profile === 'object') {
    if (profile.id) body.profile_id = profile.id;
    else if (profile.name) body.name = profile.name;
  } else if (profile) {
    body.profile_id = String(profile);
  }
  if (o.name && !body.profile_id) body.name = o.name;
  if (o.pin) body.pin = o.pin;
  if (o.password) body.password = o.password;
  if (o.remember) body.remember = true;
  return body;
}

export const api = {
  health: () => j('/api/health'),

  // Auth / lock
  // Short, because this is the call that decides whether a device is locked.
  // A slow answer here is not worth waiting for: the caller treats a failure
  // as "locked" and re-checks, which is the safe direction and is far better
  // than leaving an unlocked view on screen while a request hangs.
  authStatus: () => j('/api/auth/status', { timeoutMs: 5000 }),
  unlock: (pin, remember) => j('/api/auth/unlock', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ pin, remember: !!remember }) }),
  lock: () => j('/api/auth/lock', { method: 'POST' }),
  rememberConfig: (days) => j('/api/auth/remember-config', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ days }) }),
  forgetDevices: () => j('/api/auth/forget-devices', { method: 'POST' }),
  setupPin: (newPin, currentPin) => j('/api/auth/setup', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ new_pin: newPin, current_pin: currentPin }) }),
  recover: (code) => j('/api/auth/recover', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ code }) }),

  // ---- 3.0 profiles, roles and policy -----------------------------------
  // The session cookie is HttpOnly; nothing here ever reads it. Credentials go
  // in the body of the call that checks them and nowhere else.
  me: () => j('/api/auth/me', { timeoutMs: 5000 }),
  login: (profile, opts = {}) => j('/api/auth/login', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(loginBody(profile, opts)) }),
  switchProfile: (profile, opts = {}) => j('/api/auth/switch', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(loginBody(profile, opts)) }),
  // First run: creates the Owner and signs in. `setupToken` is only needed
  // when the install is reached from the network (see status.setup_required).
  setupOwner: ({ name, password, newPin, setupToken } = {}) => j('/api/auth/setup', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(compact({ name, password, new_pin: newPin, setup_token: setupToken })) }),
  changePassword: ({ current, currentPin, newPassword } = {}) => j('/api/auth/password', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(compact({ current, current_pin: currentPin, new: newPassword })) }),
  apiTokenInfo: () => j('/api/auth/api-token'),
  rotateApiToken: () => j('/api/auth/api-token', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ rotate: true }) }),
  setMachineAccess: ({ machineLoopbackTrust, machineScopes } = {}) => j('/api/auth/api-token', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(compact({ machine_loopback_trust: machineLoopbackTrust, machine_scopes: machineScopes })) }),

  people: () => j('/api/people'),
  createPerson: (body) => j('/api/people', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body || {}) }),
  patchPerson: (id, patch) => j(`/api/people/${encodeURIComponent(id)}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(patch || {}) }),
  deletePerson: (id) => j(`/api/people/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  setPersonPin: (id, pin) => j(`/api/people/${encodeURIComponent(id)}/pin`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ pin }) }),
  clearPersonPin: (id) => j(`/api/people/${encodeURIComponent(id)}/pin`, { method: 'DELETE' }),
  setPersonPassword: (id, password) => j(`/api/people/${encodeURIComponent(id)}/password`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ password }) }),
  signOutPerson: (id) => j(`/api/people/${encodeURIComponent(id)}/sign-out`, { method: 'POST' }),

  devices: () => j('/api/people/devices'),
  // Binds the CALLING browser; profileId null unbinds it.
  bindThisDevice: (profileId, label) => j('/api/people/devices/this', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(compact({ profile_id: profileId == null ? null : profileId, label })) }),
  forgetDevice: (id) => j(`/api/people/devices/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // Counts only, never message content.
  peopleActivity: ({ profileId, days } = {}) => {
    const q = new URLSearchParams();
    if (profileId) q.set('profile_id', profileId);
    if (days) q.set('days', String(days));
    const qs = q.toString();
    return j(`/api/people/activity${qs ? `?${qs}` : ''}`);
  },
  peopleAudit: () => j('/api/people/audit'),
  myActivity: (days) => j(`/api/me/activity${days ? `?days=${encodeURIComponent(days)}` : ''}`),

  bots: () => j('/api/bots'),
  // Quick actions in use (public; Safe Mode too): {actions: [{id, label, hint, tools}]}.
  quickActions: () => j('/api/quick-actions'),
  allBots: () => j('/api/bots/all'),
  // Per-bot editing (docs/design/platform-2-3.md section 3). Operator only.
  addBot: (body) => j('/api/bots', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }),
  patchBot: (id, patch) => j(`/api/bots/${encodeURIComponent(id)}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(patch) }),
  deleteBot: (id) => j(`/api/bots/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  saveOrder: (bots) => j('/api/bots/order', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ bots }) }),

  threads: (botId) => j(`/api/threads?bot_id=${encodeURIComponent(botId)}`),
  // One thread's row (title, avatar_snapshot, bot_id). Used when a thread is
  // opened from outside its bot's list (an app pane handing a thread over).
  thread: (tid) => j(`/api/threads/${encodeURIComponent(tid)}`),
  createThread: (botId) => j('/api/threads', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ bot_id: botId }) }),
  messages: (tid, beforeId) => j(`/api/threads/${encodeURIComponent(tid)}/messages?limit=200${beforeId ? `&before_id=${encodeURIComponent(beforeId)}` : ''}`),
  rename: (tid, title) => j(`/api/threads/${encodeURIComponent(tid)}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify({ title }) }),
  pin: (tid, pinned) => j(`/api/threads/${encodeURIComponent(tid)}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify({ pinned }) }),
  archive: (tid) => j(`/api/threads/${encodeURIComponent(tid)}`, { method: 'DELETE' }),
  remove: (tid) => j(`/api/threads/${encodeURIComponent(tid)}?hard=true`, { method: 'DELETE' }),
  // Per-thread model/thinking override (the header model chip). `prefs` is
  // MERGED server-side (db.update_thread_prefs) — a null value removes that
  // key, an absent one leaves it alone. Operator-only; a decoy or
  // machine-inbound caller 403s (see main._require_operator_session).
  setThreadPrefs: (tid, prefs) => j(`/api/threads/${encodeURIComponent(tid)}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify({ prefs }) }),
  // The models this bot is ALLOWED to use, for that same picker. Operator-only.
  botModels: (botId) => j(`/api/bots/${encodeURIComponent(botId)}/models`),

  deleteMessage: (mid) => j(`/api/messages/${encodeURIComponent(mid)}`, { method: 'DELETE' }),
  // {content} rewrites the text, {hidden} takes the row out of an API
  // bot's context without removing it from the transcript. Either may be
  // sent alone; the server merges, so one never clears the other.
  editMessage: (mid, body) => j(`/api/messages/${encodeURIComponent(mid)}`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(body) }),
  // Advisor handoff cards (Send / Dismiss). Full session only, server-side.
  // `mid` is the card's own message id; the server refuses any other message.
  advisorSend: (rid, mid) => j(`/api/advisor/requests/${encodeURIComponent(rid)}/send`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ message_id: mid }) }),
  advisorDismiss: (rid, mid) => j(`/api/advisor/requests/${encodeURIComponent(rid)}/dismiss`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ message_id: mid }) }),
  // Takes either a row op ({index, checked, list}) or a whole array. The row
  // op is what the widget sends: the server merges it, so a request cannot
  // carry a stale view of rows it does not mention.
  updateChecklist: (mid, body) => j(`/api/messages/${encodeURIComponent(mid)}/checklist`, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(Array.isArray(body) ? { checked: body } : body) }),
  // Thumbs feedback on one of the bot's own replies. `body` is {vote} or
  // {vote, reason} — both closed enums server-side (Pydantic Literal), so a
  // caller can never smuggle free text through this call.
  messageFeedback: (mid, body) => j(`/api/messages/${encodeURIComponent(mid)}/feedback`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }),

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

  markRead: (tid) => j(`/api/threads/${encodeURIComponent(tid)}/read`, { method: 'POST' }),
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
  deleteFile: (fid) => j(`/api/files/${encodeURIComponent(fid)}`, { method: 'DELETE' }),
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

  // Local viewer roots (full-session only; the server 403s these in Safe Mode).
  // The viewer's own stat/ls calls do NOT come through here — viewer.js fetches
  // them itself so the module stays usable without importing the app's client.
  localRoots: () => j('/api/local/roots'),
  saveLocalConfig: (body) => j('/api/local/config', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(body) }),

  // DeepSeek Harness (full-session only)
  harnessStatus: () => j('/api/harness/status'),
  harnessAction: (action) => j(`/api/harness/${encodeURIComponent(action)}`, { method: 'POST' }),
  harnessSetModel: (provider, model) => j('/api/harness/model', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ provider, model }) }),
  harnessJobs: () => j('/api/harness/jobs'),
  harnessSubmitJob: (task, cwd) => j('/api/harness/jobs', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ task, cwd }) }),
  harnessCancelJob: () => j('/api/harness/jobs/cancel', { method: 'POST' }),
  // Live sessions: several at once, readable while they run, gone when stopped.
  harnessSessions: () => j('/api/harness/sessions'),
  harnessSessionLaunch: (task, cwd, model) => j('/api/harness/sessions', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ task, cwd, model }) }),
  harnessSession: (id, after) => j(`/api/harness/sessions/${encodeURIComponent(id)}?after=${Number(after) || 0}`),
  harnessSessionStop: (id) => j(`/api/harness/sessions/${encodeURIComponent(id)}/stop`, { method: 'POST' }),
  harnessSessionDismiss: (id) => j(`/api/harness/sessions/${encodeURIComponent(id)}/dismiss`, { method: 'POST' }),

  // StudioForge control panel (full-session only). Read-only: the address of
  // the rig's panel plus whether the SERVER could reach it. There is no other
  // route — DisPatch never manages the rig.
  studioforgeStatus: () => j('/api/studioforge/status'),

  // Emails tab (full-session only): MailForge dashboard reachability + the
  // one-time launch URL, when the service is actually up. See
  // backend/app/mailforge_bridge.py.
  mailStatus: () => j('/api/mail/status'),

  // Clients tab (full-session only): thin passthrough onto the practice box's
  // client-pipeline API (docs/GUI-PLAN.md §2), proxied server-side. `path` is
  // the practice-side path with no leading slash, e.g. "board",
  // "clients/c1/actions/build". See backend/app/practice_bridge.py.
  practiceGet: (path) => j(`/api/practice/${path}`),
  practicePost: (path, body) => j(`/api/practice/${path}`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body || {}) }),

  // Clients tab "site links": a DisPatch-local overlay (never proxied to the
  // practice box, and NOT under /api/practice/*) that records where a
  // client's build actually ended up once handed off — live URL, repo/notes
  // free text. See backend/app/client_links.py.
  practiceLinksGet: (clientId) => j(`/api/practice-links/${encodeURIComponent(clientId)}`),
  practiceLinksPut: (clientId, body) => j(`/api/practice-links/${encodeURIComponent(clientId)}`, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(body || {}) }),

  // Tools (docs/design/2026-09-25-tools-plugins.md). The list is filtered
  // server-side for Safe Mode; refresh and the write are operator-only.
  // Refresh runs a real command, so it gets the tool's own ceiling (max
  // 3600 s server-side) instead of the 20 s default.
  tools: () => j('/api/tools'),
  // Settings -> Plugins (app/plugins_api.py): the aggregate and its two switches.
  plugins: () => j('/api/plugins'),
  setPluginExtension: (id, enabled) => j(`/api/plugins/extensions/${encodeURIComponent(id)}`, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ enabled: !!enabled }) }),
  setPluginLivetools: (enabled) => j('/api/plugins/livetools', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ enabled: !!enabled }) }),
  toolStatus: (id) => j(`/api/tools/${encodeURIComponent(id)}/status`),
  toolRefresh: (id) => j(`/api/tools/${encodeURIComponent(id)}/refresh`, { method: 'POST', timeoutMs: 3600 * 1000 }),
  toolsSave: (list) => j('/api/tools', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ tools: list }) }),
};
