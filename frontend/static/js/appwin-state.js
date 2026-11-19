// App windows - the pure half (docs/design/app-windows.md). Which tool windows
// exist, which one is on screen, which are minimised to pills and in what
// order, each tool's remembered layout / float rectangle / dock share, the
// snap zones and the clamping sums. No DOM and no side effects at import, so
// it is node-tested (tests/appwin-state.test.js); js/appwin.js draws it.
//
// model = {
//   wins:  [{id, state: 'open'|'min', order, att?}], live windows, at most ONE open;
//          order = first-open order (pills never reshuffle); att = finished while a pill
//   open:  id | null,                             the one on screen
//   prefs: {id: {layout, rect?, ratio?}},         per tool, kept after a close
//   def:   {layout, ratio} | null,                default for a tool with no prefs
//   seq:   n,                                     open counter (pill order)
// }
// Status, badge, attention and unsaved are live-only and live in js/appwin.js.

export const VERSION = 1;
export const KEY_PREFIX = 'dispatch-appwin:';   // + 'v1:' + profile id
export const LAYOUTS = Object.freeze(['float', 'left', 'right', 'full']);
export const DOCKS = Object.freeze(['left', 'right']);
export const MAX_WINS = 24;
export const MAX_BYTES = 8 * 1024;
export const SNAP_EDGE = 24;          // px from an edge of the content area that snaps
export const KEY_MOVE = 24;           // px per arrow press on the move handle
export const KEY_MOVE_BIG = 96;       // with Shift
export const DEFAULT_W = 1040;        // first-open float size when the tool names none
export const DEFAULT_H = 760;
export const FLOOR_W = 240;           // never smaller than this whatever a manifest says
export const FLOOR_H = 200;

export const OPTION_DEFAULTS = Object.freeze({
  layout: 'float', min_width: 360, min_height: 320, dockable: true, keep_alive: true, auto: false,
});
/** Below this content width a tool that did not ask for a layout opens docked at
 *  the inline end: a float there covers most of the chat. */
export const WIDE = 1600;

// A tool id, or an add-on's group window ("group:<owner>").
const ID_RE = /^(group:)?[a-z0-9-]{1,40}$/;
const PROFILE_RE = /^[A-Za-z0-9_-]{1,64}$/;
const SPLIT_KEY = 'dispatch-tool-split';
const SPLIT_RATIO_KEY = 'dispatch-tool-split-ratio';
const SPLIT_THREAD_KEY = 'dispatch-tool-split-thread';

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const int = (v, lo, hi) => { const n = num(v); return n !== null && Number.isInteger(n) && n >= lo && n <= hi ? n : null; };

/** A tool row's `window` block (already validated by the server) with every
 *  default filled; anything odd falls back to the default. */
export function windowOptions(raw) {
  const w = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  const o = { ...OPTION_DEFAULTS };
  if (LAYOUTS.includes(w.layout)) o.layout = w.layout;
  const mw = int(w.min_width, FLOOR_W, 1600); if (mw !== null) o.min_width = mw;
  const mh = int(w.min_height, FLOOR_H, 1200); if (mh !== null) o.min_height = mh;
  const ww = int(w.width, 320, 3840); if (ww !== null) o.width = ww;
  const hh = int(w.height, 240, 2160); if (hh !== null) o.height = hh;
  if (typeof w.dockable === 'boolean') o.dockable = w.dockable;
  if (typeof w.keep_alive === 'boolean') o.keep_alive = w.keep_alive;
  // The server says whether the tool named a layout itself (layout_auto).
  o.auto = w.layout_auto === true;
  if (!o.dockable && DOCKS.includes(o.layout)) o.layout = 'float';
  return o;
}

export function empty() { return { wins: [], open: null, prefs: {}, def: null, seq: 0 }; }

export function find(model, id) { return model.wins.find((w) => w.id === id) || null; }

/** Minimised windows in pill order (first opened first: stable targets). */
export function pills(model) {
  return model.wins.filter((w) => w.state === 'min').sort((a, b) => a.order - b.order);
}

/** The remembered (or default) layout settings for a tool. `env.contentW` (the
 *  width beside the rail) picks the default of a tool that did not ask for one:
 *  docked at the inline end below WIDE, a float above. */
