// Settings → Device → Drive mode → Voices: the cloned-voice manager, native.
//
// Replaces the link to dispatch-voice's standalone page (/api/voices/ui, still
// served for `dispatch-voice serve` and CLI use, no longer linked from here).
// Same JSON API as that page, same rules: a voice is built on THIS computer
// from a 10–20 s clip, and whose voice it is (consent) is a required choice.
//
//   GET    /api/voices                → {voices, default, engine, watermarking}
//   GET    /api/voices/consent-kinds  → {kind: statement}
//   POST   /api/voices/analyse        multipart file → quality report
//   POST   /api/voices                multipart file,name,consent,consent_note
//   PATCH  /api/voices/{id}           {default: true} | {name}
//   DELETE /api/voices/{id}
//   GET    /api/voices/{id}/files/audition.wav
//
// Built-in (Kokoro) voices are listed too, with ▶ and "Set as default"
// through /api/voice/settings, so one screen answers "what will I hear".
//
// Rows are the Device tab's .dev-group/.dev-row markup; nothing here has a
// look of its own beyond the quality chips and the recording script box.

import { t } from './i18n.js?v=3';
import { el, railIcon, RAIL_ICONS } from './util.js?v=20';
import {
  voiceJson, voiceStatus, patchVoiceSettings, previewButton, selectInput, row, MIC_ICON, builtinValue,
} from './drive-settings.js?v=2';

const API = '/api/voices';
const RECORD_LIMIT_S = 40;

/** Quality checks → chip level classes (server levels ok | warn | fail). */
export function chipClass(level) {
  return level === 'fail' ? 'drive-chip fail' : level === 'warn' ? 'drive-chip warn' : 'drive-chip ok';
}

/** Can this clip be built? Mirrors the server's own refusal rules. */
export function canBuild({ clip, analysis, name, consent }) {
  return !!(clip && analysis && analysis.ok && String(name || '').trim() && consent);
}

/** One line under a voice's name: who it is, when it was added, how long the clip. */
export function voiceMetaLine(v, fmtDate) {
  const parts = [];
  const kind = v && v.consent && v.consent.kind;
  if (kind === 'self') parts.push(t('voices.kind_self'));
  else if (kind === 'permission') parts.push(t('voices.kind_permission'));
  else if (kind === 'synthetic') parts.push(t('voices.kind_synthetic'));
  if (v && v.created) parts.push(t('voices.added', { date: fmtDate(v.created * 1000) }));
  if (v && v.reference_s) parts.push(t('voices.clip_len', { s: Math.round(v.reference_s) }));
  return parts.join(' · ');
}

/**
 * Mount the sub-pane. `pane` is the #spane-device section; the sub-pane
 * takes its place until ‹ Drive mode is pressed. ctx: { toast, confirm(msg,
 * opts), prompt(title, value), onChanged() — repaint the Drive group }.
 */
