// Things the 4.0 rewrite had dropped or half-wired for non-Admin profiles (Opus check 2).

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (p) => readFileSync(join(STATIC, p), 'utf8');
const LOCALES = ['ar', 'de', 'en', 'es', 'fr', 'ja', 'pt', 'zh'];

test('own-bot editor: the AI build keeps the description the model wrote and the editor sends it', async () => {
  const be = await import('../static/js/bot-editor.js?v=1');
  const f = be.draftToForm({ name: 'Mochi', emoji: '🍡', description: 'A cheerful home-cooking helper.', persona: 'Be brief.' });
  assert.equal(f.description, 'A cheerful home-cooking helper.');
  assert.equal(be.botPayload({ name: 'Mochi', description: '  A helper.  ', persona: '' }).description, 'A helper.');
  const orig = { id: 'p1', name: 'Mochi', emoji: '', description: 'A helper.', persona: '' };
  assert.equal('description' in be.botPayload({ name: 'Mochi', emoji: '', description: 'A helper.', persona: '' }, orig), false);
  assert.equal(be.botPayload({ name: 'Mochi', emoji: '', description: '', persona: '' }, orig).description, '');
  assert.equal(be.botPayload({ name: 'X', description: 'y'.repeat(900) }).description.length, be.BOT_DESCRIPTION_MAX);
  const src = read('js/bot-editor.js');
  assert.match(src, /field\(t\('editor\.description'\), descIn/);
  // No Voice field with nothing in it when the voice add-on is not installed.
  assert.match(src, /options\.voiceOn !== false \|\| form\.voice \? field\(t\('editor\.voice'\)/);
  for (const l of LOCALES) assert.equal(typeof JSON.parse(read(`locales/${l}.json`)).editor.description, 'string', l);
});

test('My bots: a household bot an Admin hid is not offered for copying', () => {
  const src = read('js/my-bots.js');
  assert.match(src, /household\.filter\(\(b\) => !isHidden\(b\) && canCopyBot\(b, d, mine\)\)/);
});

test('File Server: an upload-only profile is never asked for the list (no 403 after a good upload)', () => {
  const main = read('js/main.js');
  const fn = main.slice(main.indexOf('async function renderFileServer()'));
  assert.ok(fn.indexOf("if (!sessionCan('files_read'))") > 0);
  assert.ok(fn.indexOf("if (!sessionCan('files_read'))") < fn.indexOf('api.files()'));
  for (const l of LOCALES) assert.equal(typeof JSON.parse(read(`locales/${l}.json`)).files.upload_only, 'string', l);
});

test('chat menu: "Model & thinking" and the picker only for a character manager; Recovery restore only for an Admin', () => {
  const main = read('js/main.js');
  assert.match(main, /if \(modelItem\) modelItem\.hidden = !sessionCan\('manage_characters'\);/);
  const omp = main.slice(main.indexOf('async function openModelPicker()'));
  assert.ok(omp.indexOf("if (!sessionCan('manage_characters'))") < omp.indexOf('toggleModelPicker(true)') || omp.indexOf('toggleModelPicker(true)') < 0);
  assert.match(main, /if \(adminPart\) adminPart\.hidden = !canAdmin\(\);/);
  const html = read('index.html');
  const part = html.slice(html.indexOf('<div id="recover-admin">'));
  assert.ok(part.indexOf('id="recover-all"') > 0 && part.indexOf('id="browse-sessions"') > 0);
  assert.ok(html.indexOf('<div id="recover-admin">') > html.indexOf('data-export="html"'));
});

test('rail: the + tile says "no bots yet" only when there are none', () => {
  const main = read('js/main.js');
  assert.match(main, /'aria-label': hasMine \? t\('profiles\.rail_make_bot'\) :/);
  assert.match(main, /railMakeBotButton\(hasMine\)/);
});

test('Image Studio: Send is hidden where the server refuses it, and an own bot\'s avatar goes to its own route', () => {
  const src = readFileSync(join(HERE, '..', '..', 'apps', 'imagestudio', 'static', 'studio.js'), 'utf8');
  assert.match(src, /if \(S\.info && S\.info\.can_send === false\) actions\.children\[3\]\.hidden = true;/);
  assert.match(src, /url = `\/api\/me\/characters\/\$\{encodeURIComponent\(bot\.value\)\}\/avatar\?\$\{q\}`;/);
  assert.match(src, /if \(!targets\.bots\.length\) \{ setStatus\(t\('avatar\.no_bots'\), true\); return; \}/);
  for (const l of LOCALES) {
    const L = JSON.parse(readFileSync(join(HERE, '..', '..', 'apps', 'imagestudio', 'locales', `${l}.json`), 'utf8'));
    assert.equal(typeof L['avatar.no_bots'], 'string', l);
  }
});
