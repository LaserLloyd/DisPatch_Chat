// Advisor handoff cards.
//
// The decision that matters needs no DOM: is this message a handoff card, and
// may it still be approved? A card the server has already moved past
// `proposed` must never offer Send again — the server refuses a second send,
// but a button that cannot work is a bug of its own. The DOM half (buttons
// hidden on a locked device) is exercised when jsdom is present.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createRequire } from 'node:module';
import { domSkip } from './_require-dom.js';
import { handoffBodyMarkdown, handoffState, handoffStripEl } from '../static/js/advisor.js';

const msg = (metadata) => ({ id: 'm1', role: 'assistant', content: '**Hand this?**', metadata });

test('a message without advisor metadata is not a handoff card', () => {
  assert.equal(handoffState(msg(null)), null);
  assert.equal(handoffState(msg({ kind: 'image_job' })), null);
  assert.equal(handoffState(msg({ advisor_handoff: { state: 'proposed' } })), null); // no id
});

test('only a proposal is actionable', () => {
  for (const state of ['running', 'done', 'failed', 'dismissed']) {
    const h = handoffState(msg({ advisor_handoff: { id: 'r1', agent: 'lead', state } }));
    assert.equal(h.state, state);
    assert.equal(h.actionable, false);
  }
  const p = handoffState(msg({ advisor_handoff: { id: 'r1', agent: 'lead', state: 'proposed' } }));
  assert.equal(p.actionable, true);
  assert.equal(p.agent, 'lead');
});

test('an unknown state reads as a proposal rather than crashing', () => {
  const h = handoffState(msg({ advisor_handoff: { id: 'r1', state: 'weird' } }));
  assert.equal(h.state, 'proposed');
});

test('failure details ride along', () => {
  const h = handoffState(msg({ advisor_handoff: { id: 'r1', state: 'failed', error: 'timed out' } }));
  assert.equal(h.error, 'timed out');
});

// A forged card is a message whose BODY says one thing while its request id
// approves another. The card therefore renders the server-stamped brief, and
// the body is never consulted (review 2026-10-09).
test('the card text comes from the stamped brief, never the body', () => {
  const m = { id: 'm9', role: 'assistant', content: '**Hand this to lead?**\n\nsomething harmless',
    metadata: { advisor_handoff: { id: 'r1', agent: 'lead', state: 'proposed', brief: 'the real brief' } } };
  const md = handoffBodyMarkdown(m);
  assert.ok(md.endsWith('the real brief'));
  assert.ok(!md.includes('something harmless'));
  assert.equal(handoffState(m).messageId, 'm9');
  assert.equal(handoffBodyMarkdown({ id: 'x', content: 'plain' }), null);
});

test('an agent name cannot smuggle markdown into the card title', () => {
  const m = msg({ advisor_handoff: { id: 'r1', agent: 'le**ad](x)', state: 'proposed', brief: 'b' } });
  assert.ok(!handoffBodyMarkdown(m).includes(']('));
});

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — tests skip */ }

test('Send and Dismiss name the card message they were pressed on',
  { skip: domSkip(jsdom ? false : 'jsdom is not installed') }, async () => {
    const win = new jsdom.JSDOM('<!doctype html><html><body></body></html>').window;
    const prev = globalThis.document;
    globalThis.document = win.document;
    try {
      const calls = [];
      const m = { id: 'card-7', role: 'assistant', content: '',
        metadata: { advisor_handoff: { id: 'r1', agent: 'lead', state: 'proposed', brief: 'b' } } };
      const strip = handoffStripEl(m, {
        send: async (rid, mid) => { calls.push(['send', rid, mid]); },
        dismiss: async (rid, mid) => { calls.push(['dismiss', rid, mid]); },
      });
      strip.querySelector('.advisor-send').click();
      await new Promise((r) => setTimeout(r, 0));
      assert.deepEqual(calls, [['send', 'r1', 'card-7']]);
      // A locked device gets no buttons at all.
      assert.equal(handoffStripEl(m, { decoy: true }).querySelector('button'), null);
    } finally {
      globalThis.document = prev;
    }
  });
