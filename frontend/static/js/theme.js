// Appearance: the rail button toggles Dark ☾ ↔ Light ☀ — and ONLY the theme.
// Avatar style (full pictures vs minimal names-in-the-rail) is a separate,
// rarely-changed preference that lives in Settings (see main.js). It used to
// be part of a four-state cycle on this button, but the minimal states resize
// the sidebar (72→176px), so cycling through them to reach the other theme
// visibly shoved the rail buttons around — theme switching must never move
// layout. Persisted as two keys, both applied by the no-FOUC <head> script
// before first paint: dispatch-theme = dark|light, dispatch-avatar-style =
// minimal (absent = full). This isolated module (no app state, no imports)
// owns the button; 'system' was retired in favour of the explicit toggle.
import { applyDom, hasDictionary } from './i18n.js?v=3';
import { railIcon, RAIL_ICONS } from './util.js?v=13';

const THEME_KEY = 'dispatch-theme';
// The button's label depends on which theme is active, so it is TWO keys, not
// one — which is why this module can't just leave a fixed data-i18n-attr in the
// markup. `label` is the English fallback that ships in the DOM: this module
// runs from <head>, before the dictionaries have loaded, and a hard-coded
// English label reads better at that moment than t()'s humanized key would.
const STATES = {
  // The icon shows the ACTIVE theme (moon while dark), same as the old glyphs;
  // line art from the shared rail set so this button matches its neighbours.
  dark:  { icon: RAIL_ICONS.moon, key: 'theme.dark',  label: 'Dark theme — click for light' },
  light: { icon: RAIL_ICONS.sun,  key: 'theme.light', label: 'Light theme — click for dark' },
};
const meta = document.querySelector('meta[name="theme-color"]');

function get(key) { try { return localStorage.getItem(key); } catch { return null; } }
function set(key, val) {
  try { if (val == null) localStorage.removeItem(key); else localStorage.setItem(key, val); }
  catch { /* private mode */ }
}
function currentTheme() {
  return get(THEME_KEY) === 'light' ? 'light' : 'dark';   // default + legacy 'system' → dark
}
function syncMeta() {
  if (!meta) return;
  const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg-primary').trim();
  if (bg) { meta.setAttribute('content', bg); return; }
  // The stylesheet may not have applied yet when this head module first runs, so
  // --bg-primary reads empty. Fall back to the per-theme constant now and re-sync
  // once styles land, else a light-mode user keeps the hard-coded dark tint.
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  meta.setAttribute('content', light ? '#eceae4' : '#0f0f1a');
  addEventListener('load', syncMeta, { once: true });
}
function updateBtn(theme) {
  const b = document.getElementById('theme-toggle');
  if (!b) return;
  const s = STATES[theme];
  b.replaceChildren(railIcon(s.icon));
  b.title = s.label;
  b.setAttribute('aria-label', s.label);
  // Re-point the DOM pass at whichever key is now current and let it own the
  // text from here. That keeps ONE translator for this button: the pass runs on
  // boot and again on every language switch, so no listener is needed here, and
  // nothing can clobber the label back to the generic markup wording.
  b.setAttribute('data-i18n-attr', `title=${s.key};aria-label=${s.key}`);
  // Translate THIS BUTTON, nothing else. `b.parentNode` is the gear rail, so
  // the old form re-translated every sibling's tooltip too — and this module
  // runs from <head>, before any dictionary exists, so that pass wrote humanized
  // key tails ("Unlock title", "Drop aria", "Fileserver title") over a dozen
  // hand-written English tooltips in index.html. The dictionary check is the
  // second half of the same point: until init() lands, the English `label`
  // above is already the best text we have (applyDom no-ops without a
  // dictionary too — belt and braces, since this call is the one that used to
  // do the damage). The boot pass and every language switch re-run applyDom on
  // the whole document, so the button still gets translated the moment it can be.
  if (hasDictionary()) applyDom(b);
}
function apply(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  set(THEME_KEY, theme);
  syncMeta();
  updateBtn(theme);
}
function toggle() { apply(currentTheme() === 'dark' ? 'light' : 'dark'); }
function init() {
  updateBtn(currentTheme());
  syncMeta();
  const b = document.getElementById('theme-toggle');
  if (b) b.addEventListener('click', toggle);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
