// DisPatch Chat 3.0 -- agents and tasks (frontend half of gate G17).
//
//   1. Pure logic: the @mention parser, the list query, what a profile is offered, the timeline
//      model (events in, a tree out), cost wording ("estimate"), the agent form <-> API body.
//   2. approval_card.js / task_card.js / tasks.js / agents.js against a real DOM (jsdom) and a fake
//      API: nothing here talks to a server.
//   3. Wiring: main.js, index.html, sw.js and api.js carry the pieces, and no UI string names the
//      forbidden product labels.

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
const MAIN = read('js/main.js');
const HTML = read('index.html');
const SW = read('sw.js');
const API_SRC = read('js/api.js');
const EN = JSON.parse(read('locales/en.json'));

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* DOM tests skip */ }
const dom = { skip: domSkip(jsdom ? false : 'jsdom is not installed') };
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

async function boot() {
  const { JSDOM } = jsdom;
  const win = new JSDOM('<!doctype html><body><button id="tasks-btn" class="gear-btn hidden"></button><div id="host"></div></body>',
    { url: 'http://127.0.0.1:18765/', pretendToBeVisual: true }).window;
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
  const i18n = await import('../static/js/i18n.js?v=4');
  await i18n.init('en');
  return win;
}

// ------------------------------------------------------------------ pure logic

// tasks.js pulls in i18n and the DOM helpers, so it is imported only after a DOM exists.
async function loadTasks() {
  const win = await boot();
  return { win, Tasks: await import('../static/js/tasks.js?v=1') };
}

test('slug and @mention parsing', { skip: dom.skip }, async () => {
  const { Tasks } = await loadTasks();
  const agents = [
    { id: 'a1', name: 'Code Reviewer', available: true },
    { id: 'a2', name: 'Researcher', available: true },
    { id: 'a3', name: 'Broken', available: false },
  ];
  assert.equal(Tasks.slug('Code Reviewer'), 'code-reviewer');
  assert.equal(Tasks.slug('  Dr. Ünal!  '), 'dr-ünal');
  const m = Tasks.parseMention('@code-reviewer look at the diff\nin src/', agents);
  assert.equal(m.agent.id, 'a1');
  assert.equal(m.prompt, 'look at the diff\nin src/');
  assert.equal(Tasks.parseMention('@Researcher find x', agents).agent.id, 'a2');
  assert.equal(Tasks.parseMention('@nobody do it', agents), null, 'an unknown name is ordinary chat');
  assert.equal(Tasks.parseMention('@researcher', agents), null, 'a mention with nothing after it is not a task');
  assert.equal(Tasks.parseMention('hello @researcher x', agents), null, 'only a leading mention counts');
  assert.equal(Tasks.parseMention('@broken do it', agents), null, 'an agent that cannot run is not offered');
  assert.equal(Tasks.parseMention('', agents), null);
});

test('the list query leaves empty filters out', { skip: dom.skip }, async () => {
  const { Tasks } = await loadTasks();
  assert.deepEqual(Tasks.listQuery({ state: '', agent: '', profile: '', since: '' }), { limit: 100 });
  assert.deepEqual(Tasks.listQuery({ state: 'failed', agent: 'a1', profile: 'p', since: '2026-10-01' }),
    { state: 'failed', agent_id: 'a1', profile: 'p', since: '2026-10-01', limit: 100 });
});

test('what a profile is offered follows /api/auth/me, and a Restricted one nothing', { skip: dom.skip }, async () => {
  const { Tasks } = await loadTasks();
  const owner = Tasks.accessFrom({ role: 'owner', permissions: ['*'], restricted: false });
  assert.deepEqual(owner, { dispatch: true, approve: true, manage: true, operator: true });
  const member = Tasks.accessFrom({ role: 'member', permissions: ['use_chat', 'dispatch_agents'], restricted: false });
  assert.deepEqual(member, { dispatch: true, approve: false, manage: false, operator: false });
  const plain = Tasks.accessFrom({ role: 'member', permissions: ['use_chat'], restricted: false });
  assert.equal(plain.dispatch, false);
  const kid = Tasks.accessFrom({ role: 'restricted', permissions: ['use_chat', 'dispatch_agents', '*'], restricted: true });
  assert.deepEqual(kid, { dispatch: false, approve: false, manage: false, operator: false });
  assert.deepEqual(Tasks.accessFrom(null), Tasks.NO_ACCESS);
});

