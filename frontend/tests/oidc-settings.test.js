// 4.3 WP-A13 (issue #24): outside sign-in (OIDC) in the front end.
//   js/oidc-settings.js  the Owner's settings card, linkButtonFor(profile) for People
//   js/signin.js         "Sign in with <provider>" under the tiles when it is on

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

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const oidcMod = await import('../static/js/oidc-settings.js');

const t = (key, vars = {}) => {
  let v = key.split('.').reduce((o, k) => (o ? o[k] : undefined), EN);
  if (v && typeof v === 'object' && v.other !== undefined) v = vars.count === 1 && v.one !== undefined ? v.one : v.other;
  if (typeof v !== 'string') return key;
  for (const [k, x] of Object.entries(vars)) v = v.replaceAll(`{${k}}`, String(x));
  return v;
};
const tick = () => new Promise((r) => setTimeout(r, 0));

function makeDom() {
  const win = new jsdom.JSDOM('<!doctype html><body><div id="host"></div></body>', { url: 'https://chat.example.net/' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win); put('document', win.document); put('Node', win.Node); put('HTMLElement', win.HTMLElement);
  put('localStorage', win.localStorage); put('navigator', win.navigator); put('location', win.location);
  const el = (tag, attrs = {}, kids = []) => {
    const n = win.document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else n.setAttribute(k, v === true ? '' : v);
    }
    for (const c of [].concat(kids)) if (c != null) n.append(c);
    return n;
  };
  return { win, el };
}

const OWNER = { profile: { id: 'o1', name: 'Boss', protected: true } };
const ADMIN = { profile: { id: 'a1', name: 'Ops', protected: false } };
const USER = { profile: { id: 'm1', name: 'Sam', protected: false } };
const CFG = {
  enabled: true, active: true, issuer: 'https://idp.example', client_id: 'dispatch', scopes: 'openid email',
  label: 'Family ID', display_label: 'Family ID', has_secret: true, redirect_uri: 'https://chat.example.net/api/oidc/callback',
  public_url_ok: true, links: 2,
};

// ------------------------------------------------------------- pure helpers

test('the card is the protected Admin\'s alone', () => {
  assert.equal(oidcMod.showsOidcCard(OWNER), true);
  assert.equal(oidcMod.showsOidcCard(ADMIN), false);
  assert.equal(oidcMod.showsOidcCard(USER), false);
  assert.equal(oidcMod.showsOidcCard(null), false);
});

test('configBody: an empty secret keeps the stored one; scopes are normalised', () => {
  const b = oidcMod.configBody({ enabled: 1, issuer: ' https://idp.example ', client_id: 'x', client_secret: '  ', scopes: 'openid   email', label: ' Fam ' });
  assert.deepEqual(b, { enabled: true, issuer: 'https://idp.example', client_id: 'x', scopes: 'openid email', label: 'Fam' });
  assert.equal(oidcMod.configBody({ client_secret: 's3' }).client_secret, 's3');
  assert.equal(oidcMod.configBody({ clear_secret: true }).clear_secret, true);
  assert.equal(oidcMod.configBody({}).scopes, 'openid email profile');
});

// ------------------------------------------------------------- the card

test('card: not mounted for anyone but the Owner', { skip: dom.skip }, () => {
  const { win, el } = makeDom();
  const host = win.document.getElementById('host');
  const calls = [];
  const out = oidcMod.renderOidcCard(host, { t, el, me: ADMIN, request: async (...a) => { calls.push(a); return CFG; } });
  assert.equal(out, null);
  assert.equal(host.querySelector('.oidc-card'), null);
  assert.equal(calls.length, 0, 'not even a GET');
});

test('card: loads the config, never shows a secret, saves with PUT', { skip: dom.skip }, async () => {
  const { win, el } = makeDom();
  const host = win.document.getElementById('host');
  const calls = [];
  const toasts = [];
  const request = async (method, url, body) => { calls.push({ method, url, body }); return { ...CFG, ...(body || {}) }; };
  const card = oidcMod.renderOidcCard(host, { t, el, me: OWNER, request, toast: (m) => toasts.push(m) });
  await card.ready;
  assert.deepEqual(calls[0], { method: 'GET', url: '/api/oidc/config', body: undefined });
  assert.equal(host.querySelector('#oidc-issuer').value, 'https://idp.example');
  assert.equal(host.querySelector('#oidc-client-secret').value, '');
  assert.equal(host.querySelector('#oidc-client-secret').type, 'password');
  assert.equal(host.querySelector('#oidc-client-secret').placeholder, EN.oidc.secret_saved);
  assert.equal(host.querySelector('.oidc-redirect').textContent, CFG.redirect_uri);
  assert.equal(host.querySelector('.oidc-status').textContent, 'On. 2 profiles are linked.');
  assert.ok(host.querySelector('.oidc-warn').hidden);

  host.querySelector('#oidc-label').value = 'Home';
  host.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick(); await tick();
  const put = calls.find((c) => c.method === 'PUT');
  assert.equal(put.url, '/api/oidc/config');
  assert.equal(put.body.label, 'Home');
  assert.equal('client_secret' in put.body, false, 'an untouched secret field keeps the stored secret');
  assert.deepEqual(toasts, [EN.oidc.saved]);
});

