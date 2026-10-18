// Settings -> Bots (called Characters in 3.0): what the 3.0 character model adds to each row.
//
//   Details panel   per character: its connection and model, personality preset, audience, which
//                   profiles may see it, a one-line description, and a link to Voice -> Voices for
//                   this character (the voice add-on owns that section; this only opens it).
//   Starter packs   Animal Friends (gentle animals for children, Restricted audience) and
//                   Assistants, one button each; installing is idempotent.
//
// The rows themselves, their on/off switches and the Save button are main.js's (and bots-edit.js);
// this module only adds to them, through `decorateCharacterRows`. Everything here acts at once, like
// the inline editor beside it. The server is the authority: it refuses a connection for an agent
// character, an unknown profile, and so on, and its sentence is what the person reads.

import { w5 as realApi, errorText } from './w5-api.js?v=1';
import { t } from './i18n.js?v=4';
import { el } from './util.js?v=20';

// ------------------------------------------------------------------ pure

export const AUDIENCES = ['members', 'restricted', 'all'];
const AUDIENCE_KEYS = { members: 'chars.audience_members', restricted: 'chars.audience_restricted', all: 'chars.audience_all' };
export const audienceLabel = (a) => t(AUDIENCE_KEYS[a] || AUDIENCE_KEYS.members);

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

/** Build the details panel for one character. Resolves after its data has loaded. */
export async function characterPanel(bot, ctx) {
  const api = ctx.api || realApi;
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
    form.conn = conn; form.model = model; form.pers = pers;
    panel.append(
      field(t('chars.connection'), conn, t('chars.connection_hint')),
      field(t('chars.model'), model), models,
      field(t('chars.personality'), pers, t('chars.personality_hint')));
    if (!conns.length) {
      const go = el('button', { type: 'button', class: 'btn-secondary', text: t('chars.add_connection') });
      go.addEventListener('click', () => ctx.openTab && ctx.openTab('connections'));
      panel.append(go);
    }
  } else {
    panel.append(el('p', { class: 'muted', text: t('chars.agent_note') }));
  }
  panel.append(field(t('chars.description'), desc), field(t('chars.audience'), aud, t('chars.audience_hint')), visBox);

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
      description: desc.value, audience: aud.value,
      profiles: checks.filter((c) => c.checked).map((c) => c.value),
    };
    if (form.conn) { values.connection = form.conn.value; values.model = form.model.value.trim(); values.personality = form.pers.value; }
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
  close.addEventListener('click', () => panel.remove());
  panel.append(err, el('div', { class: 'cxn-actions' }, [save, voice, close]));
  return panel;
}

/** Add a "Details" button to every row of the Bots list. `bots` is the list the rows were drawn from. */
export function decorateCharacterRows(listEl, bots, ctx) {
  const rows = [...listEl.querySelectorAll('.bm-row')];
  rows.forEach((row, idx) => {
    const bot = bots[idx];
    if (!bot || row.querySelector('.chr-details-btn')) return;
    const btn = el('button', { type: 'button', class: 'bm-avatar-btn chr-details-btn', text: t('chars.details'),
      'aria-expanded': 'false', 'aria-label': t('chars.details_open', { name: bot.name }) });
    let panel = null;
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (panel) { panel.remove(); panel = null; btn.setAttribute('aria-expanded', 'false'); return; }
      btn.setAttribute('aria-expanded', 'true');
      panel = await characterPanel(bot, ctx);
      row.after(panel);
    });
    row.append(btn);
  });
}

/** The starter-pack card at the top of the Bots pane. */
export async function mountStarterPacks(host, ctx = {}) {
  const api = ctx.api || realApi;
  const old = host.querySelector('.chr-packs');
  if (old) old.remove();
  const box = el('section', { class: 'dev-group chr-packs', 'aria-labelledby': 'chr-packs-h' }, [
    el('h3', { class: 'dev-group-title', id: 'chr-packs-h', text: t('chars.packs_title') }),
    el('p', { class: 'dev-group-hint', text: t('chars.packs_hint') })]);
  host.prepend(box);
  let packs;
  let conns = [];
  try {
    const [p, c] = await Promise.all([api.starterPacks(), api.connections().catch(() => ({ connections: [] }))]);
    packs = p.packs; conns = c.connections || [];
  } catch (e) { box.append(el('p', { class: 'muted', text: errorText(e) })); return box; }
  const connSel = el('select', { class: 'ui-select', 'aria-label': t('chars.packs_connection') }, [
    el('option', { value: '', text: t('chars.no_connection') }),
    ...conns.map((c) => el('option', { value: c.id, text: c.name }))]);
  if (conns.length) connSel.value = conns[0].id;
  box.append(el('div', { class: 'bkp-form' }, [el('label', { text: t('chars.packs_connection') }), connSel]));
  const body = el('div', { class: 'dev-group-body' });
  box.append(body);
  for (const p of packs) {
    const btn = el('button', { type: 'button', class: 'btn-secondary',
      text: p.installed ? t('chars.pack_installed') : (p.partly_installed ? t('chars.pack_install_rest') : t('chars.pack_install')) });
    if (p.installed) btn.disabled = true;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const r = await api.installPack(p.id, connSel.value ? { connection: connSel.value } : {});
        ctx.toast && ctx.toast(t('chars.pack_done', { count: r.installed.length }));
        ctx.reload && (await ctx.reload());
        await mountStarterPacks(host, ctx);
      } catch (e) { ctx.toast && ctx.toast(errorText(e), true); btn.disabled = false; }
    });
    body.append(el('div', { class: 'dev-row chr-pack', dataset: { pack: p.id } }, [
      el('span', { class: 'dev-row-text' }, [
        el('strong', {}, [p.name, ' ', el('span', { class: 'ui-badge', text: audienceLabel(p.audience) })]),
        el('span', { class: 'dev-row-hint', text: p.description }),
        el('span', { class: 'dev-row-hint', text: p.characters.map((c) => `${c.emoji} ${c.name}`).join('  ') }),
      ]), btn]));
  }
  return box;
}
