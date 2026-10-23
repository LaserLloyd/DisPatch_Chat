// Paging for long lists, 4.0 scale round (design target: 20 profiles x 3 years, 1-2 million messages, no slowdowns).
// slowdowns"; design target 20 profiles x 3 years, 1-2 million messages).
//
//   * ThreadPager      the rail: one page of threads at a time by `next_cursor`
//                      (GET /api/threads?bot_id=&limit=&cursor=&archived=), page one has
//                      the pinned threads, a refresh never duplicates a row.
//   * MessageWindow    a long thread: the DOM holds at most about WINDOW_MAX message rows.
//                      Rows far from the viewport are replaced by one spacer of estimated
//                      height at each end and come back as the person scrolls; the row the
//                      person is reading never moves.
//   * the small DOM bits (Show more, Load earlier) and the "Archive my chats" control.
//
// Nothing here knows main.js. main.js hands in what is needed (fetchers, node builders,
// translator) and owns state.threads / state.messages. No work at import time.

export const THREAD_PAGE = 50;
export const MESSAGE_PAGE = 100;
/** Above this many message rows in the DOM the list is windowed. */
export const WINDOW_MAX = 300;
/** Rows added or dropped per step as the person scrolls. */
export const WINDOW_CHUNK = 50;
/** Rows kept when the window is rebuilt around a point (below WINDOW_MAX with room to grow). */
export const WINDOW_TARGET = 200;

// ------------------------------------------------------------------ thread rows

/** Pinned first, then newest first, then id (the server's keyset order). */
export function sortThreadRows(rows) {
  return rows.slice().sort((a, b) =>
    (b.is_pinned ? 1 : 0) - (a.is_pinned ? 1 : 0)
    || String(b.updated_at || '').localeCompare(String(a.updated_at || ''))
    || String(b.id).localeCompare(String(a.id)));
}

/** Add rows to a list without ever repeating an id; a row already there is replaced in place. */
export function appendUnique(current, incoming) {
  const out = current.slice();
  const at = new Map(out.map((r, i) => [r.id, i]));
  for (const r of incoming || []) {
    if (at.has(r.id)) out[at.get(r.id)] = r;
    else { at.set(r.id, out.length); out.push(r); }
  }
  return out;
}

/** The list after page one was fetched again (a reconnect, a bot switch back).
 *  `current` may hold later pages already; those older rows stay, anything newer that page
 *  one no longer lists was deleted, archived or unpinned, and goes. `fresh.next` null means
 *  page one was the whole list. Returns {rows, next}. */
export function mergeFirstPage(current, fresh, freshNext, keptNext) {
  if (!freshNext) return { rows: sortThreadRows(fresh), next: null };
  const freshIds = new Set(fresh.map((r) => r.id));
  // The oldest non-pinned row page one reached: whatever is older than it belongs to a later page.
  let edge = null;
  for (const r of fresh) if (!r.is_pinned && (edge === null || String(r.updated_at || '') < edge)) edge = String(r.updated_at || '');
  const older = (current || []).filter((r) => !freshIds.has(r.id) && !r.is_pinned
    && edge !== null && String(r.updated_at || '') < edge);
  if (!older.length) return { rows: sortThreadRows(fresh), next: freshNext };
  return { rows: sortThreadRows(fresh.concat(older)), next: keptNext || freshNext };
}

/** A pager over `fetchPage(cursor) -> {threads, next_cursor}`. It holds the cursor, the busy
 *  flag and a generation, not the rows: callers pass the current rows in and take the merged
 *  rows back, or null when the answer arrived stale (the person switched bot or view). */
export class ThreadPager {
  constructor(fetchPage) {
    this.fetchPage = fetchPage;
    this.next = null;
    this.loading = false;
    this.gen = 0;
  }

  /** Forget the cursor and drop any answer still in flight. */
  reset() { this.gen += 1; this.next = null; this.loading = false; }

  get hasMore() { return !!this.next; }

  /** Page one. `current` = the rows now shown (kept where older than page one). */
  async loadFirst(current = []) {
    const gen = ++this.gen;
    this.loading = true;
    try {
      const r = await this.fetchPage(null);
      if (gen !== this.gen) return null;
      const merged = mergeFirstPage(current, r.threads || [], r.next_cursor || null, this.next);
      this.next = merged.next;
      return merged.rows;
    } finally {
      if (gen === this.gen) this.loading = false;
    }
  }

