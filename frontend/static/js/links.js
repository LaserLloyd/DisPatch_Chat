// Custom link buttons — put a website on the rail, next to the pinned
// settings, so the page you keep opening (a server panel, a dashboard) is one
// tap away instead of a bookmark hunt.
//
// Scope, deliberately narrower than pins.js even: a link button DOES nothing.
// It is an <a> that opens a URL in a new tab — no server call, no app state,
// no toggle. That is what keeps it safe to let users define freely: the worst
// a bad entry can be is a dead tab.
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

import { el } from './util.js?v=10';
import { railIcon } from './pins.js?v=5';

// The default face of a link button when no emoji is chosen: an external-link
// arrow in the same thin-stroke line language as the pinned-setting icons.
const ICON_LINK = [
  'M15 3h6v6',
  'M10 14 21 3',
  'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6',
];

const KEY = 'dispatch-custom-links';

// http(s) only. javascript:, data:, file: and friends have no business on a
// button the app itself draws; refusing here (and again on every READ, so a
// hand-edited storage entry cannot smuggle one in) keeps the whole feature
// inside "opens a web page".
export function validUrl(url) {
  try {
    const u = new URL(String(url));
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch {
    return false;
  }
}

function validEntry(e) {
  return e && typeof e === 'object'
    && typeof e.id === 'string' && e.id
    && typeof e.label === 'string' && e.label.trim()
    && validUrl(e.url);
}

/** The device's custom links, in the order they were added. Never throws: a
 *  browser with site data blocked reads as "no links", the safe default, and
 *  every entry is re-validated so an older build's (or hand-edited) row cannot
 *  put a malformed button on the rail. */
export function customLinks() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '[]');
    if (!Array.isArray(raw)) return [];
    return raw.filter(validEntry).map((e) => ({
      id: e.id,
      glyph: typeof e.glyph === 'string' ? e.glyph.trim() : '',
      label: e.label.trim(),
      url: e.url,
    }));
  } catch {
    return [];
  }
}

function save(list) {
  try { localStorage.setItem(KEY, JSON.stringify(list)); } catch { /* storage off */ }
}

/** Add a link. Returns the new entry, or null if the input does not validate
 *  (the caller shows the error; this module never renders one). */
export function addLink({ glyph, label, url }) {
  const entry = {
    id: `link-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
    glyph: typeof glyph === 'string' ? glyph.trim() : '',
    label: typeof label === 'string' ? label.trim() : '',
    url: typeof url === 'string' ? url.trim() : '',
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
 */
export function renderLinkRail(container, { decoy = false } = {}) {
  if (!container) return;
  container.querySelectorAll('.link-btn').forEach((n) => n.remove());
  if (decoy) return;                       // unlocked-only, no exceptions
  const anchor = container.querySelector('#manage-bots');   // links sit before ⚙
  for (const entry of customLinks()) {
    // A real <a>, not a button with window.open: middle-click, ctrl-click and
    // "copy link address" all behave, for free.
    const a = el('a', {
      class: 'gear-btn link-btn',
      href: entry.url,
      target: '_blank',
      rel: 'noopener noreferrer',
      title: `${entry.label} — ${entry.url}`,
      'aria-label': entry.label,
    });
    if (entry.glyph) a.textContent = entry.glyph;
    else a.append(railIcon(ICON_LINK));
    if (anchor) container.insertBefore(a, anchor);
    else container.append(a);
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
          entry.glyph ? [entry.glyph] : [railIcon(ICON_LINK)]),
        el('span', { class: 'bm-links-label', text: entry.label }),
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
  const url = el('input', {
    class: 'bm-links-in-url', type: 'url', placeholder: 'https://…',
    'aria-label': t('links.url'), inputmode: 'url', autocomplete: 'off',
    spellcheck: 'false',
  });
  const add = el('button', {
    type: 'button', class: 'btn-secondary bm-links-add', text: t('links.add'),
    onclick: () => {
      showErr('');
      if (!label.value.trim()) { showErr(t('links.need_label')); label.focus(); return; }
      if (!validUrl(url.value.trim())) { showErr(t('links.need_url')); url.focus(); return; }
      const entry = addLink({ glyph: glyph.value, label: label.value, url: url.value });
      if (!entry) { showErr(t('links.need_url')); return; }
      glyph.value = ''; label.value = ''; url.value = '';
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

  wrap.append(list, el('div', { class: 'bm-links-addrow' }, [glyph, label, url, add]), err);
  return wrap;
}
