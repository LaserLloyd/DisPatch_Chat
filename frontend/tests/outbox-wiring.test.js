// The persisted outbox: the two facts that live in wiring rather than in a
// function, and are therefore the two that a refactor silently breaks.
//
// Run: node --test frontend/tests/
//
//   1. ChatSocket must announce EVERY open, not only reconnects. This is a real
//      behavioural test against a stub WebSocket, because the distinction is
//      the whole point: onReconnect exists to resync after a drop and is
//      deliberately skipped on the first connect, which is exactly when an
//      outbox restored from disk needs a socket to replay onto. Before this
//      change there was no onOpen at all, so a queued message survived the
//      reload and then sat there until something else happened to reconnect.
//
//   2. main.js must restore that outbox BEFORE it connects, and must wipe the
//      unlocked tier BEFORE it reboots into Safe Mode. Both are orderings, not
//      behaviours — there is no return value to assert on, and getting either
//      backwards fails silently in the direction that looks fine. Pinned by
//      grep, the way this repo already pins the harness wiring: spinning up
//      main.js drags in the whole import graph and a jsdom window for a
//      question about the order of two lines.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { ChatSocket } from '../static/js/ws.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const MAIN = readFileSync(join(HERE, '..', 'static', 'js', 'main.js'), 'utf8');

// ---------------------------------------------------------------------------
// 1. onOpen fires on every open — including the first
// ---------------------------------------------------------------------------

/** The smallest WebSocket the class can be driven through. */
function stubSockets() {
  const made = [];
  class StubWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      this._listeners = new Map();
      made.push(this);
    }
    addEventListener(type, fn) {
      if (!this._listeners.has(type)) this._listeners.set(type, []);
      this._listeners.get(type).push(fn);
    }
    fire(type, ev) { for (const fn of this._listeners.get(type) || []) fn(ev); }
    open() { this.readyState = 1; this.fire('open', {}); }
    send(raw) { this.sent.push(raw); }
    close() { this.readyState = 3; this.fire('close', {}); }
  }
  StubWebSocket.OPEN = 1;
  return { made, StubWebSocket };
}

function withStubEnv(fn) {
  const { made, StubWebSocket } = stubSockets();
  const prevWS = globalThis.WebSocket;
  const prevLoc = globalThis.location;
  globalThis.WebSocket = StubWebSocket;
  if (!prevLoc) {
    globalThis.location = { protocol: 'http:', host: '198.51.100.7:8765' };
  }
  try { return fn(made); }
  finally {
    globalThis.WebSocket = prevWS;
    if (!prevLoc) delete globalThis.location;
  }
}

test('the first open is announced to onOpen and NOT to onReconnect', () => {
  withStubEnv((made) => {
    const opens = [];
    const reconnects = [];
    const sock = new ChatSocket({
      onMessage: () => {},
      onOpen: (reconnected) => opens.push(reconnected),
      onReconnect: () => reconnects.push(true),
    });
    sock.connect();
    made[0].open();

    assert.deepEqual(opens, [false],
      'onOpen did not fire on the first connect — a boot-restored outbox would '
      + 'sit unsent until something else happened to reconnect');
    assert.deepEqual(reconnects, [],
      'onReconnect fired on a first connect; it means "you lost the link and '
      + 'got it back" and resyncing freshly-loaded state is noise');

    sock.stop();
  });
});

test('a reconnect is announced to both, onOpen first', () => {
  withStubEnv((made) => {
    const order = [];
    const sock = new ChatSocket({
      onMessage: () => {},
      onOpen: () => order.push('open'),
      onReconnect: () => order.push('reconnect'),
    });
    sock.connect();
    made[0].open();
    order.length = 0;

    sock.connect();          // the reconnect path lands here
    made[1].open();

    assert.deepEqual(order, ['open', 'reconnect'],
      'the queued frames must go out before the resync that would otherwise '
      + 'redraw the thread without them');

    sock.stop();
  });
});

test('a socket built with no onOpen still works', () => {
  withStubEnv((made) => {
    const sock = new ChatSocket({ onMessage: () => {} });
    sock.connect();
    assert.doesNotThrow(() => made[0].open());
    sock.stop();
  });
});

// ---------------------------------------------------------------------------
// 2. Orderings in main.js
// ---------------------------------------------------------------------------

