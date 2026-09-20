// The local store: tier partition, wipe on lock, and the stale-paint rule.
//
// Run: node --test frontend/tests/
//
// Three of these tests are about the feature. The other three are the reason
// the feature is allowed to exist at all, and they are the ones to read first:
//
//   1. TIER PARTITION — a locked session must not be able to read what an
//      unlocked one cached. DisPatch's whole two-tier promise is that handing
//      somebody a locked tablet hands them the redacted view; a cache that
//      ignored the tier would be a way around that which never touches the
//      server and therefore never meets a single one of its gates.
//
//   2. WIPE ON LOCK — not being able to READ the unlocked tier is not enough.
//      Dropping to Safe Mode has to delete it.
//
//   3. A STALE CACHE MUST NEVER PAINT OVER FRESH DATA. The cache read is
//      asynchronous, so the fetch it was racing can land while it is in
//      flight. This is the one that would be reported as "the app showed me an
//      old message", and the only way to test it honestly is to interleave the
//      two deliberately rather than hope the ordering holds.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createStore, createCachePainter, scopedKey, keyTier, keyId, tierFor,
  DB_NAME, STORE_DRAFTS, STORE_OUTBOX, STORE_MESSAGES,
  TIER_FULL, TIER_SAFE, MESSAGE_CACHE_ROWS,
} from '../static/js/store.js';
import { createFakeIndexedDB } from './helpers/fake-idb.js';

// One factory = one disk. Two stores built on it are two SESSIONS sharing that
// disk, which is exactly the relationship an unlocked session and the locked
// one that replaces it have.
const disk = () => createFakeIndexedDB();

const unlocked = (factory) => createStore({ factory, tier: TIER_FULL });
const locked = (factory) => createStore({ factory, tier: TIER_SAFE });

// ---------------------------------------------------------------------------
// 1. TIER PARTITION  — the proof test
// ---------------------------------------------------------------------------

test('a locked session cannot read what an unlocked session cached', async () => {
  const factory = disk();

  const full = unlocked(factory);
  await full.setDraft('t1', 'the unsent half of a private conversation');
  await full.cacheMessages('t1', [{ id: 'm1', content: 'private' }]);
  await full.queueSend({ frame: { type: 'send', thread_id: 't1', text: 'private', client_msg_id: 'c-1' } });
  full.close();

  // Same device, same database, same thread id — the only thing that differs
  // is the tier the session runs in.
  const safe = locked(factory);
  assert.equal(await safe.getDraft('t1'), '',
    'a locked session read the unlocked tier\'s draft');
  assert.deepEqual(await safe.cachedMessages('t1'), [],
    'a locked session read the unlocked tier\'s cached messages');
  assert.deepEqual(await safe.listOutbox(), [],
    'a locked session read the unlocked tier\'s outbox');
  assert.deepEqual(await safe.draftThreadIds(), [],
    'a locked session enumerated the unlocked tier\'s draft threads');

  // And the partition is symmetric: what the locked session writes under the
  // same thread id is its own row, not an overwrite of the other tier's.
  await safe.setDraft('t1', 'safe-mode text');
  assert.equal(await safe.getDraft('t1'), 'safe-mode text');

  const again = unlocked(factory);
  assert.equal(await again.getDraft('t1'), 'the unsent half of a private conversation',
    'the locked session overwrote the unlocked tier\'s draft');

  // What is actually on the disk: two distinct keys, each carrying its tier.
  const keys = factory._rawKeys(DB_NAME, STORE_DRAFTS);
  assert.deepEqual(keys.sort(), [scopedKey(TIER_FULL, 't1'), scopedKey(TIER_SAFE, 't1')].sort());
});

test('a key carries its tier, and the halves round-trip', () => {
  const key = scopedKey(TIER_FULL, 'thread-42');
  assert.equal(keyTier(key), TIER_FULL);
  assert.equal(keyId(key), 'thread-42');
  assert.equal(keyTier('not-a-scoped-key'), null);
  assert.equal(tierFor(true), TIER_SAFE);
  assert.equal(tierFor(false), TIER_FULL);
});

// ---------------------------------------------------------------------------
// 2. WIPE ON LOCK
// ---------------------------------------------------------------------------

