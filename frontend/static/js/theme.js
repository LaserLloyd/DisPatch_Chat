// Appearance: a fixed set of complete palettes, one of which is active.
//
// This module owns three things and nothing else:
//   1. the persisted pick (`dispatch-palette`, applied before first paint by
//      the no-FOUC script in index.html, and listed in privacy.js APP_KEYS);
//   2. the Settings → Theme gallery, built here so the CSS palette blocks and
//      the picker can never list different themes;
//   3. the rail button's icon. The button itself now OPENS that gallery
//      (wired in main.js) instead of cycling skins.
//
// It used to be a Dark ☾ ↔ Light ☀ toggle with a `data-theme` attribute and
// `theme.dark` / `theme.light` labels. The light/dark switch was retired
// 2026-09-15 in favour of named palettes: a theme is now one fixed skin, not a
// pair, so there is no second state to toggle to and no per-theme label.
// Palette names are proper nouns and are deliberately NOT translated.
import { applyDom, hasDictionary } from './i18n.js?v=3';
import { el, railIcon } from './util.js?v=13';

const PALETTE_KEY = 'dispatch-palette';
const DEFAULT_PALETTE = 'glacier';
// Two labels, and they are the ONLY translated strings in here. The mode badge
// must not be built as `t('theme.mode_' + mode)`: the i18n guard test reads
// literal keys out of the source, and a concatenated one reads as dead in
// en.json and missing at the call site at the same time.
const MODE_KEY = { dark: 'theme.mode_dark', light: 'theme.mode_light' };
const MODE_FALLBACK = { dark: 'Dark', light: 'Light' };

/** The shipped skins, in picker order. `mode` is display metadata (the badge)
 *  and a CSS `color-scheme` sanity reference — the actual scheme is declared on
 *  the palette block in theme.css, which is the source of truth.
 *
 *  frontend/tests/theme-palettes.test.js asserts this list matches the
 *  [data-palette] blocks in theme.css and the no-FOUC list in index.html, in
 *  both directions, and that the default named here is the default everywhere. */
export const PALETTES = [
  { id: 'glacier',       name: 'Glacier',        mode: 'dark'  },
  { id: 'midnight-gold', name: 'Midnight Gold',  mode: 'dark'  },
  { id: 'forest',        name: 'Forest',         mode: 'dark'  },
  { id: 'paper',         name: 'Paper',          mode: 'light' },
  { id: 'daylight',      name: 'Daylight',       mode: 'light' },
  { id: 'purple',        name: 'Classic Purple', mode: 'dark'  },
];

// Swatch glyph for the rail button. Kept local rather than added to
// RAIL_ICONS: util.js is imported under a versioned URL by half the app, and
// adding an icon there would mean either bumping that URL (two live copies of
// the module) or shipping a new theme.js against a cached old util.js, where
// RAIL_ICONS.palette is undefined and railIcon() throws on first paint.
const PALETTE_ICON = [
  'M4 5h6v6H4z',
  'M14 5h6v6h-6z',
  'M4 15h6v6H4z',
  'M14 15h6v6h-6z',
];

const meta = document.querySelector('meta[name="theme-color"]');
let grid = null;

function get(key) { try { return localStorage.getItem(key); } catch { return null; } }
function set(key, val) {
  try { if (val == null) localStorage.removeItem(key); else localStorage.setItem(key, val); }
  catch { /* private mode */ }
}
function known(id) { return PALETTES.some((p) => p.id === id); }

function currentPalette() {
  const saved = get(PALETTE_KEY);
  return known(saved) ? saved : DEFAULT_PALETTE;
}

/** Keep the browser/PWA chrome colour in step with the palette.
 *
 *  --bg-primary is read from the live stylesheet rather than from a table of
 *  constants here, so this can never disagree with what is on screen. The
 *  stylesheet may not have applied yet when this module first runs from <head>,
 *  in which case the read is empty and we retry once styles have landed. */
function syncMeta() {
  if (!meta) return;
  const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg-primary').trim();
  if (bg) { meta.setAttribute('content', bg); return; }
  addEventListener('load', syncMeta, { once: true });
}

/** One miniature of the app: rail, thread list, and a bot/user bubble pair.
 *
 *  The card element carries `data-palette="<id>"`, so the miniature is painted
 *  by the THEME'S OWN token block — the same CSS the real app uses. There is no
 *  copy of any colour in this file, which is why a preview cannot drift from
 *  the theme it advertises. */
