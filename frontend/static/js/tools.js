// Tools — the rail group, the generic full-page tool pane, and Settings → Tools.
//
// Contract: docs/design/2026-09-25-tools-plugins.md. A tool is an entry in the
// data dir's tools.yaml (never in git); the server lists them at /api/tools,
// serves a static tool's pages at /tools/<id>/ and runs its optional refresh.
//
// Four kinds, one rail:
//   builtin — the four panes main.js already owns (Harness, StudioForge,
//             Emails, Clients). This module only draws their tile and calls
//             the opener main.js hands in; their views are untouched.
//   app     — a trusted package under apps/<id>/ (docs/design/2026-09-25-
//             apps.md): its page is framed from /apps/<id>/ with NO sandbox —
//             it is repo code on our own origin and calls its own API with the
//             session cookie. It talks to the shell only through postMessage
//             (js/app-sdk.js); the listener below accepts nothing that is not
//             from our origin AND from the frame on screen.
//   static  — a directory on the host, framed from /tools/<id>/ with NO
//             allow-same-origin: the page may run its own JS but cannot touch
//             DisPatch's cookies or DOM (the server's CSP says the same).
//   url     — someone else's page, framed as-is. Loopback addresses only load
//             when DisPatch itself was opened on the host (the Harness rule).
//
// How a tool opens (`open`, from the server's row): 'frame' — the pane — or
// 'window', its own browser tab. Some pages refuse every embed (StudioForge's
// panel sends X-Frame-Options: DENY + frame-ancestors 'none', so framed it can
// only ever be an empty rectangle); for those a click LAUNCHES the page
// instead of opening a blank pane, and the StudioForge builtin defaults to it.
// A launch needs a user gesture (a popup blocker is right to refuse anything
// else), so a hash-restore of such a tool opens the pane with a card and one
// button. A framed tool that turns out to be unembeddable, unreachable, empty
// or stuck gets the same themed card with "Open in a new window" rather than
// a blank pane. `open` only moves WHERE a page opens: every tier gate on WHO
// may open it (Safe Mode, the loopback rule) runs first, unchanged.
//
// Every opener, builtin or generic, puts `tool-full` on <body>: the thread
// column goes away and the pane owns the whole content area. Closing takes the
// class off and hands the previous selection back to main.js.
//
// Pure-ish module: no side effects at import time (it is in sw.js SHELL and
// imported by main.js). main.js injects everything app-shaped via wireTools().

import { api } from './api.js?v=32';
import { t, relTimeLong } from './i18n.js?v=3';
import { el, railIcon, RAIL_ICONS } from './util.js?v=20';

// Builtin ids are the pseudo-bot ids main.js has always used, in rail order.
// `icon` names a RAIL_ICONS entry: a builtin tile is chrome, so it is drawn in
// the same currentColor line art as the gear row, never a platform emoji.
const BUILTINS = [
  { id: 'deepseek-harness', feature: 'harness', icon: 'terminal', name: 'harness.name', aria: 'harness.sidebar_aria' },
  { id: 'studioforge-panel', feature: 'studioforge', icon: 'tools', name: 'studioforge.name', aria: 'studioforge.sidebar_aria' },
  { id: 'mail-panel', feature: 'mail', icon: 'mail', name: 'mail.name', aria: 'mail.sidebar_aria' },
  { id: 'clients-panel', feature: 'practice', icon: 'users', name: 'clients.name', aria: 'clients.sidebar_aria' },
];
const BUILTIN_BY_ID = new Map(BUILTINS.map((b) => [b.id, b]));
const BUILTIN_BY_FEATURE = new Map(BUILTINS.map((b) => [b.feature, b]));

// A tools.yaml `icon` is what a person typed: usually one emoji. The handful
// below are the emoji people reach for when they mean a piece of CHROME (a
// report, a console, a mailbox), and the rail draws those in the theme's line
// art so a manifest tool sits beside the builtins and the gear row as one
// family. A RAIL_ICONS name (`icon: chart`) works too. Anything else is the
// person's own choice and is shown as typed — content, not chrome (see the
// note on RAIL_ICONS in util.js) — centred in the same themed tile.
const ICON_ALIASES = {
  '📊': 'chart', '📈': 'chart', '📉': 'chart',
  '🎛': 'tools', '🎚': 'tools',
  '💻': 'terminal', '🖥': 'terminal', '⌨': 'terminal',
  '✉': 'mail', '📧': 'mail', '📨': 'mail', '📬': 'mail',
  '👥': 'users', '👤': 'user',
  '💼': 'jobs', '📋': 'jobs',
  '🔗': 'link', '📁': 'folder', '📂': 'folder', '⚙': 'gear',
  '🔍': 'search', '🔎': 'search', '🗄': 'database', '💾': 'disk',
};
// Word names people (and app manifests) use for an icon the set already has
// under another name. `icon: clipboard` is the contract's own example.
const NAME_ALIASES = { clipboard: 'jobs', briefcase: 'jobs', report: 'chart', console: 'terminal' };
const DEFAULT_GLYPH = '🧩';

/** The RAIL_ICONS name a tool is drawn with, or null for a typed glyph. */
export function toolIconName(tool) {
  const b = tool && tool.kind === 'builtin' ? builtinOf(tool) : null;
  if (b) return b.icon;
  const raw = String((tool && tool.icon) || '').trim();
  if (!raw) return null;
  if (Object.prototype.hasOwnProperty.call(RAIL_ICONS, raw) && /^[a-z-]+$/.test(raw)) return raw;
  if (Object.prototype.hasOwnProperty.call(NAME_ALIASES, raw)) return NAME_ALIASES[raw];
  // U+FE0F (emoji presentation) is optional in YAML: 🎛 and 🎛️ are one icon.
  return ICON_ALIASES[raw.replace(/\uFE0F/g, '')] || null;
}

/** The glyph node for a tool: a line icon, or the typed emoji as text. */
export function toolGlyph(tool) {
  const name = toolIconName(tool);
  if (name && RAIL_ICONS[name]) return railIcon(RAIL_ICONS[name]);
  return el('span', { class: 'tool-glyph-text', text: (tool && String(tool.icon || '').trim()) || DEFAULT_GLYPH });
}

/** The rounded tile a tool's glyph sits in — the bot tile's geometry, the
 *  theme's surface and border. `extra` adds size variants (Settings rows). */
export function toolTile(tool, extra = '') {
  const icon = !!toolIconName(tool);
  return el('span', {
    class: 'bot-avatar tool-avatar ' + (icon ? 'tool-avatar-icon' : 'tool-avatar-glyph') + (extra ? ' ' + extra : ''),
    'aria-hidden': 'true',
  }, [toolGlyph(tool)]);
}

// Literal keys (not t(`tools.kind_${k}`)) so the i18n key scan can see them.
const KIND_LABEL = { builtin: 'tools.kind_builtin', app: 'tools.kind_app', static: 'tools.kind_static', url: 'tools.kind_url' };

// Same shape the server enforces. Checked here only to give a faster, inline
// answer — the server's 422 is the real gate.
const ID_RE = /^[a-z0-9-]{1,40}$/;

// The sandbox a static page gets. url tools add allow-same-origin: they are a
// different origin anyway, and most real web apps refuse to run without their
// own storage.
const SANDBOX_STATIC = 'allow-scripts allow-forms allow-popups';
const SANDBOX_URL = 'allow-scripts allow-forms allow-popups allow-same-origin';

// Fields the manifest schema accepts. Everything else in a ToolOut
// (has_refresh, builtin_feature) is output-only and must not be sent back —
// the schema is closed and would 422 it.
const WRITABLE = ['id', 'title', 'icon', 'kind', 'root', 'entry', 'url', 'remote_url', 'enabled', 'safe', 'refresh', 'open'];

// A framed page that has not fired `load` by now gets the "not finished
// loading" card (the frame keeps loading underneath and is shown if it lands).
export const FRAME_TIMEOUT_MS = 20000;

let deps = null;
let tools = null;        // null = never loaded / API absent; [] = loaded, none
let toolsPath = '';
let openId = null;       // the generic tool on screen, or null
let prev = null;         // {botId, threadId} to hand back on close
let refreshing = false;
let statusSeq = 0;
let frameTimer = null;   // the FRAME_TIMEOUT_MS watchdog for the open frame
let frameLoaded = false; // the open frame fired `load` for its current src
let cardShown = null;    // why the card is up ('slow', 'refuses', …) or null

// Settings draft
let draft = null;        // working copy while Settings → Tools is open
let draftDirty = false;
let addOpen = false;
let settingsHost = null;

const $ = (id) => document.getElementById(id);

/** A builtin tool's metadata, by tool id. */
function builtinOf(tool) {
  if (!tool) return null;
  if (tool.builtin_feature && BUILTIN_BY_FEATURE.has(tool.builtin_feature)) {
    return BUILTIN_BY_FEATURE.get(tool.builtin_feature);
  }
  return BUILTIN_BY_ID.get(tool.id) || null;
}

function isLoopbackHost(h) {
  h = String(h || '').toLowerCase();
  return h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '[::1]'
    || h.startsWith('127.');
}
/** Is THIS browser on the host (so a loopback URL means the same machine)? */
export function onHost() { return isLoopbackHost(location.hostname); }

