// markdown.js imports the i18n function as `t` at module scope and uses it for
// the code-block copy-button labels. A `const t = code.trim()` inside
// renderCodeBlock once shadowed it, putting those earlier uses in the const
// temporal dead zone — so renderCodeBlock threw ReferenceError on EVERY fenced
// block: code degraded to plain text and block-art broke the whole message.
// It shipped to the family app and no test caught it (the render path needs a
// browser). This static guard catches the shadow class cheaply.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(HERE, '..', 'static', 'js', 'markdown.js'), 'utf8');

test('markdown.js imports the i18n function as `t`', () => {
  assert.match(SRC, /import\s*\{[^}]*\bt\b[^}]*\}\s*from\s*['"]\.\/i18n\.js/,
    'expected `import { t } from ./i18n.js` — this guard keys off that name');
});

test('no local re-declaration shadows the module-level i18n `t`', () => {
  // A local `const/let/var t = …` anywhere in the module re-binds the name for
  // its whole scope; combined with a `t(...)` call above it, that is the TDZ
  // crash. The import line itself is allowed; a destructured `{ t }` from i18n
  // is the import. Anything else declaring a bare `t` is the footgun.
  const offenders = [];
  SRC.split('\n').forEach((line, i) => {
    const code = line.replace(/\/\/.*$/, '');
    if (/\b(?:const|let|var)\s+t\s*=/.test(code)) {
      offenders.push(`markdown.js:${i + 1}  ${line.trim()}`);
    }
  });
  assert.deepEqual(offenders, [],
    '\nThese shadow the module-level i18n `t` (rename the local):\n'
    + offenders.join('\n'));
});

// --- Local Viewer wiring (docs/design/2026-09-03-local-viewer-design.md §4.2)
// Static guards: these three signatures are the contract main.js integrates
// against, and a silently-renamed option would fail OPEN — Safe Mode would keep
// rendering local-path links because `noLocal` simply never arrived.

test('renderMarkdown and enhanceContent both accept the noLocal gate', () => {
  assert.match(SRC, /export function renderMarkdown\(text,\s*\{[^}]*\bnoLocal\s*=\s*false/,
    'renderMarkdown lost its noLocal option — Safe Mode would render local links');
  assert.match(SRC, /export function enhanceContent\(container,\s*\{[^}]*\bnoLocal\s*=\s*false/,
    'enhanceContent lost its noLocal option — a local href would stay navigable');
});

test('installMarkdownHandlers takes an onOpenFile callback', () => {
  assert.match(SRC, /export function installMarkdownHandlers\(onToast,\s*\{\s*onOpenFile\s*\}\s*=\s*\{\}\)/,
    'the file-link branch can no longer open the viewer');
  // Shift/Alt-click and a long press must keep the clipboard copy.
  assert.match(SRC, /e\.shiftKey \|\| e\.altKey \|\| longPress/,
    'the copy-to-clipboard escape hatch is gone');
});

test('DisPatch\'s own URL prefixes are never treated as disk paths', () => {
  // `/media/<uuid>` is the app's media route and `/media` is also a real Linux
  // mount point. The app wins, or every inline picture would render as a file
  // link to a path that does not exist on disk.
  assert.match(SRC, /const APP_PATH_RE = .*api\|media\|static/,
    'the app-URL exclusion is gone');
});