test('dropping to Safe Mode deletes every unlocked-tier row', async () => {
  const factory = disk();

  const full = unlocked(factory);
  await full.setDraft('t1', 'draft');
  await full.cacheMessages('t1', [{ id: 'm1', content: 'hello' }]);
  await full.queueSend({ frame: { thread_id: 't1', text: 'hello', client_msg_id: 'c-1' } });
  full.close();

  const safe = locked(factory);
  await safe.setDraft('t9', 'safe mode draft');      // the locked tier's own row
  assert.equal(await safe.wipeTier(TIER_FULL), true);

  // Nothing of the unlocked tier is left ON THE DISK — not merely unreadable
  // through the API, which is the weaker claim the partition test already made.
  for (const s of [STORE_DRAFTS, STORE_OUTBOX, STORE_MESSAGES]) {
    const left = factory._rawKeys(DB_NAME, s).filter((k) => keyTier(k) === TIER_FULL);
    assert.deepEqual(left, [], `${s} still holds unlocked-tier rows after the wipe`);
  }

  // And the locked session's own row survived: the wipe is a partition, not a
  // panic button.
  assert.equal(await safe.getDraft('t9'), 'safe mode draft');

  // An unlocked session coming back later finds nothing of its old self.
  const back = unlocked(factory);
  assert.equal(await back.getDraft('t1'), '');
  assert.deepEqual(await back.cachedMessages('t1'), []);
  assert.deepEqual(await back.listOutbox(), []);
});

test('wipeAll removes both tiers', async () => {
  const factory = disk();
  const full = unlocked(factory);
  const safe = locked(factory);
  await full.setDraft('t1', 'a');
  await safe.setDraft('t1', 'b');
  await full.wipeAll();
  assert.deepEqual(factory._rawKeys(DB_NAME, STORE_DRAFTS), []);
});

// ---------------------------------------------------------------------------
// 3. A STALE CACHE MUST NEVER PAINT OVER FRESH DATA
// ---------------------------------------------------------------------------

test('a cached paint that resolves after the fetch does not paint', async () => {
  const factory = disk();
  const store = unlocked(factory);
  await store.cacheMessages('t1', [{ id: 'old', content: 'yesterday' }]);

  const painted = [];
  const painter = createCachePainter({
    store,
    paint: (tid, rows) => painted.push([tid, rows]),
    isEmpty: () => true,
  });

  // Start the cached paint, then let the real fetch land while the cache read
  // is still in flight. This is the actual race, not a simulation of it: the
  // store read really is asynchronous.
  const inFlight = painter.paintCached('t1');
  painter.markFresh('t1');                       // the fetch resolved
  assert.equal(await inFlight, false, 'a stale cache painted over fresh data');
  assert.deepEqual(painted, []);
});

test('a live socket frame closes the door on the cache too', async () => {
  const factory = disk();
  const store = unlocked(factory);
  await store.cacheMessages('t1', [{ id: 'old', content: 'yesterday' }]);

  const painted = [];
  const painter = createCachePainter({ store, paint: (t, r) => painted.push([t, r]) });
  painter.markFresh('t1');                       // a WS frame arrived first
  assert.equal(await painter.paintCached('t1'), false);
  assert.deepEqual(painted, []);
});

test('a cached paint runs when nothing fresher exists, and only once per open', async () => {
  const factory = disk();
  const store = unlocked(factory);
  await store.cacheMessages('t1', [{ id: 'm1', content: 'hello' }]);

  const painted = [];
  const painter = createCachePainter({ store, paint: (t, r) => painted.push([t, r]) });

  assert.equal(await painter.paintCached('t1'), true);
  assert.equal(painted.length, 1);
  assert.deepEqual(painted[0][1], [{ id: 'm1', content: 'hello' }]);

  // The fetch then resolves; a second open must not repaint from the cache.
  painter.markFresh('t1');
  assert.equal(await painter.paintCached('t1'), false);
  assert.equal(painted.length, 1);
});

test('a thread that already has rows on screen is never painted over', async () => {
  const factory = disk();
  const store = unlocked(factory);
  await store.cacheMessages('t1', [{ id: 'm1', content: 'hello' }]);
  const painter = createCachePainter({
    store, paint: () => assert.fail('painted over a non-empty view'),
    isEmpty: () => false,
  });
  assert.equal(await painter.paintCached('t1'), false);
});

test('a thread with nothing cached paints nothing', async () => {
  const factory = disk();
  const store = unlocked(factory);
  const painter = createCachePainter({ store, paint: () => assert.fail('painted an empty cache') });
  assert.equal(await painter.paintCached('t-unknown'), false);
});

// ---------------------------------------------------------------------------
// Drafts
// ---------------------------------------------------------------------------

test('a draft round-trips, and an empty one is a deletion', async () => {
  const store = unlocked(disk());
  await store.setDraft('t1', 'half a thought');
  assert.equal(await store.getDraft('t1'), 'half a thought');
  assert.deepEqual(await store.draftThreadIds(), ['t1']);

  await store.setDraft('t1', '');
  assert.equal(await store.getDraft('t1'), '');
  assert.deepEqual(await store.draftThreadIds(), [],
    'clearing a draft left the thread in the Draft-label list');
});

