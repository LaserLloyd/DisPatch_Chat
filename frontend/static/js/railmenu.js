// Rail layout — which static rail buttons sit on the bar and which live in the
// "Tools & more" popup (js/tools.js), in what order.
//
// The same idea as menubots.js (a bot parked in the Tools popup), applied to the
// fixed buttons under the roster: the profile chip, Tasks, the File Server, the
// theme button, Lock / Unlock, Settings. The rail had grown a column of glyphs;
// most of them are opened rarely, so by default the profile chip, Tasks, the
// File Server and the theme button go into the popup and the bar keeps Lock /
// Unlock and Settings (and the Restricted send-a-file button, see below).
//
// HOW IT WORKS. The buttons are the real elements from index.html, with their
// ids, handlers and visibility classes; this module only MOVES them between
// #gear-row (the bar) and #tools-more (a section of the Tools popup), in the
// stored order. Whatever decides that a button is hidden for this profile
// (`.hidden`, set by main.js / tasks.js / signin.js) keeps deciding it wherever
// the button sits, so moving one never exposes anything.
//
// A DEVICE setting, like menubots.js and pins.js: localStorage, wrapped in
// try/catch, and a missing or corrupt value is the default layout.
//
// RESTRICTED IGNORES THIS ENTIRELY, like parked bots: a locked / Restricted
// device always gets every button back on the bar in the shipped order, so its
// reduced set is exactly what it was, and nothing it cannot reach is hidden in
// a popup it may not even have.

export const KEY = 'dispatch-rail-layout';

/** The drag payload's type (a private MIME, so nothing else reads it as text). */
export const RAIL_DRAG_TYPE = 'application/x-dispatch-rail';

/** Every managed button, in the shipped (index.html) order. */
export const RAIL_ITEMS = ['theme-toggle', 'profile-chip', 'lock-now', 'tasks-btn', 'fs-chip', 'manage-bots'];

/** Where each one goes when nobody has moved anything. */
export const DEFAULT_MENU = ['profile-chip', 'tasks-btn', 'fs-chip', 'theme-toggle'];

export function defaultLayout() {
  return {
    rail: RAIL_ITEMS.filter((id) => !DEFAULT_MENU.includes(id)),
    menu: DEFAULT_MENU.slice(),
  };
}

/** Any stored value -> a complete layout: unknown ids dropped, duplicates
 *  dropped, an id the stored value does not mention goes where the default
 *  puts it (at the end of that list), so a button added in a later release
 *  appears instead of vanishing. */
export function normalizeLayout(raw) {
  const def = defaultLayout();
  const seen = new Set();
  const take = (list) => (Array.isArray(list) ? list : [])
    .filter((id) => typeof id === 'string' && RAIL_ITEMS.includes(id) && !seen.has(id) && seen.add(id));
  const src = raw && typeof raw === 'object' ? raw : {};
  const rail = take(src.rail);
  const menu = take(src.menu);
  for (const id of def.rail) if (!seen.has(id)) rail.push(id);
  for (const id of def.menu) if (!seen.has(id)) menu.push(id);
  return { rail, menu };
}

export function loadLayout() {
  try { return normalizeLayout(JSON.parse(localStorage.getItem(KEY) || 'null')); }
  catch { return defaultLayout(); }
}

export function saveLayout(layout) {
  const l = normalizeLayout(layout);
  try { localStorage.setItem(KEY, JSON.stringify(l)); } catch { /* storage off: this tab only */ }
  return l;
}

/** Which list `id` is in ('rail' | 'menu' | null). */
export function zoneOf(layout, id) {
  if (layout.rail.includes(id)) return 'rail';
  if (layout.menu.includes(id)) return 'menu';
  return null;
}

/** Move `id` into `zone`, before `beforeId` (or at the end when it is null or
 *  not in that zone). Pure: returns a new layout. */
export function moveItem(layout, id, zone, beforeId = null) {
  const l = normalizeLayout(layout);
  if (!RAIL_ITEMS.includes(id) || (zone !== 'rail' && zone !== 'menu')) return l;
  l.rail = l.rail.filter((x) => x !== id);
  l.menu = l.menu.filter((x) => x !== id);
  const list = l[zone];
  const at = beforeId && beforeId !== id ? list.indexOf(beforeId) : -1;
  if (at < 0) list.push(id); else list.splice(at, 0, id);
  return l;
}

/** One step up (-1) or down (+1) inside its own list. */
export function nudgeItem(layout, id, delta) {
  const l = normalizeLayout(layout);
  const zone = zoneOf(l, id);
  if (!zone) return l;
  const list = l[zone];
  const i = list.indexOf(id);
  const j = i + delta;
  if (j < 0 || j >= list.length) return l;
  [list[i], list[j]] = [list[j], list[i]];
  return l;
}

/** The layout to draw with, given the tier: Restricted always gets the shipped
 *  bar with nothing in the popup. */
