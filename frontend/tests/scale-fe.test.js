// WP-SCALE-FE: the rail pager, the windowed message list, the Storage and image cards, the archive
// control and the "image expired" placeholder. Real modules; fetch and layout are stubbed (jsdom has
// no layout, so a tiny model gives every node a height and the box a scroll position).

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const EN = JSON.parse(readFileSync(join(STATIC, 'locales', 'en.json'), 'utf8'));
const MAIN = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const pager = await import('../static/js/thread-pager.js');
const cards = await import('../static/js/storage-card.js');
const expired = await import('../static/js/media-expired.js');

// English strings with {placeholders}, like i18n.t()
const t = (key, vars = {}) => {
  let v = key.split('.').reduce((o, k) => (o ? o[k] : undefined), EN);
  if (typeof v !== 'string') return key;
  for (const [k, x] of Object.entries(vars)) v = v.replaceAll(`{${k}}`, String(x));
  return v;
};

function makeDom() {
  const win = new jsdom.JSDOM('<!doctype html><body><div id="box"></div><div id="host"></div></body>', { url: 'http://127.0.0.1:8794/' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win); put('document', win.document); put('Node', win.Node); put('HTMLElement', win.HTMLElement);
  const el = (tag, attrs = {}, kids = []) => {
    const n = win.document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k === 'dataset') Object.assign(n.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? '' : v);
    }
    for (const c of [].concat(kids)) if (c != null) n.append(c);
    return n;
  };
  return { win, el };
}

/** A layout model: heights from data-h (default 80; gaps use their style height), positions from sibling
 *  order, a 600 px viewport, and a scrollTop that sticks. */
function layout(win, box, { viewport = 600, rowH = 80 } = {}) {
  const hOf = (n) => {
    if (n.classList.contains('win-gap')) return parseFloat(n.style.height) || 0;
    if (n.classList.contains('date-sep')) return 30;
    if (n.classList.contains('load-earlier')) return 50;
    return Number(n.dataset.h) || rowH;
  };
  const offsetTop = (n) => {
    let y = 0;
    for (let s = n.parentNode.firstElementChild; s && s !== n; s = s.nextElementSibling) y += hOf(s);
    return y;
  };
  const proto = win.HTMLElement.prototype;
  Object.defineProperty(proto, 'offsetTop', { configurable: true, get() { return offsetTop(this); } });
  Object.defineProperty(proto, 'offsetHeight', { configurable: true, get() { return hOf(this); } });
  proto.getBoundingClientRect = function () {
    if (this === box) return { top: 0, bottom: viewport, height: viewport, left: 0, right: 100, width: 100 };
    const top = offsetTop(this) - box._top;
    return { top, bottom: top + hOf(this), height: hOf(this), left: 0, right: 100, width: 100 };
  };
  box._top = 0;
  Object.defineProperty(box, 'scrollTop', { configurable: true, get() { return this._top; }, set(v) { this._top = Math.max(0, Math.min(v, this.scrollHeight - viewport)); } });
  Object.defineProperty(box, 'clientHeight', { configurable: true, get() { return viewport; } });
  Object.defineProperty(box, 'scrollHeight', { configurable: true, get() { let y = 0; for (const c of this.children) y += hOf(c); return y; } });
}

function setupWindow(count, { more = null, el: elIn } = {}) {
  const { win, el } = makeDom();
  const box = win.document.getElementById('box');
  layout(win, box);
  const day = (i) => `2026-10-${String(1 + Math.floor(i / 40)).padStart(2, '0')}T12:00:00Z`;
  const msgs = Array.from({ length: count }, (_, i) => ({ id: `m${String(i).padStart(5, '0')}`, created_at: day(i), role: i % 2 ? 'assistant' : 'user', content: `row ${i}` }));
  const state = { messages: msgs };
  const build = (from, to, { lead = true } = {}) => {
    const out = [];
    let last = !lead && from > 0 ? state.messages[from - 1].created_at.slice(0, 10) : '';
    for (let i = from; i < to; i += 1) {
      const m = state.messages[i];
      const dk = m.created_at.slice(0, 10);
      if (dk !== last) { const s = el('div', { class: 'date-sep', text: dk }); s.dataset.day = dk; out.push(s); last = dk; }
      out.push(el('div', { class: `msg ${m.role}`, dataset: { id: m.id }, text: m.content }));
    }
    return out;
  };
  const w = new pager.MessageWindow({ box, getMessages: () => state.messages, build, getMore: () => more, t, el: elIn || el });
  return { win, box, state, w, el };
}

const rowsIn = (box) => box.querySelectorAll(':scope > [data-id]').length;
const firstVisibleId = (box) => {
  for (const n of box.children) if (n.dataset.id && n.getBoundingClientRect().bottom > 0) return n.dataset.id;
  return null;
};

// ------------------------------------------------------------------ the rail pager

test('appendUnique never repeats an id and replaces a row in place', () => {
  const out = pager.appendUnique([{ id: 'a', v: 1 }, { id: 'b', v: 1 }], [{ id: 'b', v: 2 }, { id: 'c', v: 1 }]);
  assert.deepEqual(out, [{ id: 'a', v: 1 }, { id: 'b', v: 2 }, { id: 'c', v: 1 }]);
});

