// Settings -> Characters: the household roster (Admins only).
//
// A household character is one every profile may be given. 4.0 replaced the old "who can see
// it" select (audience / safe flags) with two things that live elsewhere or here:
//
//   Which profiles get it   People -> a profile -> Household bots (people-perms.js).
//   Hide from lists         the `hidden` flag, Admin-only vocabulary: a hidden bot stays out
//                           of the bot lists but opens from a link and shows for a profile
//                           that has it ticked.
//   Copy to a profile       an Admin copies the setup (name, picture, personality, voice,
//                           backend; never chats) into a User's or Admin's own bots. Agent
//                           bots can't be copied.
//   Delete                  with the numbers of chats it leaves behind, and "Hide instead".
//
// The rows themselves, the feature switches and the save are main.js's (and bots-edit.js);
// this module builds the Details panel and the delete flow. The server is the authority: it
// refuses a connection for an agent character, an unknown profile, and so on, and its
// sentence is what the person reads.

import { w5 as realApi } from './w5-api.js?v=2';
import { t } from './i18n.js?v=5';
import { el } from './util.js?v=21';
import { askDialog, profileCalls, errorText } from './people-perms.js?v=1';

// ------------------------------------------------------------------ pure

/** Is the character kept off the bot lists? (`hidden` alone: the 3.x `hide_unlocked` /
 *  `audience` keys are inert data since migration 010 and never read here.) */
export function isHidden(bot) {
  if (!bot) return false;
  if (typeof bot.hidden === 'boolean') return bot.hidden;
  return !!(bot.flags && bot.flags.hidden === true);
}

/** Can this character be given a connection? Agent and app characters are answered elsewhere. */
export const takesConnection = (orig) => !!orig && (orig.backend_kind === 'direct' || orig.backend_kind === 'legacy-api');

/** Agent bots run commands on this computer; they can't be copied. */
export const isAgentBot = (bot) => !!bot && (bot.backend_kind === 'agent' || bot.backend_kind === 'app'
  || (bot.backend && bot.backend.kind === 'agent') || bot.backend === 'agent' || bot.backend === 'app');

/** Profiles a household bot can be copied into: those that own bots (not Basic), enabled. */
export const copyTargets = (profiles) =>
  (profiles || []).filter((p) => p && p.role !== 'basic' && !p.disabled);

/** The PATCH body for what changed between a character as the server returned it and the form. */
export function diffCharacter(orig, form) {
  const out = {};
  const be = orig.backend || {};
  if (form.description !== undefined && form.description !== (orig.description ?? '')) out.description = form.description;
  if (form.hidden !== undefined && !!form.hidden !== isHidden(orig)) out.hidden = !!form.hidden;
  if (form.connection !== undefined && (form.connection || '') !== (be.connection || '')) {
    out.connection = form.connection || null;
    if (form.connection && form.model) out.model = form.model;
  } else if (form.model && form.model !== (be.model || '')) {
    out.model = form.model;
  }
  const p = orig.personality || {};
  if (form.personality && form.personality !== (p.preset || '') && form.personality !== 'custom') {
    out.personality = form.personality;
  }
  return out;
}

/** Hash the voice section may read to open on this character's voice. Documented in docs/characters.md. */
export const voiceHash = (id) => `#voice/voices/${encodeURIComponent(id)}`;

// ------------------------------------------------------------------ calls

/** Hide or show a household character. Resolves to the saved row. */
export function setCharacterHidden(id, hidden, api = realApi) {
  return api.patchCharacter(id, { hidden: !!hidden });
}

/**
 * Delete a household character: the numbers first (chats that stay, in how many profiles),
 * then a dialog with "Hide instead" beside the delete. Resolves 'deleted', 'hidden' or null.
 *   ctx: { toast, reload?, api? }
 */
export async function removeCharacter(bot, ctx) {
  const api = ctx.api || realApi;
  let pv;
  try { pv = await profileCalls.characterDeletePreview(bot.id); } catch (e) {
    ctx.toast(errorText(e), true);
    return null;
  }
  const r = await askDialog({
    title: t('chars.remove_title', { name: bot.name }),
    body: t('chars.remove_body', { chats: pv.chats || 0, profiles: pv.profiles || 0 }),
    okText: t('chars.remove_button'),
    altText: isHidden(bot) ? '' : t('chars.remove_hide'),
  });
  if (!r) return null;
  try {
    if (r === 'alt') {
      await setCharacterHidden(bot.id, true, api);
      ctx.toast(t('chars.hidden_saved', { name: bot.name }));
      if (ctx.reload) await ctx.reload();
      return 'hidden';
    }
    await profileCalls.deleteCharacter(bot.id);
    ctx.toast(t('chars.removed', { name: bot.name }));
    if (ctx.reload) await ctx.reload();
    return 'deleted';
  } catch (e) {
    ctx.toast(errorText(e), true);
    return null;
  }
}

