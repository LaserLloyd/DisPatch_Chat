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
