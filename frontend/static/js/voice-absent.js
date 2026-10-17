// Settings → Voice when the voice add-on is not running.
//
// Voice chat is a separate package — DisPatch Voice (MIT) — that DisPatch
// loads as an extension (js/extensions.js, docs/extensions.md). This page is
// what the Voice tab shows until that add-on mounts its own pane: what voice
// chat is, and exactly how to add it. Four states, from GET /api/extensions:
//
//   absent        nothing provides `voice` — the three install steps
//   incompatible  installed, but its extension API range excludes this shell
//   failed        installed, but its setup raised (the server's `detail`)
//   disabled      DISPATCH_EXTENSIONS leaves it out
//
// plus `client_failed`: the server says ok but the add-on's own page could not
// load or handshake in THIS browser.
//
// Markup is the Settings modal's own (.dev-group / .dev-row / .btn-*), so it
// follows every palette with no colour of its own. Shell commands are shown
// as typed and never translated; the prose is `voice_absent.*`. The install
// URL (about.js, beside every other repository URL) and the moving `v1` tag
// (here) are each one constant, and a test reads them: re-point the add-on
// repo's `v1` tag at each 1.x release and every install command follows.

import { t } from './i18n.js?v=4';
import { el } from './util.js?v=20';
import { SOURCE_URL, VOICE_REPO_URL } from './about.js?v=19';

export { VOICE_REPO_URL };
/** The moving tag that always names the latest 1.x release (SPEC §3.4). */
export const VOICE_TAG = 'v1';
/** The install guide: DisPatch's docs/extensions.md, "Voice" section. */
export const VOICE_GUIDE_URL = `${SOURCE_URL}/blob/main/docs/extensions.md#voice`;

const PKG = `dispatch-voice @ git+${VOICE_REPO_URL}@${VOICE_TAG}`;

/** Every command the page shows, keyed by what it does. Shell, never translated. */
export const COMMANDS = Object.freeze({
  install: `uv pip install --python .venv/bin/python "${PKG}"`,
  upgrade: `uv pip install --python .venv/bin/python --upgrade "${PKG}"`,
  editable: 'uv pip install --python .venv/bin/python -e /path/to/DisPatch_Voice',
  models: '.venv/bin/dispatch-voice fetch-models',
  kokoro: 'uv pip install --python .venv/bin/python /path/to/dispatch-voice-kokoro',
  kokoroEnv: 'DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup',
  dockerBuild: 'VOICE=1 docker compose build && docker compose up -d',
  dockerModels: 'docker compose exec app dispatch-voice fetch-models',
  uvRun: 'uv run --offline --with-editable /path/to/DisPatch_Voice uvicorn app.main:app --host 0.0.0.0 --port 8765 --ws-max-size 262144',
});

/**
 * What the page should say, from the /api/extensions answer.
 *   list        the `extensions` array (or null when it could not be read)
 *   mode        the answer's `mode` ('entry_points' | 'env' | 'off')
 *   clientFail  { detail } when the add-on's page failed in this browser
 */
export function voiceState(list, mode, clientFail = null) {
  const rows = Array.isArray(list) ? list.filter((e) => e && e.id === 'voice') : [];
  const ok = rows.find((e) => e.state === 'ok');
  if (ok && clientFail && clientFail.state === 'incompatible'
      && /extension API/.test(clientFail.detail || '')) {
    // The add-on's JS handshake disagreed with this shell: the same
    // which-side-to-update copy as a server-side mismatch (review T1 L5).
    return { state: 'incompatible', ext: { ...ok, detail: clientFail.detail }, mode };
  }
  if (ok) return { state: clientFail ? 'client_failed' : 'ok', ext: ok, mode };
  const pick = (s) => rows.find((e) => e.state === s);
  const ext = pick('failed') || pick('incompatible') || pick('disabled') || null;
  if (ext) return { state: ext.state, ext, mode };
  return { state: 'absent', ext: null, mode: mode || null };
}

