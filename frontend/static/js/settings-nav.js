// Settings: its sections, the old tab ids that still lead to them, and the
// search box that finds any setting by name.
//
// 3.2 replaced the thirteen-tab strip with seven sections in two groups (you,
// the household) plus one for add-ons. Every id the strip used to have is an
// ALIAS here, so a caller written against the old tabs (a pinned Theme button,
// the voice add-on's settings.open('voice'), a deep link in a guide) lands on
// the section that holds the same controls now. `section#anchor` opens a
// subsection inside it.
//
// Pure first (SECTIONS, TAB_ALIASES, resolveSettingsTarget, buildSearchIndex,
// searchSettings) so the tests need no DOM; mountSettingsSearch is the DOM half.
// Nothing runs at import time.

import { el } from './util.js?v=20';

/** The sections, in nav order. `group`: you (this person / device), household
 *  (Owner and Admin), addons (tabs an add-on adds; none built in). `perm` is
 *  what main.js asks sessionCan()/canAdmin() before showing one. */
export const SECTIONS = [
  { id: 'appearance', group: 'you', icon: 'palette', label: 'settings.sec_appearance', perm: null },
  { id: 'account', group: 'you', icon: 'user', label: 'settings.sec_account', perm: null },
  { id: 'voice', group: 'you', icon: 'mic', label: 'settings.sec_voice', perm: 'voice' },
  { id: 'characters', group: 'household', icon: 'bots', label: 'settings.sec_characters', perm: 'admin' },
  { id: 'connections', group: 'household', icon: 'plug', label: 'settings.sec_connections', perm: 'admin' },
  { id: 'people', group: 'household', icon: 'users', label: 'settings.sec_people', perm: 'admin' },
  { id: 'plugins', group: 'household', icon: 'tools', label: 'settings.sec_plugins', perm: 'admin' },
  { id: 'system', group: 'household', icon: 'pulse', label: 'settings.sec_system', perm: 'admin' },
];

export const SECTION_IDS = SECTIONS.map((s) => s.id);

/** Every tab id Settings has ever had (2.x / 3.0 / 3.1), and where it lives now.
 *  This is the compatibility contract for add-ons and old links: an id here
 *  keeps working, and none of them may be claimed by an add-in tab. */
export const TAB_ALIASES = Object.freeze({
  bots: 'characters',
  reactions: 'characters#reactions',
  theme: 'appearance',
  device: 'appearance',
  security: 'account#signin',
  ai: 'connections',
  agents: 'connections#agents',
  health: 'system',
  settings: 'system',
  people: 'people',
  plugins: 'plugins',
  voice: 'voice',
  connections: 'connections',
});

/** Ids an add-in tab may never take: the sections and every alias. */
export const RESERVED_TAB_IDS = new Set([...SECTION_IDS, ...Object.keys(TAB_ALIASES)]);

/** 'theme' / 'characters#reactions' / 'key:people.lock_timeout_seconds' -> { id, anchor }.
 *  An unknown id comes back as itself (an add-in tab), an empty one as null. */
export function resolveSettingsTarget(target) {
  const raw = String(target == null ? '' : target).trim();
  if (!raw) return { id: null, anchor: '' };
  const hash = raw.indexOf('#');
  let id = hash < 0 ? raw : raw.slice(0, hash);
  let anchor = hash < 0 ? '' : raw.slice(hash + 1);
  if (!SECTION_IDS.includes(id) && Object.prototype.hasOwnProperty.call(TAB_ALIASES, id)) {
    const to = TAB_ALIASES[id];
    const h = to.indexOf('#');
    id = h < 0 ? to : to.slice(0, h);
    if (!anchor && h >= 0) anchor = to.slice(h + 1);
  }
  return { id, anchor };
}

// ------------------------------------------------------------------ search

/** The rows that are not schema settings: { section, anchor, label (i18n key),
 *  terms (extra words, English, never shown) }. `perm` as in SECTIONS ('host'
 *  = needs host control on top). Kept beside the markup's data-anchor names. */
