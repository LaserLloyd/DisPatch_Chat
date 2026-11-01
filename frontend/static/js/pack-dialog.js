// The Add-a-pack dialog (4.4): one modal per starter pack. It lists the pack's bots with a
// picture (or the emoji), a name, a short description and a checkbox; bots already added are
// ticked, greyed and marked "Added". Select all, the connection picker, then Add selected (N),
// Add all or Cancel. It sends `only` with the chosen ids (POST /api/starter-packs/<id>/install).
//
// Opened from the household Add menu (main.js) and from the pack cards on Settings -> Characters
// -> Household (characters-page.js), both only for a profile with manage_characters: the server
// refuses everyone else anyway.
//
// Pure helpers first (tested without a DOM), then the dialog.
import { t } from './i18n.js?v=5';
import { el } from './util.js?v=23';
import { nimEnabled } from './nim.js?v=8';

// ------------------------------------------------------------------ pure

/** The ids of the pack's bots that are not added yet, in pack order. */
export function addableIds(pack) {
  return ((pack && pack.characters) || []).filter((c) => c && !c.installed).map((c) => c.id);
}

/** What the controls show for a selection (`selected`: a Set of ids). Installed bots never
 *  count, whatever the set holds. */
export function selectionState(pack, selected) {
  const addable = addableIds(pack);
  const n = addable.filter((id) => selected.has(id)).length;
  return {
    addable: addable.length,
    count: n,
    all: addable.length > 0 && n === addable.length,
    some: n > 0 && n < addable.length,
    canAddSelected: n > 0,
    canAddAll: addable.length > 0,
  };
}

/** Select all on (every addable bot) or off (none). */
export function selectAll(pack, on) {
  return new Set(on ? addableIds(pack) : []);
}

/** One bot ticked or unticked; an installed bot cannot be. */
export function toggleOne(pack, selected, id, on) {
  const next = new Set(selected);
  if (!addableIds(pack).includes(id)) return next;
  if (on) next.add(id); else next.delete(id);
  return next;
}

/** The install body. `mode` 'selected' sends the ticked ids, 'all' every addable id (never an
 *  empty `only`: the server reads an empty list as "the whole pack"). Null = nothing to add. */
export function installBody(pack, selected, mode, connection) {
  const addable = addableIds(pack);
  const only = mode === 'all' ? addable : addable.filter((id) => selected.has(id));
  if (!only.length) return null;
  const body = { only };
  if (connection) body.connection = connection;
  return body;
}

/** The pack card's one-line state: how many bots, how many already added. */
export function packSummary(pack) {
  const all = ((pack && pack.characters) || []).length;
  const added = all - addableIds(pack).length;
  if (added === 0) return t('packs.count', { count: all });
  if (added === all) return t('packs.all_added');
  return t('packs.some_added', { count: added, total: all });
}

// ------------------------------------------------------------------ the dialog

let seq = 0;

/**
 * Show the dialog for `pack` (a GET /api/starter-packs row). Resolves the install result, or
 * null when it was cancelled.
 *   opts: { connections: [{id, name}], install(packId, body) -> Promise<result>, toast(msg, bad),
 *           errorText(e), doc }
 */
