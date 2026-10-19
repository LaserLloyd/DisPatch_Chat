// Settings -> Characters: what the 3.0 character model adds to each row.
//
//   Who can see it  one select per row (3.2) for the two server flags: Everyone,
//                   Members and up, Restricted profiles only, Hidden. Lossless:
//                   whoCanSee / flagsForWho are a 4-row truth table over
//                   (safe, hide_unlocked), the same one config.Bot.audience reads.
//   Details panel   per character: its connection and model, a one-line description,
//                   which profiles may see it, and a link to Voice -> Voices for this
//                   character (the voice add-on owns that section; this only opens it).
//
// The rows themselves, the feature switches and the save are main.js's (and
// bots-edit.js); this module builds the panel. The server is the authority: it
// refuses a connection for an agent character, an unknown profile, and so on, and
// its sentence is what the person reads.

import { w5 as realApi, errorText } from './w5-api.js?v=1';
import { t } from './i18n.js?v=4';
import { el } from './util.js?v=20';

// ------------------------------------------------------------------ pure

export const AUDIENCES = ['members', 'restricted', 'all'];
const AUDIENCE_KEYS = { members: 'chars.audience_members', restricted: 'chars.audience_restricted', all: 'chars.audience_all' };
export const audienceLabel = (a) => t(AUDIENCE_KEYS[a] || AUDIENCE_KEYS.members);

/** "Who can see it", in the order the select lists them. */
export const WHO_OPTIONS = ['everyone', 'members', 'restricted', 'hidden'];
const WHO_FLAGS = {
  everyone: { safe: true, hide_unlocked: false },      // audience "all"
  members: { safe: false, hide_unlocked: false },      // audience "members", on the rail
  restricted: { safe: true, hide_unlocked: true },     // audience "restricted"
  hidden: { safe: false, hide_unlocked: true },        // "members", off every rail (open by link)
};
/** The select's value for a roster row (its `safe` / `hide_unlocked` flags). */
export function whoCanSee(bot) {
  const safe = !!(bot && bot.safe);
  const hide = !!(bot && bot.hide_unlocked);
  if (safe) return hide ? 'restricted' : 'everyone';
  return hide ? 'hidden' : 'members';
}
/** The flags to send for a select value (a copy; unknown values are refused). */
export function flagsForWho(value) {
  const f = WHO_FLAGS[value];
  if (!f) throw new Error(`unknown visibility ${value}`);
  return { ...f };
}
/** Does going from `from` to `to` let Restricted profiles see a character they could not? */
export const widensToRestricted = (from, to) => !WHO_FLAGS[from].safe && WHO_FLAGS[to].safe;

