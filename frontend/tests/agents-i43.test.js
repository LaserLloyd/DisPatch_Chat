// DisPatch 4.3 (WP-F3) -- the agents UI for #9, #10/#11, #13, #30/#31.
//
//   * Claude routes that cannot sign in here are offered disabled, saying what they need (#9)
//   * the four default limits with units and help, saved only when changed (#10/#11)
//   * a task waiting for an OK says how long is left (#11)
//   * schedules: the editor's every-N-minutes / daily / weekly rules become the POST body,
//     with a next-run preview (#13); CLI presets are the Owner's alone (#13)
//   * a second Admin ({protected:false}) sees the Owner-only controls disabled with
//     "Only the Owner ...", and is never sent to a host-only route (#30/#31, A14)
//   * tasks.js no longer reads an 'owner' role (since 4.0 the Owner is the protected Admin)

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from './_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');
const EN = JSON.parse(read('locales/en.json'));
const MAIN = read('js/main.js');

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><body><button id="tasks-btn" class="gear-btn hidden"></button><div id="host"></div></body>',
    { url: 'http://127.0.0.1:18890/', pretendToBeVisual: true }).window;
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win);
  put('document', win.document);
  put('location', win.location);
  put('navigator', win.navigator);
  put('localStorage', win.localStorage);
  put('Node', win.Node);
  put('HTMLElement', win.HTMLElement);
  put('requestAnimationFrame', (fn) => setTimeout(fn, 0));
  put('cancelAnimationFrame', (id) => clearTimeout(id));
  globalThis.fetch = async (u) => {
    if (String(u).endsWith('/static/locales/en.json')) {
      return { ok: true, status: 200, json: async () => EN, headers: new win.Headers() };
    }
    return { ok: false, status: 404, json: async () => ({}), headers: new win.Headers() };
  };
  const i18n = await import('../static/js/i18n.js?v=5');
  await i18n.init('en');
  return win;
}

const ROUTES = {
  current: 'api_key',
  routes: [
    { route: 'api_key', available: false, missing: ['ANTHROPIC_API_KEY'] },
    { route: 'bedrock', available: false, missing: ['AWS_REGION or AWS_DEFAULT_REGION', 'AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY'] },
    { route: 'google', available: true, missing: [] },
    { route: 'foundry', available: false, missing: ['ANTHROPIC_FOUNDRY_RESOURCE'] },
  ],
};

function agentsApi({ protectedAdmin = true, settings = {}, presets = [] } = {}) {
  const calls = [];
  const rec = (name, ret) => async (...a) => { calls.push([name, ...a]); return typeof ret === 'function' ? ret(...a) : ret; };
  return {
    calls,
    me: rec('me', { role: 'admin', protected: protectedAdmin, permissions: ['manage_agents', 'dispatch_agents', 'approve_agent_actions'] }),
    agents: rec('agents', { agents: [], profiles: [{ id: 'p1', name: 'Sam', role: 'admin' }] }),
    agentBackends: rec('agentBackends', { backends: [
      { name: 'openclaw', label: 'OpenClaw', available: true, capabilities: {} },
      { name: 'claude_sdk', label: 'Claude Agent', available: false, capabilities: {} },
      { name: 'cli', label: 'Command-line agent', available: true, capabilities: {} },
    ] }),
    claudeCredentials: rec('claudeCredentials', { route: 'api_key', routes: ['api_key', 'bedrock', 'google', 'foundry'], api_key_set: false, api_key_source: null, provider_env: '' }),
    agentSettings: rec('agentSettings', {
      unified_dispatch: false, unified_dispatch_from_env: false, retention_days: 30, max_concurrent: 4,
      default_max_usd: 2, default_max_turns: 60, default_max_usd_per_day: 10, approval_timeout_s: 1800, ...settings,
    }),
    agentUsage: rec('agentUsage', { day: '2026-10-31', estimate: true, spend: [] }),
    agentSecrets: rec('agentSecrets', { secrets: [] }),
    saveAgentSettings: rec('saveAgentSettings', {}),
    agentCreate: rec('agentCreate', {}),
    claudeRoutes: rec('claudeRoutes', ROUTES),
    cliPresets: rec('cliPresets', () => {
      if (!protectedAdmin) { const e = new Error('403: Only the Owner can do this.'); e.status = 403; throw e; }
      return { presets };
    }),
    saveCliPresets: rec('saveCliPresets', (p) => ({ presets: p })),
  };
}

