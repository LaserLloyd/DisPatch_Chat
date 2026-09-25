// Appearance: the Settings → Theme gallery and the rail button's icon.
//
// The THEME itself is not this module's any more. ui-theme.js — a generated,
// vendored runtime loaded as the first, blocking <script> in index.html —
// owns the stored pick (`dispatch-palette`), stamps <html data-palette /
// data-theme / data-contrast-profile> and <meta name="theme-color"> before
// first paint, and exposes window.UITheme. This module only renders what that
// runtime offers:
//
//   1. the gallery, built from UITheme.list() so the picker can never list a
//      theme the stylesheet does not define (or miss one it does);
//   2. the selection state, kept in step through UITheme.onChange — which
//      also fires when another tab changes the theme;
//   3. the theme revision line under the gallery;
//   4. the rail button's icon. The button OPENS the gallery (wired in
//      main.js); there is no toggle.
//
// Theme names are proper nouns and are deliberately NOT translated.
import { applyDom, hasDictionary } from './i18n.js?v=3';
import { el, railIcon } from './util.js?v=20';

// The badge labels are the only translated strings on a card. Literal keys,
// never `t('theme.mode_' + mode)`: the i18n guard test reads keys out of the
// source, and a concatenated one reads as dead and missing at once.
const MODE_KEY = { dark: 'theme.mode_dark', light: 'theme.mode_light' };
const MODE_FALLBACK = { dark: 'Dark', light: 'Light' };
// Same attribute the runtime derives for <html> from a theme's ground. A
// preview card carries it too, so the card resolves the adapter's
// [data-theme="light"] surfaces exactly as the page would.
const DATA_THEME = { oled: 'amoled', dark: 'dark', light: 'light' };

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

let grid = null;

/** The runtime, or null when it failed to load. Everything below degrades to
 *  "no gallery" rather than throwing: the page still paints with whatever
 *  theme the stylesheet's :root base gives it. */
function runtime() {
  const ui = window.UITheme;
  return ui && typeof ui.list === 'function' ? ui : null;
}

/** One miniature of the app: rail, thread list, and a bot/user bubble pair.
 *
 *  The card element carries the theme's own `data-palette` (plus the
 *  data-theme / data-contrast-profile the runtime would put on <html>), so
 *  the miniature is painted by the THEME'S OWN token block — the same CSS the
 *  real app uses. No colour is copied into this file. */
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

function card(ui, t) {
  const mode = t.ground === 'light' ? 'light' : 'dark';
  return el('button', {
    class: 'theme-card',
    type: 'button',
    role: 'radio',
    'data-palette': t.slug,
    'data-theme': DATA_THEME[t.ground] || 'dark',
    'data-contrast-profile': t.contrastProfile || 'standard',
    'aria-checked': 'false',
    tabindex: '-1',
    onclick: () => ui.set(t.slug),
  }, [
    snip(),
    el('span', { class: 'theme-card-meta' }, [
      el('span', { class: 'theme-card-name', text: t.name }),
      el('span', { class: 'theme-card-mode', 'data-i18n': MODE_KEY[mode], text: MODE_FALLBACK[mode] }),
    ]),
    el('span', { class: 'theme-check', 'aria-hidden': 'true', text: '✓' }),
  ]);
}

/** Move the checked state to `slug`. The checked card is the tab stop; the
 *  rest are reachable with the arrow keys, radio-group style. */
function paintSelection(slug) {
  if (!grid) return;
  for (const b of grid.querySelectorAll('.theme-card')) {
    const on = b.dataset.palette === slug;
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = on ? 0 : -1;
  }
}

function buildGallery(ui) {
  grid = document.getElementById('theme-grid');
  if (!grid) return;
  const themes = ui.list();
  const nodes = [];
  themes.forEach((t, i) => {
    // The opt-in themes follow the core set behind a labelled rule, so the
    // six DisPatch has always shipped read as the main choice.
    if (t.set !== 'core' && (i === 0 || themes[i - 1].set === 'core')) {
      nodes.push(el('p', {
        class: 'theme-group-label', role: 'presentation',
        'data-i18n': 'settings.theme_more', text: 'More themes',
      }));
    }
    nodes.push(card(ui, t));
  });
  grid.replaceChildren(...nodes);
  paintSelection(ui.current());
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
    const slugs = ui.list().map((t) => t.slug);
    const rtl = document.documentElement.getAttribute('dir') === 'rtl';
    const fwd = e.key === 'ArrowDown' || e.key === (rtl ? 'ArrowLeft' : 'ArrowRight');
    const back = e.key === 'ArrowUp' || e.key === (rtl ? 'ArrowRight' : 'ArrowLeft');
    const cur = Math.max(0, slugs.indexOf(ui.current()));
    let next = cur;
    if (fwd) next = (cur + 1) % slugs.length;
    else if (back) next = (cur - 1 + slugs.length) % slugs.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = slugs.length - 1;
    e.preventDefault();
    ui.set(slugs[next]);
    const b = grid.querySelector(`.theme-card[data-palette="${slugs[next]}"]`);
    if (b) b.focus();
  });
}

/** "Theme revision 2026-09-22" under the gallery. The date goes in through
 *  data-i18n-vars so a later language switch re-renders it. */
function fillRevision(ui) {
  const p = document.getElementById('theme-revision');
  if (!p) return;
  const rev = typeof ui.revision === 'function' ? ui.revision() : '';
  if (!rev) { p.hidden = true; return; }
  p.setAttribute('data-i18n-vars', JSON.stringify({ date: rev }));
  p.textContent = `Theme revision ${rev}`;
  if (hasDictionary()) applyDom(p);
}

function init() {
  const b = document.getElementById('theme-toggle');
  if (b) b.replaceChildren(railIcon(PALETTE_ICON));
  const ui = runtime();
  if (!ui) return;
  buildGallery(ui);
  fillRevision(ui);
  ui.onChange((detail) => paintSelection(detail.slug));
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
