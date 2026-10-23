// The simple personal-bot editor and the AI build flow: name, picture (upload / pick /
// generate when the profile may make images), personality preset or own text (at most
// 4,000 characters), voice (household voices plus the caller's own clones), backend
// (when more than one is allowed). Owned by WP-FE-YOU.
//
//   POST   /api/me/characters            create          PATCH /api/me/characters/{id}  change
//   POST   /api/me/characters/{id}/avatar  (multipart "file")
//   POST   /api/me/characters/draft      {description} -> {draft:{name,emoji,description,persona}}
//   GET    /api/voice/profiles           the voice add-on: rows carry `household` / `mine`
//   GET    /api/me/personas              preset personalities (create_characters)
//   GET    /api/me/backends              {may_choose, default, connections:[{id,name,...}]}: only the
//                                        connections this profile may use, never a key
//
// Everything is a text node (el() has no innerHTML). Nothing runs at import time.

import { t } from './i18n.js?v=5';
import { el } from './util.js?v=21';

export const NAME_MAX = 40;
export const EMOJI_MAX = 8;
export const PERSONA_MAX = 4000;
export const DESCRIPTION_MAX = 1000;       // POST /api/me/characters/draft: 1..1000

let shell = {};
/** Called once at boot with the shell's helpers ({ t, toast, confirm, api, state }). */
export function initBotEditor(deps = {}) { shell = deps || {}; }
/** The helpers handed to init, overlaid by whatever the caller passes. */
export function withShell(deps = {}) { return { ...shell, ...(deps || {}) }; }

/** The face square `crop` ({x, y, size}, fractions as the server reads them) of `blob`, as a
 *  PNG Blob for the preview; null where the browser cannot draw it (the server crops anyway). */
export async function faceBlob(blob, crop, px = 144) {
  try {
    const makeBitmap = globalThis.createImageBitmap;
    if (typeof makeBitmap !== 'function' || typeof document === 'undefined') return null;
    const bmp = await makeBitmap(blob);
    const side = Math.max(1, crop.size * Math.min(bmp.width, bmp.height));
    const sx = Math.max(0, Math.min(crop.x * bmp.width, bmp.width - side));
    const sy = Math.max(0, Math.min(crop.y * bmp.height, bmp.height - side));
    const c = document.createElement('canvas');
    c.width = c.height = px;
    c.getContext('2d').drawImage(bmp, sx, sy, side, side, 0, 0, px, px);
    if (bmp.close) bmp.close();
    return await new Promise((res) => c.toBlob(res, 'image/png'));
  } catch { return null; }
}

// ------------------------------------------------------------------ requests

/** A failed profile call: `status`, `code` (the 3.0 error envelope) and a plain `message`. */
export class ProfileError extends Error {
  constructor(status, code, message, body) {
    super(message || `HTTP ${status}`);
    this.name = 'ProfileError';
    this.status = status;
    this.code = code || '';
    this.body = body || null;
  }
}

/** JSON (or multipart) call that throws ProfileError. `form` is a FormData. */
export async function profileReq(method, path, { json, form, timeoutMs = 60000 } = {}) {
  const ctl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
  const init = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (ctl) init.signal = ctl.signal;
  if (json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(json); }
  if (form) init.body = form;
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ProfileError(0, 'network', e && e.name === 'AbortError' ? 'timeout' : String((e && e.message) || e));
  } finally {
    if (timer) clearTimeout(timer);
  }
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const env = body && typeof body.error === 'object' && body.error ? body.error : {};
    let detail = body && body.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d && d.msg).filter(Boolean).join('; ');
    throw new ProfileError(res.status, env.code || '', (typeof env.message === 'string' && env.message)
      || (typeof detail === 'string' && detail) || `HTTP ${res.status}`, body);
  }
  return body;
}

/** Does this session hold `perm`? (/api/auth/status `permissions`; ["*"] = everything.) */
export function can(perm, deps = {}) {
  const d = withShell(deps);
  const ps = d.state && d.state.auth && d.state.auth.permissions;
  if (!Array.isArray(ps)) return d.role === 'admin' || d.role === 'owner';
  return ps.includes('*') || ps.includes(perm);
}

// ------------------------------------------------------------------ pure logic

