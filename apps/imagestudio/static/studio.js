// Image Studio — the page (/apps/imagestudio/).
//
// One form, one grid. The server (apps/imagestudio/backend.py) owns the
// provider call and the key; this page only sends the request the form
// describes and shows what comes back. Pictures open in the SHELL's own
// lightbox (app-sdk lightbox(): zoom, pan, swipe, arrows, download, Esc,
// No-Image Mode) -- there is no second viewer here.
//
// No-Image Mode: with the shell's flag set (localStorage 'dispatch-nim') the
// grid shows labelled placeholders and never creates an <img>, so nothing is
// fetched; the lightbox refuses too.

import {
  api, t, ready, theme, toast, lightbox, el, railIcon, RAIL_ICONS,
} from '/static/js/app-sdk.js?v=4';

/** A 403: this profile may not use Image Studio (4.0; 3.x sent a Safe-Mode DecoyError). */
const refused = (e) => !!e && e.status === 403;
import {
  PROMPT_MAX, isGenerateShortcut, shortcutLabel, promptStatus, canGenerate, clampCount,
  buildRequest, cropRect, defaultCropPos, galleryItems, isMetered,
  SORTS, normalizeSort, sortRows,
} from './studio-logic.js?v=2';
import { installExpiredImages } from '/static/js/media-expired.js?v=1';

export const API = '/api/apps/imagestudio';

const S = {
  info: null,            // GET /state
  images: [],            // GET /images, oldest -> newest
  busy: false,
  reference: null,       // {kind, ..., preview}
  status: '',
  statusError: false,
  nim: false,
  sort: 'oldest',        // the grid's order (SORTS), remembered per device
};
const R = {};            // DOM refs
const SORT_LABELS = { oldest: 'sort.oldest', newest: 'sort.newest' };
// The grid's order on this device (privacy.js APP_KEYS wipes it in privacy mode).
const SORT_KEY = 'dispatch-imagestudio-sort';

function nimOn() {
  try { return localStorage.getItem('dispatch-nim') === '1'; } catch { return false; }
}

function loadSort() {
  try { return normalizeSort(localStorage.getItem(SORT_KEY)); } catch { return 'oldest'; }
}

function saveSort(value) {
  try { localStorage.setItem(SORT_KEY, value); } catch { /* private window: this tab only */ }
}

/** The pictures in the order the grid shows them. */
function shown() { return sortRows(S.images, S.sort); }

function provider() {
  const id = R.provider ? R.provider.value : '';
  return (S.info && S.info.providers.find((p) => p.id === id)) || null;
}

function caps() {
  const p = provider();
  return (p && p.supports) || { reference: false, optimizer: false, seed: false, max_n: 1 };
}

function setStatus(text, isError = false) {
  S.status = text; S.statusError = isError;
  if (R.status) {
    R.status.textContent = text;
    R.status.classList.toggle('st-error', !!isError);
  }
}

function formState() {
  return {
    prompt: R.prompt.value, provider: R.provider.value, model: R.model.value, ratio: R.ratio.value,
    n: R.count.value, optimizer: R.optimizer.checked, seed: R.seed.value, reference: S.reference ? S.reference.ref : null,
  };
}

// ---------------------------------------------------------------- form

function refreshForm() {
  const p = provider();
  const c = caps();
  const st = promptStatus(R.prompt.value, S.info ? S.info.prompt_max : PROMPT_MAX);
  R.counter.textContent = t('prompt.counter', { n: st.n, max: st.max });
  R.counter.classList.toggle('st-over', st.over);
  R.count.max = String(c.max_n);
  if (Number(R.count.value) > c.max_n) R.count.value = String(c.max_n);
  R.optimizerRow.hidden = !c.optimizer;
  R.seedRow.hidden = !c.seed;
  R.refBox.hidden = !c.reference;
  R.go.disabled = !canGenerate({ prompt: R.prompt.value, busy: S.busy, provider: p && p.configured ? p.id : '' });
  R.go.textContent = S.busy ? t('generating') : t('generate');
  R.go.title = t('generate.tip', { keys: shortcutLabel(navigator.platform || '') });
  R.go.setAttribute('aria-busy', S.busy ? 'true' : 'false');
  R.cost.hidden = !isMetered(p);
  if (p && isMetered(p)) R.cost.textContent = t('cost.note', { provider: p.label, note: p.price_note });
  R.model.placeholder = p ? p.model : '';
  R.models.replaceChildren(...((p && p.models) || []).map((m) => el('option', { value: m })));
}

