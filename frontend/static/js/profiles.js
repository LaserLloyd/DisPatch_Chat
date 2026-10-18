// Pure helpers for DisPatch Chat 3.0 "profiles, roles and policy".
//
// No DOM, no fetch, no imports: everything here is a function of its arguments,
// so frontend/tests/people.test.js can drive it under plain node. The DOM
// halves live in people.js (Settings -> People & Profiles) and signin.js (the
// lock screen tiles, first-run setup, the Restricted banner).
//
// NOTHING in this file is a security decision. The server enforces every role,
// budget and permission; these helpers only decide what the UI OFFERS, so a
// Member is not shown an admin tab that could only 403.

export const ROLES = ['owner', 'admin', 'member', 'restricted'];
const RANK = { restricted: 0, member: 1, admin: 2, owner: 3 };

export const roleRank = (role) => (role in RANK ? RANK[role] : -1);

/** The role the UI should act as, from a /api/auth/status or /api/auth/me body.
 *  A server that predates 3.0 sends no `role`: derive it the way 2.x worked
 *  (decoy = locked/limited, otherwise the operator). */
export function normalizeRole(s) {
  if (s && typeof s.role === 'string' && s.role in RANK) return s.role;
  return s && s.decoy ? 'restricted' : 'owner';
}

export const isAdminRole = (role) => role === 'owner' || role === 'admin';
export const isRestrictedRole = (role) => role === 'restricted';

/** Mirrors the server's rule (people_api._may_manage): the Owner manages every
 *  other profile; an Admin manages Members and Restricted only. */
export function canManage(actorRole, targetRole) {
  if (actorRole === 'owner') return targetRole !== 'owner';
  if (actorRole === 'admin') return roleRank(targetRole) >= 0 && roleRank(targetRole) < RANK.admin;
  return false;
}

/** Roles a create/edit form may offer. Admin only for the Owner, and only once
 *  the Owner has a password of their own (the server answers 409
 *  `password_required` otherwise). `adminBlocked` says why it is missing so the
 *  form can explain rather than silently omit it. */
export function roleOptions(actorRole, ownerHasPassword) {
  const options = ['member', 'restricted'];
  let adminBlocked = null;
  if (actorRole !== 'owner') adminBlocked = 'not_owner';
  else if (!ownerHasPassword) adminBlocked = 'owner_needs_password';
  else options.unshift('admin');
  return { options, adminBlocked };
}

// ---- Sign-in ---------------------------------------------------------------

/** What a lock-screen tile asks for. credential is "password" | "pin_or_password"
 *  | "pin" | "none". `modes` lists the inputs on offer, first = the default; a
 *  Restricted profile with no PIN needs nothing at all and signs in at once. */
export function credentialModes(kind) {
  switch (kind) {
    case 'password': return ['password'];
    case 'pin_or_password': return ['password', 'pin'];
    case 'pin': return ['pin'];
    default: return [];
  }
}

/** Body options for api.login()/switchProfile(): only the secret in use. */
export function credentialOptions(mode, secret, remember = false) {
  const o = {};
  if (mode === 'pin' && secret) o.pin = secret;
  else if (mode === 'password' && secret) o.password = secret;
  if (remember) o.remember = true;
  return o;
}

/** Should the lock overlay show profile tiles, or stay the 2.x PIN keypad?
 *  The keypad stays when the only profile is the Owner and the install has a
 *  PIN: there is nobody to pick and a PIN is what the keypad takes. (The
 *  server's lock-screen list calls the Owner "pin_or_password" whether or not a
 *  password exists, so the PIN's existence is read from pin_set instead.) An
 *  Owner with no PIN gets the tile, because only a password can sign them in.
 *  An older server sends no `profiles` at all -> keypad. Accepts the raw
 *  status body (pin_set) or main.js's state.auth (pinSet). */
export function signInMode(status) {
  const profiles = status && Array.isArray(status.profiles) ? status.profiles : [];
  if (!status || !status.claimed || !profiles.length) return 'keypad';
  const pinSet = !!(status.pin_set ?? status.pinSet);
  const only = profiles.length === 1 ? profiles[0] : null;
  if (only && only.role === 'owner' && (only.credential === 'pin' || (pinSet && only.credential === 'pin_or_password'))) return 'keypad';
  return 'tiles';
}

