// Settings -> Characters (DisPatch 4.2): one page, two tabs.
//
//   Mine        the bots only you can chat with: your copies of household templates, bots you
//               made, and (the Owner) the agent bots. Everything the old "My bots" section did.
//   Household   the templates an Admin hands out. Nobody chats with a template; every profile
//               it is given to gets its own copy that follows it. Admins only.
//
// The server is the authority: GET /api/characters/manage returns rows that already say what
// this caller may do (`actions`), what is on (`badges`) and what kind of bot each is. Rows,
// buttons and the menu are drawn from those fields alone - there is no role logic here
// (API-4.2.md). Pure helpers first (tests need no DOM), the DOM half after.
//
//   GET    /api/characters/manage                       -> { me, profiles, mine, household }
//   PATCH  /api/characters/{id}                         unified edit (Hide, Pictures, Lookups ...)
//   GET    /api/characters/{id}/give-to/preview, PUT .../give-to   (give-to.js)
//   POST   /api/characters/{id}/stop-sharing/preview, POST .../stop-sharing
//   POST   /api/me/characters/{id}/share | /reset | /clone ; DELETE /api/me/characters/{id}
//
// main.js calls initCharactersPage() once at boot and mountCharactersPage() when the section
// opens; refreshCharactersPage() follows every server `bots` change.

import { t } from './i18n.js?v=5';
import { el, spriteIcon } from './util.js?v=23';
import { nimEnabled } from './nim.js?v=8';
import { avatarSpec, markNode, isMineBot } from './profiles.js?v=6';
import { askDialog, errorText } from './people-perms.js?v=8';
import { kitSection } from './bot-kits.js?v=8';
import { voiceHash } from './bot-ids.js?v=1';
import {
  profileReq, can, renderBotEditor, renderAiBuild, loadEditorOptions, withShell,
} from './bot-editor.js?v=5';
import { entitled, toggleProfile, toggleEveryone, giveToCalls, changeGiveTo } from './give-to.js?v=3';
import { packDialog, packSummary } from './pack-dialog.js?v=1';

// ------------------------------------------------------------------ pure

/** The backend pill: colour AND words AND an icon, never colour alone. */
export const BACKEND_PILLS = Object.freeze({
  agent: { key: 'cpage.backend_agent', titleKey: 'cpage.backend_agent_tip', cls: 'ui-badge--accent', icon: 'bot' },
  direct: { key: 'cpage.backend_direct', titleKey: 'cpage.backend_direct_tip', cls: 'ui-badge--info', icon: 'sparkles' },
  app: { key: 'cpage.backend_app', titleKey: 'cpage.backend_app_tip', cls: 'ui-badge--warning', icon: 'puzzle' },
  'legacy-api': { key: 'cpage.backend_older', titleKey: 'cpage.backend_older_tip', cls: '', icon: 'plug' },
});

export function backendPill(row) {
  return BACKEND_PILLS[row && row.backend_kind] || BACKEND_PILLS.direct;
}

const isAgentish = (row) => row.backend_kind === 'agent' || row.backend_kind === 'app';
const isTemplateRow = (row) => !!row && row.kind === 'template';

/** The row a User sees for a template given to them whose copy they deleted: not a template
 *  to manage but one way back (`actions.make_copy`, no `give_to` list). */
export const isGetCopyRow = (row, inMine = false) => isTemplateRow(row) && !!(row.actions || {}).make_copy && (inMine || row.give_to === null || row.give_to === undefined);

/** The at-a-glance status icons: only what is on (plus "linked" / "customised" for a copy). */
export function rowBadges(row) {
  const b = (row && row.badges) || {};
  const out = [];
  const add = (id, icon, key, vars) => out.push({ id, icon, key, vars: vars || {} });
  if (b.voice) add('voice', 'mic', 'cpage.badge_voice');
  if (b.pictures) add('pictures', 'image', 'cpage.badge_pictures');
  if (b.reactions) add('reactions', 'thumbs-up', 'cpage.badge_reactions');
  if (b.lookups) add('lookups', 'globe', 'cpage.badge_lookups');
  if (b.quick_actions) add('quick', 'zap', 'cpage.badge_quick');
  if (row && row.kind === 'copy') {
    const n = (row.overridden || []).length;
    add(n ? 'custom' : 'linked', n ? 'pencil' : 'link',
      n ? 'cpage.badge_follows_changed' : 'cpage.badge_follows', { count: n, name: (row.template && row.template.name) || '' });
  }
  return out;
}

/** The quick buttons on a row: exactly the ones the row's actions allow. `patch` is the
 *  PATCH body for a toggle; `nav` marks the Voice button (it opens the voice page). */
export function rowQuick(row) {
  const a = (row && row.actions) || {};
  const b = (row && row.badges) || {};
  const out = [];
  if (a.hide && !isTemplateRow(row)) {
    out.push({ id: 'hide', icon: row.hidden ? 'eye-off' : 'eye',
      key: row.hidden ? 'cpage.quick_show' : 'cpage.quick_hide', tipKey: row.hidden ? 'cpage.quick_show_tip' : 'cpage.quick_hide_tip', patch: { hidden: !row.hidden } });
  }
  if (a.pictures) {
    out.push({ id: 'pictures', icon: b.pictures ? 'image' : 'image-off', pressed: !!b.pictures,
      key: 'cpage.quick_pictures_label', tipKey: b.pictures ? 'cpage.quick_pictures_off' : 'cpage.quick_pictures_on', patch: { image_jobs: !b.pictures } });
  }
  if (a.lookups) {
    out.push({ id: 'lookups', icon: 'globe', pressed: !!b.lookups,
      key: 'cpage.quick_lookups_label', tipKey: b.lookups ? 'cpage.quick_lookups_off' : 'cpage.quick_lookups_on', patch: { tools: !b.lookups } });
  }
  if (a.voice) out.push({ id: 'voice', icon: 'mic', key: 'cpage.quick_voice', tipKey: 'cpage.quick_voice_tip', nav: true });
  return out;
}

/** The ☰ menu: Details/Edit first, then what the server allows. `ctx` (all optional) adds the
 *  page's own items: `up` / `down` (Move up / Move down: reordering is on and there is a slot
 *  that way) and `parked` (true / false = the Tools & more toggle is offered; undefined = not). */
export function rowMenu(row, ctx = {}) {
  const a = (row && row.actions) || {};
  const out = a.details === false ? [] : [{ id: 'edit', key: a.edit ? 'cpage.menu_edit' : 'cpage.menu_details' }];
  if (ctx.up) out.push({ id: 'move_up', key: 'cpage.menu_move_up', icon: 'arrow-up' });
  if (ctx.down) out.push({ id: 'move_down', key: 'cpage.menu_move_down', icon: 'arrow-down' });
  if (typeof ctx.parked === 'boolean' && !isTemplateRow(row)) {
    out.push(ctx.parked ? { id: 'unpark', key: 'cpage.menu_unpark', icon: 'undo-2' } : { id: 'park', key: 'cpage.menu_park', icon: 'wrench' });
  }
  if (a.reset) out.push({ id: 'reset', key: 'cpage.menu_reset' });
  if (a.make_copy) out.push({ id: 'make_copy', key: 'cpage.menu_get_copy' });
  if (ctx.getCopy && !a.make_copy) out.push({ id: 'get_copy', key: 'cpage.menu_get_copy' });
  if (a.share) out.push({ id: 'share', key: 'cpage.menu_share' });
  if (a.stop_sharing) out.push({ id: 'stop_sharing', key: 'cpage.menu_stop_sharing' });
  // A template's Delete and Stop sharing are one action (Stop sharing, with its confirm token):
  // one item. An agent bot gets Delete only when the server says so (the Owner's deliberate delete).
  if (a.delete && !(isTemplateRow(row) && a.stop_sharing)) out.push({ id: 'delete', key: 'cpage.menu_delete', danger: true });
  return out;
}

