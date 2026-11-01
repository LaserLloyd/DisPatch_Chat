// 4.3 #27: Settings has a URL. `#settings`, `#settings/<section>` and
// `#settings/<section>/<anchor>` open it (after sign-in when the page loads
// signed out), opening it writes the hash with replaceState only (no history
// entries, so Back leaves the app as before), closing removes it, and the
// voice add-on's `#voice/...` and the tools' `#tool=<id>` hashes are never
// touched. Pure: a fake window stands in for location/history/events.

import test from 'node:test';
import assert from 'node:assert/strict';
import { parseSettingsHash, settingsHashRoute, SECTION_IDS } from '../static/js/settings-nav.js';

// ------------------------------------------------------------------ parse

test('parse: what is and is not a Settings hash', () => {
  for (const h of ['', '#', '#voice/voices/main', '#tool=jobboard', '#settingsx', '#settings-x', '#Settings2', null, undefined]) {
    assert.equal(parseSettingsHash(h), null, String(h));
  }
});

test('parse: #settings (and a bare slash) is the default section', () => {
  for (const h of ['#settings', '#settings/', 'settings']) {
    assert.deepEqual(parseSettingsHash(h), { section: null, anchor: '', target: null, known: true }, h);
  }
});

test('parse: every section by id, with and without an anchor', () => {
  for (const id of SECTION_IDS) {
    assert.deepEqual(parseSettingsHash(`#settings/${id}`), { section: id, anchor: '', target: id, known: true });
  }
  assert.deepEqual(parseSettingsHash('#settings/people/machine'),
    { section: 'people', anchor: 'machine', target: 'people#machine', known: true });
  assert.deepEqual(parseSettingsHash('#settings/people/key:people.lock_timeout_seconds'),
    { section: 'people', anchor: 'key:people.lock_timeout_seconds', target: 'people#key:people.lock_timeout_seconds', known: true });
  // a trailing slash is not an anchor
  assert.deepEqual(parseSettingsHash('#settings/system/'), { section: 'system', anchor: '', target: 'system', known: true });
});

test('parse: old tab ids go through resolveSettingsTarget (aliases keep their subsection)', () => {
  assert.equal(parseSettingsHash('#settings/theme').target, 'appearance');
  assert.equal(parseSettingsHash('#settings/security').target, 'account#signin');
  assert.equal(parseSettingsHash('#settings/mybots').target, 'characters#mine');
  assert.equal(parseSettingsHash('#settings/agents').target, 'connections#agents');
  // an anchor in the link wins over the alias's own
  assert.equal(parseSettingsHash('#settings/reactions/avatar-pools').target, 'characters#avatar-pools');
  // case and percent-encoding are forgiven for the section
  assert.equal(parseSettingsHash('#settings/People').target, 'people');
  assert.equal(parseSettingsHash('#settings/%70eople').target, 'people');
});

test('parse: an unknown section is the default section, flagged unknown (the caller toasts)', () => {
  for (const h of ['#settings/nope', '#settings/myaddon', '#settings/__proto__', '#settings/constructor',
    '#settings/toString', '#settings/%E0%A4%A', '#settings/' + 'a'.repeat(300)]) {
    assert.deepEqual(parseSettingsHash(h), { section: null, anchor: '', target: null, known: false }, h);
  }
});

test('parse: a hostile section never survives (matched against SECTIONS, never used as a selector or HTML)', () => {
  for (const h of ['#settings/<script>alert(1)</script>', '#settings/%3Cscript%3E', '#settings/people"]',
    '#settings/people%22%5D', '#settings/[data-x]', '#settings/people%2Fmachine', '#settings/../account']) {
    const r = parseSettingsHash(h);
    assert.equal(r.section, null, h);
    assert.equal(r.target, null, h);
    assert.equal(r.known, false, h);
  }
});

test('parse: a hostile anchor is dropped, the section still opens', () => {
  for (const a of ['<img src=x onerror=1>', '%3Cb%3E', 'a"]', 'x y', 'a%2Fb', "'", '%E0%A4%A', 'b'.repeat(200)]) {
    assert.deepEqual(parseSettingsHash(`#settings/account/${a}`),
      { section: 'account', anchor: '', target: 'account', known: true }, a);
  }
  // more than one anchor segment: the extra ones are ignored, never joined into a selector
  assert.equal(parseSettingsHash('#settings/account/signin/x').target, 'account#signin');
});