  /** The next page appended to `current`. Null when there is none, one is already loading, or
   *  the answer is stale. */
  async loadMore(current = []) {
    if (!this.next || this.loading) return null;
    const gen = this.gen;
    this.loading = true;
    try {
      const r = await this.fetchPage(this.next);
      if (gen !== this.gen) return null;
      this.next = r.next_cursor || null;
      return appendUnique(current, r.threads || []);
    } finally {
      if (gen === this.gen) this.loading = false;
    }
  }
}

/** The "Show more" row at the end of a paged list. `onMore` is called by the button and, when
 *  the row scrolls into view of `root`, automatically. Returns the node. */
export function showMoreRow({ t, el, root = null, loading = false, onMore }) {
  const btn = el('button', {
    type: 'button', class: 'ui-btn thread-more-btn', disabled: loading ? 'disabled' : null,
    text: loading ? t('common.loading') : t('archive.show_more'),
    onclick: () => onMore(),
  });
  const row = el('div', { class: 'thread-more' }, [btn]);
  if (!loading && typeof IntersectionObserver === 'function') {
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { io.disconnect(); onMore(); }
    }, { root, rootMargin: '0px 0px 240px 0px' });
    io.observe(row);
    row._io = io;
  }
  return row;
}

// ------------------------------------------------------------------ message window

/** A node's message id, or ''. Rows the window cares about carry data-id. */
const idOf = (n) => (n && n.dataset && n.dataset.id) || '';

/** Windowing for the message list `box`.
 *
 *    getMessages()          the full array (oldest first)
 *    build(from, to)        the DOM nodes (date separators included) for messages[from, to)
 *    getMore()              {has: bool, loading: bool, onClick: fn} for the "Load earlier" row, or null
 *    t, el                  translator and element factory (only for the Load earlier row)
 *    measure(node)          optional; height of a node (tests; default getBoundingClientRect)
 *
 *  The box holds, in order: [Load earlier] [top spacer] message rows ... [bottom spacer]
 *  followed by whatever main.js appends (the typing bubble, a streaming bubble). Which messages
 *  are in the DOM is always read back from the DOM (first and last [data-id] row), so a delete
 *  or an edit elsewhere cannot desync a second copy of that state. */
export class MessageWindow {
  constructor({ box, getMessages, build, getMore = () => null, t = (k) => k, el = null, measure = null,
    max = WINDOW_MAX, chunk = WINDOW_CHUNK, target = WINDOW_TARGET }) {
    this.box = box;
    this.getMessages = getMessages;
    this.build = build;
    this.getMore = getMore;
    this.t = t;
    this.el = el;
    this.measure = measure;
    this.max = max;
    this.chunk = chunk;
    this.target = target;
    this.avg = 96;             // px per message, learned from what has been rendered
    this._idx = null;          // id -> index cache
    this._busy = false;
    this._raf = 0;
  }

  // ---- reading the DOM

  indexMap() {
    const msgs = this.getMessages();
    const c = this._idx;
    if (c && c.arr === msgs && c.len === msgs.length && c.first === (msgs[0] && msgs[0].id) && c.last === (msgs.length && msgs[msgs.length - 1].id)) return c.map;
    const map = new Map();
    for (let i = 0; i < msgs.length; i += 1) map.set(msgs[i].id, i);
    this._idx = { arr: msgs, len: msgs.length, first: msgs[0] && msgs[0].id, last: msgs.length ? msgs[msgs.length - 1].id : undefined, map };
    return map;
  }

  /** Message rows in the DOM that are in state (a streaming bubble or a cached row is not). */
  rows() {
    const map = this.indexMap();
    const out = [];
    for (const n of this.box.children) {
      const id = idOf(n);
      if (id && map.has(id)) out.push(n);
    }
    return out;
  }

  /** {a, b, rows}: the DOM holds messages[a, b); null when no message row is painted. */
  bounds() {
    const rows = this.rows();
    if (!rows.length) return null;
    const map = this.indexMap();
    return { a: map.get(idOf(rows[0])), b: map.get(idOf(rows[rows.length - 1])) + 1, rows };
  }

  gap(side) { return this.box.querySelector(`:scope > .win-gap[data-side="${side}"]`); }

