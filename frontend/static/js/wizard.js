// First-run wizard: six short steps that take a fresh install to a first working chat.
//
//   1 Your account       (POST /api/auth/setup: from this computer, or with the setup token in the log)
//   2 First connection   (look for StudioForge / LM Studio / Ollama, Test, save, point a character at it)
//   3 Voice              (optional; just a link to Settings -> Voice and what is installed)
//   4 People             (optional; User and Basic profiles; the shared DisPatch profile is made for you)
//   5 Network            (this computer only - the default - or the home network, with the HTTPS note)
//   6 Done               (a checklist of what is left)
//
// It appears only when the server says it is needed (GET /api/wizard: a fresh install, or one that
// is not yet set up) and can be dismissed for the session with "Set up later". Every call is the
// same one a script would make, so what the page does is what docs/first-run.md says an API client can.
//
// Pure helpers are exported for the tests; nothing runs at import time.

import { w5 as realApi, errorText } from './w5-api.js?v=2';
import { t } from './i18n.js?v=5';
import { el, spriteIcon } from './util.js?v=21';
import { describeTest } from './connections.js?v=3';

export const STEPS = ['owner', 'connection', 'voice', 'family', 'network', 'done'];
export const OPTIONAL = new Set(['voice', 'family']);
const STEP_KEYS = {
  owner: 'wiz.step_owner', connection: 'wiz.step_connection', voice: 'wiz.step_voice',
  family: 'wiz.step_family', network: 'wiz.step_network', done: 'wiz.step_done',
};
export const stepLabel = (s) => t(STEP_KEYS[s] || STEP_KEYS.done);
const LATER_KEY = 'dp-wizard-later';

// ------------------------------------------------------------------ pure

/** Where to start: the first required step that is not done (owner, connection, network), else 'done'. */
export function startStep(state) {
  const by = {};
  for (const s of (state && state.steps) || []) by[s.id] = s;
  if (!state || !state.claimed) return 'owner';
  if (by.connection && !by.connection.done) return 'connection';
  if (by.network && !by.network.done) return 'voice';
  return 'done';
}

/** Step after `step`, skipping steps already done when `skipDone` is set. */
export function nextStep(step, state, { skipDone = false } = {}) {
  const i = STEPS.indexOf(step);
  const by = {};
  for (const s of (state && state.steps) || []) by[s.id] = s;
  for (let k = i + 1; k < STEPS.length; k += 1) {
    const id = STEPS[k];
    if (skipDone && by[id] && by[id].done && id !== 'done') continue;
    return id;
  }
  return 'done';
}

/** The setup body for step 1. A password needs 8+ characters, a PIN 4+; at least one is required. */
export function ownerPayload({ name, password, pin, token }) {
  const o = {};
  if (name && name.trim()) o.name = name.trim();
  if (password) o.password = password;
  if (pin) o.new_pin = pin;
  if (token && token.trim()) o.setup_token = token.trim();
  return o;
}

export function ownerError({ password, pin, confirm }) {
  if (!password && !pin) return 'wiz.owner_need_one';
  if (password && password.length < 8) return 'wiz.owner_pw_short';
  if (password && password !== confirm) return 'wiz.owner_pw_mismatch';
  if (pin && pin.length < 4) return 'wiz.owner_pin_short';
  return '';
}

/** The people body: skip blank rows. A PIN is optional on both types. */
export function familyPayload(rows) {
  const people = rows.filter((r) => r.name && r.name.trim()).map((r) => ({
    name: r.name.trim(), role: r.role, ...(r.pin ? { pin: r.pin } : {}),
  }));
  return { people };
}

export const dismissedForNow = (storage = typeof sessionStorage === 'undefined' ? null : sessionStorage) => {
  try { return !!storage && storage.getItem(LATER_KEY) === '1'; } catch { return false; }
};

// ------------------------------------------------------------------ DOM

function field(label, input, hint) {
  if (!input.id) input.id = `wz-${Math.random().toString(36).slice(2, 8)}`;
  const kids = [el('label', { for: input.id, text: label }), input];
  if (hint) kids.push(el('span', { class: 'people-field-hint', text: hint }));
  return el('div', { class: 'sec-field people-field' }, kids);
}

/**
 * Open the wizard if it is needed. ctx: { api?, toast, openTab(tab), onFinished(), force? }.
 * Resolves with the overlay's controller ({ close }) or null when it is not needed / dismissed.
 */