// ------------------------------------------------------------------ route

function fakeWin(hash = '', { state = { view: 'bots' } } = {}) {
  const listeners = {};
  const calls = { push: 0, replace: [] };
  const win = {
    location: { pathname: '/', search: '', hash },
    history: {
      state,
      replaceState(s, _title, url) {
        calls.replace.push(url);
        this.state = s;
        const i = url.indexOf('#');
        win.location.hash = i < 0 ? '' : url.slice(i);
      },
      pushState() { calls.push += 1; },
    },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter((f) => f !== fn); },
    // a fragment navigation: the browser makes a new entry (state null) and fires hashchange
    navigate(h, { state: st = null } = {}) {
      const old = `http://x/${win.location.hash}`;
      win.location.hash = h;
      win.history.state = st;
      for (const fn of listeners.hashchange || []) fn({ oldURL: old, newURL: `http://x/${h}` });
    },
    listeners,
    calls,
  };
  return win;
}

function harness(hash, opts = {}) {
  const win = fakeWin(hash, opts);
  const log = [];
  let signedIn = opts.signedIn ?? true;
  const route = settingsHashRoute({
    win,
    open: (target) => { log.push(['open', target]); },
    close: () => { log.push(['close']); },
    isSignedIn: () => signedIn,
    allowed: opts.allowed,
    notify: (why) => { log.push(['notify', why]); },
    onReady: opts.onReady,
  });
  return { win, log, route, signIn() { signedIn = true; } };
}

test('route: a link that loads with the page waits for apply() (the hello frame), then opens once', () => {
  const h = harness('#settings/people/machine');
  assert.deepEqual(h.log, []);
  h.route.apply();
  assert.deepEqual(h.log, [['open', 'people#machine']]);
  h.route.apply();                 // a reconnect's hello: nothing pending any more
  assert.deepEqual(h.log, [['open', 'people#machine']]);
});

test('route: #settings opens the default (open(null)); an unknown section opens the default and notifies', () => {
  const a = harness('#settings');
  a.route.apply();
  assert.deepEqual(a.log, [['open', null]]);
  const b = harness('#settings/<script>');
  b.route.apply();
  assert.deepEqual(b.log, [['notify', 'unknown'], ['open', null]]);
});

test('route: before sign-in the link is kept and applied after sign-in', () => {
  const h = harness('#settings/system', { signedIn: false });
  h.route.apply();                 // the gate: nothing opens
  assert.deepEqual(h.log, []);
  h.signIn();
  h.route.apply();
  assert.deepEqual(h.log, [['open', 'system']]);
});

test('route: a hash typed while signed out replaces the kept one', () => {
  const h = harness('#settings/system', { signedIn: false });
  h.win.navigate('#settings/account');
  assert.deepEqual(h.log, []);
  h.signIn();
  h.route.apply();
  assert.deepEqual(h.log, [['open', 'account']]);
});

test('route: onReady, when given, is how the route learns the app is ready', () => {
  let ready = null;
  const h = harness('#settings/voice', { onReady: (fn) => { ready = fn; } });
  assert.equal(typeof ready, 'function');
  assert.deepEqual(h.log, []);
  ready();
  assert.deepEqual(h.log, [['open', 'voice']]);
});

test('route: a section the role may not see opens the default section (no notice: it exists)', () => {
  const h = harness('#settings/people/machine', { allowed: (id) => id !== 'people' });
  h.route.apply();
  assert.deepEqual(h.log, [['open', null]]);
});

test('route: a hash typed into a running app opens Settings at once', () => {
  const h = harness('');
  h.route.apply();
  assert.deepEqual(h.log, []);
  h.win.navigate('#settings/plugins/lookups');
  assert.deepEqual(h.log, [['open', 'plugins#lookups']]);
});

test('route: opening writes #settings/<section> with replaceState only, keeping history.state', () => {
  const h = harness('');
  h.route.apply();
  h.route.opened(null);            // the phone list
  assert.equal(h.win.location.hash, '#settings');
  h.route.opened('appearance');
  assert.equal(h.win.location.hash, '#settings/appearance');
  h.route.opened('people', 'machine');
  assert.equal(h.win.location.hash, '#settings/people/machine');
  h.route.opened('people', 'machine');            // unchanged: no write
  assert.equal(h.win.calls.replace.length, 3);
  assert.equal(h.win.calls.push, 0, 'never a history entry');
  assert.equal(h.win.history.state.view, 'bots', 'the app\'s own history state survives');
  h.route.opened('myaddon');                      // an add-in tab: not a section, bare #settings
  assert.equal(h.win.location.hash, '#settings');
  h.route.opened('people', 'a"]<b>');              // a junk anchor is not written
  assert.equal(h.win.location.hash, '#settings/people');
});