/** Ask the shell to open Settings on `target` ('section#anchor'). The shell checks
 *  the target and the session; a page outside the shell does nothing. */
function openShellSettings(target) {
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({ type: 'dispatch:open-settings', target }, location.origin);
    }
  } catch { /* not framed */ }
}

function renderProviders() {
  const { providers, default: def } = S.info;
  R.provider.replaceChildren(...providers.map((p) => el('option', {
    value: p.id,
    text: p.configured ? p.label : t('provider.no_key', { name: p.label }),
    ...(p.configured ? {} : { disabled: '' }),
  })));
  if (def) R.provider.value = def;
  R.none.hidden = !!def;
  R.setup.hidden = !S.info.can_configure;
  R.ask.hidden = !!S.info.can_configure;
  R.form.hidden = !def;
}

function renderRatios() {
  R.ratio.replaceChildren(...S.info.ratios.map((r) => el('option', { value: r, text: r })));
  R.ratio.value = '1:1';
}

function renderRecent() {
  const list = S.info.prompts || [];
  R.recent.replaceChildren();
  R.recentWrap.hidden = false;
  if (!list.length) {
    R.recent.append(el('li', { class: 'st-muted', text: t('prompt.recent_empty') }));
    return;
  }
  for (const p of list) {
    const b = el('button', { type: 'button', class: 'st-recent', title: p, text: p.length > 140 ? `${p.slice(0, 140)}…` : p });
    b.addEventListener('click', () => { R.prompt.value = p; refreshForm(); R.prompt.focus(); });
    R.recent.append(el('li', {}, [b]));
  }
}

function renderToday() {
  R.today.textContent = t('today.count', { n: S.info.today_count });
  R.today.title = (S.info.days || []).map((d) => `${d.day}: ${d.count}`).join('\n');
}

// ---------------------------------------------------------------- reference

function setReference(ref) {
  S.reference = ref;
  R.refPreview.replaceChildren();
  if (ref) {
    R.refPreview.append(S.nim ? el('span', { class: 'st-muted', text: t('nim.hidden') })
      : el('img', { src: ref.preview, alt: t('ref.label') }));
  }
  R.refClear.hidden = !ref;
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result));
    fr.onerror = () => reject(new Error('read'));
    fr.readAsDataURL(file);
  });
}