async function mountAgents(opts) {
  const win = await boot();
  const A = await import('../static/js/agents.js?v=4');
  const api = agentsApi(opts);
  const host = win.document.getElementById('host');
  const toasts = [];
  // main.js passes isOwner = "any Admin": the pane must not trust it for host-level powers.
  await A.mountAgentsSettings(host, { api, isOwner: true, toast: (m, e) => toasts.push([m, !!e]), confirm: async () => true });
  await tick(5);
  return { win, host, api, A, toasts };
}

const fire = (win, node, type = 'input') => node.dispatchEvent(new win.Event(type, { bubbles: true }));

// ------------------------------------------------------------------ #9 routes

test('Claude routes: an unavailable cloud route is disabled and says what it needs; the API key route never is', { skip: dom.skip }, async () => {
  const { host, api } = await mountAgents();
  assert.ok(api.calls.some((c) => c[0] === 'claudeRoutes'), 'the pane asks GET /api/agents/claude/routes');
  const sel = host.querySelector('.agents-claude select');
  const byVal = Object.fromEntries([...sel.options].map((o) => [o.value, o]));
  assert.equal(byVal.api_key.disabled, false, 'choosing the API key route is how the key gets typed');
  assert.equal(byVal.google.disabled, false);
  assert.equal(byVal.bedrock.disabled, true);
  assert.equal(byVal.foundry.disabled, true);
  assert.equal(byVal.bedrock.textContent, 'Amazon Bedrock (Needs: AWS_REGION or AWS_DEFAULT_REGION, …)', 'short in the picker');
  assert.equal(byVal.foundry.textContent, 'Microsoft Foundry (Needs: ANTHROPIC_FOUNDRY_RESOURCE)');
  assert.equal(byVal.bedrock.title, 'Needs: AWS_REGION or AWS_DEFAULT_REGION, AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY');
  const needs = host.querySelector('.agents-route-needs');
  assert.ok(needs, 'a visible list says what the greyed-out routes need');
  assert.deepEqual([...needs.querySelectorAll('li')].map((li) => li.dataset.route), ['bedrock', 'foundry']);
  assert.match(needs.textContent, /Microsoft Foundry Needs: ANTHROPIC_FOUNDRY_RESOURCE/);
  assert.doesNotMatch(host.textContent, /sk-ant/);
});

test('Claude routes: when the routes call fails the four routes are offered as before', { skip: dom.skip }, async () => {
  const win = await boot();
  const A = await import('../static/js/agents.js?v=4');
  const opts = A.routeOptions(['api_key', 'bedrock'], null);
  assert.deepEqual(opts.map((o) => [o.route, o.disabled, o.text]), [['api_key', false, 'Anthropic API key'], ['bedrock', false, 'Amazon Bedrock']]);
  void win;
});

// ------------------------------------------------------------------ #10/#11 limits

test('Limits: the four defaults show with units and plain help, and only a changed one is saved', { skip: dom.skip }, async () => {
  const { win, host, api } = await mountAgents();
  const lim = host.querySelector('#agents-limits');
  const rows = [...lim.querySelectorAll('.agents-limit')];
  assert.equal(rows.length, 4);
  const units = rows.map((r) => r.querySelector('.agents-unit').textContent);
  assert.deepEqual(units, ['$ per task', 'steps', '$ per day', 'minutes']);
  const val = (k) => lim.querySelector(`[data-limit="${k}"]`);
  assert.equal(val('default_max_usd').value, '2');
  assert.equal(val('default_max_turns').value, '60');
  assert.equal(val('default_max_usd_per_day').value, '10');
  assert.equal(val('approval_timeout_s').value, '30', 'the timeout shows in minutes');
  for (const r of rows) {
    const input = r.querySelector('input');
    assert.ok(r.querySelector('label').textContent.trim(), 'every limit has a visible label');
    assert.ok(r.querySelector('.people-field-hint').textContent.length > 30, 'and a sentence of help');
    assert.match(input.getAttribute('aria-describedby'), /-unit/, 'the unit is part of what a screen reader hears');
  }
  assert.match(lim.textContent, /Chats with bots are never limited by them/);
  val('approval_timeout_s').value = '45';
  fire(win, val('approval_timeout_s'));
  [...lim.querySelectorAll('button')].find((b) => b.textContent === 'Save').click();
  await tick(10);
  assert.deepEqual(api.calls.find((c) => c[0] === 'saveAgentSettings')[1],
    { retention_days: 30, max_concurrent: 4, unified_dispatch: false, approval_timeout_s: 2700 });
  // a mistyped number is a sentence, and nothing is sent
  api.calls.length = 0;
  val('default_max_turns').value = '0';
  [...lim.querySelectorAll('button')].find((b) => b.textContent === 'Save').click();
  await tick(10);
  assert.equal(api.calls.some((c) => c[0] === 'saveAgentSettings'), false);
  const err = lim.querySelector('.task-error[role="alert"]');
  assert.equal(err.hidden, false);
  assert.match(err.textContent, /^Most steps per task must be a number from 1 to 1,?000 \(steps\)\.$/);
});

