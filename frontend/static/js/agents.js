// Settings -> AI & Connections -> Agents (Owner and Admin).
//
// Four groups in the Settings modal's own .dev-group / .dev-row vocabulary, so every palette applies:
//
//   Backends      what each backend is, whether it can run here, and what it can and cannot do
//                 (approvals, steering, subagents, cost figures, "stays on this machine" or
//                 "prompts go to <service>"). The Claude Agent's credentials live in its card: an
//                 Anthropic Console API key, or Amazon Bedrock / Google Cloud / Microsoft Foundry
//                 credentials set on this machine. There is NO subscription sign-in and this page
//                 never offers one.
//   Agents        define an agent (backend, model, instructions, tools, MCP servers, workspace,
//                 approval policy, budget), publish it to profiles, switch it off, delete it
//   Limits        per-profile daily USD / turns / time / token limits, how long transcripts are
//                 kept, how many tasks run at once, today's (estimated) spend, and the
//                 unified-dispatch switch
//   Secrets       the names MCP servers need (write-only: the page can set a value, never read one)
//
// The server enforces everything here. This module decides what to OFFER: the capabilities that
// are host-level in effect (running a repository's own hooks, local MCP programs, unattended
// destructive tools) are only enabled for the Owner, because the server refuses them to an Admin.
//
// Pure-ish: nothing runs at import time; app-shaped things arrive through `deps`.

import { api as realApi } from './api.js?v=35';
import { t } from './i18n.js?v=4';
import { el } from './util.js?v=20';

let uid = 0;
const nid = (p) => `${p}-${++uid}`;

// The tools an agent can be given, by risk. "ask" ones always raise an approval card.
export const TOOL_PRESETS = {
  read_only: ['Read', 'Glob', 'Grep'],
  studioforge_read_only: ['mcp__studioforge__*'],
};

export const STUDIOFORGE_PRESET = {
  name: 'studioforge',
  server: {
    transport: 'http',
    url: 'http://127.0.0.1:1234/mcp',
    header_secrets: { Authorization: 'STUDIOFORGE_MCP_KEY_RO' },
    privileged: false,
  },
};

const BACKEND_NOTES = {
  openclaw: 'agents.backend_note_openclaw',
  claude_sdk: 'agents.backend_note_claude',
};

const ROUTE_LABELS = {
  api_key: 'agents.route_api_key', // scrub-ok: an i18n key, not a credential
  bedrock: 'agents.route_bedrock',
  google: 'agents.route_google',
  foundry: 'agents.route_foundry',
};

const MODE_LABELS = {
  interactive: 'agents.mode_interactive',
  unattended: 'agents.mode_unattended',
  plan: 'agents.mode_plan',
};

// ----------------------------------------------------------------------------- pure helpers

const lines = (text) => String(text || '').split(/\r?\n|,/).map((s) => s.trim()).filter(Boolean);

const numOrNull = (v) => {
  const s = String(v ?? '').trim();
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) && n > 0 ? n : NaN;
};

/** A blank form for `backend`. */
export function blankForm(backend = 'openclaw') {
  return {
    id: null, base: {}, name: '', description: '', backend, seat: '', model: '', instructions: '',
    tools: '', mcp: [], workspace_mode: 'scratch', trust_repo_settings: false,
    approval_mode: 'interactive', ask_tools: '', unattended_allow: '',
    max_usd: '', max_usd_per_day: '', max_turns: '', max_minutes: '', max_tokens: '',
    reads_untrusted_content: false, published_all: false, published: [], disabled: false,
  };
}

