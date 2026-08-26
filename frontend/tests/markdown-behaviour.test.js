// Behavioural tests for the markdown pipeline.
//
// Split in two halves, on purpose:
//
//   1. toPlainPreview() is pure string work and runs everywhere. The thread
//      list is a TEXT node, so raw markdown used to be shown to the reader as
//      syntax — rows read "```python", "**Done**", "![](/media/…)".
//
//   2. The SANITIZER can only be tested against a real DOM, because that is
//      what DOMPurify is: a parser plus a walk. Those tests need jsdom, which
//      this repo deliberately does not vendor (no build step, no node_modules).
//      They therefore SKIP cleanly when jsdom is absent — `node --test tests/`
//      stays green on a clean checkout and in CI — and run for anyone who has
//      it:
//
//        mkdir /tmp/jsdom && cd /tmp/jsdom && npm i --no-save jsdom
//        NODE_PATH=/tmp/jsdom/node_modules node --test frontend/tests/
//
//      A skipped security test is not a passing one. If you are changing the
//      sanitizer, install jsdom and watch these run.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');

// markdown.js guards its module-scope `window` access, so it imports under
// plain node. Only the sanitize path needs a DOM.
const { toPlainPreview, renderMarkdown } = await import(join(STATIC, 'js', 'markdown.js'));

// ---------------------------------------------------------------------------
// 1. Thread-list previews
// ---------------------------------------------------------------------------

test('toPlainPreview strips the markdown a preview would otherwise show', () => {
  const cases = [
    ['```python\nprint(1)\n```', 'print(1)'],
    ['# Heading\n\nbody', 'Heading body'],
    ['**Done**', 'Done'],
    ['*emphasis*', 'emphasis'],
    ['__strong__ and _em_', 'strong and em'],
    ['~~struck~~', 'struck'],
    ['`inline code`', 'inline code'],
    ['> quoted line', 'quoted line'],
    ['- one\n- two', 'one two'],
    ['1. first\n2. second', 'first second'],
    ['- [x] done\n- [ ] todo', 'done todo'],
    ['[label](https://example.com)', 'label'],
    ['![alt text](/media/a.png)', 'alt text'],
    ['---', ''],
    ['| a | b |\n| --- | --- |\n| 1 | 2 |', 'a b 1 2'],
    ['<https://example.com>', 'https://example.com'],
    ['[[doc:12|notes.txt]]', 'notes.txt'],
    ['[[media:/tmp/a.png]]', ''],
    ['line one\n\nline two', 'line one line two'],
  ];
  for (const [input, want] of cases) {
    assert.equal(toPlainPreview(input), want, `preview of ${JSON.stringify(input)}`);
  }
});

test('toPlainPreview leaves ordinary prose alone', () => {
  assert.equal(toPlainPreview('Deploy finished at 14:05 — 3 files changed.'),
                              'Deploy finished at 14:05 — 3 files changed.');
  // Intraword underscores are NOT emphasis in markdown, and a preview that ate
  // them would rename every identifier the agents talk about.
  assert.equal(toPlainPreview('see snake_case_name in main_module.py'),
                              'see snake_case_name in main_module.py');
});

test('toPlainPreview never returns null/undefined', () => {
  assert.equal(toPlainPreview(null), '');
  assert.equal(toPlainPreview(undefined), '');
  assert.equal(toPlainPreview(''), '');
});

// ---------------------------------------------------------------------------
// 2. The sanitizer (jsdom)
// ---------------------------------------------------------------------------

// createRequire, not `import('jsdom')`: jsdom ships as CommonJS, and require()
// honours NODE_PATH while ESM resolution does not — which is what lets the
// one-liner in the header point at a scratch install outside this repo.
const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* not installed — tests skip */ }

const dom = { skip: jsdom ? false : 'jsdom is not installed (see the header of this file)' };

// ONE window for the whole file, memoised. markdown.js registers its DOMPurify
// hooks once per module (ensureHooks), so a second window would get a
// hook-less sanitizer and the tests below would "pass" against unprotected
// output — the exact failure this test exists to catch.
let _win = null;
async function withDom() {
  if (_win) return _win;
  const { JSDOM } = jsdom;
  // runScripts:'outside-only' is what makes window.eval real — without it the
  // vendored bundles evaluate into nothing and every render silently takes the
  // fail-closed plain-text path, which looks like a passing XSS test.
  const win = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://127.0.0.1:8765/',
    runScripts: 'outside-only',
  }).window;
  // The app loads its sanitizer and parser as vendored globals; do the same.
  const run = (file) => {
    const code = readFileSync(join(STATIC, 'vendor', file), 'utf8');
    win.eval(code);
  };
  run('purify.min.js');
  run('marked.min.js');
  globalThis.window = win;
  globalThis.document = win.document;
  globalThis.DOMPurify = win.DOMPurify;
  globalThis.marked = win.marked;
  // markdown.js calls the bare globals `marked` / `DOMPurify` (they are
  // vendored <script> tags in the browser), so both realms have to see them.
  assert.ok(globalThis.DOMPurify, 'purify.min.js did not define DOMPurify');
  assert.ok(globalThis.marked, 'marked.min.js did not define marked');
  _win = win;
  return win;
}