/** `a `code` b` → text and <code> nodes (locale strings carry commands in
 *  backticks; no markup is ever parsed). Each <code> is a left-to-right
 *  isolate: in an Arabic sentence an inline `--offline` otherwise renders as
 *  "offline--" (the leading hyphens take the paragraph's direction). */
function withCode(text) {
  const parts = String(text).split('`');
  return parts.map((p, i) => (i % 2 ? el('code', { text: p, dir: 'ltr' }) : document.createTextNode(p)));
}

function hint(text, extra = '') {
  return el('span', { class: 'dev-row-hint' + (extra ? ' ' + extra : '') }, withCode(text));
}

/** The server's (or the browser's) own error text: shown exactly as sent —
 *  never split on backticks — and left-to-right, since it is English with
 *  punctuation and module names that an RTL paragraph would reorder. */
function detailLine(text) {
  return el('span', { class: 'dev-row-hint ext-detail', dir: 'ltr', text: String(text) });
}

/** One copyable command: the line as typed, with the chat's own code-block
 *  Copy button (.copy-btn) in its corner. Always left-to-right. */
function command(cmd) {
  const btn = el('button', { type: 'button', class: 'copy-btn ext-cmd-copy', text: t('msg.copy') });
  btn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(cmd);
      btn.textContent = t('msg.copied');
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = t('msg.copy'); btn.classList.remove('copied'); }, 1500);
    } catch { /* no clipboard (http:, old browser): the line is still selectable */ }
  });
  return el('div', { class: 'ext-cmd-row', dir: 'ltr' }, [
    el('pre', { class: 'ext-cmd' }, [el('code', { text: cmd })]),
    btn,
  ]);
}

/** The same copyable command line, for other Settings panes (Plugins). */
export const commandBlock = command;

function row(title, hints = [], extra = []) {
  return el('div', { class: 'dev-row dev-row-block ext-row' }, [
    el('span', { class: 'dev-row-text' }, [
      title ? el('strong', {}, withCode(title)) : null,
      ...hints.filter(Boolean),
    ]),
    ...extra.filter(Boolean),
  ]);
}

function group(title, rows, extraClass = '') {
  return el('section', { class: 'dev-group' + (extraClass ? ' ' + extraClass : '') }, [
    el('h3', { class: 'dev-group-title', text: title }),
    el('div', { class: 'dev-group-body' }, rows.filter(Boolean)),
  ]);
}

function details(summary, hintText, cmds) {
  return el('details', { class: 'ext-details' }, [
    el('summary', {}, withCode(summary)),
    hint(hintText),
    ...cmds.map(command),
  ]);
}

function link(href, text) {
  return el('a', { href, target: '_blank', rel: 'noopener noreferrer', class: 'ext-link', text });
}

function checkAgainButton(onCheckAgain, busy) {
  const b = el('button', { type: 'button', class: 'btn-primary ext-check-again',
    text: busy ? t('voice_absent.checking') : t('voice_absent.check_again') });
  b.disabled = !!busy;
  b.addEventListener('click', () => { if (onCheckAgain) onCheckAgain(); });
  return b;
}

function aboutRow() {
  return row(t('voice_absent.title'), [hint(t('voice_absent.lead')), hint(t('voice_absent.english_only'))]);
}

function stepsGroup(opts) {
  const release = t('voice_absent.latest_release');
  return group(t('voice_absent.steps_title'), [
    row(t('voice_absent.step_install', { release }), [hint(t('voice_absent.where_backend'))], [
      command(COMMANDS.install),
      hint(t('voice_absent.uv_sync_note'), 'ext-note'),
      details(t('voice_absent.checkout_variant'), t('voice_absent.checkout_hint'),
        [COMMANDS.editable, COMMANDS.uvRun]),
    ]),
    row(t('voice_absent.step_models'), [hint(t('voice_absent.models_hint'))], [command(COMMANDS.models)]),
    row(t('voice_absent.step_restart'), [hint(t('voice_absent.restart_hint'))], [
      el('div', { class: 'ext-actions' }, [checkAgainButton(opts.onCheckAgain, opts.busy)]),
      opts.notYet ? hint(t('voice_absent.not_found_yet'), 'ext-note') : null,
    ]),
  ]);
}