test('Agent editor: an empty budget box says which default applies', { skip: dom.skip }, async () => {
  const { host } = await mountAgents();
  host.querySelector('#agents-new').click();
  const ph = [...host.querySelectorAll('.agents-budget input')].map((i) => i.placeholder);
  assert.deepEqual(ph.slice(0, 3), ['Default: $2.00', 'Default: $10.00', 'Default: 60']);
});

test('Task card: a task waiting for an OK says how many minutes are left', { skip: dom.skip }, async () => {
  const win = await boot();
  const C = await import('../static/js/task_card.js?v=4');
  const asked = new Date(Date.now() - 5 * 60000 - 1000).toISOString();
  const api = { calls: [], task: async () => ({ task: null }), taskEvents: async () => ({ events: [] }) };
  const task = { id: 't1', agent_name: 'Helper', backend: 'claude_sdk', backend_label: 'Claude Agent', state: 'awaiting_approval', last_seq: 0, turns: 1 };
  const approvals = [{ request_id: 'a1', task_id: 't1', tool: 'Bash', args_summary: 'ls', scopes: ['once'], state: 'pending', requested_at: asked }];
  const card = C.createTaskCard({ api, canApprove: true, toast() {}, approvalTimeoutS: 1800 });
  win.document.body.append(card.el);
  card.load(task, [{ seq: 0, type: 'approval_request', request_id: 'a1', tool_name: 'Bash', input: {}, scopes: ['once'] }], approvals);
  await tick(5);
  const pill = card.el.querySelector('.task-pill-wait');
  assert.ok(pill);
  assert.equal(pill.textContent.trim(), 'Waiting for your OK (25 min left)');
  assert.ok(pill.title.length > 20, 'the pill explains itself on hover');
  card.dispose();
  // a profile that cannot read the timeout still sees the wait, without a guessed number
  const card2 = C.createTaskCard({ api, canApprove: true, toast() {} });
  card2.load(task, [], approvals);
  assert.equal(card2.el.querySelector('.task-pill-wait').textContent.trim(), 'Waiting for your OK');
  card2.dispose();
  assert.equal(C.approvalLeftMinutes(Date.now() - 31 * 60000, 1800), 0, 'never negative');
});

// ------------------------------------------------------------------ #30/#31 Owner-only

test('Owner-only: a second Admin sees bash_allow, stdio and CLI presets disabled with "Only the Owner"; the Owner can use them', { skip: dom.skip }, async () => {
  for (const owner of [false, true]) {
    const { host, api } = await mountAgents({ protectedAdmin: owner, presets: [{ id: 'aider', label: 'Aider', kind: 'generic', executable: '/usr/bin/aider', args: ['--message', '{prompt}'] }] });
    host.querySelector('#agents-new').click();
    const form = host.querySelector('.agents-form');
    const bash = form.querySelector('textarea.agents-bash-allow');
    assert.ok(bash, 'the editor has a bash_allow box');
    assert.equal(bash.disabled, !owner);
    const field = bash.closest('.people-field');
    assert.ok(field.querySelector('label').textContent.includes('Shell commands that may run without asking'));
    if (!owner) {
      assert.match(field.textContent, /Only the Owner can let shell commands run without asking\./);
      assert.match(field.querySelector('.agents-owner-badge').textContent, /Owner only/);
      assert.ok(bash.title.startsWith('Only the Owner'));
      assert.match(form.textContent, /Only the Owner can give an agent tools that run commands or edit files/);
    }
    const unattended = [...form.querySelectorAll('.people-field')].find((f) => /Pre-approved tools/.test(f.querySelector('label').textContent));
    assert.equal(unattended.querySelector('textarea').disabled, !owner);
    [...form.querySelectorAll('button')].find((b) => b.textContent === 'Add tool server').click();
    const stdio = [...form.querySelectorAll('.agents-mcp-row select option')].find((o) => o.value === 'stdio');
    assert.equal(stdio.disabled, !owner);
    const cli = host.querySelector('#agents-cli');
    assert.ok(cli, 'the CLI presets group is shown to every Admin');
    if (owner) {
      assert.ok(api.calls.some((c) => c[0] === 'cliPresets'));
      assert.equal(cli.querySelector('.agents-cli-row input').value, 'aider');
    } else {
      assert.equal(api.calls.some((c) => c[0] === 'cliPresets'), false, 'a host-only route is never called for a second Admin');
      assert.match(cli.textContent, /Only the Owner can choose which programs agents may run on the DisPatch computer\./);
      const add = [...cli.querySelectorAll('button')].find((b) => /Add a program/.test(b.textContent));
      assert.equal(add.disabled, true);
      assert.ok(add.title.startsWith('Only the Owner'));
    }
  }
});

