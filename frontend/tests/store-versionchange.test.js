// store.js must close its IndexedDB connection on `versionchange`.
//
// privacy.js deletes the database while the tab is open. IndexedDB holds
// that delete as "blocked" until every open connection closes, so a
// connection with no onversionchange handler kept the wipe waiting until the
// tab died — and every draft/message write in between still landed
// (review 2026-09-24). The fake factory hands out the same FakeDatabase
// object the store opened, so the handler the store installed is inspectable.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createStore, TIER_FULL } from '../static/js/store.js';
import { createFakeIndexedDB } from './helpers/fake-idb.js';

test('the open connection closes itself on versionchange', async () => {
  const factory = createFakeIndexedDB();
  let conn = null;
  const spying = {
    ...factory,
    open(name, version) {
      const req = factory.open(name, version);
      // The store assigns req.onsuccess after open() returns; the fake fires
      // it from a microtask. A setter wraps whatever the store installs.
      let handler = null;
      Object.defineProperty(req, 'onsuccess', {
        get: () => handler,
        set: (fn) => { handler = (ev) => { conn = req.result; if (fn) fn(ev); }; },
      });
      return req;
    },
  };
  const store = createStore({ factory: spying, tier: TIER_FULL });
  await store.setDraft('t1', 'hello');           // forces the open
  assert.ok(conn, 'no open connection was observed');
  assert.equal(typeof conn.onversionchange, 'function', 'onversionchange must be set');
  assert.equal(conn.closed, false);
  conn.onversionchange();
  assert.equal(conn.closed, true, 'versionchange must close the connection');
});