test('sanitizer: no script survives', { skip: dom.skip }, async () => {
  await withDom();
  const out = renderMarkdown('<script>window.pwned = 1</script>hello');
  assert.ok(!/<script/i.test(out), out);
  assert.ok(!/pwned/.test(out) || !/<script/i.test(out));
});

// Raw HTML in agent/tool text is ESCAPED by the renderer (the Control UI does
// the same), so an event handler never becomes an attribute in the first place
// — it is shown as the text it was. Assert the escaping, not the absence of the
// word: `&lt;img … onerror=&quot;…&quot;&gt;` is the correct, inert output.
test('sanitizer: raw HTML is escaped, not parsed', { skip: dom.skip }, async () => {
  await withDom();
  const out = renderMarkdown('<img src="/x.png" onerror="alert(1)">');
  assert.ok(!/<img/i.test(out), out);
  assert.match(out, /&lt;img/);
});

// …and if raw HTML ever does reach the sanitizer (a future renderer change),
// DOMPurify itself must still drop the handler. Sanitize directly to pin that.
test('sanitizer: DOMPurify drops event handlers', { skip: dom.skip }, async () => {
  const win = await withDom();
  const out = win.DOMPurify.sanitize('<img src="/x.png" onerror="alert(1)">');
  assert.ok(!/onerror/i.test(out), out);
});