export function activeLayout(decoy) {
  return decoy ? { rail: RAIL_ITEMS.slice(), menu: [] } : loadLayout();
}

/** Put the buttons where `layout` says. Rail buttons keep their place relative
 *  to the pins and custom links (pins.js / links.js insert those before ⚙):
 *  the static ones are re-ordered among themselves, and whatever else is in
 *  #gear-row stays just before ⚙ when ⚙ is on the bar, at the end otherwise.
 *  Menu buttons become menu items (role=menuitem, a label from aria-label in
 *  CSS). Returns how many menu buttons are visible. */
export function applyLayout(layout, { rail, menu, decoy = false } = {}) {
  if (!rail || !menu) return 0;
  const byId = (id) => document.getElementById(id);
  const others = [...rail.children].filter((n) => !RAIL_ITEMS.includes(n.id));
  for (const id of layout.rail) {
    const b = byId(id);
    if (!b) continue;
    b.removeAttribute('role');
    b.removeAttribute('tabindex');
    b.classList.remove('rail-menu-item');
    b.setAttribute('draggable', decoy ? 'false' : 'true');
    rail.append(b);
  }
  const gear = byId('manage-bots');
  for (const n of others) {
    if (gear && gear.parentNode === rail) rail.insertBefore(n, gear); else rail.append(n);
  }
  let shown = 0;
  for (const id of layout.menu) {
    const b = byId(id);
    if (!b) continue;
    b.setAttribute('role', 'menuitem');
    b.setAttribute('tabindex', '-1');
    b.classList.add('rail-menu-item');
    b.setAttribute('draggable', 'true');
    menu.append(b);
    if (!b.classList.contains('hidden') && !b.hidden) shown += 1;
  }
  return shown;
}

// ---------------------------------------------------------------- drag

/** Drag a rail button along the bar, into the popup (drop it on the popup's
 *  rail button, or on a row of the open popup), back out onto the bar, or
 *  along the popup. The bot rail's own drag (main.js renderSidebarInner) is the
 *  model: HTML5 drag events, drop-above / drop-below marks, the move decided
 *  by the pointer's side of the target. Delegated, so the listeners survive
 *  every repaint.
 *
 *  opts: rail (#gear-row), menu (#tools-more), toolList (#tool-list, holds the
 *  popup's button), isDecoy(), openMenu(), onMove(id, zone, beforeId). */
export function wireRailDrag({ rail, menu, toolList, isDecoy, openMenu, onMove }) {
  let dragId = null;
  const item = (e) => {
    const b = e.target && e.target.closest ? e.target.closest('[id]') : null;
    return b && RAIL_ITEMS.includes(b.id) ? b : null;
  };
  const clear = () => {
    for (const host of [rail, menu, toolList]) {
      if (!host) continue;
      host.querySelectorAll('.drop-above, .drop-below, .drop-into, .dragging')
        .forEach((n) => n.classList.remove('drop-above', 'drop-below', 'drop-into', 'dragging'));
    }
  };
  // A row (the phone's bar) compares X, a column (the desktop rail, the popup) Y.
  const after = (host, target, e) => {
    const r = target.getBoundingClientRect();
    const row = host === rail && host.offsetWidth > host.offsetHeight * 1.5;
    const rtl = document.documentElement.dir === 'rtl';
    if (row) return rtl ? e.clientX < r.left + r.width / 2 : e.clientX > r.left + r.width / 2;
    return e.clientY > r.top + r.height / 2;
  };
  const nextManaged = (target) => {
    let n = target.nextElementSibling;
    while (n && !RAIL_ITEMS.includes(n.id)) n = n.nextElementSibling;
    return n ? n.id : null;
  };
  for (const host of [rail, menu]) {
    if (!host) continue;
    const zone = host === rail ? 'rail' : 'menu';
    host.addEventListener('dragstart', (e) => {
      const b = item(e);
      if (!b || isDecoy()) return;
      dragId = b.id;
      b.classList.add('dragging');
      // Our own type, never text/plain: a text payload would be typed into the
      // composer ("profile-chip") if the button were dropped there. Some engines
      // will not start a drag without any data, so the custom type is set.
      try { e.dataTransfer.setData(RAIL_DRAG_TYPE, b.id); e.dataTransfer.effectAllowed = 'move'; } catch { /* old engines */ }
    });
    host.addEventListener('dragend', () => { dragId = null; clear(); });
    host.addEventListener('dragover', (e) => {
      if (!dragId) return;
      e.preventDefault();
      const b = item(e);
      host.querySelectorAll('.drop-above, .drop-below').forEach((n) => n.classList.remove('drop-above', 'drop-below'));
      if (!b || b.id === dragId) return;
      const below = after(host, b, e);
      b.classList.toggle('drop-above', !below);
      b.classList.toggle('drop-below', below);
    });
    host.addEventListener('drop', (e) => {
      if (!dragId) return;
      e.preventDefault();
      const b = item(e);
      const id = dragId;
      dragId = null;
      clear();
      if (b && b.id === id) return;
      const before = b ? (after(host, b, e) ? nextManaged(b) : b.id) : null;
      onMove(id, zone, before);
    });
  }
  if (toolList) {
    const onBtn = (e) => (e.target && e.target.closest ? e.target.closest('#tools-menu-btn') : null);
    toolList.addEventListener('dragenter', (e) => {
      if (!dragId || !onBtn(e)) return;
      e.preventDefault();
      onBtn(e).classList.add('drop-into');
      openMenu();   // so the person can also drop it at a place in the list
    });
    toolList.addEventListener('dragover', (e) => { if (dragId && onBtn(e)) e.preventDefault(); });
    toolList.addEventListener('dragleave', (e) => { const b = onBtn(e); if (b) b.classList.remove('drop-into'); });
    toolList.addEventListener('drop', (e) => {
      if (!dragId || !onBtn(e)) return;
      e.preventDefault();
      const id = dragId;
      dragId = null;
      clear();
      onMove(id, 'menu', null);
    });
  }
  return { dragging: () => dragId };
}

