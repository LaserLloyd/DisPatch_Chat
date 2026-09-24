// Local store — drafts, a persisted outbox, and enough recent rows to read a
// thread before the network answers.
//
// Three object stores in one IndexedDB database:
//
//   drafts    thread_id      -> the composer text that was never sent
//   outbox    client_msg_id  -> a 'send' frame the server has not acked
//   messages  thread_id      -> the last MESSAGE_CACHE_ROWS rows we were shown
//
// None of it is authoritative. The server owns every one of these facts; this
// file exists so a reload, a dead tunnel or a backgrounded tab does not lose
// typed text, and so opening a thread shows something other than a shimmer.
//
// == THE PART THAT IS NOT ABOUT CONVENIENCE ==================================
//
// This device runs in two tiers. A locked "Safe Mode" session sees a redacted
// view of the same threads an unlocked one sees in full. Putting either on the
// same disk without a partition would mean a family tablet could be handed
// over locked and still read, out of a cache, the messages the lock exists to
// hide.
//
// So EVERY key is scoped to the tier it was written in, and a store instance
// only ever composes keys in its OWN tier. A locked session cannot address an
// unlocked key — not "is not supposed to": the key it would have to ask for is
// not a key it can build. That is the invariant frontend/tests/store.test.js
// pins down first, and it is why the tier is fixed at construction rather than
// read per call — there is no code path where a caller chooses which tier it
// reads.
//
// Rows cached in Safe Mode are the rows THE SERVER SENT to a Safe-Mode
// session: already redacted, upstream, before they reached this file. Nothing
// here un-redacts anything, and nothing here should ever be handed a full-tier
// row to write into the safe tier. The cache sits downstream of the gate, never
// beside it.
//
// Dropping to Safe Mode wipes the unlocked tier outright (wipeTier), because a
// cache that merely stops being readable is still a cache sitting on the disk
// of a device somebody has just handed to a child.

export const DB_NAME = 'dispatch-store';
export const DB_VERSION = 1;

export const STORE_DRAFTS = 'drafts';
export const STORE_OUTBOX = 'outbox';
export const STORE_MESSAGES = 'messages';
export const STORES = [STORE_DRAFTS, STORE_OUTBOX, STORE_MESSAGES];

export const TIER_FULL = 'full';
export const TIER_SAFE = 'safe';

// How many rows of a thread are worth keeping: enough to read the tail of a
// conversation, not so many that the cache becomes a second copy of the app.
export const MESSAGE_CACHE_ROWS = 50;

// U+0000 cannot occur in a thread id, a client_msg_id or a tier name, so
// `tier + SEP + id` is unambiguous in both directions. A printable separator
// would let an id that contained it forge a key in the other tier.
const SEP = '\u0000';

/** The tier a session with this `decoy` flag reads and writes. */
export function tierFor(decoy) { return decoy ? TIER_SAFE : TIER_FULL; }

/** tier + id -> the one key that pair is allowed to touch. */
export function scopedKey(tier, id) { return `${tier}${SEP}${id}`; }

/** The tier half of a scoped key, or null if it is not one. */
export function keyTier(key) {
  if (typeof key !== 'string') return null;
  const i = key.indexOf(SEP);
  return i < 0 ? null : key.slice(0, i);
}

/** The id half of a scoped key, or null if it is not one. */
export function keyId(key) {
  if (typeof key !== 'string') return null;
  const i = key.indexOf(SEP);
  return i < 0 ? null : key.slice(i + 1);
}

/** The browser's IndexedDB, or null where there is not one: a test realm, a
 *  browser with storage switched off, a private window that refuses. Null is a
 *  supported state everywhere below — the store degrades to a no-op rather
 *  than throwing into whatever called it. */
export function defaultFactory() {
  try { return globalThis.indexedDB || null; } catch { return null; }
}

// -- IndexedDB, promisified --------------------------------------------------
// The surface used here is deliberately tiny (open / createObjectStore /
// transaction / put / get / delete / getAllKeys / getAll) so a test can pass a
// small in-memory stand-in instead of dragging in a fake-indexeddb dependency
// the app itself would then have to ship or ignore.

function wrap(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('idb request failed'));
  });
}

function done(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error || new Error('idb transaction failed'));
    tx.onabort = () => reject(tx.error || new Error('idb transaction aborted'));
  });
}

/** Open the database, creating the three stores on first use.
 *
 *  Out-of-line keys (`put(value, key)`) on purpose: the key carries the tier
 *  scope and nothing else, so it must not be derivable from — and therefore
 *  forgeable through — the value being stored.
 */
function openDb(factory) {
  return new Promise((resolve, reject) => {
    let req;
    try { req = factory.open(DB_NAME, DB_VERSION); }
    catch (e) { reject(e); return; }
    req.onupgradeneeded = () => {
      const db = req.result;
      for (const name of STORES) {
        if (!db.objectStoreNames.contains(name)) db.createObjectStore(name);
      }
    };
    req.onsuccess = () => {
      const db = req.result;
      // privacy.js deletes this database while the tab is open. IndexedDB
      // holds that delete as "blocked" until every connection closes, so
      // without this the wipe waited for the tab to die — and every write in
      // between still landed.
      db.onversionchange = () => { try { db.close(); } catch { /* already gone */ } };
      resolve(db);
    };
    req.onerror = () => reject(req.error || new Error('idb open failed'));
    req.onblocked = () => reject(new Error('idb open blocked'));
  });
}