/** The form a stored agent edits as. */
export function specToForm(agent) {
  const s = agent.spec || {};
  const b = s.budget || {};
  const f = blankForm(s.backend || 'openclaw');
  return Object.assign(f, {
    id: agent.id, base: s, name: agent.name, description: agent.description || '',
    seat: (s.backend_options && s.backend_options.agent_id) || '',
    model: s.model || '', instructions: s.instructions || '',
    tools: (s.tool_allowlist || []).join('\n'),
    mcp: Object.entries(s.mcp_servers || {}).map(([name, srv]) => ({
      name, transport: srv.transport || 'http', url: srv.url || '', command: srv.command || '',
      args: (srv.args || []).join(' '),
      header: Object.keys(srv.header_secrets || {})[0] || '',
      secret: Object.values(srv.header_secrets || {})[0] || '',
      privileged: !!srv.privileged,
    })),
    workspace_mode: (s.workspace_policy || {}).mode || 'scratch',
    trust_repo_settings: !!(s.workspace_policy || {}).trust_repo_settings,
    approval_mode: (s.approval_policy || {}).mode || 'interactive',
    ask_tools: ((s.approval_policy || {}).ask_tools || []).join('\n'),
    unattended_allow: ((s.approval_policy || {}).unattended_allow || []).join('\n'),
    max_usd: b.max_usd ?? '', max_usd_per_day: b.max_usd_per_day ?? '', max_turns: b.max_turns ?? '',
    max_minutes: b.max_wall_clock_s ? Math.round(b.max_wall_clock_s / 60) : '',
    max_tokens: b.max_tokens ?? '',
    reads_untrusted_content: !!s.reads_untrusted_content,
    published_all: (agent.published_to || []).includes('*'),
    published: (agent.published_to || []).filter((x) => x !== '*'),
    disabled: !!agent.disabled,
  });
}

/**
 * The request body for POST/PATCH /api/agents, or {error: key} for a number the person mistyped.
 * `spec` carries only what the form sets; the server fills every other default (and validates
 * the lot: quarantine rule, secret names, the Owner-only capabilities).
 */
export function formToBody(f) {
  const nums = {
    max_usd: numOrNull(f.max_usd), max_usd_per_day: numOrNull(f.max_usd_per_day),
    max_turns: numOrNull(f.max_turns), max_tokens: numOrNull(f.max_tokens),
    max_wall_clock_s: numOrNull(f.max_minutes),
  };
  if (Object.values(nums).some((v) => Number.isNaN(v))) return { error: 'agents.err_number' };
  if (nums.max_wall_clock_s != null) nums.max_wall_clock_s *= 60;
  const budget = Object.fromEntries(Object.entries(nums).filter(([, v]) => v != null));
  const mcp = {};
  for (const m of f.mcp || []) {
    const name = String(m.name || '').trim();
    if (!name) continue;
    const srv = { transport: m.transport || 'http', privileged: !!m.privileged };
    if (srv.transport === 'stdio') {
      srv.command = String(m.command || '').trim();
      srv.args = String(m.args || '').split(/\s+/).filter(Boolean);
    } else {
      srv.url = String(m.url || '').trim();
    }
    srv.header_secrets = m.header && m.secret ? { [m.header.trim()]: m.secret.trim() } : {};
    const prior = (f.base && f.base.mcp_servers && f.base.mcp_servers[name]) || {};
    mcp[name] = { ...prior, ...srv };
  }
  // Start from the stored spec so fields this form does not show (the always-ask list, the git ref,
  // subagent limits, extra high-privilege patterns, backend options) survive an edit instead of
  // being reset to their defaults.
  const base = f.base || {};
  const spec = {
    ...base,
    backend: f.backend,
    tool_allowlist: lines(f.tools),
    mcp_servers: mcp,
    workspace_policy: { ...(base.workspace_policy || {}), mode: f.workspace_mode, trust_repo_settings: !!f.trust_repo_settings },
    approval_policy: {
      ...(base.approval_policy || {}),
      mode: f.approval_mode, ask_tools: lines(f.ask_tools), unattended_allow: lines(f.unattended_allow),
    },
    budget,
    reads_untrusted_content: !!f.reads_untrusted_content,
    instructions: f.instructions,
  };
  delete spec.name;                                   // the server sets it from the agent's name
  if (f.model.trim()) spec.model = f.model.trim(); else delete spec.model;
  if (f.backend === 'openclaw' && f.seat.trim()) {
    spec.backend_options = { ...(base.backend_options || {}), agent_id: f.seat.trim() };
  }
  const published = f.published_all ? ['*'] : (f.published || []);
  return { name: f.name.trim(), description: f.description.trim(), spec, published_to: published, disabled: !!f.disabled };
}

// ----------------------------------------------------------------------------- the pane

let seq = 0;

function errText(e) {
  if (!e) return '';
  const msg = String(e.message || e).replace(/^\d{3}:\s*/, '');
  return e.fix ? `${msg} ${e.fix}` : msg;
}

const hintLine = (text, cls = '') => el('span', { class: `dev-row-hint${cls ? ` ${cls}` : ''}`, text });

