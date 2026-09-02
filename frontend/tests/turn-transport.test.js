// The socket turn contract, as the browser half implements it.
//
// These frames carry a reply that does not exist yet. `stream_start` may name
// a PROVISIONAL id ("run:<runId>") because tokens are flowing before any row
// has been persisted; `stream_chunk` may carry the whole buffer instead of the
// next piece; `stream_done` may say "the bubble you have been painting is
// actually this message" — or that there is no message at all, because the run
// was aborted.
//
// Every one of those has a failure mode that LOOKS FINE on screen:
//
//   · a `replace` chunk appended instead of assigned duplicates the reply so
//     far, and the duplicate is only visible if you read the words
//   · a provisional id pushed into state.messages is a row with an id no API
//     call can resolve — Delete and Regenerate 404 against it, and the next
//     full render draws it beside the real message
//   · a null `message` left as a "still streaming" bubble sits there with a
//     live cursor forever, which reads as a model that is thinking, not one
//     that stopped
//
// None of that is reachable from a unit test: main.js is one DOM module with
// no exports. So this reads the handlers as source and pins the decisions that
// distinguish those failures, in the same spirit as thumbnails.test.js.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const MAIN = readFileSync(join(HERE, '..', 'static', 'js', 'main.js'), 'utf8');

// The same source with comments removed. Several assertions below are of the
// form "this expression must NOT appear", and main.js documents the very
// mistakes it is avoiding — the comment above TURN_PHASE_KEYS spells out
// t('turn.phase_' + phase) as the thing not to write. A scan that reads the
// warning as the offence fails on the fix.
const CODE = MAIN
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/(^|[^:'"`])\/\/[^\n]*/g, '$1');

/** The body of one `case '<name>': { … }` block in handleWs's switch. */
function handlerBody(name) {
  const open = MAIN.indexOf(`case '${name}': {`);
  assert.notEqual(open, -1, `no handler for the '${name}' frame`);
  let depth = 0;
  for (let i = MAIN.indexOf('{', open); i < MAIN.length; i++) {
    if (MAIN[i] === '{') depth++;
    else if (MAIN[i] === '}' && --depth === 0) return MAIN.slice(open, i + 1);
  }
  throw new Error(`unbalanced braces in the '${name}' handler`);
}

test("a 'replace' chunk REPLACES the buffer instead of appending", () => {
  const body = handlerBody('stream_chunk');
  assert.match(body, /data\.replace/,
    'stream_chunk ignores the replace flag — a resumed stream would duplicate '
    + 'everything the client already had');
  // The assigning branch must be the one guarded by `replace`, and the
  // appending branch must be the else. Written the other way round the flag is
  // read and still gets the behaviour backwards.
  assert.match(body, /if \(data\.replace\) streamBuffers\[[^\]]+\] = /,
    'the replace branch must ASSIGN the full text');
  assert.match(body, /else streamBuffers\[[^\]]+\] \+= /,
    'the non-replace branch must APPEND, as it always did');
});