test('the timeline model builds a tree from parent ids and merges tool results', { skip: dom.skip }, async () => {
  await boot();
  const C = await import('../static/js/task_card.js?v=1');
  const evs = [
    { seq: 0, type: 'status', state: 'queued' },
    { seq: 1, type: 'status', state: 'running' },
    { seq: 2, type: 'text_delta', text: 'Looking' },
    { seq: 3, type: 'text_delta', text: ' around.' },
    { seq: 4, type: 'tool_call', call_id: 'c1', name: 'Read', input: { file_path: 'a.txt' } },
    { seq: 5, type: 'tool_result', call_id: 'c1', ok: true, output: 'contents' },
    { seq: 6, type: 'tool_call', call_id: 'sub1', name: 'Agent', input: { description: 'dig', subagent_type: 'researcher' } },
    { seq: 7, type: 'subagent_started', subagent_id: 'sub1', name: 'researcher', description: 'dig deeper' },
    { seq: 8, type: 'text_delta', text: 'child says hi', parent_id: 'sub1' },
    { seq: 9, type: 'tool_call', call_id: 'c2', name: 'Grep', input: { pattern: 'x' }, parent_id: 'sub1' },
    { seq: 10, type: 'subagent_finished', subagent_id: 'sub1', ok: true, summary: 'found it' },
    { seq: 11, type: 'tool_result', call_id: 'sub1', ok: true, output: 'done' },
    { seq: 12, type: 'approval_request', request_id: 'apr_1', tool_name: 'Bash', input: { command: 'ls' }, scopes: ['once'] },
    { seq: 13, type: 'cost_update', estimate: true, total_cost_usd: 0.0123, input_tokens: 10, output_tokens: 5, turns: 2 },
    { seq: 14, type: 'result', state: 'succeeded', text: 'All done', resumable: true },
  ];
  const m = C.foldEvents(evs);
  assert.equal(m.lastSeq, 14);
  assert.equal(m.state, 'succeeded');
  assert.deepEqual(m.top.map((n) => n.kind), ['text', 'tool', 'subagent', 'approval']);
  assert.equal(m.top[0].text, 'Looking around.', 'deltas coalesce');
  assert.equal(m.top[1].ok, true);
  assert.equal(m.top[1].output, 'contents');
  const sub = m.top[2];
  assert.equal(sub.agentType, 'researcher');
  assert.equal(sub.description, 'dig deeper');
  assert.equal(sub.done, true);
  assert.deepEqual(sub.children.map((n) => n.kind), ['text', 'tool'], 'subagent output nests under it');
  assert.equal(m.approvals.get('apr_1').canTask, false, 'a destructive tool offers only "once"');
  assert.equal(C.applyEvent(m, { seq: 14, type: 'text_delta', text: 'again' }), null, 'a repeated seq is ignored');
  const r = C.createModel();
  C.applyEvent(r, { seq: 0, type: 'text_delta', text: 'abc' });
  C.applyEvent(r, { seq: 1, type: 'text_delta', text: 'xyz', replace: true });
  assert.equal(r.top[0].text, 'xyz', 'a cumulative snapshot replaces');
});

test('costs are always labelled as estimates', { skip: dom.skip }, async () => {
  await boot();
  const C = await import('../static/js/task_card.js?v=1');
  assert.equal(C.costLabel({ cost_usd: 0.0123 }), 'about $0.0123 (estimate)');
  assert.equal(C.costLabel({ cost_usd: 1.5 }), 'about $1.50 (estimate)');
  assert.equal(C.costLabel({ cost_usd: null, tokens: 1200 }), `Tokens: ${(1200).toLocaleString()} (estimate)`);
  assert.equal(C.costLabel({ cost_usd: null, tokens: 0 }), '');
  assert.equal(C.durationLabel(75), '1 min 15 s');
  assert.equal(C.durationLabel(null), '');
});

test('the agent form round-trips and keeps what the form does not show', async () => {
  const A = await import('../static/js/agents.js?v=1').catch(() => null);
  if (!A) return;
  const stored = {
    id: 'ag_1', name: 'Reviewer', description: 'checks code', disabled: false, published_to: ['p1'],
    spec: {
      backend: 'claude_sdk', name: 'Reviewer', model: 'x', instructions: 'be kind', tool_allowlist: ['Read', 'Grep'],
      mcp_servers: { sf: { transport: 'http', url: 'http://localhost:1234/mcp', headers: { 'X-A': 'b' },
        header_secrets: { Authorization: 'STUDIOFORGE_MCP_KEY_RO' }, privileged: false } },
      workspace_policy: { mode: 'worktree', trust_repo_settings: false, ref: 'main' },
      approval_policy: { mode: 'interactive', ask_tools: ['Read'], always_ask: ['Bash', 'Zap'], unattended_allow: [], timeout_s: 90 },
      budget: { max_usd: 1, max_wall_clock_s: 600 }, required_permission: 'dispatch_agents',
      reads_untrusted_content: false, high_privilege_tools: ['Zap'], max_subagent_depth: 3,
      max_concurrent_subagents: 2, backend_options: { keep: 1 },
    },
  };
  const f = A.specToForm(stored);
  assert.equal(f.max_minutes, 10);
  const body = A.formToBody(f);
  assert.equal(body.name, 'Reviewer');
  assert.deepEqual(body.published_to, ['p1']);
  const s = body.spec;
  assert.equal(s.name, undefined, 'the server names the spec from the agent name');
  assert.deepEqual(s.approval_policy.always_ask, ['Bash', 'Zap'], 'the always-ask list survives an edit');
  assert.equal(s.approval_policy.timeout_s, 90);
  assert.equal(s.workspace_policy.ref, 'main');
  assert.equal(s.max_subagent_depth, 3);
  assert.deepEqual(s.high_privilege_tools, ['Zap']);
  assert.deepEqual(s.backend_options, { keep: 1 });
  assert.deepEqual(s.budget, { max_usd: 1, max_wall_clock_s: 600 });
  assert.deepEqual(s.mcp_servers.sf.headers, { 'X-A': 'b' });
  assert.deepEqual(s.mcp_servers.sf.header_secrets, { Authorization: 'STUDIOFORGE_MCP_KEY_RO' });
  f.max_usd = 'abc';
  assert.deepEqual(A.formToBody(f), { error: 'agents.err_number' });
  f.max_usd = ''; f.max_minutes = '5'; f.published_all = true; f.backend = 'openclaw'; f.seat = 'lead';
  const b2 = A.formToBody(f);
  assert.deepEqual(b2.published_to, ['*']);
  assert.equal(b2.spec.budget.max_wall_clock_s, 300);
  assert.deepEqual(b2.spec.backend_options, { keep: 1, agent_id: 'lead' });
});

// ------------------------------------------------------------------ approval cards