function group(id, title, hint, body) {
  return el('section', { class: 'dev-group', id, 'aria-labelledby': `${id}-h` }, [
    el('h3', { class: 'dev-group-title', id: `${id}-h`, text: title }),
    hint ? el('p', { class: 'dev-group-hint', text: hint }) : null,
    body,
  ]);
}

function field(labelText, input, hint) {
  if (!input.id) input.id = nid('af');
  const kids = [el('label', { for: input.id, text: labelText }), input];
  if (hint) {
    const h = el('span', { class: 'people-field-hint', id: `${input.id}-hint`, text: hint });
    input.setAttribute('aria-describedby', h.id);
    kids.push(h);
  }
  return el('div', { class: 'sec-field people-field' }, kids);
}

function checkbox(labelText, checked, onChange, hint, disabled = false) {
  const id = nid('ac');
  const input = el('input', { type: 'checkbox', id });
  input.checked = !!checked;
  input.disabled = !!disabled;
  input.addEventListener('change', () => onChange(input.checked));
  return el('div', { class: 'agents-check' }, [
    input, el('label', { for: id, text: labelText }), hint ? hintLine(hint) : null,
  ]);
}

/**
 * Mount the pane into `host`. deps: { api?, toast, confirm, isOwner }
 * Re-mounting replaces the previous contents (the Settings tab calls it on every visit).
 */
export async function mountAgentsSettings(host, deps = {}) {
  const api = deps.api || realApi;
  const mine = ++seq;
  host.replaceChildren(el('p', { class: 'muted', text: t('common.loading') }));
  let data;
  try {
    const [agents, backends, creds, settings, budgets, usage, secrets] = await Promise.all([
      api.agents(), api.agentBackends(), api.claudeCredentials(), api.agentSettings(),
      api.agentProfileBudgets(), api.agentUsage(), api.agentSecrets(),
    ]);
    data = { agents, backends: backends.backends, creds, settings, budgets: budgets.budgets, usage, secrets: secrets.secrets };
  } catch (e) {
    host.replaceChildren(el('p', { class: 'task-error', role: 'alert', text: errText(e) }));
    return;
  }
  if (mine !== seq) return;

  const toast = (m, bad) => deps.toast && deps.toast(m, bad);
  const reload = () => mountAgentsSettings(host, deps);
  const guard = async (fn) => {
    try { await fn(); } catch (e) { toast(errText(e), true); }
  };

  host.replaceChildren(
    group('agents-backends', t('agents.backends_title'), t('agents.backends_hint'), backendsBody(data, deps, api, guard, reload, toast)),
    group('agents-list', t('agents.list_title'), t('agents.list_hint'), agentsBody(data, deps, api, guard, reload, toast)),
    group('agents-limits', t('agents.limits_title'), t('agents.limits_hint'), limitsBody(data, deps, api, guard, reload, toast)),
    group('agents-secrets', t('agents.secrets_title'), t('agents.secrets_hint'), secretsBody(data, api, guard, reload, toast)),
  );
}

// ---- Backends + Claude credentials ----

function capLine(c) {
  if (!c) return '';
  const bits = [];
  bits.push(c.approvals ? t('agents.cap_approvals') : t('agents.cap_no_approvals'));
  if (c.steering) bits.push(t('agents.cap_steering'));
  if (c.subagents) bits.push(t('agents.cap_subagents'));
  bits.push(c.cost_reporting === 'usd' ? t('agents.cap_cost_usd')
    : c.cost_reporting === 'tokens' ? t('agents.cap_cost_tokens') : t('agents.cap_cost_none'));
  bits.push(c.same_host_required ? t('agents.cap_same_host') : '');
  bits.push(c.data_leaves_host ? t('agents.cap_leaves_host') : t('agents.cap_stays_here'));
  return bits.filter(Boolean).join(' · ');
}