/** Field problems -> { name?, emoji?, persona? } with an i18n-ready message each. */
export function validateBot({ name = '', emoji = '', persona = '' } = {}) {
  const out = {};
  const n = String(name).trim();
  if (!n) out.name = t('editor.name_required');
  else if ([...n].length > NAME_MAX) out.name = t('editor.name_long', { max: NAME_MAX });
  if ([...String(emoji).trim()].length > EMOJI_MAX) out.emoji = t('editor.emoji_long', { max: EMOJI_MAX });
  const count = [...String(persona)].length;
  if (count > PERSONA_MAX) out.persona = t('editor.too_long', { count });
  return out;
}

/** The body for POST/PATCH /api/me/characters. A text equal to a preset is sent as that
 *  preset's id; anything else as `persona`. On a PATCH only changed fields go out. */
export function botPayload(form, original = null, presets = []) {
  const name = String(form.name || '').trim();
  const emoji = String(form.emoji || '').trim();
  const text = String(form.persona || '');
  const voice = String(form.voice || '');
  const conn = form.connection == null ? '' : String(form.connection);
  const body = {};
  const origText = original ? String((original.personality && original.personality.text) || original.persona || '') : '';
  const origVoice = original ? String(original.voice || '') : '';
  const origConn = original ? String((original.backend && original.backend.connection) || original.connection || '') : '';
  if (!original || name !== original.name) body.name = name;
  if (!original || emoji !== String(original.emoji || '')) body.emoji = emoji;
  if (!original || voice !== origVoice) body.voice = voice;
  if (conn && (!original || conn !== origConn)) body.connection = conn;
  if (!original || text.trim() !== origText.trim()) {
    const preset = (presets || []).find((p) => p && p.text && p.text.trim() === text.trim());
    if (preset) body.personality = preset.id;
    else if (text.trim() || original) body.persona = text.trim();
  }
  if (!original) { for (const k of Object.keys(body)) if (body[k] === '') delete body[k]; if (!body.name) body.name = name; }
  return body;
}

/** /api/voice/profiles rows -> { mine: [], household: [] } (other profiles' clones never arrive). */
export function voiceGroups(rows) {
  const out = { mine: [], household: [] };
  for (const r of rows || []) {
    if (!r || !r.id) continue;
    (r.mine ? out.mine : out.household).push({ id: String(r.id), name: String(r.name || r.id) });
  }
  return out;
}

/** The model's draft -> editor fields; unknown or oversized values are trimmed to fit. */
export function draftToForm(draft) {
  const d = draft || {};
  return {
    name: [...String(d.name || '')].slice(0, NAME_MAX).join(''),
    emoji: [...String(d.emoji || '')].slice(0, EMOJI_MAX).join(''),
    persona: String(d.persona || '').slice(0, PERSONA_MAX),
  };
}

/** Message for a failed AI build call. */
export function draftErrorText(e) {
  if (e && e.status === 503) return t('aibuild.unavailable');
  if (e && e.status === 403) return t('aibuild.not_allowed');
  return t('aibuild.failed');
}

// ------------------------------------------------------------------ options

async function tryGet(path) {
  try { return await profileReq('GET', path, { timeoutMs: 15000 }); } catch { return null; }
}

/** Everything the editor offers besides the fields: presets, voices, backends. Each part
 *  that the caller cannot read (403/404) is simply absent. */
export async function loadEditorOptions(deps = {}) {
  // The voice routes exist only when the voice add-on is installed and loaded: ask
  // /api/extensions first, so an install without it never logs a 404.
  const exts = await tryGet('/api/extensions');
  const voiceOn = !!(exts && Array.isArray(exts.extensions)
    && exts.extensions.some((e) => e && e.id === 'voice' && e.entry));
  const [personas, voices, conns, status] = await Promise.all([
    tryGet('/api/me/personas'),
    voiceOn ? tryGet('/api/voice/profiles') : Promise.resolve(null),
    can('choose_backend', deps) ? tryGet('/api/me/backends') : Promise.resolve(null),
    voiceOn ? tryGet('/api/voice/status') : Promise.resolve(null),
  ]);
  const rows = (voices && (voices.voices || voices.profiles)) || [];
  const builtin = ((status && (status.builtin_voices || (status.voice_choices && status.voice_choices.builtin_voices))) || [])
    .filter((v) => v && v.id).map((v) => ({ id: String(v.id), name: String(v.name || v.id) }));
  const list = (conns && conns.may_choose !== false && conns.connections) || [];
  return {
    presets: (personas && personas.presets) || [],
    voices: { ...voiceGroups(rows), builtin },
    connections: (Array.isArray(list) ? list : [])
      .filter((c) => c && c.id).map((c) => ({ id: String(c.id), name: String(c.name || c.id) })),
    defaultConnection: (conns && typeof conns.default === 'string') ? conns.default : '',
  };
}