// M3. Media/doc directives inside a code span are QUOTED text: expanding them
// there would paste an image (or doc card) into someone's quoted JSON/curl
// example — and because the expansion inserts blank lines it also BREAKS the
// backtick span, so the image actually renders. That is how an agent's
// inject-result echoes used to leak pictures into the gateway mirror thread:
// the directive must stay visible as text instead.
test('media directive inside a code span is not expanded', { skip: dom.skip }, async () => {
  await withDom();
  const md = '**Injection Result:**\n`{"content":"I am yours. [[media:/media/685bfa8b.png|I am yours.]]","id":"x"}`';
  const out = renderMarkdown(md);
  assert.ok(!/<img/.test(out), out);
  assert.match(out, /\[\[media:\/media\/685bfa8b\.png/);
  // fenced blocks are equally off-limits
  const fenced = '```\n  1701017 [[media:/media/c2fe4877.png]]\n```';
  assert.ok(!/<img/.test(renderMarkdown(fenced)), fenced);
  // …while a directive in plain prose still renders
  assert.match(renderMarkdown('here [[media:/media/b.png|y]] ok'), /\/media\/b\.png/);
});

// M12. The fence guard has to hold for the WHOLE fence, not just its first
// content line. CODE_SPAN_RE's end-of-fence alternation once used a bare `$`
// under /m, which matches the end of ANY line — so the lazy body stopped after
// line 1 and every later line was treated as prose. The test above only ever
// exercised a one-line fence, which is precisely the case the bug did not hit.
//
// Two consequences are asserted here. The obvious one: quoted directives on
// line 2+ got expanded, so an agent explaining the syntax had its example
// silently rewritten (and an unterminated ```python fence leaked a resolved
// /api/media?path=… for whatever path it quoted). The structural one: expansion
// parks raw HTML behind a placeholder, and restore() substitutes that HTML
// wherever the token landed — inside a fence the token ends up in the copy
// button's data-code attribute, where the `"` in class="doc-card" closes the
// attribute early and a doc-card <a> or an autoplaying <video> becomes a DOM
// child of <button class=code-block-copy>. Nothing escapes into chrome now.
test('directives stay verbatim on every line of a fence, not just the first', { skip: dom.skip }, async () => {
  await withDom();

  const multi = [
    '```',
    'line1 [[media:/media/a.png|first]]',
    'line2 [[media:/media/b.png|second]]',
    'line3 [[doc:someid|quoted-doc.txt]]',
    'line4 [[media:/tmp/fake.mp4|quoted-video]]',
    '```',
  ].join('\n');
  const out = renderMarkdown(multi);
  assert.ok(!/<img/.test(out), out);
  assert.ok(!/<video/.test(out), out);
  assert.ok(!/doc-card/.test(out), out);
  for (const raw of ['[[media:/media/b.png|second]]', '[[doc:someid|quoted-doc.txt]]',
                     '[[media:/tmp/fake.mp4|quoted-video]]']) {
    assert.ok(out.includes(escapeForHtml(raw)), `${raw} was rewritten: ${out}`);
  }
  // The parked-HTML placeholder must never reach the copy button's payload.
  const copy = out.match(/data-code="([^"]*)"/);
  if (copy) {
    assert.ok(!/<(a|video|div)\b/i.test(copy[1]), copy[1]);
    assert.ok(!/doc-card|api\/media/.test(copy[1]), copy[1]);
  }

  // An UNTERMINATED fence runs to end of string, like the backend's \Z — it
  // must not decay to "protects line 1 only".
  const open = '```python\nprint(1)\nx = "[[media:/etc/hostname]]"\n';
  const openOut = renderMarkdown(open);
  assert.ok(!/api\/media/.test(openOut), openOut);
  assert.ok(openOut.includes(escapeForHtml('[[media:/etc/hostname]]')), openOut);

  // Tilde fences take the same path.
  const tilde = '~~~\na [[media:/media/a.png]]\nb [[media:/media/b.png]]\n~~~';
  assert.ok(!/<img/.test(renderMarkdown(tilde)), tilde);

  // …and text AFTER a closed fence is still ordinary prose.
  assert.match(renderMarkdown('```\nq [[media:/media/a.png]]\nr [[media:/media/b.png]]\n```\n\nafter [[media:/media/c.png|cap]] ok'),
               /\/media\/c\.png/);
});

// M13. Collapsing an untagged fence behind a "JSON" <details> on brace-matching
// alone hid ordinary code: a bash block that opens with `{`, a function body
// pasted without its signature, array-shaped output. All of it rendered folded
// and mislabelled. An untagged fence must PARSE as JSON to be treated as JSON;
// a lang-tagged ```json fence still folds on the tag, malformed or not.
test('only real JSON collapses behind the JSON details widget', { skip: dom.skip }, async () => {
  await withDom();
  const fence = (body, lang = '') => `\`\`\`${lang}\n${body}\n\`\`\``;

  // Not JSON, must NOT collapse.
  for (const body of ['{\n  echo hi\n  ls -la\n}', '{\n  return a + b;\n}',
                      '[\n  ok\n  also ok\n]', '{ this is not json }']) {
    const out = renderMarkdown(fence(body));
    assert.ok(!/json-collapse/.test(out), `collapsed non-JSON: ${body}\n${out}`);
  }

  // Real JSON in an untagged fence still collapses.
  assert.match(renderMarkdown(fence('{\n  "a": 1,\n  "b": [2, 3]\n}')), /json-collapse/);
  assert.match(renderMarkdown(fence('[\n  {"a": 1},\n  {"b": 2}\n]')), /json-collapse/);

  // A lang-tagged json fence collapses on the tag alone, even truncated.
  assert.match(renderMarkdown(fence('{ "a": 1,', 'json')), /json-collapse/);

  // A bare scalar is valid JSON but is not an object/array — no widget.
  assert.ok(!/json-collapse/.test(renderMarkdown(fence('42'))));
});

// Fences render their body HTML-escaped; compare against the same encoding.
function escapeForHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;');
}

// M2. `[/.#?]` in ALLOWED_URI_REGEXP accepted a bare leading '/', which also
// matched the PROTOCOL-RELATIVE form `//host/path` — a message could therefore
// fetch from an arbitrary third-party host (a tracking beacon in a chat).
test('sanitizer: protocol-relative URLs are rejected', { skip: dom.skip }, async () => {
  await withDom();
  for (const md of ['![x](//evil.example/beacon.gif)', '[x](//evil.example/page)']) {
    const out = renderMarkdown(md);
    assert.ok(!/\/\/evil\.example/.test(out), `${md} -> ${out}`);
  }
  // …while ordinary root-relative and absolute URLs still work.
  assert.match(renderMarkdown('![x](/media/a.png)'), /\/media\/a\.png/);
  assert.match(renderMarkdown('[x](https://example.com/a)'), /https:\/\/example\.com\/a/);
});

