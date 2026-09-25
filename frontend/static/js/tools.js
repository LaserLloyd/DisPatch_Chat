// Tools — the rail group, the generic full-page tool pane, and Settings → Tools.
//
// Contract: docs/design/2026-09-25-tools-plugins.md. A tool is an entry in the
// data dir's tools.yaml (never in git); the server lists them at /api/tools,
// serves a static tool's pages at /tools/<id>/ and runs its optional refresh.
//
// Three kinds, one rail:
//   builtin — the four panes main.js already owns (Harness, StudioForge,
//             Emails, Clients). This module only draws their tile and calls
//             the opener main.js hands in; their views are untouched.
//   static  — a directory on the host, framed from /tools/<id>/ with NO
//             allow-same-origin: the page may run its own JS but cannot touch
//             DisPatch's cookies or DOM (the server's CSP says the same).
//   url     — someone else's page, framed as-is. Loopback addresses only load
//             when DisPatch itself was opened on the host (the Harness rule).
//
// Every opener, builtin or generic, puts `tool-full` on <body>: the thread
// column goes away and the pane owns the whole content area. Closing takes the
// class off and hands the previous selection back to main.js.
//
// Pure-ish module: no side effects at import time (it is in sw.js SHELL and
// imported by main.js). main.js injects everything app-shaped via wireTools().

import { api } from './api.js?v=29';
import { t, relTimeLong } from './i18n.js?v=3';
import { el } from './util.js?v=19';

// Builtin ids are the pseudo-bot ids main.js has always used, in rail order.
const BUILTINS = [
  { id: 'deepseek-harness', feature: 'harness', glyph: 'dsh', cls: 'harness-avatar', name: 'harness.name', aria: 'harness.sidebar_aria' },
  { id: 'studioforge-panel', feature: 'studioforge', glyph: 'SF', cls: 'studioforge-avatar', name: 'studioforge.name', aria: 'studioforge.sidebar_aria' },
  { id: 'mail-panel', feature: 'mail', glyph: '✉', cls: 'mail-avatar', name: 'mail.name', aria: 'mail.sidebar_aria' },
  { id: 'clients-panel', feature: 'practice', glyph: '👥', cls: 'clients-avatar', name: 'clients.name', aria: 'clients.sidebar_aria' },
];
const BUILTIN_BY_ID = new Map(BUILTINS.map((b) => [b.id, b]));
const BUILTIN_BY_FEATURE = new Map(BUILTINS.map((b) => [b.feature, b]));

// Literal keys (not t(`tools.kind_${k}`)) so the i18n key scan can see them.
const KIND_LABEL = { builtin: 'tools.kind_builtin', static: 'tools.kind_static', url: 'tools.kind_url' };

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
const WRITABLE = ['id', 'title', 'icon', 'kind', 'root', 'entry', 'url', 'enabled', 'safe', 'refresh'];

let deps = null;
let tools = null;        // null = never loaded / API absent; [] = loaded, none
let toolsPath = '';
let openId = null;       // the generic tool on screen, or null
let prev = null;         // {botId, threadId} to hand back on close
let refreshing = false;
let statusSeq = 0;

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
function onHost() { return isLoopbackHost(location.hostname); }

function parseHttpUrl(u) {
  try {
    const url = new URL(String(u || ''));
    return (url.protocol === 'http:' || url.protocol === 'https:') ? url : null;
  } catch { return null; }
}

function normalize(list) {
  if (!Array.isArray(list)) return [];
  return list.filter((x) => x && typeof x === 'object' && typeof x.id === 'string' && x.id);
}

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

