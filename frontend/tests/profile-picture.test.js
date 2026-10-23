// WP-AVATAR: uploading a profile picture (js/profile-picture.js), rendering a picture
// (profiles.js avatarSpec, No-Image Mode) and the People editor not clearing it by accident.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const HTML = readFileSync(join(STATIC, 'index.html'), 'utf8');
const EN = JSON.parse(readFileSync(join(STATIC, 'locales', 'en.json'), 'utf8'));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const URL1 = '/api/profile-avatars/abc-0123456789abcdef.webp';
let calls = [];
let routes = {};

async function setup(extra = {}) {
  const win = new jsdom.JSDOM(HTML, { url: 'http://127.0.0.1:8803/', runScripts: 'outside-only' }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win); put('document', win.document); put('location', win.location);
  put('navigator', win.navigator); put('localStorage', win.localStorage);
  put('Node', win.Node); put('HTMLElement', win.HTMLElement); put('HTMLInputElement', win.HTMLInputElement);
  put('FormData', win.FormData); put('File', win.File);
  calls = [];
  routes = {
    'GET /static/locales/en.json': () => [200, EN],
    'POST /api/me/avatar': () => [200, { id: 'abc', name: 'Uma', avatar: URL1 }],
    'DELETE /api/me/avatar': () => [200, { id: 'abc', name: 'Uma', avatar: '' }],
    ...extra,
  };
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    calls.push({ key, body: opts.body });
    const h = routes[key];
    const [status, out] = h ? h() : [404, { detail: 'Not Found' }];
    return { ok: status >= 200 && status < 300, status, headers: new win.Headers(), json: async () => out };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  calls.length = 0;
  const pp = await import('../static/js/profile-picture.js?v=1');
  return { win, pp };
}

const tick = async (n = 8) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

// ------------------------------------------------------------------ pure

test('avatarSpec: a served picture is an image, an emoji a glyph, and No-Image Mode falls back to the initial', async () => {
  const P = await import('../static/js/profiles.js?v=3');
  assert.deepEqual(P.avatarSpec({ name: 'Uma', avatar: URL1 }), { kind: 'img', src: URL1 });
  assert.deepEqual(P.avatarSpec({ name: 'Uma', avatar: URL1 }, { noImages: true }), { kind: 'initial', text: 'U' });
  assert.deepEqual(P.avatarSpec({ name: 'Uma', avatar: '🦊' }, { noImages: true }), { kind: 'glyph', text: '🦊' });
  assert.equal(P.avatarSpec({ name: 'Uma', avatar: '//evil.example/x.webp' }, { noImages: true }).kind, 'initial');
  assert.equal(P.avatarSpec({ name: 'Uma', avatar: '/ab' }, { noImages: true }).kind, 'initial');
});

test('pictureProblem: only png / jpeg / webp / gif, 5 MB at most', { skip: dom.skip }, async () => {
  const { pp } = await setup();
  assert.equal(pp.pictureProblem({ type: 'image/png', size: 100 }), '');
  assert.equal(pp.pictureProblem({ type: 'image/webp', size: pp.MAX_BYTES }), '');
  assert.equal(pp.pictureProblem({ type: 'image/gif', size: pp.MAX_BYTES + 1 }), 'picture.err_size');
  assert.equal(pp.pictureProblem({ type: 'image/svg+xml', size: 10 }), 'picture.err_type');
  assert.equal(pp.pictureProblem({ type: 'text/plain', size: 10 }), 'picture.err_type');
  assert.equal(pp.pictureProblem(null), 'picture.err_type');
  assert.equal(pp.isUploadedPicture(URL1), true);
  assert.equal(pp.isUploadedPicture('🦊'), false);
  assert.equal(pp.isUploadedPicture('/static/avatars/a.png'), false);
});