// ------------------------------------------------------------------ DOM

function field(label, control, { hint = '', error = null, id = '' } = {}) {
  return el('div', { class: 'yo-field' + (error ? ' has-error' : '') }, [
    el('label', { class: 'yo-label', for: id, text: label }),
    control,
    hint ? el('div', { class: 'yo-hint muted', text: hint }) : null,
    error ? el('div', { class: 'yo-error', role: 'alert', text: error }) : null,
  ]);
}

function option(value, text, selected) {
  const o = el('option', { value, text });
  if (selected) o.selected = true;
  return o;
}

async function blobFromUrl(url) {
  const r = await fetch(url, { credentials: 'same-origin' });
  if (!r.ok) throw new ProfileError(r.status, '', `HTTP ${r.status}`);
  return r.blob();
}

/** Make a picture with the Image Studio (only for a profile that holds use_images). */
export async function generatePicture(name, persona) {
  const subject = [name, String(persona || '').slice(0, 200)].filter(Boolean).join(': ');
  const out = await profileReq('POST', '/api/apps/imagestudio/generate', {
    json: { prompt: `Portrait avatar of a friendly chat character. ${subject}`.slice(0, 900), ratio: '1:1', n: 1 },
    timeoutMs: 180000,
  });
  const row = out && out.images && out.images[0];
  if (!row || !row.url) throw new ProfileError(502, 'no_image', t('editor.picture_generate'));
  return blobFromUrl(row.url);
}

/**
 * Open the editor for `bot` (null = a new bot) inside `host`. Resolves to the saved bot, or
 * null when the editor was closed without saving.
 * deps: toast, prefill ({name,emoji,persona} from AI build), notice (a line above the form),
 * options (preloaded loadEditorOptions result), householdAvatars ([{id,name,avatar_url}]).
 */
