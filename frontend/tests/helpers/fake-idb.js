// A small in-memory stand-in for IndexedDB.
//
// Deliberately NOT a dependency. DisPatch ships vanilla ES modules with no
// build step and no runtime packages, and adding fake-indexeddb to make a test
// possible would put a package in the tree that the app itself must then be
// careful never to import. store.js takes its IDBFactory as an argument for
// exactly this reason, so the fake only has to cover the surface store.js
// actually uses:
//
//   factory.open(name, version) -> request { onupgradeneeded, onsuccess,
//                                            onerror, onblocked, result }
//   db.objectStoreNames.contains(name)
//   db.createObjectStore(name)          (out-of-line keys)
//   db.transaction(names, mode) -> tx { objectStore, oncomplete, onerror,
//                                       onabort, error }
//   store.put(value, key) / get(key) / delete(key) / getAllKeys() / getAll()
//
// Every request resolves on a microtask, the way a real one resolves off the
// main thread. That asynchrony is not incidental: it is what lets the
// stale-paint test interleave a fetch with a cache read.
//
// Keys are compared as strings. Values are cloned on the way in and on the way
// out, so a caller that mutates what it stored does not silently mutate the
// store — the real thing structured-clones too, and a test sharing a reference
// would pass for the wrong reason.

const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));

class FakeTransaction {
  constructor(db, names, mode) {
    this._db = db;
    this._names = names;
    this.mode = mode || 'readonly';
    this.error = null;
    this.oncomplete = null;
    this.onerror = null;
    this.onabort = null;
    this._settled = false;
    this._pending = 0;
    this._idle = 0;
    // A real transaction auto-commits once it goes idle. Waiting for a few
    // clear microtask hops rather than a fixed delay means a body that issues
    // requests in a loop (await, then the next delete) keeps the transaction
    // open for as long as it is actually doing something, and `done(tx)`
    // resolves after the last write rather than in the middle of them.
    const tick = () => {
      if (this._settled) return;
      if (this._pending > 0) { this._idle = 0; queueMicrotask(tick); return; }
      this._idle += 1;
      if (this._idle < 4) { queueMicrotask(tick); return; }
      this._settled = true;
      if (this.oncomplete) this.oncomplete({ target: this });
    };
    queueMicrotask(tick);
  }

  _assertWritable() {
    if (this.mode !== 'readwrite') throw new Error('ReadOnlyError');
  }

  _request(run) {
    const req = { onsuccess: null, onerror: null, result: undefined, error: null };
    this._pending += 1;
    queueMicrotask(() => {
      this._pending -= 1;
      try {
        req.result = run();
        if (req.onsuccess) req.onsuccess({ target: req });
      } catch (e) {
        req.error = e;
        if (req.onerror) req.onerror({ target: req });
      }
    });
    return req;
  }

  objectStore(name) {
    if (!this._names.includes(name)) throw new Error(`NotFoundError: ${name}`);
    const map = this._db._stores.get(name);
    if (!map) throw new Error(`NotFoundError: ${name}`);
    return new FakeObjectStore(map, this);
  }
}

class FakeObjectStore {
  constructor(map, tx) { this._map = map; this._tx = tx; }

  put(value, key) {
    return this._tx._request(() => {
      this._tx._assertWritable();
      this._map.set(String(key), clone(value));
      return String(key);
    });
  }

  get(key) { return this._tx._request(() => clone(this._map.get(String(key)))); }

  delete(key) {
    return this._tx._request(() => {
      this._tx._assertWritable();
      this._map.delete(String(key));
      return undefined;
    });
  }

  // Sorted, because a real store iterates in key order; a test that leaned on
  // insertion order would be testing this file rather than store.js.
  getAllKeys() { return this._tx._request(() => [...this._map.keys()].sort()); }

  getAll() {
    return this._tx._request(() =>
      [...this._map.keys()].sort().map((key) => clone(this._map.get(key))));
  }
}

class FakeDatabase {
  constructor(name, version) {
    this.name = name;
    this.version = version;
    this._stores = new Map();
    this.closed = false;
    const stores = this._stores;
    this.objectStoreNames = { contains: (n) => stores.has(n) };
  }

  createObjectStore(name) {
    if (!this._stores.has(name)) this._stores.set(name, new Map());
    return name;
  }

  transaction(names, mode) {
    if (this.closed) throw new Error('InvalidStateError: database is closed');
    return new FakeTransaction(this, Array.isArray(names) ? names : [names], mode);
  }

  close() { this.closed = true; }
}

/** An IDBFactory-shaped object backed by one in-memory database per name.
 *
 *  The backing data outlives `close()`, which is what makes a lock/unlock
 *  round trip testable: the session goes away, the disk does not.
 *
 *  Options:
 *    failOpen — every open() errors, so the fail-soft paths can be exercised.
 */
export function createFakeIndexedDB({ failOpen = false } = {}) {
  const databases = new Map();   // name -> FakeDatabase (persists across close)

  return {
    open(name, version) {
      const req = {
        onupgradeneeded: null, onsuccess: null, onerror: null, onblocked: null,
        result: undefined, error: null,
      };
      queueMicrotask(() => {
        if (failOpen) {
          req.error = new Error('open refused');
          if (req.onerror) req.onerror({ target: req });
          return;
        }
        let db = databases.get(name);
        const isNew = !db;
        if (isNew) { db = new FakeDatabase(name, version); databases.set(name, db); }
        db.closed = false;
        req.result = db;
        if (isNew && req.onupgradeneeded) req.onupgradeneeded({ target: req, oldVersion: 0 });
        if (req.onsuccess) req.onsuccess({ target: req });
      });
      return req;
    },

    /** Test-only: the raw keys of a store, bypassing every tier scope. This is
     *  how a test asks "what is ACTUALLY on the disk" rather than asking the
     *  store, which can only ever answer about its own tier. */
    _rawKeys(dbName, storeName) {
      const db = databases.get(dbName);
      if (!db) return [];
      const m = db._stores.get(storeName);
      return m ? [...m.keys()].sort() : [];
    },

    /** Test-only: the raw value behind a literal key. */
    _rawGet(dbName, storeName, key) {
      const db = databases.get(dbName);
      if (!db) return undefined;
      const m = db._stores.get(storeName);
      return m ? clone(m.get(String(key))) : undefined;
    },
  };
}
