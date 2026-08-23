// The Content-Security-Policy in index.html, kept honest.
//
// The policy hashes the four inline <script> blocks instead of allowing
// 'unsafe-inline'. That is the right trade — an injected <script> still cannot
// run — but it is BYTE-EXACT: reindent a no-FOUC script by one space and the
// hash no longer matches, the browser refuses to run it, and the app boots
// with no theme, no language, no direction and no service worker. Nothing
// throws; it just quietly looks wrong.
//
// So this test does what a human cannot be relied on to do: recompute every
// hash from the current source and compare it to the meta tag.

import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const HTML = readFileSync(join(STATIC, 'index.html'), 'utf8');
// Comments are stripped before anything looks for tags. The CSP comment in
// index.html discusses inline <script> and style="" by name, and a naive scan
// happily matched the word inside the comment — which silently shifted the
// first "script block" to start in the middle of the documentation and made
// every hash wrong. Strip first, then parse.
const MARKUP = HTML.replace(/<!--[\s\S]*?-->/g, '');
const MAIN = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');

const sha256 = (text) => `'sha256-${createHash('sha256').update(text, 'utf8').digest('base64')}'`;

function policy() {
  const m = /<meta http-equiv="Content-Security-Policy" content="([\s\S]*?)"\s*\/?>/.exec(MARKUP);
  assert.ok(m, 'index.html has no Content-Security-Policy meta tag');
  const out = new Map();
  for (const part of m[1].split(';')) {
    const tokens = part.trim().split(/\s+/).filter(Boolean);
    if (!tokens.length) continue;
    out.set(tokens[0], tokens.slice(1));
  }
  return out;
}

// Inline = a <script> with no src attribute. Matches the browser's own notion
// of what needs a hash.
const inlineScripts = () =>
  [...MARKUP.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);

test('script-src hashes match the inline scripts byte for byte', () => {
  const blocks = inlineScripts();
  assert.ok(blocks.length >= 3, `expected the no-FOUC scripts, found ${blocks.length}`);
  const want = blocks.map(sha256);
  const have = policy().get('script-src') || [];
  const missing = want.filter((h) => !have.includes(h));
  assert.deepEqual(missing, [],
    '\nAn inline <script> in index.html changed but its hash in the CSP meta did\n'
    + 'not. Recompute and paste these into script-src:\n\n' + want.join('\n')
    + '\n\nCurrent script-src: ' + have.join(' '));
  // And no stale hashes left behind: a hash that matches nothing is a hole
  // pointing at a script that no longer exists.
  const stale = have.filter((tok) => tok.startsWith("'sha256-") && !want.includes(tok));
  assert.deepEqual(stale, [], `\nstale script hashes in the CSP (delete them):\n${stale.join('\n')}`);
});

test('style-src hashes the ComfyUI holding-page <style>', () => {
  // That popup is opened as about:blank, so it INHERITS this document's policy.
  // Its <style> block therefore needs a hash here or the holding page paints
  // unstyled while ComfyUI cold-starts.
  const styles = [...MAIN.matchAll(/<style>([\s\S]*?)<\/style>/g)].map((m) => m[1]);
  assert.equal(styles.length, 1, 'expected exactly one inline <style> in main.js');
  const have = policy().get('style-src') || [];
  assert.ok(have.includes(sha256(styles[0])),
    `\nThe holding-page <style> changed. Put this in style-src:\n${sha256(styles[0])}\n`
    + `Current style-src: ${have.join(' ')}`);
});

test("the policy keeps its non-negotiables", () => {
  const p = policy();
  assert.deepEqual(p.get('object-src'), ["'none'"]);
  assert.deepEqual(p.get('base-uri'), ["'none'"]);
  assert.deepEqual(p.get('form-action'), ["'self'"]);
  assert.deepEqual(p.get('default-src'), ["'self'"]);
  for (const directive of ['script-src', 'style-src']) {
    assert.ok(!(p.get(directive) || []).includes("'unsafe-inline'"),
      `${directive} must not fall back to 'unsafe-inline' — hash the block instead`);
    assert.ok(!(p.get(directive) || []).includes("'unsafe-eval'"),
      `${directive} must not allow 'unsafe-eval'`);
  }
});

// An inline style ATTRIBUTE (style="…") is blocked by style-src 'self'; the
// CSSOM (element.style.width = …) is not. This is the check that stops someone
// reintroducing the former and finding out in production.
test('no inline style="" attributes in the markup', () => {
  const hits = [...MARKUP.matchAll(/\sstyle="[^"]*"/g)].map((m) => m[0].trim());
  assert.deepEqual(hits, [],
    '\nInline style attributes are blocked by this page\'s CSP. Move them to\n'
    + 'app.css (or set them through element.style.*):\n' + hits.join('\n'));
});

test('no JS builds an inline style attribute', () => {
  // el() writes anything that is not a known key through setAttribute, so
  // `el('div', { style: '…' })` produces exactly the attribute the policy
  // blocks. Same for an explicit setAttribute('style', …).
  const files = ['main.js', 'reactions.js', 'dashboard.js', 'llm.js', 'nim.js',
                 'privacy.js', 'markdown.js', 'theme.js', 'about.js', 'util.js'];
  const hits = [];
  for (const f of files) {
    const src = readFileSync(join(STATIC, 'js', f), 'utf8');
    src.split('\n').forEach((line, i) => {
      const code = line.replace(/\/\/.*$/, '');
      if (/setAttribute\(\s*['"]style['"]/.test(code)) hits.push(`${f}:${i + 1} ${line.trim()}`);
      // `style:` as an el() option — but NOT `{ style: 'percent' }`, which is
      // an Intl option, and not `.style.foo =`, which is CSSOM.
      if (/(?:^|[{,]\s*)style\s*:\s*['"`]/.test(code) && !/intl\(|Intl\./.test(code)) {
        hits.push(`${f}:${i + 1} ${line.trim()}`);
      }
    });
  }
  assert.deepEqual(hits, [],
    '\nThese build an inline style attribute, which the CSP blocks:\n' + hits.join('\n'));
});