export const STATIC_ROWS = [
  { section: 'appearance', anchor: 'theme', label: 'settings.theme_title', terms: 'palette colour color dark light skin look' },
  { section: 'appearance', anchor: 'language', label: 'settings.language', terms: 'locale translate' },
  { section: 'appearance', anchor: 'avatar-style', label: 'settings.avatar_style', terms: 'minimal pictures names sidebar' },
  { section: 'appearance', anchor: 'menu-bar', label: 'settings.dev_bar', terms: 'pin shortcut link button rail' },
  { section: 'appearance', anchor: 'hidden-bots', label: 'settings.show_hidden', terms: 'hidden characters rail', perm: 'admin' },
  { section: 'account', anchor: 'notice', label: 'host_notice.title', terms: 'privacy private stored host encrypted who can read' },
  { section: 'account', anchor: 'signin', label: 'acct.signin_title', terms: 'pin password change remove sign in' },
  { section: 'account', anchor: 'signin', label: 'settings.lock_now', terms: 'lock sign out' },
  { section: 'account', anchor: 'notifications', label: 'push.group', terms: 'push notify alert' },
  { section: 'account', anchor: 'pictures', label: 'settings.dev_pictures', terms: 'no-image nim privacy mode hide pictures' },
  { section: 'account', anchor: 'recovery', label: 'settings.recovery', terms: 'export restore backup messages' },
  { section: 'account', anchor: 'about', label: 'about.title', terms: 'version source licence' },
  { section: 'voice', anchor: '', label: 'settings.sec_voice', terms: 'speech microphone talk tts stt' },
  { section: 'characters', anchor: 'roster', label: 'settings.sec_characters', terms: 'bots order rename avatar photo remove add', perm: 'admin' },
  { section: 'characters', anchor: 'add', label: 'chars.add', terms: 'new starter pack', perm: 'admin' },
  { section: 'characters', anchor: 'reactions', label: 'settings.tab_reactions', terms: 'reaction images moods pool', perm: 'admin' },
  { section: 'characters', anchor: 'avatar-pools', label: 'settings.pool_section', terms: 'avatar pool faces', perm: 'admin' },
  { section: 'connections', anchor: 'list', label: 'cxn.title', terms: 'server model provider key api', perm: 'admin' },
  { section: 'connections', anchor: 'quick-setup', label: 'integrations.qs_title', terms: 'one key provider setup', perm: 'admin' },
  { section: 'connections', anchor: 'images', label: 'integrations.img_title', terms: 'image generation pictures', perm: 'admin' },
  { section: 'connections', anchor: 'agents', label: 'agents.tab', terms: 'agent backend claude openclaw secrets', perm: 'admin' },
  { section: 'people', anchor: 'people', label: 'people.title', terms: 'profiles add person member admin restricted', perm: 'admin' },
  { section: 'people', anchor: 'device', label: 'people.device_title', terms: 'this device open as bind', perm: 'admin' },
  { section: 'people', anchor: 'signin-rules', label: 'people.rules_title', terms: 'auto-lock timeout remember session', perm: 'admin' },
  { section: 'people', anchor: 'restricted-limits', label: 'people.limits_title', terms: 'restricted quota daily limits', perm: 'admin' },
  { section: 'people', anchor: 'machine', label: 'people.machine_title', terms: 'api key token machine trust', perm: 'admin' },
  { section: 'people', anchor: 'activity', label: 'people.activity_title', terms: 'audit log activity', perm: 'admin' },
  { section: 'plugins', anchor: 'addons', label: 'plugins.addons_title', terms: 'extension add-on voice', perm: 'admin' },
  { section: 'plugins', anchor: 'tools', label: 'plugins.tools_title', terms: 'apps tools panes', perm: 'admin' },
  { section: 'plugins', anchor: 'lookups', label: 'plugins.lookups_title', terms: 'web search weather wikipedia', perm: 'admin' },
  { section: 'plugins', anchor: 'discover', label: 'integrations.catalog_title', terms: 'works with catalogue catalog', perm: 'admin' },
  { section: 'system', anchor: 'health', label: 'settings.health_title', terms: 'health dashboard status checks log', perm: 'admin' },
  { section: 'system', anchor: 'backups', label: 'bkp.title', terms: 'backup snapshot restore support bundle', perm: 'admin' },
  { section: 'system', anchor: 'viewer', label: 'viewer.settings_title', terms: 'local viewer folders files', perm: 'admin' },
];

/** Where a schema row is drawn: its `ui_section` from the server ('people#signin-rules'),
 *  else its group's section. */
export function schemaTarget(def) {
  if (def && def.ui_section) return resolveSettingsTarget(def.ui_section);
  const byGroup = { characters: 'characters', connections: 'connections', voice: 'voice', tools: 'plugins',
    people: 'people', appearance: 'appearance', system: 'system' };
  return { id: byGroup[def && def.group] || 'system', anchor: '' };
}