/** The address a url tool is framed from in THIS browser: its `url` on the
 *  host, its optional `remote_url` anywhere else (a phone on the tailnet),
 *  falling back to `url` — whose loopback rule in loadFrame still applies. */
export function toolUrl(tool) {
  if (!tool) return '';
  if (!onHost() && tool.remote_url) return tool.remote_url;
  return tool.url || '';
}

function parseHttpUrl(u) {
  try {
    const url = new URL(String(u || ''));
    return (url.protocol === 'http:' || url.protocol === 'https:') ? url : null;
  } catch { return null; }
}

/** 'window' when this tool launches in its own tab, else 'frame'. Only kinds
 *  that are a page at an address can: an app never does (its frame is our
 *  own code behind the message bridge), a native builtin never does. */
export function openMode(tool) {
  if (!tool || tool.open !== 'window') return 'frame';
  if (tool.kind === 'static' || tool.kind === 'url') return 'window';
  if (tool.kind === 'builtin' && Object.prototype.hasOwnProperty.call(tool, 'open_url')) return 'window';
  return 'frame';
}

/** The address this tool opens at in a tab from THIS browser, or '' when it
 *  cannot be opened here (a loopback address seen from another device — the
 *  same rule the pane applies — or no address at all). */
export function windowUrl(tool) {
  if (!tool) return '';
  let raw = '';
  if (tool.kind === 'static') raw = new URL(`/tools/${encodeURIComponent(tool.id)}/`, location.href).href;
  else if (tool.kind === 'url') raw = toolUrl(tool);
  else if (tool.kind === 'builtin') raw = (!onHost() && tool.open_remote_url) || tool.open_url || '';
  const u = parseHttpUrl(raw);
  if (!u) return '';
  if (tool.kind !== 'static' && isLoopbackHost(u.hostname) && !onHost()) return '';
  return u.href;
}

/** Does this call run inside a user gesture (so a new tab is not a popup a
 *  blocker should refuse)? Browsers without the API: assume yes. */
function hasGesture() {
  const ua = typeof navigator !== 'undefined' ? navigator.userActivation : null;
  return ua ? !!ua.isActive : true;
}

/** Open `tool` in its own tab. noopener: the page never gets a handle on
 *  DisPatch's window (so it cannot navigate it). False when there is no
 *  usable address from this browser. */
export function launchWindow(tool) {
  const url = windowUrl(tool);
  if (!url) return false;
  try { window.open(url, '_blank', 'noopener,noreferrer'); } catch { return false; }
  if (deps && typeof deps.toast === 'function') deps.toast(t('tools.opened_window', { name: toolTitle(tool) }));
  return true;
}

function normalize(list) {
  if (!Array.isArray(list)) return [];
  return list.filter((x) => x && typeof x === 'object' && typeof x.id === 'string' && x.id);
}

/** Is `tool` an app package row? */
function isApp(tool) { return !!tool && tool.kind === 'app'; }

/** Every tool the API knows, or — before/without the API — the builtins the
 *  feature probes found, so an older backend loses nothing. */
function allTools() {
  if (tools) return tools;
  return BUILTINS.map((b) => ({ id: b.id, kind: 'builtin', enabled: true, builtin_feature: b.feature }));
}

export function findTool(id) {
  return allTools().find((x) => x.id === id) || null;
}

/** Does `id` name a tool (so selectBot must route it here, not to a bot)? */
export function isToolId(id) {
  if (!id) return false;
  return BUILTIN_BY_ID.has(id) || !!(tools && tools.some((x) => x.id === id));
}

/** A tool's display name. Builtins use their existing locale key. */
export function toolTitle(tool) {
  const b = tool && tool.kind === 'builtin' ? builtinOf(tool) : null;
  if (b) return t(b.name);
  return (tool && (tool.title || tool.id)) || '';
}

/** The enabled app row whose roster bot is `botId` — the app that owns that
 *  bot's threads (and may hook them). Null in Safe Mode for a non-safe app. */
export function appForBot(botId) {
  if (!botId || !tools) return null;
  const st = deps && deps.state;
  for (const x of tools) {
    if (!isApp(x) || x.enabled === false || !x.bot_id) continue;
    if (String(x.bot_id).toLowerCase() !== String(botId).toLowerCase()) continue;
    if (st && st.decoy && !x.safe) return null;
    return x;
  }
  return null;
}

/** Apps in rail order: manifest `order`, then id. */
function byAppOrder(a, b) {
  const oa = Number.isFinite(a.order) ? a.order : 1000;
  const ob = Number.isFinite(b.order) ? b.order : 1000;
  return oa - ob || String(a.id).localeCompare(String(b.id));
}

/** The tiles the rail should draw: builtins, then apps, then static/url. */
export function railEntries() {
  if (!deps) return [];
  const st = deps.state;
  const builtins = [];
  const apps = [];
  const others = [];
  for (const tool of allTools()) {
    if (tool.enabled === false) continue;
    if (tool.kind === 'builtin') {
      // Never in Safe Mode, and only when the pane's own probe found it on.
      const b = builtinOf(tool);
      if (st.decoy || !b || !deps.builtinOn(b.feature)) continue;
      builtins.push(tool);
    } else if (isApp(tool)) {
      if (st.decoy && !tool.safe) continue;
      apps.push(tool);
    } else if (tool.kind === 'static' || tool.kind === 'url') {
      // The API already filters for Safe Mode; this is the belt to its braces.
      if (st.decoy && !tool.safe) continue;
      others.push(tool);
    }
  }
  const order = (x) => BUILTINS.indexOf(builtinOf(x));
  builtins.sort((a, b) => order(a) - order(b));
  apps.sort(byAppOrder);
  return builtins.concat(apps, others);
}

// ===================== Loading =====================

/** Fetch /api/tools and repaint the rail. Never throws. */
export async function loadTools() {
  try {
    const r = await api.tools();
    tools = normalize(r && r.tools);
    toolsPath = (r && typeof r.path === 'string') ? r.path : '';
  } catch (e) {
    // 404: a backend without the tools feature — keep the builtin fallback.
    // 401/403: this tier may see none.
    if (e && e.status === 404) tools = null;
    else if (e && (e.status === 401 || e.status === 403)) tools = [];
    // Anything else (network): keep whatever we had.
  }
  renderToolRail();
  return tools;
}

// ===================== Rail =====================
//
// Two layouts, one control (2026-09-26):
//   desktop         — the rail is a 72px column shared with the roster, and a
//                     56px tile per tool pushed the bots off it. The group is
//                     ONE rail button ("Tools", nine-dot line icon, worst-of
//                     status dot) that opens a popup menu: one row per enabled
//                     tool, then the bots parked from Settings → Bots under a
//                     separator.
//   phone Bots page — the rail IS the page, and DisPatch is a chat app: the
//                     roster is the page and the tools are add-ons. So the
//                     group is ONE quiet full-width row under the roster (the
//                     same button, drawn as a list row), and the same menu
//                     opens as a bottom sheet over a scrim. A tile grid of
//                     tools used to sit there at the bots' own 84px size.
// With no tools but some parked bots, the one control is "More bots".

// The nine-dot "apps launcher" glyph, drawn like every RAIL_ICONS entry
// (24-unit box, currentColor stroke). Nine dots, not four squares: the theme
// button right below it in the gear row is a four-square swatch, and two
// look-alike glyphs stacked in one rail read as one control. Local to this
// module on purpose: it is the one place that draws it, and util.js is
// imported by a dozen modules whose cache-busting versions would all have to
// move for one icon.
const APPS_ICON = [
  'M6 4.6a1.4 1.4 0 1 1 0 2.8a1.4 1.4 0 1 1 0-2.8Z', 'M12 4.6a1.4 1.4 0 1 1 0 2.8a1.4 1.4 0 1 1 0-2.8Z', 'M18 4.6a1.4 1.4 0 1 1 0 2.8a1.4 1.4 0 1 1 0-2.8Z',
  'M6 10.6a1.4 1.4 0 1 1 0 2.8a1.4 1.4 0 1 1 0-2.8Z', 'M12 10.6a1.4 1.4 0 1 1 0 2.8a1.4 1.4 0 1 1 0-2.8Z', 'M18 10.6a1.4 1.4 0 1 1 0 2.8a1.4 1.4 0 1 1 0-2.8Z',
  'M6 16.6a1.4 1.4 0 1 1 0 2.8a1.4 1.4 0 1 1 0-2.8Z', 'M12 16.6a1.4 1.4 0 1 1 0 2.8a1.4 1.4 0 1 1 0-2.8Z', 'M18 16.6a1.4 1.4 0 1 1 0 2.8a1.4 1.4 0 1 1 0-2.8Z',
];

// Bots parked in the menu (menubots.js decides WHICH; main.js hands the rows
// over on every sidebar repaint via setParkedBots): [{id, name, avatar(),
// dot, active, open()}]. Ignored in Safe Mode, like the placement itself.
let parked = [];
let menuOpen = false;
let menuMode = 'tools';     // 'tools' (tools + parked) | 'bots' (parked only — no tools to list)
let lastCompact = null;

