// Unit tests for No-Image Mode's pure logic.
//
// Run: node --test frontend/tests/
//
// Only the two decision functions are covered here, deliberately. They are the
// ones with branches worth pinning: everything else in nim.js is DOM plumbing
// whose real behaviour lives in a browser, and is covered by the end-to-end
// pass instead (which asserts on the NETWORK — the claim that actually matters).
//
// nim.js touches `document` and `localStorage` only inside function bodies, so
// it imports cleanly under plain node with no DOM shim.

import test from 'node:test';
import assert from 'node:assert/strict';

import { isMediaOnly, canDisableNim, shouldDropMessage } from '../static/js/nim.js';

// The real stripMediaSource from markdown.js is a regex over [[media:…]] and
// markdown image syntax. Reproducing the exact pattern here would test the copy
// rather than the contract, so isMediaOnly takes the stripper as a parameter
// and these tests inject an obvious one.
const strip = (t) => (t || '').replace(/!\[[^\]]*\]\([^)]*\)|\[\[media:[^\]]*\]\]/g, '');

const img = { mime: 'image/png' };
const vid = { mime: 'video/mp4' };
const pdf = { mime: 'application/pdf' };

test('isMediaOnly: an image with no caption is media-only', () => {
  assert.equal(isMediaOnly({ content: '', attachments: [img] }, strip), true);
});

test('isMediaOnly: markdown image and nothing else is media-only', () => {
  assert.equal(isMediaOnly({ content: '![](/media/abc.png)', attachments: [img] }, strip), true);
});

test('isMediaOnly: a caption keeps the row', () => {
  // The whole point of the feature is to drop pictures, not people's words.
  assert.equal(isMediaOnly({ content: 'look at this ![](/media/a.png)', attachments: [img] }, strip), false);
});

test('isMediaOnly: video counts as media', () => {
  assert.equal(isMediaOnly({ content: '', attachments: [vid] }, strip), true);
});

test('isMediaOnly: a PDF attachment is NOT media-only', () => {
  // Readable content NIM has no business hiding — dropping it would lose
  // information rather than just pictures.
  assert.equal(isMediaOnly({ content: '', attachments: [pdf] }, strip), false);
});

test('isMediaOnly: a mixed image+PDF message keeps its row', () => {
  assert.equal(isMediaOnly({ content: '', attachments: [img, pdf] }, strip), false);
});

test('isMediaOnly: a text-only message is never media-only', () => {
  assert.equal(isMediaOnly({ content: 'hello', attachments: [] }, strip), false);
});

test('isMediaOnly: an empty message is not media-only', () => {
  // No text AND no attachments is an empty message, not a picture. Whatever
  // already handles those should keep handling them; NIM must not silently
  // start eating rows it was never asked to touch.
  assert.equal(isMediaOnly({ content: '', attachments: [] }, strip), false);
});

test('isMediaOnly: reads attachments from metadata too', () => {
  assert.equal(isMediaOnly({ content: '', metadata: { attachments: [img] } }, strip), true);
});

test('isMediaOnly: tolerates null', () => {
  assert.equal(isMediaOnly(null, strip), false);
});

test('isMediaOnly: tolerates a malformed attachment', () => {
  // An attachment with no mime must not read as an image — unknown means keep
  // the row, because dropping something we cannot identify is the lossy answer.
  assert.equal(isMediaOnly({ content: '', attachments: [{}] }, strip), false);
});

// --- the ratchet -----------------------------------------------------------
// canDisableNim(decoy), where decoy === (pinSet && !authenticated).

test('canDisableNim: an unlocked session may turn NIM off', () => {
  // Being unlocked IS the credential — no second prompt.
  assert.equal(canDisableNim(false), true);
});

test('canDisableNim: a locked Safe-Mode session may NOT', () => {
  // This is the case the whole feature exists for: the handed-over tablet.
  assert.equal(canDisableNim(true), false);
});

test('canDisableNim: with no PIN set, NIM stays reversible', () => {
  // decoy is false when pinSet is false, so this is the same branch as
  // "unlocked" — asserted separately because it is a distinct product rule
  // and someone will eventually try to "fix" it into a lockout.
  assert.equal(canDisableNim(false), true);
});

