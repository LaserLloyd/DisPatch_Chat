// The task card: one agent task as a live timeline.
//
//   header      the agent, its backend ("OpenClaw" / "Claude Agent"), the state, the cost (always
//               labelled "estimate": the figures are computed client-side by the SDK and are
//               never billing data) and how long it ran
//   timeline    what the agent said and did, as a tree: the main agent's text and tool calls,
//               and each subagent as a nested group (events carry `parent_id`); tool calls fold
//               open to show their arguments and result; approvals appear INLINE, in order,
//               as approval cards that stay in the timeline once answered
//   result      the agent's final answer, plus any artifacts it produced
//   controls    Stop (interrupt, resumable), Resume, a steer / follow-up box, End task (cancel
//               for good) and, for a hand-off card waiting for its owner, Send / Dismiss
//
// The card is fed two ways and tolerates both at once: `load(task, events, approvals)` for the
// history the server already has, and `applyFrame(frame)` for the live `task.event` /
// `task.approval` / `task.update` frames. `seq` is the cursor: a frame that is not exactly the
// next event means a gap, and the card asks the server for the events it missed instead of
// guessing. Text is rendered as TEXT (an agent's words may quote anything it read on the web);
// there is no markdown or HTML path in this file.
//
// DOM is built incrementally: an event appends or patches one element, so a focused button in
// an approval card is never torn down by an unrelated delta arriving.

import { el, spriteIcon } from './util.js?v=21';
import { t } from './i18n.js?v=4';
import { approvalCardEl, approvalView } from './approval_card.js?v=2';

export const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);
export const ACTIVE = new Set(['running', 'awaiting_approval']);
const SUBAGENT_TOOLS = new Set(['Agent', 'Task']);
const OUTPUT_PREVIEW = 4000;

const STATE_KEYS = {
  queued: 'tasks.state_queued',
  running: 'tasks.state_running',
  awaiting_approval: 'tasks.state_awaiting_approval',
  succeeded: 'tasks.state_succeeded',
  failed: 'tasks.state_failed',
  cancelled: 'tasks.state_cancelled',
};
export const stateLabel = (s) => (STATE_KEYS[s] ? t(STATE_KEYS[s]) : String(s || ''));

// ----------------------------------------------------------------------------- pure model

let nodeSeq = 0;

/** The timeline model: events in, a tree of nodes out. No DOM. */
export function createModel() {
  return {
    lastSeq: -1,
    top: [],                 // top-level nodes, in order
    byId: new Map(),         // call_id / subagent_id -> container node
    approvals: new Map(),    // request_id -> approval view
    artifacts: [],
    cost: null,              // {usd, input, output, turns}
    result: null,            // the result event
    error: null,             // the last error event
    state: 'queued',
  };
}

function listFor(model, parentId) {
  const parent = parentId ? model.byId.get(parentId) : null;
  return parent ? parent.children : model.top;
}

function newNode(kind, extra) {
  return { id: ++nodeSeq, kind, children: [], ...extra };
}

/**
 * Apply one event to the model. Returns {node, created, parentList} describing what changed so a
 * renderer can patch just that, or null when the event changes nothing visible (a status flip).
 */