/** A store scoped to ONE tier.
 *
 *  Every method builds its key with `scopedKey(tier, ...)`, so no argument a
 *  caller can pass reaches another tier's rows — except wipeTier, which exists
 *  precisely to delete them and takes the tier explicitly for that reason.
 *
 *  Every method is fail-soft. Storage that is full, blocked or absent is a
 *  reason to lose a draft, never a reason to break the composer: reads return
 *  the empty value, writes return false.
 */
export function createStore({ factory = defaultFactory(), tier = TIER_FULL } = {}) {
  let dbPromise = null;

  function db() {
    if (!factory) return Promise.resolve(null);
    if (!dbPromise) {
      // A failed open must not be retried on every keystroke: one null and the
      // store stays inert for the life of this page.
      dbPromise = openDb(factory).catch(() => null);
    }
    return dbPromise;
  }

  async function tx(names, mode, fn) {
    const d = await db();
    if (!d) return null;
    try {
      const t = d.transaction(names, mode);
      const out = await fn(t);
      if (mode === 'readwrite') await done(t);
      return out;
    } catch {
      return null;
    }
  }

  const k = (id) => scopedKey(tier, id);

  const self = {
    tier,

    /** True once the database is reachable. Used by tests, and by nothing in
     *  the app — which never needs to ask. */
    async ready() { return !!(await db()); },

    // -- drafts --------------------------------------------------------------

    async setDraft(threadId, text) {
      if (!threadId) return false;
      if (!text) return self.clearDraft(threadId);
      const r = await tx([STORE_DRAFTS], 'readwrite', async (t) => {
        await wrap(t.objectStore(STORE_DRAFTS).put({ text, at: Date.now() }, k(threadId)));
        return true;
      });
      return !!r;
    },

    async getDraft(threadId) {
      if (!threadId) return '';
      const r = await tx([STORE_DRAFTS], 'readonly', (t) =>
        wrap(t.objectStore(STORE_DRAFTS).get(k(threadId))));
      return (r && typeof r.text === 'string') ? r.text : '';
    },

    async clearDraft(threadId) {
      if (!threadId) return false;
      const r = await tx([STORE_DRAFTS], 'readwrite', async (t) => {
        await wrap(t.objectStore(STORE_DRAFTS).delete(k(threadId)));
        return true;
      });
      return !!r;
    },

    /** Thread ids that have a saved draft, for the "Draft" label on the thread
     *  row. Keys only — the texts are never needed to draw a list. */
    async draftThreadIds() {
      const keys = await tx([STORE_DRAFTS], 'readonly', (t) =>
        wrap(t.objectStore(STORE_DRAFTS).getAllKeys()));
      if (!keys) return [];
      return keys.filter((key) => keyTier(key) === tier).map(keyId).filter(Boolean);
    },

    // -- outbox --------------------------------------------------------------

    /** Queue a send frame. Keyed by its client_msg_id, which is the SAME
     *  identity the server dedups on — so replaying a row that did in fact
     *  land is answered with an ack and never persisted twice. That is why
     *  this needs no delivery bookkeeping of its own. */
    async queueSend(entry) {
      const cmid = entry && entry.frame && entry.frame.client_msg_id;
      if (!cmid) return false;
      const r = await tx([STORE_OUTBOX], 'readwrite', async (t) => {
        await wrap(t.objectStore(STORE_OUTBOX).put({
          frame: entry.frame,
          thread_id: entry.thread_id || entry.frame.thread_id || null,
          text: entry.text || entry.frame.text || '',
          at: entry.at || Date.now(),
        }, k(cmid)));
        return true;
      });
      return !!r;
    },

    async dropSend(clientMsgId) {
      if (!clientMsgId) return false;
      const r = await tx([STORE_OUTBOX], 'readwrite', async (t) => {
        await wrap(t.objectStore(STORE_OUTBOX).delete(k(clientMsgId)));
        return true;
      });
      return !!r;
    },

    /** Everything still queued, oldest first — the order they were typed is
     *  the order they should reach the thread. */
    async listOutbox() {
      const out = await tx([STORE_OUTBOX], 'readonly', async (t) => {
        const s = t.objectStore(STORE_OUTBOX);
        const keys = await wrap(s.getAllKeys());
        const vals = await wrap(s.getAll());
        return { keys, vals };
      });
      if (!out || !out.keys) return [];
      const rows = [];
      out.keys.forEach((key, i) => {
        if (keyTier(key) !== tier) return;
        const v = out.vals[i];
        if (v && v.frame && v.frame.client_msg_id) rows.push(v);
      });
      rows.sort((a, b) => (a.at || 0) - (b.at || 0));
      return rows;
    },

    async clearOutbox() {
      const keys = await tx([STORE_OUTBOX], 'readonly', (t) =>
        wrap(t.objectStore(STORE_OUTBOX).getAllKeys()));
      if (!keys) return false;
      const mine = keys.filter((key) => keyTier(key) === tier);
      if (!mine.length) return true;
      const r = await tx([STORE_OUTBOX], 'readwrite', async (t) => {
        const s = t.objectStore(STORE_OUTBOX);
        for (const key of mine) await wrap(s.delete(key));
        return true;
      });
      return !!r;
    },

    // -- message cache -------------------------------------------------------

    /** Keep the tail of a thread. `rows` are stored EXACTLY as the server sent
     *  them to this tier; see the header — this is downstream of redaction and
     *  must never be handed rows another tier's session fetched. */
    async cacheMessages(threadId, rows) {
      if (!threadId || !Array.isArray(rows)) return false;
      const tail = rows.slice(-MESSAGE_CACHE_ROWS);
      const r = await tx([STORE_MESSAGES], 'readwrite', async (t) => {
        await wrap(t.objectStore(STORE_MESSAGES).put({ rows: tail, at: Date.now() }, k(threadId)));
        return true;
      });
      return !!r;
    },

    async cachedMessages(threadId) {
      if (!threadId) return [];
      const r = await tx([STORE_MESSAGES], 'readonly', (t) =>
        wrap(t.objectStore(STORE_MESSAGES).get(k(threadId))));
      return (r && Array.isArray(r.rows)) ? r.rows : [];
    },

    // -- wipe ----------------------------------------------------------------

    /** Delete every row of one tier, across all three stores.
     *
     *  Takes the tier explicitly because its whole job is to reach a tier this
     *  store is not scoped to: the locked session that has just been created is
     *  the one that must destroy what the unlocked session left behind.
     */
    async wipeTier(which) {
      const d = await db();
      if (!d) return false;
      try {
        const t = d.transaction(STORES, 'readwrite');
        for (const name of STORES) {
          const s = t.objectStore(name);
          for (const key of await wrap(s.getAllKeys())) {
            if (keyTier(key) === which) await wrap(s.delete(key));
          }
        }
        await done(t);
        return true;
      } catch {
        return false;
      }
    },

    /** Everything, both tiers. Privacy mode's wipe path. */
    async wipeAll() {
      const d = await db();
      if (!d) return false;
      try {
        const t = d.transaction(STORES, 'readwrite');
        for (const name of STORES) {
          const s = t.objectStore(name);
          for (const key of await wrap(s.getAllKeys())) await wrap(s.delete(key));
        }
        await done(t);
        return true;
      } catch {
        return false;
      }
    },

    close() {
      const p = dbPromise;
      dbPromise = null;
      if (!p) return;
      p.then((d) => { try { if (d) d.close(); } catch { /* already gone */ } })
        .catch(() => {});
    },
  };

  return self;
}