export async function renderBotEditor(host, bot = null, deps = {}) {
  const d = withShell(deps);
  if (!host) return null;
  host.replaceChildren(el('p', { class: 'muted', text: t('common.loading') }));
  const options = d.options || await loadEditorOptions(d);
  const original = bot || null;
  const pre = d.prefill || {};
  const text0 = original ? String((original.personality && original.personality.text) || original.persona || '') : '';
  const form = {
    name: original ? original.name : (pre.name || ''),
    emoji: original ? String(original.emoji || '') : (pre.emoji || ''),
    persona: original ? text0 : (pre.persona || ''),
    voice: original ? String(original.voice || '') : '',
    connection: original ? String((original.backend && original.backend.connection) || original.connection || '') : '',
  };
  let picture = null;          // a Blob chosen but not uploaded yet
  let pictureCrop = null;      // {x, y, size}: the face square the user placed on it (fractions)
  let previewUrl = original && original.avatar_url ? original.avatar_url : '';
  let busy = false;

  return new Promise((resolve) => {
    const root = el('form', { class: 'yo-editor', novalidate: 'novalidate', 'aria-labelledby': 'yo-editor-title' });
    const title = el('h3', { class: 'yo-title', id: 'yo-editor-title',
      text: original ? t('editor.title_edit', { name: original.name }) : t('editor.title_new') });
    const notice = el('p', { class: 'yo-notice muted', text: d.notice || '', hidden: d.notice ? null : 'hidden' });
    const banner = el('div', { class: 'yo-error yo-banner', role: 'alert', hidden: 'hidden' });

    const nameIn = el('input', { class: 'ui-input', id: 'yo-name', type: 'text', maxlength: String(NAME_MAX), value: form.name, autocomplete: 'off' });
    const emojiIn = el('input', { class: 'ui-input yo-emoji', id: 'yo-emoji', type: 'text', maxlength: String(EMOJI_MAX), value: form.emoji, autocomplete: 'off' });
    const personaIn = el('textarea', { class: 'ui-input yo-persona', id: 'yo-persona', rows: '7' });
    personaIn.value = form.persona;
    const counter = el('div', { class: 'yo-counter muted', 'aria-live': 'polite' });
    const presetSel = el('select', { class: 'ui-select', id: 'yo-preset' });
    const voiceSel = el('select', { class: 'ui-select', id: 'yo-voice' });
    const connSel = el('select', { class: 'ui-select', id: 'yo-conn' });
    const previewImg = el('img', { class: 'yo-preview', alt: '', width: '72', height: '72' });
    const previewBox = el('div', { class: 'yo-preview-box' }, [previewImg]);
    const pickGrid = el('div', { class: 'yo-pick-grid', hidden: 'hidden' });
    const fileIn = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp,image/gif', hidden: 'hidden', id: 'yo-file' });

    const showPreview = () => {
      if (previewUrl) { previewImg.src = previewUrl; previewBox.hidden = false; } else previewBox.hidden = true;
    };
    const setPreview = (blob) => {
      if (previewUrl && previewUrl.startsWith('blob:')) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(blob);
      showPreview();
    };
    // Every new picture (uploaded, picked or generated) goes through the face picker, as the
    // household bots' pictures do: the user drags the square over the face, and that square
    // becomes the avatar (the full picture is kept too). `keepOnCancel`: a closed picker keeps
    // the picture with a centred square (a generated one cost a render) instead of dropping it.
    const setPicture = async (blob, { keepOnCancel = false } = {}) => {
      let chosen = null;
      if (typeof d.chooseFace === 'function') {
        chosen = await d.chooseFace(blob, nameIn.value.trim());
        if (!chosen && !keepOnCancel) return false;
      }
      picture = blob;
      pictureCrop = chosen;
      setPreview((chosen && await faceBlob(blob, chosen)) || blob);
      return true;
    };
    const updateCounter = () => {
      const n = [...personaIn.value].length;
      counter.textContent = `${n} / ${PERSONA_MAX}`;
      counter.classList.toggle('over', n > PERSONA_MAX);
    };
    updateCounter();
    showPreview();

    // presets
    if (options.presets.length) {
      presetSel.append(option('', t('editor.preset'), false), option('__own', t('editor.own_text'), false));
      for (const p of options.presets) presetSel.append(option(p.id, p.title || p.id, false));
      const match = options.presets.find((p) => p.text && p.text.trim() === personaIn.value.trim());
      presetSel.value = match ? match.id : (personaIn.value.trim() ? '__own' : '');
      presetSel.addEventListener('change', () => {
        const p = options.presets.find((x) => x.id === presetSel.value);
        if (p) { personaIn.value = p.text; updateCounter(); }
        else if (presetSel.value === '__own') personaIn.focus();
      });
      personaIn.addEventListener('input', () => {
        const m = options.presets.find((x) => x.text && x.text.trim() === personaIn.value.trim());
        presetSel.value = m ? m.id : '__own';
      });
    }
    personaIn.addEventListener('input', updateCounter);

    // voices: default, then the caller's own clones, then household voices
    voiceSel.append(option('', t('editor.voice_default'), !form.voice));
    const addGroup = (label, rows) => {
      if (!rows.length) return;
      const g = el('optgroup', { label });
      for (const r of rows) g.append(option(r.id, r.name, r.id === form.voice));
      voiceSel.append(g);
    };
    addGroup(t('editor.voice_mine'), options.voices.mine);
    addGroup(t('editor.voice_household'), options.voices.household);
    addGroup(t('editor.voice_builtin'), options.voices.builtin || []);
    if (form.voice && ![...voiceSel.options].some((o) => o.value === form.voice)) {
      voiceSel.append(option(form.voice, form.voice, true));
    }

    // backend: only when there is a real choice
    const showBackend = options.connections.length > 1;
    if (showBackend) {
      for (const c of options.connections) connSel.append(option(c.id, c.name, c.id === form.connection));
      if (!form.connection) {
        const def = options.connections.find((c) => c.id === options.defaultConnection);
        connSel.value = (def || options.connections[0]).id;
      }
    }

    // picture controls
    fileIn.addEventListener('change', () => {
      const f = fileIn.files && fileIn.files[0];
      fileIn.value = '';                    // the same file can be chosen again
      if (f) setPicture(f);
    });
    const uploadBtn = el('button', { class: 'ui-btn', type: 'button', text: t('editor.picture_upload'),
      onclick: () => fileIn.click() });
    const pickBtn = el('button', { class: 'ui-btn', type: 'button', text: t('editor.picture_pick'),
      onclick: () => { pickGrid.hidden = !pickGrid.hidden; } });
    const genBtn = el('button', { class: 'ui-btn', type: 'button', text: t('editor.picture_generate'),
      onclick: async () => {
        if (busy) return;
        const errs = validateBot({ name: nameIn.value });
        if (errs.name) { nameIn.focus(); banner.textContent = errs.name; banner.hidden = false; return; }
        busy = true; genBtn.disabled = true; genBtn.textContent = t('aibuild.working');
        try { await setPicture(await generatePicture(nameIn.value.trim(), personaIn.value), { keepOnCancel: true }); banner.hidden = true; }
        catch (e) { banner.textContent = e.message || t('aibuild.failed'); banner.hidden = false; }
        finally { busy = false; genBtn.disabled = false; genBtn.textContent = t('editor.picture_generate'); }
      } });
    const canGenerate = can('use_images', d);
    for (const h of (d.householdAvatars || []).slice(0, 16)) {
      if (!h.avatar_url) continue;
      pickGrid.append(el('button', { class: 'yo-pick', type: 'button', title: h.name || '',
        'aria-label': h.name || '', onclick: async () => {
          try { if (await setPicture(await blobFromUrl(h.avatar_url))) pickGrid.hidden = true; }
          catch { banner.textContent = t('aibuild.failed'); banner.hidden = false; }
        } }, [el('img', { src: h.avatar_url, alt: '', width: '48', height: '48', loading: 'lazy' })]));
    }
    if (!pickGrid.children.length) pickBtn.hidden = true;

    const cancelBtn = el('button', { class: 'ui-btn', type: 'button', text: t('editor.cancel') });
    const saveBtn = el('button', { class: 'ui-btn ui-btn--primary', type: 'submit', text: t('editor.save') });

    const finish = (val) => { if (previewUrl && previewUrl.startsWith('blob:')) URL.revokeObjectURL(previewUrl); resolve(val); };
    cancelBtn.addEventListener('click', () => finish(null));
    root.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); finish(null); } });

    const errBoxes = { name: null, emoji: null, persona: null };
    const showErrors = (errs) => {
      for (const k of Object.keys(errBoxes)) { if (errBoxes[k]) errBoxes[k].remove(); errBoxes[k] = null; }
      const at = { name: nameIn, emoji: emojiIn, persona: personaIn };
      for (const [k, msg] of Object.entries(errs)) {
        errBoxes[k] = el('div', { class: 'yo-error', role: 'alert', text: msg });
        at[k].after(errBoxes[k]);
        at[k].setAttribute('aria-invalid', 'true');
      }
      for (const k of Object.keys(at)) if (!errs[k]) at[k].removeAttribute('aria-invalid');
      const first = Object.keys(errs)[0];
      if (first) at[first].focus();
    };

    root.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      if (busy) return;
      const current = { name: nameIn.value, emoji: emojiIn.value, persona: personaIn.value,
        voice: voiceSel.value, connection: showBackend ? connSel.value : form.connection };
      const errs = validateBot(current);
      showErrors(errs);
      if (Object.keys(errs).length) return;
      const body = botPayload(current, original, options.presets);
      busy = true; saveBtn.disabled = true; banner.hidden = true;
      try {
        let saved = original;
        if (!original) saved = await profileReq('POST', '/api/me/characters', { json: body });
        else if (Object.keys(body).length) saved = await profileReq('PATCH', `/api/me/characters/${encodeURIComponent(original.id)}`, { json: body });
        if (picture) {
          const fd = new FormData();
          fd.append('file', picture, picture.name || 'avatar.png');
          try {
            const q = pictureCrop ? `?${new URLSearchParams({ crop_x: pictureCrop.x, crop_y: pictureCrop.y, crop_size: pictureCrop.size })}` : '';
            const r = await profileReq('POST', `/api/me/characters/${encodeURIComponent(saved.id)}/avatar${q}`, { form: fd });
            if (r && r.avatar_url) saved = { ...saved, avatar_url: r.avatar_url };
          } catch (e) {
            // The bot is saved; only the picture failed. Say so and keep the editor open on it.
            banner.textContent = e.message; banner.hidden = false;
            if (!original) { d.toast && d.toast(t('editor.saved')); }
            finish(saved); return;
          }
        }
        if (d.toast) d.toast(t('editor.saved'));
        finish(saved);
      } catch (e) {
        banner.textContent = e.message || String(e);
        banner.hidden = false;
        busy = false; saveBtn.disabled = false;
      }
    });

    root.append(...[
      title, notice, banner,
      el('div', { class: 'yo-row' }, [
        field(t('editor.name'), nameIn, { id: 'yo-name' }),
        field(t('editor.emoji'), emojiIn, { id: 'yo-emoji' }),
      ]),
      el('div', { class: 'yo-field' }, [
        el('div', { class: 'yo-label', text: t('editor.picture') }),
        previewBox,
        el('div', { class: 'yo-actions' }, [uploadBtn, pickBtn, canGenerate ? genBtn : null]),
        pickGrid, fileIn,
      ]),
      options.presets.length ? field(t('editor.preset'), presetSel, { id: 'yo-preset' }) : null,
      field(t('editor.personality'), el('div', {}, [personaIn, counter]),
        { id: 'yo-persona', hint: t('editor.own_text_hint') }),
      field(t('editor.voice'), voiceSel, { id: 'yo-voice' }),
      showBackend ? field(t('editor.backend'), connSel, { id: 'yo-conn' }) : null,
      el('div', { class: 'yo-actions yo-foot' }, [cancelBtn, saveBtn]),
    ].filter(Boolean));
    host.replaceChildren(root);
    nameIn.focus();
  });
}