/** main.js: the parked-bot rows for the popup. */
export function setParkedBots(list) {
  parked = Array.isArray(list) ? list.filter((b) => b && typeof b.id === 'string') : [];
}

/** Desktop layout? (the one-button rail). */
function compactRail() { return !!deps && !deps.isMobile(); }

function visibleParked() {
  return deps && deps.state && deps.state.decoy ? [] : parked;
}

/** The dot class for the one rail button: the builtins' worst-of state when
 *  there is one, else the parked bots' (thinking beats unread). */
function groupDotClass(mode, bots) {
  if (mode === 'tools') {
    const g = typeof deps.groupState === 'function' ? deps.groupState() : '';
    if (g) return 'bot-status-dot terminal-sidedot tools-sidedot ' + g;
  }
  const d = bots.some((b) => b.dot === 'thinking') ? 'thinking'
    : (bots.find((b) => b.dot) || {}).dot || '';
  return 'bot-status-dot' + (d ? ' ' + d : '');
}

/** The rail button that opens the popup. `mode` 'tools' on the desktop,
 *  'bots' for the phone's parked-bots tile. */
function menuButton(entries, bots, mode) {
  const st = deps.state;
  const active = (mode === 'tools' && (entries.some((x) => x.id === st.selectedBotId) || !!openId))
    || bots.some((b) => b.active);
  const label = mode === 'tools' ? t('nav.tools') : t('tools.more_bots');
  const btn = el('button', {
    class: 'bot-btn tool-btn tools-menu-btn' + (active ? ' active' : ''),
    id: 'tools-menu-btn',
    type: 'button',
    dataset: { menu: mode },
    'aria-label': mode === 'tools' ? t('tools.menu_aria') : t('tools.more_bots_aria'),
    'aria-haspopup': 'menu',
    'aria-controls': 'tools-menu',
    'aria-expanded': menuOpen ? 'true' : 'false',
    draggable: 'false',
  });
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    // e.detail 0 = keyboard (Enter/Space): land on the first row, as a menu
    // button should. A pointer click leaves focus on the button.
    toggleToolsMenu(mode, { focus: e.detail === 0 ? 'first' : null });
  });
  btn.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      openToolsMenu(mode, { focus: e.key === 'ArrowDown' ? 'first' : 'last' });
    }
  });
  btn.append(el('span', { class: 'bot-avatar tool-avatar tool-avatar-icon', 'aria-hidden': 'true' }, [railIcon(APPS_ICON)]));
  btn.append(el('span', { class: 'bot-name-tip', text: label }));
  btn.append(el('span', { class: 'bot-name-label', text: label }));
  btn.append(el('span', { class: groupDotClass(mode, bots) }));
  // Phone row only (CSS hides it on the rail): the "opens more" affordance.
  btn.append(el('span', { class: 'tools-menu-chevron', 'aria-hidden': 'true', text: '›' }));
  return btn;
}

/** Draw the Tools group into #tool-list. Cheap; main.js calls it on every
 *  sidebar repaint so active state and feature flags never go stale. */
export function renderToolRail() {
  const host = $('tool-list');
  if (!host || !deps) return;
  const entries = railEntries();
  const bots = visibleParked();
  const compact = compactRail();
  lastCompact = compact;
  const ae = document.activeElement;
  const focused = ae && host.contains(ae)
    ? (ae.id === 'tools-menu-btn' ? '#menu' : ae.closest('[data-tool]')?.dataset.tool) : null;
  host.textContent = '';
  host.classList.toggle('tool-list-compact', compact);
  host.classList.toggle('tool-list-row', !compact);
  const mode = entries.length ? 'tools' : 'bots';
  const empty = !entries.length && !bots.length;
  host.hidden = empty;
  if (empty) { closeToolsMenu(); return; }
  host.append(menuButton(entries, bots, mode));
  // A menu opened in the other layout (or for rows that are gone) closes;
  // an open one is repainted in place so its dots and active row stay true.
  if (menuOpen) {
    if (menuMode !== mode || !$('tools-menu-btn')) closeToolsMenu();
    else paintMenu();
  }
  if (focused === '#menu') {
    const b = $('tools-menu-btn');
    if (b) b.focus({ preventScroll: true });
  } else if (focused) {
    const again = host.querySelector(`[data-tool="${CSS.escape ? CSS.escape(focused) : focused}"]`);
    if (again) again.focus({ preventScroll: true });
  }
}

/** main.js: repaint just the rail button's worst-of dot (a service changed). */
export function paintToolsGroupDot() {
  const btn = $('tools-menu-btn');
  if (!btn || !deps) return;
  const dot = btn.querySelector('.bot-status-dot');
  if (dot) dot.className = groupDotClass(btn.dataset.menu, visibleParked());
}

/** The dot for a builtin, for main.js to patch in place: its phone tile, or
 *  its row in the open popup. */
export function railToolDot(id) {
  for (const hostId of ['tool-list', 'tools-menu-tools']) {
    const host = $(hostId);
    if (!host) continue;
    for (const b of host.querySelectorAll('[data-tool]')) {
      if (b.dataset.tool === id) return b.querySelector('.bot-status-dot');
    }
  }
  return null;
}

// ----- The popup -----

function menuRow(tool) {
  const st = deps.state;
  const b = tool.kind === 'builtin' ? builtinOf(tool) : null;
  const title = toolTitle(tool);
  const active = st.selectedBotId === tool.id;
  const row = el('button', {
    class: 'tools-item' + (active ? ' active' : ''),
    type: 'button',
    role: 'menuitem',
    tabindex: '-1',
    dataset: { tool: tool.id },
    'aria-label': (b ? t(b.aria) : title) + (openMode(tool) === 'window' ? ' — ' + t('tools.opens_window') : ''),
  });
  if (active) row.setAttribute('aria-current', 'page');
  row.addEventListener('click', () => { closeToolsMenu(); openTool(tool.id); });
  row.append(toolTile(tool, 'tool-avatar-row'));
  row.append(el('span', { class: 'tools-item-label', text: title }));
  // The "leaves DisPatch" affordance, the same ↗ the pane header uses.
  if (openMode(tool) === 'window') row.append(el('span', { class: 'muted tools-item-ext', 'aria-hidden': 'true', text: '↗' }));
  if (b) {
    const dot = deps.builtinDot(b.feature);
    if (dot) row.append(el('span', { class: 'bot-status-dot terminal-sidedot ' + dot }));
  }
  return row;
}

function botRow(bot) {
  const row = el('button', {
    class: 'tools-item' + (bot.active ? ' active' : ''),
    type: 'button',
    role: 'menuitem',
    tabindex: '-1',
    dataset: { bot: bot.id },
  });
  row.addEventListener('click', () => { closeToolsMenu(); if (typeof bot.open === 'function') bot.open(); });
  const av = typeof bot.avatar === 'function' ? bot.avatar() : null;
  if (av) row.append(av);
  row.append(el('span', { class: 'tools-item-label', text: bot.name || bot.id }));
  row.append(el('span', { class: 'bot-status-dot' + (bot.dot ? ' ' + bot.dot : '') }));
  return row;
}

function menuItems() {
  const menu = $('tools-menu');
  return menu ? [...menu.querySelectorAll('[role="menuitem"]')] : [];
}

/** Fill the popup for the current mode, keeping keyboard focus on the same row. */
function paintMenu() {
  const menu = $('tools-menu');
  const toolsHost = $('tools-menu-tools');
  const botsHost = $('tools-bots');
  if (!menu || !toolsHost || !botsHost) return 0;
  const ae = document.activeElement;
  const keep = ae && menu.contains(ae) ? { tool: ae.dataset.tool, bot: ae.dataset.bot } : null;
  toolsHost.textContent = '';
  botsHost.textContent = '';
  for (const tool of railEntries()) toolsHost.append(menuRow(tool));
  for (const bot of visibleParked()) botsHost.append(botRow(bot));
  const sep = $('tools-bots-sep');
  if (sep) sep.hidden = !(toolsHost.childElementCount && botsHost.childElementCount);
  const label = menuMode === 'tools' ? t('nav.tools') : t('tools.more_bots');
  menu.setAttribute('aria-label', label);
  const title = $('tools-menu-title');
  if (title) title.textContent = label;
  if (keep) {
    const again = menuItems().find((r) => (keep.tool && r.dataset.tool === keep.tool) || (keep.bot && r.dataset.bot === keep.bot));
    if (again) again.focus({ preventScroll: true });
  }
  return toolsHost.childElementCount + botsHost.childElementCount;
}

/** Anchor the fixed popup beside the button, on the rail's inline-end side,
 *  clamped to the viewport (RTL: the rail is on the right, so open leftwards). */
