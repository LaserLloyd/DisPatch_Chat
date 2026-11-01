// Settings -> AI & Connections -> Agents (Admins).
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
//   CLI presets   (4.3 #13) the programs the command-line backend may run: the Owner's alone
//
// The server enforces everything here. This module decides what to OFFER: the capabilities that
// are host-level in effect (running a repository's own hooks, local MCP programs, unattended
// tools, shell/file tools, Bash commands that skip the question, CLI presets) are the main Admin's
// (the Owner, the protected Admin) since 4.3 #30. Every Admin holds admin_system, so that is NOT
// the test: `me.protected` is. A second Admin sees those controls disabled with "Only the Owner
// can ...", and the server answers 403 agent_needs_owner / host_owner_only all the same.
//
// Pure-ish: nothing runs at import time; app-shaped things arrive through `deps`.

import { api as realApi, errorExtras } from './api.js?v=37';
import { t } from './i18n.js?v=5';
import { el, errorText, spriteIcon } from './util.js?v=23';

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
  cli: 'agents.backend_note_cli',
  managed: 'agents.backend_note_managed',
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

// ----------------------------------------------------------------------------- 4.3 calls

// The 4.3 routes (Claude routes, schedules, CLI presets). api.js gains named calls for them in the
// integration pass; until then (and for any api object without them) they go through this small
// fetch wrapper, which reads the same error envelope api.js does (code, message, fix).
async function call(url, opts = {}) {
  const r = await fetch(url, { credentials: 'same-origin', ...opts });
  if (r.ok) return r.status === 204 ? null : r.json();
  let body = null;
  try { body = await r.json(); } catch { /* not JSON */ }
  const extras = errorExtras(body, r.headers);
  const detail = body && typeof body.detail === 'string' ? body.detail : (extras.userMessage || r.statusText || 'Request failed');
  const err = new Error(`${r.status}: ${detail}`);
  err.status = r.status;
  err.body = body;
  Object.assign(err, extras);
  throw err;
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };
const jsonBody = (method, body) => ({ method, headers: JSON_HEADERS, body: JSON.stringify(body) });