/** A household bot the caller (an Admin) is not on the list for and has no copy of: the menu
 *  offers "Get my copy", which adds them to "Who can use it". (Entitled-but-removed is the
 *  server's own `actions.make_copy`.) */
export function needsOwnCopy(row, me) {
  const id = me && me.id;
  if (!id || !me.can_manage_household || !isTemplateRow(row) || ((row && row.actions) || {}).make_copy) return false;
  if ((row.copies || []).some((c) => c.profile_id === id && c.state === 'has')) return false;
  return !entitled(row, id);
}

export const FILTER_CHIPS = Object.freeze(['all', 'agent', 'direct', 'hidden']);
const FILTER_LABEL = { all: 'cpage.filter_all', agent: 'cpage.filter_agent', direct: 'cpage.filter_direct', hidden: 'cpage.filter_hidden' };
const COPY_STATE = { has: 'cpage.copy_has', removed: 'cpage.copy_removed', none: 'cpage.copy_none' };

/** Filter chip + search box. The chip narrows by backend (Agent = agent + app, Direct = direct
 *  + legacy API) or to hidden rows; the query matches name, id, description and model. */
export function filterRows(rows, { chip = 'all', query = '' } = {}) {
  const q = String(query || '').trim().toLowerCase();
  return (rows || []).filter((r) => {
    if (chip === 'agent' && !isAgentish(r)) return false;
    if (chip === 'direct' && isAgentish(r)) return false;
    if (chip === 'hidden' && !r.hidden) return false;
    if (!q) return true;
    return [r.name, r.id, r.description, r.model].some((v) => String(v || '').toLowerCase().includes(q));
  });
}

/** Is there a Details panel to open? (`actions.details === false` says no; absent = yes, except
 *  the get-my-copy row, which has nothing to show.) */
export const hasDetails = (row, inMine = false) => !isGetCopyRow(row, inMine) && (((row && row.actions) || {}).details !== false);

/** Mine in the order the rail draws it: by `order` (rows without one keep the server's order,
 *  after the ordered ones). A stable sort, so equal orders never swap. */
export function sortByOrder(rows) {
  const big = Number.MAX_SAFE_INTEGER;
  return (rows || []).map((r, i) => [r, i])
    .sort(([a, i], [b, j]) => ((Number.isFinite(a.order) ? a.order : big) - (Number.isFinite(b.order) ? b.order : big)) || (i - j))
    .map(([r]) => r);
}

/** The ids the caller may reorder (`actions.reorder`), in list order. */
export const reorderIds = (rows) => (rows || []).filter((r) => !!((r && r.actions) || {}).reorder).map((r) => r.id);

/** Move `id` to place `to` (0-based) among the reorderable rows. The other rows (a Get-my-copy
 *  row, say) keep their slots: the reorderable rows are rewritten into the slots they held,
 *  which is also how the server writes the order. Returns { rows, ids, position } or null when
 *  nothing moves. `position` is 1-based, for "Moved X to position N". */
export function reorderPlan(rows, id, to) {
  const all = [...(rows || [])];
  const slots = [];
  all.forEach((r, i) => { if (((r && r.actions) || {}).reorder) slots.push(i); });
  const sub = slots.map((i) => all[i]);
  const from = sub.findIndex((r) => r.id === id);
  if (from < 0) return null;
  const dest = Math.max(0, Math.min(Number(to), sub.length - 1));
  if (!Number.isFinite(dest) || dest === from) return null;
  const [moved] = sub.splice(from, 1);
  sub.splice(dest, 0, moved);
  slots.forEach((slot, k) => { all[slot] = sub[k]; });
  return { rows: all, ids: sub.map((r) => r.id), position: dest + 1 };
}

/** The Details fields an add-on's per-character control (`ext.bots.rowExtra`) may set: the
 *  voice add-on's two strings, nothing else. */
export const EXTRA_FIELDS = Object.freeze(['voice', 'voice_rp']);
export function extrasPatch(patch) {
  const out = {};
  for (const [k, v] of Object.entries(patch || {})) if (EXTRA_FIELDS.includes(k) && typeof v === 'string') out[k] = v;
  return out;
}

/** May this rail tile be dragged? A signed-in profile reorders its own bots (their order is its
 *  own, `PUT /api/me/characters/order`); on an install nobody has claimed yet the roster is the
 *  household's, and only an Admin moves it (the old `PUT /api/bots/order`). `auth`:
 *  { claimed, signedIn, profile, admin }. */
export function railDragAllowed(bot, auth = {}) {
  if (!auth.claimed) return !!auth.admin;
  return !!(auth.signedIn || auth.profile) && isMineBot(bot);
}

/** Save the rail's order after a drag (`bots` = the whole roster, in its new order). */
export async function saveRailOrder(bots, auth = {}, { call = profileReq, saveOrder = null } = {}) {
  const list = bots || [];
  if (!auth.claimed) {
    if (!auth.admin || typeof saveOrder !== 'function') return null;
    return saveOrder(list.map((b, i) => ({ id: b.id, order: i })));
  }
  const ids = list.filter(isMineBot).map((b) => b.id);
  if (!ids.length) return null;
  return call('PUT', '/api/me/characters/order', { json: { ids } });
}

/** Which tabs this caller sees. Mine always; Household only for a household manager. */
/** What each tab button shows: an icon, the label, a one-line hint under it (4.4: the tabs read
 *  as two big choices, not two words above the list). */
export const TAB_INFO = Object.freeze({
  mine: { icon: 'user', key: 'cpage.tab_mine', hintKey: 'cpage.tab_mine_hint' },
  household: { icon: 'users', key: 'cpage.tab_household', hintKey: 'cpage.tab_household_hint' },
});

export function visibleTabs(data) {
  const me = (data && data.me) || {};
  return me.can_manage_household ? ['mine', 'household'] : ['mine'];
}

/** The PATCH body for what the Details form changed (only fields the kind's allowlist has). */
export function detailPatch(row, orig, form) {
  const out = {};
  const be = (orig && orig.backend) || {};
  if (form.name !== undefined && form.name.trim() !== (orig.name || '')) out.name = form.name.trim();
  if (form.emoji !== undefined && form.emoji.trim() !== (orig.emoji || '')) out.emoji = form.emoji.trim();
  if (form.description !== undefined && form.description.trim() !== (orig.description || '').trim()) out.description = form.description.trim();
  if (form.persona !== undefined && form.persona.trim() !== (((orig.personality || {}).text) || '').trim()) out.persona = form.persona.trim();
  if (!isAgentish(row)) {
    if (form.connection !== undefined && (form.connection || '') !== (be.connection || '')) {
      out.connection = form.connection || null;
      if (form.connection && form.model) out.model = form.model;
    } else if (form.model !== undefined && form.model && form.model !== (be.model || '')) {
      out.model = form.model;
    }
    if (form.tools !== undefined && !!form.tools !== !!(row.badges || {}).lookups) out.tools = !!form.tools;
  }
  if (form.hidden !== undefined && !!form.hidden !== !!row.hidden) out.hidden = !!form.hidden;
  if (form.image_jobs !== undefined && !!form.image_jobs !== !!(row.badges || {}).pictures) out.image_jobs = !!form.image_jobs;
  if (form.quick_actions !== undefined && !!form.quick_actions !== !!(row.badges || {}).quick_actions) out.quick_actions = !!form.quick_actions;
  if (form.reactions !== undefined && !!form.reactions !== !!(row.badges || {}).reactions) out.reactions = !!form.reactions;
  if (form.avatar_pool !== undefined && !!form.avatar_pool !== !!(row.badges || {}).avatar_pool) out.avatar_pool = !!form.avatar_pool;
  return out;
}