function placeMenu(menu, btn) {
  const sheet = !compactRail();
  menu.classList.toggle('tools-sheet', sheet);
  document.body.classList.toggle('tools-sheet-open', sheet);
  if (sheet) {
    // The phone's bottom sheet is placed by CSS; clear any desktop anchor.
    menu.style.left = '';
    menu.style.top = '';
    return;
  }
  const r = btn.getBoundingClientRect();
  const mw = menu.offsetWidth || 240;
  const mh = menu.offsetHeight || 200;
  const vw = window.innerWidth || 1024;
  const vh = window.innerHeight || 768;
  const rtl = document.documentElement.dir === 'rtl';
  const beside = rtl ? r.left - mw - 8 : r.right + 8;
  const fits = rtl ? beside >= 8 : beside + mw <= vw - 8;
  const left = fits ? beside : Math.min(r.left, vw - mw - 8);
  // CSSOM, not a style="" attribute: the page's CSP blocks the latter.
  menu.style.left = Math.max(8, Math.min(left, vw - mw - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(r.top, vh - mh - 8)) + 'px';
}

function focusItem(which) {
  const items = menuItems();
  if (!items.length) return;
  const i = which === 'last' ? items.length - 1 : 0;
  items[i].focus({ preventScroll: true });
}

export function openToolsMenu(mode = menuMode, { focus = null } = {}) {
  const menu = $('tools-menu');
  const btn = $('tools-menu-btn');
  if (!menu || !btn || !deps) return false;
  menuMode = mode;
  if (!paintMenu()) { closeToolsMenu(); return false; }   // never an empty menu
  menu.hidden = false;
  menuOpen = true;
  btn.setAttribute('aria-expanded', 'true');
  placeMenu(menu, btn);
  // Capture phase, added on open and removed on close: sees the click before
  // any handler on the way down can stop it.
  document.addEventListener('click', onOutside, true);
  // Escape from anywhere (focus may still be on the button after a pointer
  // click), not only from inside the menu.
  document.addEventListener('keydown', onEscape, true);
  if (focus) focusItem(focus);
  return true;
}

/** Close the popup. `focusButton` hands keyboard focus back to the rail button. */
export function closeToolsMenu({ focusButton = false } = {}) {
  const wasOpen = menuOpen;
  menuOpen = false;
  document.removeEventListener('click', onOutside, true);
  document.removeEventListener('keydown', onEscape, true);
  const menu = $('tools-menu');
  if (menu) { menu.hidden = true; menu.classList.remove('tools-sheet'); }
  if (typeof document !== 'undefined') document.body.classList.remove('tools-sheet-open');
  const btn = $('tools-menu-btn');
  if (btn) {
    btn.setAttribute('aria-expanded', 'false');
    if (wasOpen && focusButton) btn.focus({ preventScroll: true });
  }
}

export function toggleToolsMenu(mode = menuMode, opts = {}) {
  if (menuOpen) { closeToolsMenu(); return false; }
  return openToolsMenu(mode, opts);
}

export function toolsMenuOpen() { return menuOpen; }

function onOutside(e) {
  const btn = $('tools-menu-btn');
  const menu = $('tools-menu');
  // position:fixed puts the menu outside the button's box — check both. Their
  // own listeners handle clicks on them.
  if ((btn && btn.contains(e.target)) || (menu && menu.contains(e.target))) return;
  // The phone sheet sits over a scrim: a tap on the scrim only dismisses it.
  // Without this the same tap would land on whatever bot tile is under it.
  if (menu && menu.classList.contains('tools-sheet')) { e.preventDefault(); e.stopPropagation(); }
  closeToolsMenu();
}

function onEscape(e) {
  if (!menuOpen || e.key !== 'Escape') return;
  e.preventDefault();
  e.stopPropagation();     // one Escape closes the popup, not the pane under it too
  closeToolsMenu({ focusButton: true });
}

function onMenuKey(e) {
  if (!menuOpen) return;
  const items = menuItems();
  const i = items.indexOf(document.activeElement);
  switch (e.key) {
    case 'Tab':
      closeToolsMenu({ focusButton: true });
      return;
    case 'ArrowDown':
      e.preventDefault();
      if (items.length) items[(i + 1) % items.length].focus();
      return;
    case 'ArrowUp':
      e.preventDefault();
      if (items.length) items[(i - 1 + items.length) % items.length].focus();
      return;
    case 'Home':
      e.preventDefault(); focusItem('first'); return;
    case 'End':
      e.preventDefault(); focusItem('last'); return;
    default:
  }
}

// ===================== Pane =====================

/** Where an app's page lives. Always under /apps/<id>/ on THIS origin: an
 *  `entry` from the server that points anywhere else is ignored, because the
 *  frame is unsandboxed and must only ever hold our own code. */
function appSrc(tool) {
  const base = `/apps/${encodeURIComponent(tool.id)}/`;
  const e = typeof tool.entry === 'string' ? tool.entry : '';
  return e.startsWith(base) && !e.includes('..') && !e.includes('//', 1) ? e : base;
}

function frameSrc(tool) {
  if (isApp(tool)) return appSrc(tool);
  if (tool.kind === 'static') return `/tools/${encodeURIComponent(tool.id)}/`;
  const u = parseHttpUrl(toolUrl(tool));
  return u ? u.href : '';
}

/** Unload a frame. Removing `src` alone does NOT navigate an iframe — the old
 *  document keeps running (scripts, sockets, timers) in a hidden element.
 *  Navigating to about:blank first is what actually tears it down. */
function unloadFrame(frame) {
  frame.classList.add('hidden');
  frame.src = 'about:blank';
  frame.removeAttribute('src');
}

function clearFrameTimer() {
  if (frameTimer) { clearTimeout(frameTimer); frameTimer = null; }
}

/** The card's action row, created once inside #tool-note (the markup only
 *  carries the two text lines). Existing themed button classes only. */
function noteActions() {
  const note = $('tool-note');
  if (!note) return null;
  let box = $('tool-note-actions');
  if (!box) {
    box = el('div', { class: 'tools-add-actions tool-note-actions', id: 'tool-note-actions' });
    note.append(box);
  }
  return box;
}

/** Show the themed card instead of the frame. `actions`: 'window' (Open in
 *  a new window — only offered when this browser has an address to open) and
 *  'retry'. `keepFrame`: leave the frame loading underneath (the slow case —
 *  if it lands after all, onFrameLoad swaps it back in). */
function showNote(textKey, hintKey, { actions = [], vars = {}, keepFrame = false, why = 'note' } = {}) {
  const note = $('tool-note');
  const frame = $('tool-frame');
  if (frame) {
    if (keepFrame) frame.classList.add('hidden');
    else { clearFrameTimer(); unloadFrame(frame); }
  }
  if (!note) return;
  cardShown = why;
  $('tool-note-text').textContent = t(textKey, vars);
  $('tool-note-hint').textContent = hintKey ? t(hintKey, vars) : '';
  const box = noteActions();
  if (box) {
    box.textContent = '';
    const tool = openId ? findTool(openId) : null;
    if (tool && actions.includes('window') && windowUrl(tool)) {
      box.append(el('button', {
        type: 'button', class: 'btn-primary', id: 'tool-note-window', text: t('tools.open_window'),
        onclick: () => { const cur = openId && findTool(openId); if (cur) launchWindow(cur); },
      }));
    }
    if (tool && actions.includes('retry')) {
      box.append(el('button', {
        type: 'button', class: 'btn-secondary', id: 'tool-note-retry', text: t('tools.retry'),
        onclick: () => { const cur = openId && findTool(openId); if (cur) { loadFrame(cur); refreshStatus(cur); } },
      }));
    }
    box.hidden = !box.childElementCount;
  }
  note.classList.remove('hidden');
}

function hideNote() {
  cardShown = null;
  const note = $('tool-note');
  if (note) note.classList.add('hidden');
}

/** The open frame fired `load`. Cross-origin, that proves little (a
 *  browser's own error page fires it too — the status probe covers what the
 *  headers say); what it does prove is that the page is not stuck. */
function onFrameLoad() {
  frameLoaded = true;
  clearFrameTimer();
  if (cardShown === 'slow') {
    hideNote();
    const frame = $('tool-frame');
    if (frame) frame.classList.remove('hidden');
  }
}

function armFrameTimer(tool) {
  clearFrameTimer();
  frameLoaded = false;
  const id = tool.id;
  frameTimer = setTimeout(() => {
    frameTimer = null;
    if (openId !== id || frameLoaded || cardShown) return;
    showNote('tools.slow_text', 'tools.slow_hint', { actions: ['window', 'retry'], keepFrame: true, why: 'slow' });
  }, FRAME_TIMEOUT_MS);
}

function loadFrame(tool) {
  const frame = $('tool-frame');
  if (!frame) return;
  const src = frameSrc(tool);
  if (!src) { showNote('tools.bad_url_text', 'tools.location_hint'); return; }
  if (tool.kind === 'url' && isLoopbackHost(new URL(src).hostname) && !onHost()) {
    showNote('tools.loopback_text', 'tools.loopback_hint');
    return;
  }
  // A window-mode tool reached without a click (a #tool= restore): the pane
  // says where it lives and offers the one button a popup blocker allows.
  if (openMode(tool) === 'window') {
    showNote('tools.window_text', 'tools.window_hint', { actions: ['window'], vars: { name: toolTitle(tool) }, why: 'window' });
    return;
  }
  hideNote();
  // Sandbox BEFORE src: the attribute is read when the navigation starts.
  // An app is trusted repo code on our own origin and needs the session
  // cookie for its own API, so its frame has NO sandbox attribute at all —
  // and nothing else is ever framed without one.
  if (isApp(tool)) frame.removeAttribute('sandbox');
  else frame.setAttribute('sandbox', tool.kind === 'url' ? SANDBOX_URL : SANDBOX_STATIC);
  frame.setAttribute('title', t('tools.frame_title', { name: toolTitle(tool) }));
  frame.classList.remove('hidden');
  armFrameTimer(tool);
  frame.setAttribute('src', src);
}

function toIso(mtime) {
  if (mtime == null || mtime === '') return null;
  if (typeof mtime === 'number') return new Date(mtime < 1e12 ? mtime * 1000 : mtime).toISOString();
  return String(mtime);
}

function paintRefreshBtn(tool) {
  const btn = $('tool-refresh');
  if (!btn) return;
  const show = !!(tool && tool.has_refresh) && !deps.state.decoy;
  btn.classList.toggle('hidden', !show);
  btn.disabled = refreshing;
  btn.textContent = refreshing ? t('tools.refreshing') : t('tools.refresh');
  btn.setAttribute('aria-busy', refreshing ? 'true' : 'false');
}

function hideError() {
  const box = $('tool-error');
  if (box) { box.classList.add('hidden'); box.open = false; }
}
function showError(summary, detail) {
  const box = $('tool-error');
  if (!box) return;
  $('tool-error-summary').textContent = summary;
  $('tool-error-text').textContent = detail || '';
  box.open = false;             // collapsed: the summary says enough
  box.classList.remove('hidden');
}

/** Ask the server about the open tool: mtime, reachability, refresh state. */
async function refreshStatus(tool) {
  const seq = ++statusSeq;
  const upd = $('tool-updated');
  let st = null;
  try { st = await api.toolStatus(tool.id); } catch { /* status is decoration */ }
  if (seq !== statusSeq || openId !== tool.id) return;
  if (upd) {
    const iso = st ? toIso(st.mtime) : null;
    upd.textContent = iso ? t('tools.updated', { when: relTimeLong(iso) }) : '';
  }
  if (st && st.refreshing && !refreshing) {
    // Another tab (or the server) is mid-run: reflect it, poll until done.
    refreshing = true;
    paintRefreshBtn(tool);
    setTimeout(() => {
      if (openId !== tool.id) return;
      refreshing = false;
      refreshStatus(tool);
    }, 3000);
    return;
  }
  paintRefreshBtn(tool);
  // The card, not an empty frame, whenever the server already knows the
  // frame cannot show the page. A card that is already up (the loopback
  // rule, a window-mode tool) is more specific and stays.
  if (st && !cardShown && openMode(tool) === 'frame') {
    if (tool.kind === 'url' && st.reachable === false) {
      showNote('tools.unreachable_text', 'tools.unreachable_hint', { actions: ['window', 'retry'], why: 'unreachable' });
    } else if (tool.kind === 'url' && st.framable === false) {
      // Its server forbids embedding: no browser will draw it in the frame.
      showNote('tools.refuses_text', 'tools.refuses_hint', { actions: ['window'], why: 'refuses' });
    } else if (tool.kind === 'static' && Object.prototype.hasOwnProperty.call(st, 'mtime') && st.mtime == null) {
      // The entry file does not resolve: the frame would show a bare 404.
      showNote('tools.missing_text', 'tools.missing_hint', { actions: ['retry'], why: 'missing' });
    }
  }
  // An app whose backend failed to import/build: the page may load, its API
  // will not. Say which half is missing.
  if (isApp(tool) && st && st.mounted === false) {
    showError(t('tools.app_unmounted_text'), t('tools.app_unmounted_hint'));
  }
}

async function doRefresh() {
  const id = openId;
  const tool = id && findTool(id);
  if (!tool || refreshing || deps.state.decoy) return;
  refreshing = true;
  hideError();
  paintRefreshBtn(tool);
  try {
    const r = await api.toolRefresh(id);
    if (openId !== id) return;
    const rc = r && typeof r.rc === 'number' ? r.rc : -1;
    if (rc === 0) {
      const frame = $('tool-frame');
      if (frame && frame.getAttribute('src')) { armFrameTimer(tool); frame.setAttribute('src', frameSrc(tool)); }
      deps.toast(t('tools.refresh_ok'));
    } else {
      showError(t('tools.refresh_failed', { rc: String(rc) }),
        (r && (r.stderr_tail || r.stdout_tail)) || '');
    }
  } catch (e) {
    if (openId !== id) return;
    if (e && e.status === 409) deps.toast(t('tools.refresh_busy'), true);
    else showError(t('tools.refresh_error', { error: (e && e.message) || '' }), '');
  } finally {
    refreshing = false;
    if (openId === id) { paintRefreshBtn(tool); refreshStatus(tool); }
  }
}

function setHash(id) {
  try {
    const url = location.pathname + location.search + (id ? '#tool=' + encodeURIComponent(id) : '');
    history.replaceState(history.state, '', url);
  } catch { /* not fatal */ }
}

/** Remember the chat a tool is replacing, so ✕ can hand it back. Called on
 *  entry into ANY tool — generic here, builtin from main.js's openers — and a
 *  no-op when hopping tool to tool (builtin or generic), or the "previous
 *  thread" would become the tool we just left and ✕ would land on nothing. */
export function rememberPrev() {
  const st = deps && deps.state;
  if (!st || isToolId(st.selectedBotId)) return;
  prev = { botId: st.selectedBotId, threadId: st.activeThreadId };
}

/** Open a tool by id. Builtins go to their own opener; static/url tools get
 *  the generic pane. Either way the page ends up in `tool-full` — except a
 *  window-mode tool opened by a click, which launches in its own tab and
 *  leaves the current view (and selection) exactly as it was. */
export function openTool(id, { launch = true } = {}) {
  if (!deps || !id) return false;
  // `launch: false` (a #tool= restore) never opens a tab by itself: Chromium
  // counts a typed or reloaded URL as user activation, so the gesture check
  // alone would pop the page again on every reload.
  launch = launch && hasGesture();
  const st = deps.state;
  const tool = findTool(id);
  const b = BUILTIN_BY_ID.get(id) || (tool && tool.kind === 'builtin' ? builtinOf(tool) : null);
  if (b) {
    if (st.decoy) return false;
    if (tool && tool.enabled === false) return false;
    const opener = deps.openers && deps.openers[b.feature];
    if (typeof opener !== 'function') return false;
    // Only once the pane's own probe found the feature on (the rail's rule):
    // the tab is another way into the SAME unlocked-only page, never a way
    // around its gate. No address from here (loopback seen off the host) →
    // the pane, whose notes explain why.
    if (tool && openMode(tool) === 'window' && deps.builtinOn(b.feature)
        && launch && launchWindow(tool)) return true;
    opener();
    return true;
  }
  if (!tool || tool.enabled === false) return false;
  if (tool.kind !== 'static' && tool.kind !== 'url' && !isApp(tool)) return false;
  if (st.decoy && !tool.safe) return false;
  if (openMode(tool) === 'window' && launch && launchWindow(tool)) return true;

  rememberPrev();
  deps.closeToolPanes('tool');
  if (openId && openId !== id) teardown();
  openId = id;
  refreshing = false;
  st.selectedBotId = id;

  const icon = $('tool-icon');
  if (icon) icon.replaceChildren(toolGlyph(tool));
  const title = $('tool-title');
  if (title) title.textContent = toolTitle(tool);
  const upd = $('tool-updated');
  if (upd) upd.textContent = '';
  const open = $('tool-open');
  const src = frameSrc(tool);
  if (open) {
    open.classList.toggle('hidden', !src);
    if (src) open.setAttribute('href', src); else open.removeAttribute('href');
  }
  hideError();
  paintRefreshBtn(tool);
  const view = $('tool-view');
  if (view) view.classList.remove('hidden');
  document.body.classList.add('tool-full');
  deps.renderSidebar();
  if (deps.paintPlaceholder) deps.paintPlaceholder(toolTitle(tool), toolGlyph(tool));
  if (deps.isMobile()) deps.navigate('chat');
  loadFrame(tool);
  refreshStatus(tool);
  setHash(id);
  return true;
}

/** Take the generic pane down without touching the selection. */
function teardown() {
  statusSeq += 1;
  refreshing = false;
  const view = $('tool-view');
  if (view) view.classList.add('hidden');
  // Unload: a hidden frame keeps its scripts and sockets running.
  const frame = $('tool-frame');
  if (frame) unloadFrame(frame);
  clearFrameTimer();
  hideNote();
  hideError();
}

/** Close the generic tool pane. `restore: false` is for a caller that is about
 *  to show something else (another tool, a bot) and owns the selection. */
export function closeTool({ restore = true } = {}) {
  if (!openId) return;
  openId = null;
  teardown();
  document.body.classList.remove('tool-full');
  setHash(null);
  // `restore: false` = the caller is showing something else. If that is
  // another tool (a builtin), keep `prev`: ✕ over there should still land on
  // the last real chat. A real chat overwrites it on the next entry anyway.
  if (!restore) return;
  const p = prev;
  prev = null;
  if (deps) deps.restoreView(p);
}

export function toolOpen() { return openId; }

// ===================== Apps: shell ⇄ frame messaging =====================
// The frame of an app is unsandboxed and same-origin, so it could reach into
// the shell directly; the contract says it does not, and the shell holds up
// its half by acting ONLY on messages that (a) come from our own origin and
// (b) come from the frame that is on screen right now, while it holds an app.
// Anything else — another window, a stale frame, a static tool (opaque
// origin), a url tool (someone else's origin) — is ignored.

const THREAD_ID_RE = /^[A-Za-z0-9_.:-]{1,200}$/;

function openApp() {
  const tool = openId ? findTool(openId) : null;
  return isApp(tool) ? tool : null;
}

/** Post to the open app's frame, if there is one. Same origin only. */
function postToApp(msg) {
  const frame = $('tool-frame');
  if (!openApp() || !frame || !frame.contentWindow) return false;
  try { frame.contentWindow.postMessage(msg, location.origin); return true; } catch { return false; }
}

/** The shell's theme, as its runtime stamped it on <html>. */
function shellTheme() {
  const root = document.documentElement;
  return {
    type: 'dispatch:theme',
    palette: root.getAttribute('data-palette'),
    theme: root.getAttribute('data-theme'),
    contrast: root.getAttribute('data-contrast-profile'),
  };
}

/** Tell the open app the shell's current theme and language. main.js calls
 *  this on a language switch; the frame's 'load' and the theme runtime's
 *  'ui-theme-change' event call it too. */
export function syncAppFrame() {
  if (!openApp()) return;
  postToApp(shellTheme());
  postToApp({ type: 'dispatch:lang', lang: document.documentElement.getAttribute('lang') || 'en' });
}

/** A live `app:<id>:*` WebSocket frame: hand it to that app's page when it is
 *  the one on screen. Returns true when it was posted. */
export function forwardAppFrame(frame) {
  const tool = openApp();
  if (!tool || !frame || typeof frame.type !== 'string') return false;
  if (!frame.type.startsWith(`app:${tool.id}:`)) return false;
  return postToApp({ type: 'dispatch:frame', frame });
}

function onAppMessage(ev) {
  if (!deps) return;
  const frame = $('tool-frame');
  const tool = openApp();
  // Both checks, always: origin says "our code", source says "the frame on
  // screen" (not a popup it opened, not a frame left over from a close).
  if (!tool || !frame || ev.origin !== location.origin || ev.source !== frame.contentWindow) return;
  const msg = ev.data;
  if (!msg || typeof msg !== 'object' || typeof msg.type !== 'string') return;
  switch (msg.type) {
    case 'dispatch:open-thread': {
      const id = typeof msg.threadId === 'string' ? msg.threadId : '';
      if (!THREAD_ID_RE.test(id)) return;
      let hint = null;
      if (msg.hint && typeof msg.hint === 'object') {
        // A hint is data for the app's own thread hook: small, and plain JSON.
        try { const s = JSON.stringify(msg.hint); if (s.length <= 2048) hint = JSON.parse(s); } catch { /* dropped */ }
      }
      // rememberPrev semantics: ✕-style close, the rail goes back to the last
      // real chat, THEN the thread opens over it.
      closeTool();
      if (typeof deps.openThread === 'function') deps.openThread(id, { botId: tool.bot_id || null, hint, appId: tool.id });
      return;
    }
    case 'dispatch:close':
      closeTool();
      return;
    case 'dispatch:toast': {
      const text = String(msg.text == null ? '' : msg.text).slice(0, 500);
      if (text) deps.toast(text, !!msg.error);
      return;
    }
    case 'dispatch:set-title': {
      const el_ = $('tool-title');
      const text = String(msg.text == null ? '' : msg.text).slice(0, 120);
      if (el_) el_.textContent = text || toolTitle(tool);
      return;
    }
    default:
  }
}

// ===================== Tool layout state (static / url / app frames) =====================
// A tool page may keep its viewer's layout (columns, sort, filters…) in the
// shell. A static tool runs sandboxed WITHOUT allow-same-origin, so its own
// localStorage throws; this bridge is the one thing the shell does for it:
//
//   frame → shell  {type:'dispatch:tool-state', op:'get'}
//   frame → shell  {type:'dispatch:tool-state', op:'set', state:<JSON, ≤16 KB>}
//   shell → frame  {type:'dispatch:tool-state', state:<obj|null>[, ok:false]}
//                  in reply to `get`, as the ack of a `set` (ok:false = refused:
//                  not JSON or over the cap; `state` is then what is still
//                  stored), and once after the frame's `load` event.
//
// Stored in THIS browser's localStorage['dispatch-tool-state:<toolId>'] —
// per viewer, per device, never sent to the server. Accepted ONLY from the
// frame on screen (`event.source === frame.contentWindow`) of an open static,
// url or app tool, and only with the origin that frame can have: 'null' (the
// opaque origin of a sandboxed page) for static and url tools, the url's own
// origin for a url tool (it has allow-same-origin), our own origin for an app.
// A static frame's origin is opaque, so replies to it must target '*'; the
// payload is the viewer's own layout, nothing else is ever posted to one.

const TOOL_STATE_TYPE = 'dispatch:tool-state';
const TOOL_STATE_PREFIX = 'dispatch-tool-state:';
export const TOOL_STATE_MAX = 16 * 1024;

/** The open tool, when it is one whose frame may keep state. */
function stateTool() {
  const tool = openId ? findTool(openId) : null;
  return tool && (tool.kind === 'static' || tool.kind === 'url' || isApp(tool)) ? tool : null;
}

function urlOrigin(tool) {
  const u = parseHttpUrl(toolUrl(tool));
  return u ? u.origin : null;
}

function stateOriginOk(tool, origin) {
  if (isApp(tool)) return origin === location.origin;
  if (origin === 'null') return true;
  return tool.kind === 'url' && origin === urlOrigin(tool);
}

/** Where a push to this tool's frame is addressed: an app → our origin, a url
 *  tool → its own origin, a static tool (opaque origin) → '*'. */
function stateTarget(tool, origin = null) {
  if (isApp(tool)) return location.origin;
  if (origin && origin !== 'null') return origin;
  if (tool.kind === 'url' && origin !== 'null') return urlOrigin(tool) || '*';
  return '*';
}

function byteLength(s) {
  try { return new TextEncoder().encode(s).length; } catch { return s.length; }
}

/** The stored state for a tool id, or null (missing, unreadable, no storage). */
export function readToolState(id) {
  try {
    const raw = window.localStorage.getItem(TOOL_STATE_PREFIX + id);
    return raw == null ? null : JSON.parse(raw);
  } catch { return null; }
}

/** Store (or, with null, forget) a tool's state. False = refused: undefined,
 *  not JSON-serialisable, over TOOL_STATE_MAX bytes, or storage unavailable. */
function writeToolState(id, state) {
  try {
    if (state === null) { window.localStorage.removeItem(TOOL_STATE_PREFIX + id); return true; }
    const json = JSON.stringify(state);
    if (typeof json !== 'string' || byteLength(json) > TOOL_STATE_MAX) return false;
    window.localStorage.setItem(TOOL_STATE_PREFIX + id, json);
    return true;
  } catch { return false; }
}

function postToolState(tool, frame, target, extra = null) {
  if (!frame || !frame.contentWindow) return false;
  const msg = { type: TOOL_STATE_TYPE, state: readToolState(tool.id), ...(extra || {}) };
  try { frame.contentWindow.postMessage(msg, target); return true; } catch { return false; }
}

/** Push the open tool's stored state to its frame (the frame's `load`). */
export function pushToolState() {
  const tool = stateTool();
  const frame = $('tool-frame');
  if (!tool || !frame || !frame.getAttribute('src')) return false;
  return postToolState(tool, frame, stateTarget(tool));
}

function onToolStateMessage(ev) {
  if (!deps) return;
  const msg = ev.data;
  if (!msg || typeof msg !== 'object' || msg.type !== TOOL_STATE_TYPE) return;
  const tool = stateTool();
  const frame = $('tool-frame');
  if (!tool || !frame || !frame.contentWindow || ev.source !== frame.contentWindow) return;
  if (!stateOriginOk(tool, ev.origin)) return;
  const target = stateTarget(tool, ev.origin);
  if (msg.op === 'get') { postToolState(tool, frame, target); return; }
  if (msg.op === 'set') {
    const ok = Object.prototype.hasOwnProperty.call(msg, 'state') && writeToolState(tool.id, msg.state);
    postToolState(tool, frame, target, ok ? null : { ok: false });
  }
}

/** `#tool=<id>` → that tool's id, or null. */
export function hashToolId(hash = location.hash) {
  const m = /^#tool=([^&]+)$/.exec(hash || '');
  if (!m) return null;
  try { return decodeURIComponent(m[1]); } catch { return null; }
}

/** Open the tool named in the URL hash, if any. Unlocked only (see spec). */
export function openFromHash() {
  const id = hashToolId();
  if (!id || !deps || deps.state.decoy) return false;
  if (openId === id) return true;
  const ok = openTool(id, { launch: false });
  if (!ok) deps.toast(t('tools.not_found'), true);
  return ok;
}

// ===================== Settings → Tools =====================

function cloneTools(list) {
  return (list || []).map((x) => JSON.parse(JSON.stringify(x)));
}

/** What goes over the wire: builtins carry only id/kind/enabled (the only
 *  fields the manifest honours for them), the rest only schema fields. */
export function serializeTools(list) {
  return (list || []).map((x) => {
    if (x.kind === 'builtin') {
      const out = { id: x.id, kind: 'builtin', enabled: x.enabled !== false };
      if (x.open === 'window' || x.open === 'frame') out.open = x.open;
      return out;
    }
    // An app row may only carry id/kind/enabled (+ trusted, for an add-on in
    // the data dir) — its package on disk owns everything else.
    if (isApp(x)) {
      const out = { id: x.id, kind: 'app', enabled: x.enabled !== false };
      if (x.trusted === true) out.trusted = true;
      return out;
    }
    const out = {};
    for (const k of WRITABLE) {
      if (x[k] === undefined || x[k] === null || x[k] === '') continue;
      out[k] = x[k];
    }
    out.enabled = x.enabled !== false;
    return out;
  });
}

function sortedDraft() {
  // Builtins, then apps (by manifest order), then static/url — the rail's order.
  const order = (x) => {
    if (x.kind === 'builtin') return BUILTINS.indexOf(builtinOf(x));
    if (isApp(x)) return 100 + (Number.isFinite(x.order) ? Math.min(Math.max(x.order, 0), 800) : 800);
    return 1000;
  };
  return draft.map((x, i) => [x, i]).sort((a, b) => order(a[0]) - order(b[0]) || a[1] - b[1]);
}

function settingsError(msg, badIdx = null) {
  const box = $('tools-settings-error');
  if (box) {
    box.textContent = msg || '';
    box.classList.toggle('hidden', !msg);
  }
  if (settingsHost) {
    settingsHost.querySelectorAll('.tools-row.invalid').forEach((r) => r.classList.remove('invalid'));
    if (badIdx != null) {
      const row = settingsHost.querySelector(`.tools-row[data-idx="${badIdx}"]`);
      if (row) row.classList.add('invalid');
    }
  }
}

function locationText(tool) {
  if (tool.kind === 'static') return [tool.root, tool.entry || 'index.html'].filter(Boolean).join(' → ');
  if (tool.kind === 'url') return tool.url || '';
  return '—';
}

function toolRow(tool, idx) {
  const name = toolTitle(tool);
  // An app's title and icon come from its package (app.yaml), like a
  // builtin's from the code: only the switch is the operator's here.
  const builtin = tool.kind === 'builtin' || isApp(tool);
  const row = el('tr', { class: 'tools-row', dataset: { idx: String(idx) } });

  // Icon
  const iconCell = el('td', { class: 'tools-cell-icon' });
  if (builtin) {
    iconCell.append(toolTile(tool));
  } else {
    const inp = el('input', {
      type: 'text', class: 'tools-input tools-input-icon', maxlength: '16',
      'aria-label': t('tools.field_icon'), value: tool.icon || '',
    });
    // A live preview: the tile the rail will draw for what is typed.
    let preview = toolTile(tool);
    inp.addEventListener('input', () => {
      draft[idx].icon = inp.value.trim(); draftDirty = true;
      const next = toolTile(draft[idx]);
      preview.replaceWith(next); preview = next;
    });
    iconCell.append(el('div', { class: 'tools-icon-edit' }, [preview, inp]));
  }
  row.append(iconCell);

  // Title
  const titleCell = el('td', { class: 'tools-cell-title' });
  if (builtin) {
    titleCell.append(el('span', { text: name }));
    if (isApp(tool)) titleCell.append(el('div', { class: 'muted tools-id', text: tool.id }));
  }
  else {
    const inp = el('input', {
      type: 'text', class: 'tools-input', maxlength: '80',
      'aria-label': t('tools.field_title'), value: tool.title || '',
    });
    inp.addEventListener('input', () => { draft[idx].title = inp.value; draftDirty = true; });
    titleCell.append(inp, el('div', { class: 'muted tools-id', text: tool.id }));
  }
  row.append(titleCell);

  row.append(el('td', { class: 'tools-cell-kind', text: t(KIND_LABEL[tool.kind] || 'tools.kind_static') }));

  // Location (read-only by design: paths and refresh live in tools.yaml)
  const loc = el('td', { class: 'tools-cell-loc' });
  if (isApp(tool)) {
    loc.append(el('code', { class: 'tools-loc', dir: 'ltr', text: appSrc(tool) }));
  } else if (!builtin) {
    loc.append(el('code', { class: 'tools-loc', dir: 'ltr', text: locationText(tool) }));
    if (tool.has_refresh || tool.refresh) loc.append(el('span', { class: 'tools-badge', text: t('tools.has_refresh') }));
  } else if (tool.available === false) {
    // The switch is what gets saved; the feature itself is off on this install.
    loc.append(el('span', { class: 'muted', text: t('tools.not_installed') }));
  } else {
    loc.append(el('span', { class: 'muted', text: '—' }));
  }
  // How it opens: only where a tab is a real alternative (a page at an
  // address — static, url, or a builtin the server gave an address field).
  const windowable = tool.kind === 'static' || tool.kind === 'url'
    || (tool.kind === 'builtin' && Object.prototype.hasOwnProperty.call(tool, 'open_url'));
  if (windowable) {
    const wl = el('label', { class: 'tools-open-window', title: t('tools.open_window_aria', { name }) });
    const wcb = el('input', { type: 'checkbox', 'aria-label': t('tools.open_window_aria', { name }) });
    wcb.checked = tool.open === 'window';
    wcb.addEventListener('change', () => { draft[idx].open = wcb.checked ? 'window' : 'frame'; draftDirty = true; });
    wl.append(wcb, document.createTextNode(' ' + t('tools.col_open')));
    loc.append(el('div', {}, [wl]));
  }
  row.append(loc);

  // Enabled
  const onCell = el('td', { class: 'tools-cell-on' });
  const lbl = el('label', { class: 'switch', title: t('tools.enabled_aria', { name }) });
  const cb = el('input', { type: 'checkbox', 'aria-label': t('tools.enabled_aria', { name }) });
  cb.checked = tool.enabled !== false;
  cb.addEventListener('change', () => { draft[idx].enabled = cb.checked; draftDirty = true; });
  lbl.append(cb, el('span', { class: 'slider' }));
  onCell.append(lbl);
  row.append(onCell);

  // Remove
  const rmCell = el('td', { class: 'tools-cell-rm' });
  if (!builtin) {
    rmCell.append(el('button', {
      type: 'button', class: 'icon-btn ghost tools-remove',
      'aria-label': t('tools.remove', { name }), title: t('tools.remove', { name }), text: '🗑',
      onclick: () => { draft.splice(idx, 1); draftDirty = true; renderSettings(); },
    }));
  }
  row.append(rmCell);
  return row;
}

function addForm() {
  const form = el('form', { class: 'tools-add-form', novalidate: 'novalidate' });
  const kind = el('select', { class: 'tools-input', id: 'tools-add-kind', 'aria-label': t('tools.add_kind') }, [
    el('option', { value: 'static', text: t('tools.kind_static') }),
    el('option', { value: 'url', text: t('tools.kind_url') }),
  ]);
  const field = (key, id, attrs = {}) => {
    const input = el('input', { type: 'text', class: 'tools-input', id, autocomplete: 'off', spellcheck: 'false', ...attrs });
    return [el('label', { class: 'sec-field tools-field', for: id }, [el('span', { text: t(key) }), input]), input];
  };
  const [fId, iId] = field('tools.field_id', 'tools-add-id', { maxlength: '40', dir: 'ltr' });
  const [fTitle, iTitle] = field('tools.field_title', 'tools-add-title', { maxlength: '80' });
  const [fIcon, iIcon] = field('tools.field_icon', 'tools-add-icon', { maxlength: '16' });
  const [fRoot, iRoot] = field('tools.field_root', 'tools-add-root', { dir: 'ltr', placeholder: '/abs/path' });
  const [fEntry, iEntry] = field('tools.field_entry', 'tools-add-entry', { dir: 'ltr', placeholder: 'index.html' });
  const [fUrl, iUrl] = field('tools.field_url', 'tools-add-url', { dir: 'ltr', placeholder: 'https://' });
  const syncKind = () => {
    const isUrl = kind.value === 'url';
    fRoot.classList.toggle('hidden', isUrl);
    fEntry.classList.toggle('hidden', isUrl);
    fUrl.classList.toggle('hidden', !isUrl);
  };
  kind.addEventListener('change', syncKind);
  syncKind();
  const kindField = el('label', { class: 'sec-field tools-field', for: 'tools-add-kind' }, [el('span', { text: t('tools.add_kind') }), kind]);
  form.append(kindField, fId, fTitle, fIcon, fRoot, fEntry, fUrl);
  const actions = el('div', { class: 'tools-add-actions' }, [
    el('button', { type: 'submit', class: 'btn-primary', text: t('tools.add_confirm') }),
    el('button', { type: 'button', class: 'btn-secondary', text: t('common.cancel'), onclick: () => { addOpen = false; renderSettings(); } }),
  ]);
  form.append(actions);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const id = iId.value.trim();
    if (!ID_RE.test(id)) { settingsError(t('tools.id_invalid')); iId.focus(); return; }
    if (draft.some((x) => x.id === id) || (deps && deps.isBotId && deps.isBotId(id))) {
      settingsError(t('tools.id_taken')); iId.focus(); return;
    }
    if (!iTitle.value.trim()) { settingsError(t('tools.title_required')); iTitle.focus(); return; }
    const tool = { id, title: iTitle.value.trim(), kind: kind.value, enabled: true };
    if (iIcon.value.trim()) tool.icon = iIcon.value.trim();
    if (kind.value === 'static') {
      const root = iRoot.value.trim();
      if (!root.startsWith('/')) { settingsError(t('tools.root_required')); iRoot.focus(); return; }
      tool.root = root;
      if (iEntry.value.trim()) tool.entry = iEntry.value.trim();
    } else {
      if (!parseHttpUrl(iUrl.value.trim())) { settingsError(t('tools.url_invalid')); iUrl.focus(); return; }
      tool.url = iUrl.value.trim();
    }
    draft.push(tool);
    draftDirty = true;
    addOpen = false;
    settingsError('');
    renderSettings();
  });
  return form;
}