export function prefsFor(model, id, opts = OPTION_DEFAULTS, env = {}) {
  const p = model.prefs[id];
  const o = opts || OPTION_DEFAULTS;
  let layout = (p && p.layout) || (model.def && model.def.layout) || defaultLayout(o, env);
  if (!o.dockable && DOCKS.includes(layout)) layout = 'float';
  const ratio = p && num(p.ratio) !== null ? p.ratio : (model.def && num(model.def.ratio) !== null ? model.def.ratio : 0.5);
  return { layout, rect: p && p.rect ? { ...p.rect } : null, ratio };
}

export function defaultLayout(opts, { contentW = null, rtl = false } = {}) {
  const o = opts || OPTION_DEFAULTS;
  const w = num(contentW);
  if (o.auto && o.dockable !== false && w !== null && w > 0 && w < WIDE) return rtl ? 'left' : 'right';
  return o.layout || 'float';
}

function withPrefs(model, id, patch) {
  const cur = model.prefs[id] || {};
  return { ...model, prefs: { ...model.prefs, [id]: { ...cur, ...patch } } };
}

function minimiseOthers(model, keep) {
  const wins = model.wins.map((w) => (w.id === keep || w.state !== 'open' ? w : { ...w, state: 'min' }));
  return { ...model, wins };
}

/** Open a tool's window, or bring back the one it already has. Whatever else
 *  was on screen is minimised (never closed). Returns the new model. */
export function openWin(model, id) {
  if (!ID_RE.test(String(id || ''))) return model;
  let m = minimiseOthers(model, id);
  if (find(m, id)) {
    m = { ...m, wins: m.wins.map((w) => { if (w.id !== id) return w; const { att, ...rest } = w; void att; return { ...rest, state: 'open' }; }) };
  } else {
    if (m.wins.length >= MAX_WINS) {
      // The oldest pill gives way (a cap so storage stays small).
      const oldest = pills(m)[0];
      if (oldest) m = { ...m, wins: m.wins.filter((w) => w.id !== oldest.id) };
    }
    const seq = m.seq + 1;
    m = { ...m, seq, wins: [...m.wins, { id, state: 'open', order: seq }] };
  }
  return { ...m, open: id };
}

export const restore = openWin;

/** Back to chat: the window becomes a pill, in its first-open place. */
export function minimize(model, id) {
  const w = find(model, id);
  if (!w || w.state === 'min') return model;
  return {
    ...model,
    wins: model.wins.map((x) => (x.id === id ? { ...x, state: 'min' } : x)),
    open: model.open === id ? null : model.open,
  };
}

/** A pill's "finished while you were away" mark (kept until it is shown). */
export function setAttention(model, id, on) {
  const w = find(model, id);
  if (!w || (on && w.state !== 'min') || !!w.att === !!on) return model;
  return { ...model, wins: model.wins.map((x) => { if (x.id !== id) return x; const { att, ...rest } = x; void att; return on ? { ...rest, att: true } : rest; }) };
}

/** Close: the window goes; its remembered layout stays for next time. */
export function closeWin(model, id) {
  if (!find(model, id)) return model;
  return { ...model, wins: model.wins.filter((w) => w.id !== id), open: model.open === id ? null : model.open };
}

/** Drop every window whose tool this session cannot see (a profile's tool list,
 *  a tool switched off). Prefs of unknown tools go too. */
export function filterKnown(model, ids) {
  const ok = new Set(ids);
  const wins = model.wins.filter((w) => ok.has(w.id));
  const prefs = {};
  for (const [k, v] of Object.entries(model.prefs)) if (ok.has(k)) prefs[k] = v;
  return { ...model, wins, prefs, open: model.open && ok.has(model.open) ? model.open : null };
}

export function setLayout(model, id, layout, opts = OPTION_DEFAULTS) {
  if (!LAYOUTS.includes(layout)) return model;
  if (DOCKS.includes(layout) && opts && opts.dockable === false) return model;
  return withPrefs(model, id, { layout });
}

export function setRect(model, id, rect) {
  const r = cleanRect(rect);
  return r ? withPrefs(model, id, { rect: r }) : model;
}