test('an approval card offers "for this task" only when the server does, and answers through onAnswer', { skip: dom.skip }, async () => {
  const win = await boot();
  const { approvalCardEl } = await import('../static/js/approval_card.js?v=1');
  const answers = [];
  const onAnswer = async (d, s) => { answers.push([d, s]); };
  const destructive = approvalCardEl({ request_id: 'r1', tool: 'Bash', args_summary: '{"command": "rm -rf x"}', scopes: ['once'] }, { onAnswer });
  win.document.body.append(destructive);
  const labels = [...destructive.querySelectorAll('button')].map((b) => b.textContent);
  assert.deepEqual(labels, ['Allow once', 'Deny']);
  assert.match(destructive.textContent, /asks every time/);
  assert.equal(destructive.querySelector('.approval-args').textContent, '{"command": "rm -rf x"}', 'arguments render as text');
  assert.equal(destructive.getAttribute('role'), 'group');
  assert.ok(destructive.getAttribute('aria-labelledby'));
  const sticky = approvalCardEl({ request_id: 'r2', tool: 'Read', args_summary: '', scopes: ['once', 'task'] }, { onAnswer });
  assert.deepEqual([...sticky.querySelectorAll('button')].map((b) => b.textContent), ['Allow once', 'Allow for this task', 'Deny']);
  sticky.querySelector('[data-act="allow-task"]').click();
  destructive.querySelector('[data-act="deny-once"]').click();
  await tick();
  assert.deepEqual(answers, [['allow', 'task'], ['deny', 'once']]);
  assert.ok([...destructive.querySelectorAll('button')].every((b) => b.disabled), 'buttons lock while the answer is in flight');
  // the server's answer repaints the same element in place
  destructive.update({ request_id: 'r1', tool: 'Bash', args_summary: '', scopes: ['once'], state: 'deny' });
  assert.equal(destructive.querySelector('button'), null);
  assert.equal(destructive.className, 'approval-card approval-deny');
  assert.match(destructive.textContent, /Denied/);
});

test('someone who may not approve sees the request but no buttons', { skip: dom.skip }, async () => {
  await boot();
  const { approvalCardEl } = await import('../static/js/approval_card.js?v=1');
  const card = approvalCardEl({ request_id: 'r3', tool: 'Bash', scopes: ['once'] }, { canAnswer: false });
  assert.equal(card.querySelector('button'), null);
  assert.match(card.textContent, /Waiting for someone who can approve agent actions/);
});

test('a failed answer re-enables the buttons and says why', { skip: dom.skip }, async () => {
  await boot();
  const { approvalCardEl } = await import('../static/js/approval_card.js?v=1');
  const card = approvalCardEl({ request_id: 'r4', tool: 'Bash', scopes: ['once'] },
    { onAnswer: async () => { throw Object.assign(new Error('gone'), { userMessage: 'That approval request is not pending any more.' }); } });
  card.querySelector('[data-act="allow-once"]').click();
  await tick(5);
  assert.ok([...card.querySelectorAll('button')].every((b) => !b.disabled));
  assert.match(card.querySelector('.approval-error').textContent, /not pending any more/);
});

// ------------------------------------------------------------------ the task card

const TASK = (over = {}) => ({
  id: 't1', agent_id: 'a1', agent_name: 'Helper', backend: 'fake', backend_label: 'OpenClaw', state: 'running',
  owner: 'p1', owner_name: 'Sam', cost_usd: 0.02, tokens: 0, turns: 2, resumable: false, hold: null,
  duration_s: 12, prompt: 'Do the thing', result_text: null, last_seq: 3, ...over,
});

function fakeApi(over = {}) {
  const calls = [];
  const rec = (name, ret) => async (...a) => { calls.push([name, ...a]); return typeof ret === 'function' ? ret(...a) : ret; };
  const api = {
    calls,
    me: rec('me', { role: 'owner', permissions: ['*'], restricted: false }),
    agentsAvailable: rec('agentsAvailable', { agents: [
      { id: 'a1', name: 'Helper', backend: 'openclaw', backend_label: 'OpenClaw', available: true, data_leaves_host: false, description: 'helps' },
      { id: 'a2', name: 'Code Reviewer', backend: 'claude_sdk', backend_label: 'Claude Agent', available: true, data_leaves_host: true },
    ], budget: { usd_per_day: 2, spent_today_usd: 0.5 } }),
    tasks: rec('tasks', (q) => ({ tasks: [TASK(), TASK({ id: 't2', agent_name: 'Other', state: 'awaiting_approval', pending_approval_count: 1 })]
      .filter((x) => !q || !q.state || x.state === q.state) })),
    task: rec('task', (id) => ({ task: TASK({ id }), approvals: [] })),
    taskEvents: rec('taskEvents', (id, after) => ({ events: after >= 3 ? [] : [
      { seq: 0, type: 'status', state: 'queued' }, { seq: 1, type: 'status', state: 'running' },
      { seq: 2, type: 'text_delta', text: 'hi there' }, { seq: 3, type: 'tool_call', call_id: 'c', name: 'Read', input: { file_path: 'x' } }] })),
    taskSend: rec('taskSend', { ok: true }),
    taskInterrupt: rec('taskInterrupt', { ok: true }),
    taskResume: rec('taskResume', { ok: true }),
    taskCancel: rec('taskCancel', { ok: true }),
    taskRelease: rec('taskRelease', { ok: true }),
    taskDismiss: rec('taskDismiss', { ok: true }),
    answerApproval: rec('answerApproval', (rid, d, s) => ({ ok: true, approval: { request_id: rid, state: d, scope: s } })),
    createTask: rec('createTask', (b) => ({ task: TASK({ id: 'tnew', agent_name: 'Helper' }), echo: b })),
    agents: rec('agents', { agents: [], profiles: [{ id: 'p1', name: 'Sam', role: 'member' }] }),
    ...over,
  };
  return api;
}