function renderSettings() {
  const host = settingsHost;
  if (!host) return;
  host.textContent = '';
  host.append(el('p', { class: 'muted modal-hint', text: t('tools.settings_hint', { path: toolsPath || 'tools.yaml' }) }));
  host.append(el('div', { class: 'tools-settings-error hidden', id: 'tools-settings-error', role: 'alert' }));
  if (!draft) {
    host.append(el('p', { class: 'muted', text: t('common.loading') }));
    return;
  }
  const rows = sortedDraft();
  if (!rows.length) host.append(el('p', { class: 'muted', text: t('tools.empty') }));
  else {
    const table = el('table', { class: 'tools-table' });
    table.append(el('thead', {}, [el('tr', {}, [
      el('th', { scope: 'col', text: t('tools.col_icon') }),
      el('th', { scope: 'col', text: t('tools.col_title') }),
      el('th', { scope: 'col', text: t('tools.col_kind') }),
      el('th', { scope: 'col', text: t('tools.col_location') }),
      el('th', { scope: 'col', text: t('tools.col_enabled') }),
      el('th', { scope: 'col' }, [el('span', { class: 'sr-only', text: t('common.delete') })]),
    ])]));
    const body = el('tbody');
    for (const [tool, idx] of rows) body.append(toolRow(tool, idx));
    table.append(body);
    host.append(el('div', { class: 'tools-table-wrap' }, [table]));
    host.append(el('p', { class: 'muted tools-loc-hint', text: t('tools.location_hint') }));
  }
  if (addOpen) host.append(addForm());
  else {
    host.append(el('button', {
      type: 'button', class: 'btn-secondary tools-add-btn', id: 'tools-add',
      text: t('tools.add'), onclick: () => { addOpen = true; renderSettings(); const f = $('tools-add-id'); if (f) f.focus(); },
    }));
  }
}