export function applyEvent(model, ev) {
  if (!ev || typeof ev.seq !== 'number' || ev.seq <= model.lastSeq) return null;
  model.lastSeq = ev.seq;
  const list = listFor(model, ev.parent_id);
  switch (ev.type) {
    case 'status': {
      model.state = ev.state || model.state;
      if (ev.detail === 'resumed') {
        const node = newNode('note', { text: 'resumed' });
        model.top.push(node);
        return { node, created: true, list: model.top };
      }
      return null;
    }
    case 'text_delta': {
      const last = list[list.length - 1];
      if (last && last.kind === 'text') {
        last.text = ev.replace ? ev.text : last.text + ev.text;
        return { node: last, created: false, list };
      }
      const node = newNode('text', { text: ev.text || '' });
      list.push(node);
      return { node, created: true, list };
    }
    case 'tool_call': {
      const sub = SUBAGENT_TOOLS.has(ev.name);
      const node = newNode(sub ? 'subagent' : 'tool', {
        callId: ev.call_id, name: ev.name, input: ev.input || {}, ok: null, output: null, done: false,
        description: sub ? String((ev.input && (ev.input.description || ev.input.prompt)) || '') : '',
        agentType: sub ? String((ev.input && ev.input.subagent_type) || '') : '',
      });
      list.push(node);
      model.byId.set(ev.call_id, node);
      return { node, created: true, list };
    }
    case 'tool_result': {
      let node = model.byId.get(ev.call_id);
      if (!node) {
        node = newNode('tool', { callId: ev.call_id, name: ev.name || '?', input: {}, ok: null, output: null });
        list.push(node);
        model.byId.set(ev.call_id, node);
        node.ok = ev.ok !== false;
        node.output = ev.output || '';
        node.done = true;
        return { node, created: true, list };
      }
      node.ok = ev.ok !== false;
      node.output = ev.output || '';
      node.done = true;
      return { node, created: false, list };
    }
    case 'subagent_started': {
      let node = model.byId.get(ev.subagent_id);
      if (node) {
        node.kind = 'subagent';
        node.agentType = ev.name || node.agentType;
        node.description = ev.description || node.description;
        return { node, created: false, list };
      }
      node = newNode('subagent', {
        callId: ev.subagent_id, name: 'Agent', input: {}, ok: null, output: null, done: false,
        description: ev.description || '', agentType: ev.name || '',
      });
      list.push(node);
      model.byId.set(ev.subagent_id, node);
      return { node, created: true, list };
    }
    case 'subagent_finished': {
      const node = model.byId.get(ev.subagent_id);
      if (!node) return null;
      node.done = true;
      node.ok = ev.ok !== false;
      node.summary = ev.summary || '';
      return { node, created: false, list };
    }
    case 'approval_request': {
      const view = approvalView({ ...ev, tool: ev.tool_name, state: 'pending' });
      model.approvals.set(view.id, view);
      const node = newNode('approval', { requestId: view.id });
      list.push(node);
      return { node, created: true, list };
    }
    case 'retry': {
      const node = newNode('retry', { attempt: ev.attempt, max: ev.max_attempts, reason: ev.reason || '' });
      list.push(node);
      return { node, created: true, list };
    }
    case 'artifact': {
      const a = { id: ev.artifact_id, name: ev.name || ev.path || ev.artifact_id, path: ev.path || '',
        kind: ev.kind, size: ev.size, mime: ev.mime };
      model.artifacts.push(a);
      const node = newNode('artifact', a);
      list.push(node);
      return { node, created: true, list };
    }
    case 'cost_update':
      model.cost = { usd: ev.total_cost_usd, input: ev.input_tokens, output: ev.output_tokens, turns: ev.turns };
      return null;
    case 'error': {
      model.error = { code: ev.code, message: ev.message };
      const node = newNode('error', { code: ev.code, message: ev.message });
      model.top.push(node);
      return { node, created: true, list: model.top };
    }
    case 'result':
      model.result = ev;
      model.state = ev.state || model.state;
      return null;
    default:
      return null;                       // unknown event types are ignored (tolerant reader)
  }
}

/** Fold a whole event list into a fresh model. */
export function foldEvents(events) {
  const m = createModel();
  for (const ev of events || []) applyEvent(m, ev);
  return m;
}

/** The text a tool call's header shows: its main argument, clipped. */
export function toolPreview(node) {
  const inp = node.input || {};
  const v = inp.command ?? inp.file_path ?? inp.path ?? inp.url ?? inp.query ?? inp.pattern ?? '';
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  const one = s.replace(/\s+/g, ' ').trim();
  return one.length > 90 ? `${one.slice(0, 89)}…` : one;
}

/** "estimate" cost line for a task row (cost_usd may be null: OpenClaw reports tokens only). */
export function costLabel(task, model) {
  const usd = task && task.cost_usd != null ? task.cost_usd : model && model.cost ? model.cost.usd : null;
  if (usd != null) return t('tasks.cost_usd_estimate', { amount: formatUsd(usd) });
  const tok = task && task.tokens ? task.tokens : model && model.cost ? (model.cost.input || 0) + (model.cost.output || 0) : 0;
  if (tok) return t('tasks.cost_tokens_estimate', { count: tok.toLocaleString() });
  return '';
}

export function formatUsd(n) {
  const v = Number(n) || 0;
  // Small sums keep their significant digits ($0.0123), the rest read as money ($1.50).
  return `$${v >= 0.1 || v === 0 ? v.toFixed(2) : v.toFixed(4).replace(/(\.\d\d\d*?)0+$/, '$1')}`;
}

