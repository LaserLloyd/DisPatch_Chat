// Web Push settings row (static/js/push.js): who gets a working switch.
//
// The gate is the point: Safe Mode never sees the row, an insecure address
// (http://<lan-ip>:8765) gets a disabled switch that says to use the https
// address, a browser without the APIs says so, and the payload the worker
// shows is a title only. Run: node --test frontend/tests/

import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { domSkip } from './_require-dom.js';
import { pushSupport, pushRow, urlBase64ToUint8Array } from '../static/js/push.js';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — DOM tests skip */ }

const HERE = dirname(fileURLToPath(import.meta.url));
const SW = readFileSync(join(HERE, '..', 'static', 'sw.js'), 'utf8');
const t = (k) => `[${k}]`;

function fakeWin({ secure = true, apis = true, vapid = { public_key: 'AQ', subscribed: false },
  vapidStatus = 200 } = {}) {
  const win = new jsdom.JSDOM('<!doctype html><html><body></body></html>').window;
  const calls = [];
  Object.defineProperty(win, 'isSecureContext', { value: secure });
  if (apis) {
    win.Notification = { permission: 'default', requestPermission: async () => 'granted' };
    win.PushManager = function PushManager() {};
    Object.defineProperty(win.navigator, 'serviceWorker', {
      value: { ready: Promise.resolve({ pushManager: { getSubscription: async () => null } }) },
    });
  }
  win.fetch = async (url, opts = {}) => {
    calls.push({ url, method: opts.method || 'GET' });
    const body = vapidStatus === 200 ? vapid
      : { detail: 'Notifications need a remembered device: lock, then unlock with "Remember this device" ticked.' };
    return { ok: vapidStatus === 200, status: vapidStatus, statusText: '', json: async () => body };
  };
  return { win, calls };
}

const tick = () => new Promise((r) => setTimeout(r, 20));

test('pushSupport: insecure context first, then missing APIs', () => {
  assert.deepEqual(pushSupport({ isSecureContext: false, Notification: {}, PushManager: {},
    navigator: { serviceWorker: {} } }), { ok: false, reason: 'insecure' });
  assert.deepEqual(pushSupport({ isSecureContext: true, navigator: {} }),
    { ok: false, reason: 'unsupported' });
  assert.deepEqual(pushSupport({ isSecureContext: true, Notification: {}, PushManager: {},
    navigator: { serviceWorker: {} } }), { ok: true, reason: '' });
  assert.deepEqual(pushSupport(null), { ok: false, reason: 'insecure' });
});

test('urlBase64ToUint8Array decodes an unpadded base64url key', () => {
  assert.deepEqual(Array.from(urlBase64ToUint8Array('BAEC_-8')), [4, 1, 2, 255, 239]);
});

test('the row is built for every signed-in profile (no Safe Mode; `decoy` is ignored)', { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, () => {
  const { win } = fakeWin();
  assert.ok(pushRow(t, { decoy: true, win }));
});

test('plain http (not a secure context): disabled, says to use the https address',
  { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, () => {
    const { win, calls } = fakeWin({ secure: false });
    const row = pushRow(t, { win });
    const box = row.querySelector('#push-toggle');
    assert.equal(box.disabled, true);
    assert.equal(row.querySelector('#push-hint').textContent, '[push.needs_https]');
    assert.equal(row.dataset.state, 'insecure');
    assert.equal(calls.length, 0);
  });

test('no Notification / PushManager: disabled, says the browser cannot',
  { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, () => {
    const { win } = fakeWin({ apis: false });
    const row = pushRow(t, { win });
    assert.equal(row.querySelector('#push-toggle').disabled, true);
    assert.equal(row.querySelector('#push-hint').textContent, '[push.unsupported]');
  });

test('secure + supported: the switch works and reflects the server',
  { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, async () => {
    const { win, calls } = fakeWin();
    const row = pushRow(t, { win });
    await tick();
    const box = row.querySelector('#push-toggle');
    assert.equal(box.disabled, false);
    assert.equal(box.checked, false);
    assert.deepEqual(calls.map((c) => c.url), ['/api/push/vapid']);
  });

test('an ordinary (not remembered) session: disabled with the remember hint',
  { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, async () => {
    const { win } = fakeWin({ vapidStatus: 403 });
    const row = pushRow(t, { win });
    await tick();
    assert.equal(row.querySelector('#push-toggle').disabled, true);
    assert.equal(row.querySelector('#push-hint').textContent, '[push.needs_remember]');
  });

test('sw.js shows a title only and opens the thread on click', () => {
  const push = SW.slice(SW.indexOf("addEventListener('push'"));
  assert.match(push, /showNotification\(title, \{/);
  assert.doesNotMatch(push.slice(0, push.indexOf("addEventListener('notificationclick'")), /body:/,
    'a push notification never carries a body');
  assert.match(SW, /addEventListener\('notificationclick'/);
  assert.match(SW, /dispatch:open-thread/);
  assert.match(SW, /'\/static\/js\/push\.js'/);
});

test('main.js mounts the Notifications row for every profile', () => {
  const MAIN = readFileSync(join(HERE, '..', 'static', 'js', 'main.js'), 'utf8');
  assert.match(MAIN, /notifyGroup\.hidden = false/);
  assert.match(MAIN, /pushRow\(t, \{ onToast: toast \}\)/);
});