test('pictureError speaks the server codes plainly', { skip: dom.skip }, async () => {
  const { pp } = await setup();
  assert.match(pp.pictureError({ code: 'avatar_too_large' }), /too large/);
  assert.match(pp.pictureError({ code: 'avatar_invalid' }), /PNG, JPEG, WebP or GIF/);
  assert.match(pp.pictureError({ code: 'lock_target_fence' }), /Only an Admin/);
  assert.match(pp.pictureError({ message: '500: boom' }), /Could not save the picture: boom/);
});

// ------------------------------------------------------------------ the row

test('the row shows the visibility note, uploads multipart to the base route and reports the new profile', { skip: dom.skip }, async () => {
  const { win, pp } = await setup();
  const seen = [];
  const row = pp.pictureRow({ profile: { id: 'abc', name: 'Uma', avatar: '' }, base: '/api/me/avatar', toast: () => {},
    onChanged: async (p) => { seen.push(p.avatar); } });
  win.document.body.append(row);
  assert.match(row.textContent, /Your picture is visible to everyone who uses this DisPatch, including on the sign-in screen\./);
  assert.equal(row.querySelector('[data-pp="remove"]').hidden, true);          // nothing to remove yet
  const input = row.querySelector('input[type=file]');
  assert.match(input.accept, /image\/png/);
  const file = new win.File([new Uint8Array(1000)], 'me.png', { type: 'image/png' });
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  input.dispatchEvent(new win.Event('change', { bubbles: true }));
  await tick();
  const post = calls.filter((c) => c.key === 'POST /api/me/avatar');
  assert.equal(post.length, 1);
  assert.ok(post[0].body instanceof win.FormData && post[0].body.get('file'));
  assert.deepEqual(seen, [URL1]);
  assert.equal(row.querySelector('img.pp-preview').getAttribute('src'), URL1);
  assert.equal(row.querySelector('[data-pp="remove"]').hidden, false);
  assert.equal(row.querySelector('[data-pp="upload"]').textContent, 'Change picture…');
  // Remove goes back to the initial.
  row.querySelector('[data-pp="remove"]').dispatchEvent(new win.Event('click', { bubbles: true }));
  await tick();
  assert.equal(calls.filter((c) => c.key === 'DELETE /api/me/avatar').length, 1);
  assert.equal(row.querySelector('img.pp-preview'), null);
  assert.equal(row.querySelector('.pp-preview--text').textContent, 'U');
});

test('a wrong type or an oversize file is refused before any request', { skip: dom.skip }, async () => {
  const { win, pp } = await setup();
  const row = pp.pictureRow({ profile: { id: 'abc', name: 'Uma', avatar: '' }, base: '/api/me/avatar' });
  const input = row.querySelector('input[type=file]');
  const pick = (f) => { Object.defineProperty(input, 'files', { value: [f], configurable: true }); input.dispatchEvent(new win.Event('change')); };
  pick(new win.File(['x'], 'a.svg', { type: 'image/svg+xml' }));
  await tick();
  assert.match(row.querySelector('.pp-status').textContent, /PNG, JPEG, WebP or GIF/);
  const big = new win.File([new Uint8Array(10)], 'b.png', { type: 'image/png' });
  Object.defineProperty(big, 'size', { value: pp.MAX_BYTES + 1 });
  pick(big);
  await tick();
  assert.match(row.querySelector('.pp-status').textContent, /too large/);
  assert.equal(calls.length, 0);
});

test('a server refusal is shown, the picture stays', { skip: dom.skip }, async () => {
  const { win, pp } = await setup({
    'POST /api/me/avatar': () => [422, { detail: 'x', error: { code: 'avatar_invalid', message: 'That file is not a picture DisPatch can read.' } }],
  });
  const row = pp.pictureRow({ profile: { id: 'abc', name: 'Uma', avatar: URL1 }, base: '/api/me/avatar' });
  const input = row.querySelector('input[type=file]');
  Object.defineProperty(input, 'files', { value: [new win.File(['x'], 'a.png', { type: 'image/png' })], configurable: true });
  input.dispatchEvent(new win.Event('change'));
  await tick();
  assert.match(row.querySelector('.pp-status').textContent, /PNG, JPEG, WebP or GIF/);
  assert.equal(row.querySelector('img.pp-preview').getAttribute('src'), URL1);
});