/** Settings → Tools was activated. Loads a fresh list into a draft. */
export async function mountToolsSettings(pane) {
  settingsHost = pane;
  draft = null;
  draftDirty = false;
  addOpen = false;
  renderSettings();
  try {
    const r = await api.tools();
    tools = normalize(r && r.tools);
    toolsPath = (r && typeof r.path === 'string') ? r.path : '';
    draft = cloneTools(tools);
  } catch (e) {
    draft = cloneTools(tools || []);
    renderSettings();
    settingsError(t('tools.load_failed', { error: (e && e.message) || '' }));
    return;
  }
  renderSettings();
}

export function toolsSettingsDirty() { return !!draftDirty; }

/** Pull an offending row index out of a 422 body, whatever shape it has. */
function errorIndex(body) {
  // The tools API puts it at the top level: {detail, index, field, message}.
  if (body && typeof body.index === 'number') return body.index;
  const d = body && (body.detail !== undefined ? body.detail : body);
  const look = (o) => {
    if (!o || typeof o !== 'object') return null;
    if (typeof o.index === 'number') return o.index;
    if (Array.isArray(o.loc)) { const n = o.loc.find((x) => typeof x === 'number'); if (n != null) return n; }
    return null;
  };
  if (Array.isArray(d)) { for (const x of d) { const n = look(x); if (n != null) return n; } return null; }
  return look(d) ?? look(d && d.errors);
}