test('startApp restores the outbox BEFORE it connects the socket', () => {
  const restore = MAIN.indexOf('await restoreLocalState()');
  const connect = MAIN.indexOf('getSocket().connect()');
  assert.ok(restore > 0, 'main.js must await restoreLocalState() at boot');
  assert.ok(connect > 0, 'main.js must connect the socket at boot');
  assert.ok(restore < connect,
    'restoreLocalState() must be awaited BEFORE getSocket().connect(). Restored '
    + 'frames have to be in pendingSends by the time the first open fires, or '
    + 'the replay that carries them has nothing to carry — and the failure is '
    + 'silent: the message is on disk, off screen, and never sent.');
});

test('the socket replay is wired to onOpen, not only to onReconnect', () => {
  assert.match(MAIN, /onOpen:\s*\(\)\s*=>\s*resendPendingSends\(\)/,
    'resendPendingSends() must be wired to onOpen — onReconnect never fires on '
    + 'the first connect, which is the one boot gets');
});

test('goSafe wipes the unlocked tier BEFORE it reboots', () => {
  const goSafe = MAIN.slice(MAIN.indexOf('async function goSafe('));
  assert.ok(goSafe, 'main.js must define goSafe');
  const wipe = goSafe.indexOf('wipeTier(TIER_FULL)');
  const reboot = goSafe.indexOf('await reboot()');
  assert.ok(wipe > 0,
    'goSafe must wipe the unlocked tier of the local store — a locked device '
    + 'that merely cannot READ the cache is still carrying it');
  assert.ok(reboot > 0, 'goSafe must reboot');
  assert.ok(wipe < reboot,
    'the wipe must happen BEFORE reboot(), which re-runs startApp() and reloads '
    + 'the store; wiping afterwards races the session that is meant to be clean');
});

test('a send clears its draft, and typing schedules one', () => {
  assert.match(MAIN, /clearDraftFor\(draftThread\)/,
    'sendMessage() must clear the draft for the thread it sent into — the words '
    + 'are in the outbox now, and a leftover draft puts them back in the '
    + 'composer on the next open');
  assert.match(MAIN, /addEventListener\('input',[\s\S]{0,140}scheduleDraftSave\(\)/,
    'the composer input handler must schedule a draft save');
});

test('a FAILED messages fetch is not treated as freshness', () => {
  // The distinction the offline half of this feature turns on. A fetch that
  // resolved is fresh data and shuts the cache out for good; a fetch that
  // THREW is no data, and is the one moment the cache is the answer rather
  // than a preview. Marking the failure fresh — the conservative-looking
  // choice — makes the cached paint refuse on exactly the connection where it
  // was the point, and the app shows an empty state it does not have to.
  const open = MAIN.indexOf('async function openThread(');
  assert.ok(open > 0, 'main.js must define openThread');
  // The catch that wraps `await api.messages(id)`, bounded by its own closing
  // brace at the function's indent level.
  const start = MAIN.indexOf('} catch (e) {', open);
  assert.ok(start > open, 'openThread must still have its fetch catch');
  const end = MAIN.indexOf('\n  }\n', start);
  assert.ok(end > start, 'could not find the end of openThread\'s catch block');
  const branch = MAIN.slice(start, end);

  // A CALL, not the word: the branch's comment explains at length that this is
  // the one place markThreadFresh is deliberately not called, and a test that
  // matched the name would fail on its own explanation.
  assert.ok(!/markThreadFresh\s*\(/.test(branch),
    'the failed-fetch branch must NOT mark the thread fresh — that shuts the '
    + 'cache out on the one connection where it is the only thing to show');
  assert.match(branch, /await paintCachedIfUseful\(id\)/,
    'the failed-fetch branch must await a cached paint, or an offline open '
    + 'lands on an empty state with a readable history sitting on the disk');
  assert.match(branch, /state\.messages = \[\]/,
    'the failed-fetch branch must still empty state.messages — leaving the '
    + 'PREVIOUS thread\'s rows there is how a cached row and a live row end up '
    + 'disagreeing about which conversation you are in');
});

test('a cached paint is DOM-only and carries no message id', () => {
  const fn = MAIN.slice(MAIN.indexOf('function paintCachedMessages('));
  const body = fn.slice(0, fn.indexOf('\n}\n') + 2);
  assert.match(body, /delete node\.dataset\.id/,
    'a cached row must not be addressable as a live message: Delete, Regenerate '
    + 'and the last-message check all key off the id, and the row it names may '
    + 'have been deleted on the server since');
  assert.ok(!/state\.messages\s*=/.test(body) && !/state\.messages\.push/.test(body),
    'paintCachedMessages must not put cached rows into state.messages');
});