function backendsBody(data, deps, api, guard, reload, toast) {
  let credsPlaced = false;
  const rows = data.backends.map((b) => {
    const label = b.label;
    const status = b.available ? t('agents.backend_ready') : t('agents.backend_needs_setup');
    // The Claude Agent's credentials belong to its own card (3.2: it used to be a
    // second card with the same title right under it), behind a Connect disclosure.
    const creds = b.name === 'claude_sdk' ? claudeCredentials(data, api, guard, reload, toast) : null;
    if (creds) credsPlaced = true;
    return el('div', { class: 'dev-row agents-backend', 'data-backend': b.name }, [
      el('div', { class: 'dev-row-text' }, [
        el('strong', { text: label }),
        el('span', { class: `task-pill task-pill-${b.available ? 'succeeded' : 'hold'}`, text: status }),
        BACKEND_NOTES[b.name] ? hintLine(t(BACKEND_NOTES[b.name])) : null,
        b.problem ? hintLine(b.problem.message, 'task-error') : null,
        hintLine(capLine(b.capabilities)),
        creds ? el('details', { class: 'agents-connect', open: b.available ? null : '' }, [
          el('summary', { text: t('agents.connect') }), creds]) : null,
      ]),
    ]);
  });
  // A server that lists no Claude backend still gets the credentials, on their own.
  if (!credsPlaced) rows.push(el('div', { class: 'dev-row agents-claude-only' }, [claudeCredentials(data, api, guard, reload, toast)]));
  return el('div', { class: 'dev-group-body' }, rows);
}

function claudeCredentials(data, api, guard, reload, toast) {
  const c = data.creds;
  const name = nid('claude');
  const box = el('div', { class: 'agents-claude' });
  const routeSel = el('select', { id: `${name}-route`, class: 'ui-select' }, c.routes.map((r) => el('option', { value: r, text: t(ROUTE_LABELS[r]) })));
  routeSel.value = c.route;
  const key = el('input', {
    type: 'password', id: `${name}-key`, autocomplete: 'off', spellcheck: 'false', maxlength: '512',
    placeholder: c.api_key_set ? t('agents.key_placeholder_set') : 'sk-ant-…',
  });
  const status = el('p', { class: 'dev-row-hint', role: 'status' });
  const paintStatus = () => {
    if (routeSel.value !== 'api_key') {
      status.textContent = t('agents.provider_note', { var: c.provider_env || '' });
    } else if (c.api_key_set) {
      status.textContent = c.api_key_source === 'environment' ? t('agents.key_from_env') : t('agents.key_is_stored');
    } else {
      status.textContent = t('agents.key_missing');
    }
    keyField.hidden = routeSel.value !== 'api_key';
  };
  const keyField = field(t('agents.key_label'), key, t('agents.key_hint'));
  routeSel.addEventListener('change', paintStatus);
  const save = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.save_credentials') });
  save.addEventListener('click', () => guard(async () => {
    const body = { route: routeSel.value };
    if (routeSel.value === 'api_key' && key.value.trim()) body.api_key = key.value.trim();
    await api.saveClaudeCredentials(body);
    key.value = '';
    toast(t('agents.credentials_saved'));
    await reload();
  }));
  const clear = el('button', { type: 'button', class: 'btn-secondary danger-text', text: t('agents.remove_key') });
  clear.hidden = !(c.api_key_set && c.api_key_source === 'stored');
  clear.addEventListener('click', () => guard(async () => { await api.clearClaudeKey(); await reload(); }));
  box.append(
    el('div', { class: 'dev-row-text' }, [
      hintLine(t('agents.claude_no_subscription')),
      field(t('agents.route_label'), routeSel),
      keyField,
      status,
      el('div', { class: 'agents-actions' }, [save, clear]),
    ]));
  paintStatus();
  return box;
}

// ---- Agents list + editor ----

function agentsBody(data, deps, api, guard, reload, toast) {
  const body = el('div', { class: 'dev-group-body' });
  const editorHost = el('div', { class: 'agents-editor' });
  const list = el('div', { class: 'agents-rows' });
  const addBtn = el('button', { type: 'button', class: 'btn-primary', id: 'agents-new', text: t('agents.new') });
  addBtn.addEventListener('click', () => openEditor(blankForm()));
  const profileNames = new Map((data.agents.profiles || []).map((p) => [p.id, p.name]));

  function openEditor(form) {
    editorHost.replaceChildren(editorEl(form, data, deps, api, {
      guard, toast,
      done: async () => { await reload(); },
      cancel: () => { editorHost.replaceChildren(); addBtn.focus(); },
    }));
    const first = editorHost.querySelector('input,select,textarea');
    if (first) first.focus();
  }

  const agents = data.agents.agents;
  if (!agents.length) list.append(el('p', { class: 'muted', text: t('agents.none_yet') }));
  for (const a of agents) {
    const published = (a.published_to || []).includes('*') ? t('agents.published_all')
      : a.published_to.length ? t('agents.published_to', { names: a.published_to.map((id) => profileNames.get(id) || id).join(', ') })
        : t('agents.published_nobody');
    const edit = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.edit'), 'aria-label': t('agents.edit_aria', { name: a.name }) });
    edit.addEventListener('click', () => openEditor(specToForm(a)));
    const del = el('button', { type: 'button', class: 'btn-secondary danger-text', text: t('agents.delete'), 'aria-label': t('agents.delete_aria', { name: a.name }) });
    del.addEventListener('click', () => guard(async () => {
      if (deps.confirm && !(await deps.confirm(t('agents.delete_confirm', { name: a.name }), { danger: true, okText: t('agents.delete') }))) return;
      await api.agentDelete(a.id);
      await reload();
    }));
    list.append(el('div', { class: `dev-row agents-row${a.disabled ? ' agents-off' : ''}`, 'data-agent': a.id }, [
      el('div', { class: 'dev-row-text' }, [
        el('strong', { text: a.name }),
        el('span', { class: 'task-backend', text: a.backend_label || a.backend }),
        a.disabled ? el('span', { class: 'task-pill task-pill-hold', text: t('agents.off') }) : null,
        hintLine(published),
      ]),
      el('div', { class: 'agents-actions' }, [edit, del]),
    ]));
  }
  body.append(list, el('div', { class: 'agents-actions' }, [addBtn]), editorHost);
  return body;
}