export async function maybeStartWizard(ctx = {}) {
  const api = ctx.api || realApi;
  if (!ctx.force && dismissedForNow()) return null;
  let state;
  try { state = await api.wizard(); } catch { return null; }
  if (!ctx.force && !state.needed) return null;
  return openWizard(state, ctx);
}

export function openWizard(initial, ctx = {}) {
  const api = ctx.api || realApi;
  const toast = ctx.toast || (() => {});
  let state = initial;
  let step = startStep(state);
  const overlay = el('div', { class: 'modal-backdrop wizard-backdrop', role: 'presentation' });
  const dialog = el('div', { class: 'modal wizard', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'wizard-title' });
  const progress = el('ol', { class: 'wizard-steps', 'aria-label': t('wiz.steps_aria') });
  const content = el('div', { class: 'wizard-content' });
  const err = el('p', { class: 'lock-error', role: 'alert', hidden: '' });
  const later = el('button', { type: 'button', class: 'btn-secondary', text: t('wiz.later') });
  dialog.append(el('header', { class: 'modal-header' }, [el('h2', { id: 'wizard-title', text: t('wiz.title') })]), progress, content, err,
    el('footer', { class: 'modal-footer' }, [later]));
  overlay.append(dialog);
  document.body.append(overlay);

  const close = () => {
    overlay.remove();
    if (typeof ctx.onClosed === 'function') ctx.onClosed();
  };
  later.addEventListener('click', () => {
    try { sessionStorage.setItem(LATER_KEY, '1'); } catch { /* ignore */ }
    close();
  });
  overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') later.click(); });

  const showError = (e) => { err.textContent = typeof e === 'string' ? e : errorText(e); err.hidden = !err.textContent; };
  const refresh = async () => { try { state = await api.wizard(); } catch { /* keep the old state */ } };
  const go = async (to) => { err.hidden = true; step = to; await refresh(); paint(); };
  const next = (skip = false) => go(nextStep(step, state, { skipDone: skip }));

  // A step indicator (3.2): a numbered dot per step, the current one filled and
  // bold, finished ones ticked, optional ones quieter and saying so.
  function paintProgress() {
    progress.textContent = '';
    const by = {};
    for (const s of (state && state.steps) || []) by[s.id] = s;
    const at = STEPS.indexOf(step);
    STEPS.forEach((s, i) => {
      const done = i < at || (by[s] && by[s].done && s !== 'done');
      const cls = ['wizard-step'];
      if (s === step) cls.push('current');
      else if (done) cls.push('done');
      if (OPTIONAL.has(s)) cls.push('optional');
      progress.append(el('li', { class: cls.join(' '), 'aria-current': s === step ? 'step' : null }, [
        el('span', { class: 'wizard-dot', 'aria-hidden': 'true' }, [done && s !== step ? spriteIcon('check', 12) : String(i + 1)]),
        el('span', { class: 'wizard-step-label', text: stepLabel(s) }),
        OPTIONAL.has(s) ? el('span', { class: 'sr-only', text: ` (${t('wiz.optional')})` }) : null,
      ]));
    });
  }

  function actions(...buttons) { return el('div', { class: 'cxn-actions' }, buttons.filter(Boolean)); }
  const btn = (text, cls, fn) => { const b = el('button', { type: 'button', class: cls, text }); b.addEventListener('click', fn); return b; };

  function stepOwner() {
    if (state.claimed) {
      content.append(el('p', { text: t('wiz.owner_done') }), actions(btn(t('wiz.next'), 'btn-primary', () => next())));
      return;
    }
    const name = el('input', { type: 'text', class: 'ui-input', autocomplete: 'username', value: t('setup.default_name'), maxlength: '40' });
    const pw = el('input', { type: 'password', class: 'ui-input', autocomplete: 'new-password' });
    const pw2 = el('input', { type: 'password', class: 'ui-input', autocomplete: 'new-password' });
    const pin = el('input', { type: 'password', class: 'ui-input', inputmode: 'numeric', autocomplete: 'off' });
    const token = el('input', { type: 'text', class: 'ui-input', autocomplete: 'off', spellcheck: 'false' });
    const go1 = btn(t('wiz.owner_create'), 'btn-primary', async () => {
      const bad = ownerError({ password: pw.value, pin: pin.value, confirm: pw2.value });
      if (bad) { showError(t(bad)); return; }
      go1.disabled = true;
      try {
        await api.setup(ownerPayload({ name: name.value, password: pw.value, pin: pin.value, token: token.value }));
        // The page is signed in as the new Admin now: let the app learn who it is before the next step.
        if (typeof ctx.onAccountCreated === 'function') await ctx.onAccountCreated();
        await refresh();
        toast(t('wiz.owner_created'));
        await next();
      } catch (e) { showError(e); } finally { go1.disabled = false; }
    });
    content.append(el('p', { text: t('wiz.owner_intro') }), field(t('wiz.owner_name'), name), field(t('wiz.owner_password'), pw, t('wiz.owner_password_hint')),
      field(t('wiz.owner_password2'), pw2), field(t('wiz.owner_pin'), pin, t('wiz.owner_pin_hint')),
      el('details', { class: 'cxn-adv' }, [el('summary', { text: t('wiz.owner_token_title') }), field(t('wiz.owner_token'), token, t('wiz.owner_token_hint'))]),
      actions(go1));
  }

  async function stepConnection() {
    const status = el('p', { class: 'muted', role: 'status', text: t('wiz.searching') });
    const list = el('div', { class: 'cxn-found dev-group-body' });
    const manual = el('div', { class: 'cxn-manual' });
    content.append(el('p', { text: t('wiz.conn_intro') }), status, list, manual);
    const result = el('div', { class: 'cxn-result', role: 'status', hidden: '' });
    const useType = async (spec) => {
      showError('');
      result.hidden = false; result.className = 'cxn-result cxn-idle'; result.textContent = t('cxn.testing');
      try {
        const r = await api.wizardConnection(spec);
        const [kind, head, detail] = describeTest(r.test);
        result.className = `cxn-result cxn-${kind}`; result.textContent = detail ? `${head} · ${detail}` : head;
        if (r.test.ok) {
          if (r.assigned) result.append(' ', t('wiz.conn_assigned', { name: r.assigned.name || r.assigned.character }));
          list.textContent = '';
          content.append(actions(btn(t('wiz.next'), 'btn-primary', () => next())));
        }
      } catch (e) { showError(e); result.hidden = true; }
    };
    let found = [];
    try { found = (await api.wizardDetect({})).found; } catch (e) { showError(e); }
    status.textContent = found.length ? t('wiz.found', { count: found.length }) : t('wiz.found_none');
    for (const f of found) {
      list.append(el('div', { class: 'dev-row' }, [
        el('span', { class: 'dev-row-text' }, [el('strong', { text: f.product }), ' ', el('span', { class: 'dev-row-hint', text: f.base_url })]),
        btn(t('wiz.use_this'), 'btn-primary', () => useType({ type: f.type, base_url: f.base_url, name: f.product }))]));
    }
    const types = el('select', { class: 'ui-select' }, [
      ['openai_compatible', 'OpenAI-compatible'], ['studioforge', 'StudioForge'], ['lmstudio', 'LM Studio'], ['ollama', 'Ollama'],
      ['openai', 'OpenAI'], ['anthropic', 'Anthropic'], ['deepseek', 'DeepSeek'], ['openrouter', 'OpenRouter']]
      .map(([v, l]) => el('option', { value: v, text: l })));
    const url = el('input', { type: 'url', class: 'ui-input', placeholder: 'http://127.0.0.1:1234/v1', autocomplete: 'off' });
    const key = el('input', { type: 'password', class: 'ui-input', autocomplete: 'new-password' });
    const mdl = el('input', { type: 'text', class: 'ui-input', autocomplete: 'off' });
    manual.append(el('h3', { class: 'dev-group-title', text: t('wiz.conn_manual') }), field(t('cxn.type'), types), field(t('cxn.address'), url),
      field(t('cxn.key'), key), field(t('cxn.default_model'), mdl),
      actions(btn(t('wiz.conn_test_save'), 'btn-secondary', () => useType({
        type: types.value, base_url: url.value.trim(), api_key: key.value.trim(), model: mdl.value.trim() }))), result);
    content.append(actions(btn(t('wiz.skip_for_now'), 'btn-secondary', () => next())));
  }

  function stepVoice() {
    const open = btn(t('wiz.voice_open'), 'btn-secondary', () => { later.click(); ctx.openTab && ctx.openTab('voice'); });
    content.append(el('p', { text: t('wiz.voice_intro') }), actions(open,
      btn(t('wiz.skip'), 'btn-secondary', async () => { await api.wizardStep('voice', 'skipped'); await next(); }),
      btn(t('wiz.next'), 'btn-primary', () => next())));
  }

  function stepFamily() {
    const rows = [];
    const host = el('div', { class: 'wizard-people dev-group-body' });
    const addRow = (role) => {
      const name = el('input', { type: 'text', class: 'ui-input', maxlength: '40', 'aria-label': t('wiz.family_name') });
      const pin = el('input', { type: 'password', class: 'ui-input', inputmode: 'numeric', 'aria-label': t('wiz.family_pin') });
      const r = { role, name, pin };
      rows.push(r);
      host.append(el('div', { class: 'dev-row' }, [el('span', { class: 'ui-badge', text: t(`role.${role}`) }), name, pin]));
    };
    addRow('user');
    const go4 = btn(t('wiz.family_create'), 'btn-primary', async () => {
      go4.disabled = true;
      try {
        const r = await api.wizardFamily(familyPayload(rows.map((x) => ({ role: x.role, name: x.name.value, pin: x.pin.value }))));
        if (r && Array.isArray(r.created) && r.created.length) toast(t('wiz.family_done'));
        await next();
      } catch (e) { showError(e); } finally { go4.disabled = false; }
    });
    content.append(el('p', { text: t('wiz.family_intro') }), host,
      el('p', { class: 'muted', text: `${t('role.user')}: ${t('people.type_user_hint')} ${t('role.basic')}: ${t('people.type_basic_hint')}` }),
      btn(t('wiz.family_add_user'), 'btn-secondary', () => addRow('user')),
      btn(t('wiz.family_add_basic'), 'btn-secondary', () => addRow('basic')),
      actions(go4, btn(t('wiz.skip'), 'btn-secondary', async () => { await api.wizardStep('family', 'skipped'); await next(); })));
  }

  function stepNetwork() {
    const local = el('input', { type: 'radio', name: 'wz-exposure', id: 'wz-local', value: 'local' });
    const lan = el('input', { type: 'radio', name: 'wz-exposure', id: 'wz-lan', value: 'lan' });
    local.checked = true;
    const url = el('input', { type: 'url', class: 'ui-input', placeholder: 'https://chat.example.net', autocomplete: 'off' });
    const notes = el('ul', { class: 'wizard-notes' });
    const save = btn(t('wiz.network_save'), 'btn-primary', async () => {
      save.disabled = true;
      try {
        const r = await api.wizardNetwork({ exposure: lan.checked ? 'lan' : 'local', ...(url.value.trim() ? { public_url: url.value.trim(), behind_tls: url.value.trim().startsWith('https://') } : {}) });
        notes.textContent = '';
        r.notes.forEach((n) => notes.append(el('li', { text: n })));
        if (r.mismatch) notes.append(el('li', { class: 'lock-error', text: r.mismatch_note }));
        content.append(actions(btn(t('wiz.next'), 'btn-primary', () => next())));
        save.hidden = true;
      } catch (e) { showError(e); } finally { save.disabled = false; }
    });
    content.append(el('p', { text: t('wiz.network_intro') }),
      el('div', { class: 'wizard-choices', role: 'radiogroup', 'aria-label': t('wiz.step_network') }, [
        el('label', { class: 'wizard-choice', for: 'wz-local' }, [local, el('span', { text: t('wiz.network_local') })]),
        el('label', { class: 'wizard-choice', for: 'wz-lan' }, [lan, el('span', { text: t('wiz.network_lan') })]),
      ]),
      field(t('wiz.network_url'), url, t('wiz.network_url_hint')), notes, actions(save));
  }

  function stepDone() {
    const list = el('ul', { class: 'wizard-checklist' });
    for (const s of state.steps || []) {
      list.append(el('li', { class: s.done ? 'done' : '' }, [
        el('span', { 'aria-hidden': 'true' }, [s.done ? spriteIcon('check', 14) : (s.skipped ? '–' : '○'), ' ']),
        el('strong', { text: s.title }), s.hint && !s.done ? el('span', { class: 'muted', text: ` ${s.hint}` }) : null]));
    }
    content.append(el('p', { text: t('wiz.done_intro') }), list, actions(btn(t('wiz.finish'), 'btn-primary', async () => {
      try { await api.wizardFinish(); } catch (e) { showError(e); return; }
      close();
      ctx.onFinished && ctx.onFinished();
    })));
  }

  function paint() {
    paintProgress();
    content.textContent = '';
    const heading = el('h3', { class: 'dev-group-title', text: stepLabel(step) });
    content.append(heading);
    ({ owner: stepOwner, connection: stepConnection, voice: stepVoice, family: stepFamily, network: stepNetwork, done: stepDone })[step]();
    heading.setAttribute('tabindex', '-1');
    if (heading.focus) heading.focus();
  }

  paint();
  return { close, paint, get step() { return step; } };
}
