// Unit tests for the local viewer overlay.
//
// Run: node --test frontend/tests/
//
// The overlay itself is DOM plumbing; what is worth pinning down is the
// decision-making around it, because every one of these branches is a way the
// viewer can quietly show the wrong thing (or nothing):
//
//   * the kind → pane table, including the two No-Image-Mode exemptions
//     (html/pdf still frame — the operator asked for that page by name) and
//     the `text_ok:false` fall-through to a download card;
//   * breadcrumbs, whose only real rule is that a crumb ABOVE a served root is
//     never navigable — the client must not offer a walk out of the tree;
//   * the in-viewer history stack (‹ back), which is not the browser's;
//   * error classification: a feature-off 404 and a not-found 404 arrive with
//     the same status and must not render the same pane;
//   * that closeViewer() with nothing open is a no-op rather than a throw
//     (closeAllOverlays calls it on every drop to Safe Mode).
//
// viewer.js imports util/i18n/markdown/nim, all of which touch `document` only
// inside function bodies, so this imports cleanly under plain node — provided
// localStorage exists (nim.js) and, for viewerIcon, a document with
// createElementNS. Both are stubbed below.

import test from 'node:test';
import assert from 'node:assert/strict';

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

// The smallest thing railIcon() can build an <svg> out of: setAttribute,
// classList.add, append, and a namespace-aware factory.
function fakeEl(name) {
  return {
    nodeName: name,
    attrs: {},
    children: [],
    classList: { add() {} },
    setAttribute(k, v) { this.attrs[k] = v; },
    append(...kids) { this.children.push(...kids); },
  };
}
// i18n.js stamps <html lang>/<html dir> at module-eval time when a `document`
// exists, so the stub needs a documentElement as well as the SVG factory.
globalThis.document = {
  documentElement: fakeEl('html'),
  createElementNS: (ns, name) => fakeEl(name),
};

const {
  paneForStat, viewMode, errorKind, joinPath, computeCrumbs, makeStack,
  iconNameForKind, viewerIcon, closeViewer, viewerOpen, openViewer,
  installViewerHandlers, noteRoot, rootFor, _resetRoots, VIEWER_KINDS,
} = await import('../static/js/viewer.js');

// --- the kind → pane decision table -----------------------------------------

const stat = (over = {}) => ({ kind: 'file', viewer: 'text', size: 10, text_ok: true, ...over });

test('every backend kind maps to a pane', () => {
  const table = {
    html: 'iframe', pdf: 'iframe', image: 'image', svg: 'image', video: 'video',
    audio: 'audio', markdown: 'markdown', text: 'text', listing: 'listing',
    download: 'download',
  };
  for (const kind of VIEWER_KINDS) {
    assert.equal(paneForStat(stat({ viewer: kind })), table[kind], kind);
  }
  // An unknown classification is a download, never a blank pane.
  assert.equal(paneForStat(stat({ viewer: 'something-new' })), 'download');
  assert.equal(paneForStat(null), 'download');
});

test('No-Image Mode hides pictures and video but not a page the operator opened', () => {
  const nim = { nim: true };
  assert.equal(paneForStat(stat({ viewer: 'image' }), nim), 'nim');
  assert.equal(paneForStat(stat({ viewer: 'svg' }), nim), 'nim');
  assert.equal(paneForStat(stat({ viewer: 'video' }), nim), 'nim');
  // Explicitly requested documents still render; audio has no picture to hide.
  assert.equal(paneForStat(stat({ viewer: 'html' }), nim), 'iframe');
  assert.equal(paneForStat(stat({ viewer: 'pdf' }), nim), 'iframe');
  assert.equal(paneForStat(stat({ viewer: 'audio' }), nim), 'audio');
  assert.equal(paneForStat(stat({ viewer: 'text' }), nim), 'text');
});

test('a directory is a listing unless the server resolved it to an index page', () => {
  assert.equal(paneForStat(stat({ kind: 'dir', viewer: 'listing' })), 'listing');
  assert.equal(paneForStat(stat({ kind: 'dir', viewer: 'download' })), 'listing');
  assert.equal(paneForStat(stat({ kind: 'dir', viewer: 'html', has_index: true })), 'iframe');
});