test('the task card shows the timeline, the estimate, and live frames; a gap asks for what was missed', { skip: dom.skip }, async () => {
  const win = await boot();
  const C = await import('../static/js/task_card.js?v=1');
  const api = fakeApi();
  const card = C.createTaskCard({ api, canApprove: true, showOwner: true, toast() {}, confirm: async () => true });
  win.document.body.append(card.el);
  const evs = (await api.taskEvents('t1', -1)).events;
  card.load(TASK(), evs, []);
  await tick(5);
  const head = card.el.querySelector('.task-head').textContent;
  assert.match(head, /Helper/); assert.match(head, /OpenClaw/); assert.match(head, /Running/);
  assert.match(head, /Sam/); assert.match(head, /about \$0\.02 \(estimate\)/); assert.match(head, /Turns: 2/);
  assert.equal(card.el.querySelector('.task-text').textContent, 'hi there');
  assert.equal(card.el.querySelector('.task-tool-name').textContent, 'Read');
  assert.equal(card.el.querySelector('.task-timeline').getAttribute('role'), 'log');
  // a live delta extends the text
  card.applyFrame({ type: 'task.event', task_id: 't1', event: { seq: 4, type: 'tool_result', call_id: 'c', ok: true, output: 'file body' }, task: { state: 'running', cost_usd: 0.03 } });
  await tick(5);
  assert.match(card.el.querySelector('.task-io-out').textContent, /file body/);
  // an approval request arrives live, inline, and is answered in place
  card.applyFrame({ type: 'task.event', task_id: 't1', event: { seq: 5, type: 'approval_request', request_id: 'apr_9', tool_name: 'Bash', input: { command: 'ls' }, scopes: ['once'], call_id: 'x' }, task: { state: 'awaiting_approval' } });
  await tick(5);
  const apr = card.el.querySelector('.approval-card[data-request-id="apr_9"]');
  assert.ok(apr, 'the approval card sits in the timeline');
  assert.match(card.el.querySelector('.task-head').textContent, /Approvals waiting: 1/);
  apr.querySelector('[data-act="allow-once"]').click();
  await tick(5);
  assert.deepEqual(api.calls.find((c) => c[0] === 'answerApproval'), ['answerApproval', 'apr_9', 'allow', 'once']);
  assert.equal(card.el.querySelector('.approval-card[data-request-id="apr_9"]').dataset.state, 'allow');
  // a gap (seq 9 after 5) triggers a catch-up fetch from the last seq seen
  api.taskEvents = async (id, after) => { api.calls.push(['taskEvents', id, after]); return { events: [{ seq: 6, type: 'text_delta', text: 'between' }, { seq: 7, type: 'result', state: 'succeeded', text: 'ok', resumable: true }] }; };
  card.applyFrame({ type: 'task.event', task_id: 't1', event: { seq: 9, type: 'text_delta', text: 'late' }, task: { state: 'running' } });
  await tick(20);
  assert.ok(api.calls.some((c) => c[0] === 'taskEvents' && c[2] === 5), 'it asked for events after seq 5');
  assert.equal(card.model.lastSeq, 7);
});

test('Stop, steer, Resume and End are real buttons that call the right routes', { skip: dom.skip }, async () => {
  const win = await boot();
  const C = await import('../static/js/task_card.js?v=1');
  const api = fakeApi();
  const asked = [];
  const card = C.createTaskCard({ api, canApprove: true, toast() {}, confirm: async (m, o) => { asked.push([m, o]); return true; } });
  win.document.body.append(card.el);
  card.load(TASK({ state: 'running', last_seq: -1 }), [], []);
  await tick(5);
  const labels = () => [...card.el.querySelectorAll('.task-controls button')].map((b) => b.textContent);
  assert.deepEqual(labels(), ['Stop', 'Steer', 'End task']);
  card.el.querySelector('.task-stop').click();
  card.el.querySelector('.task-steer-input').value = '  go left  ';
  card.el.querySelector('.task-steer .btn').click();
  await tick(5);
  assert.deepEqual(api.calls.filter((c) => /^task(Interrupt|Send)$/.test(c[0])), [['taskInterrupt', 't1'], ['taskSend', 't1', 'go left']]);
  // interrupted: resumable, idle -> Resume + follow-up
  card.setTask(TASK({ state: 'cancelled', resumable: true, reason: 'interrupted' }));
  await tick(5);
  assert.deepEqual(labels(), ['Resume', 'Send to agent', 'End task']);
  card.el.querySelector('.btn-primary').click();
  await tick(5);
  assert.deepEqual(api.calls.at(-1), ['taskResume', 't1']);
  card.el.querySelector('.task-end').click();
  await tick(5);
  assert.equal(asked.length, 1);
  assert.deepEqual(api.calls.at(-1), ['taskCancel', 't1']);
  // finished for good: no controls
  card.setTask(TASK({ state: 'failed', resumable: false }));
  await tick(5);
  assert.deepEqual(labels(), []);
});

test('a hand-off card waiting for Send shows Send / Dismiss only to someone who may approve', { skip: dom.skip }, async () => {
  const win = await boot();
  const C = await import('../static/js/task_card.js?v=1');
  const api = fakeApi();
  for (const [canApprove, want] of [[true, ['Send', 'Dismiss']], [false, []]]) {
    const card = C.createTaskCard({ api, canApprove, toast() {}, confirm: async () => true });
    win.document.body.append(card.el);
    card.load(TASK({ state: 'queued', hold: 'send', last_seq: -1 }), [], []);
    await tick(5);
    assert.deepEqual([...card.el.querySelectorAll('.task-controls button')].map((b) => b.textContent), want);
    assert.match(card.el.querySelector('.task-head').textContent, /Waiting for you to send it/);
  }
});