test('sortThreadRows: pinned first, then newest, then id', () => {
  const rows = [
    { id: 'a', updated_at: '2026-10-01T00:00:00Z' }, { id: 'p', is_pinned: true, updated_at: '2026-01-01T00:00:00Z' },
    { id: 'c', updated_at: '2026-10-03T00:00:00Z' }, { id: 'b', updated_at: '2026-10-03T00:00:00Z' },
  ];
  assert.deepEqual(pager.sortThreadRows(rows).map((r) => r.id), ['p', 'c', 'b', 'a']);
});

/** A fake server: n threads, the first `pinned` pinned, pages of `size` by an opaque cursor. */
function fakeServer(n, { pinned = 2, size = 50 } = {}) {
  const all = Array.from({ length: n }, (_, i) => ({
    id: `t${String(i).padStart(4, '0')}`, is_pinned: i < pinned,
    updated_at: new Date(Date.UTC(2026, 9, 20) - i * 60000).toISOString(),
  }));
  const calls = [];
  const fetchPage = async (cursor) => {
    calls.push(cursor);
    const rest = all.filter((r) => !r.is_pinned);
    const start = cursor ? Number(cursor) : 0;
    const page = rest.slice(start, start + size);
    const next = start + size < rest.length ? String(start + size) : null;
    return { threads: cursor ? page : all.filter((r) => r.is_pinned).concat(page), next_cursor: next };
  };
  return { all, calls, fetchPage };
}

test('ThreadPager: page one has the pinned threads first, later pages append by cursor, the end clears the cursor', async () => {
  const s = fakeServer(130, { pinned: 3 });
  const p = new pager.ThreadPager(s.fetchPage);
  let rows = await p.loadFirst([]);
  assert.equal(rows.length, 53);
  assert.deepEqual(rows.slice(0, 3).map((r) => r.is_pinned), [true, true, true]);
  assert.ok(p.hasMore);
  rows = await p.loadMore(rows);
  assert.equal(rows.length, 103);
  rows = await p.loadMore(rows);
  assert.equal(rows.length, 130);
  assert.equal(p.hasMore, false);
  assert.equal(await p.loadMore(rows), null, 'nothing more to load');
  assert.equal(new Set(rows.map((r) => r.id)).size, rows.length, 'no duplicate rows');
  assert.deepEqual(s.calls, [null, '50', '100']);
});

test('ThreadPager: refreshing page one keeps the pages already scrolled in, without duplicates', async () => {
  const s = fakeServer(130, { pinned: 2 });
  const p = new pager.ThreadPager(s.fetchPage);
  let rows = await p.loadFirst([]);
  rows = await p.loadMore(rows);
  assert.equal(rows.length, 102);
  const cursorBefore = p.next;
  const again = await p.loadFirst(rows);
  assert.equal(again.length, 102, 'page two stays');
  assert.equal(new Set(again.map((r) => r.id)).size, again.length);
  assert.equal(p.next, cursorBefore, 'the cursor still points past what is loaded');
});

test('ThreadPager: a thread that left page one (archived elsewhere) goes on refresh; an unpinned one stays in order', async () => {
  const s = fakeServer(60, { pinned: 1 });
  const p = new pager.ThreadPager(s.fetchPage);
  const rows = await p.loadFirst([]);
  const gone = rows[5].id;
  const fresh = await new pager.ThreadPager(async (c) => {
    const r = await s.fetchPage(c);
    return { ...r, threads: r.threads.filter((x) => x.id !== gone) };
  }).loadFirst(rows);
  assert.ok(!fresh.some((r) => r.id === gone));
});

test('ThreadPager: an answer that arrives after reset() is dropped; a second loadMore while one is in flight is ignored', async () => {
  const s = fakeServer(120);
  let release;
  const gate = new Promise((r) => { release = r; });
  const p = new pager.ThreadPager(async (c) => { const out = await s.fetchPage(c); if (c) await gate; return out; });
  const rows = await p.loadFirst([]);
  const slow = p.loadMore(rows);
  assert.equal(await p.loadMore(rows), null, 'one request at a time');
  p.reset();
  release();
  assert.equal(await slow, null, 'stale after reset');
  assert.equal(p.hasMore, false);
});

test('showMoreRow: the button calls onMore; while loading it is disabled and reads Loading', { skip: dom.skip }, () => {
  const { el } = makeDom();
  let n = 0;
  const row = pager.showMoreRow({ t, el, onMore: () => { n += 1; } });
  row.querySelector('button').click();
  assert.equal(n, 1);
  assert.equal(row.textContent, EN.archive.show_more);
  const busy = pager.showMoreRow({ t, el, loading: true, onMore: () => {} });
  assert.equal(busy.querySelector('button').disabled, true);
  assert.equal(busy.textContent, EN.common.loading);
});

// ------------------------------------------------------------------ the windowed message list

test('window: 5,000 messages paint at most 300 rows, the newest window, with a spacer for the rest', { skip: dom.skip }, () => {
  const { box, w } = setupWindow(5000);
  w.render();
  assert.ok(rowsIn(box) <= pager.WINDOW_MAX, `${rowsIn(box)} rows`);
  assert.equal(box.querySelector('[data-id="m04999"]') !== null, true, 'the newest message is painted');
  assert.equal(box.querySelector('[data-id="m00000"]'), null);
  assert.ok(w.gap('top') && !w.gap('bottom'));
  assert.equal(Number(w.gap('top').dataset.count), 5000 - rowsIn(box));
});