// ---------------------------------------------------------------- the no-drag way

/** "Arrange the bar": the same moves without dragging, for touch screens and
 *  keyboards. Two lists (On the bar / In Tools & more), each row with up, down
 *  and a move-across button, plus Reset. Every press applies at once.
 *  opts: t, el, labelOf(id) -> string, visible(id) -> bool, onChange(layout). */
export function openArrangeDialog({ t, el, labelOf, visible, onChange }) {
  const dlg = el('dialog', { class: 'ui-dialog rail-arrange', 'aria-labelledby': 'rail-arrange-title' });
  const body = el('div', { class: 'ui-dialog-body' });
  const lists = el('div', { class: 'rail-arrange-lists' });
  const done = el('button', { type: 'button', class: 'ui-dialog-btn primary', text: t('common.done') });
  const reset = el('button', { type: 'button', class: 'ui-dialog-btn', text: t('rail.reset') });
  let focusKey = null;
  const paint = () => {
    const layout = loadLayout();
    lists.replaceChildren();
    for (const zone of ['rail', 'menu']) {
      const ids = layout[zone].filter((id) => visible(id));
      const ul = el('ul', { class: 'rail-arrange-list', 'aria-label': t(zone === 'rail' ? 'rail.on_bar' : 'rail.in_menu') });
      ids.forEach((id, i) => {
        const name = labelOf(id);
        const act = (key, label, fn, disabled = false) => {
          const b = el('button', { type: 'button', class: 'btn-secondary rail-arrange-btn', text: label,
            'aria-label': `${label}: ${name}`, dataset: { key: `${id}:${key}` } });
          if (disabled) b.disabled = true;
          b.addEventListener('click', () => { focusKey = `${id}:${key}`; onChange(saveLayout(fn(loadLayout()))); paint(); });
          return b;
        };
        ul.append(el('li', { class: 'rail-arrange-row', dataset: { id } }, [
          el('span', { class: 'rail-arrange-name', text: name }),
          act('up', '↑', (l) => nudgeItem(l, id, -1), i === 0),
          act('down', '↓', (l) => nudgeItem(l, id, +1), i === ids.length - 1),
          act('move', t(zone === 'rail' ? 'rail.to_menu' : 'rail.to_bar'),
            (l) => moveItem(l, id, zone === 'rail' ? 'menu' : 'rail', null)),
        ]));
      });
      if (!ids.length) ul.append(el('li', { class: 'muted rail-arrange-empty', text: t('rail.empty') }));
      lists.append(el('h4', { class: 'rail-arrange-h', text: t(zone === 'rail' ? 'rail.on_bar' : 'rail.in_menu') }), ul);
    }
    if (focusKey) {
      const again = lists.querySelector(`[data-key="${focusKey}"]`);
      const target = again && !again.disabled ? again : lists.querySelector(`[data-key^="${focusKey.split(':')[0]}:"]:not([disabled])`);
      if (target) target.focus();
    }
  };
  body.append(
    el('h3', { class: 'ui-dialog-title', id: 'rail-arrange-title', text: t('rail.arrange_title') }),
    el('p', { class: 'ui-dialog-msg', text: t('rail.arrange_hint') }),
    lists,
    el('div', { class: 'ui-dialog-foot' }, [reset, done]),
  );
  dlg.append(body);
  document.body.append(dlg);
  const close = () => { try { dlg.close(); } catch { /* ignore */ } dlg.remove(); };
  done.addEventListener('click', close);
  reset.addEventListener('click', () => { focusKey = null; onChange(saveLayout(defaultLayout())); paint(); reset.focus(); });
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); });
  dlg.addEventListener('click', (e) => { if (e.target === dlg) close(); });
  paint();
  if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  done.focus();
  return { close, dialog: dlg };
}