test('text_ok:false becomes a download card, not a truncated render', () => {
  assert.equal(paneForStat(stat({ viewer: 'text', text_ok: false })), 'too_large');
  assert.equal(paneForStat(stat({ viewer: 'markdown', text_ok: false })), 'too_large');
  // Absent (an older payload) is not "false".
  const noFlag = { kind: 'file', viewer: 'markdown', size: 10 };
  assert.equal(paneForStat(noFlag), 'markdown');
});

// --- url vs path dispatch ----------------------------------------------------

test('viewMode dispatches on what was asked for', () => {
  assert.equal(viewMode({ path: '~/x.md' }), 'path');
  assert.equal(viewMode({ url: 'https://example.com' }), 'url');
  // A path wins: it is the mode that can stat, classify and go back.
  assert.equal(viewMode({ path: '/var/x', url: 'https://example.com' }), 'path');
  assert.equal(viewMode({}), null);
  assert.equal(viewMode(), null);
});

test('openViewer refuses without a target, and refuses in Safe Mode', () => {
  installViewerHandlers({ isDecoy: () => true });
  assert.equal(openViewer({ path: '~/x.md' }), null);
  assert.equal(openViewer({ url: 'https://example.com' }), null);
  installViewerHandlers({ isDecoy: () => false });
  assert.equal(openViewer({}), null);
});

// --- errors ------------------------------------------------------------------

test('feature-off and not-found are both 404 and must render differently', () => {
  assert.equal(errorKind(404, 'Local viewer is off — add a root in Settings'), 'off');
  assert.equal(errorKind(404, 'Not found'), 'not_found');
  assert.equal(errorKind(403, 'Not served by the local viewer'), 'denied');
  // A 403 that happens to carry the off message is still the off pane: the
  // detail is the only thing that names the cause.
  assert.equal(errorKind(403, 'Local viewer is off'), 'off');
  // Anything else says as little as the 403 does.
  assert.equal(errorKind(500, ''), 'denied');
  assert.equal(errorKind(undefined, undefined), 'denied');
});

// --- paths and breadcrumbs ---------------------------------------------------

test('joinPath tolerates a trailing slash on the directory', () => {
  assert.equal(joinPath('/var/x', 'y.md'), '/var/x/y.md');
  assert.equal(joinPath('/var/x/', 'y.md'), '/var/x/y.md');
  assert.equal(joinPath('~', 'y.md'), '~/y.md');
});

test('crumbs cover the whole path but only navigate at or below the boundary', () => {
  _resetRoots();
  const crumbs = computeCrumbs('~/Projects/site/css', { parent: '~/Projects/site' });
  assert.deepEqual(crumbs.map((c) => c.name), ['~', 'Projects', 'site', 'css']);
  assert.deepEqual(crumbs.map((c) => c.path),
    ['~', '~/Projects', '~/Projects/site', '~/Projects/site/css']);
  // Only the confirmed parent is navigable; the current crumb never is.
  assert.deepEqual(crumbs.map((c) => c.nav), [false, false, true, false]);
});

test('a known root widens the navigable range; above it stays plain text', () => {
  _resetRoots();
  noteRoot('~/Projects');
  assert.equal(rootFor('~/Projects/site/css'), '~/Projects');
  assert.equal(rootFor('/etc/ssh'), null);
  const crumbs = computeCrumbs('~/Projects/site/css',
    { parent: '~/Projects/site', root: rootFor('~/Projects/site/css') });
  assert.deepEqual(crumbs.map((c) => c.nav), [false, true, true, false]);
  _resetRoots();
});

test('at a root (parent null, nothing known) no crumb is navigable', () => {
  _resetRoots();
  const crumbs = computeCrumbs('/var/home/user/site', { parent: null });
  assert.deepEqual(crumbs.map((c) => c.name), ['/', 'var', 'home', 'user', 'site']);
  assert.deepEqual(crumbs.map((c) => c.path),
    ['/', '/var', '/var/home', '/var/home/user', '/var/home/user/site']);
  assert.equal(crumbs.every((c) => c.nav === false), true);
});