export function durationLabel(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return '';
  const s = Math.round(seconds);
  if (s < 60) return t('tasks.duration_s', { s });
  const m = Math.floor(s / 60);
  return m < 60 ? t('tasks.duration_m', { m, s: s % 60 }) : t('tasks.duration_h', { h: Math.floor(m / 60), m: m % 60 });
}

// ----------------------------------------------------------------------------- the card

/**
 * deps: {
 *   api               the REST client (js/api.js): task, taskEvents, taskSend, taskInterrupt,
 *                     taskResume, taskCancel, taskRelease, taskDismiss, answerApproval
 *   canApprove        may this person answer approval cards (approve_agent_actions)?
 *   confirm           async (message, {danger, okText}) => boolean
 *   toast             (message, isError) => void
 *   announce          (message) => void   - optional screen-reader live text
 *   compact           in a chat bubble: only the tail of the timeline, expandable
 *   showOwner         show who started it (an Owner / Admin looking at everyone's)
 *   onChange          (task) => void      - the task row changed (state, cost)
 * }
 */
export function createTaskCard(deps) {
  const { api } = deps;
  const model = createModel();
  let task = null;
  let disposed = false;
  let catching = false;
  const dirty = new Set();
  let raf = 0;
  const approvalEls = new Map();
  const nodeEls = new Map();

  const root = el('section', { class: `task-card${deps.compact ? ' task-card-compact' : ''}`, 'aria-busy': 'false' });
  const head = el('header', { class: 'task-head' });
  const live = el('p', { class: 'sr-only', role: 'status', 'aria-live': 'polite' });
  const promptBox = el('details', { class: 'task-prompt' });
  const timeline = el('div', { class: 'task-timeline', role: 'log', 'aria-label': t('tasks.timeline_aria') });
  const resultBox = el('div', { class: 'task-result', hidden: '' });
  const controls = el('div', { class: 'task-controls' });
  root.append(live, head, promptBox, timeline, resultBox, controls);

  const say = (msg) => {
    live.textContent = msg;
    if (deps.announce) deps.announce(msg);
  };

  // ---- rendering of nodes (incremental) ----

  function bodyOf(parentNode) {
    if (!parentNode) return timeline;
    const rec = nodeEls.get(parentNode.id);
    return rec ? rec.children : timeline;
  }

  function renderTool(node) {
    const isSub = node.kind === 'subagent';
    const summary = el('summary', { class: 'task-tool-sum' });
    const mark = el('span', { class: 'task-tool-mark', 'aria-hidden': 'true' });
    const label = el('span', { class: 'task-tool-name' });
    const prev = el('span', { class: 'task-tool-preview', dir: 'ltr' });
    summary.append(mark, label, prev);
    const details = el('details', { class: `task-tool${isSub ? ' task-sub' : ''}` });
    if (isSub) details.open = true;
    const io = el('div', { class: 'task-tool-io' });
    const children = el('div', { class: 'task-children' });
    details.append(summary, io, children);
    const rec = { el: details, summary, mark, label, prev, io, children };
    nodeEls.set(node.id, rec);
    return rec;
  }

  function paintTool(node) {
    const rec = nodeEls.get(node.id);
    if (!rec) return;
    const isSub = node.kind === 'subagent';
    rec.el.className = `task-tool${isSub ? ' task-sub' : ''}${node.done ? (node.ok ? ' task-ok' : ' task-bad') : ' task-wait'}`;
    rec.mark.replaceChildren(...(node.done ? [spriteIcon(node.ok ? 'check' : 'x', 14)] : ['…']));
    if (isSub) {
      rec.label.textContent = t('tasks.subagent', { name: node.agentType || node.name });
      rec.prev.textContent = node.description || '';
    } else {
      rec.label.textContent = node.name;
      rec.prev.textContent = toolPreview(node);
    }
    const parts = [];
    if (!isSub && node.input && Object.keys(node.input).length) {
      parts.push(el('pre', { class: 'task-io-in', dir: 'ltr', text: safeJson(node.input) }));
    }
    if (node.output) {
      const out = node.output.length > OUTPUT_PREVIEW ? `${node.output.slice(0, OUTPUT_PREVIEW)}\n…` : node.output;
      parts.push(el('pre', { class: `task-io-out${node.ok === false ? ' task-io-bad' : ''}`, dir: 'ltr', text: out }));
    }
    if (isSub && node.summary) parts.push(el('p', { class: 'task-sub-summary', text: node.summary }));
    rec.io.replaceChildren(...parts);
  }

  function renderNode(node) {
    switch (node.kind) {
      case 'text': {
        const p = el('div', { class: 'task-text', dir: 'auto' });
        nodeEls.set(node.id, { el: p });
        p.textContent = node.text;
        return p;
      }
      case 'tool':
      case 'subagent': {
        const rec = renderTool(node);
        paintTool(node);
        return rec.el;
      }
      case 'approval': {
        const a = model.approvals.get(node.requestId);
        const card = approvalCardEl(a, { canAnswer: !!deps.canApprove, onAnswer: (decision, scope) => answer(node.requestId, decision, scope) });
        approvalEls.set(node.requestId, card);
        nodeEls.set(node.id, { el: card });
        return card;
      }
      case 'retry':
        return el('p', { class: 'task-note task-retry', text: t('tasks.retry', { attempt: node.attempt || 1, reason: node.reason || '' }) });
      case 'artifact':
        return el('p', { class: 'task-note task-artifact' }, [
          el('span', { 'aria-hidden': 'true', text: '\u{1F4C4} ' }),
          el('strong', { text: node.name }),
          node.size != null ? el('span', { class: 'muted', text: ` · ${formatBytes(node.size)}` }) : null,
        ]);
      case 'error':
        return el('p', { class: 'task-note task-error', role: 'alert' }, [
          el('strong', { text: t('tasks.error') }), ` ${node.message || node.code}`,
        ]);
      case 'note':
        return el('p', { class: 'task-note', text: t('tasks.resumed_note') });
      default:
        return el('span');
    }
  }

  function patch(info) {
    if (!info) return;
    const { node, created, list } = info;
    if (created) {
      const parentNode = findParent(node, list);
      const host = bodyOf(parentNode);
      host.append(renderNode(node));
      if (node.kind === 'text') dirty.add(node);
    } else if (node.kind === 'text') {
      dirty.add(node);
    } else if (node.kind === 'tool' || node.kind === 'subagent') {
      paintTool(node);
    }
    schedule();
  }

  function findParent(node, list) {
    if (list === model.top) return null;
    for (const cand of model.byId.values()) if (cand.children === list) return cand;
    return null;
  }

  function schedule() {
    if (raf || disposed) return;
    // One repaint per frame; a hidden tab pauses animation frames, so a timer is the backstop and
    // whichever fires first does the work (the other finds nothing left to do).
    const run = () => {
      if (!raf) return;
      if (typeof cancelAnimationFrame === 'function' && raf.frame) cancelAnimationFrame(raf.frame);
      clearTimeout(raf.timer);
      raf = 0;
      for (const node of dirty) {
        const rec = nodeEls.get(node.id);
        if (rec && node.kind === 'text') rec.el.textContent = node.text;
      }
      dirty.clear();
      paintHead();
      paintResult();
      paintControls();
    };
    raf = { frame: typeof requestAnimationFrame === 'function' ? requestAnimationFrame(run) : 0, timer: setTimeout(run, 120) };
  }

  // ---- header, result, controls ----

  function paintHead() {
    if (!task) return;
    const state = model.state && TERMINAL.has(model.state) ? model.state : task.state;
    const cost = costLabel(task, model);
    const pending = [...model.approvals.values()].filter((a) => a.state === 'pending').length;
    root.dataset.state = task.state;
    root.dataset.taskId = task.id;
    root.setAttribute('aria-busy', String(ACTIVE.has(task.state)));
    const bits = [
      el('span', { class: 'task-agent', text: task.agent_name }),
      el('span', { class: 'task-backend', text: task.backend_label || task.backend }),
      el('span', { class: `task-pill task-pill-${task.state}`, text: stateLabel(state) }),
    ];
    if (task.hold === 'send') bits.push(el('span', { class: 'task-pill task-pill-hold', text: t('tasks.waiting_for_send') }));
    if (pending) bits.push(el('span', { class: 'task-pill task-pill-hold', text: t('tasks.approvals_waiting', { count: pending }) }));
    const meta = [];
    if (deps.showOwner && task.owner_name) meta.push(task.owner_name);
    if (cost) meta.push(cost);
    if (task.turns) meta.push(t('tasks.turns', { count: task.turns }));
    const dur = durationLabel(task.duration_s);
    if (dur) meta.push(dur);
    head.replaceChildren(
      el('div', { class: 'task-title' }, bits),
      meta.length ? el('div', { class: 'task-meta', text: meta.join(' · ') }) : null,
    );
  }

  function paintPrompt() {
    if (!task || !task.prompt) {
      promptBox.hidden = true;
      return;
    }
    promptBox.hidden = false;
    promptBox.replaceChildren(
      el('summary', { text: t('tasks.prompt') }),
      el('div', { class: 'task-prompt-text', dir: 'auto', text: task.prompt }),
    );
  }

  function paintResult() {
    const text = (model.result && model.result.text) || (task && task.result_text) || '';
    if (!text || !task || task.state !== 'succeeded') {
      resultBox.hidden = true;
      return;
    }
    resultBox.hidden = false;
    resultBox.replaceChildren(
      el('h4', { class: 'task-result-title', text: t('tasks.result') }),
      el('div', { class: 'task-result-text', dir: 'auto', text }),
    );
  }

  let steerInput = null;

  let controlsSig = '';

  function paintControls() {
    if (!task) return;
    // Rebuild only when the controls themselves change. A live frame arrives many times a second;
    // replacing the buttons each time would drop keyboard focus from Stop and the steer box.
    const sig = [task.state, task.hold, !!task.resumable, !!deps.canApprove].join('|');
    if (sig === controlsSig) return;
    controlsSig = sig;
    const state = task.state;
    const active = ACTIVE.has(state);
    const kids = [];
    const btn = (key, cls, fn, extra = {}) => {
      const b = el('button', { type: 'button', class: `btn ${cls}`, text: t(key), ...extra });
      b.addEventListener('click', async () => {
        b.disabled = true;
        try { await fn(); } catch (e) { fail(e); } finally { b.disabled = false; }
      });
      return b;
    };
    if (task.hold === 'send') {
      if (deps.canApprove) {
        kids.push(btn('tasks.send_held', 'btn-primary', () => api.taskRelease(task.id)));
        kids.push(btn('tasks.dismiss_held', 'btn-secondary', () => api.taskDismiss(task.id)));
      }
      controls.replaceChildren(...kids);
      return;
    }
    if (active) kids.push(btn('tasks.stop', 'btn-secondary task-stop', () => api.taskInterrupt(task.id)));
    if (!active && task.resumable && state !== 'succeeded') {
      kids.push(btn('tasks.resume', 'btn-primary', () => api.taskResume(task.id)));
    }
    if (active || task.resumable) {
      // The textarea node is reused across repaints so a half-typed message survives a frame.
      if (!steerInput) {
        steerInput = el('textarea', { class: 'task-steer-input', rows: '2', maxlength: '8000', dir: 'auto' });
        steerInput.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); const b = controls.querySelector('.task-steer .btn'); if (b) b.click(); }
        });
      }
      steerInput.setAttribute('aria-label', t(active ? 'tasks.steer_aria' : 'tasks.followup_aria'));
      steerInput.placeholder = t(active ? 'tasks.steer_placeholder' : 'tasks.followup_placeholder');
      const steerSend = btn(active ? 'tasks.steer' : 'tasks.send_followup', 'btn-secondary', async () => {
        const msg = steerInput.value.trim();
        if (!msg) return;
        await api.taskSend(task.id, msg);
        steerInput.value = '';
      });
      kids.push(el('div', { class: 'task-steer' }, [steerInput, steerSend]));
    }
    if (!TERMINAL.has(state) || task.resumable) {
      kids.push(btn('tasks.end', 'btn-secondary task-end', async () => {
        const ok = deps.confirm ? await deps.confirm(t('tasks.end_confirm'), { danger: true, okText: t('tasks.end') }) : true;
        if (ok) await api.taskCancel(task.id);
      }));
    }
    controls.replaceChildren(...kids);
  }

  function fail(e) {
    const msg = String((e && (e.userMessage || e.message)) || e);
    const fix = e && e.fix ? ` ${e.fix}` : '';
    if (deps.toast) deps.toast(msg + fix, true);
  }

  async function answer(requestId, decision, scope) {
    const res = await api.answerApproval(requestId, decision, scope);
    const a = res && res.approval ? res.approval : { request_id: requestId, state: decision, scope };
    setApproval(a);
    return res;
  }

  function setApproval(a) {
    const view = approvalView(a);
    if (!view.id) return;
    const prev = model.approvals.get(view.id) || {};
    model.approvals.set(view.id, { ...prev, ...view, tool: view.tool || prev.tool, args: view.args || prev.args,
      scopes: a.scopes ? view.scopes : prev.scopes || view.scopes, canTask: a.scopes ? view.canTask : prev.canTask });
    const card = approvalEls.get(view.id);
    if (card) card.update(model.approvals.get(view.id));
    schedule();
  }

  // ---- feeding it ----

  /** Load the history. `approvals` are the server's approval rows (with their decisions). */
  function load(row, events, approvals) {
    task = row;
    for (const a of approvals || []) {
      const v = approvalView(a);
      model.approvals.set(v.id, v);
    }
    for (const ev of events || []) {
      const info = applyEvent(model, ev);
      // the approval rows carry the decision; the event only the request
      if (info && info.node.kind === 'approval') {
        const have = (approvals || []).find((x) => x.request_id === info.node.requestId);
        if (have) model.approvals.set(have.request_id, approvalView(have));
      }
      patch(info);
    }
    if (model.lastSeq < row.last_seq && !catching) catchUp();
    paintHead();
    paintPrompt();
    paintResult();
    paintControls();
    maybeCompact();
  }

  async function catchUp() {
    if (catching || disposed || !task) return;
    catching = true;
    try {
      for (let guard = 0; guard < 20; guard++) {
        const res = await api.taskEvents(task.id, model.lastSeq);
        const evs = (res && res.events) || [];
        if (!evs.length) break;
        for (const ev of evs) patch(applyEvent(model, ev));
        if (evs.length < 2000) break;
      }
      const fresh = await api.task(task.id);
      if (fresh && fresh.task) setTask(fresh.task, fresh.approvals);
    } catch { /* offline: the next frame or reload catches up */ } finally {
      catching = false;
    }
  }

  function setTask(row, approvals) {
    const before = task && task.state;
    task = { ...(task || {}), ...row };
    if (row.state && TERMINAL.has(row.state) && !model.result) model.state = row.state;
    for (const a of approvals || []) setApproval(a);
    if (deps.onChange) deps.onChange(task);
    if (before && before !== task.state) {
      if (task.state === 'succeeded') say(t('tasks.said_done', { agent: task.agent_name }));
      else if (task.state === 'failed') say(t('tasks.said_failed', { agent: task.agent_name }));
    }
    paintHead();
    paintPrompt();
    paintResult();
    paintControls();
  }

  /** A live frame from the WebSocket. */
  function applyFrame(frame) {
    if (disposed || !task || frame.task_id !== task.id) return;
    if (frame.type === 'task.event') {
      const ev = frame.event;
      if (ev.seq > model.lastSeq + 1) {
        if (task) task = { ...task, ...frame.task };
        catchUp();                                  // a gap: ask for what we missed
        return;
      }
      patch(applyEvent(model, ev));
      if (ev.type === 'approval_request') say(t('approval.said_needed', { tool: ev.tool_name }));
      setTask({ ...task, ...frame.task, ...(ev.type === 'result' ? { resumable: ev.resumable, state: ev.state, reason: ev.reason, result_text: ev.text ?? task.result_text } : {}) });
    } else if (frame.type === 'task.approval') {
      setApproval({ request_id: frame.request_id, tool: frame.tool, args_summary: frame.args_summary, scopes: frame.scopes, state: frame.state, decided_by: frame.decided_by });
    } else if (frame.type === 'task.update') {
      setTask({ ...task, ...frame.task });
    }
  }

  function maybeCompact() {
    if (!deps.compact) return;
    root.dataset.compact = '1';
  }

  function dispose() {
    disposed = true;
    if (raf) {
      if (typeof cancelAnimationFrame === 'function' && raf.frame) cancelAnimationFrame(raf.frame);
      clearTimeout(raf.timer);
    }
    raf = 0;
  }

  return {
    el: root, load, setTask, applyFrame, dispose, catchUp,
    get taskId() { return task && task.id; },
    get model() { return model; },
    get task() { return task; },
    get connected() { return root.isConnected; },
  };
}

function safeJson(v) {
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}

function formatBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / 1024 / 1024).toFixed(1)} MB`;
}