// -- The stale-paint gate ----------------------------------------------------
//
// The cache is allowed to put rows on screen EARLY. It is never allowed to put
// them on screen LATE, and "late" is not a question of milliseconds: once a
// real fetch for a thread has resolved, or a live socket frame for it has
// arrived, the cached copy is by definition behind, and painting it would show
// somebody a message that has already been superseded. "The app showed me an
// old message" is the bug; this object is the thing that prevents it.
//
// Two checks, and the second is the one that matters. `paintCached` reads the
// cache asynchronously, so between deciding to paint and having something to
// paint, the fetch can land. Checking only before the await is the version of
// this that looks right and is wrong — a guard that measures nothing. The
// check AFTER the await is what makes the promise true.

export function createCachePainter({ store, paint, isEmpty = () => true }) {
  const fresh = new Set();   // thread ids the network has spoken for

  return {
    /** A real fetch resolved, or a live frame arrived. Either way the cache is
     *  now behind and must not paint for this thread again. */
    markFresh(threadId) { if (threadId) fresh.add(threadId); },

    /** Has the network spoken for this thread yet? */
    isFresh(threadId) { return fresh.has(threadId); },

    /** Forget everything — a reboot into another tier starts over. */
    reset() { fresh.clear(); },

    /** Paint the cached tail of `threadId`, unless anything fresher exists.
     *  Returns true only if rows actually reached the screen. */
    async paintCached(threadId) {
      if (!threadId) return false;
      if (fresh.has(threadId)) return false;          // network already answered
      if (!isEmpty(threadId)) return false;           // something real is on screen
      let rows = [];
      try { rows = await store.cachedMessages(threadId); } catch { return false; }
      if (!rows || !rows.length) return false;
      // THE LOAD-BEARING LINE. The read above is async; the fetch may have
      // resolved while it was in flight. Without this re-check the cache wins a
      // race it must always lose.
      if (fresh.has(threadId)) return false;
      if (!isEmpty(threadId)) return false;
      try { paint(threadId, rows); } catch { return false; }
      return true;
    },
  };
}
