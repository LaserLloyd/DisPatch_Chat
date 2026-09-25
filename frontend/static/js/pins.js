// Pinned settings — put a device setting on the rail, so the switch you
// actually use is one tap away instead of three.
//
// Scope, deliberately: only DEVICE settings are pinnable — the ones stored in
// this browser (No-Image Mode, Privacy mode), never a server setting and never
// anything that mutates the roster. A pin is a shortcut, and a shortcut to a
// gated action would be a second path to it. Keeping the registry to
// device-local toggles means a pin cannot become a way around the tier gate,
// because there is no server call behind any of them.
//
// The gate still applies to what is DRAWN. Each entry declares `safe`: false
// means the button is omitted entirely in Safe Mode, and because the rail is
// re-rendered on every tier change, a device that locks loses those pins on
// the spot rather than keeping a stale button that would 403 on use.
//
// Storage is localStorage, like the settings themselves: a pin is a per-device
// convenience, not an account preference, and the family tablet wanting a
// different rail from the phone is the normal case, not an edge one.

import { railIcon, RAIL_ICONS } from './util.js?v=20';
import { nimEnabled, setNim, canDisableNim, minimalAvatarsEnabled, setMinimalAvatars } from './nim.js?v=5';
import { privacyEnabled, setPrivacy } from './privacy.js?v=9';

const KEY = 'dispatch-pinned-settings';
// Defaults are stored as OPT-OUTS, not by seeding KEY. A device that already
// has pins saved has a non-empty KEY, so "empty means defaults" would never
// reach it, and seeding KEY on first read would make a default indistinguish-
// able from a deliberate pin the moment the default changed. An explicit
// unpin list says exactly what it means: the user turned this one off.
const OFF_KEY = 'dispatch-unpinned-defaults';

/** The pinnable registry.
 *
 *  `enabled()`  – is the setting currently on?
 *  `toggle()`   – flip it; may be async (privacy writes through a helper).
 *  `blocked()`  – why the button must not act right now, or null. This is how
 *                 NIM's ratchet reaches the rail: once NIM is on, a locked
 *                 device cannot turn it off, so the pinned button says so
 *                 instead of silently doing nothing.
 */
export const PINNABLE = [
  {
    id: 'nim',
    icon: RAIL_ICONS.nim,
    titleKey: 'nim.title',
    titleEn: 'No-Image Mode',
    safe: true,                       // works in Safe Mode; that is its point
    enabled: () => nimEnabled(),
    toggle: (on) => setNim(on),
    blocked: (decoy) =>
      nimEnabled() && !canDisableNim(decoy)
        ? 'nim.hint_locked'
        : null,
  },
  {
    id: 'privacy',
    icon: RAIL_ICONS.privacy,
    titleKey: 'privacy.title',
    titleEn: 'Privacy mode',
    safe: true,
    enabled: () => privacyEnabled(),
    toggle: (on) => setPrivacy(on),
    blocked: () => null,
  },
  {
    id: 'avatars',
    icon: RAIL_ICONS.avatars,
    titleKey: 'settings.minimal_avatars',
    titleEn: 'Minimal avatars',
    safe: true,                       // pure CSS display preference, no gate behind it
    enabled: () => minimalAvatarsEnabled(),
    toggle: (on) => setMinimalAvatars(on),
    // While NIM is on the attribute is borrowed and the preference read-only —
    // same rule syncMinimalAvatarRow enforces on the Settings checkbox.
    blocked: () => (nimEnabled() ? 'nim.controls_avatars' : null),
  },
  // The theme button is the one pin that ships ON. It is also the one whose
  // button this module does not draw: 🎨 is markup in index.html (its own rail
  // slot, its own click into Settings → Theme), so the pin only decides
  // whether that button is shown — `kind: 'rail'`, skipped by visiblePins.
  // Unpinning it is a device preference like any other; nothing else changes.
  {
    id: 'theme',
    kind: 'rail',
    icon: null,
    titleKey: 'nav.theme',
    titleEn: 'Theme',
    safe: false,               // the palette picker is a full-session surface
    pinnedByDefault: true,
    enabled: () => false,      // not a switch: there is no on/off state to show
    toggle: () => {},
    blocked: () => null,
  },
];

export function pinnableById(id) {
  return PINNABLE.find((p) => p.id === id) || null;
}

/** Pinned ids, in the order they were pinned. Never throws: a browser with
 *  site data blocked reads as "nothing pinned", which is the safe default. */
export function pinnedIds() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '[]');
    if (!Array.isArray(raw)) return [];
    // Filter through the registry so an id left behind by an older build (or
    // hand-edited storage) cannot put an unknown button on the rail.
    return raw.filter((id) => typeof id === 'string' && pinnableById(id));
  } catch {
    return [];
  }
}