// ------------------------------------------------------------------ DOM

function field(label, input, hint) {
  if (!input.id) input.id = `chr-${Math.random().toString(36).slice(2, 8)}`;
  const kids = [el('label', { for: input.id, text: label }), input];
  if (hint) kids.push(el('span', { class: 'people-field-hint', text: hint }));
  return el('div', { class: 'sec-field people-field' }, kids);
}

/** "Copy to a profile": pick one, copy the setup. Never offered for an agent bot. */
function copySection(bot, orig, people, ctx, calls) {
  const box = el('div', { class: 'chr-copy dev-group-body', role: 'group', 'aria-label': t('copybot.to_profile') });
  box.append(el('h4', { class: 'chr-drawer-title', text: t('copybot.to_profile') }));
  if (isAgentBot(orig)) {
    box.append(el('p', { class: 'muted', text: t('copybot.agent_refused') }));
    return box;
  }
  const targets = copyTargets(people);
  if (!targets.length) { box.append(el('p', { class: 'muted', text: t('perms.household_none') })); return box; }
  const pick = el('select', { class: 'ui-select', 'aria-label': t('copybot.pick_profile') },
    targets.map((p) => el('option', { value: p.id, text: p.name })));
  const note = el('p', { class: 'people-field-hint', role: 'note' });
  const syncNote = () => {
    const p = targets.find((x) => x.id === pick.value);
    // An Admin copying into their own profile can see and edit the copy.
    note.textContent = p && p.id !== ctx.meId ? t('copybot.admin_note', { profile: p.name }) : '';
  };
  pick.addEventListener('change', syncNote);
  syncNote();
  const go = el('button', { type: 'button', class: 'btn-secondary', text: t('chars.copy_button') });
  const err = el('p', { class: 'lock-error', role: 'alert', hidden: '' });
  go.addEventListener('click', async () => {
    const p = targets.find((x) => x.id === pick.value);
    if (!p) return;
    go.disabled = true;
    err.hidden = true;
    try {
      const r = await calls.cloneTo(bot.id, p.id);
      ctx.toast(t('copybot.done', { profile: p.name }) + (r && r.voice_cleared ? ` ${t('copybot.voice_cleared')}` : ''));
    } catch (e) {
      err.textContent = e && e.code === 'not_copyable' ? t('copybot.agent_refused') : errorText(e);
      err.hidden = false;
    } finally { go.disabled = false; }
  });
  box.append(el('p', { class: 'people-field-hint', text: t('copybot.what') }),
    el('div', { class: 'cxn-actions' }, [pick, go]), note, err);
  return box;
}

/** Build the details panel for one household character. Resolves after its data has loaded.
 *  `ctx.skip` leaves fields out that the caller draws elsewhere ('personality': the row's
 *  Personality editor). `ctx.calls` replaces the profile calls in tests. */