const fold = (s) => String(s || '').toLocaleLowerCase()
  .normalize('NFKD').replace(/[̀-ͯ]/g, '');

/**
 * One searchable list. `t` translates the static labels; `allowed(section, perm)`
 * says what this session may see; `schema` is GET /api/settings/schema (or null
 * for a session that cannot read it); `sectionLabel(id)` names a section.
 * -> [{ section, anchor, label, where, hay }]
 */
export function buildSearchIndex({ t = (k) => k, allowed = () => true, schema = null, sectionLabel = (s) => s } = {}) {
  const out = [];
  for (const r of STATIC_ROWS) {
    if (!allowed(r.section, r.perm || null)) continue;
    const label = t(r.label);
    out.push({ section: r.section, anchor: r.anchor, label, where: sectionLabel(r.section),
      hay: fold(`${label} ${r.terms || ''} ${sectionLabel(r.section)}`) });
  }
  for (const def of (schema && schema.settings) || []) {
    const { id, anchor } = schemaTarget(def);
    if (!allowed(id, 'admin')) continue;
    out.push({ section: id, anchor: `key:${def.key}`, label: def.label, where: sectionLabel(id),
      hay: fold(`${def.label} ${def.description || ''} ${def.key} ${def.section || ''} ${anchor}`) });
  }
  return out;
}

/** Entries matching every word of `q`, label matches first. At most `limit`. */
export function searchSettings(index, q, limit = 12) {
  const words = fold(q).trim().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const hits = [];
  for (const e of index) {
    if (!words.every((w) => e.hay.includes(w))) continue;
    const lab = fold(e.label);
    const score = words.every((w) => lab.includes(w)) ? (lab.startsWith(words[0]) ? 0 : 1) : 2;
    hits.push([score, e]);
  }
  hits.sort((a, b) => a[0] - b[0]);
  return hits.slice(0, limit).map((h) => h[1]);
}

// ------------------------------------------------------------------ DOM

/**
 * Wire the search box. deps:
 *   input, results      the <input type=search> and its listbox host
 *   index()             -> Promise<entries> (built lazily: the schema is fetched on first use)
 *   go(entry)           open the entry's section and anchor
 *   t                   translator
 */
export function mountSettingsSearch({ input, results, index, go, t = (k) => k }) {
  if (!input || !results) return { close() {} };
  let entries = null;
  let shown = [];
  let active = -1;
  let seq = 0;
  const close = () => {
    results.hidden = true;
    results.replaceChildren();
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    shown = []; active = -1;
  };
  const mark = (i) => {
    active = i;
    [...results.children].forEach((n, j) => n.setAttribute('aria-selected', String(j === i)));
    const cur = results.children[i];
    if (cur) { input.setAttribute('aria-activedescendant', cur.id); try { cur.scrollIntoView({ block: 'nearest' }); } catch { /* ignore */ } }
  };
  const pick = (e) => { close(); input.value = ''; go(e); };
  const paint = async () => {
    const q = input.value;
    if (!q.trim()) { close(); return; }
    const mine = ++seq;
    if (!entries) entries = await index();
    if (mine !== seq) return;
    shown = searchSettings(entries, q);
    results.replaceChildren();
    if (!shown.length) {
      results.append(el('div', { class: 'ssearch-empty', role: 'option', 'aria-disabled': 'true', id: 'ssearch-none',
        text: t('settings.search_empty', { q: q.trim() }) }));
    }
    shown.forEach((e, i) => {
      const opt = el('div', { class: 'ssearch-hit', role: 'option', id: `ssearch-${i}`, 'aria-selected': 'false' }, [
        el('span', { class: 'ssearch-label', text: e.label }),
        el('span', { class: 'ssearch-where', text: e.where }),
      ]);
      opt.addEventListener('mousedown', (ev) => ev.preventDefault());   // keep focus in the box
      opt.addEventListener('click', () => pick(e));
      results.append(opt);
    });
    results.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    mark(shown.length ? 0 : -1);
  };
  input.addEventListener('input', paint);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' && shown.length) { e.preventDefault(); mark((active + 1) % shown.length); }
    else if (e.key === 'ArrowUp' && shown.length) { e.preventDefault(); mark((active - 1 + shown.length) % shown.length); }
    else if (e.key === 'Enter' && active >= 0 && shown[active]) { e.preventDefault(); pick(shown[active]); }
    else if (e.key === 'Escape' && !results.hidden) { e.preventDefault(); e.stopPropagation(); close(); }
  });
  input.addEventListener('blur', () => setTimeout(close, 150));
  return { close, reset: () => { entries = null; } };
}