/** The 4.3 agent calls on `api`, falling back to the wrapper above for any it lacks. */
export function agentApi(api = realApi) {
  const pick = (name, fallback) => (api && typeof api[name] === 'function' ? api[name].bind(api) : fallback);
  return {
    claudeRoutes: pick('claudeRoutes', () => call('/api/agents/claude/routes')),
    cliPresets: pick('cliPresets', () => call('/api/agents/cli-presets')),
    saveCliPresets: pick('saveCliPresets', (presets) => call('/api/agents/cli-presets', jsonBody('PUT', { presets }))),
    schedules: pick('agentSchedules', (scope = 'mine') => call(`/api/agents/schedules?scope=${encodeURIComponent(scope)}`)),
    createSchedule: pick('createAgentSchedule', (body) => call('/api/agents/schedules', jsonBody('POST', body))),
    patchSchedule: pick('patchAgentSchedule', (id, body) => call(`/api/agents/schedules/${encodeURIComponent(id)}`, jsonBody('PATCH', body))),
    deleteSchedule: pick('deleteAgentSchedule', (id) => call(`/api/agents/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' })),
  };
}

/** Is the person the main Admin (the Owner)? `deps.isProtected` when the shell knows; otherwise
 *  GET /api/auth/me; otherwise (an api without `me`, as in old tests) `deps.isOwner`. Fails
 *  closed: an unreadable answer offers nothing host-level (the server would refuse it anyway). */
export async function resolveOwner(api, deps = {}) {
  if (typeof deps.isProtected === 'boolean') return deps.isProtected;
  if (api && typeof api.me === 'function') {
    try { const me = await api.me(); return !!(me && me.protected); } catch { return false; }
  }
  return !!deps.isOwner;
}

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
    approval_mode: 'interactive', ask_tools: '', unattended_allow: '', bash_allow: '',
    preset: '', managed_agent: '', managed_env: '',
    max_usd: '', max_usd_per_day: '', max_turns: '', max_minutes: '', max_tokens: '',
    reads_untrusted_content: false, published_all: false, published: [], disabled: false,
  };
}

/** The form a stored agent edits as. */
export function specToForm(agent) {
  const s = agent.spec || {};
  const b = s.budget || {};
  const f = blankForm(s.backend || 'openclaw');
  const opts = s.backend_options || {};
  const managed = s.backend === 'managed';
  return Object.assign(f, {
    id: agent.id, base: s, name: agent.name, description: agent.description || '',
    seat: managed ? '' : opts.agent_id || '',
    preset: opts.preset || '',
    managed_agent: managed ? opts.agent_id || '' : '',
    managed_env: opts.environment_id || '',
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
    bash_allow: ((s.approval_policy || {}).bash_allow || []).join('\n'),
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
 * the lot: quarantine rule, secret names, the Owner-only (protected Admin) grants).
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
      // One pattern per line (a command prefix such as `git status`): never split on commas,
      // which a shell command may contain.
      bash_allow: String(f.bash_allow || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean),
    },
    budget,
    reads_untrusted_content: !!f.reads_untrusted_content,
    instructions: f.instructions,
  };
  delete spec.name;                                   // the server sets it from the agent's name
  if (f.model.trim()) spec.model = f.model.trim(); else delete spec.model;
  // Backend options belong to one backend: a switch starts from none (an OpenClaw seat must not
  // become a Managed agent id), and a field the person cleared is dropped, not kept from before.
  const opts = base.backend && base.backend !== f.backend ? {} : { ...(base.backend_options || {}) };
  const setOpt = (k, v) => { const t = String(v || '').trim(); if (t) opts[k] = t; else delete opts[k]; };
  if (f.backend === 'openclaw') {
    setOpt('agent_id', f.seat);
  } else if (f.backend === 'cli') {
    setOpt('preset', f.preset);
  } else if (f.backend === 'managed') {
    setOpt('agent_id', f.managed_agent);
    setOpt('environment_id', f.managed_env);
  }
  spec.backend_options = opts;
  const published = f.published_all ? ['*'] : (f.published || []);
  return { name: f.name.trim(), description: f.description.trim(), spec, published_to: published, disabled: !!f.disabled };
}

/**
 * The route picker's rows from GET /api/agents/claude/routes (4.3 #9): a cloud route whose
 * variables are not set on this machine is offered disabled, saying what it needs. The API-key
 * route is never disabled: choosing it is how the key gets typed in. `res` null (an older server,
 * or the call failed) = every route offered, as before.
 */
export function routeOptions(routes, res) {
  const by = new Map(((res && res.routes) || []).map((r) => [r.route, r]));
  return (routes || []).map((route) => {
    const r = by.get(route);
    const missing = r && !r.available ? (r.missing || []) : [];
    const disabled = route !== 'api_key' && !!r && !r.available;
    const label = t(ROUTE_LABELS[route] || route);
    const needs = missing.length ? t('agents.route_needs', { names: missing.join(', ') }) : '';
    // The option itself stays short (a phone's picker is narrow): the first thing it needs, then
    // "…"; the whole list is its tooltip and the list under the picker.
    const short = missing.length ? t('agents.route_needs', { names: missing.length > 1 ? `${missing[0]}, …` : missing[0] }) : '';
    return { route, label, disabled, missing, needs, text: disabled ? `${label} (${short})` : label };
  });
}

/** The four 4.3 limits (#10/#11) as the panel shows them: money in dollars, the approval timeout
 *  in minutes (the API keeps seconds). */
export const LIMIT_FIELDS = [
  { key: 'default_max_usd', label: 'agents.default_max_usd', unit: 'agents.unit_usd_task', help: 'agents.default_max_usd_help', step: '0.01', min: 0.01, max: 1000 },
  { key: 'default_max_turns', label: 'agents.default_max_turns', unit: 'agents.unit_turns', help: 'agents.default_max_turns_help', step: '1', min: 1, max: 1000, whole: true },
  { key: 'default_max_usd_per_day', label: 'agents.default_max_usd_per_day', unit: 'agents.unit_usd_day', help: 'agents.default_max_usd_per_day_help', step: '0.01', min: 0.01, max: 1000 },
  { key: 'approval_timeout_s', label: 'agents.approval_timeout', unit: 'agents.unit_minutes', help: 'agents.approval_timeout_help', step: '1', min: 1, max: 1440, whole: true, minutes: true },
];

/** What the panel shows for a stored setting (minutes for the timeout). */
export function limitShown(field, settings) {
  const v = settings && settings[field.key];
  if (v == null || v === '') return '';
  return String(field.minutes ? Math.round(Number(v) / 60) : v);
}

/**
 * The changed limits as PUT /api/agents/settings fields, or {error} naming the field a person
 * mistyped. Only what changed is sent, so saving the other limits never writes these defaults
 * into the store (the server's own defaults stay in charge of an untouched install).
 */
export function limitChanges(values, settings) {
  const out = {};
  for (const f of LIMIT_FIELDS) {
    const raw = String(values[f.key] ?? '').trim();
    if (raw === limitShown(f, settings)) continue;
    const n = Number(raw);
    if (!raw || !Number.isFinite(n) || n < f.min || n > f.max || (f.whole && !Number.isInteger(n))) {
      return { error: t('agents.limit_invalid', { name: t(f.label), min: f.min, max: f.max, unit: t(f.unit) }) };
    }
    out[f.key] = f.minutes ? n * 60 : n;
  }
  return out;
}

/** CLI presets (4.3 #13) to and from the editor's text fields. */
export function presetToForm(p) {
  return {
    id: p.id || '', label: p.label || '', kind: p.kind || 'generic', executable: p.executable || '',
    args: (p.args || []).join('\n'), resume_args: (p.resume_args || []).join('\n'),
    env_secrets: Object.entries(p.env_secrets || {}).map(([k, v]) => `${k}=${v}`).join('\n'),
  };
}

export function formToPreset(f) {
  const list = (v) => String(v || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
  const out = { id: String(f.id || '').trim(), label: String(f.label || '').trim(), kind: f.kind || 'generic', executable: String(f.executable || '').trim() };
  if (out.kind !== 'claude') {
    out.args = list(f.args);
    const resume = list(f.resume_args);
    if (resume.length) out.resume_args = resume;
  }
  const env = {};
  for (const line of list(f.env_secrets)) {
    const i = line.indexOf('=');
    if (i > 0) env[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  if (Object.keys(env).length) out.env_secrets = env;
  if (!out.label) delete out.label;
  return out;
}

// ----------------------------------------------------------------------------- the pane

let seq = 0;

const errText = errorText;

const hintLine = (text, cls = '') => el('span', { class: `dev-row-hint${cls ? ` ${cls}` : ''}`, text });

/** "Schedules are on the Tasks page": the way from here to where jobs are given and repeated. */
function tasksLink(deps) {
  if (typeof deps.openTasks !== 'function') return null;
  const b = el('button', { type: 'button', class: 'text-link-btn agents-tasks-link', text: t('agents.tasks_link') });
  b.addEventListener('click', () => deps.openTasks());
  return b;
}

function group(id, title, hint, body, link = null) {
  return el('section', { class: 'dev-group', id, 'aria-labelledby': `${id}-h` }, [
    el('h3', { class: 'dev-group-title', id: `${id}-h`, text: title }),
    hint ? el('p', { class: 'dev-group-hint', text: hint }) : null,
    link,
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
    const [agents, backends, creds, settings, usage, secrets] = await Promise.all([
      api.agents(), api.agentBackends(), api.claudeCredentials(), api.agentSettings(),
      api.agentUsage(), api.agentSecrets(),
    ]);
    data = { agents, backends: backends.backends, creds, settings, usage, secrets: secrets.secrets };
  } catch (e) {
    host.replaceChildren(el('p', { class: 'task-error', role: 'alert', text: errText(e) }));
    return;
  }
  // 4.3: who may grant host-level powers, which Claude routes can sign in here, and (the Owner
  // only: the route answers 403 to anyone else) the CLI presets. None of these may break the pane.
  const extra = agentApi(api);
  const [owner, routes] = await Promise.all([resolveOwner(api, deps), extra.claudeRoutes().catch(() => null)]);
  data.owner = owner;
  data.routes = routes;
  data.presets = null;
  if (owner) {
    try { data.presets = (await extra.cliPresets()).presets || []; } catch (e) { data.presetsError = errText(e); }
  }
  if (mine !== seq) return;
  deps = { ...deps, isOwner: owner };

  const toast = (m, bad) => deps.toast && deps.toast(m, bad);
  const reload = () => mountAgentsSettings(host, deps);
  const guard = async (fn) => {
    try { await fn(); } catch (e) { toast(errText(e), true); }
  };

  host.replaceChildren(
    group('agents-backends', t('agents.backends_title'), t('agents.backends_hint'), backendsBody(data, deps, api, guard, reload, toast)),
    group('agents-list', t('agents.list_title'), t('agents.list_hint'), agentsBody(data, deps, api, guard, reload, toast), tasksLink(deps)),
    group('agents-limits', t('agents.limits_title'), t('agents.limits_hint'), limitsBody(data, deps, api, guard, reload, toast)),
    group('agents-secrets', t('agents.secrets_title'), t('agents.secrets_hint'), secretsBody(data, api, guard, reload, toast)),
    group('agents-cli', t('agents.cli_title'), t('agents.cli_hint'), cliBody(data, extra, guard, reload, toast)),
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
  // 4.3 #9: a cloud route this machine cannot sign in with is shown, disabled, with what it needs.
  const opts = routeOptions(c.routes, data.routes);
  const routeSel = el('select', { id: `${name}-route`, class: 'ui-select' }, opts.map((o) => {
    const opt = el('option', { value: o.route, text: o.text });
    if (o.disabled) { opt.disabled = true; opt.title = o.needs; }
    return opt;
  }));
  routeSel.value = c.route;
  const needs = opts.filter((o) => o.disabled);
  const needsBox = needs.length ? el('ul', { class: 'agents-route-needs', 'aria-label': t('agents.route_needs_title') },
    needs.map((o) => el('li', { 'data-route': o.route }, [
      spriteIcon('lock', 14), ' ', el('strong', { text: o.label }), ` ${o.needs}`,
    ]))) : null;
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
      field(t('agents.route_label'), routeSel, needs.length ? t('agents.route_needs_hint') : ''),
      needsBox ? el('details', { class: 'agents-route-details' }, [el('summary', { text: t('agents.show_details') }), needsBox]) : null,
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
  // Nothing yet: one centred box that says what an agent is, with the button inside it.
  if (!agents.length) {
    list.append(el('div', { class: 'agents-empty' }, [
      el('p', { class: 'agents-empty-title', text: t('agents.none_yet') }),
      el('p', { class: 'muted', text: t('agents.none_hint') }),
      addBtn,
    ]));
  }
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
  body.append(...[list, agents.length ? el('div', { class: 'agents-actions' }, [addBtn]) : null, editorHost].filter(Boolean));
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
  // 4.3 #13: a command-line agent names one of the Owner's presets; a Managed agent names the
  // agent and environment made in the Anthropic Console.
  const presetIds = (data.presets || []).map((p) => p.id);
  const presetInput = bind(el('input', { type: 'text', maxlength: '40', autocomplete: 'off', spellcheck: 'false', dir: 'ltr', list: presetIds.length ? 'agents-preset-ids' : null }), 'preset');
  const presetField = field(t('agents.preset_label'), presetInput, t('agents.preset_hint'));
  if (presetIds.length) presetField.append(el('datalist', { id: 'agents-preset-ids' }, presetIds.map((id) => el('option', { value: id }))));
  const managedAgent = field(t('agents.managed_agent_label'), bind(el('input', { type: 'text', maxlength: '120', autocomplete: 'off', spellcheck: 'false', dir: 'ltr' }), 'managed_agent'), t('agents.managed_agent_hint'));
  const managedEnv = field(t('agents.managed_env_label'), bind(el('input', { type: 'text', maxlength: '120', autocomplete: 'off', spellcheck: 'false', dir: 'ltr' }), 'managed_env'));
  const paintBackend = () => {
    seatField.hidden = f.backend !== 'openclaw';
    presetField.hidden = f.backend !== 'cli';
    managedAgent.hidden = managedEnv.hidden = f.backend !== 'managed';
  };
  // The note under the Backend select follows the choice (it used to keep the first one's words).
  const backendField = field(t('agents.backend'), backendSel, ' ');
  const backendNote = backendField.querySelector('.people-field-hint');
  const paintNote = () => {
    const key = BACKEND_NOTES[f.backend];
    backendNote.textContent = key ? t(key) : '';
    backendNote.hidden = !key;
  };
  backendSel.addEventListener('change', () => { f.backend = backendSel.value; paintBackend(); paintNote(); });
  paintBackend();
  paintNote();

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
    owner ? t('agents.trust_repo_warning') : `${t('agents.trust_repo_warning')} ${t('agents.only_owner')}`, !owner);

  // 4.3 #10: an empty box uses the default from the Limits group - say which number that is.
  const st = data.settings || {};
  const dflt = (v, money) => (v == null || v === '' ? {} : { placeholder: t('agents.budget_default', { value: money ? `$${Number(v).toFixed(2)}` : String(v) }) });
  const budgetRow = el('div', { class: 'agents-budget' }, [
    field(t('agents.budget_task'), bind(el('input', { type: 'number', min: '0', step: '0.01', inputmode: 'decimal', ...dflt(st.default_max_usd, true) }), 'max_usd')),
    field(t('agents.budget_day'), bind(el('input', { type: 'number', min: '0', step: '0.01', inputmode: 'decimal', ...dflt(st.default_max_usd_per_day, true) }), 'max_usd_per_day')),
    field(t('agents.budget_turns'), bind(el('input', { type: 'number', min: '0', step: '1', inputmode: 'numeric', ...dflt(st.default_max_turns) }), 'max_turns')),
    field(t('agents.budget_minutes'), bind(el('input', { type: 'number', min: '0', step: '1', inputmode: 'numeric' }), 'max_minutes')),
    field(t('agents.budget_tokens'), bind(el('input', { type: 'number', min: '0', step: '1', inputmode: 'numeric' }), 'max_tokens')),
  ]);

  // 4.3 #8/#30: Bash commands that may run without asking. Each pattern is a host-level grant, so
  // only the Owner may add one; a second Admin sees the list (it is kept on save) but cannot edit it.
  const bashInput = bind(el('textarea', { rows: '3', spellcheck: 'false', dir: 'ltr', class: 'agents-bash-allow', placeholder: 'git status\nnpm test' }), 'bash_allow');
  bashInput.disabled = !owner;
  if (!owner) bashInput.title = t('agents.owner_only_bash');
  const bashAllow = field(t('agents.bash_allow'), bashInput, owner ? t('agents.bash_allow_hint') : t('agents.owner_only_bash'));
  bashAllow.classList.add('agents-bash-field');
  if (!owner) bashAllow.querySelector('label').after(el('span', { class: 'agents-owner-badge', title: t('agents.owner_only_bash') }, [spriteIcon('lock', 14), ` ${t('agents.owner_badge')}`]));

  const people = data.agents.profiles || [];
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
    backendField,
    seatField, presetField, managedAgent, managedEnv,
    field(t('agents.model'), bind(el('input', { type: 'text', maxlength: '120', autocomplete: 'off' }), 'model'), t('agents.model_hint')),
    field(t('agents.instructions'), bind(el('textarea', { rows: '5', maxlength: '20000', dir: 'auto' }), 'instructions')),
    field(t('agents.tools'), tools, owner ? t('agents.tools_hint') : `${t('agents.tools_hint')} ${t('agents.owner_only_shell')}`),
    el('div', { class: 'agents-actions' }, [readOnly, sfBtn]),
    el('h5', { text: t('agents.mcp_title') }), mcpHost, el('div', { class: 'agents-actions' }, [addMcp]),
    field(t('agents.workspace'), wsSel),
    trust,
    field(t('agents.approval_mode'), modeSel, t('agents.approval_hint')),
    field(t('agents.ask_tools'), bind(el('textarea', { rows: '2', spellcheck: 'false', dir: 'ltr' }), 'ask_tools'), t('agents.ask_tools_hint')),
    field(t('agents.unattended_allow'), Object.assign(bind(el('textarea', { rows: '2', spellcheck: 'false', dir: 'ltr' }), 'unattended_allow'), { disabled: !owner }), owner ? t('agents.unattended_hint') : t('agents.owner_only_unattended')),
    bashAllow,
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
  // A local program is code execution on this machine: only the Owner (protected Admin) may add one.
  const stdioOpt = el('option', { value: 'stdio', text: t('agents.mcp_stdio') });
  stdioOpt.disabled = !owner;
  const transport = el('select', { class: 'ui-select' }, [el('option', { value: 'http', text: t('agents.mcp_http') }), stdioOpt]);
  transport.value = m.transport;
  transport.addEventListener('change', () => { m.transport = transport.value; repaint(); });
  const remove = el('button', { type: 'button', class: 'btn-secondary danger-text', text: t('agents.remove_mcp'), 'aria-label': t('agents.remove_mcp_aria', { name: m.name || String(i + 1) }) });
  remove.addEventListener('click', () => { f.mcp.splice(i, 1); repaint(); });
  const kids = [
    field(t('agents.mcp_name'), bindv(el('input', { type: 'text', maxlength: '40' }), 'name')),
    field(t('agents.mcp_transport'), transport),
  ];
  if (m.transport === 'stdio') {
    kids.push(field(t('agents.mcp_command'), Object.assign(bindv(el('input', { type: 'text', dir: 'ltr' }), 'command'), { disabled: !owner }), owner ? '' : t('agents.only_owner')));
    kids.push(field(t('agents.mcp_args'), Object.assign(bindv(el('input', { type: 'text', dir: 'ltr' }), 'args'), { disabled: !owner })));
  } else {
    kids.push(field(t('agents.mcp_url'), bindv(el('input', { type: 'url', dir: 'ltr' }), 'url')));
    kids.push(field(t('agents.mcp_header'), bindv(el('input', { type: 'text', dir: 'ltr', placeholder: 'Authorization' }), 'header')));
    kids.push(field(t('agents.mcp_secret'), bindv(el('input', { type: 'text', dir: 'ltr', placeholder: 'STUDIOFORGE_MCP_KEY_RO' }), 'secret'), t('agents.mcp_secret_hint')));
  }
  kids.push(checkbox(t('agents.mcp_privileged'), m.privileged, (v) => { m.privileged = v; }, owner ? t('agents.mcp_privileged_hint') : t('agents.only_owner'), !owner));
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
  const purge = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.purge_now') });
  purge.addEventListener('click', () => guard(async () => {
    const r = await api.purgeAgentTasks();
    toast(t('agents.purged', { count: r.purged }));
  }));
  // 4.3 #10/#11: the defaults every agent TASK gets (an agent's own budget wins), with units.
  const limitInputs = {};
  const limitRows = LIMIT_FIELDS.map((lf) => {
    const input = el('input', {
      type: 'number', min: String(lf.min), max: String(lf.max), step: lf.step,
      inputmode: lf.whole ? 'numeric' : 'decimal', value: limitShown(lf, s), 'data-limit': lf.key,
    });
    limitInputs[lf.key] = input;
    const row = field(t(lf.label), input, t(lf.help));
    row.classList.add('agents-limit');
    // The unit sits beside the box ("$ per task", "turns", ...) and is part of its name.
    const unit = el('span', { class: 'agents-unit', id: `${input.id}-unit`, text: t(lf.unit) });
    input.after(unit);
    input.setAttribute('aria-describedby', `${input.id}-unit ${input.getAttribute('aria-describedby') || ''}`.trim());
    return row;
  });
  const limitErr = el('p', { class: 'task-error', role: 'alert', hidden: '' });
  const save = el('button', { type: 'button', class: 'btn-secondary', text: t('agents.save_limits') });
  save.addEventListener('click', () => guard(async () => {
    const values = Object.fromEntries(Object.entries(limitInputs).map(([k, i]) => [k, i.value]));
    const changed = limitChanges(values, s);
    limitErr.hidden = !changed.error;
    if (changed.error) { limitErr.textContent = changed.error; return; }
    await api.saveAgentSettings({
      retention_days: Number(retention.value), max_concurrent: Number(maxc.value),
      ...(s.unified_dispatch_from_env ? {} : { unified_dispatch: unified.querySelector('input').checked }),
      ...changed,
    });
    toast(t('agents.saved'));
    await reload();
  }));
  body.append(
    el('h4', { text: t('agents.defaults_title') }),
    hintLine(t('agents.defaults_hint')),
    el('div', { class: 'agents-limits-grid' }, limitRows),
    el('h4', { text: t('agents.housekeeping_title') }),
    field(t('agents.retention'), retention, t('agents.retention_hint')),
    field(t('agents.max_concurrent'), maxc),
    unified,
    limitErr,
    el('div', { class: 'agents-actions' }, [save, purge]),
  );
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

// ---- CLI presets (4.3 #13: the Owner's alone) ----

function cliBody(data, extra, guard, reload, toast) {
  const body = el('div', { class: 'dev-group-body agents-cli' });
  if (!data.owner) {
    // A second Admin is told what this is and who can change it - and offered nothing to press:
    // the route answers 403 host_owner_only to anyone but the Owner.
    body.append(el('p', { class: 'agents-owner-note', role: 'note' }, [
      spriteIcon('lock', 14), ' ', el('span', { text: t('agents.owner_only_cli') }),
    ]));
    const add = el('button', { type: 'button', class: 'btn-secondary', disabled: '', title: t('agents.owner_only_cli'), text: t('agents.cli_add') });
    body.append(el('div', { class: 'agents-actions' }, [add]));
    return body;
  }
  if (data.presetsError) {
    body.append(el('p', { class: 'task-error', role: 'alert', text: data.presetsError }));
    return body;
  }
  const rows = (data.presets || []).map(presetToForm);
  const list = el('div', { class: 'agents-cli-rows' });
  const err = el('p', { class: 'task-error', role: 'alert', hidden: '' });

  const paint = () => {
    list.replaceChildren(...(rows.length ? rows.map((r, i) => presetRow(r, i)) : [el('p', { class: 'muted', text: t('agents.cli_none') })]));
  };

  function presetRow(r, i) {
    const bindv = (input, key) => { input.value = r[key] ?? ''; input.addEventListener('input', () => { r[key] = input.value; }); return input; };
    const kind = el('select', { class: 'ui-select' }, [
      el('option', { value: 'generic', text: t('agents.cli_kind_generic') }),
      el('option', { value: 'claude', text: t('agents.cli_kind_claude') }),
    ]);
    kind.value = r.kind;
    kind.addEventListener('change', () => { r.kind = kind.value; paint(); });
    const name = r.label || r.id || String(i + 1);
    const remove = el('button', {
      type: 'button', class: 'btn-secondary danger-text', title: t('agents.cli_remove_aria', { name }),
      'aria-label': t('agents.cli_remove_aria', { name }),
    }, [spriteIcon('trash', 14), ` ${t('agents.cli_remove')}`]);
    remove.addEventListener('click', () => { rows.splice(i, 1); paint(); });
    const kids = [
      field(t('agents.cli_id'), bindv(el('input', { type: 'text', maxlength: '40', autocomplete: 'off', spellcheck: 'false', dir: 'ltr', required: '' }), 'id'), t('agents.cli_id_hint')),
      field(t('agents.cli_label'), bindv(el('input', { type: 'text', maxlength: '80' }), 'label')),
      field(t('agents.cli_kind'), kind),
      field(t('agents.cli_executable'), bindv(el('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', dir: 'ltr', placeholder: '/usr/local/bin/my-agent' }), 'executable'), t('agents.cli_executable_hint')),
    ];
    if (r.kind !== 'claude') {
      kids.push(field(t('agents.cli_args'), bindv(el('textarea', { rows: '3', spellcheck: 'false', dir: 'ltr' }), 'args'), t('agents.cli_args_hint')));
      kids.push(field(t('agents.cli_resume_args'), bindv(el('textarea', { rows: '2', spellcheck: 'false', dir: 'ltr' }), 'resume_args'), t('agents.cli_resume_hint')));
    }
    kids.push(field(t('agents.cli_env'), bindv(el('textarea', { rows: '2', spellcheck: 'false', dir: 'ltr', placeholder: 'MY_TOKEN=MCP_MY_TOKEN' }), 'env_secrets'), t('agents.cli_env_hint')));
    kids.push(el('div', { class: 'agents-actions' }, [remove]));
    return el('fieldset', { class: 'agents-cli-row', 'data-preset': r.id }, [el('legend', { text: name }), ...kids]);
  }

  const add = el('button', { type: 'button', class: 'btn-secondary', title: t('agents.cli_add') }, [spriteIcon('plus', 14), ` ${t('agents.cli_add')}`]);
  add.addEventListener('click', () => {
    rows.push({ id: '', label: '', kind: 'generic', executable: '', args: '', resume_args: '', env_secrets: '' });
    paint();
    const last = list.querySelector('.agents-cli-row:last-child input');
    if (last) last.focus();
  });
  const save = el('button', { type: 'button', class: 'btn-primary', title: t('agents.cli_save') }, [spriteIcon('save', 14), ` ${t('agents.cli_save')}`]);
  save.addEventListener('click', async () => {
    err.hidden = true;
    save.disabled = true;
    try {
      await extra.saveCliPresets(rows.map(formToPreset));
      toast(t('agents.saved'));
      await reload();
    } catch (e) {
      err.hidden = false;
      err.textContent = errText(e);
    } finally { save.disabled = false; }
  });
  void guard;
  paint();
  body.append(list, err, el('div', { class: 'agents-actions' }, [add, save]));
  return body;
}