/** The tiles the rail should draw, builtins first. */
export function railEntries() {
  if (!deps) return [];
  const st = deps.state;
  const builtins = [];
  const others = [];
  for (const tool of allTools()) {
    if (tool.enabled === false) continue;
    if (tool.kind === 'builtin') {
      // Never in Safe Mode, and only when the pane's own probe found it on.
      const b = builtinOf(tool);
      if (st.decoy || !b || !deps.builtinOn(b.feature)) continue;
      builtins.push(tool);
    } else if (tool.kind === 'static' || tool.kind === 'url') {
      // The API already filters for Safe Mode; this is the belt to its braces.
      if (st.decoy && !tool.safe) continue;
      others.push(tool);
    }
  }
  const order = (x) => BUILTINS.indexOf(builtinOf(x));
  builtins.sort((a, b) => order(a) - order(b));
  return builtins.concat(others);
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

function tile(tool) {
  const st = deps.state;
  const active = st.selectedBotId === tool.id;
  const title = toolTitle(tool);
  const b = tool.kind === 'builtin' ? builtinOf(tool) : null;
  const btn = el('button', {
    class: 'bot-btn tool-btn' + (active ? ' active' : ''),
    type: 'button',
    dataset: { tool: tool.id, id: tool.id },
    // Builtins keep their longer "…, unlocked only" description.
    'aria-label': b ? t(b.aria) : title,
    draggable: 'false',
    onclick: () => openTool(tool.id),
  });
  if (active) btn.setAttribute('aria-current', 'page');
  btn.append(el('span', {
    class: 'bot-avatar terminal-avatar tool-avatar' + (b ? ' ' + b.cls : ''),
    'aria-hidden': 'true',
    text: b ? b.glyph : (tool.icon || '🧩'),
  }));
  btn.append(el('span', { class: 'bot-name-tip', text: title }));
  btn.append(el('span', { class: 'bot-name-label', text: title }));
  if (b) {
    // The same per-service colours the ⌥ menu rows used; main.js's
    // renderHarness/renderStudioForge keep patching this dot in place.
    const dot = deps.builtinDot(b.feature);
    if (dot) btn.append(el('span', { class: 'bot-status-dot terminal-sidedot ' + dot }));
  }
  return btn;
}

/** Draw the Tools group into #tool-list. Cheap; main.js calls it on every
 *  sidebar repaint so active state and feature flags never go stale. */
export function renderToolRail() {
  const host = $('tool-list');
  if (!host || !deps) return;
  const entries = railEntries();
  const focused = document.activeElement && host.contains(document.activeElement)
    ? document.activeElement.closest('[data-tool]')?.dataset.tool : null;
  host.textContent = '';
  host.hidden = entries.length === 0;
  if (!entries.length) return;
  host.append(el('div', { class: 'tool-list-head', 'aria-hidden': 'true', text: t('nav.tools') }));
  for (const tool of entries) host.append(tile(tool));
  if (focused) {
    const again = host.querySelector(`[data-tool="${CSS.escape ? CSS.escape(focused) : focused}"]`);
    if (again) again.focus({ preventScroll: true });
  }
}

/** The rail dot for a builtin, for main.js to patch in place. */
export function railToolDot(id) {
  const host = $('tool-list');
  if (!host) return null;
  for (const b of host.querySelectorAll('[data-tool]')) {
    if (b.dataset.tool === id) return b.querySelector('.bot-status-dot');
  }
  return null;
}

// ===================== Pane =====================

function frameSrc(tool) {
  if (tool.kind === 'static') return `/tools/${encodeURIComponent(tool.id)}/`;
  const u = parseHttpUrl(tool.url);
  return u ? u.href : '';
}

function showNote(textKey, hintKey) {
  const note = $('tool-note');
  const frame = $('tool-frame');
  if (frame) { frame.classList.add('hidden'); frame.removeAttribute('src'); }
  if (!note) return;
  $('tool-note-text').textContent = t(textKey);
  $('tool-note-hint').textContent = hintKey ? t(hintKey) : '';
  note.classList.remove('hidden');
}

function loadFrame(tool) {
  const frame = $('tool-frame');
  const note = $('tool-note');
  if (!frame) return;
  const src = frameSrc(tool);
  if (!src) { showNote('tools.bad_url_text', 'tools.location_hint'); return; }
  if (tool.kind === 'url' && isLoopbackHost(new URL(src).hostname) && !onHost()) {
    showNote('tools.loopback_text', 'tools.loopback_hint');
    return;
  }
  if (note) note.classList.add('hidden');
  // Sandbox BEFORE src: the attribute is read when the navigation starts.
  frame.setAttribute('sandbox', tool.kind === 'url' ? SANDBOX_URL : SANDBOX_STATIC);
  frame.setAttribute('title', t('tools.frame_title', { name: toolTitle(tool) }));
  frame.classList.remove('hidden');
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
  // A url tool the server could not reach: say so instead of an empty frame.
  if (tool.kind === 'url' && st && st.reachable === false) {
    showError(t('tools.unreachable_text'), t('tools.unreachable_hint'));
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
      if (frame && frame.getAttribute('src')) frame.setAttribute('src', frameSrc(tool));
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

/** Open a tool by id. Builtins go to their own opener; static/url tools get
 *  the generic pane. Either way the page ends up in `tool-full`. */
export function openTool(id) {
  if (!deps || !id) return false;
  const st = deps.state;
  const tool = findTool(id);
  const b = BUILTIN_BY_ID.get(id) || (tool && tool.kind === 'builtin' ? builtinOf(tool) : null);
  if (b) {
    if (st.decoy) return false;
    if (tool && tool.enabled === false) return false;
    const opener = deps.openers && deps.openers[b.feature];
    if (typeof opener !== 'function') return false;
    if (deps.beforeOpen) deps.beforeOpen();
    opener();
    return true;
  }
  if (!tool || tool.enabled === false) return false;
  if (tool.kind !== 'static' && tool.kind !== 'url') return false;
  if (st.decoy && !tool.safe) return false;

  // Remember where we came from — but not when hopping tool to tool, or the
  // "previous thread" would become the tool we just left.
  if (!isToolId(st.selectedBotId)) prev = { botId: st.selectedBotId, threadId: st.activeThreadId };
  if (deps.beforeOpen) deps.beforeOpen();
  deps.closeToolPanes('tool');
  if (openId && openId !== id) teardown();
  openId = id;
  refreshing = false;
  st.selectedBotId = id;

  const icon = $('tool-icon');
  if (icon) icon.textContent = tool.icon || '🧩';
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
  if (deps.paintPlaceholder) deps.paintPlaceholder(toolTitle(tool), tool.icon || '🧩');
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
  if (frame) { frame.classList.add('hidden'); frame.removeAttribute('src'); }
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
  const p = prev;
  prev = null;
  if (restore && deps) deps.restoreView(p);
}

export function toolOpen() { return openId; }

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
  const ok = openTool(id);
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
    if (x.kind === 'builtin') return { id: x.id, kind: 'builtin', enabled: x.enabled !== false };
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
  const order = (x) => (x.kind === 'builtin' ? BUILTINS.indexOf(builtinOf(x)) : 1000);
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
  const builtin = tool.kind === 'builtin';
  const row = el('tr', { class: 'tools-row', dataset: { idx: String(idx) } });
  const b = builtin ? builtinOf(tool) : null;

  // Icon
  const iconCell = el('td', { class: 'tools-cell-icon' });
  if (builtin) {
    iconCell.append(el('span', { class: 'bot-avatar terminal-avatar tool-avatar' + (b ? ' ' + b.cls : ''), 'aria-hidden': 'true', text: b ? b.glyph : '?' }));
  } else {
    const inp = el('input', {
      type: 'text', class: 'tools-input tools-input-icon', maxlength: '8',
      'aria-label': t('tools.field_icon'), value: tool.icon || '',
    });
    inp.addEventListener('input', () => { draft[idx].icon = inp.value.trim(); draftDirty = true; });
    iconCell.append(inp);
  }
  row.append(iconCell);

  // Title
  const titleCell = el('td', { class: 'tools-cell-title' });
  if (builtin) titleCell.append(el('span', { text: name }));
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
  if (!builtin) {
    loc.append(el('code', { class: 'tools-loc', dir: 'ltr', text: locationText(tool) }));
    if (tool.has_refresh || tool.refresh) loc.append(el('span', { class: 'tools-badge', text: t('tools.has_refresh') }));
  } else {
    loc.append(el('span', { class: 'muted', text: '—' }));
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
  const [fIcon, iIcon] = field('tools.field_icon', 'tools-add-icon', { maxlength: '8' });
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
 *    closeToolPanes   main.js's pane closer (called with 'tool' before opening)
 *    builtinOn(f)     is that builtin's feature available right now?
 *    builtinDot(f)    the status-dot class for that builtin ('' = no dot)
 *    restoreView(p)   put the selection {botId, threadId} back after a close
 *    beforeOpen()     leave any view a tool pane cannot show over (the Job Board)
 *    paintPlaceholder(title, icon)  the thread-list stand-in while a tool is up
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
}

// Test hook: reset module state between jsdom cases.
export function _resetForTest() {
  deps = null; tools = null; toolsPath = ''; openId = null; prev = null;
  refreshing = false; draft = null; draftDirty = false; addOpen = false; settingsHost = null;
}