export function createVoicesPane(pane, ctx) {
  let host = null;
  let pollTimer = null;
  let recorder = null;
  let recTimer = null;
  let clip = null;
  let analysis = null;
  let audio = null;

  const fmtDate = (ms) => {
    try { return new Intl.DateTimeFormat(document.documentElement.lang || undefined, { day: 'numeric', month: 'short' }).format(new Date(ms)); }
    catch { return new Date(ms).toDateString(); }
  };

  function stopAudio() {
    if (audio) { try { audio.pause(); } catch { /* gone */ } audio = null; }
  }
  function play(url) {
    stopAudio();
    audio = new Audio(url);
    audio.play().catch(() => ctx.toast(t('drive.err_preview'), true));
  }

  function open() {
    if (host) return;
    for (const c of pane.children) { c.dataset.voicesHidden = c.hidden ? '1' : '0'; c.hidden = true; }
    host = el('div', { class: 'voices-pane', id: 'voices-pane' });
    pane.append(host);
    pane.scrollTop = 0;
    render();
  }

  function close() {
    if (!host) return;
    clearTimeout(pollTimer); pollTimer = null;
    stopRecording(true);
    stopAudio();
    host.remove(); host = null;
    for (const c of pane.children) { if (c.dataset.voicesHidden != null) { c.hidden = c.dataset.voicesHidden === '1'; delete c.dataset.voicesHidden; } }
    clip = null; analysis = null;
    ctx.onChanged && ctx.onChanged();
    const btn = document.getElementById('dev-voices-open');
    if (btn) { try { btn.focus(); btn.scrollIntoView({ block: 'center' }); } catch { /* ignore */ } }
  }

  async function render() {
    if (!host) return;
    const back = el('button', { type: 'button', class: 'voices-back' }, [
      railIcon(RAIL_ICONS.back), document.createTextNode(` ${t('voices.back')}`),
    ]);
    back.addEventListener('click', close);
    const title = el('h3', { class: 'voices-title', text: t('voices.title') });
    const listBody = el('div', { class: 'dev-group-body', id: 'voices-list' }, [
      el('div', { class: 'dev-row' }, [el('span', { class: 'dev-row-text' }, [el('span', { class: 'dev-row-hint', text: t('common.loading') })])]),
    ]);
    const builtinBody = el('div', { class: 'dev-group-body', id: 'voices-builtin' });
    const addBody = el('div', { class: 'dev-group-body', id: 'voices-add' });
    const engine = el('p', { class: 'dev-group-hint voices-engine' });
    host.replaceChildren(
      el('div', { class: 'voices-top' }, [back, title]),
      el('p', { class: 'dev-group-hint voices-intro', text: t('voices.intro') }),
      el('section', { class: 'dev-group' }, [el('h3', { class: 'dev-group-title', text: t('voices.yours') }), listBody]),
      el('section', { class: 'dev-group' }, [el('h3', { class: 'dev-group-title', text: t('voices.builtin') }), builtinBody]),
      el('section', { class: 'dev-group' }, [el('h3', { class: 'dev-group-title', text: t('voices.add') }), addBody]),
      engine,
    );
    renderAdd(addBody);
    back.focus();
    await refresh();
  }

  async function refresh() {
    if (!host) return;
    const listBody = host.querySelector('#voices-list');
    const builtinBody = host.querySelector('#voices-builtin');
    let data; let st = null;
    try {
      [data, st] = await Promise.all([voiceJson(API), voiceStatus().catch(() => null)]);
    } catch (e) {
      listBody.replaceChildren(el('div', { class: 'dev-row' }, [el('span', { class: 'dev-row-text' }, [
        el('span', { class: 'dev-row-hint voices-err', text: t('voices.load_failed', { error: e.message }) }),
      ])]));
      return;
    }
    if (!host) return;
    const defaultVoice = (st && st.default_voice) || (data.default ? data.default : '');
    const voices = data.voices || [];
    if (!voices.length) {
      listBody.replaceChildren(el('div', { class: 'dev-row' }, [el('span', { class: 'dev-row-text' }, [
        el('strong', { text: t('voices.none') }),
        el('span', { class: 'dev-row-hint', text: t('voices.none_hint') }),
      ])]));
    } else {
      listBody.replaceChildren(...voices.map((v) => voiceRow(v, defaultVoice)));
    }
    renderBuiltin(builtinBody, st, defaultVoice);
    const eng = host.querySelector('.voices-engine');
    if (eng) {
      eng.textContent = data.engine
        ? (data.watermarking ? t('voices.engine_wm', { engine: data.engine }) : t('voices.engine', { engine: data.engine }))
        : t('voices.engine_off');
    }
    clearTimeout(pollTimer);
    if (voices.some((v) => v.fillers && v.fillers !== 'ready')) pollTimer = setTimeout(refresh, 4000);
  }

  function voiceRow(v, defaultVoice) {
    const isDefault = defaultVoice ? defaultVoice === v.id : !!v.default;
    const base = `${API}/${encodeURIComponent(v.id)}`;
    const text = el('span', { class: 'dev-row-text' }, [
      el('strong', { dir: 'auto' }, [
        document.createTextNode(v.name),
        isDefault ? el('span', { class: 'pin-toggle pinned voices-default', text: t('voices.default_badge') }) : null,
      ]),
      el('span', { class: 'dev-row-hint', text: voiceMetaLine(v, fmtDate) }),
      v.fillers && v.fillers !== 'ready' ? el('span', { class: 'dev-row-hint', text: t('voices.fillers_pending') }) : null,
    ]);
    const test = el('button', { type: 'button', class: 'btn-secondary' }, [railIcon(['M8 5.5v13l10.5-6.5Z']), document.createTextNode(` ${t('voices.test')}`)]);
    test.addEventListener('click', () => play(`${base}/files/audition.wav`));
    const actions = el('div', { class: 'voices-actions' }, [test]);
    if (!isDefault) {
      const def = el('button', { type: 'button', class: 'btn-secondary', text: t('voices.set_default') });
      def.addEventListener('click', () => setDefault(v.id, true));
      actions.append(def);
    }
    const ren = el('button', { type: 'button', class: 'btn-secondary', text: t('voices.rename') });
    ren.addEventListener('click', async () => {
      const n = await ctx.prompt(t('voices.rename_title'), v.name);
      if (n && n.trim() && n.trim() !== v.name) {
        try {
          await voiceJson(base, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: n.trim() }) });
          refresh();
        } catch (e) { ctx.toast(e.message, true); }
      }
    });
    const del = el('button', { type: 'button', class: 'btn-secondary voices-delete', text: t('common.delete') });
    del.addEventListener('click', async () => {
      if (!await ctx.confirm(t('voices.delete_confirm', { name: v.name }), { danger: true, okText: t('common.delete') })) return;
      try { await voiceJson(base, { method: 'DELETE' }); refresh(); ctx.onChanged && ctx.onChanged(); }
      catch (e) { ctx.toast(e.message, true); }
    });
    actions.append(ren, del);
    return el('div', { class: 'dev-row dev-row-block voices-row', dataset: { id: v.id } }, [text, actions]);
  }

  async function setDefault(value, isProfile) {
    try {
      try {
        await patchVoiceSettings({ default_voice: value });
      } catch (e) {
        // A server without /api/voice/settings yet: the profile store's own default.
        if (isProfile && e.status === 404) {
          await voiceJson(`${API}/${encodeURIComponent(value)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ default: true }) });
        } else throw e;
      }
      ctx.toast(t('voices.default_set'));
      refresh();
      ctx.onChanged && ctx.onChanged();
    } catch (e) { ctx.toast(t('drive.err_save') + ' ' + e.message, true); }
  }

  function renderBuiltin(body, st, defaultVoice) {
    const list = (st && st.builtin_voices) || [];
    if (!list.length) {
      body.replaceChildren(el('div', { class: 'dev-row' }, [el('span', { class: 'dev-row-text' }, [
        el('span', { class: 'dev-row-hint', text: t('voices.builtin_unknown') }),
      ])]));
      return;
    }
    const opts = list.map((b) => ({ value: builtinValue(b.id), label: b.name || b.id }));
    const cur = defaultVoice && defaultVoice.startsWith('builtin:') ? defaultVoice : opts[0].value;
    const sel = selectInput(t('voices.builtin'), opts, cur, () => paintDef());
    const def = el('button', { type: 'button', class: 'btn-secondary', text: t('voices.set_default') });
    def.addEventListener('click', () => setDefault(sel.value, false));
    const r = row(t('voices.builtin_pick'), '', [sel, previewButton(() => sel.value, (m) => ctx.toast(m, true)), def]);
    r.classList.add('voices-builtin-row');
    const hint = el('span', { class: 'dev-row-hint' });
    r.querySelector('.dev-row-text').append(hint);
    function paintDef() {
      const on = sel.value === defaultVoice;
      def.hidden = on;
      hint.textContent = on ? t('voices.builtin_is_default') : t('voices.builtin_hint');
    }
    paintDef();
    body.replaceChildren(r);
  }

  // ---- add a voice -------------------------------------------------------
  function renderAdd(body) {
    const status = el('p', { class: 'voices-status', role: 'status', 'aria-live': 'polite' });
    const recBtn = el('button', { type: 'button', class: 'btn-secondary' }, [railIcon(MIC_ICON), document.createTextNode(` ${t('voices.record')}`)]);
    const upBtn = el('button', { type: 'button', class: 'btn-secondary' }, [railIcon(RAIL_ICONS.upload), document.createTextNode(` ${t('voices.upload')}`)]);
    const file = el('input', { type: 'file', accept: 'audio/*,.wav,.mp3,.m4a,.ogg,.opus,.webm,.flac,.aac,.mp4', hidden: '' });
    const start = el('div', { class: 'dev-row dev-row-block' }, [
      el('span', { class: 'dev-row-text' }, [
        el('strong', { text: t('voices.add_title') }),
        el('span', { class: 'dev-row-hint', text: t('voices.add_hint') }),
      ]),
      el('div', { class: 'voices-actions' }, [recBtn, upBtn, file]),
    ]);
    const recPane = el('div', { class: 'dev-row dev-row-block voices-rec', hidden: '' });
    const review = el('div', { class: 'dev-row dev-row-block voices-review', hidden: '' });
    body.replaceChildren(start, recPane, review, el('div', { class: 'dev-row dev-row-block voices-status-row' }, [status]));

    const say = (msg, isErr = false) => { status.textContent = msg || ''; status.classList.toggle('err', !!isErr); };

    upBtn.addEventListener('click', () => file.click());
    file.addEventListener('change', () => { const f = file.files && file.files[0]; if (f) useClip(f, f.name); });

    recBtn.addEventListener('click', () => {
      recPane.hidden = false; review.hidden = true;
      const timer = el('span', { class: 'voices-rec-time', 'aria-live': 'off' });
      const go = el('button', { type: 'button', class: 'btn-primary', text: t('voices.rec_start') });
      const halt = el('button', { type: 'button', class: 'btn-secondary', text: t('voices.rec_stop'), disabled: '' });
      recPane.replaceChildren(
        el('span', { class: 'dev-row-text' }, [
          el('strong', { text: t('voices.rec_title') }),
          el('span', { class: 'dev-row-hint', text: t('voices.rec_hint') }),
        ]),
        el('blockquote', { class: 'voices-script', text: t('voices.script') }),
        el('div', { class: 'voices-actions' }, [go, halt, timer]),
      );
      go.addEventListener('click', async () => {
        try {
          const stream = await navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 },
          });
          const chunks = [];
          recorder = new MediaRecorder(stream);
          recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
          recorder.onstop = () => {
            stream.getTracks().forEach((tr) => tr.stop());
            clearInterval(recTimer); recTimer = null;
            go.disabled = false; halt.disabled = true;
            timer.textContent = '';
            const discard = recorder && recorder._discard;
            const type = (recorder && recorder.mimeType) || 'audio/webm';
            recorder = null;
            if (discard) return;
            recPane.hidden = true;
            useClip(new Blob(chunks, { type }), `recording.${type.includes('ogg') ? 'ogg' : 'webm'}`);
          };
          recorder.start();
          const t0 = Date.now();
          go.disabled = true; halt.disabled = false;
          const tick = () => {
            const s = Math.round((Date.now() - t0) / 1000);
            timer.textContent = t('voices.rec_seconds', { s });
            if (s >= RECORD_LIMIT_S) stopRecording(false);
          };
          tick();
          recTimer = setInterval(tick, 500);
          halt.focus();
        } catch {
          say(t('drive.err_mic_denied'), true);
        }
      });
      halt.addEventListener('click', () => stopRecording(false));
      go.focus();
    });

    async function useClip(blob, name) {
      clip = { blob, name };
      analysis = null;
      review.hidden = false;
      const player = el('audio', { controls: '', src: URL.createObjectURL(blob), class: 'voices-clip' });
      const chips = el('div', { class: 'voices-chips' });
      const nameIn = el('input', { type: 'text', class: 'voices-name', maxlength: '60', autocomplete: 'off', placeholder: t('voices.name_ph'), 'aria-label': t('voices.name') });
      const consent = el('fieldset', { class: 'voices-consent' }, [el('legend', { text: t('voices.consent') })]);
      const note = el('input', { type: 'text', class: 'voices-name', maxlength: '300', autocomplete: 'off', placeholder: t('voices.note_ph'), 'aria-label': t('voices.note') });
      const build = el('button', { type: 'button', class: 'btn-primary', text: t('voices.build'), disabled: '' });
      const discard = el('button', { type: 'button', class: 'btn-secondary', text: t('common.discard') });
      const kinds = [['self', 'voices.consent_self'], ['permission', 'voices.consent_permission'], ['synthetic', 'voices.consent_synthetic']];
      for (const [value, key] of kinds) {
        const r = el('input', { type: 'radio', name: 'voices-consent', value });
        r.addEventListener('change', update);
        consent.append(el('label', { class: 'voices-consent-opt' }, [r, el('span', { text: t(key) })]));
      }
      review.replaceChildren(
        el('span', { class: 'dev-row-text' }, [el('strong', { text: t('voices.review') })]),
        player, chips,
        el('label', { class: 'voices-field' }, [el('span', { text: t('voices.name') }), nameIn]),
        consent,
        el('label', { class: 'voices-field' }, [el('span', { text: t('voices.note') }), note]),
        el('div', { class: 'voices-actions' }, [build, discard]),
      );
      nameIn.addEventListener('input', update);
      function consentValue() { const c = consent.querySelector('input:checked'); return c ? c.value : ''; }
      function update() { build.disabled = !canBuild({ clip, analysis, name: nameIn.value, consent: consentValue() }); }
      discard.addEventListener('click', () => { clip = null; analysis = null; review.hidden = true; file.value = ''; say(''); });
      build.addEventListener('click', async () => {
        build.disabled = true;
        say(t('voices.building'));
        const fd = new FormData();
        fd.append('file', clip.blob, clip.name);
        fd.append('name', nameIn.value.trim());
        fd.append('consent', consentValue());
        fd.append('consent_note', note.value.trim());
        try {
          const p = await voiceJson(API, { method: 'POST', body: fd });
          say(t('voices.built', { name: p.name }));
          clip = null; analysis = null; review.hidden = true; file.value = '';
          await refresh();
          ctx.onChanged && ctx.onChanged();
        } catch (e) {
          say(e.message, true);
          update();
        }
      });
      say(t('voices.checking'));
      const fd = new FormData();
      fd.append('file', blob, name);
      try {
        analysis = await voiceJson(`${API}/analyse`, { method: 'POST', body: fd });
        chips.replaceChildren(...(analysis.checks || []).map((c) => el('span', { class: chipClass(c.level), text: c.message })));
        say(analysis.ok ? t('voices.clip_ok') : t('voices.clip_bad'), !analysis.ok);
      } catch (e) {
        analysis = null;
        chips.replaceChildren();
        say(e.message, true);
      }
      update();
      if (analysis && analysis.ok) nameIn.focus();
    }
  }

  function stopRecording(discard) {
    if (recorder && recorder.state === 'recording') {
      recorder._discard = !!discard;
      recorder.stop();
    }
    clearInterval(recTimer); recTimer = null;
  }

  return { open, close, isOpen: () => !!host };
}
