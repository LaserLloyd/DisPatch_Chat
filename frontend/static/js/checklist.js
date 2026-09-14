// Interactive ```checklist tables: a checkbox column, a sort that keeps
// completed rows pinned to the bottom in check order, and server persistence.
//
// markdown.js renders a ```checklist fence as a `.checklist-widget` containing
// a plain <table> — the SAME table a normal markdown table produces. This
// module injects the leading checkbox column and owns the interaction, because
// it needs the message id and the API, context the renderer does not have.
//
// Model text cannot smuggle a checkbox in. NOT because of DOMPurify -- its
// allow-list permits <input> and `checked`/`disabled`/`type` for GFM task
// lists -- but because markdown.js's html() renderer override ESCAPES raw
// HTML inside the fence before it is ever parsed. That override is the
// load-bearing control here; do not remove it thinking the sanitizer covers
// this. State is stored on the message's
// metadata — {"checklist": {"checked": [rowIndicesInCheckOrder]}} — where the
// array order IS the check order, so the array alone reconstructs both the
// checkbox state and the "completed rows grouped at the bottom" ordering after
// a reload or on another device.
//
// Sort is per-paint (like markdown.js's generic table sort): the persisted
// state is checkbox state + check order, exactly what the feature promises to
// survive a reload.
import { api } from './api.js?v=22';
import { t } from './i18n.js?v=3';
import { cellSortValue, compareCells } from './markdown.js?v=28';

// The checkbox column is always column 0; data columns start at 1.
const CHECK_COL = 0;

/** Pure display order for a checklist table's data rows.
 *
 *  `rows` is [{index, text(col)}] — index is the authored (0-based) row index,
 *  text(col) returns the cell text for a data column. `checked` is the check
 *  order (list of authored indices). Completed rows are always last, in check
 *  order; incomplete rows follow `sort` ({col, dir}) or authored order.
 *  Exported so the node test suite can pin it without a DOM. */
export function checklistOrder(rows, checked, sort) {
  const checkedSet = new Set(checked);
  const incomplete = rows.filter((r) => !checkedSet.has(r.index));
  const completed = rows.filter((r) => checkedSet.has(r.index))
    .sort((a, b) => checked.indexOf(a.index) - checked.indexOf(b.index));
  if (sort && sort.dir) {
    const sign = sort.dir === 'descending' ? -1 : 1;
    incomplete.sort((a, b) => {
      const av = cellSortValue(a.text(sort.col));
      const bv = cellSortValue(b.text(sort.col));
      const blank = (!av.str ? 1 : 0) - (!bv.str ? 1 : 0);
      if (blank) return blank;
      return sign * compareCells(av, bv) || a.index - b.index;
    });
  } else {
    incomplete.sort((a, b) => a.index - b.index);
  }
  return [...incomplete, ...completed].map((r) => r.index);
}

function rowsOf(table) {
  const tbody = table.tBodies[0];
  return tbody ? Array.from(tbody.rows) : [];
}

function currentSort(table) {
  const dir = table.dataset.sortDir;
  if (!dir) return null;
  const col = Number(table.dataset.sortCol);
  return Number.isNaN(col) ? null : { col, dir };
}

function reorder(table, checked) {
  const tbody = table.tBodies[0];
  if (!tbody) return;
  const rows = rowsOf(table);
  const order = checklistOrder(
    rows.map((r) => ({ index: Number(r.dataset.row), text: (c) => r.cells[c]?.textContent ?? '' })),
    checked, currentSort(table));
  order.forEach((idx) => {
    const r = rows.find((rr) => Number(rr.dataset.row) === idx);
    if (r) tbody.appendChild(r);
  });
}

function paint(table, checked) {
  const checkedSet = new Set(checked);
  for (const r of rowsOf(table)) {
    const done = checkedSet.has(Number(r.dataset.row));
    const cb = r.querySelector('input.cl-checkbox');
    if (cb) cb.checked = done;
    r.classList.toggle('cl-done', done);
  }
}

function checkedOf(table) {
  const out = [];
  for (const r of rowsOf(table)) {
    if (r.querySelector('input.cl-checkbox')?.checked) out.push(Number(r.dataset.row));
  }
  return out;
}

function sortChecklist(table, col, dir) {
  table.dataset.sortCol = String(col);
  table.dataset.sortDir = dir || '';
  Array.from(table.tHead?.rows[0]?.cells || []).forEach((th, i) => {
    if (dir && i === col) th.setAttribute('aria-sort', dir);
    else th.removeAttribute('aria-sort');
  });
  reorder(table, checkedOf(table));
}