/** Tiles in display order: Owner first, then by role, then name. */
export function sortTiles(profiles) {
  return [...(profiles || [])].sort((a, b) =>
    (roleRank(b.role) - roleRank(a.role)) || String(a.name).localeCompare(String(b.name)));
}

/** How to draw a profile's picture. `avatar` is server data: an image is only
 *  accepted as a same-origin path ("/..."), a short string is drawn as a glyph
 *  (an emoji), anything else falls back to the name's first letter. */
export function avatarSpec(profile) {
  const a = profile && typeof profile.avatar === 'string' ? profile.avatar.trim() : '';
  if (/^\/(?![/\\])[^\s"'<>]*$/.test(a)) return { kind: 'img', src: a };
  if (a && [...a].length <= 4) return { kind: 'glyph', text: a };
  const name = (profile && profile.name ? String(profile.name).trim() : '') || '?';
  return { kind: 'initial', text: [...name][0].toUpperCase() };
}

// ---- Throttle countdown ----------------------------------------------------

export function remainingSeconds(deadlineMs, nowMs) {
  return Math.max(0, Math.ceil((deadlineMs - nowMs) / 1000));
}

/** 75 -> "1:15", 9 -> "0:09". */
export function formatCountdown(secs) {
  const s = Math.max(0, Math.floor(secs));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** A 429 carries retry_after. Ticks once a second until it reaches zero.
 *  Timer functions are injectable so the tests need no real clock. */
export function createCountdown({ seconds, onTick, onDone, now = Date.now,
  setTimer = setInterval, clearTimer = clearInterval } = {}) {
  const deadline = now() + Math.max(0, seconds) * 1000;
  let id = null;
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    if (id != null) clearTimer(id);
    if (onDone) onDone();
  };
  const tick = () => {
    const left = remainingSeconds(deadline, now());
    if (onTick) onTick(left);
    if (left <= 0) finish();
  };
  if (seconds > 0) {
    tick();
    if (!finished) id = setTimer(tick, 1000);
  } else {
    finish();
  }
  return {
    deadline,
    stop() { finished = true; if (id != null) clearTimer(id); id = null; },
  };
}

// ---- Budgets ---------------------------------------------------------------

const COUNT_KEYS = { turns: 'turns', threads: 'threads', feedback: 'feedback', uploadMb: 'upload_mb' };

export const EMPTY_BUDGET_FORM = Object.freeze({
  minutes: '', quietStart: '', quietEnd: '', turns: '', threads: '', feedback: '', uploadMb: '', sessionMinutes: '',
});

/** "20:30-07:00" -> {start:"20:30", end:"07:00"}; anything else -> empty pair. */
export function parseQuietHours(s) {
  const m = /^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$/.exec(String(s || ''));
  if (!m) return { start: '', end: '' };
  const pad = (h, mm) => `${String(h).padStart(2, '0')}:${mm}`;
  return { start: pad(m[1], m[2]), end: pad(m[3], m[4]) };
}

export function formatQuietHours(start, end) {
  return start && end ? `${start}-${end}` : '';
}

/** A profile row (GET /api/people) -> the strings the edit form shows. */
export function budgetsToForm(person) {
  const b = (person && person.budgets) || {};
  const q = parseQuietHours(b.quiet_hours);
  const str = (v) => (Number.isInteger(v) ? String(v) : '');
  const secs = person && Number.isInteger(person.session_timeout) ? person.session_timeout : null;
  return {
    minutes: str(b.minutes_per_day),
    quietStart: q.start,
    quietEnd: q.end,
    turns: str(b.turns),
    threads: str(b.threads),
    feedback: str(b.feedback),
    uploadMb: str(b.upload_mb),
    sessionMinutes: secs ? String(Math.round(secs / 60)) : '',
  };
}

const wholeNumber = (v) => /^\d+$/.test(String(v).trim());

/** The edit form -> the API payload. An empty field means "the install
 *  default", so it is ABSENT from `budgets`. Returns {budgets, session_timeout,
 *  errors} where errors maps a form field to a short code the UI turns into a
 *  localized message; when errors is non-empty nothing should be sent. */
export function formToBudgets(form) {
  const f = { ...EMPTY_BUDGET_FORM, ...(form || {}) };
  const budgets = {};
  const errors = {};
  const mins = String(f.minutes).trim();
  if (mins !== '') {
    if (!wholeNumber(mins) || Number(mins) < 1 || Number(mins) > 1440) errors.minutes = 'range_1_1440';
    else budgets.minutes_per_day = Number(mins);
  }
  for (const [field, key] of Object.entries(COUNT_KEYS)) {
    const v = String(f[field]).trim();
    if (v === '') continue;
    if (!wholeNumber(v) || Number(v) > 100000) errors[field] = 'range_0_100000';
    else budgets[key] = Number(v);
  }
  const hasStart = !!f.quietStart;
  const hasEnd = !!f.quietEnd;
  if (hasStart !== hasEnd) errors.quiet = 'both_times';
  else if (hasStart) {
    if (f.quietStart === f.quietEnd) errors.quiet = 'same_times';
    else budgets.quiet_hours = formatQuietHours(f.quietStart, f.quietEnd);
  }
  let session_timeout;
  const sm = String(f.sessionMinutes).trim();
  if (sm !== '') {
    // The server accepts 60 s .. 30 days.
    if (!wholeNumber(sm) || Number(sm) < 1 || Number(sm) > 43200) errors.sessionMinutes = 'range_1_43200';
    else session_timeout = Number(sm) * 60;
  }
  return { budgets, session_timeout, errors };
}

/** Body for POST /api/people. Throws nothing: returns {body, errors}. */
export function createPayload(form) {
  const errors = {};
  const name = String(form.name || '').trim();
  if (!name) errors.name = 'required';
  const role = form.role;
  const body = { name, role };
  if (role === 'member') {
    if (!form.pin || String(form.pin).length < 4) errors.pin = 'min_4';
    else body.pin = String(form.pin);
  } else if (role === 'restricted') {
    if (form.pin) {
      if (String(form.pin).length < 4) errors.pin = 'min_4'; else body.pin = String(form.pin);
    }
  } else if (role === 'admin') {
    if (!form.password || String(form.password).length < 8) errors.password = 'min_8';
    else body.password = String(form.password);
  }
  if (form.avatar) body.avatar = String(form.avatar).trim();
  const b = formToBudgets(form);
  Object.assign(errors, b.errors);
  if (Object.keys(b.budgets).length) body.budgets = b.budgets;
  if (b.session_timeout) body.session_timeout = b.session_timeout;
  return { body, errors };
}

/** Body for PATCH /api/people/{id}: only what changed against `person`. */
export function patchPayload(person, form) {
  const errors = {};
  const patch = {};
  const name = String(form.name || '').trim();
  if (!name) errors.name = 'required';
  else if (name !== person.name) patch.name = name;
  if (form.role && form.role !== person.role && person.role !== 'owner') patch.role = form.role;
  const avatar = String(form.avatar || '').trim();
  if (avatar !== (person.avatar || '')) patch.avatar = avatar;
  const b = formToBudgets(form);
  Object.assign(errors, b.errors);
  const before = JSON.stringify(sortedEntries(person.budgets || {}));
  if (JSON.stringify(sortedEntries(b.budgets)) !== before) patch.budgets = b.budgets;
  if (b.session_timeout && b.session_timeout !== person.session_timeout) patch.session_timeout = b.session_timeout;
  if (typeof form.disabled === 'boolean' && form.disabled !== !!person.disabled) patch.disabled = form.disabled;
  return { patch, errors };
}

function sortedEntries(o) {
  return Object.entries(o).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

// ---- Activity (counts only, never message content) -------------------------

export const ACTIVITY_COLUMNS = ['login', 'turn', 'thread', 'upload', 'feedback', 'limit_hit'];

/** summary.days -> rows newest first, at most `days` of them, with the six
 *  columns filled (0 where absent) and a total. */
export function activityRows(summary, days = 7) {
  const list = summary && Array.isArray(summary.days) ? summary.days : [];
  return [...list]
    .sort((a, b) => (a.day < b.day ? 1 : a.day > b.day ? -1 : 0))
    .slice(0, days)
    .map((d) => {
      const counts = {};
      let total = 0;
      for (const c of ACTIVITY_COLUMNS) {
        const n = Number(d.counts && d.counts[c]) || 0;
        counts[c] = n;
        total += n;
      }
      return { day: d.day, counts, total };
    });
}

export function activityTotals(rows) {
  const totals = Object.fromEntries(ACTIVITY_COLUMNS.map((c) => [c, 0]));
  for (const r of rows) for (const c of ACTIVITY_COLUMNS) totals[c] += r.counts[c];
  return totals;
}

/** Today's usage against the limits, for the Restricted profile's own panel.
 *  `today` is the server's usage_daily map (active_seconds, upload_bytes,
 *  action:turn ...); `limits` its budgets. A line with no limit still shows
 *  what was used. */
export function usageLines(today, limits) {
  const u = today || {};
  const l = limits || {};
  const pick = (...keys) => { for (const k of keys) if (Number.isFinite(u[k])) return u[k]; return 0; };
  const lines = [
    { key: 'minutes', used: Math.floor(pick('active_seconds') / 60), limit: Number.isInteger(l.minutes_per_day) ? l.minutes_per_day : null },
    { key: 'turns', used: pick('action:turn', 'turn', 'turns'), limit: Number.isInteger(l.turns) ? l.turns : null },
    { key: 'threads', used: pick('action:thread', 'thread', 'threads'), limit: Number.isInteger(l.threads) ? l.threads : null },
    { key: 'feedback', used: pick('action:feedback', 'feedback'), limit: Number.isInteger(l.feedback) ? l.feedback : null },
    { key: 'upload_mb', used: Math.round(pick('upload_bytes') / (1024 * 1024) * 10) / 10, limit: Number.isInteger(l.upload_mb) ? l.upload_mb : null },
  ];
  return lines.map((x) => ({ ...x, over: x.limit != null && x.limit > 0 && x.used >= x.limit }));
}

// ---- Policy refusals --------------------------------------------------------

/** 403 error.code values that mean "this profile may not use the app right
 *  now" rather than "you lack a permission". Value = locale key stem. */
export const BLOCK_NOTICES = {
  time_limit: 'notice.time_limit',
  quiet_hours: 'notice.quiet_hours',
};

export const blockNoticeKey = (code) => BLOCK_NOTICES[code] || null;

/** A /ws error frame (`{type:'error', code?, error?}`) -> what the UI does.
 *  'safe_mode' = the session is Restricted, 'permission_denied' = a plain
 *  refusal toast, time_limit/quiet_hours = the full-screen notice. */
export function wsErrorKind(frame) {
  const code = frame && (frame.code || (frame.error && frame.error.code));
  if (code === 'time_limit' || code === 'quiet_hours') return { kind: 'notice', code };
  if (code === 'safe_mode') return { kind: 'restricted', code };
  if (code === 'permission_denied') return { kind: 'denied', code };
  return { kind: 'plain', code: code || null };
}

// ---- Device binding ---------------------------------------------------------

/** Profiles a browser can be opened as: Members and Restricted only (the
 *  server refuses to bind an Admin or the Owner), never a disabled one. */
export function bindableProfiles(profiles) {
  return (profiles || []).filter((p) => (p.role === 'member' || p.role === 'restricted') && !p.disabled);
}

// ---- Characters ("bots") ----------------------------------------------------

/** Who sees a character: 'all' | 'members' | 'restricted'. Derived from the two
 *  switches (safe = "Show to Restricted profiles", hide_unlocked = "not on the
 *  Owner's rail") so the caption follows a click before Save; falls back to the
 *  server's own `audience` when the flags are absent. restricted = safe &&
 *  hide_unlocked, all = safe && !hide_unlocked, members = !safe. */
export function audienceOf(bot) {
  if (bot && typeof bot.safe === 'boolean') {
    if (!bot.safe) return 'members';
    return bot.hide_unlocked ? 'restricted' : 'all';
  }
  const a = bot && bot.audience;
  return a === 'all' || a === 'members' || a === 'restricted' ? a : 'members';
}