export function setRatio(model, id, ratio) {
  const v = num(ratio);
  return v !== null && v > 0 && v < 1 ? withPrefs(model, id, { ratio: Math.round(v * 10000) / 10000 }) : model;
}

// ---- geometry (content-area relative CSS px) ----

function cleanRect(r) {
  if (!r || typeof r !== 'object') return null;
  const x = num(r.x); const y = num(r.y); const w = num(r.w); const h = num(r.h);
  if (x === null || y === null || w === null || h === null || w <= 0 || h <= 0) return null;
  if (Math.abs(x) > 20000 || Math.abs(y) > 20000 || w > 20000 || h > 20000) return null;
  return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
}

/** Keep a float inside `bounds` ({w, h}) and no smaller than the tool's minimum
 *  (unless the area itself is smaller, then it fills it). */
export function clampRect(rect, bounds, opts = OPTION_DEFAULTS) {
  const bw = Math.max(0, Math.floor(num(bounds && bounds.w) || 0));
  const bh = Math.max(0, Math.floor(num(bounds && bounds.h) || 0));
  const r = cleanRect(rect) || initialRect(opts, bounds);
  const minW = Math.min(bw, (opts && opts.min_width) || OPTION_DEFAULTS.min_width);
  const minH = Math.min(bh, (opts && opts.min_height) || OPTION_DEFAULTS.min_height);
  const w = Math.min(bw, Math.max(minW, r.w));
  const h = Math.min(bh, Math.max(minH, r.h));
  const x = Math.min(Math.max(0, r.x), bw - w);
  const y = Math.min(Math.max(0, r.y), bh - h);
  return { x, y, w, h };
}

/** A tool's first float. A size the tool asked for (manifest width/height) wins,
 *  up to the area less MARGIN on each side; with none, 60 % of the area's width
 *  (at most DEFAULT_W) and at most 760 px tall. Never under its minimum; placed
 *  at the inline-end side, a margin in from the edges. */
export const FIRST_SHARE = 0.6;
export const FIRST_MAX_H = 760;
export const MARGIN = 16;
export function initialRect(opts, bounds, { rtl = false } = {}) {
  const bw = Math.max(0, Math.floor(num(bounds && bounds.w) || 0));
  const bh = Math.max(0, Math.floor(num(bounds && bounds.h) || 0));
  const o = opts || OPTION_DEFAULTS;
  const room = (n) => Math.max(0, n - 2 * MARGIN);
  const wantW = o.width ? Math.min(o.width, room(bw)) : Math.min(Math.floor(bw * FIRST_SHARE), DEFAULT_W);
  const wantH = o.height ? Math.min(o.height, room(bh)) : Math.min(room(bh), FIRST_MAX_H, DEFAULT_H);
  const w = Math.min(bw, Math.max(o.min_width || 0, wantW));
  const h = Math.min(bh, Math.max(o.min_height || 0, wantH));
  const x = rtl ? Math.min(MARGIN, bw - w) : Math.max(0, bw - w - MARGIN);
  const y = Math.max(0, Math.min(MARGIN, bh - h));
  return { x: Math.max(0, x), y, w, h };
}

/** Where a title-bar drag would snap: pointer `pt` relative to the content
 *  area. Left/right edges dock (if the tool may), the top edge fills. */
export function snapZone(pt, bounds, { dockable = true, edge = SNAP_EDGE } = {}) {
  const x = num(pt && pt.x); const y = num(pt && pt.y);
  const w = num(bounds && bounds.w); const h = num(bounds && bounds.h);
  if (x === null || y === null || !w || !h) return null;
  if (y <= edge) return 'full';
  if (dockable && x <= edge) return 'left';
  if (dockable && x >= w - edge) return 'right';
  return null;
}

/** What is actually drawn: a phone always gets the sheet; a dock that does not
 *  fit (or a tool that may not dock) is shown full until it fits again. */
export function effectiveLayout(layout, { mobile = false, canDock = true, dockable = true } = {}) {
  if (mobile) return 'sheet';
  if (DOCKS.includes(layout)) return dockable && canDock ? layout : 'full';
  return LAYOUTS.includes(layout) ? layout : 'float';
}