// --- what disappears entirely -----------------------------------------------
// shouldDropMessage is the single answer to "does this message leave a row?".
// The reaction case is here because getting it wrong is not theoretical: the
// first implementation only declined to build the trace ROW, so the message
// fell through to ordinary rendering and printed its caption
// ("\u26a1 Nova reacted \u00b7 Check In") in a bubble.

const reaction = { role: 'system', content: '\u26a1 Nova reacted \u00b7 Check In',
                   metadata: { kind: 'reaction', reaction_id: 'abc' } };

test('shouldDropMessage: a reaction trace leaves no row', () => {
  assert.equal(shouldDropMessage(reaction, strip), true);
});

test('shouldDropMessage: a reaction trace with NO content still drops', () => {
  assert.equal(shouldDropMessage({ metadata: { kind: 'reaction' } }, strip), true);
});

test('shouldDropMessage: a picture-only message leaves no row', () => {
  assert.equal(shouldDropMessage({ content: '', attachments: [img] }, strip), true);
});

test('shouldDropMessage: an ordinary text message is KEPT', () => {
  assert.equal(shouldDropMessage({ role: 'user', content: 'hello' }, strip), false);
});

test('shouldDropMessage: a PDF-only message is KEPT', () => {
  assert.equal(shouldDropMessage({ content: '', attachments: [pdf] }, strip), false);
});

test('shouldDropMessage: a NON-reaction system message is KEPT', () => {
  // Only kind === 'reaction' drops. A system notice is text, and text stays.
  assert.equal(shouldDropMessage(
    { role: 'system', content: 'Thread archived', metadata: { kind: 'notice' } }, strip), false);
});

test('shouldDropMessage: a captioned image is KEPT', () => {
  assert.equal(shouldDropMessage(
    { content: 'look ![](/media/a.png)', attachments: [img] }, strip), false);
});

test('shouldDropMessage: tolerates null', () => {
  assert.equal(shouldDropMessage(null, strip), false);
});

// --- the REAL message shape --------------------------------------------------
// Everything above this line was originally written against an invented
// `attachments` array. DisPatch's MessageOut has no such field — media travels
// as `media_url` or as [[media:…]] / ![](…) inside `content` — so those tests
// passed while testing a fiction, and picture-only rows were never dropped in
// the actual app. These assert the shape the backend really sends.

test('REAL shape: a message with only media_url is media-only', () => {
  assert.equal(isMediaOnly({ role: 'user', content: '', media_url: '/media/a.png' }, strip), true);
});

test('REAL shape: media_url WITH a caption keeps its row', () => {
  assert.equal(isMediaOnly(
    { role: 'user', content: 'look at this', media_url: '/media/a.png' }, strip), false);
});

test('REAL shape: content that is only a [[media:…]] directive is media-only', () => {
  assert.equal(isMediaOnly({ role: 'user', content: '[[media:abc-123]]' }, strip), true);
});

test('REAL shape: content that is only a markdown image is media-only', () => {
  assert.equal(isMediaOnly({ role: 'user', content: '![](/media/a.png)' }, strip), true);
});

test('REAL shape: a [[doc:…]] card is NOT media-only', () => {
  // stripMediaSource does not touch doc directives, so the text survives and
  // the row is kept. Documents are readable content, not pictures.
  assert.equal(isMediaOnly({ role: 'user', content: '[[doc:report.pdf]]' }, strip), false);
});

test('REAL shape: a media directive PLUS a doc card keeps its row', () => {
  assert.equal(isMediaOnly(
    { role: 'user', content: '[[media:a]] [[doc:report.pdf]]' }, strip), false);
});

test('REAL shape: a genuinely empty message is not media-only', () => {
  assert.equal(isMediaOnly({ role: 'user', content: '' }, strip), false);
});

test('REAL shape: shouldDropMessage agrees on media_url rows', () => {
  assert.equal(shouldDropMessage(
    { role: 'user', content: '', media_url: '/media/a.png' }, strip), true);
});
