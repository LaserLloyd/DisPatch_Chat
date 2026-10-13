// Settings -> Bots: the per-row editing controls added in 2.3.
//
// Contract: docs/design/platform-2-3.md section 3 ("Bots").
//   PATCH  /api/bots/{id}   {name, emoji, model_hint, tools, ...} -> the bot row
//   POST   /api/bots        {name, emoji?, backend, agent?}       -> 201 + the row
//   DELETE /api/bots/{id}                                        -> {removed}; 409 for an app bot
//
// Unlike the Safe / Reactions / visible toggles, which wait for the Save
// button, these act at once (a rename, a new bot and a removal are server
// facts, not draft state). main.js owns the roster (`bmBots`) and the sidebar;
// this module only builds controls and talks to the API, reporting back
// through `ctx`:
//
//   ctx.api            the API client (default: js/api.js), a stub in the tests
//   ctx.toast(msg, isErr)
//   ctx.confirm(msg, { okText, danger }) -> Promise<boolean>   the shell dialog
//   ctx.openAi()       open Settings -> AI models
//   ctx.onPatched(id, row)   a PATCH came back
//   ctx.onRemoved(id)        a DELETE succeeded
//   ctx.onAdded(row)         a POST came back
//
// Every string is a text node; no markup is ever parsed.

import { api as realApi } from './api.js?v=33';
import { t } from './i18n.js?v=4';
import { el, railIcon, RAIL_ICONS } from './util.js?v=20';

export const NAME_MAX = 40;
export const EMOJI_MAX = 8;
export const HINT_MAX = 60;
export const AGENT_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

const apiOf = (ctx) => (ctx && ctx.api) || realApi;

function errText(e) {
  return String((e && e.message) || e || '').replace(/^\d{3}:\s*/, '');
}

/** direct | legacy-api | agent | app. A row from an older server has no
 *  `backend`: treat it as an agent bot, the only kind that existed. */
export function backendKind(bot) {
  const b = bot && bot.backend;
  return b === 'direct' || b === 'legacy-api' || b === 'app' ? b : 'agent';
}

const badgeLabels = () => ({
  direct: t('bots_edit.badge_direct'),
  'legacy-api': t('bots_edit.badge_legacy'),
  agent: t('bots_edit.badge_agent'),
  app: t('bots_edit.badge_app'),
});

export function backendBadge(bot) {
  const kind = backendKind(bot);
  return el('span', {
    class: `bm-backend bm-backend-${kind}`, dataset: { backend: kind },
    title: t('bots_edit.badge_title'), text: badgeLabels()[kind],
  });
}

/** "Not connected - open AI models": only for a direct-API bot with no provider. */
export function notConnectedLink(bot, ctx) {
  if (backendKind(bot) !== 'direct' || bot.connected !== false) return null;
  const a = el('button', {
    type: 'button', class: 'bm-notconnected', text: t('bots_edit.not_connected'),
    dataset: { bm: 'not-connected' },
  });
  a.addEventListener('click', (e) => { e.stopPropagation(); if (ctx.openAi) ctx.openAi(); });
  return a;
}

/** The Lookups pill (direct-API bots only): PATCH {tools}. */
export function lookupsPill(bot, ctx) {
  if (backendKind(bot) !== 'direct') return null;
  const on = bot.tools !== false;
  const btn = el('button', {
    type: 'button', class: 'bm-safe-btn' + (on ? ' on' : ''), text: t('bots_edit.lookups'),
    title: t('bots_edit.lookups_title'), 'aria-pressed': String(on), dataset: { bm: 'lookups' },
  });
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    const want = btn.getAttribute('aria-pressed') !== 'true';
    btn.disabled = true;
    try {
      const row = await apiOf(ctx).patchBot(bot.id, { tools: want });
      btn.classList.toggle('on', want);
      btn.setAttribute('aria-pressed', String(want));
      if (ctx.onPatched) ctx.onPatched(bot.id, row || { tools: want });
    } catch (err) {
      ctx.toast(t('bots_edit.save_failed', { error: errText(err) }), true);
    } finally {
      btn.disabled = false;
    }
  });
  return btn;
}

function field(label, input) {
  return el('label', { class: 'bm-edit-field' }, [el('span', { text: label }), input]);
}

