// Every thumbnail must offer its full-resolution original.
//
// THE PRINCIPLE: a thumbnail in this app is a VIEW of a larger image, and
// clicking it opens THAT image at full size — not today's version of it, not a
// similar one. The same picture.
//
// It is enforced by convention: a thumbnail carries `data-full="<url>"` and a
// single delegated listener (installThumbnailLightbox) opens the lightbox. That
// exists because per-call-site handlers kept going missing:
//
//   · thread avatars shipped with no click handler at all — the thumbnail was
//     there, clicking it just opened the thread
//   · restoring an old avatar wrote the face crop and left the PREVIOUS
//     avatar's full-resolution file in place, so the lightbox opened a
//     different picture entirely
//   · the header avatar and the message avatar each wired their own handler,
//     in two places, with two different rules about Safe Mode
//
// This test reads main.js and asserts every <img>-producing site either routes
// through setFullRes() or is on the exempt list WITH a reason. A new thumbnail
// added without a full-resolution source fails here rather than shipping
// silently unclickable.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const MAIN = readFileSync(join(HERE, '..', 'static', 'js', 'main.js'), 'utf8');

// Image-producing sites that legitimately have NO larger version.
// Each entry must say why, or it is indistinguishable from an oversight.
const EXEMPT = [
  {
    match: "src, draggable: 'false', alt: downloadName",
    why: 'this IS the lightbox image — it is already the full-resolution view',
  },
  {
    match: "src: previewSrc, alt: a.name",
    why: 'composer attachment chip: a local object URL for a file not yet sent, '
       + 'so there is no server-side original to open',
  },
  {
    match: "class: 'bm-avatar-pick'",
    why: 'Bot Manager picker: clicking it opens the file/crop flow, which is a '
       + 'different and more useful action than viewing the current image',
  },
  {
    match: "id: cur.id, class: baseClass",
    why: 'header avatar placeholder built empty; paintHeaderAvatar calls '
       + 'setFullRes on it once the bot is known',
  },
  {
    match: "class: 'comp-avatar', src: url",
    why: 'Companions is a Safe-Mode-only screen and /api/bots/<id>/avatar/full '
       + 'is PIN-gated, so a full-res affordance there is a guaranteed 403 — '
       + 'thumbnails-only is the Safe-Mode rule everywhere',
  },
];

function imageProducingLines(src) {
  const out = [];
  src.split('\n').forEach((line, i) => {
    const stripped = line.replace(/\/\/.*$/, '');
    if (/el\('img'|createElement\('img'\)|new Image\(/.test(stripped)) {
      out.push({ line: i + 1, text: line.trim() });
    }
  });
  return out;
}

test('every thumbnail producer offers a full-resolution original', () => {
  const producers = imageProducingLines(MAIN);
  assert.ok(producers.length >= 5, 'expected to find the image-producing sites');

  const lines = MAIN.split('\n');
  const missing = [];

  for (const p of producers) {
    // setFullRes may wrap the call inline, or apply a few lines later behind a
    // condition. The window is generous because these sites carry long
    // explanatory comments between the construction and the call — a tight
    // window made this test fail on correct code, which is the worst kind of
    // guard: one people learn to edit rather than believe.
    const window = lines.slice(Math.max(0, p.line - 6), p.line + 18).join('\n');
    if (/setFullRes\s*\(/.test(window)) continue;
    if (EXEMPT.some((e) => window.includes(e.match))) continue;
    missing.push(`main.js:${p.line}  ${p.text.slice(0, 88)}`);
  }

  assert.deepEqual(missing, [],
    '\nThese build an image with no full-resolution source.\n'
    + 'Either wrap it in setFullRes(node, url), or add it to EXEMPT WITH A REASON:\n'
    + missing.join('\n'));
});

test('the left rail opens the CHAT, not a lightbox', () => {
  // The one deliberate exception to the principle. Those avatars ARE the
  // button that selects a bot — the picture is the navigation, not the
  // subject. Wiring them to a lightbox removed the app\'s primary control.
  assert.match(MAIN, /baseClass !== 'bot-avatar'/,
    'sidebar avatars are no longer excluded from the full-res convention');
  assert.match(MAIN, /closest\('\.bot-btn'\)/,
    'the delegated listener no longer defers to the bot button');
});

test('the delegated listener exists and is installed at boot', () => {
  assert.match(MAIN, /function installThumbnailLightbox\(/,
    'the single delegated listener is gone');
  assert.match(MAIN, /installThumbnailLightbox\(\);/,
    'installThumbnailLightbox is never called — no thumbnail would be clickable');
  assert.match(MAIN, /closest\?\.\('\[data-full\]'\)/,
    'the listener no longer keys off the data-full convention');
});

test('Safe Mode strips the full-resolution affordance rather than hiding it', () => {
  // Full-res avatars are PIN-gated. Leaving data-full on a Safe-Mode avatar
  // would offer a click that 403s — worse than offering nothing.
  assert.match(MAIN, /delete\s+\w+\.dataset\.full/,
    'nothing removes data-full for a Safe-Mode session');
});

test('a thread avatar opens the THREAD\'S image, not the bot\'s current one', () => {
  // The bug this whole convention came from: a chat started in June must not
  // open today's face.
  assert.match(MAIN, /full=1/,
    'the thread avatar no longer requests its own full-resolution snapshot');
});

test('an image the AGENT sent is clickable too', () => {
  // Attachments go through mediaThumbEl (which wires data-full); an agent's
  // picture arrives as markdown and was reaching the DOM with no data-full at
  // all, so the delegated listener skipped it and clicking did nothing.
  // Measured on real family data: 6 of 20 inline images were dead.
  const MD = readFileSync(join(HERE, '..', 'static', 'js', 'markdown.js'), 'utf8');
  assert.match(MD, /img\.dataset\.full = norm/,
    'markdown-rendered images no longer carry their full-resolution original');
});

test('a thread wears its OWN face, in the header and against every message', () => {
  // Opening a June conversation repainted it with today's avatar — in the chat
  // header and on every message row — silently undoing the snapshot the thread
  // list had already pinned.
  assert.match(MAIN, /function threadFaceUrl\(/,
    'the thread-face helper is gone');
  assert.match(MAIN, /function avatarNode\(bot, baseClass, thread\)/,
    'message avatars no longer accept the thread they belong to');
  assert.match(MAIN, /avatarNode\(bot, 'msg-avatar', state\.activeThread\)/,
    'a message avatar is no longer painted from its thread');
  assert.match(MAIN, /paintHeaderAvatar\('ch-avatar', bot, th\)/,
    'the chat header no longer wears the thread face');
  // ...and the rail and the thread-list header deliberately do NOT.
  assert.match(MAIN, /paintHeaderAvatar\('tl-avatar', bot\);/,
    'the thread-list header should keep showing the CURRENT avatar');
  assert.match(MAIN, /btn\.append\(avatarNode\(bot, 'bot-avatar'\)\)/,
    'the left rail should keep showing the CURRENT avatar');
});

test('the header re-points its full-res target when you switch threads', () => {
  // Latching it behind a one-shot flag left the previous thread's picture
  // behind the click, which is worse than no lightbox at all.
  assert.doesNotMatch(MAIN, /if \(!state\.decoy && !dom\[key\]\._zoomWired\)/,
    'the header full-res wiring is latched again');
});