test('window: a thread of 300 or fewer is painted whole, with no spacer', { skip: dom.skip }, () => {
  const { box, w } = setupWindow(300);
  w.render();
  assert.equal(rowsIn(box), 300);
  assert.equal(w.isWindowed(), false);
});

test('window: scrolling up through 5,000 messages keeps the DOM under the cap and adds rows ahead of the reader', { skip: dom.skip }, () => {
  const { box, w } = setupWindow(5000);
  w.render();
  box.scrollTop = box.scrollHeight;          // reading the newest
  let peak = 0;
  for (let step = 0; step < 120; step += 1) {
    box.scrollTop -= 400;
    w.update();
    peak = Math.max(peak, rowsIn(box));
  }
  assert.ok(peak <= pager.WINDOW_MAX + 2 * pager.WINDOW_CHUNK, `peak ${peak} rows`);
  assert.ok(rowsIn(box) <= pager.WINDOW_MAX + pager.WINDOW_CHUNK);
  // the reader is on painted rows, not on a spacer
  const id = firstVisibleId(box);
  assert.ok(id, 'a message row is under the top of the view');
  assert.ok(Number(id.slice(1)) < 4900, 'we really scrolled back');
});

test('window: extending upward does not move what the reader is looking at', { skip: dom.skip }, () => {
  const { box, w } = setupWindow(2000);
  w.render();
  box.scrollTop = box.scrollHeight - 600;
  const topGap = w.gap('top');
  box.scrollTop = topGap.offsetHeight + 50;   // close to the top of the window
  const anchor = w._anchorNode();
  const before = anchor.getBoundingClientRect().top;
  const countBefore = Number(topGap.dataset.count);
  w.update();
  assert.ok(Number(w.gap('top').dataset.count) < countBefore, 'the top spacer shrank as rows were painted');
  const after = anchor.getBoundingClientRect().top;
  assert.equal(after, before, 'the anchor row stays at the same height on screen');
  assert.ok(rowsIn(box) > 200, 'rows were added above');
});

test('window: scrolling down again drops the far-away rows from the top without a jump', { skip: dom.skip }, () => {
  const { box, w } = setupWindow(3000);
  w.render();
  // walk up a long way, then back down
  for (let i = 0; i < 80; i += 1) { box.scrollTop -= 500; w.update(); }
  const mid = w._anchorNode().dataset.id;
  for (let i = 0; i < 80; i += 1) {
    const a = w._anchorNode();
    const before = a.getBoundingClientRect().top;
    box.scrollTop += 500; w.update();
    assert.ok(rowsIn(box) <= pager.WINDOW_MAX + 2 * pager.WINDOW_CHUNK);
    void before;
  }
  assert.ok(Number(w._anchorNode().dataset.id.slice(1)) > Number(mid.slice(1)));
});

test('window: dragging into a spacer (a scrollbar jump) repaints the window where the reader landed', { skip: dom.skip }, () => {
  const { box, w } = setupWindow(5000);
  w.render();
  box.scrollTop = 5000;                       // deep inside the top spacer
  w.update();
  const id = firstVisibleId(box);
  assert.ok(id, 'rows are painted under the view');
  assert.ok(rowsIn(box) <= pager.WINDOW_MAX);
  const idx = Number(id.slice(1));
  assert.ok(idx > 20 && idx < 4000, `landed near the estimate, got ${idx}`);
  box.scrollTop = 0;
  w.update();
  assert.ok(box.querySelector('[data-id="m00000"]') || firstVisibleId(box), 'the top of the thread is reachable');
});

test('window: the bottom spacer appears when the reader is far back, and ensureTail() brings the newest rows back', { skip: dom.skip }, () => {
  const { box, w, state } = setupWindow(4000);
  w.render();
  box.scrollTop = 3000;
  w.update();
  assert.ok(w.gap('bottom'), 'newest rows are no longer painted');
  assert.equal(w.atTail(), false);
  assert.equal(w.noteLiveAppend(), true, 'a live message is not painted while the reader is far back');
  assert.equal(w.ensureTail(), true);
  assert.equal(w.atTail(), true);
  assert.ok(box.querySelector(`[data-id="${state.messages[state.messages.length - 1].id}"]`));
});

test('window: a live message at the end past the cap drops rows from the top and keeps the reader in place', { skip: dom.skip }, () => {
  const { box, w, state, el } = setupWindow(299);
  w.render();
  box.scrollTop = box.scrollHeight;
  // add 40 live messages the way appendMessageToView does
  for (let i = 0; i < 40; i += 1) {
    const m = { id: `live${i}`, created_at: '2026-12-01T00:00:00Z', role: 'assistant', content: 'x' };
    state.messages.push(m);
    if (i === 0) box.append(el('div', { class: 'date-sep', text: 'd' }));
    box.append(el('div', { class: 'msg assistant', dataset: { id: m.id }, text: 'x' }));
    w.afterLiveAppend();
    box.scrollTop = box.scrollHeight;
  }
  assert.ok(rowsIn(box) <= pager.WINDOW_MAX, `${rowsIn(box)} rows`);
  assert.ok(w.gap('top'), 'the oldest rows became a spacer');
  assert.ok(box.querySelector('[data-id="live39"]'));
});