// ------------------------------------------------------------------ deep links

const cssAttr = (s) => String(s).replace(/["\\]/g, '\\$&');

/** Open `anchor` inside `pane` (a data-anchor name, or key:<schema key>): open any
 *  closed <details> on the way, scroll it to the top and flash it.
 *
 *  Sections fill in after fetches (schema rows, the backups card, a module's
 *  groups), so the target may not exist yet, and content that arrives above it
 *  pushes it down after the first scroll. It looks again until it exists, then
 *  keeps it at the top while the section settles - until the reader scrolls,
 *  types or taps, which ends the follow at once. `timers` is for tests. */
export function revealAnchor(pane, anchor, { timers = globalThis, settleMs = 2000, step = 150 } = {}) {
  if (!pane || !anchor) return () => {};
  const sel = anchor.startsWith('key:')
    ? `[data-key="${cssAttr(anchor.slice(4))}"]`
    : `[data-anchor="${cssAttr(anchor)}"]`;
  let stopped = false;
  let handle = null;
  const events = ['wheel', 'touchstart', 'keydown', 'pointerdown'];
  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (handle != null) timers.clearTimeout(handle);
    for (const ev of events) pane.removeEventListener(ev, stop, true);
  };
  for (const ev of events) pane.addEventListener(ev, stop, { capture: true, passive: true });
  const toTop = (node, smooth) => {
    try { node.scrollIntoView({ block: 'start', behavior: smooth ? 'smooth' : 'auto' }); } catch { /* ignore */ }
  };
  // Each tick looks the target up again: a section may re-render its rows (the
  // schema hosts do, shortly after mounting), and a replaced node is the one to keep
  // in view, not the detached original. A pane already scrolled to its end cannot
  // bring a lower target any higher, so that tick does nothing.
  const follow = (left) => {
    if (stopped) return;
    if (left <= 0) { stop(); return; }
    handle = timers.setTimeout(() => {
      if (stopped) return;
      const node = pane.querySelector(sel);
      if (node) {
        const off = node.getBoundingClientRect().top - pane.getBoundingClientRect().top;
        const atEnd = pane.scrollHeight - pane.clientHeight - pane.scrollTop <= 1;
        if (off < -4 || (off > 4 && !atEnd)) toTop(node, false);
      }
      follow(left - step);
    }, step);
  };
  const find = (tries) => {
    if (stopped) return;
    const node = pane.querySelector(sel);
    if (!node) {
      if (tries < 20) handle = timers.setTimeout(() => find(tries + 1), 100);
      else stop();
      return;
    }
    for (let n = node; n && n !== pane; n = n.parentElement) {
      if (n.tagName === 'DETAILS' && !n.open) { n.open = true; n.dispatchEvent(new Event('toggle')); }
    }
    if (node.tagName === 'DETAILS' && !node.open) { node.open = true; node.dispatchEvent(new Event('toggle')); }
    toTop(node, true);
    node.classList.remove('settings-flash');
    void node.offsetWidth;
    node.classList.add('settings-flash');
    timers.setTimeout(() => node.classList.remove('settings-flash'), 1600);
    follow(settleMs);
  };
  find(0);
  return stop;
}

/** One deep-link follow at a time, across sections that mount asynchronously.
 *
 *  `begin()` at the start of a section change hands out a ticket and stops the
 *  follow in progress. `reveal(ticket, pane, anchor, id)` runs after the section
 *  has mounted: it does nothing if a newer `begin()` happened meanwhile (a later
 *  link wins, whichever mount finishes first) or if `isActive(pane, id)` says the
 *  section is no longer on screen (Settings closed, another section, the phone
 *  list). `end()` stops the follow without voiding the ticket. */
export function createRevealer({ isActive = () => true, reveal = revealAnchor } = {}) {
  let gen = 0;
  let stop = () => {};
  const end = () => { const s = stop; stop = () => {}; s(); };
  return {
    begin() { end(); gen += 1; return gen; },
    reveal(ticket, pane, anchor, id) {
      if (!anchor || ticket !== gen || !isActive(pane, id)) return false;
      end();
      stop = reveal(pane, anchor) || (() => {});
      return true;
    },
    end,
  };
}