async function onUpload() {
  const f = R.file.files && R.file.files[0];
  R.file.value = '';
  if (!f) return;
  if (!/^image\//.test(f.type)) { setStatus(t('ref.not_image'), true); return; }
  if (f.size > 20 * 1024 * 1024) { setStatus(t('ref.too_big'), true); return; }
  try {
    const data = await readFileAsDataUrl(f);
    setReference({ ref: { kind: 'data_url', data }, preview: data });
  } catch { setStatus(t('ref.not_image'), true); }
}

async function openChatPicker() {
  R.picker.hidden = false;
  R.pickerGrid.replaceChildren(el('p', { class: 'st-muted', text: t('loading') }));
  try {
    const r = await api(`${API}/chat-images`);
    R.pickerGrid.replaceChildren();
    if (!r.images.length) { R.pickerGrid.append(el('p', { class: 'st-muted', text: t('ref.none_in_chat') })); return; }
    for (const im of r.images) {
      const b = el('button', { type: 'button', class: 'st-pick', title: im.thread || '' });
      if (S.nim) b.append(el('span', { class: 'st-muted', text: t('nim.hidden') }));
      else b.append(el('img', { src: im.url, alt: im.thread || t('ref.label'), loading: 'lazy' }));
      b.addEventListener('click', () => {
        setReference({ ref: { kind: 'media', url: im.url }, preview: im.url });
        R.picker.hidden = true;
      });
      R.pickerGrid.append(b);
    }
  } catch (e) { R.pickerGrid.replaceChildren(el('p', { class: 'st-error', text: e.message })); }
}

// ---------------------------------------------------------------- results

function tileMeta(r) {
  const bits = [r.model, r.ratio];
  if (r.seed != null) bits.push(t('tile.seed', { n: r.seed }));
  if (r.reference) bits.push(t('tile.ref_used'));
  return bits.filter(Boolean).join(' · ');
}

function iconBtn(icon, label, onClick) {
  const b = el('button', { type: 'button', class: 'st-icon', title: label, 'aria-label': label }, [railIcon(icon)]);
  b.addEventListener('click', onClick);
  return b;
}

function renderGrid() {
  R.grid.replaceChildren();
  R.empty.hidden = S.images.length > 0;
  R.sortWrap.hidden = S.images.length < 2;
  shown().forEach((r, i) => {
    const media = S.nim
      ? el('div', { class: 'st-noimg', text: t('nim.hidden') })
      : el('img', { src: r.url, alt: r.prompt, loading: 'lazy' });
    const open = el('button', { type: 'button', class: 'st-open', title: t('tile.open'), 'aria-label': t('tile.open') }, [media]);
    open.addEventListener('click', () => openFull(i));
    const dl = el('a', { class: 'st-icon', href: r.url, download: '', title: t('tile.download'), 'aria-label': t('tile.download') }, [railIcon(RAIL_ICONS['viewer-download'])]);
    const actions = el('div', { class: 'st-actions' }, [
      dl,
      iconBtn(RAIL_ICONS.copy, t('tile.copy'), () => copyPrompt(r)),
      iconBtn(RAIL_ICONS.attach, t('tile.reference'), () => useAsReference(r)),
      iconBtn(RAIL_ICONS.send, t('tile.send'), () => openSend(r)),
      iconBtn(RAIL_ICONS.user, t('tile.avatar'), () => openAvatar(r)),
    ]);
    if (!caps().reference) actions.children[2].hidden = true;
    // Send posts through the inject route: only for a profile the server lets do that.
    if (S.info && S.info.can_send === false) actions.children[3].hidden = true;
    if (S.info && S.info.can_clear) {
      const clear = iconBtn(RAIL_ICONS.trash, t('tile.clear'), () => openClear(r));
      clear.classList.add('st-icon-danger');
      actions.append(clear);
    }
    R.grid.append(el('li', { class: 'st-tile', dataset: { id: r.id } }, [
      open, actions,
      el('div', { class: 'st-tilemeta' }, [
        el('div', { class: 'st-tileprompt', title: r.prompt, text: r.prompt }),
        el('div', { class: 'st-muted', text: tileMeta(r) }),
      ]),
    ]));
  });
}

function openFull(index) {
  if (S.nim) { setStatus(t('nim.hidden')); return; }
  lightbox(galleryItems(shown(), { source: t('title') }), index);
}

async function copyPrompt(r) {
  try {
    await navigator.clipboard.writeText(r.prompt);
  } catch {
    const ta = el('textarea', { class: 'st-sr' });
    ta.value = r.prompt; document.body.append(ta); ta.select();
    try { document.execCommand('copy'); } catch { /* best effort */ }
    ta.remove();
  }
  setStatus(t('tile.copied'));
  toast(t('tile.copied'));
}

function useAsReference(r) {
  if (!caps().reference) return;
  setReference({ ref: { kind: 'result', id: r.id }, preview: r.url });
  setStatus(t('tile.reference_set'));
  R.refBox.scrollIntoView?.({ block: 'nearest' });
}

// ---------------------------------------------------------------- dialogs

function closeDialog() {
  R.dlg.hidden = true;
  R.dlg.replaceChildren();
}

function dialog(title, body, actions) {
  R.dlg.replaceChildren(el('div', { class: 'st-dlg', role: 'dialog', 'aria-modal': 'true', 'aria-label': title }, [
    el('h2', { text: title }), ...body, el('div', { class: 'st-dlg-actions' }, actions),
  ]));
  R.dlg.hidden = false;
  const first = R.dlg.querySelector('select, input, button');
  if (first) first.focus();
}

async function openSend(r) {
  let targets;
  try { targets = await api(`${API}/targets`); } catch (e) { setStatus(e.message, true); return; }
  if (!targets.threads.length) { setStatus(t('send.no_threads'), true); return; }
  const thread = el('select', { id: 'st-send-thread' }, targets.threads.map((th) => el('option', {
    value: th.id, text: `${th.bot} — ${th.title || t('send.untitled')}`,
  })));
  const asBot = el('input', { type: 'radio', name: 'st-as', value: 'assistant', checked: '' });
  const asSys = el('input', { type: 'radio', name: 'st-as', value: 'system' });
  const cap = el('input', { type: 'text', maxlength: '500', id: 'st-send-caption' });
  const go = el('button', { type: 'button', class: 'st-btn st-primary', text: t('send.go') });
  const cancel = el('button', { type: 'button', class: 'st-btn', text: t('common.cancel') });
  cancel.addEventListener('click', closeDialog);
  go.addEventListener('click', async () => {
    go.disabled = true;
    const th = targets.threads.find((x) => x.id === thread.value);
    try {
      await api('/api/inject', { method: 'POST', body: {
        thread_id: thread.value, role: asSys.checked ? 'system' : 'assistant',
        content: cap.value.trim(), media_url: r.url,
        metadata: { source: 'image-studio', image_id: r.id },
      } });
      closeDialog();
      setStatus(t('send.done', { title: (th && (th.title || th.bot)) || '' }));
      toast(t('send.done', { title: (th && (th.title || th.bot)) || '' }));
    } catch (e) { go.disabled = false; setStatus(t('send.failed', { error: e.message }), true); }
  });
  dialog(t('send.title'), [
    el('label', { for: 'st-send-thread', text: t('send.thread') }), thread,
    el('fieldset', { class: 'st-radios' }, [
      el('legend', { text: t('send.as') }),
      el('label', {}, [asBot, ` ${t('send.as_bot')}`]),
      el('label', {}, [asSys, ` ${t('send.as_system')}`]),
    ]),
    el('label', { for: 'st-send-caption', text: t('send.caption') }), cap,
  ], [cancel, go]);
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const im = new Image();
    im.onload = () => resolve(im);
    im.onerror = () => reject(new Error('image'));
    im.src = url;
  });
}