function editorEl(form, data, deps, api, ctl) {
  const f = form;
  const owner = !!deps.isOwner;
  const wrap = el('form', { class: 'agents-form', 'aria-label': t(f.id ? 'agents.edit_title' : 'agents.new_title') });
  wrap.addEventListener('submit', (e) => e.preventDefault());
  const bind = (input, key, evt = 'input') => {
    input.value = f[key] ?? '';
    input.addEventListener(evt, () => { f[key] = input.value; });
    return input;
  };
  const backends = (data.backends || []).filter((b) => b.name !== 'fake' || f.backend === 'fake');
  const backendSel = el('select', { class: 'ui-select' }, backends.map((b) => el('option', { value: b.name, text: b.label + (b.available ? '' : ` (${t('agents.backend_needs_setup')})`) })));
  backendSel.value = f.backend;
  const seat = bind(el('input', { type: 'text', maxlength: '64', autocomplete: 'off' }), 'seat');
  const seatField = field(t('agents.seat_label'), seat, t('agents.seat_hint'));
  const paintBackend = () => { seatField.hidden = f.backend !== 'openclaw'; };
  backendSel.addEventListener('change', () => { f.backend = backendSel.value; paintBackend(); });
  paintBackend();

  const tools = bind(el('textarea', { rows: '4', spellcheck: 'false', dir: 'ltr' }), 'tools');
  const readOnly = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.preset_read_only') });
  readOnly.addEventListener('click', () => { addLines(f, 'tools', TOOL_PRESETS.read_only); tools.value = f.tools; });

  const mcpHost = el('div', { class: 'agents-mcp' });
  const paintMcp = () => {
    mcpHost.replaceChildren(...f.mcp.map((m, i) => mcpRow(m, i, f, owner, paintMcp)));
  };
  const sfBtn = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.preset_studioforge') });
  sfBtn.addEventListener('click', () => {
    if (!f.mcp.some((m) => m.name === STUDIOFORGE_PRESET.name)) {
      const s = STUDIOFORGE_PRESET.server;
      f.mcp.push({ name: STUDIOFORGE_PRESET.name, transport: 'http', url: s.url, command: '', args: '', header: 'Authorization', secret: 'STUDIOFORGE_MCP_KEY_RO', privileged: false }); // scrub-ok: the NAME of a secret, not its value
    }
    addLines(f, 'tools', TOOL_PRESETS.studioforge_read_only);
    tools.value = f.tools;
    paintMcp();
  });
  const addMcp = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.add_mcp') });
  addMcp.addEventListener('click', () => { f.mcp.push({ name: '', transport: 'http', url: '', command: '', args: '', header: '', secret: '', privileged: false }); paintMcp(); });
  paintMcp();

  const modeSel = el('select', { class: 'ui-select' }, Object.keys(MODE_LABELS).map((m) => el('option', { value: m, text: t(MODE_LABELS[m]) })));
  modeSel.value = f.approval_mode;
  modeSel.addEventListener('change', () => { f.approval_mode = modeSel.value; });
  const wsSel = el('select', { class: 'ui-select' }, [el('option', { value: 'scratch', text: t('agents.ws_scratch') }), el('option', { value: 'worktree', text: t('agents.ws_worktree') })]);
  wsSel.value = f.workspace_mode;
  wsSel.addEventListener('change', () => { f.workspace_mode = wsSel.value; });

  const trust = checkbox(t('agents.trust_repo'), f.trust_repo_settings, (v) => { f.trust_repo_settings = v; },
    owner ? t('agents.trust_repo_warning') : `${t('agents.trust_repo_warning')} ${t('agents.owner_only')}`, !owner);

  const budgetRow = el('div', { class: 'agents-budget' }, [
    field(t('agents.budget_task'), bind(el('input', { type: 'number', min: '0', step: '0.01', inputmode: 'decimal' }), 'max_usd')),
    field(t('agents.budget_day'), bind(el('input', { type: 'number', min: '0', step: '0.01', inputmode: 'decimal' }), 'max_usd_per_day')),
    field(t('agents.budget_turns'), bind(el('input', { type: 'number', min: '0', step: '1', inputmode: 'numeric' }), 'max_turns')),
    field(t('agents.budget_minutes'), bind(el('input', { type: 'number', min: '0', step: '1', inputmode: 'numeric' }), 'max_minutes')),
    field(t('agents.budget_tokens'), bind(el('input', { type: 'number', min: '0', step: '1', inputmode: 'numeric' }), 'max_tokens')),
  ]);

  const people = (data.agents.profiles || []).filter((p) => p.role !== 'restricted');
  const pubBox = el('fieldset', { class: 'agents-publish' }, [
    el('legend', { text: t('agents.publish_legend') }),
    checkbox(t('agents.publish_all'), f.published_all, (v) => { f.published_all = v; paintPub(); }),
  ]);
  const pubList = el('div', { class: 'agents-publish-list' });
  const paintPub = () => {
    pubList.replaceChildren(...people.map((p) => {
      const on = f.published.includes(p.id);
      const c = checkbox(`${p.name} (${t(`role.${p.role}`)})`, on || f.published_all, (v) => {
        f.published = v ? [...new Set([...f.published, p.id])] : f.published.filter((x) => x !== p.id);
      }, null, f.published_all);
      return c;
    }));
  };
  paintPub();
  pubBox.append(pubList);

  const err = el('p', { class: 'task-error', role: 'alert', hidden: '' });
  const save = el('button', { type: 'submit', class: 'btn-primary', text: t('agents.save') });
  const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('common.cancel') });
  cancel.addEventListener('click', ctl.cancel);
  save.addEventListener('click', async () => {
    const body = formToBody(f);
    if (body.error) { err.hidden = false; err.textContent = t(body.error); return; }
    save.disabled = true;
    err.hidden = true;
    try {
      if (f.id) await api.agentSave(f.id, body);
      else await api.agentCreate(body);
      ctl.toast(t('agents.saved'));
      await ctl.done();
    } catch (e) {
      save.disabled = false;
      err.hidden = false;
      err.textContent = errText(e);
    }
  });

  wrap.append(
    el('h4', { text: t(f.id ? 'agents.edit_title' : 'agents.new_title') }),
    field(t('agents.name'), bind(el('input', { type: 'text', maxlength: '80', required: '' }), 'name')),
    field(t('agents.description'), bind(el('input', { type: 'text', maxlength: '400' }), 'description')),
    field(t('agents.backend'), backendSel, (BACKEND_NOTES[f.backend] && t(BACKEND_NOTES[f.backend])) || ''),
    seatField,
    field(t('agents.model'), bind(el('input', { type: 'text', maxlength: '120', autocomplete: 'off' }), 'model'), t('agents.model_hint')),
    field(t('agents.instructions'), bind(el('textarea', { rows: '5', maxlength: '20000', dir: 'auto' }), 'instructions')),
    field(t('agents.tools'), tools, t('agents.tools_hint')),
    el('div', { class: 'agents-actions' }, [readOnly, sfBtn]),
    el('h5', { text: t('agents.mcp_title') }), mcpHost, el('div', { class: 'agents-actions' }, [addMcp]),
    field(t('agents.workspace'), wsSel),
    trust,
    field(t('agents.approval_mode'), modeSel, t('agents.approval_hint')),
    field(t('agents.ask_tools'), bind(el('textarea', { rows: '2', spellcheck: 'false', dir: 'ltr' }), 'ask_tools'), t('agents.ask_tools_hint')),
    field(t('agents.unattended_allow'), Object.assign(bind(el('textarea', { rows: '2', spellcheck: 'false', dir: 'ltr' }), 'unattended_allow'), { disabled: !owner }), owner ? t('agents.unattended_hint') : t('agents.owner_only')),
    checkbox(t('agents.reads_untrusted'), f.reads_untrusted_content, (v) => { f.reads_untrusted_content = v; }, t('agents.reads_untrusted_hint')),
    el('h5', { text: t('agents.budget_title') }), hintLine(t('agents.budget_hint')), budgetRow,
    pubBox,
    checkbox(t('agents.disabled'), f.disabled, (v) => { f.disabled = v; }),
    err,
    el('div', { class: 'agents-actions' }, [cancel, save]),
  );
  return wrap;
}