  isWindowed() { return !!(this.gap('top') || this.gap('bottom')); }

  /** True when the newest message is painted (no bottom spacer). */
  atTail() { return !this.gap('bottom'); }

  h(node) {
    if (this.measure) return this.measure(node);
    return node.getBoundingClientRect().height;
  }

  viewH() { return this.box.clientHeight || 600; }

  // ---- drawing

  _setGap(side, count) {
    let g = this.gap(side);
    if (count <= 0) { if (g) g.remove(); return; }
    if (!g) {
      g = (this.el ? this.el('div', { class: 'win-gap', 'aria-hidden': 'true' }) : this.box.ownerDocument.createElement('div'));
      g.className = 'win-gap';
      g.setAttribute('aria-hidden', 'true');
      g.dataset.side = side;
    }
    g.dataset.count = String(count);
    g.style.height = `${Math.round(count * this.avg)}px`;
    if (side === 'top') {
      const first = this.box.firstChild;
      if (first !== g) this.box.insertBefore(g, first);
    } else if (g.parentNode !== this.box) {
      const rows = this.rows();
      const last = rows[rows.length - 1];
      let after = last ? last.nextSibling : null;
      // keep it right behind the last message row, before the typing bubble and the like
      this.box.insertBefore(g, after);
    }
  }

  _syncLoadEarlier() {
    const old = this.box.querySelector(':scope > .load-earlier');
    const b = this.bounds();
    const more = this.getMore();
    const want = !!(more && more.has && b && b.a === 0 && this.el);
    if (!want) { if (old) old.remove(); return; }
    const btn = this.el('button', {
      type: 'button', class: 'ui-btn load-earlier-btn', disabled: more.loading ? 'disabled' : null,
      text: more.loading ? this.t('common.loading') : this.t('archive.load_earlier'),
      onclick: () => more.onClick(),
    });
    const row = this.el('div', { class: 'load-earlier' }, [btn]);
    if (old) old.replaceWith(row); else this.box.insertBefore(row, this.box.firstChild);
  }

  _learnAvg() {
    const rows = this.rows();
    if (rows.length < 8) return;
    const first = rows[0];
    const last = rows[rows.length - 1];
    let span;
    if (this.measure) {
      span = rows.reduce((s, n) => s + this.measure(n), 0);
    } else {
      span = last.getBoundingClientRect().bottom - first.getBoundingClientRect().top;
    }
    const per = span / rows.length;
    if (Number.isFinite(per) && per >= 24 && per <= 800) this.avg = this.avg * 0.5 + per * 0.5;
  }

  /** Remove every message row, date separator and spacer, keeping what main.js put in the box
   *  that is not part of the list (typing bubble, streaming bubble, welcome card, optimistic send). */
  _clearList() {
    const map = this.indexMap();
    for (const n of [...this.box.children]) {
      const id = idOf(n);
      if ((id && map.has(id)) || n.classList.contains('date-sep') || n.classList.contains('win-gap')
        || n.classList.contains('load-earlier') || n.classList.contains('skeleton-wrap')) n.remove();
    }
  }

