// Sign-in and first-run setup (DOM half; the decisions live in profiles.js).
//
//   renderSignIn   profile TILES and the credential prompt a tile asks for
//                  (password, PIN, both, or nothing), with the 429 countdown.
//   renderSetup    the form that creates the first Admin: full page when the install
//                  is reached from the network (needs the setup token printed in
//                  the server log), inline in Settings -> Account on the box.
//   attachChipMenu the rail chip and its Switch profile / Lock menu.
//
// Nothing here talks to the network: every credential goes out through the
// callback main.js passes in, which owns the session state.

import { t } from './i18n.js?v=5';
import { el } from './util.js?v=21';
import { nimEnabled } from './nim.js?v=6';
import {
  avatarSpec, createCountdown, credentialModes, credentialOptions, formatCountdown, sortTiles,
} from './profiles.js?v=4';

const roleLabel = (role) => t(`role.${role}`);

function avatar(profile, cls) {
  const spec = avatarSpec(profile, { noImages: nimEnabled() });
  if (spec.kind === 'img') {
    return el('img', { class: cls, src: spec.src, alt: '', width: '48', height: '48', draggable: 'false' });
  }
  return el('span', { class: `${cls} ${cls}--text`, 'aria-hidden': 'true', text: spec.text });
}

function errMessage(e, fallbackKey) {
  if (e && e.code === 'incorrect_credential') return t('signin.wrong');
  if (e && e.code === 'setup_forbidden') return t('setup.err_forbidden');
  const msg = String((e && e.message) || '').replace(/^\d{3}:\s*/, '');
  const text = msg || t(fallbackKey || 'signin.failed');
  return e && e.fix ? `${text} ${e.fix}` : text;
}

// ---- Sign in ---------------------------------------------------------------

/**
 * Fill `host` with the tiles (and, once one is picked, its credential form).
 *   profiles   status.profiles
 *   currentId  the profile already signed in here (switching), or null
 *   onSubmit   async (profile, {pin?, password?, remember?}) -> resolves on
 *              success, rejects with the api Error otherwise
 *   remember   () -> bool, whether the "keep this device signed in" box is on
 * Returns { reset(), pick(id), destroy() }.
 */
