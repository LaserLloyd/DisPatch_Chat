// Settings -> Bots -> Personality: the system-prompt editor for bots whose
// personality DisPatch owns.
//
//   GET   /api/bots/personas  -> {presets: [{id, title, description, text}], default_id, max_chars}
//   PATCH /api/bots/{id}      {persona: "..."}  ("" = back to the shipped default) -> the bot row
//
// The row carries `persona_kind` (direct | legacy-api | none), `persona` (the
// text the bot runs with) and `persona_custom`. A direct-API bot's text is
// `advisor.persona`, a legacy-API bot's `api.system_prompt`: the same field the
// AI models Connect panel writes, so the two never disagree. The text is only
// the first block of the prompt; the date, the rules (locked rules on a
// Safe-Mode device), the lookups and the profile are appended by the server
// and are not shown or editable here. An agent bot gets a short note: its
// personality lives in its OpenClaw workspace.
//
// Like the rest of js/bots-edit.js, a Save is a server fact (PATCH at once),
// not part of the Bots tab's Save-button draft. Every string is a text node.

import { t } from './i18n.js?v=4';
import { el, railIcon, RAIL_ICONS } from './util.js?v=20';

export const MAX_CHARS = 8000;
export const CUSTOM = 'custom';

/** Bot ids whose panel is open: the Bots tab is rebuilt after every PATCH,
 *  and a Save must not snap the editor shut under the operator's hands. */
const openFor = new Set();

let presetsPromise = null;