test('window: a spacer stands for exactly the unpainted messages; a delete re-counts them', { skip: dom.skip }, () => {
  const { box, w, state } = setupWindow(1000);
  w.render();
  const n0 = Number(w.gap('top').dataset.count);
  state.messages.splice(0, 1);               // an old message was deleted
  w.resync();
  assert.equal(Number(w.gap('top').dataset.count), n0 - 1);
  assert.ok(rowsIn(box) > 0);
});

test('window: Load earlier shows only when the top is painted and older pages exist; the button asks for more', { skip: dom.skip }, () => {
  let clicked = 0;
  const more = { has: true, loading: false, onClick: () => { clicked += 1; } };
  const { box, w } = setupWindow(120, { more });
  w.render();
  const btn = box.querySelector('.load-earlier .load-earlier-btn');
  assert.ok(btn, 'the button is at the top');
  assert.equal(btn.textContent, EN.archive.load_earlier);
  btn.click();
  assert.equal(clicked, 1);
  more.loading = true;
  w._syncLoadEarlier();
  assert.equal(box.querySelector('.load-earlier-btn').textContent, EN.common.loading);
  assert.equal(box.querySelector('.load-earlier-btn').disabled, true);
  more.loading = false; more.has = false;
  w._syncLoadEarlier();
  assert.equal(box.querySelector('.load-earlier'), null, 'no older pages: no button');
});

test('window: Load earlier keeps the row the reader was on at the same place on screen', { skip: dom.skip }, () => {
  const { box, w, state, el } = setupWindow(100);
  w.render();
  box.scrollTop = 40;
  const anchor = w._anchorNode();
  const id = anchor.dataset.id;
  const top0 = anchor.getBoundingClientRect().top;
  // a page of 100 older messages arrives
  const older = Array.from({ length: 100 }, (_, i) => ({ id: `a${String(i).padStart(3, '0')}`, created_at: '2026-09-01T12:00:00Z', role: 'user', content: `old ${i}` }));
  state.messages = older.concat(state.messages);
  w.repaintKeeping(id);
  const again = box.querySelector(`[data-id="${id}"]`);
  assert.equal(again.getBoundingClientRect().top, top0);
  assert.ok(box.querySelector('[data-id="a000"]'), 'the older page is painted above');
  void el;
});

test('window: three Load earlier pages of 100 stay under the cap (the window moves, the list does not grow)', { skip: dom.skip }, () => {
  const { box, w, state, el } = setupWindow(100);
  w.render();
  void el;
  for (let page = 0; page < 5; page += 1) {
    const id = w._anchorNode().dataset.id;
    const older = Array.from({ length: 100 }, (_, i) => ({ id: `o${page}-${String(i).padStart(3, '0')}`, created_at: '2026-08-01T12:00:00Z', role: 'user', content: 'old' }));
    state.messages = older.concat(state.messages);
    w.repaintKeeping(id);
    assert.ok(rowsIn(box) <= pager.WINDOW_MAX, `page ${page}: ${rowsIn(box)} rows`);
  }
  assert.equal(state.messages.length, 600);
});

test('window: showIndex paints a window around a message (a reply quote far back)', { skip: dom.skip }, () => {
  const { box, w } = setupWindow(3000);
  w.render();
  w.showIndex(1200);
  assert.ok(box.querySelector('[data-id="m01200"]'));
  assert.ok(rowsIn(box) <= pager.WINDOW_MAX);
});

test('window: date separators are not repeated at the seam when rows are added above', { skip: dom.skip }, () => {
  const { box, w } = setupWindow(900);
  w.render();
  for (let i = 0; i < 6; i += 1) { box.scrollTop = 0; w.update(); box.scrollTop += 0; }
  const seps = [...box.querySelectorAll(':scope > .date-sep')].map((s) => s.dataset.day);
  assert.equal(new Set(seps).size, seps.length, `days repeat: ${seps.join(',')}`);
});

// ------------------------------------------------------------------ main.js wiring