function addLines(f, key, extra) {
  const have = new Set(lines(f[key]));
  for (const x of extra) have.add(x);
  f[key] = [...have].join('\n');
}

function mcpRow(m, i, f, owner, repaint) {
  const row = el('div', { class: 'agents-mcp-row' });
  const bindv = (input, key) => { input.value = m[key] ?? ''; input.addEventListener('input', () => { m[key] = input.value; }); return input; };
  // A local program is code execution on this machine: only the Owner may add one.
  const stdioOpt = el('option', { value: 'stdio', text: t('agents.mcp_stdio') });
  stdioOpt.disabled = !owner;
  const transport = el('select', { class: 'ui-select' }, [el('option', { value: 'http', text: 'http' }), stdioOpt]);
  transport.value = m.transport;
  transport.addEventListener('change', () => { m.transport = transport.value; repaint(); });
  const remove = el('button', { type: 'button', class: 'btn-secondary danger-text', text: t('agents.remove_mcp'), 'aria-label': t('agents.remove_mcp_aria', { name: m.name || String(i + 1) }) });
  remove.addEventListener('click', () => { f.mcp.splice(i, 1); repaint(); });
  const kids = [
    field(t('agents.mcp_name'), bindv(el('input', { type: 'text', maxlength: '40' }), 'name')),
    field(t('agents.mcp_transport'), transport),
  ];
  if (m.transport === 'stdio') {
    kids.push(field(t('agents.mcp_command'), bindv(el('input', { type: 'text', dir: 'ltr' }), 'command'), owner ? '' : t('agents.owner_only')));
    kids.push(field(t('agents.mcp_args'), bindv(el('input', { type: 'text', dir: 'ltr' }), 'args')));
  } else {
    kids.push(field(t('agents.mcp_url'), bindv(el('input', { type: 'url', dir: 'ltr' }), 'url')));
    kids.push(field(t('agents.mcp_header'), bindv(el('input', { type: 'text', dir: 'ltr', placeholder: 'Authorization' }), 'header')));
    kids.push(field(t('agents.mcp_secret'), bindv(el('input', { type: 'text', dir: 'ltr', placeholder: 'STUDIOFORGE_MCP_KEY_RO' }), 'secret'), t('agents.mcp_secret_hint')));
  }
  kids.push(checkbox(t('agents.mcp_privileged'), m.privileged, (v) => { m.privileged = v; }, owner ? t('agents.mcp_privileged_hint') : t('agents.owner_only'), !owner));
  kids.push(remove);
  row.append(...kids);
  return row;
}