export function renderSignIn(host, { profiles, currentId = null, onSubmit, remember = () => false }) {
  let countdown = null;
  let busy = false;
  let picked = null;              // the tile whose credential form is up (null on the tile list)
  const tiles = sortTiles(profiles);

  const stopCountdown = () => { if (countdown) { countdown.stop(); countdown = null; } };

  function showTiles() {
    stopCountdown();
    picked = null;
    const list = el('ul', { class: 'signin-tiles', role: 'list' }, tiles.map((p) => {
      const b = el('button', {
        type: 'button', class: 'signin-tile', 'data-profile': p.id,
        'aria-current': p.id === currentId ? 'true' : null,
      }, [
        avatar(p, 'signin-avatar'),
        el('span', { class: 'signin-name', text: p.name }),
        el('span', { class: 'signin-role', text: roleLabel(p.role) }),
        p.protected ? el('span', { class: 'signin-badge', text: t('people.protected') }) : null,
        p.shared ? el('span', { class: 'signin-badge', text: t('profiles.shared_badge') }) : null,
      ]);
      b.addEventListener('click', () => pick(p));
      return el('li', {}, [b]);
    }));
    const err = el('div', { class: 'lock-error signin-error', id: 'signin-tiles-error', role: 'alert', 'aria-live': 'assertive', hidden: '' });
    host.replaceChildren(
      el('p', { class: 'signin-prompt', text: t('signin.who') }),
      list,
      err,
    );
    host._err = err;
    const first = list.querySelector('button');
    if (first) { try { first.focus(); } catch { /* not attached yet */ } }
  }

  async function submit(profile, opts, ui) {
    if (busy) return;
    busy = true;
    ui.setBusy(true);
    try {
      await onSubmit(profile, { ...opts, remember: !!remember() });
    } catch (e) {
      ui.clearSecret();
      if (e && e.status === 429 && e.retryAfter) {
        ui.wait(e.retryAfter);
      } else {
        ui.error(errMessage(e));
      }
    } finally {
      busy = false;
      ui.setBusy(false);
    }
  }

  function pick(profileOrId) {
    const profile = typeof profileOrId === 'string' ? tiles.find((p) => p.id === profileOrId) : profileOrId;
    if (!profile) return;
    const modes = credentialModes(profile.credential);
    picked = modes.length ? profile : null;
    if (!modes.length) {
      // Nothing to type: picking the tile IS the sign-in.
      const ui = {
        setBusy: (b) => host.querySelectorAll('.signin-tile').forEach((x) => { x.disabled = b; }),
        clearSecret: () => {},
        wait: (s) => { const d = host._err; if (d) { d.textContent = t('signin.wait', { time: formatCountdown(s) }); d.hidden = false; } },
        error: (m) => { const d = host._err; if (d) { d.textContent = m; d.hidden = false; } },
      };
      submit(profile, {}, ui);
      return;
    }
    showCredential(profile, modes);
  }

  function showCredential(profile, modes) {
    let mode = modes[0];
    const input = el('input', {
      type: 'password', id: 'signin-secret', autocomplete: 'current-password', autocapitalize: 'off',
      spellcheck: 'false', maxlength: '256', 'aria-describedby': 'signin-cred-error',
    });
    const label = el('label', { for: 'signin-secret' });
    const err = el('div', { class: 'lock-error signin-error', id: 'signin-cred-error', role: 'alert', 'aria-live': 'assertive', hidden: '' });
    const wait = el('div', { class: 'signin-wait', role: 'status', 'aria-live': 'off', hidden: '' });
    const go = el('button', { type: 'submit', class: 'btn-primary signin-go', text: t('signin.go') });
    const back = el('button', { type: 'button', class: 'btn-secondary signin-back', text: t('signin.back') });
    const toggle = modes.length > 1 ? el('button', { type: 'button', class: 'lock-forgot signin-toggle' }) : null;

    const applyMode = () => {
      const pin = mode === 'pin';
      label.textContent = t(pin ? 'signin.pin_for' : 'signin.password_for', { name: profile.name });
      input.setAttribute('inputmode', pin ? 'numeric' : 'text');
      input.setAttribute('autocomplete', pin ? 'one-time-code' : 'current-password');
      if (toggle) toggle.textContent = t(pin ? 'signin.use_password' : 'signin.use_pin');
      input.value = '';
    };
    applyMode();
    if (toggle) toggle.addEventListener('click', () => {
      mode = mode === modes[0] ? modes[1] : modes[0];
      applyMode();
      input.focus();
    });

    const ui = {
      setBusy: (b) => { go.disabled = b || !!countdown; input.disabled = b; back.disabled = b; },
      clearSecret: () => { input.value = ''; },
      error: (m) => { wait.hidden = true; err.textContent = m; err.hidden = !m; input.focus(); },
      wait: (secs) => {
        stopCountdown();
        err.textContent = t('signin.wait_announce', { seconds: secs });
        err.hidden = false;
        wait.hidden = false;
        go.disabled = true;
        countdown = createCountdown({
          seconds: secs,
          onTick: (left) => { wait.textContent = t('signin.wait', { time: formatCountdown(left) }); },
          onDone: () => {
            countdown = null;
            wait.hidden = true;
            err.hidden = true;
            go.disabled = false;
            try { input.focus(); } catch { /* ignore */ }
          },
        });
      },
    };

    const form = el('form', { class: 'signin-form', 'data-no-submit': '', novalidate: '' }, [
      el('div', { class: 'signin-who' }, [avatar(profile, 'signin-avatar'), el('strong', { text: profile.name }), el('span', { class: 'signin-role', text: roleLabel(profile.role) })]),
      el('div', { class: 'sec-field signin-field' }, [label, input]),
      toggle,
      err,
      wait,
      go,
      back,
    ]);
    form.addEventListener('submit', (ev) => {
      ev.preventDefault();
      if (!input.value || go.disabled) return;
      submit(profile, credentialOptions(mode, input.value), ui);
    });
    back.addEventListener('click', () => showTiles());
    host.replaceChildren(form);
    host._err = err;
    try { input.focus(); } catch { /* ignore */ }
  }

  showTiles();

  return {
    reset: showTiles,
    pick,
    /** The profile whose PIN/password form is showing, or null. */
    picked: () => picked,
    destroy: () => { stopCountdown(); host.replaceChildren(); },
  };
}

// ---- First-run setup -----------------------------------------------------------

/**
 * The "create the first Admin" form.
 *   setupRequired  show the Setup token field (network install)
 *   onSubmit       async ({name, password, newPin, setupToken})
 *   onCancel       optional; adds a Cancel/Not now button
 * Returns the form element and a `focus()`.
 */