/** PUT the draft. Inline error on a 422; rail re-rendered on success. */
export async function saveToolsSettings() {
  if (!draft) return false;
  const btn = $('tools-save');
  if (btn) btn.disabled = true;
  settingsError('');
  // Send in the displayed order (builtins first) so a 422's index points at
  // the row the user sees.
  const ordered = sortedDraft().map(([x]) => x);
  try {
    const r = await api.toolsSave(serializeTools(ordered));
    tools = normalize(r && r.tools);
    draft = cloneTools(tools);
    draftDirty = false;
    renderSettings();
    renderToolRail();
    if (deps) {
      deps.toast(t('tools.saved'));
      // A builtin switched on/off changes a server feature flag — let the
      // pane probes catch up.
      if (typeof deps.onSaved === 'function') deps.onSaved();
      if (openId && !findTool(openId)) closeTool();
    }
    return true;
  } catch (e) {
    const idx = e && e.status === 422 ? errorIndex(e.body) : null;
    const b = e && e.body;
    // Prefer the structured {field, message} the tools API sends; the flat
    // detail repeats the index we already print.
    const msg = (b && typeof b.message === 'string' && b.message)
      ? (b.field ? `${b.field}: ${b.message}` : b.message)
      : String((e && e.message) || '').replace(/^\d{3}:\s*/, '');
    // idx is into `ordered`; map it back to the draft index the rows carry.
    const draftIdx = idx != null && ordered[idx] ? draft.indexOf(ordered[idx]) : null;
    settingsError(idx != null
      ? t('tools.error_at', { n: String(idx + 1), error: msg })
      : t('tools.save_failed', { error: msg }), draftIdx);
    return false;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ===================== Wiring =====================

/** Hand this module the app. `d`:
 *    state            the app state (decoy, selectedBotId, activeThreadId)
 *    openers          {harness, studioforge, mail, practice} → the builtin openers
 *    openThread(id, {botId, hint, appId})  an app asked to open one of its threads
 *    closeToolPanes   main.js's pane closer (called with 'tool' before opening)
 *    builtinOn(f)     is that builtin's feature available right now?
 *    builtinDot(f)    the status-dot class for that builtin ('' = no dot)
 *    groupState()     worst-of the builtin services ('error'|'starting'|'running'|'')
 *                     for the desktop Tools button's dot
 *    restoreView(p)   put the selection {botId, threadId} back after a close
 *    paintPlaceholder(title, glyph)  the thread-list stand-in while a tool is up
 *                     (glyph is a node: toolGlyph()'s line icon or typed emoji)
 *    renderSidebar, isMobile, navigate, toast, isBotId, onSaved
 */
export function wireTools(d) {
  deps = d;
  const close = $('tool-close');
  if (close) close.addEventListener('click', () => closeTool());
  const back = $('tool-back');
  if (back) back.addEventListener('click', () => { closeTool(); if (deps.isMobile()) deps.navigate('bots'); });
  const refresh = $('tool-refresh');
  if (refresh) refresh.addEventListener('click', () => doRefresh());
  const save = $('tools-save');
  if (save) save.addEventListener('click', () => saveToolsSettings());
  window.addEventListener('hashchange', () => { if (hashToolId()) openFromHash(); });
  // Apps: messages from the framed page, and the shell's theme/language
  // handed to it once it has loaded and whenever either changes.
  window.addEventListener('message', onAppMessage);
  // Every framed tool: its saved layout, on request and once per load.
  window.addEventListener('message', onToolStateMessage);
  const frame = $('tool-frame');
  if (frame) {
    frame.addEventListener('load', () => {
      if (!frame.getAttribute('src')) return;
      onFrameLoad();
      if (openApp()) syncAppFrame();
      pushToolState();
    });
  }
  document.addEventListener('ui-theme-change', () => syncAppFrame());
  // The Tools popup: its keys, and a re-layout when the viewport crosses the
  // phone breakpoint (one button ⇄ tile grid). Any resize moves the anchor,
  // so an open popup closes rather than floating somewhere stale.
  const menu = $('tools-menu');
  if (menu) menu.addEventListener('keydown', onMenuKey);
  window.addEventListener('resize', () => {
    if (menuOpen) closeToolsMenu();
    if (deps && compactRail() !== lastCompact) renderToolRail();
  });
}

// Test hook: reset module state between jsdom cases.
export function _resetForTest() {
  if (typeof window !== 'undefined') {
    window.removeEventListener('message', onAppMessage);
    window.removeEventListener('message', onToolStateMessage);
  }
  if (typeof document !== 'undefined') {
    document.removeEventListener('click', onOutside, true);
    document.removeEventListener('keydown', onEscape, true);
  }
  clearFrameTimer(); frameLoaded = false; cardShown = null;
  deps = null; tools = null; toolsPath = ''; openId = null; prev = null;
  parked = []; menuOpen = false; menuMode = 'tools'; lastCompact = null;
  refreshing = false; draft = null; draftDirty = false; addOpen = false; settingsHost = null;
}