function moreGroup() {
  return group(t('voice_absent.more_title'), [
    row(t('voice_absent.optional_kokoro'), [hint(t('voice_absent.kokoro_hint'))],
      [command(COMMANDS.kokoro), command(COMMANDS.kokoroEnv)]),
    row(null, [], [
      el('div', { class: 'ext-links' }, [
        link(VOICE_REPO_URL, t('voice_absent.links_repo')),
        link(VOICE_GUIDE_URL, t('voice_absent.links_guide')),
      ]),
      details(t('voice_absent.docker_note'), t('voice_absent.docker_hint'),
        [COMMANDS.dockerBuild, COMMANDS.dockerModels]),
      details(t('voice_absent.bare_metal_note'), t('voice_absent.bare_metal_hint'), [COMMANDS.uvRun]),
    ]),
  ]);
}

/** The one row that says why voice chat is not running here. */
function stateRow(info, opts) {
  const ext = info.ext || {};
  const name = ext.name || 'DisPatch Voice';
  const version = ext.version || '';
  const again = el('div', { class: 'ext-actions' }, [checkAgainButton(opts.onCheckAgain, opts.busy)]);
  if (info.state === 'incompatible') {
    const addonOld = /update the add-on/i.test(ext.detail || '');
    return row(t('voice_absent.state_incompatible', { name, version }), [
      ext.detail ? detailLine(ext.detail) : null,
      hint(addonOld ? t('voice_absent.update_addon') : t('voice_absent.update_dispatch')),
    ], [addonOld ? command(COMMANDS.upgrade) : null, again]);
  }
  if (info.state === 'failed') {
    return row(t('voice_absent.state_failed', { name, version }), [
      ext.detail ? detailLine(ext.detail) : null,
      hint(t('voice_absent.failed_fix')),
    ], [again]);
  }
  if (info.state === 'disabled') {
    // The operator's own switch (Settings -> Plugins writes extensions.yaml):
    // the server's detail starts "Switched off in Settings".
    if (/^Switched off in Settings/.test(ext.detail || '')) {
      return row(t('voice_absent.state_user_off_title'), [hint(t('voice_absent.state_user_off'))], [again]);
    }
    return row(t('voice_absent.state_disabled_title'), [hint(t('voice_absent.state_disabled'))],
      opts.noAgain ? [] : [again]);
  }
  // client_failed: the server mounted it; this browser could not load its page.
  return row(t('voice_absent.could_not_load_title'), [
    hint(t('voice_absent.could_not_load')),
    opts.clientDetail ? detailLine(opts.clientDetail) : null,
  ]);
}

/**
 * Paint the page into `root` (the Voice pane's container). Re-callable: it
 * replaces whatever is there. `opts`:
 *   onCheckAgain()  re-read /api/extensions and mount without a reload
 *   busy            a check is in flight
 *   notYet          the last check found nothing new
 *   clientDetail    why the add-on's page failed here (client_failed)
 * Returns the root.
 */
export function renderVoiceAbsent(root, info, opts = {}) {
  if (!root) return root;
  const st = (info && info.state) || 'absent';
  const parts = [];
  const title = t('voice_absent.group');
  if (st === 'absent') {
    // Extensions switched off AND nothing installed: say both.
    const off = info && info.mode === 'off'
      ? stateRow({ state: 'disabled' }, { ...opts, noAgain: true }) : null;
    parts.push(group(title, [aboutRow(), off]), stepsGroup(opts), moreGroup());
  } else if (st === 'ok') {
    // Mounted (or mounting): the add-on's own pane replaces this one.
    parts.push(group(title, [aboutRow()]));
  } else {
    parts.push(group(title, [stateRow(info, opts), aboutRow()]));
  }
  root.replaceChildren(...parts);
  root.dataset.voiceState = st;
  return root;
}