test('draftThreadIds lists this tier only', async () => {
  const factory = disk();
  const full = unlocked(factory);
  const safe = locked(factory);
  await full.setDraft('t1', 'a');
  await full.setDraft('t2', 'b');
  await safe.setDraft('t3', 'c');
  assert.deepEqual((await full.draftThreadIds()).sort(), ['t1', 't2']);
  assert.deepEqual(await safe.draftThreadIds(), ['t3']);
});

// ---------------------------------------------------------------------------
// Outbox
// ---------------------------------------------------------------------------

test('the outbox survives a reload and comes back in the order it was typed', async () => {
  const factory = disk();

  const before = unlocked(factory);
  await before.queueSend({ frame: { type: 'send', thread_id: 't1', text: 'first', client_msg_id: 'c-1' }, at: 1000 });
  await before.queueSend({ frame: { type: 'send', thread_id: 't1', text: 'second', client_msg_id: 'c-2' }, at: 2000 });
  before.close();                                   // the tab goes away

  const after = unlocked(factory);                  // ...and comes back
  const rows = await after.listOutbox();
  assert.deepEqual(rows.map((r) => r.frame.client_msg_id), ['c-1', 'c-2']);
  assert.deepEqual(rows.map((r) => r.frame.text), ['first', 'second']);
  // The frame is replayed UNCHANGED — same client_msg_id is what lets the
  // server's dedup answer a duplicate with an ack instead of a second message.
  assert.deepEqual(rows[0].frame, {
    type: 'send', thread_id: 't1', text: 'first', client_msg_id: 'c-1',
  });
});

test('an acked send leaves the outbox', async () => {
  const store = unlocked(disk());
  await store.queueSend({ frame: { thread_id: 't1', text: 'x', client_msg_id: 'c-1' } });
  await store.queueSend({ frame: { thread_id: 't1', text: 'y', client_msg_id: 'c-2' } });
  await store.dropSend('c-1');
  assert.deepEqual((await store.listOutbox()).map((r) => r.frame.client_msg_id), ['c-2']);
  await store.clearOutbox();
  assert.deepEqual(await store.listOutbox(), []);
});

test('a frame with no client_msg_id is not queued', async () => {
  const store = unlocked(disk());
  assert.equal(await store.queueSend({ frame: { thread_id: 't1', text: 'x' } }), false);
  assert.deepEqual(await store.listOutbox(), []);
});

// ---------------------------------------------------------------------------
// Message cache
// ---------------------------------------------------------------------------

test('the message cache keeps the TAIL, capped', async () => {
  const store = unlocked(disk());
  const rows = Array.from({ length: MESSAGE_CACHE_ROWS + 20 }, (_, i) => ({ id: `m${i}`, content: String(i) }));
  await store.cacheMessages('t1', rows);
  const got = await store.cachedMessages('t1');
  assert.equal(got.length, MESSAGE_CACHE_ROWS);
  assert.equal(got[got.length - 1].id, `m${rows.length - 1}`, 'the cache kept the head, not the tail');
  assert.equal(got[0].id, `m${rows.length - MESSAGE_CACHE_ROWS}`);
});

test('cached rows are a copy — mutating them later does not rewrite the cache', async () => {
  const store = unlocked(disk());
  const rows = [{ id: 'm1', content: 'as sent' }];
  await store.cacheMessages('t1', rows);
  rows[0].content = 'tampered';
  assert.equal((await store.cachedMessages('t1'))[0].content, 'as sent');
});

// ---------------------------------------------------------------------------
// Fail-soft
// ---------------------------------------------------------------------------

test('storage that refuses to open costs a draft, never a throw', async () => {
  const store = createStore({ factory: createFakeIndexedDB({ failOpen: true }), tier: TIER_FULL });
  assert.equal(await store.ready(), false);
  assert.equal(await store.setDraft('t1', 'x'), false);
  assert.equal(await store.getDraft('t1'), '');
  assert.deepEqual(await store.listOutbox(), []);
  assert.deepEqual(await store.cachedMessages('t1'), []);
  assert.deepEqual(await store.draftThreadIds(), []);
  assert.equal(await store.wipeTier(TIER_FULL), false);
  store.close();
});

test('no IndexedDB at all is a supported state', async () => {
  const store = createStore({ factory: null, tier: TIER_FULL });
  assert.equal(await store.ready(), false);
  assert.equal(await store.setDraft('t1', 'x'), false);
  assert.equal(await store.getDraft('t1'), '');
  assert.equal(await store.cacheMessages('t1', [{ id: 'm' }]), false);
});
