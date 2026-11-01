// Menu bots — move a bot off the avatar rail and into the Tools popup
// (js/tools.js: under the tools on the desktop, a "More bots" tile on the phone).
//
// Why this is a DEVICE setting and not a roster field: it answers "where do I
// want this on THIS screen", not "does this bot exist". A desktop rail carries
// every agent plus the operator tools and gets crowded; a phone has room for less.
// Those screens genuinely want different rails, which is the same reasoning behind
// No-Image Mode, pinned settings and custom links — all localStorage, none of
// them an account preference. The server keeps owning which bots a profile may
// use at all; this only chooses where a listed bot is drawn.
//
// A device can be shared by several profiles, so placements are kept per profile
// inside the one storage key ({ "<profile id>": [bot ids] }): one person parking
// a bot never moves another person's, and pruning the ids a profile cannot see
// never deletes what the other profiles parked. main.js names the profile with
// setMenuScope() whenever a different one takes over. If storage is unavailable
// every bot simply stays on the rail.

const KEY = 'dispatch-menu-bots';
let scope = '';

/** Whose placements menuBotIds() / toggleMenuBot() / pruneMenuBots() work on. */
export function setMenuScope(profileId) {
  scope = String(profileId || '');
}

function readAll() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
    if (Array.isArray(raw)) return { [scope]: raw };       // the pre-4.0 shape: one list for the device
    return raw && typeof raw === 'object' ? raw : {};
  } catch {
    return {};   // storage off / corrupt value: everything stays on the rail
  }
}

function writeIds(ids) {
  const all = readAll();
  if (ids.length) all[scope] = ids; else delete all[scope];
  try { localStorage.setItem(KEY, JSON.stringify(all)); } catch { /* storage off */ }
}

/** Bot ids the signed-in profile has moved into the Tools popup on this device. */
export function menuBotIds() {
  const mine = readAll()[scope];
  return new Set(Array.isArray(mine) ? mine.filter((v) => typeof v === 'string') : []);
}

export function isMenuBot(id) {
  return menuBotIds().has(id);
}

/** Move a bot into the menu, or back to the rail. Returns the new state. */
export function toggleMenuBot(id) {
  const ids = menuBotIds();
  if (ids.has(id)) ids.delete(id); else ids.add(id);
  writeIds([...ids]);
  return ids.has(id);
}

/** Drop ids that are no longer in the roster, so a deleted bot cannot keep a
 *  slot in the menu forever. Called with the live roster on each render; writes
 *  only when something actually changed, to avoid a storage write per repaint. */
export function pruneMenuBots(knownIds) {
  const ids = menuBotIds();
  if (!ids.size) return;
  const known = new Set(knownIds);
  if (!known.size) return;   // the roster has not arrived yet: never read that as "every bot is gone"
  const kept = [...ids].filter((id) => known.has(id));
  if (kept.length === ids.size) return;
  writeIds(kept);
}