// L1. ADD_DATA_URI_TAGS lets data: through on img/video/source. That switch is
// per-TAG, not per-TYPE: `data:text/html,<script>…` in an <img src> is still a
// document, and DisPatch copies src into data-full for the lightbox.
test('sanitizer: data: URIs are confined to real media types', { skip: dom.skip }, async () => {
  await withDom();
  const bad = renderMarkdown('![x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)');
  assert.ok(!/data:text\/html/i.test(bad), bad);
  const good = renderMarkdown('![x](data:image/png;base64,iVBORw0KGgo=)');
  assert.match(good, /data:image\/png/);
});

test('sanitizer: javascript: URLs never reach an href', { skip: dom.skip }, async () => {
  await withDom();
  const out = renderMarkdown('[click](javascript:alert(1))');
  assert.ok(!/javascript:/i.test(out), out);
});

test('sanitizer: links open in a new tab with rel=noreferrer', { skip: dom.skip }, async () => {
  await withDom();
  const out = renderMarkdown('[x](https://example.com/)');
  assert.match(out, /rel="noreferrer noopener"/);
  assert.match(out, /target="_blank"/);
});

// ---------------------------------------------------------------------------
// 3. Tables: alignment survives the sanitizer; headers sort and resize
// ---------------------------------------------------------------------------

test('table column alignment and h5/h6 headings survive sanitization', { skip: dom.skip }, async () => {
  await withDom();
  const out = renderMarkdown('| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |');
  assert.match(out, /<th align="center">b<\/th>/, out);
  assert.match(out, /<td align="right">3<\/td>/, out);
  const heads = renderMarkdown('##### five\n###### six');
  assert.match(heads, /<h5>five<\/h5>/, heads);
  assert.match(heads, /<h6>six<\/h6>/, heads);
});

test('enhanceContent makes markdown tables sortable (numeric-aware, blanks last) and resizable', { skip: dom.skip }, async () => {
  const win = await withDom();
  const { enhanceContent } = await import(join(STATIC, 'js', 'markdown.js'));
  const div = win.document.createElement('div');
  div.innerHTML = renderMarkdown('| name | price |\n|---|---|\n| pear | $1,200 |\n| apple | 95 |\n| fig |  |\n| kiwi | 1,050.5 |');
  win.document.body.appendChild(div);
  enhanceContent(div);
  const table = div.querySelector('table');
  assert.equal(table.parentElement.className, 'table-scroll');
  const ths = Array.from(table.tHead.rows[0].cells);
  assert.equal(ths.length, 2);
  assert.ok(ths.every((th) => th.querySelector('.md-th-resize')), 'each header has a grip');
  const names = () => Array.from(table.tBodies[0].rows).map((r) => r.cells[0].textContent);

  // numeric column: 95 < 1,050.5 < $1,200, blank last
  ths[1].click();
  assert.deepEqual(names(), ['apple', 'kiwi', 'pear', 'fig']);
  assert.equal(ths[1].getAttribute('aria-sort'), 'ascending');
  ths[1].click();
  assert.deepEqual(names(), ['pear', 'kiwi', 'apple', 'fig'], 'descending still keeps the blank last');
  assert.equal(ths[1].getAttribute('aria-sort'), 'descending');
  ths[1].click();
  assert.deepEqual(names(), ['pear', 'apple', 'fig', 'kiwi'], 'third click restores authored order');
  assert.equal(ths[1].getAttribute('aria-sort'), null);

  // text column: locale, case-insensitive; sorting one column clears the other
  ths[0].click();
  assert.deepEqual(names(), ['apple', 'fig', 'kiwi', 'pear']);
  assert.equal(ths[0].getAttribute('aria-sort'), 'ascending');
  assert.equal(ths[1].getAttribute('aria-sort'), null);

  // keyboard: Enter on a focused header sorts too
  ths[0].dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  assert.equal(ths[0].getAttribute('aria-sort'), 'descending');

  // resize: a 60px drag on the first grip widens that column by 60px and
  // locks the table to fixed layout; the drag does NOT also sort.
  const grip = ths[0].querySelector('.md-th-resize');
  const ptr = (type, x, target) => target.dispatchEvent(new win.MouseEvent(type, { clientX: x, bubbles: true, button: 0 }));
  ptr('pointerdown', 100, grip);
  ptr('pointermove', 160, win.document);
  ptr('pointerup', 160, win.document);
  assert.equal(table.style.tableLayout, 'fixed');
  // jsdom has no layout, so the locked width is the 40px floor; +60 → 100px.
  assert.equal(ths[0].style.width, '100px');
  assert.equal(table.style.width, '140px', 'table width = sum of column widths');
  grip.click();
  assert.equal(ths[0].getAttribute('aria-sort'), 'descending', 'grip click never sorts');
  assert.ok(!table.classList.contains('is-resizing'));
  div.remove();
});