export async function characterPanel(bot, ctx) {
  const api = ctx.api || realApi;
  const calls = ctx.calls || profileCalls;
  const skip = new Set(ctx.skip || []);
  const panel = el('div', { class: 'chr-panel dev-group', role: 'group', 'aria-label': t('chars.details_aria', { name: bot.name }) });
  panel.append(el('p', { class: 'muted', text: t('chars.loading') }));
  let orig;
  let conns = [];
  let presets = { presets: [] };
  let people = [];
  let meId = '';
  try {
    const [all, cx, pr, pp] = await Promise.all([
      api.allCharacters(), api.connections().catch(() => ({ connections: [] })),
      api.personalities().catch(() => ({ presets: [] })), api.people().catch(() => ({ profiles: [] }))]);
    orig = (all.characters || []).find((c) => c.id === bot.id);
    conns = cx.connections || [];
    presets = pr;
    people = pp.profiles || [];
    meId = typeof pp.me === 'string' ? pp.me : '';
  } catch (e) {
    panel.textContent = errorText(e);
    return panel;
  }
  if (!orig) { panel.textContent = t('chars.gone'); return panel; }
  panel.textContent = '';

  const desc = el('input', { type: 'text', class: 'ui-input', maxlength: '400', value: orig.description || '' });
  const hide = el('input', { type: 'checkbox', role: 'switch', class: 'ui-checkbox', id: `chr-hide-${bot.id}`.replace(/[^a-zA-Z0-9_-]/g, '-') });
  hide.checked = isHidden(orig);

  const form = {};
  if (takesConnection(orig)) {
    const conn = el('select', { class: 'ui-select' }, [
      el('option', { value: '', text: t('chars.no_connection') }),
      ...conns.map((c) => el('option', { value: c.id, text: `${c.name} (${c.type_label})` }))]);
    conn.value = (orig.backend && orig.backend.connection) || '';
    const model = el('input', { type: 'text', class: 'ui-input', list: `chr-models-${bot.id}`, autocomplete: 'off', spellcheck: 'false',
      value: (orig.backend && orig.backend.model) || '' });
    const models = el('datalist', { id: `chr-models-${bot.id}` });
    const fillModels = () => {
      models.textContent = '';
      const c = conns.find((x) => x.id === conn.value);
      (c ? c.models : []).forEach((m) => models.append(el('option', { value: m })));
      if (c && !model.value && c.default_model) model.value = c.default_model;
    };
    conn.addEventListener('change', fillModels);
    fillModels();
    const pers = el('select', { class: 'ui-select' }, [
      ...(presets.presets || []).map((p) => el('option', { value: p.id, text: p.title, title: p.description })),
      el('option', { value: 'custom', text: t('chars.personality_custom') })]);
    pers.value = (orig.personality && orig.personality.preset) || 'default';
    form.conn = conn; form.model = model; form.pers = skip.has('personality') ? null : pers;
    panel.append(
      field(t('chars.connection'), conn, t('chars.connection_hint')),
      field(t('chars.model'), model), models);
    if (!skip.has('personality')) panel.append(field(t('chars.personality'), pers, t('chars.personality_hint')));
    if (!conns.length) {
      const go = el('button', { type: 'button', class: 'btn-secondary', text: t('chars.add_connection') });
      go.addEventListener('click', () => ctx.openTab && ctx.openTab('connections'));
      panel.append(go);
    }
  } else {
    panel.append(el('p', { class: 'muted', text: t('chars.agent_note') }));
  }
  panel.append(field(t('chars.description'), desc),
    el('div', { class: 'dev-group-body chr-hide' }, [
      el('div', { class: 'dev-row chr-feature', dataset: { feature: 'hidden' } }, [
        el('label', { class: 'dev-row-text', for: hide.id }, [
          el('strong', { text: t('chars.hidden') }), el('span', { class: 'dev-row-hint', text: t('chars.hidden_hint') })]),
        hide])]),
    copySection(bot, orig, people, { ...ctx, meId }, calls));

  const voice = el('button', { type: 'button', class: 'btn-secondary chr-voice', text: t('chars.voice_link') });
  voice.addEventListener('click', () => {
    try { window.location.hash = voiceHash(bot.id); } catch { /* ignore */ }
    ctx.openTab && ctx.openTab('voice');
  });
  const save = el('button', { type: 'button', class: 'btn-primary', text: t('chars.save') });
  const close = el('button', { type: 'button', class: 'btn-secondary', text: t('common.close') });
  const err = el('p', { class: 'lock-error', role: 'alert', hidden: '' });
  save.addEventListener('click', async () => {
    const values = { description: desc.value, hidden: hide.checked };
    if (form.conn) {
      values.connection = form.conn.value; values.model = form.model.value.trim();
      if (form.pers) values.personality = form.pers.value;
    }
    const changes = diffCharacter(orig, values);
    if (!Object.keys(changes).length) { err.hidden = true; ctx.toast && ctx.toast(t('chars.nothing')); return; }
    save.disabled = true;
    try {
      await api.patchCharacter(bot.id, changes);
      err.hidden = true;
      ctx.toast && ctx.toast(t('chars.saved'));
      ctx.reload && (await ctx.reload());
    } catch (e) { err.textContent = errorText(e); err.hidden = false; }
    finally { save.disabled = false; }
  });
  close.addEventListener('click', () => (ctx.onClose ? ctx.onClose() : panel.remove()));
  panel.append(err, el('div', { class: 'cxn-actions' }, [save, voice, ctx.noClose ? null : close]));
  return panel;
}
