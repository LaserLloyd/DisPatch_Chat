// Regenerate, edit, and the two things about them that must not drift.
//
// main.js is one DOM module with no exports, so — exactly as
// turn-transport.test.js and thumbnails.test.js do — this reads it as source
// and pins the decisions, not the pixels. Each assertion below names a
// failure that LOOKS FINE on screen:
//
//   · Regenerate sending `retry` produces a bot that answers a second time
//     with its own previous reply still in context. The button works, the
//     spinner spins, a new bubble appears, and it says almost the same thing.
//     That was the shipped behaviour, and nothing about it looked broken.
//   · The Retry button under a failed reply sending `regenerate` would take
//     the one re-ask a LOCKED family tablet is allowed to do and turn it into
//     a frame the server refuses with "Unlock for full access".
//   · Hide-from-context offered on a GATEWAY bot would hide the row from the
//     chat while the agent went on remembering it — a control that reports
//     success and changes nothing.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const JS = join(HERE, '..', 'static', 'js');
const MAIN = readFileSync(join(JS, 'main.js'), 'utf8');
const API = readFileSync(join(JS, 'api.js'), 'utf8');

/** The balanced body of `function <name>(…) { … }`, comments and all. */
function fnBody(name) {
  const open = MAIN.search(new RegExp(`(?:async )?function ${name}\\s*\\(`));
  assert.notEqual(open, -1, `main.js has no function ${name}`);
  let depth = 0;
  for (let i = MAIN.indexOf('{', open); i < MAIN.length; i++) {
    if (MAIN[i] === '{') depth++;
    else if (MAIN[i] === '}' && --depth === 0) return MAIN.slice(open, i + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

/** The balanced block that starts at `from` (which must contain a `{`). */
function blockAt(from) {
  let depth = 0;
  for (let i = MAIN.indexOf('{', from); i < MAIN.length; i++) {
    if (MAIN[i] === '{') depth++;
    else if (MAIN[i] === '}' && --depth === 0) return MAIN.slice(from, i + 1);
  }
  throw new Error('unbalanced braces');
}

// --------------------------------------------------------------------------- //
// The frame Regenerate sends
// --------------------------------------------------------------------------- //

test('Regenerate sends the rewinding frame, not a plain retry', () => {
  const body = fnBody('regenerateLast');
  assert.match(body, /type:\s*'regenerate'/,
    'Regenerate must send the frame that rewinds the session first. Sending '
    + "`retry` re-asks inside the SAME session, so the model writes a "
    + 'variation on the answer it is still looking at.');
  assert.doesNotMatch(body, /type:\s*'retry'/,
    'the old frame must be gone from this path');
});

test('the Retry button under a failed reply still sends `retry`', () => {
  // A regression guard for a DELIBERATE departure from the plan, which made
  // regenerate a mutation and therefore a 403 on locked devices. Rewinding
  // stays unlocked-only, and this button stays exactly as it was, so a family
  // tablet keeps the re-ask it has always had.
  const body = fnBody('appendErrorBubble');
  assert.match(body, /type:\s*'retry'/,
    'switching this to `regenerate` would hand a locked device a frame the '
    + 'server refuses, and the only recovery a Safe-Mode tablet has would '
    + 'stop working');
});

// --------------------------------------------------------------------------- //
// Where the new controls may appear
// --------------------------------------------------------------------------- //

test('hide-from-context is offered for API bots and nowhere else', () => {
  const at = MAIN.indexOf('threadIsApiBot(msg.thread_id)');
  assert.notEqual(at, -1, 'the action row must ask whether this is an API bot');
  const guard = blockAt(MAIN.indexOf('{', at));
  for (const key of ['msg.hide_context', 'msg.show_context']) {
    assert.ok(guard.includes(key), `${key} must sit inside the API-bot guard`);
    assert.equal(
      MAIN.split(`t('${key}')`).length - 1, 1,
      `${key} must be offered from exactly one place — a second, ungated call `
      + 'site is how a gateway bot ends up with a button that hides a row the '
      + 'agent still remembers');
  }
});

test('every new message control is behind the unlocked-only gate', () => {
  const at = MAIN.indexOf("if (!state.decoy) {",
    MAIN.indexOf('const actions = el(\'div\', { class: \'msg-actions\' })'));
  assert.notEqual(at, -1, 'the action row keeps its Safe-Mode gate');
  const gated = blockAt(at);
  for (const key of ['msg.edit', 'msg.hide_context', 'msg.regenerate']) {
    assert.ok(gated.includes(`t('${key}')`),
      `${key} must be inside the !state.decoy block — Safe Mode is view and `
      + 'send, and a dead button advertises the lock');
  }
});

test('"Save and rerun" is offered only on the newest user message', () => {
  const body = fnBody('openMessageEditor');
  assert.match(body, /isLastUserMessage\(msg\)[\s\S]{0,200}msg\.save_rerun/,
    'the re-run button must be gated on the message the server will accept — '
    + 'rewind addresses a session by the entry id of a user turn, and only '
    + 'the newest one can be identified without guessing');
  const gate = fnBody('isLastUserMessage');
  assert.match(gate, /role === 'user'/,
    'the newest USER row, not the newest row: a reply normally sits after it');
});

// --------------------------------------------------------------------------- //
// The markers, which are the feature
// --------------------------------------------------------------------------- //

test('an edited message says so, and a context-dropped one says so', () => {
  const body = fnBody('messageEl');
  assert.match(body, /meta\.edited_at/,
    'without the marker the transcript lies: the text changed and nothing says so');
  assert.match(body, /t\('msg\.edited'\)/);
  assert.match(body, /meta\.hidden/);
  assert.match(body, /t\('msg\.hidden_marker'\)/,
    'a row that is no longer sent to the model must look different from one '
    + 'that is, or hiding it is invisible');
});

test('the pager opens on the live answer and never writes', () => {
  const body = fnBody('altPagerEl');
  assert.match(body, /alts\.concat\(/,
    'the live reply is a page of the pager, not a special case outside it');
  assert.match(body, /idx = pages\.length - 1/,
    'it must open on the answer that is actually stored — opening on an '
    + 'alternate would show the family a reply the bot did not give');
  assert.doesNotMatch(body, /\bapi\./,
    'paging is a view: nothing is persisted, so a reload lands back on the '
    + 'live answer');
});

// --------------------------------------------------------------------------- //
// The REST call behind the editor
// --------------------------------------------------------------------------- //

test('editMessage PATCHes the message and sends only what changed', () => {
  assert.match(API, /editMessage:\s*\(mid,\s*body\)/,
    'api.js must expose the edit call');
  const line = API.split('\n').find((l) => l.includes('editMessage:'));
  assert.match(line, /method: 'PATCH'/);
  assert.match(line, /\/api\/messages\/\$\{encodeURIComponent\(mid\)\}/);
  assert.match(line, /JSON\.stringify\(body\)/,
    'the caller passes {content} or {hidden} alone; the server merges, so '
    + 'this must not fill in the other one');
});