async function fetchPresets() {
  const r = await fetch('/api/bots/personas', {
    credentials: 'same-origin', headers: { Accept: 'application/json' },
  });
  if (!r.ok) {
    const err = new Error(`${r.status}`);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

/** The preset table, fetched once per page (a failure is retried next time). */
export function loadPresets(ctx) {
  const load = (ctx && ctx.loadPresets) || fetchPresets;
  if (!presetsPromise) {
    presetsPromise = Promise.resolve().then(load).catch((e) => { presetsPromise = null; throw e; });
  }
  return presetsPromise;
}

/** Test hook: forget the cached presets and open panels. */
export function _reset() { presetsPromise = null; openFor.clear(); }

export function kindOf(bot) {
  const k = bot && bot.persona_kind;
  if (k === 'direct' || k === 'legacy-api' || k === 'none') return k;
  // An older server's row: no editor rather than a broken one.
  return 'none';
}

/** The preset whose text equals `text` (after trimming), or CUSTOM. */
export function matchPreset(presets, text) {
  const want = String(text || '').replace(/\r\n/g, '\n').trim();
  const hit = (presets || []).find((p) => String(p.text || '').trim() === want);
  return hit ? hit.id : CUSTOM;
}

function errText(e) {
  return String((e && e.message) || e || '').replace(/^\d{3}:\s*/, '');
}

/** The Personality button for the row. Null for bots with nothing to show
 *  (app bots: their character ships with the app). */
export function personaButton(bot, toggle) {
  if (bot && bot.backend === 'app') return null;
  const b = el('button', {
    type: 'button', class: 'bm-avatar-btn bm-avatar-btn--icon bm-persona-btn',
    title: t('persona.button_title'), 'aria-label': t('persona.button_aria', { name: bot.name }),
    dataset: { bm: 'persona' },
  }, [railIcon(RAIL_ICONS.sparkle)]);
  b.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
  return b;
}

/** The panel under the row. Returns { panel, toggle }; panel is null when
 *  the bot gets no button at all. */
export function personaPanel(bot, ctx) {
  if (bot && bot.backend === 'app') return { panel: null, toggle: () => {} };
  const kind = kindOf(bot);
  const panel = el('div', { class: 'bm-persona hidden', dataset: { bm: 'persona-panel', kind } });
  const toggle = (open) => {
    const show = open === undefined ? panel.classList.contains('hidden') : open;
    panel.classList.toggle('hidden', !show);
    if (show) openFor.add(bot.id); else openFor.delete(bot.id);
    if (show && !panel.dataset.built) build();
  };

  function build() {
    panel.dataset.built = '1';
    const head = el('div', { class: 'bm-persona-head' }, [
      el('strong', { text: t('persona.title') }),
    ]);
    if (kind === 'none') {
      panel.append(head, el('p', { class: 'muted bm-persona-note', dataset: { bm: 'persona-agent-note' }, text: t('persona.agent_note') }));
      return;
    }
    const saved = { text: String(bot.persona || '') };
    const select = el('select', { dataset: { bm: 'persona-preset' }, 'aria-label': t('persona.preset') });
    const desc = el('p', { class: 'muted bm-persona-desc', dataset: { bm: 'persona-desc' } });
    const area = el('textarea', {
      rows: '9', spellcheck: 'true', maxlength: String(MAX_CHARS),
      class: 'bm-persona-text', dataset: { bm: 'persona-text' }, 'aria-label': t('persona.editor'),
      placeholder: kind === 'legacy-api' ? t('persona.legacy_placeholder') : '',
    });
    area.value = saved.text;
    const count = el('span', { class: 'bm-persona-count', dataset: { bm: 'persona-count' }, 'aria-live': 'polite' });
    const save = el('button', { type: 'button', class: 'btn-primary', text: t('persona.save'), dataset: { bm: 'persona-save' } });
    const revert = el('button', { type: 'button', class: 'btn-secondary', text: t('persona.revert'), dataset: { bm: 'persona-revert' } });
    const reset = el('button', { type: 'button', class: 'btn-secondary', text: t('persona.reset'), dataset: { bm: 'persona-reset' } });
    const close = el('button', { type: 'button', class: 'btn-secondary', text: t('common.close'), dataset: { bm: 'persona-close' } });
    const notes = [];
    if (bot.safe) {
      notes.push(el('p', { class: 'bm-persona-safe', dataset: { bm: 'persona-safe-note' }, text: t('persona.safe_note') }));
    }
    notes.push(el('p', { class: 'muted bm-persona-hint', text: t(kind === 'legacy-api' ? 'persona.hint_legacy' : 'persona.hint') }));
    panel.append(
      head,
      el('label', { class: 'bm-edit-field bm-persona-pick' }, [el('span', { text: t('persona.preset') }), select]),
      desc,
      ...notes,
      area,
      el('div', { class: 'bm-persona-foot' }, [count, el('div', { class: 'bm-edit-actions' }, [reset, revert, save, close])]),
    );

    let presets = [];
    let lastPreset = null;
    const byId = (id) => presets.find((p) => p.id === id);

    const paintSelect = () => {
      select.replaceChildren(
        ...presets.map((p) => el('option', { value: p.id, text: p.title })),
        el('option', { value: CUSTOM, text: t('persona.custom') }),
      );
    };
    const sync = () => {
      const id = matchPreset(presets, area.value);
      select.value = id;
      if (id !== CUSTOM) lastPreset = id;
      const p = byId(id);
      desc.textContent = p ? p.description : t('persona.custom_desc');
      const n = area.value.length;
      count.textContent = t('persona.count', { n: String(n), max: String(MAX_CHARS) });
      count.classList.toggle('over', n > MAX_CHARS);
      const dirty = area.value.trim() !== saved.text.trim();
      save.disabled = !dirty || n > MAX_CHARS;
      revert.disabled = !dirty;
      const target = byId(lastPreset || 'default');
      reset.disabled = !target || area.value.trim() === String(target.text).trim();
    };

    select.addEventListener('change', () => {
      const p = byId(select.value);
      if (p) { area.value = p.text; lastPreset = p.id; }
      sync();
    });
    area.addEventListener('input', sync);
    area.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); toggle(false); }
      else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (!save.disabled) save.click(); }
    });
    revert.addEventListener('click', () => { area.value = saved.text; sync(); });
    reset.addEventListener('click', () => {
      const p = byId(lastPreset || 'default');
      if (p) { area.value = p.text; sync(); }
    });
    close.addEventListener('click', () => toggle(false));
    save.addEventListener('click', async () => {
      const text = area.value.trim();
      save.disabled = true;
      try {
        const row = await ctx.api.patchBot(bot.id, { persona: text });
        saved.text = String((row && row.persona) ?? text);
        bot.persona = saved.text;
        if (row && row.persona_custom !== undefined) bot.persona_custom = row.persona_custom;
        area.value = saved.text;
        ctx.toast(t('persona.saved', { name: bot.name }));
        if (ctx.onPatched) ctx.onPatched(bot.id, row || { persona: saved.text });
      } catch (err) {
        ctx.toast(t('bots_edit.save_failed', { error: errText(err) }), true);
      } finally {
        sync();
      }
    });

    select.disabled = true;
    sync();
    loadPresets(ctx).then((r) => {
      presets = (r && r.presets) || [];
      paintSelect();
      select.disabled = false;
      sync();
    }).catch(() => {
      desc.textContent = t('persona.presets_failed');
    });
  }

  if (openFor.has(bot.id)) toggle(true);
  return { panel, toggle };
}