/** Keyboard on the move handle: arrows move, Alt+arrows resize. Returns the
 *  new clamped rect, or null when the key is not ours. `rtl` does not flip:
 *  the keys move the window the way the arrow points. */
export function keyRect(key, rect, bounds, opts, { shift = false, alt = false } = {}) {
  const step = shift ? KEY_MOVE_BIG : KEY_MOVE;
  const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[key];
  if (!d) return null;
  const r = clampRect(rect, bounds, opts);
  const next = alt ? { ...r, w: r.w + d[0], h: r.h + d[1] } : { ...r, x: r.x + d[0], y: r.y + d[1] };
  return clampRect(next, bounds, opts);
}

// ---- storage ----

/** The key for a profile's windows, or null: a session with no profile id
 *  keeps its windows for this page only (never a key two people share). */
export function storageKey(profileId) {
  return typeof profileId === 'string' && PROFILE_RE.test(profileId) ? `${KEY_PREFIX}v${VERSION}:${profileId}` : null;
}

/** What is written: ids, states, order, layouts, rects, ratios. Nothing a tool said. */
export function serialize(model) {
  const prefs = {};
  for (const [k, v] of Object.entries(model.prefs || {})) {
    if (!ID_RE.test(k) || !v) continue;
    const o = {};
    if (LAYOUTS.includes(v.layout)) o.layout = v.layout;
    const r = cleanRect(v.rect); if (r) o.rect = r;
    const q = num(v.ratio); if (q !== null && q > 0 && q < 1) o.ratio = q;
    prefs[k] = o;
  }
  return JSON.stringify({
    v: VERSION,
    wins: model.wins.map((w) => (w.att ? { id: w.id, state: w.state, order: w.order, att: true } : { id: w.id, state: w.state, order: w.order })),
    open: model.open,
    prefs,
    def: model.def,
    seq: model.seq,
  });
}

/** Read stored state. Anything malformed is dropped piece by piece; a wholly
 *  unreadable value is an empty model. */
export function parse(raw) {
  const m = empty();
  if (typeof raw !== 'string' || !raw || raw.length > MAX_BYTES) return m;
  let d;
  try { d = JSON.parse(raw); } catch { return m; }
  if (!d || typeof d !== 'object' || d.v !== VERSION) return m;
  const seen = new Set();
  if (Array.isArray(d.wins)) {
    for (const w of d.wins.slice(0, MAX_WINS)) {
      if (!w || !ID_RE.test(String(w.id)) || seen.has(w.id)) continue;
      if (w.state !== 'open' && w.state !== 'min') continue;
      seen.add(w.id);
      const one = { id: w.id, state: w.state, order: int(w.order, 0, 1e9) ?? 0 };
      if (w.att === true && w.state === 'min') one.att = true;
      m.wins.push(one);
    }
  }
  // At most one open, and `open` must name it.
  const open = typeof d.open === 'string' && m.wins.some((w) => w.id === d.open && w.state === 'open') ? d.open : null;
  let seq = int(d.seq, 0, 1e9) ?? 0;
  m.wins = m.wins.map((w) => (w.state === 'open' && w.id !== open ? { ...w, state: 'min' } : w));
  m.open = open;
  m.seq = Math.max(seq, ...m.wins.map((w) => w.order), 0);
  if (d.prefs && typeof d.prefs === 'object' && !Array.isArray(d.prefs)) {
    for (const [k, v] of Object.entries(d.prefs).slice(0, 64)) {
      if (!ID_RE.test(k) || !v || typeof v !== 'object') continue;
      const o = {};
      if (LAYOUTS.includes(v.layout)) o.layout = v.layout;
      const r = cleanRect(v.rect); if (r) o.rect = r;
      const q = num(v.ratio); if (q !== null && q > 0 && q < 1) o.ratio = q;
      m.prefs[k] = o;
    }
  }
  if (d.def && typeof d.def === 'object' && DOCKS.includes(d.def.layout)) {
    const q = num(d.def.ratio);
    m.def = { layout: d.def.layout, ratio: q !== null && q > 0 && q < 1 ? q : 0.5 };
  }
  return m;
}

export function load(profileId) {
  const k = storageKey(profileId);
  if (!k) return empty();
  try { return parse(localStorage.getItem(k)); } catch { return empty(); }
}