test('route: closing removes the hash (path and query kept), with replaceState', () => {
  const h = harness('');
  h.win.location.search = '?x=1';
  h.route.opened('system');
  h.route.closed();
  assert.equal(h.win.location.hash, '');
  assert.equal(h.win.calls.replace.at(-1), '/?x=1');
  assert.equal(h.win.calls.push, 0);
});

test('route: a close the route never saw open leaves the hash (boot and the sign-in gate hide the modal)', () => {
  const h = harness('#settings/system', { signedIn: false });
  h.route.closed();                // closeAllOverlays() at the gate
  assert.equal(h.win.location.hash, '#settings/system');
  assert.equal(h.win.calls.replace.length, 0);
  h.signIn();
  h.route.closed();                // reboot() before the hello frame
  h.route.apply();
  assert.deepEqual(h.log, [['open', 'system']]);
});

test('route: never fights #voice/... or #tool= (not written over on open, not cleared on close)', () => {
  for (const foreign of ['#voice/voices/main', '#tool=jobboard']) {
    const h = harness(foreign);
    h.route.apply();
    assert.deepEqual(h.log, [], 'a foreign hash opens nothing');
    h.route.opened('voice');
    h.route.closed();
    assert.equal(h.win.location.hash, foreign);
    assert.equal(h.win.calls.replace.length, 0);
    // and the voice link from Characters (location.hash = #voice/...) while Settings is open
    const g = harness('');
    g.route.opened('characters');
    g.win.navigate(foreign);
    g.route.opened('voice');
    assert.deepEqual(g.log, []);
    assert.equal(g.win.location.hash, foreign);
  }
});

test('route: removing the hash by hand closes Settings; other hash changes do not', () => {
  const h = harness('');
  h.route.opened('system');
  h.win.navigate('#tool=jobboard');
  assert.deepEqual(h.log, []);
  const g = harness('');
  g.route.opened('system');
  g.win.navigate('');
  assert.deepEqual(g.log, [['close']]);
});

test('route: Back onto an entry the route wrote is history, not a request (no reopen, stale hash dropped)', () => {
  // phone: list (#settings) -> section pushed an entry (copy of #settings + marker) -> Back
  const h = harness('');
  h.route.opened(null);
  const marked = h.win.history.state;
  h.route.opened('appearance');
  // Back to the list entry while Settings stays open: ignored
  h.win.navigate('#settings', { state: marked });
  assert.deepEqual(h.log, []);
  // Settings closed, then the closer's history.back() lands on an older entry still holding the hash
  h.route.closed();
  h.win.navigate('#settings', { state: marked });
  assert.deepEqual(h.log, [], 'Settings does not come back');
  assert.equal(h.win.location.hash, '', 'the stale hash is removed');
});

test('route: dispose() stops listening', () => {
  const h = harness('');
  h.route.dispose();
  h.win.navigate('#settings/system');
  assert.deepEqual(h.log, []);
});

test('route: a failing open() never throws out of the hash handler', () => {
  const win = fakeWin('');
  const route = settingsHashRoute({ win, open: () => { throw new Error('boom'); }, close() {}, isSignedIn: () => true });
  route.apply();
  assert.doesNotThrow(() => win.navigate('#settings/system'));
});