test('Owner: bash_allow and a CLI preset reach the API in the shapes the server takes', { skip: dom.skip }, async () => {
  const { win, host, api } = await mountAgents({ protectedAdmin: true });
  host.querySelector('#agents-new').click();
  const form = host.querySelector('.agents-form');
  const name = [...form.querySelectorAll('.people-field')].find((f) => f.querySelector('label').textContent === 'Name').querySelector('input');
  name.value = 'Builder'; fire(win, name);
  const bash = form.querySelector('textarea.agents-bash-allow');
  bash.value = 'git status\nnpm test, --watch=false\n'; fire(win, bash);
  [...form.querySelectorAll('button')].find((b) => b.textContent === 'Save agent').click();
  await tick(10);
  const body = api.calls.find((c) => c[0] === 'agentCreate')[1];
  assert.deepEqual(body.spec.approval_policy.bash_allow, ['git status', 'npm test, --watch=false'], 'one pattern per line; commas stay');
  // CLI presets
  const cli = host.querySelector('#agents-cli');
  [...cli.querySelectorAll('button')].find((b) => /Add a program/.test(b.textContent)).click();
  const row = cli.querySelector('.agents-cli-row');
  const input = (label) => [...row.querySelectorAll('.people-field')].find((f) => f.querySelector('label').textContent === label).querySelector('input,textarea,select');
  input('Short name').value = 'mytool'; fire(win, input('Short name'));
  input('Program').value = '/usr/local/bin/mytool'; fire(win, input('Program'));
  input('Arguments').value = '--prompt\n{prompt}'; fire(win, input('Arguments'));
  input('Secrets passed as variables').value = 'MY_TOKEN=MCP_MY_TOKEN'; fire(win, input('Secrets passed as variables'));
  [...cli.querySelectorAll('button')].find((b) => /Save programs/.test(b.textContent)).click();
  await tick(10);
  assert.deepEqual(api.calls.find((c) => c[0] === 'saveCliPresets')[1],
    [{ id: 'mytool', kind: 'generic', executable: '/usr/local/bin/mytool', args: ['--prompt', '{prompt}'], env_secrets: { MY_TOKEN: 'MCP_MY_TOKEN' } }]);
});

test('Owner-only: the pane trusts /api/auth/me "protected", not the shell\'s "any Admin" flag', { skip: dom.skip }, async () => {
  await boot();
  const A = await import('../static/js/agents.js?v=4');
  assert.equal(await A.resolveOwner({ me: async () => ({ protected: false }) }, { isOwner: true }), false);
  assert.equal(await A.resolveOwner({ me: async () => { throw new Error('offline'); } }, { isOwner: true }), false, 'fails closed');
  assert.equal(await A.resolveOwner({}, { isProtected: true }), true);
  assert.doesNotMatch(read('js/agents.js'), /admin_system-only|need admin_system, which every Admin holds; the server refuses/);
});

