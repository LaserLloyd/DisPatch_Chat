// Custom link buttons — put a website (or a local file) on the rail, next to
// the pinned settings, so the page you keep opening (a server panel, a
// dashboard, a report on disk) is one tap away instead of a bookmark hunt.
//
// Scope, deliberately narrower than pins.js even: a link button DOES nothing.
// It either is an <a> that opens a URL in a new tab, or a button that hands a
// target to the local viewer — no server call of its own, no app state, no
// toggle. That is what keeps it safe to let users define freely: the worst a
// bad entry can be is a dead tab or a viewer pane that says "not served".
//
// The viewer target is NOT a trust boundary that lives here. This module only
// keeps the shape of a target honest (http(s), or an absolute local path with
// no traversal); what may actually be READ is the server's roots allowlist and
// built-in deny list, checked again on every request. A link button cannot
// widen that — it can only ask.
//
// The tier gate is stricter than the pins', on purpose: custom links are
// UNLOCKED-ONLY, with no per-link opt-in. The URLs people put here are exactly
// the ones Safe Mode exists to not advertise — internal panels, tailnet hosts,
// admin dashboards — and a family tablet has no business rendering them. The
// rail is rebuilt on every tier change (main.js renderPins), so locking the
// device removes the buttons on the spot. The Settings editor follows the same
// rule: it is simply not built for a Safe-Mode session.
//
// Storage is localStorage, like the pins: a link is a per-device convenience,
// not an account preference. The key is in privacy.js APP_KEYS — privacy
// mode's wipe must forget a device's links like everything else it forgets.

import { el, railIcon, RAIL_ICONS } from './util.js?v=13';

const KEY = 'dispatch-custom-links';

// http(s) only. javascript:, data:, file: and friends have no business on a
// button the app itself draws; refusing here (and again on every READ, so a
// hand-edited storage entry cannot smuggle one in) keeps the whole feature
// inside "opens a web page".
//
// Kept exported, and kept meaning exactly what it always meant, because it is
// the URL half of validTarget and callers (and tests) still ask it directly.
export function validUrl(url) {
  try {
    const u = new URL(String(url));
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch {
    return false;
  }
}

const MAX_PATH = 1024;

// A local path is a *shape*, not a permission: absolute (`/…`) or home-rooted
// (`~`, `~/…`), no `..` segment, no control characters or whitespace, and
// bounded so a pathological entry cannot be used to bloat a request line. The
// `~foo` form (another user's home) is refused — the server only ever expands a
// literal `~` first segment, so anything else would be a lie on the button.
function validPath(value) {
  if (value.length > MAX_PATH) return false;
  if (value !== '~' && value !== '/' && !value.startsWith('/') && !value.startsWith('~/')) {
    return false;
  }
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(value)) return false;   // control chars, newlines
  if (/\s/.test(value)) return false;                       // no spaces or tabs
  return !value.split('/').includes('..');
}

/** Classify a link target. Returns `{ kind: 'url'|'path', value }` for
 *  something this module is willing to put on the rail, or null.
 *
 *  Order matters: the path test runs first, so a path is never handed to
 *  `new URL` (which would happily parse some of them as scheme-relative junk),
 *  and everything that is not a path must survive the http(s)-only URL rule —
 *  which is what keeps `file:`, `javascript:` and `data:` out. */
export function validTarget(value) {
  if (typeof value !== 'string') return null;
  const v = value.trim();
  if (!v) return null;
  if (v.startsWith('/') || v.startsWith('~')) {
    return validPath(v) ? { kind: 'path', value: v } : null;
  }
  return validUrl(v) ? { kind: 'url', value: v } : null;
}

// A local path has no "open in a tab" reading — the browser cannot fetch it and
// navigating the app to a raw path would just 404 the SPA. So the mode is
// derived, not trusted: paths are always 'viewer', and a stored mode we do not
// recognise reads as the safe default.
function openModeFor(kind, open) {
  if (kind === 'path') return 'viewer';
  return open === 'viewer' ? 'viewer' : 'tab';
}

function validEntry(e) {
  return !!(e && typeof e === 'object'
    && typeof e.id === 'string' && e.id
    && typeof e.label === 'string' && e.label.trim()
    && validTarget(e.url));
}