/** The inline editor under a row: name, emoji, model hint. Hidden until the
 *  pencil is pressed. Returns { panel, toggle }. */
export function editPanel(bot, ctx) {
  const name = el('input', { type: 'text', maxlength: String(NAME_MAX), value: bot.name || '', autocomplete: 'off', dataset: { bm: 'name' } });
  const emoji = el('input', { type: 'text', maxlength: String(EMOJI_MAX), value: bot.emoji || '', autocomplete: 'off', class: 'bm-edit-emoji', dataset: { bm: 'emoji' } });
  const hint = el('input', {
    type: 'text', maxlength: String(HINT_MAX), value: bot.model_hint || '', autocomplete: 'off',
    placeholder: t('bots_edit.model_hint_ph'), dataset: { bm: 'model_hint' },
  });
  const save = el('button', { type: 'button', class: 'btn-primary', text: t('common.save'), dataset: { bm: 'edit-save' } });
  const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('common.cancel') });
  const panel = el('div', { class: 'bm-edit-panel hidden', dataset: { bm: 'edit-panel' } }, [
    field(t('bots_edit.name'), name),
    field(t('bots_edit.emoji'), emoji),
    field(t('bots_edit.model_hint'), hint),
    el('div', { class: 'bm-edit-actions' }, [save, cancel]),
  ]);
  const reset = () => { name.value = bot.name || ''; emoji.value = bot.emoji || ''; hint.value = bot.model_hint || ''; };
  const toggle = (open) => {
    const show = open === undefined ? panel.classList.contains('hidden') : open;
    if (show) reset();
    panel.classList.toggle('hidden', !show);
    if (show) name.focus();
  };
  cancel.addEventListener('click', () => toggle(false));
  panel.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); toggle(false); }
    else if (e.key === 'Enter' && e.target instanceof HTMLInputElement) { e.preventDefault(); save.click(); }
  });
  save.addEventListener('click', async () => {
    const patch = {};
    const n = name.value.trim();
    if (!n) { ctx.toast(t('bots_edit.name_required'), true); name.focus(); return; }
    if (n !== (bot.name || '')) patch.name = n;
    if (emoji.value.trim() !== (bot.emoji || '')) patch.emoji = emoji.value.trim();
    if (hint.value.trim() !== (bot.model_hint || '')) patch.model_hint = hint.value.trim();
    if (!Object.keys(patch).length) { toggle(false); return; }
    save.disabled = true;
    try {
      const row = await apiOf(ctx).patchBot(bot.id, patch);
      ctx.toast(t('bots_edit.saved'));
      if (ctx.onPatched) ctx.onPatched(bot.id, row || patch);
    } catch (err) {
      ctx.toast(t('bots_edit.save_failed', { error: errText(err) }), true);
    } finally {
      save.disabled = false;
    }
  });
  return { panel, toggle };
}

export function editButton(bot, toggle) {
  const b = el('button', {
    type: 'button', class: 'bm-avatar-btn bm-avatar-btn--icon', title: t('bots_edit.edit_title'),
    'aria-label': t('bots_edit.edit_aria', { name: bot.name }), dataset: { bm: 'edit' },
  }, [railIcon(RAIL_ICONS.rename)]);
  b.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
  return b;
}

/** Remove: confirm, DELETE, report. The server keeps the threads; an app-owned
 *  bot answers 409 and says so. */
export function removeButton(bot, ctx) {
  const b = el('button', {
    type: 'button', class: 'bm-avatar-btn bm-avatar-btn--icon bm-remove', title: t('bots_edit.remove'),
    'aria-label': t('bots_edit.remove_aria', { name: bot.name }), dataset: { bm: 'remove' },
  }, [railIcon(RAIL_ICONS.trash)]);
  b.addEventListener('click', async (e) => {
    e.stopPropagation();
    const ok = await ctx.confirm(t('bots_edit.remove_confirm', { name: bot.name }),
      { okText: t('bots_edit.remove'), danger: true });
    if (!ok) return;
    b.disabled = true;
    try {
      await apiOf(ctx).deleteBot(bot.id);
      ctx.toast(t('bots_edit.removed', { name: bot.name }));
      if (ctx.onRemoved) ctx.onRemoved(bot.id);
    } catch (err) {
      ctx.toast(err && err.status === 409
        ? t('bots_edit.remove_blocked', { name: bot.name })
        : t('bots_edit.remove_failed', { error: errText(err) }), true);
      b.disabled = false;
    }
  });
  return b;
}