test('agent and tool text is rendered as text, never as markup', { skip: dom.skip }, async () => {
  const win = await boot();
  const C = await import('../static/js/task_card.js?v=1');
  const card = C.createTaskCard({ api: fakeApi(), canApprove: true, toast() {} });
  win.document.body.append(card.el);
  card.load(TASK(), [
    { seq: 0, type: 'text_delta', text: '<img src=x onerror=alert(1)><script>alert(2)</script>' },
    { seq: 1, type: 'tool_call', call_id: 'c', name: '<b>Bash</b>', input: { command: '<i>x</i>' } },
    { seq: 2, type: 'tool_result', call_id: 'c', ok: false, output: '<u>boom</u>' },
  ], []);
  await tick(5);
  assert.equal(card.el.querySelector('img'), null);
  assert.equal(card.el.querySelector('script'), null);
  assert.equal(card.el.querySelector('.task-tool-name b'), null);
  assert.match(card.el.querySelector('.task-text').textContent, /<img src=x/);
});

// ------------------------------------------------------------------ the Tasks UI

test('the Tasks button shows for someone who may assign tasks and never for a Restricted profile', { skip: dom.skip }, async () => {
  const { win, Tasks } = await loadTasks();
  const api = fakeApi();
  const nav = win.document.getElementById('tasks-btn');
  const ui = Tasks.createTasksUi({ api, navButton: nav, isDecoy: () => false, toast() {}, confirm: async () => true });
  await ui.refreshAccess();
  assert.equal(nav.classList.contains('hidden'), false);
  assert.equal(nav.dataset.waiting, '1', 'one approval is waiting (from the list)');
  assert.match(nav.getAttribute('aria-label'), /approvals waiting: 1/);
  assert.equal(ui.access.dispatch, true);
  const kidApi = fakeApi({ me: async () => ({ role: 'restricted', permissions: ['use_chat'], restricted: true }) });
  const nav2 = win.document.createElement('button');
  nav2.className = 'hidden';
  const kid = Tasks.createTasksUi({ api: kidApi, navButton: nav2, isDecoy: () => true });
  await kid.refreshAccess();
  assert.equal(nav2.classList.contains('hidden'), true);
  assert.equal(kidApi.calls.length, 0, 'a Restricted session does not even ask');
  assert.equal(kid.interceptSend('@helper do it', 't'), false);
  assert.equal(kid.cardForMessage({ metadata: { task_card: { task_id: 'x', kind: 'assigned' } } }), null);
});

test('the Tasks page lists tasks, filters through the API, expands a row into a live card', { skip: dom.skip }, async () => {
  const { win, Tasks } = await loadTasks();
  const api = fakeApi();
  const ui = Tasks.createTasksUi({ api, navButton: win.document.getElementById('tasks-btn'), isDecoy: () => false, toast() {}, confirm: async () => true });
  await ui.refreshAccess();
  await ui.openPage();
  const dlg = win.document.getElementById('tasks-backdrop');
  assert.equal(dlg.classList.contains('hidden'), false);
  assert.equal(dlg.querySelector('[role="dialog"]').getAttribute('aria-modal'), 'true');
  const rows = [...dlg.querySelectorAll('.tasks-row')];
  assert.equal(rows.length, 2);
  assert.match(rows[1].textContent, /Needs you/);
  const state = dlg.querySelector('#tasks-f-state');
  state.value = 'awaiting_approval';
  state.dispatchEvent(new win.Event('change', { bubbles: true }));
  await tick(5);
  assert.deepEqual(api.calls.filter((c) => c[0] === 'tasks').at(-1), ['tasks', { state: 'awaiting_approval', limit: 100 }]);
  // expand: a card mounts, fed from the API
  const head = dlg.querySelector('.tasks-row-head');
  head.click();
  await tick(20);
  assert.equal(head.getAttribute('aria-expanded'), 'true');
  const body = dlg.querySelector('.tasks-row-body');
  assert.equal(body.hidden, false);
  assert.ok(body.querySelector('.task-card'));
  assert.match(body.querySelector('.task-text').textContent, /hi there/);
  // a live frame reaches that card AND the row's pill
  ui.onFrame({ type: 'task.event', task_id: 't2', event: { seq: 4, type: 'text_delta', text: '!' }, task: { state: 'succeeded' } });
  await tick(10);
  assert.match(dlg.querySelector('.tasks-row-head .task-pill').textContent, /Done/);
  assert.deepEqual([...body.querySelectorAll('.task-text')].map((n) => n.textContent), ['hi there', '!']);
  // Escape closes it
  dlg.querySelector('.modal').dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(dlg.classList.contains('hidden'), true);
});