test('Connections: a second Admin cannot name a server variable as a key; the Owner can', { skip: dom.skip }, async () => {
  const types = [{ id: 'openai', label: 'OpenAI', base_url: 'https://api.openai.com/v1', key: 'required', key_env: 'OPENAI_API_KEY', capabilities: ['chat'] }];
  const keyed = { id: 'c1', name: 'Work', type: 'openai', type_label: 'OpenAI', base_url: 'https://api.openai.com/v1',
    credential: { kind: 'env', env: 'OPENAI_API_KEY', has_key: false }, capabilities: ['chat'], models: [], last_test: {}, in_use_by: [] };
  for (const owner of [false, true]) {
    const win = await boot();
    const C = await import('../static/js/connections.js?v=1');
    const created = [];
    const api = {
      connections: async () => ({ connections: [keyed], types, default: null }),
      createConnection: async (b) => { created.push(b); return { id: 'x' }; },
    };
    const pane = win.document.getElementById('host');
    await C.mountConnections(pane, { api, toast() {}, isProtected: owner });
    [...pane.querySelectorAll('.cxn-bar button')].find((b) => b.textContent === EN.cxn.add).click();
    const form = pane.querySelector('.cxn-form');
    const env = form.querySelector('.cxn-adv input');
    assert.equal(env.disabled, !owner);
    assert.equal(env.value, owner ? 'OPENAI_API_KEY' : '', 'a second Admin is not handed a variable name');
    if (!owner) assert.match(form.querySelector('.cxn-adv').textContent, /Only the Owner can use a key stored on the DisPatch computer\./);
    // editing a connection that holds a key: moving it is the Owner's call
    pane.querySelector('[data-connection="c1"] button:nth-of-type(2)').click();
    const note = pane.querySelector('.cxn-form .cxn-owner-only:not(.cxn-adv *)');
    const moveNote = [...pane.querySelectorAll('.cxn-form .cxn-owner-only')].find((n) => /point a connection with a saved key/.test(n.textContent));
    assert.equal(!!moveNote && !moveNote.hidden, !owner);
    void note;
  }
});

// ------------------------------------------------------------------ #13 schedules

function schedApi({ manage = false, rows = [] } = {}) {
  const calls = [];
  const rec = (name, ret) => async (...a) => { calls.push([name, ...a]); return typeof ret === 'function' ? ret(...a) : ret; };
  return {
    calls,
    me: rec('me', { role: manage ? 'admin' : 'user', protected: false, permissions: manage ? ['dispatch_agents', 'manage_agents'] : ['dispatch_agents'] }),
    agentsAvailable: rec('agentsAvailable', { agents: [{ id: 'ag1', name: 'Researcher', available: true }] }),
    agents: rec('agents', { agents: [], profiles: [{ id: 'p2', name: 'Mia', role: 'user' }] }),
    tasks: rec('tasks', { tasks: [] }),
    agentSettings: rec('agentSettings', { approval_timeout_s: 1800 }),
    agentSchedules: rec('agentSchedules', () => ({ schedules: rows, limits: { every_minutes_min: 15, per_profile: 20 } })),
    createAgentSchedule: rec('createAgentSchedule', (b) => ({ schedule: { id: 's9', ...b } })),
    patchAgentSchedule: rec('patchAgentSchedule', {}),
    deleteAgentSchedule: rec('deleteAgentSchedule', {}),
  };
}

test('Schedules: pure rule helpers - every N minutes, daily, weekly, and the preview', { skip: dom.skip }, async () => {
  await boot();
  const T = await import('../static/js/tasks.js?v=4');
  assert.deepEqual(T.scheduleRule({ repeat: 'every', every: '30' }), { rule: { every_minutes: 30 } });
  assert.match(T.scheduleRule({ repeat: 'every', every: '5' }).error, /at least 15 minutes/);
  assert.deepEqual(T.scheduleRule({ repeat: 'daily', at: '09:30' }), { rule: { daily_at: '09:30' } });
  assert.deepEqual(T.scheduleRule({ repeat: 'weekly', at: '18:00', day: 'fri' }), { rule: { weekly_day_at: { day: 'fri', at: '18:00' } } });
  assert.match(T.scheduleRule({ repeat: 'daily', at: '9am' }).error, /Choose a time/);
  assert.equal(T.ruleText({ weekly_day_at: { day: 'fri', at: '18:00' } }), 'Every Friday at 18:00');
  assert.equal(T.ruleText({ every_minutes: 45 }), 'Every 45 min');
  const now = new Date(2026, 9, 31, 10, 0, 0);               // a Saturday, 10:00 local
  assert.deepEqual(T.previewRuns({ daily_at: '09:30' }, now, 2).map((d) => [d.getDate(), d.getHours(), d.getMinutes()]), [[1, 9, 30], [2, 9, 30]]);
  assert.deepEqual(T.previewRuns({ weekly_day_at: { day: 'mon', at: '08:00' } }, now, 2).map((d) => [d.getMonth(), d.getDate(), d.getDay()]), [[10, 2, 1], [10, 9, 1]]);
  assert.equal(T.previewRuns({ every_minutes: 20 }, now, 1)[0].getMinutes(), 20);
  assert.deepEqual(T.scheduleBody({ agent_id: 'ag1', prompt: ' sum up ', repeat: 'every', every: '60' }, 'Asia/Tokyo'),
    { agent_id: 'ag1', prompt: 'sum up', rule: { every_minutes: 60 }, tz: 'Asia/Tokyo', thread_id: null });
});