test('the shared default profile, seen by a non-Admin, shows the reason and offers no buttons that work', { skip: dom.skip }, async () => {
  const { win, pp } = await setup();
  const host = win.document.createElement('div');
  host.innerHTML = '<section class="acct-who-group"></section>';
  pp.renderProfilePicture(host, { state: { auth: { role: 'user', profile: { id: 'abc', name: 'Lobby', avatar: '', shared: true } } } });
  const group = host.querySelector('[data-anchor="picture"]');
  assert.ok(group && host.firstElementChild.nextElementSibling === group);        // right after the who card
  assert.match(group.textContent, /Only an Admin changes its picture/);
  assert.equal(group.querySelector('[data-pp="upload"]').disabled, true);
  pp.renderProfilePicture(host, { state: { auth: { role: 'admin', profile: { id: 'abc', name: 'Lobby', avatar: '', shared: true } } } });
  assert.equal(host.querySelectorAll('[data-anchor="picture"]').length, 1);       // re-mount replaces
  assert.equal(host.querySelector('[data-pp="upload"]').disabled, false);
});

test('No-Image Mode: the preview is the initial, no <img> is created', { skip: dom.skip }, async () => {
  const { win, pp } = await setup();
  win.localStorage.setItem('dispatch-nim', '1');
  const nim = await import('../static/js/nim.js?v=6');
  nim.setNim?.(true);
  const node = pp.previewNode({ name: 'Uma', avatar: URL1 });
  assert.notEqual(node.tagName, 'IMG');
  assert.equal(node.textContent, 'U');
  nim.setNim?.(false);
});

// ------------------------------------------------------------------ wiring