  _anchorNode() {
    const rows = this.rows();
    if (!rows.length) return null;
    const top = this.box.getBoundingClientRect().top;
    // binary search for the first row that ends below the box's top edge
    let lo = 0; let hi = rows.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (rows[mid].getBoundingClientRect().bottom > top) hi = mid; else lo = mid + 1;
    }
    return rows[lo];
  }

  /** Run `fn` (which inserts or removes rows ABOVE the reader) without moving what is on screen. */
  _keepAnchor(fn) {
    const anchor = this._anchorNode();
    if (!anchor) { fn(); return; }
    const before = anchor.getBoundingClientRect().top;
    fn();
    if (!anchor.isConnected) return;
    const delta = anchor.getBoundingClientRect().top - before;
    if (delta) this._scrollBy(delta);
  }

  _scrollBy(px) {
    const b = this.box;
    const prev = b.style.scrollBehavior;
    b.style.scrollBehavior = 'auto';
    b.scrollTop += px;
    b.style.scrollBehavior = prev;
  }

  /** Paint messages[a, b) with spacers for the rest. Replaces the list; the caller sets the scroll. */
  paint(a, b) {
    const msgs = this.getMessages();
    a = Math.max(0, a); b = Math.min(msgs.length, b);
    this._clearList();
    const nodes = this.build(a, b, { lead: true });
    const frag = this.box.ownerDocument.createDocumentFragment();
    for (const n of nodes) frag.append(n);
    // before the typing bubble / streaming bubble if there is one
    const tail = [...this.box.children].find((n) => n.classList.contains('typing') || n.classList.contains('streaming')
      || n.dataset.cmid !== undefined);
    this.box.insertBefore(frag, tail || null);
    this._setGap('top', a);
    this._setGap('bottom', msgs.length - b);
    this._learnAvg();
    this._setGap('top', a);
    this._setGap('bottom', msgs.length - b);
    this._syncLoadEarlier();
    return { a, b };
  }

  /** The thread was (re)loaded: paint all of it, or the newest window of it. Caller scrolls. */
  render() {
    const n = this.getMessages().length;
    if (n <= this.max) return this.paint(0, n);
    return this.paint(n - this.target, n);
  }

  /** Make sure the newest messages are painted (scroll-to-bottom, a live message when the
   *  reader is at the end). Returns true when it had to repaint. */
  ensureTail() {
    if (this.atTail()) return false;
    const n = this.getMessages().length;
    this.paint(n - this.target, n);
    return true;
  }

  /** Repaint around message `index`, keeping the row `id` at the same height on screen. Used after
   *  older messages were prepended (indices shifted) or the list was edited. */
  repaintKeeping(anchorId = null) {
    const n = this.getMessages().length;
    const anchor = anchorId ? this.rows().find((r) => idOf(r) === anchorId) : this._anchorNode();
    const id = anchor ? idOf(anchor) : '';
    const top = anchor ? anchor.getBoundingClientRect().top : 0;
    const idx = id ? (this.indexMap().get(id) ?? n - 1) : n - 1;
    if (n <= this.max) this.paint(0, n);
    else {
      const a = Math.max(0, Math.min(n - this.target, idx - Math.floor(this.target / 2)));
      this.paint(a, a + this.target);
    }
    if (!id) return;
    const again = this.rows().find((r) => idOf(r) === id);
    if (again) this._scrollBy(again.getBoundingClientRect().top - top);
  }

  // ---- scrolling

  /** Call from the box's scroll event (cheap; the work runs once per frame). */
  onScroll() {
    if (this._raf) return;
    const run = () => { this._raf = 0; this.update(); };
    this._raf = (typeof requestAnimationFrame === 'function') ? requestAnimationFrame(run) : setTimeout(run, 16);
  }

  /** Extend the window toward the reader, drop rows far behind. */
  update() {
    if (this._busy || !this.isWindowed()) return;
    this._busy = true;
    try {
      const box = this.box;
      const vh = this.viewH();
      const b0 = this.bounds();
      if (!b0) return;
      const topGap = this.gap('top');
      const botGap = this.gap('bottom');
      const top = box.scrollTop;
      const bottom = top + vh;
      // 1. The reader dragged into a spacer: rebuild the window where they landed.
      if (topGap && bottom <= topGap.offsetTop + topGap.offsetHeight) {
        const idx = Math.max(0, Math.floor((top - topGap.offsetTop) / this.avg));
        this._jump(idx);
        return;
      }
      if (botGap && top >= botGap.offsetTop) {
        const idx = b0.b + Math.floor((top - botGap.offsetTop) / this.avg);
        this._jump(idx);
        return;
      }
      // 2. Near an end of the window: add a chunk there.
      let grew = false;
      if (topGap && top - (topGap.offsetTop + topGap.offsetHeight) < vh * 1.5) {
        this._more('up'); grew = true;
      }
      if (botGap && botGap.offsetTop - bottom < vh * 1.5) {
        this._more('down'); grew = true;
      }
      if (grew) this._trim();
    } finally {
      this._busy = false;
    }
  }

  _jump(idx) {
    const n = this.getMessages().length;
    const a = Math.max(0, Math.min(n - this.target, idx - Math.floor(this.target / 3)));
    const keepTop = this.box.scrollTop;
    this.paint(a, a + this.target);
    this.box.scrollTop = keepTop;
  }

  _more(dir) {
    const bd = this.bounds();
    if (!bd) return;
    const msgs = this.getMessages();
    if (dir === 'up') {
      const a2 = Math.max(0, bd.a - this.chunk);
      if (a2 === bd.a) return;
      this._keepAnchor(() => {
        const nodes = this.build(a2, bd.a, { lead: true });
        const frag = this.box.ownerDocument.createDocumentFragment();
        for (const n of nodes) frag.append(n);
        const first = bd.rows[0];
        // the first painted row may sit behind its own date separator: new rows go before that
        let at = first;
        while (at.previousElementSibling && at.previousElementSibling.classList.contains('date-sep')) at = at.previousElementSibling;
        // the old top separator repeats the day the new rows end on: drop it
        const oldSep = at !== first ? at : null;
        const seps = nodes.filter((n) => n.classList && n.classList.contains('date-sep'));
        const lastSep = seps[seps.length - 1];
        if (oldSep && lastSep && oldSep.dataset.day && oldSep.dataset.day === lastSep.dataset.day) {
          const nx = oldSep.nextSibling;
          oldSep.remove();
          at = nx;
        }
        this.box.insertBefore(frag, at);
        this._setGap('top', a2);
        this._syncLoadEarlier();
      });
    } else {
      const b2 = Math.min(msgs.length, bd.b + this.chunk);
      if (b2 === bd.b) return;
      const nodes = this.build(bd.b, b2, { lead: false });
      const frag = this.box.ownerDocument.createDocumentFragment();
      for (const n of nodes) frag.append(n);
      const g = this.gap('bottom');
      this.box.insertBefore(frag, g);
      this._setGap('bottom', msgs.length - b2);
    }
    this._learnAvg();
  }

  /** Over the cap: drop rows from the end the reader is not near. */
  _trim() {
    const bd = this.bounds();
    if (!bd || bd.rows.length <= this.max) return;
    const over = bd.rows.length - this.max;
    const vh = this.viewH();
    const top = this.box.scrollTop;
    const bottom = top + vh;
    const topGap = this.gap('top');
    const botGap = this.gap('bottom');
    // nearer to the top edge of the window? then the reader is up there: drop from the bottom.
    const startY = topGap ? topGap.offsetTop + topGap.offsetHeight : 0;
    const endY = botGap ? botGap.offsetTop : this.box.scrollHeight;
    const nearTop = (top - startY) < (endY - bottom);
    if (nearTop) this._dropBottom(over, bd); else this._dropTop(over, bd);
  }

  /** Rows the reader can still be looking at: never drop within this many rows of the one at the top of the view. */
  _margin() { return Math.ceil(this.viewH() / Math.max(24, this.avg)) + 10; }

  _dropBottom(count, bd) {
    const msgs = this.getMessages();
    const map = this.indexMap();
    const ai = bd.rows.indexOf(this._anchorNode());
    count = Math.min(count, bd.rows.length - 1 - Math.max(0, ai) - this._margin());
    if (count <= 0) return;
    const keep = bd.rows.length - count;
    const cut = bd.rows[keep];            // first row to go
    // remove from the cut row's own date separator (if it has one) to the last message row
    let n = cut.previousElementSibling && cut.previousElementSibling.classList.contains('date-sep') ? cut.previousElementSibling : cut;
    const last = bd.rows[bd.rows.length - 1];
    while (n) {
      const nxt = n.nextElementSibling;
      const isTail = n === last;
      n.remove();
      if (isTail) break;
      n = nxt;
    }
    this._setGap('bottom', msgs.length - map.get(idOf(cut)));
  }

  _dropTop(count, bd) {
    const ai = bd.rows.indexOf(this._anchorNode());
    count = Math.min(count, Math.max(0, ai) - this._margin());
    if (count <= 0) return;
    const keepFrom = bd.rows[count];
    this._keepAnchor(() => {
      let n = bd.rows[0];
      while (n.previousElementSibling && n.previousElementSibling.classList.contains('date-sep')) n = n.previousElementSibling;
      // the first kept row's own separator stays with it
      let stop = keepFrom;
      if (stop.previousElementSibling && stop.previousElementSibling.classList.contains('date-sep')) stop = stop.previousElementSibling;
      while (n && n !== stop) {
        const nxt = n.nextElementSibling;
        n.remove();
        n = nxt;
      }
      this._setGap('top', this.indexMap().get(idOf(keepFrom)));
      this._syncLoadEarlier();
    });
  }

  /** A live message was added to the end of the list. Returns true when it was handled here
   *  (the bottom spacer grew, nothing to paint); false when the caller should append the node. */
  noteLiveAppend() {
    const g = this.gap('bottom');
    if (!g) return false;
    const n = this.getMessages().length;
    const bd = this.bounds();
    this._setGap('bottom', n - (bd ? bd.b : 0));
    return true;
  }

  /** The list changed under the window (a message was deleted): make the spacers count what is
   *  really outside it. */
  resync() {
    const bd = this.bounds();
    if (!bd) return;
    const n = this.getMessages().length;
    if (this.gap('top')) this._setGap('top', bd.a);
    if (this.gap('bottom')) this._setGap('bottom', n - bd.b);
  }

  /** Paint a window around message `index` (a quoted reply, a search hit) and leave the scroll to the caller. */
  showIndex(index) {
    const n = this.getMessages().length;
    if (n <= this.max) { this.paint(0, n); return; }
    const a = Math.max(0, Math.min(n - this.target, index - Math.floor(this.target / 2)));
    this.paint(a, a + this.target);
  }

  /** After the caller appended a live row at the tail: keep the cap. */
  afterLiveAppend() {
    const bd = this.bounds();
    if (!bd) return;
    if (bd.rows.length > this.max) this._dropTop(bd.rows.length - this.target, bd);
  }
}