test('Schedules: the form becomes the POST body, with a next-run preview; the dialog is reachable from the Tasks page', { skip: dom.skip }, async () => {
  const win = await boot();
  const T = await import('../static/js/tasks.js?v=4');
  const api = schedApi();
  const ui = T.createTasksUi({ api, navButton: win.document.getElementById('tasks-btn'), toast() {}, confirm: async () => true });
  await ui.refreshAccess();
  await ui.openPage();
  const open = win.document.getElementById('tasks-schedules');
  assert.ok(open, 'a Schedules button sits in the Tasks page header');
  assert.equal(open.textContent.trim(), 'Schedules');
  assert.ok(open.title && open.querySelector('svg'));
  open.click();
  await tick(10);
  const dlg = win.document.getElementById('sched-backdrop');
  assert.equal(dlg.classList.contains('hidden'), false);
  assert.match(dlg.textContent, /No schedules yet/);
  assert.equal(dlg.querySelector('#sched-everyone'), null, 'only an Admin may look at everyone\'s');
  dlg.querySelector('#sched-new').click();
  const form = dlg.querySelector('.sched-form');
  for (const id of ['sched-agent', 'sched-prompt', 'sched-repeat', 'sched-every', 'sched-at', 'sched-day']) {
    assert.ok(form.querySelector(`label[for="${id}"]`), `${id} has a visible label`);
  }
  const prompt = form.querySelector('#sched-prompt');
  prompt.value = 'Summarise my unread mail'; fire(win, prompt);
  const repeat = form.querySelector('#sched-repeat');
  repeat.value = 'weekly'; fire(win, repeat, 'change');
  const day = form.querySelector('#sched-day');
  day.value = 'wed'; fire(win, day, 'change');
  const at = form.querySelector('#sched-at');
  at.value = '07:15'; fire(win, at);
  assert.equal(form.querySelector('#sched-every').closest('.sched-field').hidden, true);
  assert.match(form.querySelector('.sched-preview').textContent, /^Every Wednesday at 07:15\. Next runs: .+ · .+ · .+ \(.+ time\)$/);
  form.dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true }));
  await tick(10);
  const body = api.calls.find((c) => c[0] === 'createAgentSchedule')[1];
  assert.deepEqual({ ...body, tz: 'x' }, { agent_id: 'ag1', prompt: 'Summarise my unread mail', rule: { weekly_day_at: { day: 'wed', at: '07:15' } }, tz: 'x', thread_id: null });
  assert.equal(typeof body.tz, 'string');
  // every-N with too few minutes is a sentence, not a request
  api.calls.length = 0;
  dlg.querySelector('#sched-new').click();
  const f2 = dlg.querySelector('.sched-form');
  f2.querySelector('#sched-prompt').value = 'x'; fire(win, f2.querySelector('#sched-prompt'));
  f2.querySelector('#sched-repeat').value = 'every'; fire(win, f2.querySelector('#sched-repeat'), 'change');
  f2.querySelector('#sched-every').value = '10'; fire(win, f2.querySelector('#sched-every'));
  f2.dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true }));
  await tick(10);
  assert.equal(api.calls.some((c) => c[0] === 'createAgentSchedule'), false);
  assert.equal(f2.querySelector('.task-error').textContent, 'Choose at least 15 minutes.');
});

