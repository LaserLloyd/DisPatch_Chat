// "Who can use it" for a household template (DisPatch 4.2, API-4.2.md give-to routes).
//
// A template is never chatted with; each profile it is given to gets its own copy. Changing
// who gets one is two calls: a preview (who gets a copy, who keeps theirs as their own bot,
// who loses theirs) and a PUT carrying the preview's single-use token. This module is the pure
// half (state helpers, the dialog text) and the flow; the DOM and the network calls are
// injected, so Settings -> Characters and People -> Household both use the same code.
//
// The server decides everything (copies, detach, delete). This only asks, shows the numbers
// and handles a stale token by previewing again.

import { t } from './i18n.js?v=5';

/** Does `row.give_to` ("everyone" or a list of profile ids) reach profile `pid`? */
export function entitled(row, pid) {
  const g = row && row.give_to;
  if (g === 'everyone') return true;
  return Array.isArray(g) && g.includes(pid);
}

/** The give_to value after flipping one profile. "everyone" minus one profile becomes the
 *  explicit list of the others; a list stays a list (never silently promoted to "everyone"). */
export function toggleProfile(row, pid, on, profiles) {
  const all = (profiles || []).map((p) => p.id);
  const current = row.give_to === 'everyone' ? all : [...(row.give_to || [])];
  const set = new Set(current);
  if (on) set.add(pid); else set.delete(pid);
  if (row.give_to === 'everyone' && on) return 'everyone';
  return [...set];
}

/** The "Everyone" chip: on -> "everyone"; off -> every current profile spelled out (no copy
 *  changes), so the next tap on a profile chip means something. */
export function toggleEveryone(row, on, profiles) {
  return on ? 'everyone' : (profiles || []).map((p) => p.id);
}

/** Same set? (order-insensitive; "everyone" only equals "everyone") */
export function sameGiveTo(a, b) {
  if (a === 'everyone' || b === 'everyone') return a === b;
  const x = [...(a || [])].sort();
  const y = [...(b || [])].sort();
  return x.length === y.length && x.every((v, i) => v === y[i]);
}

const nameOf = (profiles, id) => ((profiles || []).find((p) => p.id === id) || {}).name || id;
const names = (profiles, ids) => (ids || []).map((id) => nameOf(profiles, id)).join(', ');

/** Does this change turn "everyone" into an explicit list? The server says so with
 *  `everyone_to_list`; when a server does not, the page's own comparison stands in. From then
 *  on, profiles made later get no copy: the one thing the tally cannot show. */
export function leavesEveryone(row, pv, next) {
  if (pv && pv.everyone_to_list === true) return true;
  return !!row && row.give_to === 'everyone' && next !== undefined && next !== 'everyone';
}

/** Dialog fields for a preview: tally numbers plus one sentence per non-empty group.
 *  `ending` (see leavesEveryone) puts "new profiles will no longer get it" first. */
export function previewDialog(row, pv, profiles, ending = false) {
  const add = pv.add || [];
  const detach = pv.detach || [];
  const remove = pv.remove || [];
  const notes = [];
  if (ending) notes.push(t('cpage.change_everyone_ends', { name: row.name }));
  if (add.length) notes.push(t('cpage.change_adds', { names: names(profiles, add) }));
  if (detach.length) notes.push(t('cpage.change_keeps', { names: names(profiles, detach) }));
  if (remove.length) notes.push(t('cpage.change_loses', { names: names(profiles, remove) }));
  return {
    title: t('cpage.change_title', { name: row.name }),
    body: t('cpage.change_body'),
    tally: [
      { n: add.length, label: t('cpage.change_tally_add') },
      { n: detach.length, label: t('cpage.change_tally_keep') },
      { n: remove.length, label: t('cpage.change_tally_lose') },
    ],
    notes,
    okText: t('cpage.change_apply'),
    danger: remove.length > 0,
  };
}

/** The calls, over any `call(method, url, body)` that throws an Error carrying `.code`. */
export function giveToCalls(call) {
  const enc = encodeURIComponent;
  return {
    preview: (id, giveTo) => call('GET', `/api/characters/${enc(id)}/give-to/preview?give_to=${enc(JSON.stringify(giveTo))}`),
    put: (id, giveTo, token) => call('PUT', `/api/characters/${enc(id)}/give-to`, { give_to: giveTo, token }),
  };
}

/**
 * Preview -> confirm -> PUT. Resolves to the template's fresh row, or null when the person
 * cancels or nothing would change. A `give_to_stale` answer previews again (and asks again,
 * because the numbers it just showed are out of date), at most twice.
 *   ctx: { row, next, profiles, calls, ask(dialog) -> 'ok'|'alt'|null }
 */
export async function changeGiveTo({ row, next, profiles, calls, ask }) {
  if (sameGiveTo(row.give_to, next)) return null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const pv = await calls.preview(row.id, next);
    const ending = leavesEveryone(row, pv, next);
    const quiet = !ending && !(pv.add || []).length && !(pv.detach || []).length && !(pv.remove || []).length;
    if (!quiet) {
      const r = await ask(previewDialog(row, pv, profiles, ending));
      if (r !== 'ok') return null;
    }
    try {
      return await calls.put(row.id, next, pv.token);
    } catch (e) {
      if (e && e.code === 'give_to_stale' && attempt < 2) continue;
      throw e;
    }
  }
  return null;
}
