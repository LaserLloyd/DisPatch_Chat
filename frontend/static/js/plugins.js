// Settings -> Plugins (admin-only): one place that answers "what is installed
// and what is on". Contract: docs/design/platform-2-3.md section 3.
//
// Three groups, in the Settings modal's own group/row style (.dev-group /
// .dev-row, so every palette applies and this file carries no colour):
//
//   Add-ons          one row per extension the host knows (GET /api/plugins):
//                    name, version, state and an install-wide switch bound to
//                    PUT /api/plugins/extensions/{id} (takes effect after a
//                    restart). When the voice add-on is not installed, a row
//                    says so and shows the install command (voice-absent.js).
//   Apps & tools     the Tools table. js/tools.js owns it (table, draft, Save
//                    in the footer); this module only gives it a host element.
//   Built-in lookups the install-wide switch for app/livetools
//                    (PUT /api/plugins/livetools) and the tool names.
//
// Pure-ish: nothing runs at import time. Everything app-shaped arrives through
// `deps` ({ mountTools(host), toast(msg, isErr), openTab(id), api? }), which
// is also how the tests drive it without main.js.

import { api as realApi } from './api.js?v=33';
import { t } from './i18n.js?v=4';
import { el } from './util.js?v=20';
import { COMMANDS, commandBlock } from './voice-absent.js?v=7';

let seq = 0;          // a newer mount supersedes a slower, older one

const stateLabel = () => ({
  ok: t('plugins.state_ok'),
  incompatible: t('plugins.state_incompatible'),
  failed: t('plugins.state_failed'),
  disabled: t('plugins.state_disabled'),
});

function errText(e) {
  return String((e && e.message) || e || '').replace(/^\d{3}:\s*/, '');
}

function group(id, title, hint, body) {
  return el('section', { class: 'dev-group', id, 'aria-labelledby': `${id}-h` }, [
    el('h3', { class: 'dev-group-title', id: `${id}-h`, text: title }),
    hint ? el('p', { class: 'dev-group-hint', text: hint }) : null,
    body,
  ]);
}

/** A row: name + one-line explanation on the start side, a switch at the end. */
function switchRow({ name, hints = [], checked, label, onChange, dataset = {} }) {
  const cb = el('input', { type: 'checkbox', 'aria-label': label });
  cb.checked = !!checked;
  cb.addEventListener('change', () => onChange(cb));
  return el('div', { class: 'dev-row plugins-row', dataset }, [
    el('span', { class: 'dev-row-text' }, [
      el('strong', { text: name }),
      ...hints.filter(Boolean),
    ]),
    cb,
  ]);
}

const hintLine = (text, cls = '') =>
  el('span', { class: 'dev-row-hint' + (cls ? ` ${cls}` : ''), text });

function extensionRow(ext, deps, a) {
  const states = stateLabel();
  const version = ext.version ? ` v${ext.version}` : '';
  const hints = [hintLine(states[ext.state] || ext.state, `plugins-state plugins-state-${ext.state}`)];
  // The server's own text for a failed / incompatible add-on (already scrubbed
  // of home paths); shown verbatim and left-to-right, never parsed.
  if (ext.detail && ext.state !== 'ok') {
    const d = hintLine(ext.detail, 'ext-detail');
    d.setAttribute('dir', 'ltr');
    hints.push(d);
  }
  const pending = hintLine(t('plugins.restart_hint'), 'plugins-restart');
  pending.hidden = !ext.restart_pending;
  hints.push(pending);
  return switchRow({
    name: `${ext.name || ext.id}${version}`,
    hints,
    checked: ext.enabled,
    label: t('plugins.switch_aria', { name: ext.name || ext.id }),
    dataset: { ext: ext.id },
    onChange: async (cb) => {
      const want = cb.checked;
      cb.disabled = true;
      try {
        await a.setPluginExtension(ext.id, want);
        ext.enabled = want;
        // A switch takes effect at the next start: the hint shows while the
        // choice and the running state disagree — the server's own
        // `restart_pending` rule (plugins_api._extension_rows). "Running" is
        // not the only loaded state: a failed or incompatible add-on is still
        // loaded until a restart, so switching it off is pending too.
        pending.hidden = want !== (ext.state === 'disabled');
      } catch (e) {
        cb.checked = !want;
        deps.toast(t('plugins.save_failed', { error: errText(e) }), true);
      } finally {
        cb.disabled = false;
      }
    },
  });
}