/** The device's custom links, in the order they were added. Never throws: a
 *  browser with site data blocked reads as "no links", the safe default, and
 *  every entry is re-validated so an older build's (or hand-edited) row cannot
 *  put a malformed button on the rail. */
export function customLinks() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '[]');
    if (!Array.isArray(raw)) return [];
    return raw.filter(validEntry).map((e) => {
      const target = validTarget(e.url);
      return {
        id: e.id,
        glyph: typeof e.glyph === 'string' ? e.glyph.trim() : '',
        label: e.label.trim(),
        url: target.value,
        // Derived on every read, never simply echoed: a legacy row has no
        // `open` at all (→ 'tab') and a path row is forced to 'viewer' even if
        // storage says otherwise.
        open: openModeFor(target.kind, e.open),
      };
    });
  } catch {
    return [];
  }
}

function save(list) {
  try { localStorage.setItem(KEY, JSON.stringify(list)); } catch { /* storage off */ }
}

/** Add a link. Returns the new entry, or null if the input does not validate
 *  (the caller shows the error; this module never renders one). */
export function addLink({ glyph, label, url, open }) {
  const target = validTarget(url);
  const entry = {
    id: `link-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
    glyph: typeof glyph === 'string' ? glyph.trim() : '',
    label: typeof label === 'string' ? label.trim() : '',
    url: target ? target.value : (typeof url === 'string' ? url.trim() : ''),
    open: target ? openModeFor(target.kind, open) : 'tab',
  };
  if (!validEntry(entry)) return null;
  save([...customLinks(), entry]);
  return entry;
}

/** Remove by id. Unknown ids are a no-op, not an error. */
export function removeLink(id) {
  save(customLinks().filter((e) => e.id !== id));
}

/** Render the link buttons into the rail, after the pins, before ⚙.
 *
 *  Same contract as renderPinnedRail: rebuilt wholesale on every call, and the
 *  caller (main.js renderPins) invokes it on every tier change — which is what
 *  makes the unlocked-only rule hold without this module watching anything.
 *
 *  `openViewer` is the local-viewer entry point (viewer.js `openViewer`),
 *  injected rather than imported so this module keeps no opinion about the
 *  overlay. Without it, viewer-mode URLs degrade to the plain new-tab <a> and
 *  local paths are simply not drawn: a raw path in an href would navigate the
 *  APP to a path it cannot serve, which is a broken button pretending to work.
 */
export function renderLinkRail(container, { decoy = false, openViewer = null } = {}) {
  if (!container) return;
  container.querySelectorAll('.link-btn').forEach((n) => n.remove());
  if (decoy) return;                       // unlocked-only, no exceptions
  const anchor = container.querySelector('#manage-bots');   // links sit before ⚙
  const place = (node) => {
    if (anchor) container.insertBefore(node, anchor);
    else container.append(node);
  };
  for (const entry of customLinks()) {
    const target = validTarget(entry.url);
    if (!target) continue;                 // belt and braces; customLinks filtered
    const viewer = entry.open === 'viewer' && typeof openViewer === 'function';
    if (!viewer && target.kind === 'path') continue;   // never href a raw path
    const attrs = {
      class: 'gear-btn link-btn',
      title: `${entry.label} — ${entry.url}`,
      'aria-label': entry.label,
    };
    // A real <a>, not a button with window.open: middle-click, ctrl-click and
    // "copy link address" all behave, for free. Viewer entries cannot have
    // that (there is no URL to copy) and are honest buttons instead.
    const node = viewer
      ? el('button', {
        ...attrs,
        type: 'button',
        onclick: () => openViewer(
          target.kind === 'path' ? { path: target.value } : { url: target.value },
        ),
      })
      : el('a', { ...attrs, href: target.value, target: '_blank', rel: 'noopener noreferrer' });
    if (entry.glyph) node.textContent = entry.glyph;
    else node.append(railIcon(RAIL_ICONS.link));
    place(node);
  }
}

/** The Settings → Device editor: current links, each with a remove ✕, and an
 *  add row (glyph · label · URL). Built fresh on every Device-pane mount, like
 *  its neighbours; `onChange` is how the rail finds out (main.js passes
 *  renderPins). Never built for a Safe-Mode session — the caller checks. */
export function linksSection(t, { onChange = null } = {}) {
  const wrap = el('div', { class: 'bm-links', id: 'links-row' });
  wrap.append(el('span', {}, [
    el('strong', { text: t('links.title') }),
    ` — ${t('links.hint')}`,
  ]));

  const list = el('div', { class: 'bm-links-list' });
  const err = el('p', { class: 'bm-links-error', role: 'alert', hidden: '' });
  const showErr = (msg) => { err.textContent = msg; err.hidden = !msg; };

  const paint = () => {
    list.innerHTML = '';
    for (const entry of customLinks()) {
      list.append(el('div', { class: 'bm-links-item' }, [
        el('span', { class: 'bm-links-glyph', 'aria-hidden': 'true' },
          entry.glyph ? [entry.glyph] : [railIcon(RAIL_ICONS.link)]),
        el('span', { class: 'bm-links-label' }, [
          entry.label,
          // Viewer-mode rows are visibly different, or the list would show two
          // identical-looking rows that behave differently on tap.
          entry.open === 'viewer'
            ? el('span', {
              class: 'link-viewer-mark',
              title: t('links.open_in_viewer'),
              'aria-label': t('links.open_in_viewer'),
              text: ' ⧉',
            })
            : null,
        ]),
        el('span', { class: 'bm-links-url', text: entry.url }),
        el('button', {
          type: 'button',
          class: 'bm-links-remove',
          title: t('links.remove'),
          'aria-label': `${t('links.remove')}: ${entry.label}`,
          text: '✕',
          onclick: () => {
            removeLink(entry.id);
            paint();
            if (onChange) onChange();
          },
        }),
      ]));
    }
  };
  paint();

  const glyph = el('input', {
    class: 'bm-links-in-glyph', maxlength: '4', placeholder: '🔗',
    'aria-label': t('links.glyph'),
  });
  const label = el('input', {
    class: 'bm-links-in-label', maxlength: '40', placeholder: t('links.label'),
    'aria-label': t('links.label'),
  });
  // type=text, not type=url: the field now accepts `~/reports/x.md` too, and a
  // type=url input reports that as invalid to the browser's own UI while we
  // validate it ourselves.
  const url = el('input', {
    class: 'bm-links-in-url', type: 'text', placeholder: t('links.url_or_path'),
    'aria-label': t('links.url_or_path'), inputmode: 'url', autocomplete: 'off',
    spellcheck: 'false',
  });
  const viewerBox = el('input', {
    class: 'bm-links-in-viewer', type: 'checkbox',
    'aria-label': t('links.open_in_viewer'),
  });
  const viewerLabel = el('label', { class: 'bm-links-viewer-toggle' }, [
    viewerBox, el('span', { text: t('links.open_in_viewer') }),
  ]);
  // A local path has nowhere else to go, so the choice is made for you and the
  // control says so rather than silently disagreeing with what happens.
  const syncViewerBox = () => {
    const target = validTarget(url.value);
    const forced = !!target && target.kind === 'path';
    viewerBox.disabled = forced;
    if (forced) viewerBox.checked = true;
  };
  url.addEventListener('input', syncViewerBox);
  syncViewerBox();

  const add = el('button', {
    type: 'button', class: 'btn-secondary bm-links-add', text: t('links.add'),
    onclick: () => {
      showErr('');
      if (!label.value.trim()) { showErr(t('links.need_label')); label.focus(); return; }
      if (!validTarget(url.value)) { showErr(t('links.need_url')); url.focus(); return; }
      const entry = addLink({
        glyph: glyph.value,
        label: label.value,
        url: url.value,
        open: viewerBox.checked ? 'viewer' : 'tab',
      });
      if (!entry) { showErr(t('links.need_url')); return; }
      glyph.value = ''; label.value = ''; url.value = '';
      viewerBox.checked = false;
      syncViewerBox();
      paint();
      if (onChange) onChange();
      label.focus();
    },
  });
  // Enter in any field adds — the three inputs are one form in spirit, and the
  // pane has no <form> to submit.
  for (const input of [glyph, label, url]) {
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); add.click(); }
    });
  }

  wrap.append(
    list,
    el('div', { class: 'bm-links-addrow' }, [glyph, label, url, viewerLabel, add]),
    err,
  );
  return wrap;
}