/** Everything a row gains, as separate slots so main.js can place them. */
export function rowControls(bot, ctx) {
  const ed = editPanel(bot, ctx);
  return {
    badge: backendBadge(bot),
    notConnected: notConnectedLink(bot, ctx),
    lookups: lookupsPill(bot, ctx),
    edit: editButton(bot, ed.toggle),
    remove: removeButton(bot, ctx),
    panel: ed.panel,
  };
}

/** The "Add bot" row at the bottom of the list. */
export function addBotRow(ctx) {
  const name = el('input', { type: 'text', maxlength: String(NAME_MAX), autocomplete: 'off', dataset: { bm: 'add-name' } });
  const emoji = el('input', { type: 'text', maxlength: String(EMOJI_MAX), autocomplete: 'off', class: 'bm-edit-emoji', dataset: { bm: 'add-emoji' } });
  const backend = el('select', { dataset: { bm: 'add-backend' } }, [
    el('option', { value: 'direct', text: t('bots_edit.backend_direct') }),
    el('option', { value: 'agent', text: t('bots_edit.backend_agent') }),
  ]);
  const agent = el('input', {
    type: 'text', maxlength: '64', autocomplete: 'off', spellcheck: 'false',
    placeholder: t('bots_edit.agent_ph'), dataset: { bm: 'add-agent' },
  });
  const agentField = field(t('bots_edit.agent_id'), agent);
  agentField.classList.add('hidden');
  const hint = el('p', { class: 'muted bm-add-hint', text: t('bots_edit.backend_direct_hint') });
  backend.addEventListener('change', () => {
    const isAgent = backend.value === 'agent';
    agentField.classList.toggle('hidden', !isAgent);
    hint.textContent = t(isAgent ? 'bots_edit.backend_agent_hint' : 'bots_edit.backend_direct_hint');
  });
  const add = el('button', { type: 'button', class: 'btn-primary', text: t('bots_edit.add_confirm'), dataset: { bm: 'add-confirm' } });
  const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('common.cancel') });
  const form = el('div', { class: 'bm-add-form hidden', dataset: { bm: 'add-form' } }, [
    field(t('bots_edit.name'), name),
    field(t('bots_edit.emoji'), emoji),
    field(t('bots_edit.backend'), backend),
    agentField,
    hint,
    el('div', { class: 'bm-edit-actions' }, [add, cancel]),
  ]);
  const open = el('button', {
    type: 'button', class: 'btn-secondary bm-add-open', dataset: { bm: 'add-open' },
  }, [railIcon(RAIL_ICONS.plus), el('span', { text: t('bots_edit.add_title') })]);
  const show = (on) => {
    form.classList.toggle('hidden', !on);
    open.classList.toggle('hidden', on);
    if (on) name.focus();
  };
  open.addEventListener('click', () => show(true));
  cancel.addEventListener('click', () => show(false));
  form.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); show(false); }
    else if (e.key === 'Enter' && e.target instanceof HTMLInputElement) { e.preventDefault(); add.click(); }
  });
  add.addEventListener('click', async () => {
    const n = name.value.trim();
    if (!n) { ctx.toast(t('bots_edit.name_required'), true); name.focus(); return; }
    const body = { name: n, backend: backend.value };
    if (emoji.value.trim()) body.emoji = emoji.value.trim();
    if (backend.value === 'agent') {
      const a = agent.value.trim();
      if (!AGENT_ID_RE.test(a)) { ctx.toast(t('bots_edit.agent_required'), true); agent.focus(); return; }
      body.agent = a;
    }
    add.disabled = true;
    try {
      const row = await apiOf(ctx).addBot(body);
      ctx.toast(t('bots_edit.added', { name: (row && row.name) || n }));
      if (ctx.onAdded) ctx.onAdded(row);
    } catch (err) {
      ctx.toast(t('bots_edit.add_failed', { error: errText(err) }), true);
    } finally {
      add.disabled = false;
    }
  });
  return el('div', { class: 'bm-add', dataset: { bm: 'add' } }, [open, form]);
}