export function renderSetup({ setupRequired, onSubmit, onCancel, idPrefix = 'setup' }) {
  const id = (s) => `${idPrefix}-${s}`;
  const name = el('input', { type: 'text', id: id('name'), maxlength: '40', autocomplete: 'username', value: t('setup.default_name') });
  const pw = el('input', { type: 'password', id: id('password'), autocomplete: 'new-password', maxlength: '256', 'aria-describedby': id('password-hint') });
  const pw2 = el('input', { type: 'password', id: id('confirm'), autocomplete: 'new-password', maxlength: '256' });
  const pin = el('input', { type: 'password', id: id('pin'), inputmode: 'numeric', autocomplete: 'off', maxlength: '32', 'aria-describedby': id('pin-hint') });
  const token = setupRequired
    ? el('input', { type: 'text', id: id('token'), autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', 'aria-describedby': id('token-hint') })
    : null;
  const err = el('div', { class: 'lock-error setup-error', role: 'alert', 'aria-live': 'assertive', hidden: '' });
  const submit = el('button', { type: 'submit', class: 'btn-primary', text: t('setup.create') });
  const fld = (labelKey, input, hintKey, hintId) => el('div', { class: 'sec-field setup-field' }, [
    el('label', { for: input.id, text: t(labelKey) }),
    input,
    hintKey ? el('span', { class: 'people-field-hint', id: hintId, text: t(hintKey) }) : null,
  ]);
  const buttons = [submit];
  if (onCancel) {
    const c = el('button', { type: 'button', class: 'btn-secondary', text: t('setup.not_now') });
    c.addEventListener('click', onCancel);
    buttons.push(c);
  }
  const form = el('form', { class: 'setup-form', 'data-no-submit': '', novalidate: '' }, [
    fld('setup.name', name),
    fld('setup.password', pw, 'setup.password_hint', id('password-hint')),
    fld('setup.confirm', pw2),
    fld('setup.pin', pin, 'setup.pin_hint', id('pin-hint')),
    token ? fld('setup.token', token, 'setup.token_hint', id('token-hint')) : null,
    err,
    el('div', { class: 'bm-actions setup-actions' }, buttons),
  ]);
  const show = (m) => { err.textContent = m || ''; err.hidden = !m; };
  let waitTimer = null;
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    show('');
    if (String(pw.value).length < 8) { show(t('setup.err_password_short')); pw.focus(); return; }
    if (pw.value !== pw2.value) { show(t('setup.err_mismatch')); pw2.focus(); return; }
    if (pin.value && String(pin.value).length < 4) { show(t('setup.err_pin_short')); pin.focus(); return; }
    if (token && !token.value.trim()) { show(t('setup.err_token_missing')); token.focus(); return; }
    submit.disabled = true;
    try {
      await onSubmit({
        name: name.value.trim() || undefined,
        password: pw.value,
        newPin: pin.value || undefined,
        setupToken: token ? token.value.trim() : undefined,
      });
      pw.value = pw2.value = pin.value = '';
      if (token) token.value = '';
    } catch (e) {
      if (e && e.status === 429 && e.retryAfter) {
        show(t('signin.wait_announce', { seconds: e.retryAfter }));
        if (waitTimer) clearTimeout(waitTimer);
        waitTimer = setTimeout(() => { submit.disabled = false; show(''); }, e.retryAfter * 1000);
        return;
      }
      show(errMessage(e, 'setup.failed'));
    } finally {
      if (!waitTimer) submit.disabled = false;
    }
  });
  return { form, focus: () => name.focus() };
}

/** The full-page card: logo-less, titled, with the explanation. */
export function setupPage(host, { setupRequired, onSubmit }) {
  const s = renderSetup({ setupRequired, onSubmit });
  host.replaceChildren(
    el('div', { class: 'lock-emoji', 'aria-hidden': 'true', text: '👋' }),
    el('h2', { id: 'setup-title', text: t('setup.title') }),
    el('p', { class: 'lock-sub', id: 'setup-sub', text: t(setupRequired ? 'setup.sub_remote' : 'setup.sub') }),
    s.form,
  );
  try { s.focus(); } catch { /* not attached */ }
  return s;
}

// ---- Rail chip + menu ----------------------------------------------------------------

/** The label for the chip button, e.g. "Sam, User". */
export function chipLabel(profile, role) {
  const name = (profile && profile.name) || t(`role.${role}`);
  return t('chip.label', { name, role: roleLabel(role) });
}

export function paintChip(btn, profile, role) {
  if (!btn) return;
  const spec = avatarSpec(profile || { name: roleLabel(role) }, { noImages: nimEnabled() });
  btn.replaceChildren(spec.kind === 'img'
    ? el('img', { class: 'profile-chip-img', src: spec.src, alt: '', draggable: 'false' })
    : el('span', { class: 'profile-chip-text', 'aria-hidden': 'true', text: spec.text }));
  const label = chipLabel(profile, role);
  btn.setAttribute('aria-label', label);
  btn.title = label;
  btn.dataset.label = (profile && profile.name) || roleLabel(role);
}

// ---- Rail chip menu --------------------------------------------------------------------------

