// i18n.mergeBundle (js/i18n.js): an installed extension's strings, merged
// under the shell's (CONTRACTS §E.1). An extension can never override a shell
// key; a key its active bundle lacks falls back to its own English bundle,
// then to the key itself; unmounting it drops its strings.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };

const HERE = dirname(fileURLToPath(import.meta.url));
const LOC = join(HERE, '..', 'static', 'locales');
const en = JSON.parse(readFileSync(join(LOC, 'en.json'), 'utf8'));
const de = JSON.parse(readFileSync(join(LOC, 'de.json'), 'utf8'));

test('merge precedence, fallbacks, removal and language switches', dom, async () => {
  const win = new jsdom.JSDOM('<!doctype html><html><body><p id="p" data-i18n="drive.title">x</p></body></html>').window;
  const keep = { document: globalThis.document, window: globalThis.window, fetch: globalThis.fetch };
  globalThis.document = win.document;
  globalThis.window = win;
  globalThis.fetch = async (url) => ({ ok: true, json: async () => (String(url).includes('/de.json') ? de : en) });
  const warn = console.warn;
  console.warn = () => {};
  try {
    const i18n = await import(`../static/js/i18n.js?t=${Math.random()}`);
    await i18n.init('de');
    assert.equal(i18n.lang(), 'de');
    i18n.mergeBundle('voice', {
      lang: 'de',
      dict: { drive: { title: 'Sprachchat', only_de: 'nur de' }, common: { cancel: 'GEKAPERT' } },
      en: { drive: { title: 'Voice chat', only_en: 'English only line' } },
    });
    assert.equal(i18n.t('drive.title'), 'Sprachchat', 'the add-on active bundle');
    assert.equal(i18n.t('drive.only_en'), 'English only line', 'falls back to the add-on English bundle');
    assert.equal(i18n.t('common.cancel'), de.common.cancel, 'a shell key can never be overridden');
    assert.equal(i18n.t('drive.nowhere'), 'Nowhere', 'then the key, humanized');
    assert.equal(i18n.has('drive.only_de'), true);
    assert.equal(i18n.has('drive.only_en'), false, 'has() asks the ACTIVE language only');
    // applyDom sees merged strings too (data-i18n in an add-on's markup).
    i18n.applyDom(win.document);
    assert.equal(win.document.getElementById('p').textContent, 'Sprachchat');
    // A bundle for another language does not leak into this one.
    i18n.mergeBundle('voice', { lang: 'fr', dict: { drive: { title: 'Chat vocal' } }, en: { drive: { title: 'Voice chat' } } });
    assert.equal(i18n.t('drive.title'), 'Voice chat');
    // Interpolation and plurals work on merged strings.
    i18n.mergeBundle('voice', { lang: 'de', dict: { drive: { n: { one: '# Stimme', other: '# Stimmen' }, hi: 'Hallo {name}' } }, en: {} });
    assert.equal(i18n.t('drive.n', { count: 1 }), '1 Stimme');
    assert.equal(i18n.t('drive.n', { count: 3 }), '3 Stimmen');
    assert.equal(i18n.t('drive.hi', { name: 'Sage' }), 'Hallo Sage');
    i18n.removeBundle('voice');
    assert.equal(i18n.t('drive.hi', { name: 'Sage' }), 'Hi', 'gone once removed');
    // Junk never throws.
    i18n.mergeBundle('voice', { lang: 'de', dict: ['nope'], en: null });
    assert.equal(i18n.t('drive.title'), 'Title');
    i18n.mergeBundle('', { lang: 'de', dict: { a: 'b' } });
    assert.equal(i18n.t('a'), 'A');
  } finally {
    console.warn = warn;
    Object.assign(globalThis, keep);
  }
});