/** The PATCH body for what changed between a character as the server returned it and the form. */
export function diffCharacter(orig, form) {
  const out = {};
  const be = orig.backend || {};
  if (form.description !== undefined && form.description !== (orig.description ?? '')) out.description = form.description;
  if (form.audience && form.audience !== orig.audience) out.audience = form.audience;
  if (form.profiles !== undefined) {
    const a = [...(orig.profiles || [])].sort();
    const b = [...form.profiles].sort();
    if (JSON.stringify(a) !== JSON.stringify(b)) out.profiles = form.profiles;
  }
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

/** Can this character be given a connection? Agent and app characters are answered elsewhere. */
export const takesConnection = (orig) => !!orig && (orig.backend_kind === 'direct' || orig.backend_kind === 'legacy-api');

/** Hash the voice section may read to open on this character's voice. Documented in docs/characters.md. */
export const voiceHash = (id) => `#voice/voices/${encodeURIComponent(id)}`;

// ------------------------------------------------------------------ DOM

function field(label, input, hint) {
  if (!input.id) input.id = `chr-${Math.random().toString(36).slice(2, 8)}`;
  const kids = [el('label', { for: input.id, text: label }), input];
  if (hint) kids.push(el('span', { class: 'people-field-hint', text: hint }));
  return el('div', { class: 'sec-field people-field' }, kids);
}

/** Build the details panel for one character. Resolves after its data has loaded.
 *  `ctx.skip` leaves fields out that the caller draws elsewhere: 'audience' (the
 *  row's Who can see it select) and 'personality' (the row's Personality editor). */
export async function characterPanel(bot, ctx) {
  const api = ctx.api || realApi;
  const skip = new Set(ctx.skip || []);
  const panel = el('div', { class: 'chr-panel dev-group', role: 'group', 'aria-label': t('chars.details_aria', { name: bot.name }) });
  panel.append(el('p', { class: 'muted', text: t('chars.loading') }));
  let orig;
  let conns = [];
  let presets = { presets: [] };
  let people = [];
  try {
    const [all, cx, pr, pp] = await Promise.all([
      api.allCharacters(), api.connections().catch(() => ({ connections: [] })),
      api.personalities().catch(() => ({ presets: [] })), api.people().catch(() => ({ profiles: [] }))]);
    orig = (all.characters || []).find((c) => c.id === bot.id);
    conns = cx.connections || [];
    presets = pr;
    people = (pp.profiles || []).filter((p) => p.role === 'member' || p.role === 'restricted');
  } catch (e) {
    panel.textContent = errorText(e);
    return panel;
  }
  if (!orig) { panel.textContent = t('chars.gone'); return panel; }
  panel.textContent = '';

  const desc = el('input', { type: 'text', class: 'ui-input', maxlength: '400', value: orig.description || '' });
  const aud = el('select', { class: 'ui-select' }, AUDIENCES.map((a) => el('option', { value: a, text: audienceLabel(a) })));
  aud.value = orig.audience;
  const visBox = el('fieldset', { class: 'chr-visibility' }, [
    el('legend', { text: t('chars.visibility') }), el('p', { class: 'muted', text: t('chars.visibility_hint') })]);
  const checks = people.map((p) => {
    const id = `chr-vis-${bot.id}-${p.id}`;
    const cb = el('input', { type: 'checkbox', id, class: 'ui-checkbox', value: p.id });
    cb.checked = (orig.profiles || []).includes(p.id);
    visBox.append(el('label', { class: 'bkp-check', for: id }, [cb, ' ', `${p.name} (${t(`role.${p.role}`)})`]));
    return cb;
  });
  if (!people.length) visBox.append(el('p', { class: 'muted', text: t('chars.visibility_none') }));

  const form = { api };
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
      field(t('chars.model'), model), models,
      skip.has('personality') ? null : field(t('chars.personality'), pers, t('chars.personality_hint')));
    if (!conns.length) {
      const go = el('button', { type: 'button', class: 'btn-secondary', text: t('chars.add_connection') });
      go.addEventListener('click', () => ctx.openTab && ctx.openTab('connections'));
      panel.append(go);
    }
  } else {
    panel.append(el('p', { class: 'muted', text: t('chars.agent_note') }));
  }
  panel.append(field(t('chars.description'), desc),
    skip.has('audience') ? null : field(t('chars.audience'), aud, t('chars.audience_hint')), visBox);

  const voice = el('button', { type: 'button', class: 'btn-secondary chr-voice', text: t('chars.voice_link') });
  voice.addEventListener('click', () => {
    try { window.location.hash = voiceHash(bot.id); } catch { /* ignore */ }
    ctx.openTab && ctx.openTab('voice');
  });
  const save = el('button', { type: 'button', class: 'btn-primary', text: t('chars.save') });
  const close = el('button', { type: 'button', class: 'btn-secondary', text: t('common.close') });
  const err = el('p', { class: 'lock-error', role: 'alert', hidden: '' });
  save.addEventListener('click', async () => {
    const values = {
      description: desc.value, audience: skip.has('audience') ? undefined : aud.value,
      profiles: checks.filter((c) => c.checked).map((c) => c.value),
    };
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
