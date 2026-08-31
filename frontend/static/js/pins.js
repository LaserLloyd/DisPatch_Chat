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

import { nimEnabled, setNim, canDisableNim, minimalAvatarsEnabled, setMinimalAvatars } from './nim.js?v=5';
import { privacyEnabled, setPrivacy } from './privacy.js?v=5';

const KEY = 'dispatch-pinned-settings';

// ---- Rail icons -----------------------------------------------------------
// Thin-stroke line icons in currentColor, not emoji: emoji render in the
// platform's colour set and clash with the rail's monochrome chrome (the 🙈
// experiment proved it). These follow the house line-work — 1.8px rounded
// strokes on a 24px grid — and inherit the button's colour, so they track the
// theme and pick up the accent fill of `.pin-on` for free.
//
// Built with createElementNS from constant path data. No innerHTML: el() bans
// it app-wide and a hand-rolled exception for "trusted" markup is how that
// ban erodes.

const SVG_NS = 'http://www.w3.org/2000/svg';

/** One icon: an array of path `d` strings on a 24×24 grid → an <svg>. */
export function railIcon(paths) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.classList.add('rail-icon');
  for (const d of paths) {
    const p = document.createElementNS(SVG_NS, 'path');
    p.setAttribute('d', d);
    svg.append(p);
  }
  return svg;
}

// Picture frame, broken by the slash: "no images".
const ICON_NIM = [
  'M2 2 22 22',                                       // the slash
  'M10.41 10.41a2 2 0 1 1-2.83-2.83',                 // the sun, cut open
  'M13.5 13.5 6 21',                                  // the mountainside
  'M18 12l3 3',
  'M3.59 3.59A1.99 1.99 0 0 0 3 5v14a2 2 0 0 0 2 2h14c.55 0 1.052-.22 1.41-.59',
  'M21 15V5a2 2 0 0 0-2-2H9',                         // frame, open at the slash
];
// Eye, struck through: "this device sees nothing".
const ICON_PRIVACY = [
  'M2 2 22 22',
  'M9.88 9.88a3 3 0 1 0 4.24 4.24',
  'M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68',
  'M6.61 6.61A13.53 13.53 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61',
];
// The silhouette the setting switches to.
const ICON_AVATARS = [
  'M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2',
  'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
];

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
    icon: ICON_NIM,
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
    icon: ICON_PRIVACY,
    titleKey: 'privacy.title',
    titleEn: 'Privacy mode',
    safe: true,
    enabled: () => privacyEnabled(),
    toggle: (on) => setPrivacy(on),
    blocked: () => null,
  },
  {
    id: 'avatars',
    icon: ICON_AVATARS,
    titleKey: 'settings.minimal_avatars',
    titleEn: 'Minimal avatars',
    safe: true,                       // pure CSS display preference, no gate behind it
    enabled: () => minimalAvatarsEnabled(),
    toggle: (on) => setMinimalAvatars(on),
    // While NIM is on the attribute is borrowed and the preference read-only —
    // same rule syncMinimalAvatarRow enforces on the Settings checkbox.
    blocked: () => (nimEnabled() ? 'nim.controls_avatars' : null),
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

export function isPinned(id) {
  return pinnedIds().includes(id);
}

/** Pin or unpin. Returns the new pinned list. */
export function setPinned(id, on) {
  if (!pinnableById(id)) return pinnedIds();
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
    .filter((entry) => entry && (entry.safe || !decoy));
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
    btn.textContent = '📌';
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