// ------------------------------------------------------------------ "Archive my chats" control

/** Months offered; 0 = off. */
export const ARCHIVE_CHOICES = [0, 1, 3, 6, 12, 24, 36];

/** The select's current value as the API wants it: null (off) or a whole number 1..120. */
export function archiveValue(raw) {
  const n = Number(raw);
  return Number.isInteger(n) && n >= 1 && n <= 120 ? n : null;
}

/** Settings -> You -> Account: "Archive my chats older than [off | N months]". Only the caller's
 *  own value is read and written (GET/PUT /api/me/archive); an Admin has no say over anyone else's.
 *  `deps`: {api, t, el, toast, can} where can() says whether the profile manages its history. */
export function renderArchiveControl(host, deps) {
  if (!host) return null;
  const { api, t, el, toast } = deps;
  for (const old of host.querySelectorAll('[data-anchor="archive-after"]')) old.remove();
  if (deps.can && !deps.can()) return null;
  const status = el('p', { class: 'dev-group-hint archive-would', 'aria-live': 'polite' });
  const select = el('select', { id: 'archive-after-months', class: 'settings-select archive-after-select', disabled: 'disabled' });
  const label = el('label', { class: 'archive-after-label', for: 'archive-after-months', text: t('archive.older_than') });
  let current = null;
  const paint = (months) => {
    select.replaceChildren();
    const values = ARCHIVE_CHOICES.slice();
    if (months && !values.includes(months)) values.push(months);
    values.sort((x, y) => x - y);
    for (const v of values) {
      select.append(el('option', { value: String(v), text: v ? t('archive.months', { count: v }) : t('archive.off') }));
    }
    select.value = String(months || 0);
  };
  const explain = async (months) => {
    status.textContent = '';
    if (!months) return;
    try {
      const r = await api.myArchive(months);
      if (r && typeof r.would_archive === 'number') status.textContent = t('archive.would', { count: r.would_archive });
    } catch { /* a count is a courtesy */ }
  };
  const group = el('section', { class: 'dev-group archive-after', 'data-anchor': 'archive-after', 'aria-label': t('archive.older_than') }, [
    el('div', { class: 'archive-after-row' }, [label, select]),
    el('p', { class: 'dev-group-hint', text: t('archive.hint') }),
    status,
  ]);
  host.append(group);
  paint(0);
  Promise.resolve(api.myArchive()).then((r) => {
    current = archiveValue(r && r.months);
    paint(current);
    select.disabled = false;
    explain(current);
  }).catch(() => { group.remove(); });
  select.addEventListener('change', async () => {
    const want = archiveValue(select.value);
    select.disabled = true;
    try {
      await api.setMyArchive(want);
      current = want;
      toast(t('retention.saved'));
      await explain(want);
    } catch (e) {
      paint(current);
      toast((e && e.message) || String(e), true);
    } finally {
      select.disabled = false;
    }
  });
  return group;
}