/** Ids of default pins the user has explicitly turned off. */
function unpinnedDefaults() {
  try {
    const raw = JSON.parse(localStorage.getItem(OFF_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter((id) => typeof id === 'string') : [];
  } catch {
    return [];
  }
}

export function isPinned(id) {
  const entry = pinnableById(id);
  if (entry && entry.pinnedByDefault) return !unpinnedDefaults().includes(id);
  return pinnedIds().includes(id);
}

/** Pin or unpin. Returns the new pinned list.
 *
 *  A default-on pin is written to the opt-out list instead of KEY, so KEY keeps
 *  meaning "pins the user added" and the two can never disagree about one id.
 */
export function setPinned(id, on) {
  const entry = pinnableById(id);
  if (!entry) return pinnedIds();
  if (entry.pinnedByDefault) {
    const next = unpinnedDefaults().filter((x) => x !== id);
    if (!on) next.push(id);
    try { localStorage.setItem(OFF_KEY, JSON.stringify(next)); } catch { /* storage off */ }
    return pinnedIds();
  }
  const next = pinnedIds().filter((x) => x !== id);
  if (on) next.push(id);
  try { localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* storage off */ }
  return next;
}

/** Which pins may be DRAWN for this session.
 *
 *  Safe Mode sees only entries marked `safe`. Everything else is omitted, not
 *  disabled: a greyed-out button for a surface a locked device may never reach
 *  advertises the surface, which is the opposite of what Safe Mode is for.
 */
export function visiblePins(decoy) {
  return pinnedIds()
    .map(pinnableById)
    // `kind: 'rail'` entries own a button in the markup already (🎨). Drawing
    // one here too would put the same shortcut on the rail twice.
    .filter((entry) => entry && entry.kind !== 'rail' && (entry.safe || !decoy));
}

/** Render the pinned buttons into the rail.
 *
 *  Rebuilt wholesale on every call rather than diffed: the list is at most a
 *  handful of buttons, and the alternative is keeping stale DOM in step with a
 *  tier change, which is exactly the bug class this is meant to avoid.
 */
export function renderPinnedRail(container, { decoy = false, t = null, onChange = null } = {}) {
  if (!container) return;
  container.querySelectorAll('.pin-btn').forEach((el) => el.remove());
  const anchor = container.querySelector('#manage-bots');   // pins sit before ⚙
  for (const entry of visiblePins(decoy)) {
    const btn = document.createElement('button');
    btn.className = 'gear-btn pin-btn';
    btn.dataset.pin = entry.id;
    btn.append(railIcon(entry.icon));
    const label = t ? t(entry.titleKey) : entry.titleEn;
    const on = !!entry.enabled();
    const blockedKey = entry.blocked ? entry.blocked(decoy) : null;
    btn.setAttribute('role', 'switch');
    btn.setAttribute('aria-checked', on ? 'true' : 'false');
    btn.setAttribute('aria-label', label);
    btn.classList.toggle('pin-on', on);
    if (blockedKey) {
      btn.disabled = true;
      btn.title = `${label} — ${t ? t(blockedKey) : 'unlock with your PIN to turn this off'}`;
    } else {
      btn.title = label;
      btn.addEventListener('click', async () => {
        const next = !entry.enabled();
        // toggle() shares setPrivacy()'s contract: a truthy return means the
        // change only takes effect after a reload (privacy mode has to
        // re-register the service worker, which needs a fresh page load). The
        // Settings row honours it; the rail discarded the return value, so
        // unpinning-privacy-from-the-rail left the page claiming a mode it was
        // not actually in until the next reload.
        const needsReload = await entry.toggle(next);
        if (needsReload) { location.reload(); return; }
        renderPinnedRail(container, { decoy, t, onChange });
        if (onChange) onChange(entry.id, next);
      });
    }
    if (anchor) container.insertBefore(btn, anchor);
    else container.append(btn);
  }
}

/** The 📌 control that goes on a settings row. */
export function pinToggle(id, { t = null, onChange = null } = {}) {
  const entry = pinnableById(id);
  if (!entry) return null;
  const btn = document.createElement('button');
  btn.type = 'button';                 // inside a <label>: a bare button submits
  btn.className = 'pin-toggle';
  btn.dataset.pinToggle = id;
  const paint = () => {
    const on = isPinned(id);
    btn.replaceChildren(railIcon(RAIL_ICONS.pin));
    btn.classList.toggle('pinned', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    const key = on ? 'pins.unpin' : 'pins.pin';
    btn.title = t ? t(key) : (on ? 'Unpin from the bar' : 'Pin to the bar');
    btn.setAttribute('aria-label', btn.title);
  };
  paint();
  btn.addEventListener('click', (ev) => {
    // The row is a <label> wrapping a checkbox; without this the click also
    // toggles the setting it is a pin FOR.
    ev.preventDefault();
    ev.stopPropagation();
    setPinned(id, !isPinned(id));
    paint();
    if (onChange) onChange(id, isPinned(id));
  });
  return btn;
}