// ---- Limits, retention, the flag ----

function limitsBody(data, deps, api, guard, reload, toast) {
  const s = data.settings;
  const body = el('div', { class: 'dev-group-body' });
  const retention = el('input', { type: 'number', min: '0', max: '3650', step: '1', inputmode: 'numeric', value: String(s.retention_days) });
  const maxc = el('input', { type: 'number', min: '1', max: '16', step: '1', inputmode: 'numeric', value: String(s.max_concurrent) });
  const unified = checkbox(t('agents.unified'), s.unified_dispatch, () => {}, s.unified_dispatch_from_env ? t('agents.unified_from_env') : t('agents.unified_hint'), s.unified_dispatch_from_env);
  const save = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.save_limits') });
  save.addEventListener('click', () => guard(async () => {
    await api.saveAgentSettings({
      retention_days: Number(retention.value), max_concurrent: Number(maxc.value),
      ...(s.unified_dispatch_from_env ? {} : { unified_dispatch: unified.querySelector('input').checked }),
    });
    toast(t('agents.saved'));
    await reload();
  }));
  const purge = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.purge_now') });
  purge.addEventListener('click', () => guard(async () => {
    const r = await api.purgeAgentTasks();
    toast(t('agents.purged', { count: r.purged }));
  }));
  body.append(
    field(t('agents.retention'), retention, t('agents.retention_hint')),
    field(t('agents.max_concurrent'), maxc),
    unified,
    el('div', { class: 'agents-actions' }, [save, purge]),
    el('h4', { text: t('agents.profile_limits') }),
    hintLine(t('agents.profile_limits_hint')),
  );
  const people = (data.agents.profiles || []).filter((p) => p.role !== 'restricted');
  for (const p of people) body.append(profileBudgetRow(p, data.budgets[p.id] || {}, api, guard, toast));
  const spend = data.usage.spend || [];
  if (spend.length) {
    const names = new Map([...(data.agents.agents || []).map((a) => [a.id, a.name]), ...(data.agents.profiles || []).map((p) => [p.id, p.name])]);
    body.append(el('h4', { text: t('agents.spend_today') }), hintLine(t('agents.spend_hint')),
      el('ul', { class: 'agents-spend' }, spend.map((r) => el('li', {
        text: t('agents.spend_row', { kind: t(r.kind === 'agent' ? 'agents.spend_agent' : 'agents.spend_profile'), name: names.get(r.subject) || r.subject, usd: r.usd.toFixed(4), tasks: r.tasks }),
      }))));
  }
  return body;
}

