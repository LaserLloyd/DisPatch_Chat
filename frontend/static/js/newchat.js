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

/** The character a new chat goes to.
 *   1. the one selected right now, if this profile can use it;
 *   2. else the most recently used: the usable character whose newest thread
 *      has the greatest updated_at (`latest`: id -> ISO string or ''/missing);
 *   3. else none: the caller opens the character picker.
 *  `usableIds` is the roster this profile may use, in rail order (ties and
 *  never-chatted characters fall back to that order, first wins).
 *  Returns { botId, via: 'current' | 'recent' | 'picker' }. */
export function pickNewChatTarget({ selectedId, usableIds, latest }) {
  const usable = Array.isArray(usableIds) ? usableIds : [];
  if (selectedId && usable.includes(selectedId)) return { botId: selectedId, via: 'current' };
  let best = null;
  let bestAt = '';
  for (const id of usable) {
    const at = (latest && latest[id]) || '';
    if (at && at > bestAt) { best = id; bestAt = at; }
  }
  return best ? { botId: best, via: 'recent' } : { botId: null, via: 'picker' };
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