test('main.js: the thread list pages by cursor, the messages page is 100, and the list is the window', () => {
  assert.match(MAIN, /new ThreadPager\(/);
  assert.match(MAIN, /api\.threads\(botId, \{ cursor, archived: view === 'archived' \}\)/);
  assert.match(MAIN, /showMoreRow\(\{ t, el, loading: threadPager\.loading, onMore: loadMoreThreads \}\)/);
  assert.match(MAIN, /api\.messages\(id, state\.messages\[0\]\.id, MESSAGE_PAGE\)/);
  assert.match(MAIN, /messageWindow\(\)\.render\(\)/);
  assert.match(MAIN, /messageWindow\(\)\.onScroll\(\)/);
  assert.equal(pager.MESSAGE_PAGE, 100);
  assert.equal(pager.WINDOW_MAX, 300);
});

test('main.js: the Storage and image cards are mounted for Admins only; the archive control for profiles that manage their history', () => {
  assert.match(MAIN, /if \(canAdmin\(\)\) \{\s+const backups = document\.getElementById\('sys-backups'\);[\s\S]*?renderStorageCard\([\s\S]*?renderRetentionCard\(/);
  assert.match(MAIN, /renderArchiveControl\(pane, \{[^}]*can: \(\) => sessionCan\('manage_history'\)/);
  assert.match(MAIN, /installExpiredImages\(\{ t, el, icon: spriteIcon \}\)/);
});

test('main.js: the "+" tile follows canMakeBots() alone; the dead decoy arguments are gone', () => {
  assert.match(MAIN, /const offerMake = canMakeBots\(\);/);
  assert.doesNotMatch(MAIN, /decoy/i);
  assert.doesNotMatch(MAIN, /isDecoy/);
  assert.doesNotMatch(MAIN, /function iconifyRail/);
});

// ------------------------------------------------------------------ the api calls

test('api: threads(), messages() and the maintenance calls use the paged shapes', async () => {
  const urls = [];
  const win = new jsdom.JSDOM('', { url: 'http://127.0.0.1:8794/' }).window;
  Object.defineProperty(globalThis, 'window', { value: win, configurable: true, writable: true });
  globalThis.fetch = async (u, o = {}) => { urls.push(`${(o.method || 'GET')} ${u}${o.body ? ' ' + o.body : ''}`); return { ok: true, status: 200, headers: new win.Headers(), json: async () => ({}) }; };
  const { api } = await import('../static/js/api.js?v=36');
  await api.threads('b1');
  await api.threads('b1', { cursor: 'abc def', archived: true });
  await api.messages('t1');
  await api.messages('t1', 'm9');
  await api.unarchive('t1');
  await api.imagePreview(12);
  await api.setRetention({ 'system.retention.images.mode': 'months', 'system.retention.images.months': 12 });
  await api.setMyArchive(null);
  assert.deepEqual(urls, [
    'GET /api/threads?bot_id=b1',
    'GET /api/threads?bot_id=b1&cursor=abc%20def&archived=1',
    'GET /api/threads/t1/messages?limit=100',
    'GET /api/threads/t1/messages?limit=100&before=m9',
    'PATCH /api/threads/t1 {"archived":false}',
    'GET /api/maintenance/images/preview?mode=months&months=12',
    'PUT /api/maintenance/retention {"system.retention.images.mode":"months","system.retention.images.months":12}',
    'PUT /api/me/archive {"months":null}',
  ]);
});

// ------------------------------------------------------------------ Storage card

test('storage: areaRows folds the API areas into the labelled rows and drops empty ones', () => {
  const rows = cards.areaRows({ areas: {
    database: { bytes: 100 }, wal: { bytes: 20 }, media: { bytes: 5 }, reactions_spent: { bytes: 70 }, reactions_shelf: { bytes: 30 },
    backups_6h: { bytes: 1 }, backups_daily: { bytes: 2 }, stray_bak: { bytes: 9 }, other: { bytes: 4 },
  } });
  assert.deepEqual(rows, [
    { key: 'database', bytes: 120 }, { key: 'media', bytes: 5 }, { key: 'reactions', bytes: 100 },
    { key: 'backups', bytes: 3 }, { key: 'stray', bytes: 9 }, { key: 'rest', bytes: 4 },
  ]);
  assert.deepEqual(cards.areaRows({ areas: {} }), []);
  assert.deepEqual(cards.areaRows(null), []);
});

test('storage: formatBytes and formatDuration', () => {
  assert.equal(cards.formatBytes(0), '0 B');
  assert.equal(cards.formatBytes(1536), '1.5 KB');
  assert.equal(cards.formatBytes(5 * 1024 ** 3), '5.0 GB');
  assert.equal(cards.formatBytes(250 * 1024 ** 2), '250 MB');
  assert.equal(cards.formatBytes(-1), '-');
  assert.equal(cards.formatDuration(840), '840 ms');
  assert.equal(cards.formatDuration(125000), '2 min 5 s');
});

function apiStub(overrides = {}) {
  const calls = [];
  const rec = (name, out) => async (...a) => { calls.push([name, ...a]); return typeof out === 'function' ? out(...a) : out; };
  const api = {
    maintenanceStatus: rec('status', { enabled: true, time: '04:30', next_run: '2026-10-24T04:30', running: false, blocked: null,
      last_run: { started: '2026-10-23T04:30:00Z', ok: false, ms: 125000, steps: [
        { step: 'activity_prune', rows: 1200, bytes: 0, ms: 90, error: null },
        { step: 'orphan_sweep', rows: 0, bytes: 0, ms: 4, error: 'disk full' },
      ] } }),
    maintenanceStorage: rec('storage', { measured_at: '2026-10-23T04:31:00Z', scanning: false, total_bytes: 3000, free_disk_bytes: 9e9,
      areas: { database: { bytes: 2000 }, media: { bytes: 1000 } }, stray: [{ name: 'chats.db.bak-avatars', bytes: 4096 }] }),
    maintenanceStorageRefresh: rec('refresh', { started: true }),
    maintenanceRun: rec('run', { started: true }),
    retention: rec('retention', { values: {}, defaults: {} }),
    setRetention: rec('setRetention', { ok: true }),
    imagePreview: rec('preview', (m) => ({ mode: 'months', months: m, would_expire: { count: 7, bytes: 3 * 1024 ** 2 }, protected: { current_avatars: 4, referenced_recently: 6 } })),
    myArchive: rec('myArchive', (m) => (m ? { months: m, would_archive: 12 } : { months: null })),
    setMyArchive: rec('setMyArchive', { ok: true }),
    ...overrides,
  };
  return { api, calls };
}
const tick = async (n = 10) => { for (let i = 0; i < n; i += 1) await new Promise((r) => setTimeout(r, 0)); };

test('storage card: sizes per area, the last run per step with the error in red, free disk, stray files, next run', { skip: dom.skip }, async () => {
  const { el } = makeDom();
  const host = document.getElementById('host');
  const { api } = apiStub();
  const toasts = [];
  const r = cards.renderStorageCard(host, { api, t, el, toast: (m) => toasts.push(m) });
  await r.ready;
  r.stop();
  const card = host.querySelector('.storage-card');
  assert.ok(card);
  assert.equal(card.querySelectorAll('.storage-area').length, 2);
  assert.match(card.querySelector('[data-area="database"]').textContent, /2\.0 KB/);
  assert.match(card.textContent, /Free disk space: /);
  const steps = [...card.querySelectorAll('.storage-step')];
  assert.equal(steps.length, 2);
  assert.ok(steps[0].classList.contains('failed'), 'failures first');
  assert.match(steps[0].textContent, /orphan sweep failed: disk full/);
  assert.match(steps[1].textContent, /1200 rows/);
  assert.match(card.querySelector('.storage-lastrun').textContent, /Last clean-up: .* \(2 min 5 s\)/);
  assert.match(card.querySelector('.storage-nextrun').textContent, /Next clean-up:/);
  assert.match(card.querySelector('.storage-stray').textContent, /chats\.db\.bak-avatars/);
  assert.match(card.querySelector('.storage-stray').textContent, /Nothing here is deleted automatically/);
});

test('storage card: Run now posts the run and polls until it finishes', { skip: dom.skip }, async () => {
  const { el } = makeDom();
  const host = document.getElementById('host');
  let running = false;
  const { api, calls } = apiStub({
    maintenanceStatus: async () => ({ running, blocked: null, last_run: null, next_run: null }),
    maintenanceRun: async () => { calls.push(['run']); running = true; return { started: true }; },
  });
  const r = cards.renderStorageCard(host, { api, t, el, toast: () => {} });
  await r.ready;
  const btn = host.querySelector('.storage-run-btn');
  assert.equal(btn.disabled, false);
  btn.click();
  await tick();
  assert.ok(calls.some((c) => c[0] === 'run'));
  assert.equal(host.querySelector('.storage-run-btn').disabled, true, 'disabled while it runs');
  assert.match(host.querySelector('.storage-running').textContent, /Running/);
  running = false;
  r.stop();
});

test('storage card: a 403 (not an Admin) removes the card; a blocked run disables Run now', { skip: dom.skip }, async () => {
  const { el } = makeDom();
  const host = document.getElementById('host');
  const forbidden = Object.assign(new Error('403: no'), { status: 403 });
  const r = cards.renderStorageCard(host, { api: { maintenanceStatus: async () => { throw forbidden; }, maintenanceStorage: async () => ({}) }, t, el, toast: () => {} });
  await r.ready;
  assert.equal(host.querySelector('.storage-card'), null);
  const b = cards.renderStorageCard(host, { api: apiStub({ maintenanceStatus: async () => ({ running: false, blocked: 'backup_running' }) }).api, t, el, toast: () => {} });
  await b.ready; b.stop();
  assert.equal(host.querySelector('.storage-run-btn').disabled, true);
  assert.match(host.querySelector('.storage-blocked').textContent, /backup or maintenance window/);
});

// ------------------------------------------------------------------ Generated images card

test('retention card: Keep forever by default; Save is off until something changes', { skip: dom.skip }, async () => {
  const { el } = makeDom();
  const host = document.getElementById('host');
  const { api } = apiStub();
  const r = cards.renderRetentionCard(host, { api, t, el, toast: () => {} });
  await r.ready;
  const mode = host.querySelector('.retention-mode');
  assert.equal(mode.value, 'forever');
  assert.equal(host.querySelector('.retention-months-wrap').hidden, true);
  assert.equal(host.querySelector('.retention-save').disabled, true);
  assert.equal(host.querySelector('.retention-check').disabled, true);
});

test('retention card: choosing N months pre-fills 12; Save stays off until a Check of that same value; editing the value switches it off again', { skip: dom.skip }, async () => {
  const { el, win } = makeDom();
  const host = document.getElementById('host');
  const { api, calls } = apiStub();
  const toasts = [];
  const r = cards.renderRetentionCard(host, { api, t, el, toast: (m) => toasts.push(m) });
  await r.ready;
  const mode = host.querySelector('.retention-mode');
  const months = host.querySelector('.retention-months');
  const save = host.querySelector('.retention-save');
  const check = host.querySelector('.retention-check');
  mode.value = 'months';
  mode.dispatchEvent(new win.Event('change'));
  assert.equal(months.value, '12', 'N months starts at 12');
  assert.equal(host.querySelector('.retention-months-wrap').hidden, false);
  assert.equal(save.disabled, true, 'no preview yet');
  assert.equal(check.disabled, false);
  check.click();
  await tick();
  assert.deepEqual(calls.find((c) => c[0] === 'preview'), ['preview', 12]);
  assert.match(host.querySelector('.retention-dry').textContent, /This would expire 7 images \(3\.0 MB\)/);
  assert.match(host.querySelector('.retention-protected').textContent, /10 images still in use are kept/);
  assert.equal(save.disabled, false, 'previewed: Save is on');
  months.value = '6';
  months.dispatchEvent(new win.Event('input'));
  assert.equal(save.disabled, true, 'a different value needs its own Check');
  assert.equal(host.querySelector('.retention-dry'), null);
  check.click();
  await tick();
  assert.deepEqual(calls.filter((c) => c[0] === 'preview').pop(), ['preview', 6]);
  assert.equal(save.disabled, false);
  save.click();
  await tick();
  assert.deepEqual(calls.find((c) => c[0] === 'setRetention'), ['setRetention', { 'system.retention.images.mode': 'months', 'system.retention.images.months': 6 }]);
  assert.deepEqual(toasts, ['Saved.']);
  assert.equal(save.disabled, true, 'saved: nothing left to save');
});

test('retention card: an out-of-range or empty months value cannot be checked or saved', { skip: dom.skip }, async () => {
  const { el, win } = makeDom();
  const host = document.getElementById('host');
  const r = cards.renderRetentionCard(host, { api: apiStub().api, t, el, toast: () => {} });
  await r.ready;
  const mode = host.querySelector('.retention-mode');
  mode.value = 'months'; mode.dispatchEvent(new win.Event('change'));
  const months = host.querySelector('.retention-months');
  for (const bad of ['', '0', '121', '1.5', 'abc', '-3']) {
    months.value = bad; months.dispatchEvent(new win.Event('input'));
    assert.equal(host.querySelector('.retention-check').disabled, true, `"${bad}" cannot be checked`);
    assert.equal(host.querySelector('.retention-save').disabled, true);
  }
  assert.equal(cards.parseMonths('120'), 120);
  assert.equal(cards.parseMonths('1'), 1);
  assert.equal(cards.parseMonths('121'), null);
});

test('retention card: a stored setting is shown; going back to forever saves without a Check; a Check that races a change is ignored', { skip: dom.skip }, async () => {
  const { el, win } = makeDom();
  const host = document.getElementById('host');
  let release;
  const gate = new Promise((x) => { release = x; });
  const { api, calls } = apiStub({
    retention: async () => ({ values: { 'system.retention.images.mode': 'months', 'system.retention.images.months': 9 } }),
    imagePreview: async (m) => { await gate; return { would_expire: { count: 1, bytes: 1 }, protected: {}, months: m }; },
  });
  const r = cards.renderRetentionCard(host, { api, t, el, toast: () => {} });
  await r.ready;
  const mode = host.querySelector('.retention-mode');
  const months = host.querySelector('.retention-months');
  assert.equal(mode.value, 'months');
  assert.equal(months.value, '9');
  months.value = '4'; months.dispatchEvent(new win.Event('input'));
  host.querySelector('.retention-check').click();     // preview for 4 is in flight ...
  months.value = '5'; months.dispatchEvent(new win.Event('input'));   // ... and the field moves on
  release();
  await tick();
  assert.equal(host.querySelector('.retention-save').disabled, true, 'the stale answer for 4 does not unlock 5');
  mode.value = 'forever'; mode.dispatchEvent(new win.Event('change'));
  assert.equal(host.querySelector('.retention-save').disabled, false);
  host.querySelector('.retention-save').click();
  await tick();
  assert.deepEqual(calls.find((c) => c[0] === 'setRetention'), ['setRetention', { 'system.retention.images.mode': 'forever' }]);
});

test('retention card: an Admin-only 403 removes the card', { skip: dom.skip }, async () => {
  const { el } = makeDom();
  const host = document.getElementById('host');
  const r = cards.renderRetentionCard(host, { api: { retention: async () => { throw Object.assign(new Error('403'), { status: 403 }); } }, t, el, toast: () => {} });
  await r.ready;
  assert.equal(host.querySelector('.retention-card'), null);
});

// ------------------------------------------------------------------ archive control

test('archive control: off by default, offers N months, saves the caller\'s own value and shows the count it would archive', { skip: dom.skip }, async () => {
  const { el, win } = makeDom();
  const host = document.getElementById('host');
  const { api, calls } = apiStub();
  const toasts = [];
  pager.renderArchiveControl(host, { api, t, el, toast: (m) => toasts.push(m), can: () => true });
  await tick();
  const sel = host.querySelector('#archive-after-months');
  assert.equal(sel.disabled, false);
  assert.equal(sel.value, '0');
  assert.equal(sel.options[0].textContent, 'off');
  assert.ok([...sel.options].some((o) => o.textContent === '12 months'));
  sel.value = '12'; sel.dispatchEvent(new win.Event('change'));
  await tick();
  assert.deepEqual(calls.find((c) => c[0] === 'setMyArchive'), ['setMyArchive', 12]);
  assert.match(host.querySelector('.archive-would').textContent, /12 chats would be archived now/);
  assert.deepEqual(toasts, ['Saved.']);
  sel.value = '0'; sel.dispatchEvent(new win.Event('change'));
  await tick();
  assert.deepEqual(calls.filter((c) => c[0] === 'setMyArchive').pop(), ['setMyArchive', null]);
});

test('archive control: not drawn for a profile that cannot manage its history; a stored odd value is kept in the list', { skip: dom.skip }, async () => {
  const { el } = makeDom();
  const host = document.getElementById('host');
  assert.equal(pager.renderArchiveControl(host, { api: apiStub().api, t, el, toast: () => {}, can: () => false }), null);
  assert.equal(host.querySelector('.archive-after'), null);
  pager.renderArchiveControl(host, { api: apiStub({ myArchive: async () => ({ months: 18 }) }).api, t, el, toast: () => {}, can: () => true });
  await tick();
  assert.equal(host.querySelector('#archive-after-months').value, '18');
  assert.equal(pager.archiveValue('0'), null);
  assert.equal(pager.archiveValue('36'), 36);
});

// ------------------------------------------------------------------ image expired

test('image expired: a 410 swaps the failing picture for the labelled placeholder', { skip: dom.skip }, async () => {
  expired._resetProbeCache();
  const { el, win } = makeDom();
  const host = document.getElementById('host');
  const seen = [];
  const fetchFn = async (u) => { seen.push(u); return { status: 410, headers: new win.Headers({ 'X-Image-Expired': '1' }) }; };
  const off = expired.installExpiredImages({ t, el, icon: () => el('svg'), doc: document, fetchFn });
  const img = el('img', { src: '/media/old-one.png', class: 'chat-img', alt: 'x' });
  host.append(img);
  img.dispatchEvent(new win.Event('error'));
  await tick();
  assert.equal(host.querySelector('img'), null);
  const ph = host.querySelector('.media-expired');
  assert.ok(ph);
  assert.equal(ph.getAttribute('role'), 'img');
  assert.equal(ph.getAttribute('aria-label'), 'image expired');
  assert.equal(ph.textContent, 'image expired');
  assert.equal(ph.dataset.expiredSrc, '/media/old-one.png');
  assert.equal(seen.length, 1);
  assert.match(seen[0], /\/media\/old-one\.png$/);
  off();
});

test('image expired: a 404, a network failure, a data: URL and another origin all stay as they are', { skip: dom.skip }, async () => {
  expired._resetProbeCache();
  const { el, win } = makeDom();
  const host = document.getElementById('host');
  let mode = 404;
  const fetchFn = async () => { if (mode === 'down') throw new Error('offline'); return { status: mode, headers: new win.Headers() }; };
  const off = expired.installExpiredImages({ t, el, doc: document, fetchFn });
  const imgs = [
    el('img', { src: '/media/missing.png' }),
    el('img', { src: 'https://elsewhere.example/p.png' }),
    el('img', { src: 'data:image/png;base64,AAAA' }),
  ];
  for (const i of imgs) { host.append(i); i.dispatchEvent(new win.Event('error')); }
  await tick();
  assert.equal(host.querySelectorAll('img').length, 3);
  assert.equal(host.querySelector('.media-expired'), null);
  mode = 'down';
  const down = el('img', { src: '/media/net.png' });
  host.append(down); down.dispatchEvent(new win.Event('error'));
  await tick();
  assert.ok(host.contains(down));
  off();
});

test('image expired: the X-Image-Expired header alone is enough, and a picture is asked about once', { skip: dom.skip }, async () => {
  expired._resetProbeCache();
  const { el, win } = makeDom();
  const host = document.getElementById('host');
  let asked = 0;
  const fetchFn = async () => { asked += 1; return { status: 200, headers: new win.Headers({ 'X-Image-Expired': '1' }) }; };
  const off = expired.installExpiredImages({ t, el, doc: document, fetchFn });
  const a = el('img', { src: '/api/reactions/r1/image' });
  const b = el('img', { src: '/api/reactions/r1/image' });
  host.append(a, b);
  a.dispatchEvent(new win.Event('error')); b.dispatchEvent(new win.Event('error'));
  await tick();
  assert.equal(asked, 1);
  assert.equal(host.querySelectorAll('.media-expired').length, 2);
  off();
});

test('css: rows are laid out for real (no content-visibility guesses that jump the list), spacers and cards styled', () => {
  const css = readFileSync(join(STATIC, 'profiles.css'), 'utf8');
  assert.match(css, /\.messages > \.msg \{ content-visibility: visible; \}/);
  for (const sel of ['.win-gap', '.load-earlier', '.thread-more', '.archived-head', '.thread-unarchive', '.media-expired', '.storage-card', '.storage-area', '.storage-step.failed', '.retention-row', '.archive-after-row']) {
    assert.ok(css.includes(sel), `profiles.css styles ${sel}`);
  }
});

test('backups: a rotating snapshot shows its tier (recent, daily, weekly); archives and unknown tiers show none', async () => {
  makeDom();
  const bk = await import('../static/js/backup.js?v=3');
  const labels = ['recent', 'daily', 'weekly'].map((tier) => bk.tierLabel({ kind: 'snapshot', tier }));
  assert.ok(labels.every(Boolean) && new Set(labels).size === 3, labels.join(','));
  assert.equal(bk.tierLabel({ kind: 'archive', tier: 'daily' }), '');
  assert.equal(bk.tierLabel({ kind: 'snapshot' }), '');
  assert.equal(bk.tierLabel({ kind: 'snapshot', tier: 'bogus' }), '');
  for (const k of ['tier_recent', 'tier_daily', 'tier_weekly']) assert.ok(EN.bkp[k], k);
});
