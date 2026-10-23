// Pure helpers for the profile model (4.0, "chat-first profiles"): three profile types,
// Admin, User and Basic, one of the Admins being the protected first Admin.
//
// No DOM, no fetch, no imports: everything here is a function of its arguments,
// so frontend/tests/profiles.test.js can drive it under plain node. The DOM
// halves live in people.js (Settings -> People) and signin.js (the sign-in
// tiles, first-run setup, the rail chip).
//
// NOTHING in this file is a security decision. The server enforces every role and
// permission; these helpers only decide what the UI OFFERS, so a User is not shown an
// admin tab that could only 403.

export const ROLES = ['admin', 'user', 'basic'];
const RANK = { basic: 0, user: 1, admin: 2 };

export const roleRank = (role) => (role in RANK ? RANK[role] : -1);

/** The role the UI acts as, from a /api/auth/status or /api/auth/me body. The server says
 *  admin | user | basic; anything else (a server that predates 4.0, a missing field) reads as
 *  the lowest type, so the UI never offers more than the server will allow. */
export function normalizeRole(s) {
  return s && typeof s.role === 'string' && s.role in RANK ? s.role : 'basic';
}

export const isAdminRole = (role) => role === 'admin';
export const isBasicRole = (role) => role === 'basic';

/** Mirrors the server's rule (people_api._may_manage): every Admin manages every profile except the
 *  protected first Admin (nobody may lock the install out). `target` is a profile row (or just a
 *  role word for a caller that has no row; then only the protected flag is unknown). */
export function canManage(actorRole, target) {
  const row = target && typeof target === 'object' ? target : { role: target };
  if (!isAdminRole(actorRole)) return false;
  return roleRank(row.role) >= 0 && !row.protected;
}

/** Profile types a create/edit form may offer. Only an Admin creates profiles at all; Admin
 *  accounts need a password of their own (the server answers 409 `password_required` for
 *  a protected Admin without one), so `adminBlocked` says why the option is missing and the form
 *  can explain it rather than silently omit it. */
export function roleOptions(actorRole, protectedHasPassword) {
  const options = ['user', 'basic'];
  let adminBlocked = null;
  if (!isAdminRole(actorRole)) adminBlocked = 'not_admin';
  else if (!protectedHasPassword) adminBlocked = 'owner_needs_password';
  else options.unshift('admin');
  return { options, adminBlocked };
}

// ---- Sign-in ---------------------------------------------------------------

/** What a lock-screen tile asks for. credential is "password" | "pin_or_password"
 *  | "pin" | "none". `modes` lists the inputs on offer, first = the default; a
 *  profile with no credential (the shared default) needs nothing at all and signs in at once. */
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

/** Should the sign-in overlay show profile tiles, or stay the 2.x PIN keypad?
 *  The keypad stays when the only profile is the protected Admin and the install has a PIN:
 *  there is nobody to pick and a PIN is what the keypad takes. (The server's profile list calls
 *  that Admin "pin_or_password" whether or not a password exists, so the PIN's existence is read
 *  from pin_set instead.) A protected Admin with no PIN gets the tile, because only a password
 *  can sign them in. An older server sends no `profiles` at all -> keypad. Accepts the raw status
 *  body (pin_set) or main.js's state.auth (pinSet). */
export function signInMode(status) {
  const profiles = status && Array.isArray(status.profiles) ? status.profiles : [];
  if (!status || !status.claimed || !profiles.length) return 'keypad';
  const pinSet = !!(status.pin_set ?? status.pinSet);
  const only = profiles.length === 1 ? profiles[0] : null;
  if (only && only.protected && (only.credential === 'pin' || (pinSet && only.credential === 'pin_or_password'))) return 'keypad';
  return 'tiles';
}

/** Tiles in display order: the shared default profile first (where a lock lands), then the
 *  protected Admin, then by type, then name. */
export function sortTiles(profiles) {
  const lead = (p) => (p.shared ? 2 : p.protected ? 1 : 0);
  return [...(profiles || [])].sort((a, b) =>
    (lead(b) - lead(a)) || (roleRank(b.role) - roleRank(a.role)) || String(a.name).localeCompare(String(b.name)));
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

// ---- Activity (counts only, never message content) -------------------------

export const ACTIVITY_COLUMNS = ['login', 'turn', 'thread', 'upload', 'feedback'];

/** summary.days -> rows newest first, at most `days` of them, with the five
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

// ---- Refusals --------------------------------------------------------------

/** A /ws error frame (`{type:'error', code?, error?}`) -> what the UI does:
 *  'denied' = a plain "you may not do that" toast, anything else stays a plain error. */
export function wsErrorKind(frame) {
  const code = frame && (frame.code || (frame.error && frame.error.code));
  if (code === 'permission_denied') return { kind: 'denied', code };
  return { kind: 'plain', code: code || null };
}

// ---- Characters ("bots") ----------------------------------------------------

/** Is this character the signed-in profile's own (private to it), or a household one? The server
 *  stamps `scope` on every character record it sends. */
export const isMineBot = (bot) => !!bot && bot.scope === 'personal';

/** The rail's two groups, Mine and Household, from the characters a profile may list. Household
 *  characters the profile owns a copy of (`cloned_from`) are left out: the copy stands for them, so
 *  the rail never shows the same name twice. (Their threads still open by link.) `bots` keeps its
 *  order; nothing is dropped from the data, only from the groups. */
export function railGroups(bots) {
  const list = Array.isArray(bots) ? bots : [];
  const copied = new Set(list.filter((b) => isMineBot(b) && b.cloned_from).map((b) => b.cloned_from));
  const mine = list.filter(isMineBot);
  const household = list.filter((b) => !isMineBot(b) && !copied.has(b.id));
  return { mine, household };
}