test('card: warns when the public address is not https', { skip: dom.skip }, async () => {
  const { win, el } = makeDom();
  const host = win.document.getElementById('host');
  const card = oidcMod.renderOidcCard(host, { t, el, me: OWNER, request: async () => ({ ...CFG, active: false, public_url_ok: false, redirect_uri: null }) });
  await card.ready;
  assert.equal(host.querySelector('.oidc-warn').hidden, false);
  assert.equal(host.querySelector('.oidc-redirect').textContent, EN.oidc.no_public_url);
  assert.equal(host.querySelector('.oidc-status').textContent, EN.oidc.status_off);
});

// ------------------------------------------------------------- linkButtonFor

test('linkButtonFor: off, or somebody else\'s profile for a non-Owner -> no button', { skip: dom.skip }, () => {
  const { el } = makeDom();
  const sam = { id: 'm1', name: 'Sam' };
  assert.equal(oidcMod.linkButtonFor(sam, { me: USER, enabled: false, links: [], t, el }), null);
  assert.equal(oidcMod.linkButtonFor({ id: 'o1' }, { me: USER, enabled: true, links: [], t, el }), null);
  assert.equal(oidcMod.linkButtonFor(sam, { me: ADMIN, enabled: true, links: [], t, el }), null, 'a second Admin cannot link others');
  assert.ok(oidcMod.linkButtonFor(sam, { me: USER, enabled: true, links: [], t, el }), 'your own profile');
  assert.ok(oidcMod.linkButtonFor(sam, { me: OWNER, enabled: true, links: [], t, el }), 'the Owner links anyone');
});

test('linkButtonFor: Link navigates to the provider round trip', { skip: dom.skip }, () => {
  const { el } = makeDom();
  const went = [];
  const b = oidcMod.linkButtonFor({ id: 'm 1' }, { me: OWNER, enabled: true, links: [], t, el, navigate: (u) => went.push(u) });
  assert.equal(b.textContent, EN.oidc.link);
  b.click();
  assert.deepEqual(went, ['/api/oidc/start?link=m%201']);
  assert.equal(b.disabled, true);
});

test('linkButtonFor: Unlink calls DELETE and reports back', { skip: dom.skip }, async () => {
  const { el } = makeDom();
  const calls = [];
  let changed = 0;
  const b = oidcMod.linkButtonFor({ id: 'm1' }, {
    me: USER, enabled: true, t, el, links: [{ profile_id: 'm1', email_at_link: 'sam@example.org' }],
    request: async (m, u) => { calls.push(`${m} ${u}`); return { ok: true, removed: 1 }; },
    onChange: () => { changed += 1; },
  });
  assert.equal(b.textContent, EN.oidc.unlink);
  assert.equal(b.title, 'Linked to sam@example.org');
  b.click();
  await tick();
  assert.deepEqual(calls, ['DELETE /api/oidc/links/m1']);
  assert.equal(changed, 1);
});

// ------------------------------------------------------------- signin.js

async function bootSignin() {
  const { win } = makeDom();
  globalThis.fetch = async (u) => {
    if (String(u).includes('/locales/en.json')) return { ok: true, status: 200, json: async () => EN, headers: new win.Headers() };
    return { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  return win;
}

test('sign-in: the provider button appears only when outside sign-in is on', { skip: dom.skip }, async () => {
  const win = await bootSignin();
  const { renderSignIn, oidcStartUrl } = await import(`../static/js/signin.js?t=${Math.random()}`);
  const host = win.document.getElementById('host');
  const profiles = [{ id: 'm1', name: 'Sam', role: 'user', avatar: '', credential: 'pin' }];
  renderSignIn(host, { profiles, onSubmit: async () => {} });
  assert.equal(host.querySelector('.signin-oidc'), null);
  assert.ok(!host.textContent.includes('null'));
  renderSignIn(host, { profiles, onSubmit: async () => {}, oidc: { enabled: false, label: 'X' } });
  assert.equal(host.querySelector('.signin-oidc'), null);

  const went = [];
  renderSignIn(host, { profiles, onSubmit: async () => {}, oidc: { enabled: true, label: 'Family ID' }, remember: () => true, navigate: (u) => went.push(u) });
  const b = host.querySelector('.signin-oidc');
  assert.equal(b.textContent, 'Sign in with Family ID');
  b.click();
  assert.deepEqual(went, ['/api/oidc/start?remember=1']);
  assert.equal(oidcStartUrl(), '/api/oidc/start');
  assert.equal(oidcStartUrl({ link: 'a b' }), '/api/oidc/start?link=a+b');
});