test('Schedules: an Admin sees everyone\'s; another person\'s schedule offers only Pause and Delete', { skip: dom.skip }, async () => {
  const win = await boot();
  const T = await import('../static/js/tasks.js?v=4');
  const rows = [
    { id: 's1', agent_id: 'ag1', owner_profile: 'me', rule: { daily_at: '09:00' }, tz: 'UTC', enabled: true, next_run: '2026-11-01T09:00:00Z', status: 'ok', status_detail: '', own: true, prompt: 'mine' },
    { id: 's2', agent_id: 'ag1', owner_profile: 'p2', rule: { every_minutes: 60 }, tz: 'UTC', enabled: true, next_run: '2026-11-01T10:00:00Z', status: 'ok', status_detail: '', own: false, prompt: null },
  ];
  const api = schedApi({ manage: true, rows });
  const ui = T.createTasksUi({ api, navButton: win.document.getElementById('tasks-btn'), toast() {}, confirm: async () => true });
  await ui.refreshAccess();
  await ui.openSchedules();
  await tick(5);
  const dlg = win.document.getElementById('sched-backdrop');
  const all = dlg.querySelector('#sched-everyone');
  assert.ok(all);
  all.checked = true; fire(win, all, 'change');
  await tick(5);
  assert.ok(api.calls.some((c) => c[0] === 'agentSchedules' && c[1] === 'all'));
  const other = dlg.querySelector('[data-schedule="s2"]');
  assert.match(other.textContent, /Mia's schedule/);
  const btns = [...other.querySelectorAll('button')].filter((b) => !b.hidden);
  assert.deepEqual(btns.map((b) => b.textContent.trim()), ['Pause', 'Delete']);
  for (const b of btns) assert.ok(b.getAttribute('aria-label') && b.title && b.querySelector('svg'), 'icon buttons carry a label and a tooltip');
  btns[0].click();
  await tick(5);
  assert.deepEqual(api.calls.find((c) => c[0] === 'patchAgentSchedule').slice(1), ['s2', { enabled: false }]);
});

test('Schedules: a profile that may not assign tasks gets no schedule UI at all', { skip: dom.skip }, async () => {
  const win = await boot();
  const T = await import('../static/js/tasks.js?v=4');
  const api = schedApi();
  api.me = async () => ({ role: 'basic', protected: false, permissions: ['use_chat'] });
  const ui = T.createTasksUi({ api, navButton: win.document.getElementById('tasks-btn'), toast() {}, confirm: async () => true });
  await ui.refreshAccess();
  assert.equal(await ui.openSchedules(), false);
  assert.equal(win.document.getElementById('sched-backdrop'), null);
  assert.equal(api.calls.some((c) => c[0] === 'agentSchedules'), false);
});

// ------------------------------------------------------------------ tasks.js:72 + A14 entry points

test('tasks.js: there is no "owner" role since 4.0; an Admin is the operator', { skip: dom.skip }, async () => {
  await boot();
  const T = await import('../static/js/tasks.js?v=4');
  assert.equal(T.accessFrom({ role: 'owner', permissions: ['dispatch_agents'] }).operator, false);
  assert.equal(T.accessFrom({ role: 'admin', permissions: ['dispatch_agents'] }).operator, true);
  assert.doesNotMatch(read('js/tasks.js'), /role === 'owner'/);
});

test('A14: harness and mail entry points follow auth_status.features (absent for a second Admin)', () => {
  assert.match(MAIN, /state\.auth\.features\.harness === false/);
  assert.match(MAIN, /state\.auth\.features\.mail === false/);
  assert.match(MAIN, /state\.auth\.features\.practice === false/);
});

test('A14: the local viewer is the Owner\'s (isHostRole reads state.auth.protected)', () => {
  assert.equal(MAIN.includes('const isHostRole = () => canAdmin();'), false);
  assert.match(MAIN, /const isHostRole = \(\) => canAdmin\(\) && !!state\.auth\.protected;/);
});

// ------------------------------------------------------------------ INT-3 #13: where results go

test('Schedules (FX2 #13): scheduleBody carries the chosen chat as thread_id ("" = none)', { skip: dom.skip }, async () => {
  await boot();
  const T = await import('../static/js/tasks.js?v=4');
  assert.deepEqual(T.scheduleBody({ agent_id: 'ag1', prompt: 'p', repeat: 'daily', at: '08:00', thread_id: 't-mine' }, 'UTC'),
    { agent_id: 'ag1', prompt: 'p', rule: { daily_at: '08:00' }, tz: 'UTC', thread_id: 't-mine' });
  assert.equal(T.scheduleBody({ agent_id: 'ag1', prompt: 'p', repeat: 'daily', at: '08:00', thread_id: '' }, 'UTC').thread_id, null);
  assert.equal(T.scheduleBody({ agent_id: 'ag1', prompt: 'p', repeat: 'daily', at: '08:00' }, 'UTC').thread_id, null);
});

test('Schedules (FX2 #13): "Post results in" lists only the caller\'s own chats, defaults to one, and editing keeps the chosen chat', { skip: dom.skip }, async () => {
  const win = await boot();
  const T = await import('../static/js/tasks.js?v=4');
  const seen = [];
  const own = [
    { id: 't-old', bot_id: 'atlas', title: 'Trip notes', updated_at: '2026-10-01T00:00:00Z', is_pinned: true },
    { id: 't-new', bot_id: 'main', title: '', updated_at: '2026-10-30T00:00:00Z' },
  ];
  const prev = globalThis.fetch;
  globalThis.fetch = async (u, o) => {
    seen.push(String(u));
    if (String(u).startsWith('/api/threads?')) return { ok: true, status: 200, json: async () => ({ threads: own, next_cursor: null }), headers: new win.Headers() };
    return prev(u, o);
  };
  try {
    const rows = [{ id: 's1', agent_id: 'ag1', owner_profile: 'me', rule: { daily_at: '09:00' }, tz: 'UTC', enabled: true, next_run: '2026-11-01T09:00:00Z', status: 'ok', own: true, prompt: 'mine', thread_id: 't-old' }];
    const api = schedApi({ rows });
    api.bots = async () => ({ bots: [{ id: 'main', name: 'Bits' }, { id: 'atlas', name: 'Atlas' }] });
    const ui = T.createTasksUi({ api, navButton: win.document.getElementById('tasks-btn'), toast() {}, confirm: async () => true });
    await ui.refreshAccess();
    await ui.openSchedules();
    await tick(10);
    const threadCalls = seen.filter((u) => u.startsWith('/api/threads'));
    assert.ok(threadCalls.length >= 1, 'the chats come from GET /api/threads (the caller\'s own, server-filtered)');
    for (const u of threadCalls) assert.doesNotMatch(u, /bot_id|scope|owner/, 'never a per-bot or everyone\'s listing');
    const dlg = win.document.getElementById('sched-backdrop');
    dlg.querySelector('#sched-new').click();
    const form = dlg.querySelector('.sched-form');
    const pick = form.querySelector('#sched-thread');
    assert.ok(pick, 'a chat picker');
    assert.equal(form.querySelector('label[for="sched-thread"]').textContent, 'Post results in');
    assert.deepEqual([...pick.options].map((o) => o.value), ['t-new', 't-old', ''], 'own chats newest first, then "no chat"');
    assert.equal(pick.options[0].textContent, 'Bits · Untitled chat');
    assert.equal(pick.options[1].textContent, 'Atlas · Trip notes');
    assert.equal(pick.value, 't-new', 'defaults to the most recent own chat');
    form.querySelector('#sched-prompt').value = 'morning summary'; fire(win, form.querySelector('#sched-prompt'));
    form.querySelector('#sched-at').value = '08:00'; fire(win, form.querySelector('#sched-at'));
    form.dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true }));
    await tick(10);
    assert.equal(api.calls.find((c) => c[0] === 'createAgentSchedule')[1].thread_id, 't-new');
    // editing an existing schedule keeps its chat
    await tick(10);
    dlg.querySelector('[data-schedule="s1"] button:nth-of-type(2)').click();
    const f2 = dlg.querySelector('.sched-form');
    assert.equal(f2.querySelector('#sched-thread').value, 't-old');
    f2.dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true }));
    await tick(10);
    const patch = api.calls.find((c) => c[0] === 'patchAgentSchedule');
    assert.equal(patch[1], 's1');
    assert.equal(patch[2].thread_id, 't-old');
  } finally { globalThis.fetch = prev; }
});

test('Schedules (FX2 #13): a schedule\'s chat that is not in the first page stays selectable when editing', { skip: dom.skip }, async () => {
  const win = await boot();
  const T = await import('../static/js/tasks.js?v=4');
  const rows = [{ id: 's1', agent_id: 'ag1', owner_profile: 'me', rule: { daily_at: '09:00' }, tz: 'UTC', enabled: true, next_run: null, status: 'ok', own: true, prompt: 'mine', thread_id: 't-far' }];
  const api = schedApi({ rows });
  api.ownThreads = async () => ({ threads: [{ id: 't-a', bot_id: 'x', title: 'A', updated_at: '2026-10-30T00:00:00Z' }] });
  const ui = T.createTasksUi({ api, navButton: win.document.getElementById('tasks-btn'), toast() {}, confirm: async () => true });
  await ui.refreshAccess();
  await ui.openSchedules();
  await tick(10);
  const dlg = win.document.getElementById('sched-backdrop');
  dlg.querySelector('[data-schedule="s1"] button:nth-of-type(2)').click();
  const pick = dlg.querySelector('#sched-thread');
  assert.equal(pick.value, 't-far');
  assert.deepEqual([...pick.options].map((o) => o.value), ['t-far', 't-a', '']);
});