// ------------------------------------------------------------------ module state

let shell = {};
let lastMe = {};         // the last manage response's `me`, for the Add menu (who may make an agent bot)
let mounted = null;      // { reload } while the page is on screen
let addToolbar = null;   // main.js's household Add toolbar: moved into the page on mount, so keep it for the next mount

/** The signed-in profile as the Characters page last saw it ({ protected, can_manage_household, ... }). */
export function charactersMe() { return lastMe; }

/** Called once at boot with the shell's helpers ({ toast, confirm, state, openTab }, plus
 *  { botRowExtrasFor, isMenuBot, toggleMenuBot, renderSidebar } for the add-on slot and the
 *  Tools & more toggle). */
export function initCharactersPage(deps = {}) { shell = deps || {}; }

/** Settings left Characters: stop following the roster. */
export function unmountCharactersPage() { if (mounted) mounted.destroy(); }

/** The server changed a bot (a `bots` frame, an Add, a Delete): refetch if the page is open.
 *  `{ extras: true }`: an add-on's per-character control came or went, so open Details panels
 *  are drawn again (with or without its slot). */
export function refreshCharactersPage(opts = {}) { if (mounted) mounted.reloadSoon(opts); }

const enc = encodeURIComponent;
// The last tab this page showed, for the session only (a device preference would have to join
// privacy mode's wipe list; the tab is not worth that).
let lastTab = '';
const remember = (v) => { lastTab = v; };
const recall = () => lastTab;

// ------------------------------------------------------------------ DOM bits

function pictureNode(item, cls) {
  const noImg = (() => { try { return nimEnabled(); } catch { return false; } })();
  const src = typeof item.avatar === 'string' ? item.avatar : '';
  const letter = () => el('span', { class: `${cls} cp-avatar-emoji`, 'aria-hidden': 'true', text: item.emoji || [...(item.name || '?')][0] || '•' });
  if (src && !noImg) {
    const img = el('img', { class: cls, src, alt: '', width: '40', height: '40', loading: 'lazy' });
    img.addEventListener('error', () => img.replaceWith(letter()), { once: true });   // a picture the server will not serve (403/404): the emoji, not a broken icon
    return img;
  }
  return letter();
}

function profileFace(p) {
  const spec = avatarSpec(p, { noImages: (() => { try { return nimEnabled(); } catch { return false; } })() });
  if (spec.kind === 'mark') return markNode('cp-face');
  if (spec.kind === 'img') return el('img', { class: 'cp-face', src: spec.src, alt: '', width: '28', height: '28', loading: 'lazy' });
  return el('span', { class: 'cp-face cp-face-text', 'aria-hidden': 'true', text: spec.text });
}

function iconBtn({ icon, label, text, pressed, cls = '', onclick, id }) {
  const b = el('button', { type: 'button', class: `cp-btn ${cls}`.trim(), title: label, 'aria-label': label,
    ...(pressed === undefined ? {} : { 'aria-pressed': String(!!pressed) }), ...(id ? { dataset: { q: id } } : {}) }, [
    spriteIcon(icon, 16), text ? el('span', { class: 'cp-btn-text', text }) : null]);
  if (onclick) b.addEventListener('click', onclick);
  return b;
}

let openedMenu = null;
function closeMenu({ focus = false } = {}) {
  if (!openedMenu) return;
  const { menu, btn, off } = openedMenu;
  openedMenu = null; off(); menu.remove();
  btn.setAttribute('aria-expanded', 'false');
  if (focus) { try { btn.focus(); } catch { /* ignore */ } }
}
function popMenu(btn, items) {
  const again = openedMenu && openedMenu.btn === btn;
  closeMenu();
  if (again) return;
  const menu = el('div', { class: 'menu cp-menu', role: 'menu' });
  for (const it of items) {
    const b = it.icon
      ? el('button', { type: 'button', role: 'menuitem', class: `menu-item cp-menu-item${it.danger ? ' danger-text' : ''}`, dataset: { item: it.id || '' } }, [spriteIcon(it.icon, 16), el('span', { text: it.label })])
      : el('button', { type: 'button', role: 'menuitem', class: `menu-item${it.danger ? ' danger-text' : ''}`, text: it.label, ...(it.id ? { dataset: { item: it.id } } : {}) });
    b.addEventListener('click', (e) => { e.stopPropagation(); closeMenu(); it.run(); });
    menu.append(b);
  }
  btn.after(menu);
  btn.setAttribute('aria-expanded', 'true');
  const onDoc = (e) => { if (!menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) closeMenu(); };
  const onKey = (e) => {
    const els = [...menu.querySelectorAll('button')];
    const i = els.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu({ focus: true }); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); (els[(i + 1) % els.length] || els[0]).focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (els[(i - 1 + els.length) % els.length] || els[0]).focus(); }
    else if (e.key === 'Tab') closeMenu();
  };
  document.addEventListener('click', onDoc, true);
  menu.addEventListener('keydown', onKey);
  openedMenu = { menu, btn, off: () => document.removeEventListener('click', onDoc, true) };
  const first = menu.querySelector('button');
  if (first) first.focus();
}

// ------------------------------------------------------------------ the page