test('Assign task: the dialog posts the origin thread, tells where the prompt goes, and shows server errors', { skip: dom.skip }, async () => {
  const { win, Tasks } = await loadTasks();
  const assigned = [];
  let fail = null;
  const api = fakeApi({ createTask: async (b) => { if (fail) throw fail; return { task: TASK({ id: 'tnew', agent_name: 'Code Reviewer' }), echo: b }; } });
  const ui = Tasks.createTasksUi({
    api, navButton: win.document.getElementById('tasks-btn'), isDecoy: () => false, toast() {}, confirm: async () => true,
    onAssigned: (task, o) => assigned.push([task.id, o]),
  });
  await ui.refreshAccess();
  await ui.openAssign({ threadId: 'th9' });
  const dlg = win.document.getElementById('assign-backdrop');
  const select = dlg.querySelector('#assign-agent');
  assert.deepEqual([...select.options].map((o) => o.text), ['Helper · OpenClaw', 'Code Reviewer · Claude Agent']);
  assert.match(dlg.querySelector('#assign-hint').textContent, /runs on this machine/);
  select.value = 'a2';
  select.dispatchEvent(new win.Event('change'));
  assert.match(dlg.querySelector('#assign-hint').textContent, /sent to Claude Agent's service/);
  assert.match(dlg.textContent, /about \$0\.50 of your \$2\.00 limit/);
  dlg.querySelector('#assign-prompt').value = 'review the diff';
  fail = Object.assign(new Error('x'), { userMessage: "This agent's budget for today is used up.", fix: 'Try tomorrow.' });
  dlg.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick(10);
  assert.match(dlg.querySelector('.task-error').textContent, /budget for today is used up\. Try tomorrow\./);
  assert.equal(dlg.classList.contains('hidden'), false, 'an error keeps the dialog open');
  fail = null;
  dlg.querySelector('form').dispatchEvent(new win.Event('submit', { cancelable: true }));
  await tick(10);
  assert.deepEqual(assigned, [['tnew', { fromComposer: false }]]);
  assert.equal(dlg.classList.contains('hidden'), true);
});

test('@agent in the composer opens Assign task, filled in; nothing is sent as chat', { skip: dom.skip }, async () => {
  const { win, Tasks } = await loadTasks();
  const api = fakeApi();
  const ui = Tasks.createTasksUi({ api, navButton: win.document.getElementById('tasks-btn'), isDecoy: () => false, toast() {}, confirm: async () => true });
  await ui.refreshAccess();
  assert.equal(ui.interceptSend('hello there', 'th1'), false);
  assert.equal(ui.interceptSend('@stranger hello', 'th1'), false);
  assert.equal(ui.interceptSend('@code-reviewer check the diff', 'th1'), true);
  await tick(10);
  const dlg = win.document.getElementById('assign-backdrop');
  assert.equal(dlg.querySelector('#assign-agent').value, 'a2');
  assert.equal(dlg.querySelector('#assign-prompt').value, 'check the diff');
});

test('an inline card mounts under the assigning message only, and survives re-rendering', { skip: dom.skip }, async () => {
  const { win, Tasks } = await loadTasks();
  const api = fakeApi();
  const ui = Tasks.createTasksUi({ api, navButton: win.document.getElementById('tasks-btn'), isDecoy: () => false, toast() {}, confirm: async () => true });
  await ui.refreshAccess();
  const msg = (kind) => ({ id: 'm', metadata: { task_card: { task_id: 't1', kind } } });
  assert.equal(ui.cardForMessage(msg('done')), null, 'notices do not repeat the card');
  assert.equal(ui.cardForMessage({ id: 'm', metadata: {} }), null);
  const a = ui.cardForMessage(msg('assigned'));
  const b = ui.cardForMessage(msg('assigned'));
  assert.equal(a, b, 'the same task is the same element, however often the message re-renders');
  win.document.body.append(a);
  await tick(30);
  assert.ok(a.querySelector('.task-card-compact'));
  ui.onFrame({ type: 'task.event', task_id: 't1', event: { seq: 4, type: 'text_delta', text: '??' }, task: { state: 'running' } });
  await tick(10);
  assert.deepEqual([...a.querySelectorAll('.task-text')].map((n) => n.textContent), ['hi there', '??']);
});

// ------------------------------------------------------------------ Settings -> Agents

const AGENTS_DATA = (over = {}) => ({
  agents: { agents: [{ id: 'ag1', name: 'Reviewer', description: '', backend: 'claude_sdk', backend_label: 'Claude Agent', disabled: false,
    published_to: ['p1'], spec: { backend: 'claude_sdk', tool_allowlist: ['Read'], budget: {} } }],
  profiles: [{ id: 'p1', name: 'Sam', role: 'member' }, { id: 'p2', name: 'Kid', role: 'restricted' }] },
  backends: { backends: [
    { name: 'openclaw', label: 'OpenClaw', available: true, problem: null, capabilities: { approvals: false, steering: false, subagents: false, cost_reporting: 'tokens', same_host_required: true, data_leaves_host: false } },
    { name: 'claude_sdk', label: 'Claude Agent', available: false, needs_api_key: true, problem: { code: 'auth_failed', message: 'The Claude Agent needs an Anthropic API key (Settings, Agents).' },
      capabilities: { approvals: true, steering: true, subagents: true, cost_reporting: 'usd', same_host_required: false, data_leaves_host: true } },
  ] },
  creds: { route: 'api_key', routes: ['api_key', 'bedrock', 'google', 'foundry'], api_key_set: false, api_key_source: null, provider_env: null },
  settings: { unified_dispatch: false, unified_dispatch_from_env: false, retention_days: 30, max_concurrent: 4 },
  budgets: { budgets: { p1: { usd_per_day: 1 } } },
  usage: { day: '2026-10-18', estimate: true, spend: [{ day: '2026-10-18', kind: 'agent', subject: 'ag1', usd: 0.0123, tokens: 10, turns: 1, tasks: 1 }] },
  secrets: { secrets: [{ name: 'STUDIOFORGE_MCP_KEY_RO', set: false }] },
  ...over,
});

function agentsApi(data = AGENTS_DATA()) {
  const calls = [];
  const rec = (name, ret) => async (...a) => { calls.push([name, ...a]); return typeof ret === 'function' ? ret(...a) : ret; };
  return {
    calls,
    agents: rec('agents', data.agents), agentBackends: rec('agentBackends', data.backends),
    claudeCredentials: rec('claudeCredentials', data.creds), agentSettings: rec('agentSettings', data.settings),
    agentProfileBudgets: rec('agentProfileBudgets', data.budgets), agentUsage: rec('agentUsage', data.usage),
    agentSecrets: rec('agentSecrets', data.secrets),
    saveClaudeCredentials: rec('saveClaudeCredentials', {}), clearClaudeKey: rec('clearClaudeKey', {}),
    agentCreate: rec('agentCreate', {}), agentSave: rec('agentSave', {}), agentDelete: rec('agentDelete', {}),
    saveAgentSettings: rec('saveAgentSettings', {}), saveAgentProfileBudget: rec('saveAgentProfileBudget', {}),
    saveAgentSecret: rec('saveAgentSecret', {}), deleteAgentSecret: rec('deleteAgentSecret', {}),
    purgeAgentTasks: rec('purgeAgentTasks', { purged: 2 }),
  };
}

async function mountAgents(isOwner, data) {
  const win = await boot();
  const A = await import('../static/js/agents.js?v=1');
  const api = agentsApi(data);
  const rec = { toasts: [], confirms: [] };
  const host = win.document.getElementById('host');
  await A.mountAgentsSettings(host, { api, isOwner, toast: (m, e) => rec.toasts.push([m, !!e]), confirm: async (m) => { rec.confirms.push(m); return true; } });
  await tick(5);
  return { win, host, api, rec };
}

test('Agents settings: backends are named "OpenClaw" and "Claude Agent", with what each needs and does', { skip: dom.skip }, async () => {
  const { host } = await mountAgents(true);
  const rows = [...host.querySelectorAll('.agents-backend')];
  assert.deepEqual(rows.map((r) => r.querySelector('strong').textContent), ['OpenClaw', 'Claude Agent']);
  assert.match(rows[0].textContent, /Ready/); assert.match(rows[0].textContent, /same machine/); assert.match(rows[0].textContent, /stay on this machine/);
  assert.match(rows[1].textContent, /Needs setup/); assert.match(rows[1].textContent, /needs an Anthropic API key/);
  assert.match(rows[1].textContent, /prompts leave this machine/);
  const text = host.textContent;
  assert.doesNotMatch(text, /claude\.ai|oauth/i);
  assert.match(text, /Signing in with a Claude subscription is not supported/);
});

test('Claude credentials: the key is write-only; routes are the four provider options and no sign-in', { skip: dom.skip }, async () => {
  const { host, api } = await mountAgents(true);
  const sel = host.querySelector('.agents-claude select');
  assert.deepEqual([...sel.options].map((o) => o.text), ['Anthropic API key', 'Amazon Bedrock', 'Google Cloud', 'Microsoft Foundry']);
  const key = host.querySelector('.agents-claude input[type="password"]');
  assert.equal(key.autocomplete, 'off');
  assert.equal(key.value, '');
  key.value = 'sk-ant-api03-secret';
  [...host.querySelectorAll('.agents-claude button')].find((b) => b.textContent === 'Save connection').click();
  await tick(10);
  assert.deepEqual(api.calls.find((c) => c[0] === 'saveClaudeCredentials'), ['saveClaudeCredentials', { route: 'api_key', api_key: 'sk-ant-api03-secret' }]); // scrub-ok: a fake key typed into a test input
  // choosing a cloud provider hides the key box and sends the route only
  sel.value = 'bedrock';
  sel.dispatchEvent(new host.ownerDocument.defaultView.Event('change'));
  assert.equal(key.closest('.people-field').hidden, true);
  assert.equal(host.textContent.includes('sk-ant-api03-secret'), false, 'a typed key is never echoed into the page text');
});

test('Agents: only the Owner can switch on host-level powers; an Admin sees them disabled', { skip: dom.skip }, async () => {
  for (const [owner, disabled] of [[true, false], [false, true]]) {
    const { host } = await mountAgents(owner);
    host.querySelector('#agents-new').click();
    const form = host.querySelector('.agents-form');
    const trust = [...form.querySelectorAll('.agents-check')].find((c) => /Trust the repository/.test(c.textContent));
    assert.equal(trust.querySelector('input').disabled, disabled);
    assert.match(trust.textContent, /hooks will run with this agent's privileges/);
    const addMcp = [...form.querySelectorAll('button')].find((b) => b.textContent === 'Add MCP server');
    addMcp.click();
    const stdio = [...form.querySelectorAll('.agents-mcp-row select option')].find((o) => o.value === 'stdio');
    assert.equal(stdio.disabled, disabled);
  }
});

test('Agents: creating an agent posts the API body; the StudioForge preset adds the READ-ONLY key', { skip: dom.skip }, async () => {
  const { host, api } = await mountAgents(true);
  host.querySelector('#agents-new').click();
  const form = host.querySelector('.agents-form');
  const byLabel = (re) => [...form.querySelectorAll('.people-field')].find((f) => re.test(f.querySelector('label').textContent));
  byLabel(/^Name$/).querySelector('input').value = 'Helper';
  byLabel(/^Name$/).querySelector('input').dispatchEvent(new host.ownerDocument.defaultView.Event('input'));
  [...form.querySelectorAll('button')].find((b) => b.textContent === 'Add StudioForge (read-only key)').click();
  const tools = byLabel(/Tools it may use/).querySelector('textarea');
  assert.match(tools.value, /mcp__studioforge__\*/);
  const secret = form.querySelector('.agents-mcp-row input[placeholder="STUDIOFORGE_MCP_KEY_RO"]');
  assert.equal(secret.value, 'STUDIOFORGE_MCP_KEY_RO', 'the preset names the read-only key, never an admin one');
  [...form.querySelectorAll('button')].find((b) => b.textContent === 'Save agent').click();
  await tick(10);
  const call = api.calls.find((c) => c[0] === 'agentCreate')[1];
  assert.equal(call.name, 'Helper');
  assert.deepEqual(call.spec.mcp_servers.studioforge.header_secrets, { Authorization: 'STUDIOFORGE_MCP_KEY_RO' });
  assert.equal(call.spec.mcp_servers.studioforge.privileged, false);
  assert.equal(call.spec.approval_policy.mode, 'interactive');
  assert.deepEqual(call.published_to, []);
});

test('Agents: limits, retention and the unified-dispatch switch (off by default) save through the API', { skip: dom.skip }, async () => {
  const { host, api, rec } = await mountAgents(true);
  const lim = host.querySelector('#agents-limits');
  const box = lim.querySelector('.agents-check input');
  assert.equal(box.checked, false, 'unified dispatch is off by default');
  box.checked = true;
  [...lim.querySelectorAll('button')].find((b) => b.textContent === 'Save').click();
  await tick(10);
  assert.deepEqual(api.calls.find((c) => c[0] === 'saveAgentSettings')[1], { retention_days: 30, max_concurrent: 4, unified_dispatch: true });
  [...lim.querySelectorAll('button')].find((b) => b.textContent === 'Delete old transcripts now').click();
  await tick(10);
  assert.ok(rec.toasts.some(([m]) => /Transcripts removed: 2/.test(m)));
  assert.match(lim.textContent, /about \$0\.0123 across 1 tasks/);
  assert.equal([...lim.querySelectorAll('.agents-profile-budget legend')].length, 1, 'a Restricted profile has no agent limits to set');
});

test('Agents: MCP secrets show a name and whether it is set - never a value', { skip: dom.skip }, async () => {
  const { host, api } = await mountAgents(true);
  const row = host.querySelector('.agents-secret');
  assert.match(row.textContent, /STUDIOFORGE_MCP_KEY_RO/); assert.match(row.textContent, /Not set/);
  const input = row.querySelector('input[type="password"]');
  input.value = 'ro-secret-1';
  [...row.querySelectorAll('button')].find((b) => b.textContent === 'Set value').click();
  await tick(10);
  assert.deepEqual(api.calls.find((c) => c[0] === 'saveAgentSecret'), ['saveAgentSecret', 'STUDIOFORGE_MCP_KEY_RO', 'ro-secret-1']);
});

// ------------------------------------------------------------------ wiring

test('api.js carries the agent, task and approval calls with the routes the server declares', () => {
  for (const fn of ['agentsAvailable', 'agents', 'agentCreate', 'agentSave', 'agentDelete', 'agentBackends',
    'claudeCredentials', 'saveClaudeCredentials', 'clearClaudeKey', 'agentSecrets', 'saveAgentSecret',
    'deleteAgentSecret', 'agentSettings', 'saveAgentSettings', 'agentProfileBudgets', 'saveAgentProfileBudget',
    'agentUsage', 'purgeAgentTasks', 'tasks', 'task', 'taskEvents', 'createTask', 'taskSend', 'taskInterrupt',
    'taskResume', 'taskCancel', 'taskRelease', 'taskDismiss', 'answerApproval']) {
    assert.match(API_SRC, new RegExp(`^\\s{2}${fn}:`, 'm'), fn);
  }
  assert.match(API_SRC, /\/api\/approvals\/\$\{encodeURIComponent\(requestId\)\}/);
  assert.doesNotMatch(API_SRC, /claude_ai|oauth/i);
});

test('main.js routes task frames, intercepts @mentions before sending, and mounts the pane', () => {
  assert.match(MAIN, /import \{ createTasksUi \} from '\.\/tasks\.js\?v=1'/);
  assert.match(MAIN, /import \{ mountAgentsSettings \} from '\.\/agents\.js\?v=1'/);
  assert.match(MAIN, /data\.type\.startsWith\('task\.'\)\) \{ tasksUi\.onFrame\(data\); return; \}/);
  const send = MAIN.slice(MAIN.indexOf('function sendMessage()'));
  assert.ok(send.indexOf('tasksUi.interceptSend') > -1 && send.indexOf('tasksUi.interceptSend') < send.indexOf('const draftThread'),
    'the mention check comes before anything is sent or cleared');
  assert.match(MAIN, /case 'agents':\s+await mountAgentsSettings/);
  assert.match(MAIN, /tasksUi\.cardForMessage\(msg\)/);
  assert.match(MAIN, /tasksUi\.closeAll\(\);/);
  assert.match(MAIN, /act === 'assign'/);
});

test('index.html: the rail button, the thread-menu item, the Agents tab and pane, the stylesheet', () => {
  assert.match(HTML, /id="tasks-btn"[^>]*type="button"/);
  assert.match(HTML, /id="thread-menu-assign" hidden/);
  assert.match(HTML, /id="stab-agents"[^>]*aria-controls="spane-agents"/);
  assert.match(HTML, /id="spane-agents"[^>]*aria-labelledby="stab-agents"/);
  assert.match(HTML, /\/static\/tasks\.css\?v=1/);
});

test('sw.js precaches the new modules and stylesheet', () => {
  for (const f of ['tasks', 'task_card', 'approval_card', 'agents']) assert.ok(SW.includes(`'/static/js/${f}.js'`), f);
  assert.ok(SW.includes("'/static/tasks.css'"));
});

test('no UI string uses a forbidden product label; the verbs are Assign task / Send to agent', () => {
  const bad = new RegExp(`${'Claude'} ${'Co'}${'de'}|${'Claude'} ${'Co'}${'work'}`, 'i');
  for (const ns of ['tasks', 'approval', 'agents']) assert.doesNotMatch(JSON.stringify(EN[ns]), bad);
  assert.equal(EN.tasks.assign, 'Assign task');
  assert.equal(EN.tasks.send_followup, 'Send to agent');
  assert.equal(EN.agents.claude_title, 'Claude Agent');
  assert.equal(EN.agents.route_google, 'Google Cloud');
  assert.doesNotMatch(JSON.stringify(EN.agents), /subscription login/i);
});