test('crumbs survive a trailing slash and the degenerate roots', () => {
  _resetRoots();
  assert.deepEqual(computeCrumbs('/var/x/', { parent: '/var' }).map((c) => c.path),
    ['/', '/var', '/var/x']);
  assert.deepEqual(computeCrumbs('~', { parent: null }).map((c) => c.name), ['~']);
  assert.deepEqual(computeCrumbs('/', { parent: null }).map((c) => c.name), ['/']);
  // The deepest matching root wins when several are served.
  noteRoot('~/a');
  noteRoot('~/a/b');
  assert.equal(rootFor('~/a/b/c'), '~/a/b');
  _resetRoots();
});

// --- the in-viewer history stack --------------------------------------------

test('the stack walks listing → file → back, and bottoms out', () => {
  const s = makeStack();
  assert.equal(s.depth(), 0);
  assert.equal(s.current(), null);
  assert.equal(s.canGoBack(), false);
  assert.equal(s.back(), null);

  s.push({ path: '~/site' });
  assert.equal(s.canGoBack(), false, 'the ‹ button is hidden at depth 1');
  s.push({ path: '~/site/index.html' });
  assert.equal(s.depth(), 2);
  assert.equal(s.canGoBack(), true);
  assert.deepEqual(s.back(), { path: '~/site' });
  assert.equal(s.depth(), 1);
  // Back at the bottom is a no-op, not an empty stack.
  assert.equal(s.back(), null);
  assert.deepEqual(s.current(), { path: '~/site' });
});

test('replace swaps the current entry (reload) without growing the stack', () => {
  const s = makeStack();
  s.push({ path: 'a' });
  s.push({ path: 'b' });
  s.replace({ path: 'b2' });
  assert.equal(s.depth(), 2);
  assert.deepEqual(s.current(), { path: 'b2' });
  assert.deepEqual(s.all().map((e) => e.path), ['a', 'b2']);
  // Replace on an empty stack pushes instead of dropping the entry.
  const empty = makeStack();
  empty.replace({ path: 'x' });
  assert.equal(empty.depth(), 1);
});

// --- icons -------------------------------------------------------------------

test('every kind has an icon, and directories are the only folder', () => {
  for (const kind of VIEWER_KINDS.concat(['dir', 'folder', undefined, 'nonsense'])) {
    const name = iconNameForKind(kind);
    assert.ok(name === 'viewer-file' || name === 'viewer-folder', String(kind));
    const svg = viewerIcon(kind);
    assert.equal(svg.nodeName, 'svg', String(kind));
    assert.ok(svg.children.length > 0, `${kind} icon has no paths`);
  }
  assert.equal(iconNameForKind('listing'), 'viewer-folder');
  assert.equal(iconNameForKind('dir'), 'viewer-folder');
  assert.equal(iconNameForKind('pdf'), 'viewer-file');
});

// --- close -------------------------------------------------------------------

test('closeViewer with nothing open is a no-op, not a throw', () => {
  assert.equal(viewerOpen(), false);
  assert.doesNotThrow(() => closeViewer());
  assert.equal(closeViewer(), null);
  assert.equal(viewerOpen(), false);
});

test('installViewerHandlers ignores junk and keeps working defaults', () => {
  const cfg = installViewerHandlers({ onToast: 'nope', isDecoy: null, api: { stat: 1 } });
  assert.equal(typeof cfg.onToast, 'function');
  assert.equal(typeof cfg.isDecoy, 'function');
  assert.equal(typeof cfg.api.stat, 'function');
  assert.equal(typeof cfg.api.ls, 'function');
  // A complete api is taken.
  const api = { stat: async () => ({}), ls: async () => ({}) };
  assert.equal(installViewerHandlers({ api }).api, api);
  installViewerHandlers({ api: { stat: async () => ({}), ls: async () => ({}) } });
});
