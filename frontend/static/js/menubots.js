// Menu bots — move a bot off the avatar rail and into the ⌥ Tools menu.
//
// Why this is a DEVICE setting and not a roster field: it answers "where do I
// want this on THIS screen", not "does this bot exist". The unlocked desktop
// rail carries every agent plus the operator tools and gets crowded; a family
// tablet in Safe Mode sees three safe bots and has room to spare. Those two
// screens genuinely want different rails, which is the same reasoning behind
// No-Image Mode, pinned settings and custom links — all localStorage, none of
// them an account preference. The server keeps owning whether a bot is visible
// at all (`bot.visible`); this only chooses where a visible bot is drawn.
//
// SAFE MODE IGNORES THIS ENTIRELY. The ⌥ menu is unlocked-only, so honouring a
// placement while locked would make a bot unreachable rather than merely moved
// — the roster would silently lose an entry on a handed-over tablet. menuIds()
// returns an empty set in Safe Mode, so every visible bot falls back to the
// rail, which is also what happens if storage is unavailable.

const KEY = 'dispatch-menu-bots';

/** Bot ids the user has moved into the ⌥ menu on this device. */
export function menuBotIds() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw.filter((v) => typeof v === 'string') : []);
  } catch {
    return new Set();   // storage off / corrupt value: everything stays on the rail
  }
}

/** The set to actually render with, given the current tier. */
export function activeMenuBotIds(decoy) {
  return decoy ? new Set() : menuBotIds();
}

export function isMenuBot(id) {
  return menuBotIds().has(id);
}

/** Move a bot into the menu, or back to the rail. Returns the new state. */
export function toggleMenuBot(id) {
  const ids = menuBotIds();
  if (ids.has(id)) ids.delete(id); else ids.add(id);
  try { localStorage.setItem(KEY, JSON.stringify([...ids])); } catch { /* storage off */ }
  return ids.has(id);
}

/** Drop ids that are no longer in the roster, so a deleted bot cannot keep a
 *  slot in the menu forever. Called with the live roster on each render; writes
 *  only when something actually changed, to avoid a storage write per repaint. */
export function pruneMenuBots(knownIds) {
  const ids = menuBotIds();
  if (!ids.size) return;
  const known = new Set(knownIds);
  const kept = [...ids].filter((id) => known.has(id));
  if (kept.length === ids.size) return;
  try { localStorage.setItem(KEY, JSON.stringify(kept)); } catch { /* storage off */ }
}