test('People: the emoji box never clears an uploaded picture; Admins get the picture row only on an existing profile', async () => {
  const P = await import('../static/js/people.js?v=5');
  const person = { id: 'abc', name: 'Uma', role: 'user', avatar: URL1 };
  assert.deepEqual(P.patchBody(person, { name: 'Uma', avatar: '' }).patch, {});
  assert.deepEqual(P.patchBody(person, { name: 'Uma', avatar: '🦊' }).patch, { avatar: '🦊' });
  assert.deepEqual(P.patchBody({ ...person, avatar: '🦊' }, { name: 'Uma', avatar: '' }).patch, { avatar: '' });
  const src = readFileSync(join(STATIC, 'js', 'people.js'), 'utf8');
  assert.match(src, /creating\s*\?\s*el\('p', \{ class: 'people-field-hint', text: t\('picture\.save_first'\)/);
  assert.match(src, /\/api\/people\/\$\{encodeURIComponent\(person\.id\)\}\/avatar/);
});

test('Settings -> Account runs the picture hook, main.js hands it the shell, and every key exists', async () => {
  const nav = readFileSync(join(STATIC, 'js', 'settings-nav.js'), 'utf8');
  assert.match(nav, /account: \[renderAccountReset, renderProfilePicture\]/);
  const main = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
  assert.match(main, /initPeoplePerms, initProfilePicture\]/);
  assert.match(main, /onProfileChanged: async \(\) => \{ await refreshAuthQuiet\(\); paintProfileChrome\(\); paintAccountWho\(\); \}/);
  for (const k of ['title', 'note', 'upload', 'change', 'remove', 'uploading', 'saved', 'removed', 'formats',
    'err_type', 'err_size', 'err_generic', 'shared_admin_only', 'save_first']) {
    assert.equal(typeof EN.picture[k], 'string', k);
  }
  assert.equal(EN.picture.note, 'Your picture is visible to everyone who uses this DisPatch, including on the sign-in screen.');
  const css = readFileSync(join(STATIC, 'app.css'), 'utf8');
  assert.match(css, /\.pp-actions \.ui-btn \{ min-height: 44px/);
});

// ------------------------------------------------------------------ the face picker

test('with the face picker, the square placed is uploaded with the picture; closing it uploads nothing', { skip: dom.skip }, async () => {
  const Q = '?crop_x=0.25&crop_y=0&crop_size=0.5';
  const { win, pp } = await setup({ [`POST /api/me/avatar${Q}`]: () => [200, { id: 'abc', name: 'Uma', avatar: URL1 }] });
  const asked = [];
  let answer = { x: 0.25, y: 0, size: 0.5 };
  const row = pp.pictureRow({ profile: { id: 'abc', name: 'Uma', avatar: '' }, base: '/api/me/avatar',
    chooseFace: async (file, name, opts) => { asked.push([file.name, name, opts && opts.hint]); return answer; } });
  win.document.body.append(row);
  const input = row.querySelector('input[type=file]');
  const pick = () => {
    Object.defineProperty(input, 'files', { value: [new win.File([new Uint8Array(10)], 'me.png', { type: 'image/png' })], configurable: true });
    input.dispatchEvent(new win.Event('change'));
  };
  pick();
  await tick();
  assert.deepEqual(asked, [['me.png', 'Uma', EN.picture.crop_hint]]);
  assert.deepEqual(calls.map((c) => c.key), [`POST /api/me/avatar${Q}`]);
  assert.equal(row.querySelector('img.pp-preview').getAttribute('src'), URL1);
  // Closed without saving: no request, the picture and the status stay as they were.
  answer = null;
  calls.length = 0;
  pick();
  await tick();
  assert.equal(asked.length, 2);
  assert.equal(calls.length, 0);
  assert.equal(row.querySelector('img.pp-preview').getAttribute('src'), URL1);
});

test('Account and People hand the row the shell face picker; Esc and a lock close the picker alone and settle it', async () => {
  const pp = readFileSync(join(STATIC, 'js', 'profile-picture.js'), 'utf8');
  assert.match(pp, /chooseFace: d\.chooseFace/);
  const people = readFileSync(join(STATIC, 'js', 'people.js'), 'utf8');
  assert.match(people, /chooseFace: ctx\.chooseFace/);
  assert.match(people, /chooseFace: deps\.chooseFace/);
  const main = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
  assert.match(main, /mountPeopleSettings\(pane, \{\s*toast, confirm: \(msg, o\) => uiConfirm\(msg, o\), role: state\.role, chooseFace,/);
  // Esc: the picker first, before anything that would close Settings under it.
  const esc = main.slice(main.indexOf("if (e.key === 'Escape') {\n      const lb"));
  assert.ok(esc.indexOf('if (isCropOpen()) { closeCropModal(); return; }') > 0);
  assert.ok(esc.indexOf('if (isCropOpen()) { closeCropModal(); return; }') < esc.indexOf('closeBotManager();'));
  // A lock / profile switch settles a waiting picker instead of only hiding it.
  const sweep = main.slice(main.indexOf('function closeAllOverlays()'), main.indexOf('closeToolPanes();', main.indexOf('function closeAllOverlays()')));
  assert.match(sweep, /closeCropModal\(\);/);
  assert.doesNotMatch(sweep, /'crop-backdrop'/);
  // Focus moves into the dialog so an Esc never reaches the editor under it.
  assert.match(main, /crop\.returnFocus = document\.activeElement;\s*try \{ dom\['crop-save'\]\.focus/);
  for (const loc of ['ar', 'de', 'en', 'es', 'fr', 'ja', 'pt', 'zh']) {
    const L = JSON.parse(readFileSync(join(STATIC, 'locales', `${loc}.json`), 'utf8'));
    assert.equal(typeof L.picture.crop_hint, 'string', loc);
  }
});