export function save(profileId, model) {
  const k = storageKey(profileId);
  if (!k) return false;
  try {
    const s = serialize(model);
    if (s.length > MAX_BYTES) return false;
    localStorage.setItem(KEY_PREFIX + k.slice(KEY_PREFIX.length), s);
    return true;
  } catch { return false; }
}

/** The morning's split view preference becomes the default layout of a tool
 *  opened for the first time: docked on the inline end at the saved share.
 *  Read once per device; the old keys are removed afterwards. */
export function migrateSplit(model, { rtl = false } = {}) {
  let on = false; let ratio = null;
  try {
    on = localStorage.getItem(SPLIT_KEY) === '1';
    ratio = Number(localStorage.getItem(SPLIT_RATIO_KEY));
    localStorage.removeItem(SPLIT_KEY);
    localStorage.removeItem(SPLIT_RATIO_KEY);
    if (typeof sessionStorage !== 'undefined') sessionStorage.removeItem(SPLIT_THREAD_KEY);
  } catch { return model; }
  if (!on || model.def) return model;
  const r = Number.isFinite(ratio) && ratio > 0 && ratio < 1 ? ratio : 0.5;
  return { ...model, def: { layout: rtl ? 'left' : 'right', ratio: r } };
}

// ---- docking: the chat and the tool share the chat column (the split view's sums) ----
// `total` is the width the pair can have; `ratio` is the TOOL's share of it once
// the divider is taken out. The chat never goes under MIN_CHAT, the tool never
// under its own minimum (MIN_TOOL at least).

export const MIN_CHAT = 320;
export const MIN_TOOL = 360;
export const DIVIDER = 10;
export const DEFAULT_RATIO = 0.5;
export const KEY_STEP = 24;
export const KEY_STEP_BIG = 96;

const minTool = (opts) => Math.max(MIN_TOOL, (opts && opts.min_width) || 0);

export function room(total) {
  const n = Number(total);
  return Number.isFinite(n) ? Math.max(0, Math.floor(n) - DIVIDER) : 0;
}

export function canDock(total, opts) { return room(total) >= MIN_CHAT + minTool(opts); }

/** 'list' (the thread list stays), 'nolist' (it steps aside) or null (no room: full). */
export function dockMode({ withList, noList }, opts) {
  if (canDock(withList, opts)) return 'list';
  if (canDock(noList, opts)) return 'nolist';
  return null;
}

export function clampRatio(ratio, total, opts) {
  if (!canDock(total, opts)) return null;
  const r = room(total);
  let v = Number(ratio);
  if (!Number.isFinite(v) || v <= 0 || v >= 1) v = DEFAULT_RATIO;
  return Math.min(1 - MIN_CHAT / r, Math.max(minTool(opts) / r, v));
}

/** {chat, tool} whole px summing to room(total), or null when it cannot dock. */
export function dockWidths(ratio, total, opts) {
  const v = clampRatio(ratio, total, opts);
  if (v == null) return null;
  const r = room(total);
  const tool = Math.round(r * v);
  return { chat: r - tool, tool };
}

export function ratioForTool(toolPx, total, opts) {
  const r = room(total);
  if (!r) return DEFAULT_RATIO;
  return clampRatio(Number(toolPx) / r, total, opts);
}

/** A key on the divider -> the new ratio, or null. Left/Right move the divider
 *  that way on screen; `side` is where the TOOL is ('left'|'right'); Shift is a
 *  bigger step; Home/End give the tool its smallest / largest share. */
export function stepDock(key, { ratio, total, side = 'right', shift = false, opts } = {}) {
  const w = dockWidths(ratio, total, opts);
  if (!w) return null;
  const step = shift ? KEY_STEP_BIG : KEY_STEP;
  const r = room(total);
  let tool;
  if (key === 'ArrowRight') tool = w.tool + (side === 'right' ? -step : step);
  else if (key === 'ArrowLeft') tool = w.tool + (side === 'right' ? step : -step);
  else if (key === 'Home') tool = minTool(opts);
  else if (key === 'End') tool = r - MIN_CHAT;
  else return null;
  return ratioForTool(tool, total, opts);
}