function profileBudgetRow(p, b, api, guard, toast) {
  const mk = (key, value, step) => el('input', { type: 'number', min: '0', step, inputmode: 'decimal', value: value ?? '' });
  const usd = mk('usd', b.usd_per_day, '0.01');
  const turns = mk('turns', b.max_turns, '1');
  const mins = mk('mins', b.max_wall_clock_s ? Math.round(b.max_wall_clock_s / 60) : '', '1');
  const toks = mk('toks', b.max_tokens, '1');
  const save = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.save_limits'), 'aria-label': t('agents.save_limits_for', { name: p.name }) });
  save.addEventListener('click', () => guard(async () => {
    const num = (i) => { const v = numOrNull(i.value); if (Number.isNaN(v)) throw new Error(t('agents.err_number')); return v; };
    const body = {};
    const set = (k, v) => { if (v != null) body[k] = v; };
    set('usd_per_day', num(usd));
    set('max_turns', num(turns));
    const m = num(mins);
    set('max_wall_clock_s', m == null ? null : m * 60);
    set('max_tokens', num(toks));
    await api.saveAgentProfileBudget(p.id, body);
    toast(t('agents.saved'));
  }));
  return el('fieldset', { class: 'agents-profile-budget' }, [
    el('legend', { text: `${p.name} (${t(`role.${p.role}`)})` }),
    field(t('agents.budget_day'), usd), field(t('agents.budget_turns'), turns),
    field(t('agents.budget_minutes'), mins), field(t('agents.budget_tokens'), toks), save,
  ]);
}

// ---- MCP secrets ----

function secretsBody(data, api, guard, reload, toast) {
  const body = el('div', { class: 'dev-group-body' });
  if (!data.secrets.length) body.append(el('p', { class: 'muted', text: t('agents.secrets_none') }));
  for (const sct of data.secrets) {
    const input = el('input', { type: 'password', autocomplete: 'off', spellcheck: 'false', maxlength: '4096', 'aria-label': t('agents.secret_value_aria', { name: sct.name }) });
    const save = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.secret_set') });
    save.addEventListener('click', () => guard(async () => {
      if (!input.value) return;
      await api.saveAgentSecret(sct.name, input.value);
      input.value = '';
      toast(t('agents.saved'));
      await reload();
    }));
    const del = el('button', { type: 'button', class: 'btn-secondary danger-text', text: t('agents.secret_remove') });
    del.hidden = !sct.set;
    del.addEventListener('click', () => guard(async () => { await api.deleteAgentSecret(sct.name); await reload(); }));
    body.append(el('div', { class: 'dev-row agents-secret', 'data-secret': sct.name }, [
      el('div', { class: 'dev-row-text' }, [
        el('code', { text: sct.name }),
        el('span', { class: `task-pill task-pill-${sct.set ? 'succeeded' : 'hold'}`, text: sct.set ? t('agents.secret_is_set') : t('agents.secret_missing') }),
      ]),
      el('div', { class: 'agents-actions' }, [input, save, del]),
    ]));
  }
  return body;
}
