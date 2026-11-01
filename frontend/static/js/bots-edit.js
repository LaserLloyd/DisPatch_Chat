// Settings -> Characters -> Add: the "Add a character" form (2.3, trimmed in 4.3).
//
// Contract: docs/design/platform-2-3.md section 3 ("Bots").
//   POST   /api/bots        {name, emoji?, backend, agent?}       -> 201 + the row
//
// The rows themselves (rename, picture, personality, remove, the feature switches, order) are
// the Characters page's (js/characters-page.js, js/bot-editor.js). This module only builds the
// Add form and talks to the API, reporting back through `ctx`:
//
//   ctx.api            the API client (default: js/api.js), a stub in the tests
//   ctx.toast(msg, isErr)
//   ctx.onAdded(row)   a POST came back
//   ctx.canCreateAgent false: no Agent option (an agent bot is the Owner's)
//
// Every string is a text node; no markup is ever parsed.

import { api as realApi } from './api.js?v=37';
import { t } from './i18n.js?v=5';
import { el, railIcon, RAIL_ICONS } from './util.js?v=23';

export const NAME_MAX = 40;
export const EMOJI_MAX = 8;
export const AGENT_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

const apiOf = (ctx) => (ctx && ctx.api) || realApi;

function errText(e) {
  return String((e && e.message) || e || '').replace(/^\d{3}:\s*/, '');
}

function field(label, input) {
  return el('label', { class: 'bm-edit-field' }, [el('span', { text: label }), input]);
}

/** The "Add bot" row at the bottom of the list. */
export function addBotRow(ctx) {
  const name = el('input', { type: 'text', maxlength: String(NAME_MAX), autocomplete: 'off', dataset: { bm: 'add-name' } });
  const emoji = el('input', { type: 'text', maxlength: String(EMOJI_MAX), autocomplete: 'off', class: 'bm-edit-emoji', dataset: { bm: 'add-emoji' } });
  // 4.2: an agent bot is the Owner's; the server refuses it for any other Admin, so the option
  // is not offered (`ctx.canCreateAgent === false`; callers that say nothing keep it).
  const backend = el('select', { dataset: { bm: 'add-backend' } }, [
    el('option', { value: 'direct', text: t('bots_edit.backend_direct') }),
    ctx.canCreateAgent === false ? null : el('option', { value: 'agent', text: t('bots_edit.backend_agent') }),
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