// ------------------------------------------------------------------ 4.3 FX1: the phone's pushed entry
// A stack history that behaves like the browser: pushState copies nothing but what it
// is given, back() moves to the older entry and fires hashchange when the URL's
// fragment differs. This is the phone sequence the INT-1 proof caught: boot's
// replaceState({view:'bots'}) leaves the deep-link entry unmarked, the section pushes
// a 'settings-pane' entry, the closer pops it with history.back().
function stackWin(hash) {
  const listeners = {};
  const entries = [{ state: null, hash }];
  let at = 0;
  const win = {
    location: { pathname: '/', search: '', hash },
    history: {
      get state() { return entries[at].state; },
      get length() { return entries.length; },
      replaceState(s, _t, url) {
        const h = url === undefined ? win.location.hash : (url.includes('#') ? url.slice(url.indexOf('#')) : '');
        entries[at] = { state: s, hash: h };
        win.location.hash = h;
      },
      pushState(s, _t, url) {
        const h = url === undefined ? win.location.hash : (url.includes('#') ? url.slice(url.indexOf('#')) : '');
        entries.splice(at + 1);
        entries.push({ state: s, hash: h });
        at += 1;
        win.location.hash = h;
      },
      back() {
        if (at === 0) return;
        const old = win.location.hash;
        at -= 1;
        win.location.hash = entries[at].hash;
        for (const fn of listeners.popstate || []) fn({ state: entries[at].state });
        if (old !== entries[at].hash) {
          for (const fn of listeners.hashchange || []) fn({ oldURL: `http://x/${old}`, newURL: `http://x/${entries[at].hash}` });
        }
      },
    },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter((f) => f !== fn); },
    entries,
  };
  return win;
}

function phoneApp(hash) {
  const win = stackWin(hash);
  const log = [];
  let open = false;
  let pushed = false;
  // boot: anchor the history root on the bots screen (main.js), the hash stays, the mark does not
  win.history.replaceState({ view: 'bots' }, '');
  const app = {};
  const route = settingsHashRoute({
    win,
    open: (target) => { log.push(['open', target]); app.openSection(target); },
    close: () => { log.push(['close']); app.close(); },
    isSignedIn: () => true,
  });
  app.openSection = (target) => {
    open = true;
    const id = target ? String(target).split('#')[0] : 'appearance';
    route.opened(id);                                  // setSettingsTab
    if (!pushed) {                                     // showSettingsView(true): historyPush
      pushed = true;
      win.history.pushState({ ...(win.history.state || {}), ext: 'settings-pane', extSeq: 1 }, '');
    }
  };
  app.close = () => {                                  // ✕ / Esc: hide the backdrop, the observer runs
    if (!open) return;
    open = false;
    const wasPushed = pushed;
    pushed = false;
    if (wasPushed && win.history.state && win.history.state.extSeq === 1) {
      // pop() runs history.back(), which is async in a browser: closed() lands first
      route.closed();
      win.history.back();
    } else {
      route.closed();
    }
  };
  app.back = () => {                                   // hardware Back on the section: to the list
    pushed = false;
    win.history.back();
    if (open) route.opened(null);
  };
  return { win, log, route, app, isOpen: () => open };
}

test('FX1 phone: a deep link opened at boot closes by ✕ / Esc and never reopens', () => {
  const p = phoneApp('#settings/account');
  const len0 = p.win.history.length;
  p.route.apply();
  assert.deepEqual(p.log, [['open', 'account']]);
  assert.equal(p.win.history.length, len0 + 1, 'the section pushed its Back entry');
  p.app.close();
  assert.equal(p.isOpen(), false, 'Settings stays closed');
  assert.deepEqual(p.log, [['open', 'account']], 'no reopen after the pane entry is popped');
  assert.equal(p.win.location.hash, '', 'no stale #settings hash');
});

test('FX1 phone: reload with Settings open (entry holds the hash + an old pane state) closes cleanly', () => {
  // the reloaded entry is the old pushed one: boot replaceState drops its marks
  const p = phoneApp('#settings/people');
  p.route.apply();
  p.app.close();
  assert.deepEqual(p.log, [['open', 'people']]);
  assert.equal(p.isOpen(), false);
  assert.equal(p.win.location.hash, '');
  // open again from the gear and close: still no reopen, history does not grow
  const len = p.win.history.length;
  p.app.openSection('system');
  p.app.close();
  assert.equal(p.isOpen(), false);
  assert.equal(p.win.history.length, len, 'the new pane entry replaces the popped one (no growth)');
  assert.equal(p.win.location.hash, '');
});

test('FX1 phone: Back from a deep-linked section goes to the list, then ✕ closes for good', () => {
  const p = phoneApp('#settings/account');
  p.route.apply();
  p.app.back();
  assert.equal(p.isOpen(), true, 'Back from a section is the list, not a close');
  assert.equal(p.win.location.hash, '#settings');
  p.app.close();
  assert.equal(p.isOpen(), false);
  assert.deepEqual(p.log, [['open', 'account']]);
  assert.equal(p.win.location.hash, '');
});
