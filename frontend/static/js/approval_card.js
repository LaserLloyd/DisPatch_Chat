// Approval cards: an agent asks before it uses a tool, and a person answers here.
//
// The server emits an `approval_request` event (and a `task.approval` frame) whenever a tool
// call needs a person. Destructive tools - a shell, a file writer, anything that sends, posts,
// deletes or creates - ask EVERY time and only offer "once": the card hides "for this task"
// when the request's `scopes` do not include it, and the server refuses it anyway.
//
// The card shows the tool and a short, already-redacted summary of its arguments. The summary
// comes from the server (agents_service.args_summary); this module renders it as text, never
// as markup. Buttons are real <button>s in the Tab order, in a labelled group, so the whole
// card works from the keyboard and a screen reader announces what is being asked.
//
// Pure DOM, no network: the caller passes `onAnswer(decision, scope)`.

import { el, spriteIcon } from './util.js?v=21';
import { t } from './i18n.js?v=5';

let seq = 0;

/** Normalise a server approval (an `approvals` row, or a `task.approval` frame) for rendering. */
export function approvalView(a) {
  const scopes = Array.isArray(a && a.scopes) && a.scopes.length ? a.scopes : ['once'];
  const state = a && (a.state || a.decision) ? String(a.state || a.decision) : 'pending';
  const id = String((a && (a.request_id || a.id)) || '');
  const args = String((a && (a.args_summary ?? a.args)) || '');
  // The result is itself a valid input (idempotent): the card re-normalises what the model stores.
  return {
    id, request_id: id,
    tool: String((a && (a.tool || a.tool_name)) || ''),
    args, args_summary: args,
    scopes,
    canTask: scopes.includes('task'),
    state: ['pending', 'allow', 'deny', 'expired'].includes(state) ? state : 'pending',
    scope: a && a.scope ? String(a.scope) : '',
    decided_by: (a && a.decided_by) || '',
  };
}

const STATE_KEYS = {
  pending: 'approval.state_pending',
  allow: 'approval.state_allow',
  deny: 'approval.state_deny',
  expired: 'approval.state_expired',
};

/**
 * One approval card.
 *   approval   an approval row / frame (see approvalView)
 *   canAnswer  false for a person who may see the task but not answer it (no
 *              approve_agent_actions): the buttons are replaced by a sentence
 *   onAnswer   async (decision 'allow'|'deny', scope 'once'|'task') => void
 * Returns the element; call `el.update(newApproval)` to repaint in place.
 */
export function approvalCardEl(approval, { canAnswer = true, onAnswer = async () => {} } = {}) {
  const root = el('div', { class: 'approval-card', role: 'group' });
  const titleId = `approval-t-${++seq}`;
  root.setAttribute('aria-labelledby', titleId);

  const paint = (a) => {
    const v = approvalView(a);
    root.dataset.state = v.state;
    root.dataset.requestId = v.id;
    root.className = `approval-card approval-${v.state}`;
    const kids = [
      el('div', { class: 'approval-head' }, [
        el('span', { class: 'approval-icon', 'aria-hidden': 'true' }, [spriteIcon(v.state === 'pending' ? 'pause' : v.state === 'allow' ? 'check' : 'x', 16)]),
        el('strong', { id: titleId, class: 'approval-title', text: t('approval.title', { tool: v.tool }) }),
        el('span', { class: 'approval-state', text: t(STATE_KEYS[v.state]) }),
      ]),
    ];
    if (v.args) {
      kids.push(el('pre', {
        class: 'approval-args', tabindex: '0', dir: 'ltr',
        'aria-label': t('approval.args_aria'), text: v.args,
      }));
    }
    if (v.state === 'pending') {
      if (!canAnswer) {
        kids.push(el('p', { class: 'approval-note', text: t('approval.waiting_for_admin') }));
      } else {
        const buttons = [];
        const answer = async (decision, scope, btn) => {
          for (const b of buttons) b.disabled = true;
          btn.classList.add('busy');
          try {
            await onAnswer(decision, scope);
          } catch (e) {
            for (const b of buttons) b.disabled = false;
            btn.classList.remove('busy');
            status.textContent = String((e && (e.userMessage || e.message)) || e);
            status.hidden = false;
          }
        };
        const mk = (cls, key, decision, scope) => {
          const b = el('button', { type: 'button', class: `btn ${cls}`, 'data-act': `${decision}-${scope}`, text: t(key) });
          b.addEventListener('click', () => answer(decision, scope, b));
          buttons.push(b);
          return b;
        };
        const status = el('span', { class: 'approval-error', role: 'alert', hidden: '' });
        kids.push(el('div', { class: 'approval-actions' }, [
          mk('btn-primary', 'approval.allow_once', 'allow', 'once'),
          v.canTask ? mk('btn-secondary', 'approval.allow_task', 'allow', 'task') : null,
          mk('btn-secondary approval-deny', 'approval.deny', 'deny', 'once'),
          status,
        ]));
        if (!v.canTask) kids.push(el('p', { class: 'approval-note', text: t('approval.asks_every_time') }));
      }
    } else if (v.state === 'allow' && v.scope === 'task') {
      kids.push(el('p', { class: 'approval-note', text: t('approval.allowed_for_task') }));
    }
    root.replaceChildren(...kids.filter(Boolean));
  };
  root.update = paint;
  paint(approval);
  return root;
}