function voiceMissingRow(deps) {
  const open = el('button', {
    type: 'button', class: 'btn-secondary plugins-voice-open', text: t('plugins.voice_open'),
  });
  open.addEventListener('click', () => deps.openTab && deps.openTab('voice'));
  return el('div', { class: 'dev-row dev-row-block plugins-row plugins-voice-missing' }, [
    el('span', { class: 'dev-row-text' }, [
      el('strong', { text: t('plugins.voice_missing_title') }),
      hintLine(t('plugins.voice_missing_hint')),
    ]),
    commandBlock(COMMANDS.install),
    el('div', { class: 'ext-actions' }, [open]),
  ]);
}

function addonsBody(data, deps, a) {
  const rows = (data.extensions || []).map((e) => extensionRow(e, deps, a));
  if (!(data.voice && data.voice.installed)) rows.push(voiceMissingRow(deps));
  if (!rows.length) rows.push(el('div', { class: 'dev-row' }, [hintLine(t('plugins.addons_empty'))]));
  return el('div', { class: 'dev-group-body', id: 'plugins-addons-body' }, rows);
}

function lookupsBody(data, deps, a) {
  const live = data.livetools || { enabled: true, names: [] };
  const names = el('div', { class: 'plugins-names', 'aria-label': t('plugins.lookups_names') },
    (live.names || []).map((n) => el('code', { class: 'plugins-chip', dir: 'ltr', text: n })));
  const row = switchRow({
    name: t('plugins.lookups_name'),
    hints: [hintLine(t('plugins.lookups_hint')), hintLine(t('plugins.lookups_note'))],
    checked: live.enabled,
    label: t('plugins.lookups_aria'),
    dataset: { livetools: '1' },
    onChange: async (cb) => {
      const want = cb.checked;
      cb.disabled = true;
      try {
        await a.setPluginLivetools(want);
        deps.toast(t(want ? 'plugins.lookups_on' : 'plugins.lookups_off'));
      } catch (e) {
        cb.checked = !want;
        deps.toast(t('plugins.save_failed', { error: errText(e) }), true);
      } finally {
        cb.disabled = false;
      }
    },
  });
  return el('div', { class: 'dev-group-body', id: 'plugins-lookups-body' }, [
    row,
    el('div', { class: 'dev-row dev-row-block' }, [names]),
  ]);
}

/**
 * Settings -> Plugins came on screen: (re)build the pane from a fresh read.
 *   pane   #spane-plugins
 *   deps   { mountTools(hostEl) -> Promise, toast(msg, isErr), openTab(id), api? }
 */
export async function mountPluginsSettings(pane, deps) {
  const a = deps.api || realApi;
  const mine = ++seq;
  const toolsHost = el('div', { class: 'tools-pane plugins-tools-host', id: 'plugins-tools-host' });
  const hint = el('p', { class: 'muted modal-hint', text: t('plugins.hint') });
  pane.replaceChildren(hint, el('p', { class: 'muted modal-hint', text: t('common.loading') }));
  let data = null;
  let loadError = null;
  try {
    data = await a.plugins();
  } catch (e) {
    loadError = e;
  }
  if (mine !== seq) return;                         // a newer mount took over
  const parts = [hint];
  if (data) {
    parts.push(group('plugins-addons', t('plugins.addons_title'), t('plugins.addons_hint'),
      addonsBody(data, deps, a)));
  } else {
    const retry = el('button', { type: 'button', class: 'btn-secondary', text: t('common.retry') });
    retry.addEventListener('click', () => mountPluginsSettings(pane, deps));
    parts.push(group('plugins-addons', t('plugins.addons_title'), null,
      el('div', { class: 'dev-group-body' }, [
        el('div', { class: 'dev-row dev-row-action', role: 'alert' }, [
          hintLine(t('plugins.load_failed', { error: errText(loadError) })), retry]),
      ])));
  }
  parts.push(group('plugins-tools', t('plugins.tools_title'), t('plugins.tools_hint'), toolsHost));
  if (data) {
    parts.push(group('plugins-lookups', t('plugins.lookups_title'), null, lookupsBody(data, deps, a)));
  }
  pane.replaceChildren(...parts);
  // The Tools table renders into its own host and loads its own list.
  await deps.mountTools(toolsHost);
}