export function packDialog(pack, opts = {}) {
  const doc = opts.doc || document;
  const conns = opts.connections || [];
  const toast = opts.toast || (() => {});
  const errText = opts.errorText || ((e) => String((e && e.message) || e));
  return new Promise((resolve) => {
    const id = `pk-dlg-${++seq}`;
    const opener = doc.activeElement;
    let selected = new Set();
    let busy = false;
    const done = (v) => {
      doc.removeEventListener('keydown', onKey, true);
      back.remove();
      try { if (opener && opener.focus && opener.isConnected) opener.focus(); } catch { /* ignore */ }
      resolve(v);
    };

    const noPics = (() => { try { return nimEnabled(); } catch { return false; } })();
    const rows = (pack.characters || []).map((c) => {
      const cb = el('input', { type: 'checkbox', class: 'ui-checkbox pk-check', id: `${id}-c-${c.id}`, dataset: { id: c.id } });
      if (c.installed) { cb.checked = true; cb.disabled = true; }
      cb.addEventListener('change', () => { selected = toggleOne(pack, selected, c.id, cb.checked); sync(); });
      const pic = c.picture && !noPics
        ? el('img', { class: 'pk-pic', src: c.picture, alt: '', width: '48', height: '48', loading: 'lazy', decoding: 'async' })
        : el('span', { class: 'pk-pic pk-emoji', 'aria-hidden': 'true', text: c.emoji || '🙂' });
      if (c.color) pic.style.setProperty('--pk-color', c.color);
      return el('li', { class: 'pk-row' + (c.installed ? ' pk-row--added' : ''), dataset: { id: c.id } }, [
        el('label', { class: 'pk-label', for: cb.id }, [
          cb, pic,
          el('span', { class: 'pk-text' }, [
            el('strong', { class: 'pk-name', text: c.name }),
            c.description ? el('span', { class: 'pk-desc', text: c.description }) : null,
          ]),
          c.installed ? el('span', { class: 'ui-badge ui-badge--success pk-added', text: t('packs.added') }) : null,
        ]),
      ]);
    });

    const allBox = el('input', { type: 'checkbox', class: 'ui-checkbox', id: `${id}-all` });
    allBox.addEventListener('change', () => {
      selected = selectAll(pack, allBox.checked);
      for (const cb of box.querySelectorAll('.pk-check:not(:disabled)')) cb.checked = selected.has(cb.dataset.id);
      sync();
    });
    const allRow = el('div', { class: 'pk-all' }, [el('label', { for: allBox.id }, [allBox, el('span', { text: t('packs.select_all') })])]);

    const sel = el('select', { class: 'ui-select', id: `${id}-conn` }, [
      el('option', { value: '', text: t('chars.no_connection') }),
      ...conns.map((c) => el('option', { value: c.id, text: c.name || c.id }))]);
    if (conns.length) sel.value = conns[0].id;

    const addSel = el('button', { type: 'button', class: 'btn-primary pk-add-selected' });
    const addAll = el('button', { type: 'button', class: 'btn-secondary pk-add-all', text: t('packs.add_all') });
    const cancel = el('button', { type: 'button', class: 'btn-secondary pk-cancel', text: t('common.cancel') });
    const status = el('p', { class: 'pk-status', role: 'status', 'aria-live': 'polite' });

    async function go(mode) {
      if (busy) return;
      const body = installBody(pack, selected, mode, sel.value);
      if (!body) return;
      busy = true; sync();
      try {
        const r = await opts.install(pack.id, body);
        const n = ((r && r.installed) || []).length;
        toast(t('chars.pack_done', { count: n }));
        done(r || { installed: [] });
      } catch (e) {
        busy = false; sync();
        status.textContent = errText(e);
        toast(errText(e), true);
      }
    }
    addSel.addEventListener('click', () => go('selected'));
    addAll.addEventListener('click', () => go('all'));
    cancel.addEventListener('click', () => done(null));

    function sync() {
      const s = selectionState(pack, selected);
      addSel.textContent = t('packs.add_selected', { count: s.count });
      addSel.disabled = busy || !s.canAddSelected;
      addAll.disabled = busy || !s.canAddAll;
      allBox.checked = s.all;
      allBox.indeterminate = s.some;
      allBox.disabled = busy || !s.addable;
      sel.disabled = busy || !s.addable;
      for (const cb of box.querySelectorAll('.pk-check')) {
        const c = (pack.characters || []).find((x) => x.id === cb.dataset.id);
        cb.disabled = busy || !!(c && c.installed);
      }
      box.setAttribute('aria-busy', String(busy));
    }

    const everything = !addableIds(pack).length;
    const box = el('div', { class: 'pe-dialog pk-dialog', role: 'dialog', 'aria-modal': 'true',
      'aria-labelledby': `${id}-t`, 'aria-describedby': `${id}-b` }, [
      el('h3', { class: 'pe-dialog-title', id: `${id}-t`, text: pack.name }),
      el('p', { class: 'pe-dialog-body', id: `${id}-b`, text: pack.description || '' }),
      everything ? el('p', { class: 'pe-dialog-note', text: t('packs.all_added_note') }) : allRow,
      el('ul', { class: 'pk-list', 'aria-label': t('packs.list_aria', { name: pack.name }) }, rows),
      el('div', { class: 'sec-field pk-conn' }, [el('label', { for: sel.id, text: t('chars.packs_connection') }), sel]),
      el('p', { class: 'pe-dialog-note', text: t('packs.who_note') }),
      status,
      el('div', { class: 'pe-dialog-actions' }, [cancel, addAll, addSel]),
    ]);
    const back = el('div', { class: 'pe-backdrop pk-backdrop' }, [box]);
    back.addEventListener('mousedown', (e) => { if (e.target === back && !busy) done(null); });
    const focusables = () => [...box.querySelectorAll('button, input, select')].filter((x) => !x.disabled);
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); if (!busy) done(null); return; }
      if (e.key !== 'Tab') return;
      const f = focusables();
      if (!f.length) return;
      const i = f.indexOf(doc.activeElement);
      if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && (i === f.length - 1 || i === -1)) { e.preventDefault(); f[0].focus(); }
    };
    doc.addEventListener('keydown', onKey, true);
    sync();
    doc.body.append(back);
    // The first bot to tick, else Cancel (everything already added).
    const first = box.querySelector('.pk-check:not(:disabled)') || cancel;
    try { first.focus(); } catch { /* ignore */ }
  });
}