async function openAvatar(r) {
  let targets;
  try { targets = await api(`${API}/targets`); } catch (e) { setStatus(e.message, true); return; }
  if (S.nim) { setStatus(t('nim.hidden')); return; }
  if (!targets.bots.length) { setStatus(t('avatar.no_bots'), true); return; }
  let im;
  try { im = await loadImage(r.url); } catch { setStatus(t('avatar.failed', { error: 'image' }), true); return; }
  const bot = el('select', { id: 'st-av-bot' }, targets.bots.map((b) => el('option', { value: b.id, text: b.name || b.id })));
  const pos = el('input', { type: 'range', min: '0', max: '100', value: String(Math.round(defaultCropPos(im.naturalWidth, im.naturalHeight) * 100)), id: 'st-av-pos' });
  const canvas = el('canvas', { width: '160', height: '160', class: 'st-av-prev', 'aria-label': t('avatar.preview') });
  const draw = (c, px) => {
    const rc = cropRect(im.naturalWidth, im.naturalHeight, Number(pos.value) / 100);
    c.width = px; c.height = px;
    c.getContext('2d').drawImage(im, rc.sx, rc.sy, rc.size, rc.size, 0, 0, px, px);
  };
  const redraw = () => draw(canvas, 160);
  pos.addEventListener('input', redraw);
  redraw();
  const go = el('button', { type: 'button', class: 'st-btn st-primary', text: t('avatar.go') });
  const cancel = el('button', { type: 'button', class: 'st-btn', text: t('common.cancel') });
  cancel.addEventListener('click', closeDialog);
  go.addEventListener('click', async () => {
    go.disabled = true;
    try {
      const full = await (await fetch(r.url, { credentials: 'same-origin' })).blob();
      const face = el('canvas');
      draw(face, Math.min(512, Math.min(im.naturalWidth, im.naturalHeight)));
      const faceBlob = await new Promise((res) => face.toBlob(res, 'image/png'));
      const fd = new FormData();
      fd.append('file', full, `studio-${r.id.slice(0, 8)}.${/\.jpe?g$/i.test(r.url) ? 'jpg' : 'png'}`);
      const pick = targets.bots.find((b) => b.id === bot.value) || {};
      let url;
      if (pick.personal) {
        // The caller's own bot: its route takes the square as fractions and crops itself.
        const rc = cropRect(im.naturalWidth, im.naturalHeight, Number(pos.value) / 100);
        const q = new URLSearchParams({ crop_x: rc.sx / im.naturalWidth, crop_y: rc.sy / im.naturalHeight,
          crop_size: rc.size / Math.min(im.naturalWidth, im.naturalHeight) });
        url = `/api/me/characters/${encodeURIComponent(bot.value)}/avatar?${q}`;
      } else {
        fd.append('face', faceBlob, `studio-${r.id.slice(0, 8)}-face.png`);
        url = `/api/bots/${encodeURIComponent(bot.value)}/avatar`;
      }
      const res = await fetch(url, { method: 'POST', body: fd, credentials: 'same-origin' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const name = (targets.bots.find((b) => b.id === bot.value) || {}).name || bot.value;
      closeDialog();
      setStatus(t('avatar.done', { name }));
      toast(t('avatar.done', { name }));
    } catch (e) { go.disabled = false; setStatus(t('avatar.failed', { error: e.message }), true); }
  });
  dialog(t('avatar.title'), [
    el('label', { for: 'st-av-bot', text: t('avatar.bot') }), bot,
    el('div', { class: 'st-av-row' }, [canvas, el('div', {}, [
      el('label', { for: 'st-av-pos', text: t('avatar.crop') }), pos,
      el('p', { class: 'st-muted', text: t('avatar.note') }),
    ])]),
  ], [cancel, go]);
}

// ---------------------------------------------------------------- clear from here down
//
// The File Server's "Delete this & older" for the studio: this picture and every
// picture after it in the grid's current order. The server counts first (dry run,
// so the number covers pictures beyond the grid's last 60 too), the person
// confirms in a danger dialog, then the same request runs for real.

async function openClear(r) {
  let plan;
  try {
    plan = await api(`${API}/clear`, { method: 'POST', body: { id: r.id, order: S.sort, dry_run: true } });
  } catch (e) { setStatus(t('clear.failed', { error: e.message }), true); return; }
  const go = el('button', { type: 'button', class: 'st-btn st-danger', text: t('clear.go') });
  const cancel = el('button', { type: 'button', class: 'st-btn', text: t('common.cancel') });
  cancel.addEventListener('click', closeDialog);
  go.addEventListener('click', async () => {
    go.disabled = true;
    try {
      const res = await api(`${API}/clear`, { method: 'POST', body: { id: r.id, order: S.sort, expect: plan.count } });
      closeDialog();
      S.images = (await api(`${API}/images`)).images;
      await reloadState(false);
      renderGrid();
      setStatus(t('clear.done', { n: res.deleted }));
      toast(t('clear.done', { n: res.deleted }));
    } catch (e) {
      // A picture came or went since the count (another tab): nothing was
      // deleted; count again and ask again rather than delete what was not seen.
      // The route's other 409 (a generation is running) is not a recount: it
      // falls through to the server's own message below.
      const code = e && e.body && e.body.error && e.body.error.code;
      if (code === 'imagestudio_pictures_changed') {
        closeDialog();
        setStatus(t('clear.changed'));
        try {
          S.images = (await api(`${API}/images`)).images;
        } catch (e2) { setStatus(t('clear.failed', { error: e2.message }), true); return; }
        renderGrid();
        openClear(r);
        return;
      }
      go.disabled = false; setStatus(t('clear.failed', { error: e.message }), true);
    }
  });
  const body = [el('p', { class: 'st-clear-msg', text: t(S.sort === 'newest' ? 'clear.confirm_newest' : 'clear.confirm_oldest', { n: plan.count }) })];
  if (plan.in_chat) body.push(el('p', { class: 'st-muted', text: t('clear.in_chat', { n: plan.in_chat }) }));
  dialog(t('clear.title'), body, [cancel, go]);
  cancel.focus();
}

// ---------------------------------------------------------------- generate

async function generate() {
  const p = provider();
  if (!p || !canGenerate({ prompt: R.prompt.value, busy: S.busy, provider: p.configured ? p.id : '' })) return;
  S.busy = true; refreshForm();
  setStatus(t('generating_note'));
  try {
    const body = buildRequest(formState(), caps());
    const r = await api(`${API}/generate`, { method: 'POST', body });
    S.images = S.images.concat(r.images);
    await reloadState(false);
    renderGrid();
    setStatus(r.images.length < r.asked
      ? t('results.partial', { got: r.images.length, asked: r.asked })
      : t('results.done', { n: r.images.length }));
    (S.sort === 'newest' ? R.grid.firstElementChild : R.grid.lastElementChild)?.scrollIntoView?.({ block: 'nearest' });
  } catch (e) {
    setStatus(refused(e) ? t('locked') : e.message, true);
  } finally {
    S.busy = false; refreshForm();
  }
}

async function reloadState(rebuildProviders = true) {
  S.info = await api(`${API}/state`);
  if (rebuildProviders) renderProviders();
  renderToday(); renderRecent(); refreshForm();
}

// ---------------------------------------------------------------- build

function field(label, control, extra) {
  return el('div', { class: 'st-field' }, [el('label', { text: label }), control, extra].filter(Boolean));
}

function build(root) {
  R.prompt = el('textarea', { id: 'st-prompt', rows: '6', placeholder: t('prompt.placeholder'), 'aria-label': t('prompt.label') });
  R.counter = el('div', { class: 'st-counter', 'aria-live': 'polite' });
  R.recent = el('ul', { class: 'st-recents' });
  R.recentWrap = el('details', { class: 'st-recentwrap' }, [el('summary', { text: t('prompt.recent') }), R.recent]);
  R.provider = el('select', { id: 'st-provider' });
  R.model = el('input', { type: 'text', id: 'st-model', list: 'st-models', autocomplete: 'off', spellcheck: 'false' });
  R.models = el('datalist', { id: 'st-models' });
  R.ratio = el('select', { id: 'st-ratio' });
  R.count = el('input', { type: 'number', id: 'st-count', min: '1', max: '9', value: '1', inputmode: 'numeric' });
  R.optimizer = el('input', { type: 'checkbox', id: 'st-optimizer' });
  R.optimizerRow = el('div', { class: 'st-check' }, [R.optimizer, el('label', { for: 'st-optimizer', text: t('optimizer.label') }), el('div', { class: 'st-muted', text: t('optimizer.hint') })]);
  R.seed = el('input', { type: 'text', id: 'st-seed', inputmode: 'numeric', placeholder: t('seed.placeholder'), autocomplete: 'off' });
  R.seedRow = field(t('seed.label'), R.seed);
  R.file = el('input', { type: 'file', accept: 'image/*', class: 'st-sr', id: 'st-file', tabindex: '-1' });
  R.refPreview = el('div', { class: 'st-refprev' });
  R.refClear = el('button', { type: 'button', class: 'st-btn', text: t('ref.remove'), hidden: '' });
  const refUp = el('button', { type: 'button', class: 'st-btn', text: t('ref.upload') });
  const refChat = el('button', { type: 'button', class: 'st-btn', text: t('ref.from_chat') });
  R.pickerGrid = el('div', { class: 'st-pickgrid' });
  const pickerClose = el('button', { type: 'button', class: 'st-btn', text: t('common.close') });
  R.picker = el('div', { class: 'st-picker', hidden: '' }, [el('strong', { text: t('ref.pick_title') }), R.pickerGrid, pickerClose]);
  R.refBox = el('div', { class: 'st-field st-ref' }, [
    el('label', { text: t('ref.label') }), el('div', { class: 'st-muted', text: t('ref.hint') }),
    R.refPreview, el('div', { class: 'st-row' }, [refUp, refChat, R.refClear]), R.file, R.picker,
  ]);
  R.go = el('button', { type: 'button', class: 'st-btn st-primary st-go', id: 'st-generate', text: t('generate') });
  R.cost = el('p', { class: 'st-cost', hidden: '' });
  R.status = el('p', { class: 'st-status', role: 'status', 'aria-live': 'polite' });
  R.today = el('span', { class: 'st-today' });
  // No provider yet: say so, and (for a profile that may change it) open the
  // shell's Settings -> AI & Connections -> Image generation from here (3.2).
  R.setup = el('button', { type: 'button', class: 'st-btn st-primary', hidden: '', text: t('provider.setup') });
  R.setup.addEventListener('click', () => openShellSettings('connections#images'));
  R.ask = el('p', { class: 'st-muted', hidden: '', text: t('provider.ask_owner') });
  R.none = el('div', { class: 'st-none', hidden: '' }, [
    el('p', { text: t('provider.none') }),
    R.setup, R.ask,
  ]);
  R.form = el('div', { class: 'st-form' }, [
    field(t('prompt.label'), R.prompt, R.counter), R.recentWrap,
    el('div', { class: 'st-two' }, [field(t('provider.label'), R.provider), field(t('model.label'), R.model, R.models)]),
    el('div', { class: 'st-two' }, [field(t('ratio.label'), R.ratio), field(t('count.label'), R.count)]),
    R.optimizerRow, R.seedRow, R.refBox, R.go, R.cost,
  ]);
  R.sort = el('select', { id: 'st-sort', class: 'st-sort' }, SORTS.map((v) => el('option', { value: v, text: t(SORT_LABELS[v]) })));
  R.sort.value = S.sort;
  R.sortWrap = el('label', { class: 'st-sortwrap', for: 'st-sort' }, [`${t('sort.label')}: `, R.sort]);
  R.grid = el('ul', { class: 'st-grid' });
  R.empty = el('p', { class: 'st-muted st-emptynote', text: t('results.empty') });
  R.dlg = el('div', { class: 'st-dlgwrap', hidden: '' });
  root.append(
    el('header', { class: 'st-head' }, [R.today]),
    el('div', { class: 'st-layout' }, [
      el('section', { class: 'st-controls' }, [R.none, R.form, R.status]),
      el('section', { class: 'st-results' }, [
        el('div', { class: 'st-resultshead' }, [el('h2', { text: t('results.title') }), R.sortWrap]),
        R.empty, R.grid]),
    ]),
    R.dlg,
  );

  R.prompt.addEventListener('input', refreshForm);
  // Ctrl+Enter / Cmd+Enter = the Generate button, and only while it is enabled.
  R.prompt.addEventListener('keydown', (ev) => {
    if (!isGenerateShortcut(ev)) return;
    ev.preventDefault();
    if (!R.go.disabled) generate();
  });
  R.provider.addEventListener('change', refreshForm);
  R.count.addEventListener('change', () => { R.count.value = String(clampCount(R.count.value, caps().max_n)); });
  R.go.addEventListener('click', generate);
  R.sort.addEventListener('change', () => {
    S.sort = normalizeSort(R.sort.value);
    saveSort(S.sort);
    renderGrid();
  });
  refUp.addEventListener('click', () => R.file.click());
  R.file.addEventListener('change', onUpload);
  refChat.addEventListener('click', openChatPicker);
  pickerClose.addEventListener('click', () => { R.picker.hidden = true; });
  R.refClear.addEventListener('click', () => setReference(null));
  R.dlg.addEventListener('click', (e) => { if (e.target === R.dlg) closeDialog(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !R.dlg.hidden) closeDialog(); });
}

export async function mount(root = document.getElementById('st-root')) {
  theme();
  await ready();
  S.nim = nimOn();
  S.sort = loadSort();
  document.title = t('title');
  root.setAttribute('aria-label', t('title'));
  document.documentElement.lang = document.documentElement.lang || 'en';
  // A picture the image-retention setting removed answers 410 + X-Image-Expired: the tile
  // shows the "image expired" placeholder instead of a broken image (the shell's listener
  // does not reach into this frame).
  installExpiredImages({ t: () => t('expired'), el, icon: () => railIcon(RAIL_ICONS.nim) });
  build(root);
  try {
    await reloadState();
    renderRatios();
    S.images = (await api(`${API}/images`)).images;
    renderGrid();
    refreshForm();
  } catch (e) {
    root.replaceChildren(el('p', { class: 'st-locked', text: refused(e) ? t('locked') : e.message }));
  }
  document.body.classList.add('st-ready');
}

if (typeof document !== 'undefined' && document.getElementById('st-root') && !globalThis.__ST_NO_AUTOMOUNT) {
  mount();
}
