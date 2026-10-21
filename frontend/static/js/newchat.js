// The phone "New chat" tab: which character gets the new thread.
//
// No DOM, no fetch, no imports, so frontend/tests/newchat.test.js drives it
// under plain node. NOT a security decision: the server still checks the
// profile (POST /api/threads is use_chat, quota-charged for Restricted);
// this only decides what the tab OFFERS and where a tap goes.

/** May this session start a chat? `permissions` is /api/auth/status
 *  `permissions` (["*"] on an open install). A server that predates the field
 *  sends none: offer the tab, exactly as the "+" button is always offered. */
export function newChatAllowed(permissions) {
  if (!Array.isArray(permissions)) return true;
  return permissions.includes('*') || permissions.includes('use_chat');
}

/** The character a new chat goes to, in order:
 *   1. `currentId`: the character on screen (the open thread's, else the
 *      selected one), if the profile may use it. `usableIds` is the server's
 *      roster for this profile, NOT the rail: a character hidden on this device
 *      (a push deep link, an app pane's thread) is still the one being used.
 *   2. `lastId`: the character this device used last (see rememberCharacter);
 *   3. the character whose newest thread has the greatest updated_at among
 *      `recentIds` (what this device lists, in rail order; ties keep the first),
 *      from `latest` (id -> ISO string). `attempted`/`failed` count the lookups
 *      behind `latest`: all failed = offline, some failed = do not guess;
 *   4. none: the caller opens the character picker.
 *  Returns { botId, via: 'current' | 'last' | 'recent' | 'picker' | 'offline' }. */
export function pickNewChatTarget({ currentId, usableIds, lastId, recentIds, latest, attempted = 0, failed = 0 }) {
  const usable = Array.isArray(usableIds) ? usableIds : [];
  if (currentId && usable.includes(currentId)) return { botId: currentId, via: 'current' };
  if (lastId && usable.includes(lastId)) return { botId: lastId, via: 'last' };
  if (attempted > 0 && failed >= attempted) return { botId: null, via: 'offline' };
  if (failed > 0) return { botId: null, via: 'picker' };
  let best = null;
  let bestAt = '';
  for (const id of (Array.isArray(recentIds) ? recentIds : [])) {
    if (!usable.includes(id)) continue;
    const at = (latest && latest[id]) || '';
    if (at && at > bestAt) { best = id; bestAt = at; }
  }
  return best ? { botId: best, via: 'recent' } : { botId: null, via: 'picker' };
}

/** The character "on screen": the open thread's when the chat view shows one
 *  (a deep link or an app pane opens a thread without selecting its character),
 *  else the selected one. */
export function currentCharacter({ view, activeBotId, selectedId }) {
  return (view === 'chat' && activeBotId) || selectedId || null;
}

/** The newest thread when it is still empty (no messages), so a second tap
 *  opens it instead of stacking blank chats (and spending a Restricted
 *  profile's daily thread quota). Archived threads never count. */
export function reusableEmptyThread(threads) {
  let newest = null;
  for (const th of threads || []) {
    if (th.is_archived) continue;
    if (!newest || (th.updated_at || '') > (newest.updated_at || '')) newest = th;
  }
  return newest && !newest.message_count && !newest.last_message ? newest : null;
}

export const LAST_CHARACTER_KEY = 'dispatch-last-character';
/** The character this device last opened a chat with ('' when unknown). */
export function readLastCharacter() {
  try { return localStorage.getItem('dispatch-last-character') || ''; } catch { return ''; }
}
export function rememberCharacter(id) {
  if (!id) return;
  try { localStorage.setItem('dispatch-last-character', String(id)); } catch { /* storage off */ }
}

/** Newest updated_at per character from threads lists: { botId: [thread...] }. */
export function latestByBot(listsByBot) {
  const out = {};
  for (const [id, list] of Object.entries(listsByBot || {})) {
    let m = '';
    for (const th of list || []) if ((th.updated_at || '') > m) m = th.updated_at;
    out[id] = m;
  }
  return out;
}