/** Fill `host` (the Characters pane's page container). `opts.anchor`: 'mine' | 'household'. */
export function mountCharactersPage(host, opts = {}) {
  if (!host) return null;
  const d = withShell({ ...shell, ...opts.deps });
  const call = opts.call || profileReq;
  const say = (msg, bad) => { if (d.toast) d.toast(msg, !!bad); };
  const ask = opts.ask || ((o) => askDialog(o));
  const confirmBox = async (o) => (await ask({ danger: false, ...o })) === 'ok';

  let data = { me: {}, profiles: [], mine: [], household: [] };
  let tab = opts.anchor === 'household' || opts.anchor === 'mine' ? opts.anchor : (recall() || 'mine');
  let chip = 'all';
  let query = '';
  const open = new Set();
  const panels = new Map();            // `${tab}:${id}` -> Details node, kept across repaints
  const panelSig = new Map();          // `${tab}:${id}` -> the row (as JSON) the panel was built from
  let loaded = false;
  let timer = null;

  let dropPanels = false;              // refresh({ extras }) : draw open panels again
  const banner = el('div', { class: 'yo-error', role: 'alert', hidden: 'hidden' });
  // Move up / Move down / a drop say where the bot went (screen readers; the list repaints).
  const live = el('div', { class: 'sr-only cp-live', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
  const reorderHint = el('p', { class: 'muted cp-reorder-hint', hidden: 'hidden', text: t('cpage.reorder_hint') });
  const tabs = el('div', { class: 'ui-tabs cp-tabs', role: 'tablist', 'aria-label': t('cpage.tabs_aria') });
  const lead = el('p', { class: 'muted cp-lead' });
  // A Basic profile has no Details and no switches: say why, once, instead of leaving it to guess.
  const basicNote = el('p', { class: 'muted cp-basic-note', hidden: 'hidden', text: t('cpage.basic_note') });
  // The shared default profile is used by everyone on it: "only you" would be false there.
  const shared = () => typeof d.isShared === 'function' && !!d.isShared();
  const toolbar = el('div', { class: 'cp-toolbar' });
  const filters = el('div', { class: 'cp-filters ui-segmented', role: 'group', 'aria-label': t('cpage.filter_aria') });
  const search = el('input', { type: 'search', class: 'ui-input cp-search', placeholder: t('cpage.search'), 'aria-label': t('cpage.search_aria'), autocomplete: 'off' });
  const list = el('ul', { class: 'cp-list', 'aria-live': 'polite' });
  const empty = el('p', { class: 'muted cp-empty', hidden: 'hidden' });
  const addSlot = el('div', { class: 'cp-add-slot' });
  // 4.4: the starter packs as cards on Household (also in the Add menu), for manage_characters.
  const packsBox = el('section', { class: 'cp-packs', hidden: 'hidden', 'aria-labelledby': 'cp-packs-h' });
  let packs = null;                    // GET /api/starter-packs rows; null = not fetched yet
  let packsStale = true;
  let packsLoading = false;
  const main = el('div', { class: 'cp-main-view' }, [lead, basicNote, toolbar, addSlot, packsBox, el('div', { class: 'cp-find' }, [filters, search]), reorderHint, banner, empty, list, live]);
  const stage = el('div', { class: 'cp-stage', hidden: 'hidden' });
  const page = el('section', { class: 'cp-page dev-group', 'aria-labelledby': 'cp-h' }, [
    el('h3', { class: 'dev-group-title', id: 'cp-h', text: t('settings.sec_characters') }), tabs, main, stage]);
  host.replaceChildren(page);
  // main.js owns the Add menu. A second mount finds it detached (the first mount moved it into
  // the old page, which replaceChildren just dropped), so it is found by the kept reference.
  { const add = document.getElementById('chars-add-toolbar') || (addToolbar && addToolbar.ownerDocument === document ? addToolbar : null);
    if (add) { addToolbar = add; addSlot.append(add); } }

  const rowsOf = (which) => (which === 'household' ? data.household : data.mine) || [];
  // Panels survive a refresh unless their row changed. The voice fields are the add-on's own
  // control's (it shows its saved value itself): a voice save must not throw away the form.
  const sigOf = (r) => JSON.stringify({ ...r, voice: undefined, voice_rp: undefined, badges: { ...(r.badges || {}), voice: undefined } });
  const profiles = () => data.profiles || [];

  async function load() {
    try {
      data = await call('GET', '/api/characters/manage');
      if (data && Array.isArray(data.mine)) data.mine = sortByOrder(data.mine);
      lastMe = (data && data.me) || {};
      banner.hidden = true;
    } catch (e) {
      banner.textContent = errorText(e); banner.hidden = false;
    }
    loaded = true;
    { const sum = document.getElementById('ssum-characters');   // the phone list's one-liner: your characters
      if (sum && banner.hidden) { sum.textContent = t('settings.sum_characters', { count: (data.mine || []).length }); sum.dataset.counted = '1'; } }
    // A panel built from an older row would show stale switches and chips: drop it.
    const all = dropPanels;
    dropPanels = false;
    for (const key of [...panels.keys()]) {
      const i = key.indexOf(':');
      const cur = rowsOf(key.slice(0, i)).find((r) => r.id === key.slice(i + 1));
      if (all || !cur || sigOf(cur) !== panelSig.get(key)) { panels.delete(key); panelSig.delete(key); }
    }
    if (!visibleTabs(data).includes(tab)) tab = 'mine';
    packsStale = true;                 // a bot added or removed changes what each pack has added
    paint();
  }
  const reload = () => load();
  const reloadSoon = (o = {}) => { if (o && o.extras) dropPanels = true; clearTimeout(timer); timer = setTimeout(load, 150); };
  mounted = { reload, reloadSoon, destroy: () => { mounted = null; clearTimeout(timer); closeMenu(); } };

  function paintTabs() {
    const vis = visibleTabs(data);
    tabs.replaceChildren(...vis.map((id) => {
      const n = rowsOf(id).length;
      const info = TAB_INFO[id] || TAB_INFO.mine;
      const b = el('button', { type: 'button', role: 'tab', class: 'ui-tab cp-tab', id: `cp-tab-${id}`, dataset: { tab: id },
        'aria-selected': String(tab === id), 'aria-controls': 'cp-panel', tabindex: tab === id ? '0' : '-1' }, [
        el('span', { class: 'cp-tab-top' }, [
          spriteIcon(info.icon, 18),
          el('span', { class: 'cp-tab-label', text: t(info.key) }),
          el('span', { class: 'cp-count', text: String(n), title: t('cpage.tab_count', { count: n }) })]),
        el('span', { class: 'cp-tab-hint', text: t(info.hintKey) })]);
      b.addEventListener('click', () => { tab = id; remember(id); chip = 'all'; paint(); });
      b.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        const next = vis[(vis.indexOf(id) + (e.key === 'ArrowRight' ? 1 : vis.length - 1)) % vis.length];
        tab = next; remember(next); paint();
        const nb = tabs.querySelector(`[data-tab="${next}"]`); if (nb) nb.focus();
      });
      return b;
    }));
    tabs.hidden = vis.length < 2 || !stage.hidden;   // a refresh must not re-show the tabs over the open editor
    main.setAttribute('role', 'tabpanel'); main.id = 'cp-panel'; main.setAttribute('aria-labelledby', `cp-tab-${tab}`);
  }

  function paintToolbar() {
    const kids = [];
    if (tab === 'mine' && (data.me || {}).can_create) {
      kids.push(el('button', { class: 'ui-btn ui-btn--primary', type: 'button', id: 'mb-new', text: t('mybots.new'), onclick: () => openStage((box) => renderBotEditor(box, null, { ...d, householdAvatars: household4Editor() })) }),
        el('button', { class: 'ui-btn', type: 'button', id: 'mb-build', text: t('mybots.ai_build'), onclick: () => openStage(async (box) => {
          const options = await loadEditorOptions(d);
          return renderAiBuild(box, { ...d, options, householdAvatars: household4Editor() });
        }) }));
    }
    toolbar.replaceChildren(...kids);
    toolbar.hidden = !kids.length;
    // Add (blank / starter pack) is the household's: main.js owns its menu, shown on this tab only.
    const add = addToolbar && addToolbar.ownerDocument === document ? addToolbar : document.getElementById('chars-add-toolbar');
    if (add) add.hidden = !(tab === 'household' && (data.me || {}).can_manage_household);
    // A profile that may only reorder (Basic: no create, no row it may edit) is told what it can do here.
    const me = data.me || {};
    const orderOnly = tab === 'mine' && !me.can_create && !me.can_manage_household
      && !(data.mine || []).some((r) => ((r && r.actions) || {}).edit);
    lead.textContent = t(tab !== 'mine' ? 'cpage.lead_household'
      : shared() ? 'cpage.lead_mine_shared' : orderOnly ? 'cpage.lead_mine_order' : 'cpage.lead_mine');
    basicNote.hidden = !(tab === 'mine' && me.role === 'basic');
    paintPacks();
  }

  // -------------------------------------------------------------- starter packs (Household)
  const mayAddPacks = () => tab === 'household' && !!(data.me || {}).can_manage_household;
  async function loadPacks() {
    if (packsLoading) return;
    packsLoading = true; packsStale = false;
    try {
      const r = await call('GET', '/api/starter-packs');
      packs = (r && Array.isArray(r.packs)) ? r.packs : [];
    } catch { packs = []; }
    packsLoading = false;
    paintPacks();
  }
  function paintPacks() {
    if (!mayAddPacks()) { packsBox.hidden = true; return; }
    if (packsStale) { loadPacks(); if (packs === null) { packsBox.hidden = true; return; } }
    if (!packs || !packs.length) { packsBox.hidden = true; return; }
    packsBox.hidden = false;
    packsBox.replaceChildren(
      el('h4', { class: 'cp-packs-title', id: 'cp-packs-h', text: t('packs.title') }),
      el('p', { class: 'muted cp-packs-hint', text: t('packs.hint') }),
      el('ul', { class: 'cp-packs-row' }, packs.map((pk) => {
        const faces = (pk.characters || []).slice(0, 4).map((c) => c.emoji || '').join(' ');
        const btn = el('button', { type: 'button', class: 'cp-pack-card', dataset: { pack: pk.id },
          'aria-haspopup': 'dialog' }, [
          el('span', { class: 'cp-pack-faces', 'aria-hidden': 'true', text: faces }),
          el('span', { class: 'cp-pack-name', text: pk.name }),
          el('span', { class: 'cp-pack-sum', text: packSummary(pk) }),
          el('span', { class: 'cp-pack-go', text: t(pk.installed ? 'packs.see' : 'packs.choose') }),
        ]);
        btn.addEventListener('click', () => openPack(pk));
        return el('li', {}, [btn]);
      })));
  }
  async function openPack(pk) {
    const c = await call('GET', '/api/connections').catch(() => null);
    const r = await packDialog(pk, {
      connections: (c && c.connections) || [], toast: say, errorText,
      install: (id, body) => call('POST', `/api/starter-packs/${enc(id)}/install`, { json: body }),
    });
    if (r) { packsStale = true; await load(); }
  }
  const household4Editor = () => (data.household || []).map((h) => ({ id: h.id, name: h.name, avatar_url: h.avatar }));

  function paintFilters() {
    filters.replaceChildren(...FILTER_CHIPS.map((id) => {
      const b = el('button', { type: 'button', class: 'cp-chip', 'aria-pressed': String(chip === id), dataset: { chip: id },
        text: t(FILTER_LABEL[id]) });
      b.addEventListener('click', () => { chip = id; paintList(); paintFilters(); });
      return b;
    }));
  }

  /** Reordering is on for Mine with no filter and no search (a filtered list has gaps). */
  const reorderOn = () => tab === 'mine' && chip === 'all' && !String(query || '').trim();

  function paintList() {
    const all = rowsOf(tab);
    const shown = filterRows(all, { chip, query });
    // A repaint (a refetch after Move up / down, a `bots` frame) keeps the keyboard user's place:
    // the same control on the same row gets focus back.
    const act = document.activeElement;
    const actLi = act && list.contains(act) ? act.closest('.cp-row') : null;
    const actCls = actLi ? ['cp-menu-btn', 'cp-details-btn'].find((c) => act.classList.contains(c)) : null;
    list.replaceChildren(...shown.map((r) => rowNode(r)));
    if (actLi && actCls) {
      const again = [...list.children].find((x) => x.dataset.bot === actLi.dataset.bot);
      const target = again && again.querySelector(`.${actCls}`);
      if (target) { try { target.focus({ preventScroll: true }); } catch { /* ignore */ } }
    }
    reorderHint.hidden = !(tab === 'mine' && !reorderOn() && reorderIds(all).length > 1);
    empty.hidden = shown.length > 0 || !loaded;
    empty.textContent = all.length ? t('cpage.none_match') : t(tab === 'mine' ? 'cpage.none_mine' : 'cpage.none_household');
  }

  function paint() {
    paintTabs(); paintToolbar(); paintFilters(); paintList();
  }

  search.addEventListener('input', () => { query = search.value; paintList(); });

  // -------------------------------------------------------------- stage (full editor / AI build)
  async function openStage(run) {
    main.hidden = true; stage.hidden = false; tabs.hidden = true;
    let saved = null;
    try { saved = await run(stage); } finally {
      stage.hidden = true; stage.replaceChildren(); main.hidden = false; tabs.hidden = visibleTabs(data).length < 2;
    }
    if (saved) await load();
  }

  // -------------------------------------------------------------- actions
  async function patchRow(row, body, okMsg) {
    try {
      await call('PATCH', `/api/characters/${enc(row.id)}`, { json: body });
      if (okMsg) say(okMsg);
      panels.delete(`${tab}:${row.id}`);
      await load();
      return true;
    } catch (e) { say(errorText(e), true); return false; }
  }

  /** Move a bot to place `to` among the reorderable rows: drawn at once, then saved. */
  async function doMove(row, to, { focus = false } = {}) {
    const plan = reorderPlan(data.mine, row.id, to);
    if (!plan) return;
    data.mine = plan.rows.map((r, i) => ({ ...r, order: i }));
    paintList();
    live.textContent = t('cpage.moved', { name: row.name, position: plan.position });
    if (focus) {
      const li = [...list.children].find((x) => x.dataset.bot === row.id);
      const btn = li && li.querySelector('.cp-menu-btn');
      if (btn) { try { btn.focus(); } catch { /* ignore */ } }
    }
    try {
      await call('PUT', '/api/me/characters/order', { json: { ids: plan.ids } });
    } catch (e) { say(errorText(e), true); }
    reloadSoon();
  }
  const moveBy = (row, delta) => {
    const k = reorderIds(data.mine).indexOf(row.id);
    if (k >= 0) doMove(row, k + delta, { focus: true });
  };

  /** Tools & more (this device): the shell's menubots toggle, then the rail repaints. */
  function togglePark(row) {
    if (typeof d.toggleMenuBot !== 'function') return;
    const now = d.toggleMenuBot(row.id);
    try { if (d.renderSidebar) d.renderSidebar(); } catch { /* the rail repaints on its next frame */ }
    say(t(now ? 'cpage.parked_done' : 'cpage.unparked_done', { name: row.name }));
    paintList();
  }
  const parkedState = (row) => (typeof d.isMenuBot === 'function' && typeof d.toggleMenuBot === 'function' && !isTemplateRow(row) && !isGetCopyRow(row, tab === 'mine')
    ? !!d.isMenuBot(row.id) : undefined);

  /** The add-on's per-character control changed: only `voice` / `voice_rp` strings are saved. */
  async function saveExtras(row, patch) {
    const body = extrasPatch(patch);
    if (!Object.keys(body).length) return;
    try {
      await call('PATCH', `/api/characters/${enc(row.id)}`, { json: body });
      say(t('cpage.voice_saved', { name: row.name }));
      reloadSoon();
    } catch (e) { say(errorText(e), true); }
  }

  function goVoice(row) {
    try { window.location.hash = voiceHash(row.id); } catch { /* ignore */ }
    if (d.openTab) d.openTab(`voice#${(row.voice_link && row.voice_link.anchor) || 'voices'}`);
  }

  async function doReset(row) {
    if (!(await confirmBox({ title: t('cpage.reset_title', { name: row.name }), body: t('cpage.reset_confirm', { name: row.name }), okText: t('cpage.menu_reset') }))) return;
    try {
      await call('POST', `/api/me/characters/${enc(row.id)}/reset`);
      say(t('cpage.reset_done', { name: row.name }));
      panels.delete(`${tab}:${row.id}`);
      await load();
    } catch (e) { say(errorText(e), true); }
  }

  async function doDelete(row) {
    if (isTemplateRow(row)) { await doStopSharing(row); return; }   // one action, its token flow
    const body = t(row.kind === 'copy' ? 'cpage.delete_copy_confirm' : row.kind === 'agent' ? 'cpage.delete_agent_confirm' : 'cpage.delete_confirm', { name: row.name });
    if (!(await confirmBox({ title: t('cpage.delete_title', { name: row.name }), body, okText: t('cpage.menu_delete'), danger: true }))) return;
    try {
      if (row.kind === 'agent') {
        // The Owner's deliberate delete: the household route takes the protected Admin's own agent
        // bot; an older server only knows the personal one (and answers 409 agent_bot_protected).
        try { await call('DELETE', `/api/characters/${enc(row.id)}`); } catch (e) {
          if (e && (e.status === 404 || e.status === 405)) await call('DELETE', `/api/me/characters/${enc(row.id)}`); else throw e;
        }
      } else await call('DELETE', `/api/me/characters/${enc(row.id)}`);
      say(t('cpage.deleted', { name: row.name }));
      await load();
    } catch (e) { say(e.code === 'agent_bot_protected' ? t('cpage.agent_protected') : errorText(e), true); }
  }

  async function doMakeCopy(row) {
    try {
      const out = await call('POST', `/api/characters/${enc(row.id)}/clone`);
      say(t('cpage.copy_done', { name: row.name }));
      if (out && out.voice_cleared) say(t('copybot.voice_cleared'));
      tab = 'mine'; remember('mine');
      await load();
    } catch (e) { say(errorText(e), true); }
  }

  async function doShare(row) {
    if (!(await confirmBox({ title: t('cpage.share_title', { name: row.name }), body: t('cpage.share_body', { name: row.name }), okText: t('cpage.menu_share') }))) return;
    try {
      await call('POST', `/api/me/characters/${enc(row.id)}/share`);
      say(t('cpage.shared', { name: row.name }));
      await load();
    } catch (e) { say(e.code === 'not_shareable' ? t('cpage.not_shareable') : errorText(e), true); }
  }

  async function doStopSharing(row) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      let pv;
      try { pv = await call('POST', `/api/characters/${enc(row.id)}/stop-sharing/preview`); } catch (e) { say(errorText(e), true); return; }
      const who = (pv.detach || []).map((id) => ((profiles().find((p) => p.id === id)) || {}).name || id);
      if (!(await confirmBox({
        title: t('cpage.stop_title', { name: row.name }),
        body: t('cpage.stop_body'),
        tally: [{ n: who.length, label: t('cpage.stop_tally_copies') }],
        notes: who.length ? [t('cpage.stop_who', { names: who.join(', ') })] : [],
        okText: t('cpage.menu_stop_sharing'), danger: true }))) return;
      try {
        await call('POST', `/api/characters/${enc(row.id)}/stop-sharing`, { json: { token: pv.token } });
        say(t('cpage.stopped', { name: row.name }));
        await load();
        return;
      } catch (e) {
        if (e.code === 'stop_sharing_stale' && attempt === 0) continue;
        say(errorText(e), true);
        return;
      }
    }
  }

  const MENU_RUN = {
    edit: (row) => {
      open.add(`${tab}:${row.id}`); paintList();
      const li = [...list.children].find((x) => x.dataset.bot === row.id);
      if (li && li.scrollIntoView) li.scrollIntoView({ block: 'nearest' });
    },
    reset: doReset, delete: doDelete, make_copy: doMakeCopy, share: doShare, stop_sharing: doStopSharing,
    // An Admin not on a household bot's list: put yourself on it (the usual preview + confirm).
    get_copy: (row) => applyGiveTo(row, toggleProfile(row, (data.me || {}).id, true, profiles())),
    move_up: (row) => moveBy(row, -1), move_down: (row) => moveBy(row, 1), park: togglePark, unpark: togglePark,
  };

  // -------------------------------------------------------------- "Who can use it"
  const gtCalls = giveToCalls((method, url, body) => call(method, url, body === undefined ? {} : { json: body }));

  async function applyGiveTo(row, next) {
    try {
      const fresh = await changeGiveTo({ row, next, profiles: profiles(), calls: gtCalls, ask });
      if (fresh) { say(t('cpage.change_saved', { name: row.name })); panels.delete(`${tab}:${row.id}`); await load(); }
      else paintList();
    } catch (e) { say(e.code === 'give_to_stale' ? t('cpage.change_stale') : errorText(e), true); await load(); }
  }

  function whoChips(row) {
    const everyone = row.give_to === 'everyone';
    const box = el('div', { class: 'cp-who', role: 'group', 'aria-label': t('cpage.who_title') });
    const chipBtn = (label, on, face, onclick, extra = {}) => {
      const b = el('button', { type: 'button', class: 'cp-who-chip', 'aria-pressed': String(on), title: label,
        'aria-label': `${label}: ${t(on ? 'cpage.who_on' : 'cpage.who_off')}`, ...(extra.dataset ? { dataset: extra.dataset } : {}) }, [face, el('span', { class: 'cp-who-name', text: label })]);
      b.addEventListener('click', onclick);
      return b;
    };
    box.append(chipBtn(t('cpage.who_everyone_chip'), everyone, spriteIcon('users', 16), () => applyGiveTo(row, toggleEveryone(row, !everyone, profiles())), { dataset: { who: 'everyone' } }));
    for (const p of profiles()) {
      const on = entitled(row, p.id);
      box.append(chipBtn(p.name, on, profileFace(p), () => applyGiveTo(row, toggleProfile(row, p.id, !on, profiles())), { dataset: { who: p.id } }));
    }
    return box;
  }

  // -------------------------------------------------------------- details
  async function detailsNode(row) {
    const key = `${tab}:${row.id}`;
    if (panels.has(key)) return panels.get(key);
    const ro = !(row.actions || {}).edit;
    const box = el('div', { class: 'cp-detail-body' }, [el('p', { class: 'muted', text: t('chars.loading') })]);
    panels.set(key, box);
    panelSig.set(key, sigOf(row));
    const isTemplate = row.kind === 'template';
    let orig = {};
    let conns = [];
    try {
      orig = await call('GET', isTemplate ? `/api/characters/${enc(row.id)}` : `/api/me/characters/${enc(row.id)}`);
      if (!isAgentish(row)) {
        const c = await call('GET', isTemplate || can('manage_characters', d) ? '/api/connections' : '/api/me/backends').catch(() => null);
        conns = (c && (Array.isArray(c) ? c : c.connections)) || [];
      }
    } catch (e) {
      box.replaceChildren(el('p', { class: 'lock-error', role: 'alert', text: errorText(e) }));
      panels.delete(key); panelSig.delete(key);   // a failed fetch is not cached: the next open retries
      return box;
    }

    const uid = `cpd-${row.id}`.replace(/[^a-zA-Z0-9_-]/g, '-');
    const txt = (id, val, max, type = 'text') => el('input', { type, class: 'ui-input', id: `${uid}-${id}`, maxlength: String(max), value: val || '', ...(ro ? { disabled: 'disabled' } : {}) });
    const field = (label, input, hint) => el('div', { class: 'sec-field people-field' }, [
      el('label', { for: input.id, text: label }), input, hint ? el('span', { class: 'people-field-hint', text: hint }) : null]);
    const sw = (id, label, hint, on, enabled = true) => {
      const cb = el('input', { type: 'checkbox', role: 'switch', class: 'ui-checkbox', id: `${uid}-${id}`, ...((ro || !enabled) ? { disabled: 'disabled' } : {}) });
      cb.checked = !!on;
      return { cb, row: el('div', { class: 'dev-row chr-feature', dataset: { feature: id } }, [
        el('label', { class: 'dev-row-text', for: cb.id }, [el('strong', { text: label }), hint ? el('span', { class: 'dev-row-hint', text: hint }) : null]), cb]) };
    };

    const f = {};
    f.name = txt('name', orig.name || row.name, 40);
    f.emoji = txt('emoji', orig.emoji || row.emoji, 8);
    f.description = txt('description', orig.description || row.description, 400);
    const identity = [field(t('cpage.field_name'), f.name), field(t('cpage.field_emoji'), f.emoji), field(t('cpage.field_description'), f.description)];

    const brain = [];
    if (isAgentish(row)) {
      brain.push(el('p', { class: 'people-field-hint', text: t('chars.agent_note') }));
    } else {
      // A profile that may not choose backends (no list came back) sees the current choice, locked:
      // an empty list must never read as "no connection" and clear it on Save.
      const locked = ro || !conns.length;
      const cur = (orig.backend && orig.backend.connection) || '';
      const opts = conns.map((c) => ({ id: c.id, name: c.name || c.id }));
      if (cur && !opts.some((o) => o.id === cur)) opts.unshift({ id: cur, name: cur });
      f.connection = el('select', { class: 'ui-select', id: `${uid}-conn`, ...(locked ? { disabled: 'disabled' } : {}) }, [
        el('option', { value: '', text: t('chars.no_connection') }),
        ...opts.map((o) => el('option', { value: o.id, text: o.name }))]);
      f.connection.value = cur;
      f.model = el('input', { type: 'text', class: 'ui-input', id: `${uid}-model`, autocomplete: 'off', spellcheck: 'false', value: (orig.backend && orig.backend.model) || '', ...(locked ? { disabled: 'disabled' } : {}) });
      brain.push(field(t('chars.connection'), f.connection), field(t('chars.model'), f.model));
    }
    f.persona = el('textarea', { class: 'ui-input cp-persona', id: `${uid}-persona`, rows: '5', maxlength: '4000', ...(ro ? { disabled: 'disabled' } : {}) });
    f.persona.value = (orig.personality && orig.personality.text) || '';
    brain.push(field(t('cpage.field_personality'), f.persona, t('cpage.field_personality_hint')));

    const a = row.actions || {};
    const b = row.badges || {};
    const sQuick = sw('quick', t('cpage.switch_quick'), t('cpage.switch_quick_hint'), b.quick_actions);
    const sPics = sw('pictures', t('cpage.switch_pictures'), t('chars.feat_pictures_hint'), b.pictures, a.pictures !== false);
    const sHide = isTemplate ? null : sw('hidden', t('cpage.switch_hidden'), t('cpage.hidden_hint'), row.hidden, a.hide !== false);   // a template is never on a list: nothing to hide
    f.quick_actions = sQuick.cb; f.image_jobs = sPics.cb; if (sHide) f.hidden = sHide.cb;
    const abilities = [sQuick.row, sPics.row];
    if (!isAgentish(row)) {
      const sLook = sw('lookups', t('cpage.switch_lookups'), t('bots_edit.lookups_title'), b.lookups, a.lookups !== false);
      f.tools = sLook.cb; abilities.push(sLook.row);
    }
    if (a.reactions) {
      const sReact = sw('reactions', t('cpage.switch_reactions'), t('cpage.switch_reactions_hint'), b.reactions);
      f.reactions = sReact.cb; abilities.push(sReact.row);
    }
    if (a.avatar_pool) {
      const sPool = sw('avatar_pool', t('cpage.switch_pool'), t('cpage.switch_pool_hint'), b.avatar_pool);
      f.avatar_pool = sPool.cb; abilities.push(sPool.row);
    }
    if (sHide) abilities.push(sHide.row);
    // An add-on's per-character control (the voice add-on's voice picker, `ext.bots.rowExtra`).
    // It saves at once, like the add-on expects; only its own fields reach the server.
    if (a.voice_extras && !ro && typeof d.botRowExtrasFor === 'function') {
      const str = (v) => (typeof v === 'string' ? v : undefined);
      const view = { ...row, id: row.id, name: row.name,
        voice: str(row.voice) ?? str(orig.voice) ?? '', voice_rp: str(row.voice_rp) ?? str(orig.voice_rp) ?? '' };
      let nodes = [];
      try { nodes = d.botRowExtrasFor(view, (patch) => saveExtras(row, patch)) || []; } catch { nodes = []; }
      if (nodes.length) {
        abilities.push(el('div', { class: 'dev-row chr-feature cp-ext', dataset: { feature: 'extras' } }, [
          el('span', { class: 'dev-row-text' }, [el('strong', { text: t('cpage.voice_extras') }),
            el('span', { class: 'dev-row-hint', text: t('cpage.voice_extras_hint') })]),
          el('span', { class: 'cp-ext-slot' }, nodes)]));
      }
    }
    if (a.voice) abilities.push(el('div', { class: 'cxn-actions' }, [el('button', { type: 'button', class: 'btn-secondary chr-voice', text: t('chars.voice_link'), onclick: () => goVoice(row) })]));

    const group = (id, title, kids) => el('fieldset', { class: 'cp-group', dataset: { group: id } }, [el('legend', { class: 'cp-legend', text: title }), ...kids]);
    const who = [];
    if (isTemplate) {
      who.push(el('p', { class: 'people-field-hint', text: t('cpage.who_hint') }));
      who.push(ro ? el('p', { class: 'muted', text: row.give_to === 'everyone' ? t('cpage.who_everyone') : '' }) : whoChips(row));
      if (!ro) who.push(el('p', { class: 'people-field-hint cp-who-everyone-hint', text: t('cpage.who_everyone_hint') }));
    } else if (row.kind === 'copy') {
      const n = (row.overridden || []).length;
      who.push(el('p', { class: 'muted', text: t(shared() ? 'cpage.who_copy_shared' : 'cpage.who_copy', { name: (row.template && row.template.name) || '' }) }));
      who.push(el('p', { class: 'muted', text: n ? t('cpage.who_copy_custom', { fields: row.overridden.join(', ') }) : t('cpage.who_copy_clean') }));
    } else {
      who.push(el('p', { class: 'muted', text: t(isAgentish(row) ? 'cpage.who_agent' : shared() ? 'cpage.who_own_shared' : 'cpage.who_own') }));
    }

    const kids = [group('identity', t('cpage.group_identity'), identity), group('brain', t('cpage.group_answers'), brain),
      group('abilities', t('cpage.group_abilities'), abilities), group('who', t('cpage.group_who'), who)];

    if (isTemplate && (row.copies || []).length) {
      const rows = row.copies.map((c) => {
        const p = profiles().find((x) => x.id === c.profile_id) || { name: c.profile_id };
        return el('li', { class: 'cp-copy-row' }, [el('strong', { text: p.name }), el('span', { class: 'muted', text: ` ${t(COPY_STATE[c.state] || COPY_STATE.none)}${c.chats ? ` - ${t('cpage.copy_chats', { count: c.chats })}` : ''}` })]);
      });
      kids.push(el('div', { class: 'cp-copies' }, [el('h4', { class: 'chr-drawer-title', text: t('cpage.copies_title') }), el('ul', { class: 'cp-copy-list' }, rows)]));
    }

    const err = el('p', { class: 'lock-error', role: 'alert', hidden: '' });
    if (!ro) {
      const save = el('button', { type: 'button', class: 'btn-primary', text: t('chars.save') });
      save.addEventListener('click', async () => {
        const form = {};
        for (const k of Object.keys(f)) form[k] = (f[k].type === 'checkbox') ? f[k].checked : f[k].value;
        const patch = detailPatch(row, orig, form);
        if (!Object.keys(patch).length) { say(t('chars.nothing')); return; }
        save.disabled = true;
        try {
          await call('PATCH', `/api/characters/${enc(row.id)}`, { json: patch });
          say(t('chars.saved')); panels.delete(key); await load();
        } catch (e) { err.textContent = errorText(e); err.hidden = false; } finally { save.disabled = false; }
      });
      const actions = [save];
      if (row.kind !== 'template' && !isAgentish(row) && can('create_characters', d)) {
        actions.push(el('button', { type: 'button', class: 'btn-secondary', text: t('cpage.full_editor'),
          onclick: () => openStage(async (stageBox) => renderBotEditor(stageBox, { ...row, ...orig }, { ...d, householdAvatars: household4Editor() })) }));
      }
      kids.push(err, el('div', { class: 'cxn-actions' }, actions));
    } else {
      kids.push(el('p', { class: 'muted', text: t('cpage.readonly') }));
    }
    if (isTemplate && !ro) {
      const kitHost = el('div', { class: 'chr-kit-host' });
      kids.push(kitHost);
      kitSection({ id: row.id, name: row.name }, { toast: say, ask, chatsOnly: true }).then((k) => kitHost.replaceChildren(k)).catch(() => {});
    }
    box.replaceChildren(...kids);
    return box;
  }

  // -------------------------------------------------------------- a row
  /** "Get my copy": the household gave you this, you removed your copy, one button brings it back. */
  function getCopyNode(row) {
    const li = el('li', { class: 'cp-row cp-row--cta', dataset: { bot: row.id, kind: 'get-copy' } });
    li.append(el('div', { class: 'cp-head' }, [
      pictureNode(row, 'cp-avatar'),
      el('div', { class: 'cp-text' }, [
        el('strong', { class: 'cp-name', text: row.name }),
        el('span', { class: 'cp-sub muted', text: t('cpage.get_copy_text', { name: row.name }) })]),
      el('div', { class: 'cp-actions' }, [
        iconBtn({ icon: 'copy', label: t('cpage.menu_get_copy'), text: t('cpage.menu_get_copy'), id: 'make_copy', cls: 'cp-btn--primary cp-cta-btn', onclick: () => doMakeCopy(row) })]),
    ]));
    return li;
  }

  function rowNode(row) {
    if (isGetCopyRow(row, tab === 'mine')) return getCopyNode(row);
    const key = `${tab}:${row.id}`;
    const pill = backendPill(row);
    const isOpen = open.has(key);
    const badges = rowBadges(row);
    const quick = rowQuick(row);
    const rk = reorderOn() && (row.actions || {}).reorder ? reorderIds(rowsOf(tab)) : [];
    const k = rk.indexOf(row.id);
    const parked = parkedState(row);
    const menuItems = rowMenu(row, { up: k > 0, down: k >= 0 && k < rk.length - 1, parked,
      getCopy: tab === 'household' && needsOwnCopy(row, data.me) });

    const title = el('div', { class: 'cp-title' }, [
      el('strong', { class: 'cp-name', text: row.name }),
      el('span', { class: `ui-badge cp-pill ${pill.cls}`.trim(), title: t(pill.titleKey), dataset: { pill: row.backend_kind } }, [spriteIcon(pill.icon, 14), el('span', { text: t(pill.key) })]),
      row.kind === 'template' ? el('span', { class: 'ui-badge cp-kind', text: row.give_to === 'everyone' ? t('cpage.given_everyone') : t('cpage.given_count', { count: (row.give_to || []).length }) }) : null,
      row.hidden && !isTemplateRow(row) ? el('span', { class: 'ui-badge cp-hidden-tag', text: t('cpage.hidden_tag') }) : null,
      parked ? el('span', { class: 'ui-badge cp-parked-tag', title: t('cpage.parked_tip') }, [spriteIcon('wrench', 14), el('span', { text: t('cpage.parked_tag') })]) : null,
    ]);
    const icons = el('div', { class: 'cp-icons', role: 'list', 'aria-label': t('cpage.status_aria', { name: row.name }) },
      badges.map((x) => {
        const label = t(x.key, x.vars);
        const s = el('span', { class: 'cp-ico', role: 'listitem', title: label, 'aria-label': label, dataset: { badge: x.id } }, [spriteIcon(x.icon, 15)]);
        return s;
      }));
    const sub = row.description || row.model || '';
    const quickBtns = quick.map((q) => iconBtn({ icon: q.icon, label: t(q.tipKey || q.key), text: t(q.key), pressed: q.pressed, id: q.id,
      onclick: () => (q.nav ? goVoice(row) : patchRow(row, q.patch)) }));
    // A template given to you whose copy you removed: a visible way back (the menu has it too).
    if (((row.actions || {}).make_copy)) {
      quickBtns.unshift(iconBtn({ icon: 'copy', label: t('cpage.menu_get_copy'), text: t('cpage.menu_get_copy'), id: 'make_copy', cls: 'cp-btn--primary', onclick: () => doMakeCopy(row) }));
    }
    const detailsBtn = el('button', { type: 'button', class: 'cp-btn cp-details-btn', 'aria-expanded': String(isOpen), 'aria-label': t('chars.details_open', { name: row.name }), title: t('chars.details') }, [
      spriteIcon(isOpen ? 'chevron-up' : 'chevron-down', 16), el('span', { class: 'cp-btn-text', text: t('chars.details') })]);
    const menuBtn = el('button', { type: 'button', class: 'cp-btn cp-menu-btn', 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-label': t('cpage.menu_aria', { name: row.name }), title: t('cpage.menu_aria', { name: row.name }) }, [spriteIcon('menu', 16)]);
    menuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      popMenu(menuBtn, menuItems.map((m) => ({ id: m.id, icon: m.icon, label: t(m.key), danger: m.danger, run: () => (MENU_RUN[m.id] || (() => {}))(row) })));
    });

    const li = el('li', { class: `cp-row${row.hidden && !isTemplateRow(row) ? ' cp-row--hidden' : ''}`, dataset: { bot: row.id, kind: row.kind } });
    const details = el('div', { class: 'cp-details', hidden: isOpen ? undefined : 'hidden' });
    const toggle = async () => {
      const now = !open.has(key);
      if (now) open.add(key); else open.delete(key);
      detailsBtn.setAttribute('aria-expanded', String(now));
      details.hidden = !now;
      detailsBtn.replaceChildren(spriteIcon(now ? 'chevron-up' : 'chevron-down', 16), el('span', { class: 'cp-btn-text', text: t('chars.details') }));
      if (now && !details.firstChild) details.replaceChildren(await detailsNode(row));
    };
    detailsBtn.addEventListener('click', toggle);
    const handle = k >= 0 && rk.length > 1 ? dragHandle(li, row) : null;
    if (handle) li.dataset.reorder = '1';
    li.append(el('div', { class: 'cp-head' }, [
      handle,
      pictureNode(row, 'cp-avatar'),
      el('div', { class: 'cp-text' }, [title, el('span', { class: 'cp-sub muted', text: sub }), icons]),
      el('div', { class: 'cp-actions' }, [el('div', { class: 'cp-quick', role: 'group', 'aria-label': t('cpage.quick_group') }, quickBtns), hasDetails(row, tab === 'mine') ? detailsBtn : null, menuBtn]),
    ]), details);
    if (isOpen && hasDetails(row, tab === 'mine')) detailsNode(row).then((n) => details.replaceChildren(n));
    return li;
  }

  // -------------------------------------------------------------- drag to reorder
  /** The grip: pointer events, so a mouse, a pen and a finger all drag (the row follows the
   *  pointer's height; the drop place is shown by a line). Keyboard and screen-reader users have
   *  Move up / Move down in the ☰ menu, so the grip itself is hidden from them. */
  function dragHandle(li, row) {
    const handle = el('span', { class: 'cp-handle', 'aria-hidden': 'true', title: t('cpage.drag_tip') }, [spriteIcon('grip-vertical', 18)]);
    handle.addEventListener('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      e.preventDefault();
      try { handle.setPointerCapture(e.pointerId); } catch { /* jsdom / old browsers */ }
      li.classList.add('cp-row--dragging');
      let to = null;
      const others = () => [...list.querySelectorAll('.cp-row[data-reorder="1"]')].filter((x) => x !== li);
      const clear = () => others().forEach((x) => x.classList.remove('cp-drop-before', 'cp-drop-after'));
      const move = (ev) => {
        const rest = others();
        let n = 0;
        for (const x of rest) { const r = x.getBoundingClientRect(); if (ev.clientY > r.top + r.height / 2) n += 1; }
        to = n;
        clear();
        if (!rest.length) return;
        if (n < rest.length) rest[n].classList.add('cp-drop-before'); else rest[rest.length - 1].classList.add('cp-drop-after');
      };
      const end = (commit) => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', onUp);
        handle.removeEventListener('pointercancel', onCancel);
        try { handle.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
        li.classList.remove('cp-row--dragging');
        clear();
        if (commit && to !== null) doMove(row, to);
      };
      const onUp = () => end(true);
      const onCancel = () => end(false);
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', onUp);
      handle.addEventListener('pointercancel', onCancel);
    });
    return handle;
  }

  load();
  const destroy = () => { mounted = null; clearTimeout(timer); closeMenu(); };
  return { reload, destroy };
}