function snip() {
  const dot = (accent) => el('span', { class: accent ? 'snip-dot snip-dot-accent' : 'snip-dot' });
  const line = (w) => el('span', { class: `snip-line snip-line-${w}` });
  return el('span', { class: 'theme-snip', 'aria-hidden': 'true' }, [
    el('span', { class: 'snip-rail' }, [dot(false), dot(false), dot(true)]),
    el('span', { class: 'snip-list' }, [line('w'), line('m'), line('w'), line('s')]),
    el('span', { class: 'snip-chat' }, [
      el('span', { class: 'snip-bubble snip-bot' }, [line('w'), line('m')]),
      el('span', { class: 'snip-bubble snip-user' }, [line('m')]),
      el('span', { class: 'snip-send' }),
    ]),
  ]);
}

function card(p) {
  return el('button', {
    class: 'theme-card',
    type: 'button',
    role: 'radio',
    'data-palette': p.id,
    'aria-checked': 'false',
    tabindex: '-1',
    onclick: () => applyPalette(p.id),
  }, [
    snip(),
    el('span', { class: 'theme-card-meta' }, [
      el('span', { class: 'theme-card-name', text: p.name }),
      el('span', { class: 'theme-card-mode', 'data-i18n': MODE_KEY[p.mode], text: MODE_FALLBACK[p.mode] }),
    ]),
    el('span', { class: 'theme-check', 'aria-hidden': 'true', text: '✓' }),
  ]);
}

/** Move the checked state to `id`. The checked card is the tab stop; the rest
 *  are reachable with the arrow keys, radio-group style. */
function paintSelection(id) {
  if (!grid) return;
  for (const b of grid.querySelectorAll('.theme-card')) {
    const on = b.dataset.palette === id;
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = on ? 0 : -1;
  }
}

/** Switch skin. Unknown ids (a hand-edited localStorage, a theme removed in an
 *  update) fall back to the default rather than leaving the page on a palette
 *  that no longer exists. */
function applyPalette(id) {
  const next = known(id) ? id : DEFAULT_PALETTE;
  document.documentElement.setAttribute('data-palette', next);
  set(PALETTE_KEY, next);
  syncMeta();
  paintSelection(next);
}

function buildGallery() {
  grid = document.getElementById('theme-grid');
  if (!grid) return;
  grid.replaceChildren(...PALETTES.map(card));
  paintSelection(currentPalette());
  // theme.js runs from <head>, before any dictionary exists, so the badges
  // carry English until the boot pass translates the document. Translating the
  // grid here covers the case where this module rebuilds it later.
  if (hasDictionary()) applyDom(grid);

  // ←/→ (or ↑/↓) walk the group and apply as they go, the way a radio group
  // behaves; Home/End jump. Direction follows the writing direction so an RTL
  // locale's "next" is left, matching wireSettingsTabs().
  grid.addEventListener('keydown', (e) => {
    const keys = ['ArrowRight', 'ArrowLeft', 'ArrowUp', 'ArrowDown', 'Home', 'End'];
    if (!keys.includes(e.key)) return;
    const rtl = document.documentElement.getAttribute('dir') === 'rtl';
    const fwd = e.key === 'ArrowDown' || e.key === (rtl ? 'ArrowLeft' : 'ArrowRight');
    const back = e.key === 'ArrowUp' || e.key === (rtl ? 'ArrowRight' : 'ArrowLeft');
    const cur = Math.max(0, PALETTES.findIndex((p) => p.id === currentPalette()));
    let next = cur;
    if (fwd) next = (cur + 1) % PALETTES.length;
    else if (back) next = (cur - 1 + PALETTES.length) % PALETTES.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = PALETTES.length - 1;
    e.preventDefault();
    applyPalette(PALETTES[next].id);
    const b = grid.querySelector(`.theme-card[data-palette="${PALETTES[next].id}"]`);
    if (b) b.focus();
  });
}

function init() {
  // Re-stamp the attribute even though the no-FOUC script already did: that
  // script is inside try/catch and a storage failure must not leave the app on
  // the CSS default with no way to tell which theme is selected.
  document.documentElement.setAttribute('data-palette', currentPalette());
  syncMeta();
  buildGallery();
  // The rail button is a shortcut into Settings → Theme (wired in main.js);
  // its label is the markup's static nav.theme pair, so nothing here has to
  // rewrite data-i18n-attr on every switch the way the old toggle did.
  const b = document.getElementById('theme-toggle');
  if (b) b.replaceChildren(railIcon(PALETTE_ICON));
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