/**
 * The AI build flow inside `host`: describe -> draft -> the editor prefilled with it.
 * Resolves to the saved bot or null (cancelled). Nothing is saved until the person saves.
 */
export async function renderAiBuild(host, deps = {}) {
  const d = withShell(deps);
  if (!host) return null;
  const options = d.options || await loadEditorOptions(d);
  for (;;) {
    const description = await askDescription(host, d);
    if (description == null) return null;
    let draft;
    try {
      draft = (await profileReq('POST', '/api/me/characters/draft', { json: { description }, timeoutMs: 120000 })).draft;
    } catch (e) {
      const again = await showDraftFailure(host, draftErrorText(e));
      if (again) continue;
      return null;
    }
    return renderBotEditor(host, null, { ...d, options, prefill: draftToForm(draft), notice: t('aibuild.review') });
  }
}

function askDescription(host, d) {
  return new Promise((resolve) => {
    const area = el('textarea', { class: 'ui-input yo-desc', id: 'yo-desc', rows: '4', maxlength: String(DESCRIPTION_MAX),
      placeholder: t('aibuild.placeholder') });
    const go = el('button', { class: 'ui-btn ui-btn--primary', type: 'submit', text: t('aibuild.generate') });
    const cancel = el('button', { class: 'ui-btn', type: 'button', text: t('editor.cancel'), onclick: () => resolve(null) });
    const form = el('form', { class: 'yo-editor yo-aibuild', novalidate: 'novalidate' }, [
      el('h3', { class: 'yo-title', text: t('aibuild.title') }),
      field(t('aibuild.prompt'), area, { id: 'yo-desc' }),
      el('div', { class: 'yo-actions yo-foot' }, [cancel, go]),
    ]);
    form.addEventListener('submit', (ev) => {
      ev.preventDefault();
      const v = area.value.trim();
      if (!v) { area.focus(); return; }
      go.disabled = true; go.textContent = t('aibuild.working');
      area.disabled = true;
      resolve(v);
    });
    form.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); resolve(null); } });
    host.replaceChildren(form);
    area.focus();
    void d;
  });
}

function showDraftFailure(host, message) {
  return new Promise((resolve) => {
    host.replaceChildren(el('div', { class: 'yo-editor yo-aibuild' }, [
      el('h3', { class: 'yo-title', text: t('aibuild.title') }),
      el('div', { class: 'yo-error', role: 'alert', text: message }),
      el('div', { class: 'yo-actions yo-foot' }, [
        el('button', { class: 'ui-btn', type: 'button', text: t('editor.cancel'), onclick: () => resolve(false) }),
        el('button', { class: 'ui-btn ui-btn--primary', type: 'button', text: t('aibuild.retry'), onclick: () => resolve(true) }),
      ]),
    ]));
  });
}