/** A small popup anchored to the chip. Returns { open(), close(), isOpen() }.
 *  Items: [{label, onSelect}]. Escape closes and returns focus to the chip. */
export function attachChipMenu(btn, menu, { header, items }) {
  const open = () => {
    menu.replaceChildren(
      el('div', { class: 'profile-menu-head', text: header() }),
      ...items().map((it) => {
        const b = el('button', { type: 'button', class: 'profile-menu-item', role: 'menuitem', text: it.label });
        b.addEventListener('click', () => { close(); it.onSelect(); });
        return b;
      }),
    );
    menu.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    const r = btn.getBoundingClientRect();
    menu.style.insetInlineStart = `${Math.max(8, Math.round(r.left))}px`;
    menu.style.top = `${Math.max(8, Math.round(r.top))}px`;
    const first = menu.querySelector('button');
    if (first) first.focus();
  };
  const close = () => {
    if (menu.hidden) return;
    menu.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
  };
  btn.setAttribute('aria-haspopup', 'menu');
  btn.setAttribute('aria-expanded', 'false');
  btn.addEventListener('click', () => (menu.hidden ? open() : close()));
  menu.addEventListener('keydown', (e) => {
    const els = [...menu.querySelectorAll('button')];
    const i = els.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); close(); btn.focus(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); (els[(i + 1) % els.length] || els[0]).focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (els[(i - 1 + els.length) % els.length] || els[0]).focus(); }
  });
  document.addEventListener('click', (e) => {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) close();
  });
  return { open, close, isOpen: () => !menu.hidden };
}

// ---- Password (Admins) ------------------------------------------------------------------

/**
 * Set or change YOUR password.
 *   mode        'first' (a PIN-only Admin setting the first one: confirmed with
 *               the install PIN, when there is one) | 'change'
 *   needsPin    first mode only: ask for the current PIN
 *   onSubmit    async ({current?, currentPin?, newPassword})
 *   onDismiss   optional; adds "Not now"
 */
export function renderPasswordForm({ mode, needsPin, onSubmit, onDismiss, idPrefix = 'pw' }) {
  const id = (s) => `${idPrefix}-${s}`;
  const askCurrent = mode === 'change' || needsPin;
  const cur = askCurrent
    ? el('input', { type: 'password', id: id('current'), autocomplete: mode === 'change' ? 'current-password' : 'off',
      inputmode: mode === 'change' ? 'text' : 'numeric', maxlength: '256' })
    : null;
  const pw = el('input', { type: 'password', id: id('new'), autocomplete: 'new-password', maxlength: '256', 'aria-describedby': id('hint') });
  const pw2 = el('input', { type: 'password', id: id('confirm'), autocomplete: 'new-password', maxlength: '256' });
  const err = el('div', { class: 'lock-error', role: 'alert', 'aria-live': 'assertive', hidden: '' });
  const save = el('button', { type: 'submit', class: 'btn-primary', text: t('password.save') });
  const buttons = [save];
  if (onDismiss) {
    const d = el('button', { type: 'button', class: 'btn-secondary', text: t('setup.not_now') });
    d.addEventListener('click', onDismiss);
    buttons.push(d);
  }
  const fld = (labelKey, input, hint) => el('div', { class: 'sec-field' }, [
    el('label', { for: input.id, text: t(labelKey) }),
    input,
    hint ? el('span', { class: 'people-field-hint', id: id('hint'), text: t(hint) }) : null,
  ]);
  const form = el('form', { class: 'password-form', 'data-no-submit': '', novalidate: '' }, [
    cur ? fld(mode === 'change' ? 'password.current' : 'password.current_pin', cur) : null,
    fld('password.new', pw, 'setup.password_hint'),
    fld('setup.confirm', pw2),
    err,
    el('div', { class: 'bm-actions' }, buttons),
  ]);
  const show = (m) => { err.textContent = m || ''; err.hidden = !m; };
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    show('');
    if (cur && !cur.value) { show(t('security.need_current')); cur.focus(); return; }
    if (String(pw.value).length < 8) { show(t('setup.err_password_short')); pw.focus(); return; }
    if (pw.value !== pw2.value) { show(t('setup.err_mismatch')); pw2.focus(); return; }
    save.disabled = true;
    try {
      await onSubmit({
        current: mode === 'change' ? cur.value : undefined,
        currentPin: mode === 'first' && cur ? cur.value : undefined,
        newPassword: pw.value,
      });
    } catch (e) {
      show(errMessage(e, 'setup.failed'));
    } finally {
      if (cur) cur.value = '';
      pw.value = pw2.value = '';
      save.disabled = false;
    }
  });
  return { form };
}