function installWidget(widget, { checked, readonly, messageId, listIndex = 0 }) {
  const table = widget.querySelector('table');
  if (!table || table.dataset.checklistInstalled === '1') return;
  table.dataset.checklistInstalled = '1';
  widget.dataset.listIndex = String(listIndex);
  table.classList.add('md-table', 'cl-table');
  if (!widget.dataset.messageId) {
    widget.dataset.messageId = messageId
      || widget.closest('.msg[data-id]')?.dataset.id
      || '';
  }

  // Header: a leading checkbox column, then sortable data columns.
  const headRow = table.tHead?.rows[0];
  if (headRow) {
    const th = document.createElement('th');
    th.className = 'cl-check';
    th.setAttribute('scope', 'col');
    th.setAttribute('aria-label', t ? t('checklist.done') : 'Completed');
    th.textContent = '✓';
    headRow.insertBefore(th, headRow.cells[0]);
    Array.from(headRow.cells).forEach((cell, i) => {
      if (i === CHECK_COL) return;
      cell.classList.add('md-th');
      cell.tabIndex = 0;
      cell.setAttribute('role', 'columnheader');
      const sortNext = () => {
        const cur = cell.getAttribute('aria-sort');
        sortChecklist(table, i, cur === 'ascending' ? 'descending'
          : cur === 'descending' ? null : 'ascending');
      };
      cell.addEventListener('click', sortNext);
      cell.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sortNext(); }
      });
    });
  }

  // Body: a leading checkbox cell per row, stamped with its authored index.
  rowsOf(table).forEach((row, i) => {
    row.dataset.row = String(i);
    const td = document.createElement('td');
    td.className = 'cl-check';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'cl-checkbox';
    const rowLabel = (row.cells[0]?.textContent || '').trim().slice(0, 60);
    const complete = t ? t('checklist.complete') : 'Complete';
    cb.setAttribute('aria-label', rowLabel ? `${complete}: ${rowLabel}` : complete);
    if (readonly) cb.disabled = true;
    td.appendChild(cb);
    row.insertBefore(td, row.cells[0]);
  });

  if (!readonly) {
    table.addEventListener('change', (e) => {
      const cb = e.target.closest?.('input.cl-checkbox');
      if (!cb || cb.disabled) return;
      const row = cb.closest('tr');
      const idx = Number(row?.dataset.row);
      if (Number.isNaN(idx)) return;
      const mid = widget.dataset.messageId;
      if (!mid) return;   // not a chat message (e.g. harness output) — nothing to persist
      // `change` fires AFTER the browser has flipped cb.checked, so reading the
      // DOM here gives the NEW state. Deriving `prev` from it made the revert a
      // no-op: a rejected PATCH repainted the state it was meant to undo, and
      // the row stayed checked and pinned to the bottom for good. Reconstruct
      // the previous state by inverting this one row.
      const now = checkedOf(table);
      const prev = cb.checked ? now.filter((x) => x !== idx) : [...now, idx];
      const next = cb.checked
        ? [...prev.filter((x) => x !== idx), idx]   // just-checked → end of the completed group
        : prev.filter((x) => x !== idx);
      // Optimistic: paint + reorder now, revert if the server refuses.
      paint(table, next);
      reorder(table, next);
      // A ROW op, not the whole array. Sending the full set meant two devices
      // ticking different rows raced -- the later write was built from a
      // snapshot taken before the earlier one landed and silently dropped it.
      // The server merges; this request cannot carry a stale view of rows it
      // does not mention.
      api.updateChecklist(mid, { index: idx, checked: cb.checked, list: listIndex })
        .catch(() => {
          paint(table, prev);
          reorder(table, prev);
        });
    });
  }

  paint(table, checked);
  reorder(table, checked);
}

/** Install checkboxes + interaction on every ```checklist table under `root`.
 *  Called once per rendered message bubble, with the message for its id and
 *  persisted state. `readonly` (Safe Mode) renders the checkboxes disabled. */
export function installChecklists(root, { message, readonly = false } = {}) {
  if (!root) return;
  const stored = message?.metadata?.checklist || {};
  root.querySelectorAll('.checklist-widget').forEach((w, i) => {
    // Per-widget state. Every widget used to be handed the SAME array and the
    // same message id, so a message with a warm-up list and a main-set list
    // shared one index space: checking row 0 of the second wiped row 0 of the
    // first. List 0 lives in `checked` (which is what was already being
    // stored); the rest live under `lists`.
    installWidget(w, {
      checked: checkedForList(stored, i),
      readonly,
      messageId: message?.id,
      listIndex: i,
    });
  });
}

/** The persisted rows for one widget. List 0 reads the legacy `checked` key so
 *  state stored before this existed still loads. */
function checkedForList(stored, i) {
  const raw = i === 0 ? stored?.checked : stored?.lists?.[String(i)];
  return Array.isArray(raw) ? raw : [];
}

/** Re-apply persisted state to already-installed widgets (a checklist_update
 *  WS frame, i.e. another device checked a row). */
export function applyChecklistState(root, checklist) {
  if (!root) return;
  root.querySelectorAll('.checklist-widget').forEach((w, i) => {
    const table = w.querySelector('table');
    if (!table || table.dataset.checklistInstalled !== '1') return;
    // Same per-widget split as install: a broadcast carries the whole stored
    // shape, and each widget takes only its own list out of it.
    const checked = checkedForList(checklist || {}, i);
    paint(table, checked);
    reorder(table, checked);
  });
}