test('a chunk for an unknown id opens the bubble rather than dropping tokens', () => {
  const body = handlerBody('stream_chunk');
  assert.match(body, /if \(!\(data\.message_id in streamBuffers\)\) beginStream\(/,
    'a reconnect lands mid-stream with no stream_start; without this every '
    + 'token until stream_done is silently discarded');
});

test('stream_done swaps the bubble the PROVISIONAL id built', () => {
  const body = handlerBody('stream_done');
  assert.match(body, /const streamId = data\.provisional_id \|\| data\.message_id;/,
    'the DOM node is keyed by the id its chunks arrived under, which is the '
    + 'provisional one whenever the server sends it');
  // The lookup that finds the node on screen must use that id…
  assert.match(body, /querySelector\(`\[data-id="\$\{CSS\.escape\(streamId\)\}"\]`\)/,
    'looking the node up by the real message_id would miss it entirely and '
    + 'append a second copy of the reply below the half-streamed one');
});

test('a provisional id never reaches state.messages', () => {
  const body = handlerBody('stream_done');
  // The row pushed into state must be identified by the persisted message's
  // own id. `streamId` may be "run:…", which no API call can resolve.
  assert.match(body, /state\.messages\.some\(\(m\) => m\.id === fullMsg\.id\)/,
    'the dedup check must be against the REAL id');
  assert.doesNotMatch(body, /state\.messages\.push\(\s*\{/,
    'stream_done must push the server\'s message object, never a synthesised row');
  // beginStream is the only place a provisional id becomes a DOM node, and it
  // must not touch state.messages at all.
  const begin = MAIN.slice(MAIN.indexOf('function beginStream('),
                           MAIN.indexOf('function renderStreamMarkdown('));
  assert.doesNotMatch(begin, /state\.messages/,
    'beginStream paints a placeholder — a placeholder is not a message');
});

test('stream_done with no message takes the bubble away', () => {
  const body = handlerBody('stream_done');
  assert.match(body, /const fullMsg = data\.message \|\| null;/,
    'an aborted or errored run delivers no persisted row');
  assert.match(body, /if \(!fullMsg\) \{[\s\S]*?existingEl\.remove\(\);/,
    'without this the half-streamed text sits under a live cursor forever, '
    + 'which reads as a reply still arriving');
});

test('the streaming buffers are released for BOTH ids', () => {
  const body = handlerBody('stream_done');
  assert.match(body, /for \(const id of new Set\(\[streamId, data\.message_id\]/,
    'streamPainted/streamBuffers entries keyed by an id nobody clears are the '
    + 'per-reply leak their own comment warns about');
});

test('the phase line is cleared when the turn ends', () => {
  for (const frame of ['stream_done', 'thinking']) {
    assert.match(handlerBody(frame), /setTurnPhase\([^)]*null\)/,
      `the '${frame}' frame must drop the phase, or "Loading model…" outlives `
      + 'the turn that was loading one');
  }
});

test('phase keys are literals, not a key built from server text', () => {
  // t('turn.phase_' + phase) would let a frame name any key in the catalogue,
  // and tests/i18n-keys.test.js could not tell which keys are still live.
  assert.doesNotMatch(CODE, /t\('turn\.phase_' \+/,
    'look the phase up in TURN_PHASE_KEYS instead');
  assert.match(MAIN, /const TURN_PHASE_KEYS = \{/);
  const en = JSON.parse(readFileSync(
    join(HERE, '..', 'static', 'locales', 'en.json'), 'utf8'));
  const table = MAIN.slice(MAIN.indexOf('const TURN_PHASE_KEYS = {'),
                           MAIN.indexOf('function turnPhaseText('));
  const keys = [...table.matchAll(/'turn\.(phase_[a-z_]+)'/g)].map((m) => m[1]);
  assert.ok(keys.length >= 5, 'the phase table lost its entries');
  for (const k of keys) {
    assert.ok(en.turn && en.turn[k], `turn.${k} is mapped but not translated`);
  }
  // The two the table cannot name: the tool template and the fallback.
  assert.ok(en.turn.phase_tool.includes('{name}'), 'phase_tool must interpolate the tool name');
  assert.ok(en.turn.phase_generic, 'an unknown phase must still say something');
});

test('Stop is unlocked-only, and one gate serves the composer and the palette', () => {
  const fn = MAIN.slice(MAIN.indexOf('function canStopReply()'),
                        MAIN.indexOf('function stopReply()'));
  assert.match(fn, /!state\.decoy/,
    'Safe Mode must never show a Stop button: the server 403s the abort, so '
    + 'the control could only fail — and it would advertise that a full '
    + 'session exists');
  assert.match(fn, /state\.thinking\[state\.activeThreadId\]/,
    'nothing to stop unless this thread is working');
  // Both surfaces ask the same question. Two copies of the rule is how one of
  // them ends up showing in Safe Mode.
  assert.match(MAIN, /if \(canStopReply\(\)\) a\.push\(\{ icon: '⏹'/,
    'the command palette must reuse canStopReply(), not re-derive it');
  assert.match(MAIN, /const show = canStopReply\(\);/,
    'the composer button must reuse canStopReply(), not re-derive it');
});

test('the abort frame is the documented shape', () => {
  const fn = MAIN.slice(MAIN.indexOf('function stopReply()'),
                        MAIN.indexOf('function reflectStopButton()'));
  assert.match(fn, /socket\.send\(\{ type: 'abort', thread_id: tid \}\)/);
  // A refusal comes back as an 'error' frame; the button has to come back too.
  // (That case is brace-less, so it is sliced rather than read by handlerBody.)
  const err = MAIN.slice(MAIN.indexOf("case 'error':"), MAIN.indexOf("case 'stream_start':"));
  assert.match(err, /clearStopRequest\(/,
    'a refused abort must release the button rather than leaving it disabled '
    + 'until the next turn');
});

test('an optimistic user bubble is DOM-only and keyed by client_msg_id', () => {
  const fn = MAIN.slice(MAIN.indexOf('function showOptimisticSend('),
                        MAIN.indexOf('function dropOptimistic('));
  assert.match(fn, /delete node\.dataset\.id;/,
    'a pending row must not be addressable as a message: every id-keyed path '
    + '(message_update, message_deleted, the stream swap) would find it');
  assert.match(fn, /node\.dataset\.cmid = cmid;/);
  assert.doesNotMatch(fn, /state\.messages/,
    'it has no server id yet — putting it in state.messages is how it survives '
    + 'into the next render as a duplicate of the persisted message');
  assert.match(fn, /if \(!node\) return;/,
    'No-Image Mode renders a picture-only message as nothing; there is no '
    + 'preview to show for a row that will not exist');
});

test('the persisted echo reconciles before the real row is appended', () => {
  const body = handlerBody('message');
  const rec = body.indexOf('reconcileOptimistic(data)');
  const app = body.indexOf('appendMessageToView(data.message)');
  assert.notEqual(rec, -1, "the 'message' frame must reconcile the optimistic bubble");
  assert.notEqual(app, -1);
  assert.ok(rec < app,
    'the day separator and the consecutive-sender grouping are computed around '
    + 'the rows already on screen — reconciling afterwards computes them around '
    + 'a row that is about to disappear');
  const fn = MAIN.slice(MAIN.indexOf('function reconcileOptimistic('),
                        MAIN.indexOf('function sweepOptimistic('));
  assert.match(fn, /frame\.client_msg_id \|\| msg\.client_msg_id/,
    'the id the server echoes is the identity; matching on text is the fallback');
  assert.match(fn, /startsWith/,
    'the persist chokepoint strips :react: markers, so the stored text can be '
    + 'SHORTER than what was typed — exact equality alone would leave a ghost');
});

test('a stalled send is marked, not deleted', () => {
  const fn = MAIN.slice(MAIN.indexOf('function sweepOptimistic('),
                        MAIN.indexOf('function sweepOptimistic(') + 900);
  assert.match(fn, /PENDING_RETRY_MS/,
    'the mark must use the same threshold as the Retry chip, or the bubble and '
    + 'the chip disagree about whether anything is wrong');
  assert.match(fn, /classList\.toggle\('failed'/);
  assert.doesNotMatch(fn.slice(0, fn.indexOf('classList')), /entry\.el\.remove\(\)/,
    'removing it would take away the only copy of the text the user can see');
});

test('a thread_update patches its row instead of rebuilding the list', () => {
  const start = CODE.indexOf("case 'thread_update':");
  const body = CODE.slice(start, CODE.indexOf("case 'thread_deleted':", start));
  assert.match(body, /patchThreadRow\(data\.thread\.id\)/,
    'a full renderThreads() here was one of six list rebuilds per incoming '
    + 'message (107ms at 207 threads, per threadRowEl\'s own measurement)');
  assert.doesNotMatch(body, /renderThreads\(\)/,
    'patchThreadRow falls back to a full render itself when the row moves — a '
    + 'second unconditional call would defeat the point');
});

test('a background thread never rebuilds the list you are reading', () => {
  const fn = MAIN.slice(MAIN.indexOf('function touchThreadRow('),
                        MAIN.indexOf('// Pinned-first, then most-recent'));
  assert.match(fn, /state\.threads\.some\(\(x\) => x\.id === threadId\)/,
    'patchThreadRow rebuilds the whole list for a thread it cannot find, which '
    + 'is exactly wrong for a thread that is not displayed at all');
});

test('only a bot whose look changed repaints what it is painted on', () => {
  const start = MAIN.indexOf("case 'bots':");
  const body = MAIN.slice(start, MAIN.indexOf("case 'thread_created':", start));
  assert.match(body, /const changed = changedBotLooks\(prevBots, state\.bots\);/);
  assert.match(body, /changed === null \|\| changed\.has\(activeBotId\)/,
    'renderMessages() rebuilds every row in the open chat — another bot\'s '
    + 'nightly avatar draw must not trigger it');
  const fn = MAIN.slice(MAIN.indexOf('function changedBotLooks('),
                        MAIN.indexOf('function updateThreadListHeader('));
  assert.match(fn, /if \(!Array\.isArray\(prev\) \|\| !prev\.length\) return null;/,
    'with nothing to compare against, the old unconditional repaint is the '
    + 'only safe answer');
  for (const field of ['avatar_url', 'name', 'emoji', 'model_hint']) {
    assert.ok(fn.includes(field), `${field} is painted somewhere and must be compared`);
  }
});

test('a late stream frame for a settled id cannot rebuild the bubble', () => {
  // stream_done retires the id; the router's terminal flush can still emit a
  // stream_start/stream_chunk (replace:true) for it ~30 ms later. Without the
  // guard the unknown-id reconnect path resurrects a second streaming bubble
  // under the real reply (seen on staging 2026-09-02).
  assert.match(MAIN, /function markStreamSettled\(id\)/);
  assert.match(MAIN, /SETTLED_STREAM_CAP = \d+/, 'settled set must be bounded');
  const done = MAIN.slice(MAIN.indexOf("case 'stream_done'"));
  assert.match(done.slice(0, 1500), /markStreamSettled\(id\)/,
    'stream_done must record every id it releases');
  const start = MAIN.slice(MAIN.indexOf("case 'stream_start'"),
    MAIN.indexOf("case 'stream_chunk'"));
  assert.match(start, /if \(streamIsSettled\(data\.message_id\)\) break;/);
  const chunk = MAIN.slice(MAIN.indexOf("case 'stream_chunk'"),
    MAIN.indexOf("case 'stream_done'"));
  const guard = chunk.indexOf('streamIsSettled(data.message_id)');
  const begin = chunk.indexOf('beginStream(data.message_id)');
  assert.ok(guard !== -1 && begin !== -1 && guard < begin,
    'the settled check must run before the reconnect beginStream');
});
