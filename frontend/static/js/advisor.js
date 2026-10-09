// Advisor handoff cards — "want me to have this done?"
//
// An advisor bot (backend/app/advisor.py) answers straight from a model API
// and can hand work to agents. Research goes out on its own; anything that
// DOES something is proposed instead: the server posts the brief as an
// ordinary assistant message whose metadata carries `advisor_handoff`, and
// nothing runs until somebody with an unlocked session presses Send here.
//
// The card's text comes from the SERVER-STAMPED metadata
// (`advisor_handoff.brief`), never from the message body: a body is whatever
// was posted, and a card that showed one brief while its Send approved
// another would be the whole attack. Send / Dismiss also carry the id of the
// message the button sits on, and the server refuses unless it is the card it
// recorded for that request. The server re-stamps the card's metadata on
// every transition and broadcasts `message_update`, so a reload — or another
// device — shows the same state without asking.

import { el } from './util.js?v=20';
import { t } from './i18n.js?v=3';

const STATES = ['proposed', 'running', 'done', 'failed', 'dismissed'];

/** The handoff on a message, or null when the message is not a handoff card. */
export function handoffState(msg) {
  const h = msg && msg.metadata && msg.metadata.advisor_handoff;
  if (!h || typeof h !== 'object' || typeof h.id !== 'string' || !h.id) return null;
  const state = STATES.includes(h.state) ? h.state : 'proposed';
  return {
    id: h.id,
    messageId: String((msg && msg.id) || ''),
    agent: String(h.agent || ''),
    brief: typeof h.brief === 'string' ? h.brief : '',
    state,
    actionable: state === 'proposed',
    error: String(h.error || ''),
    resultPath: String(h.result_path || ''),
  };
}

/**
 * The Markdown a handoff card shows: its title and the brief, both from the
 * server-stamped metadata. Null when the message is not a handoff card.
 */
export function handoffBodyMarkdown(msg) {
  const h = handoffState(msg);
  if (!h) return null;
  // The agent id is [a-z0-9_-] server-side; strip anything else regardless.
  const agent = h.agent.replace(/[^A-Za-z0-9_-]/g, '');
  return `**${t('advisor.card_title', { agent })}**\n\n${h.brief}`;
}

/**
 * The strip under a handoff card. `decoy` hides the buttons (a locked device
 * may see that something was proposed, never approve it). `send` / `dismiss`
 * are async callbacks taking (request id, card message id).
 */
export function handoffStripEl(msg, { decoy = false, send, dismiss } = {}) {
  const h = handoffState(msg);
  if (!h) return null;
  const strip = el('div', { class: `advisor-handoff advisor-${h.state}` });
  const label = el('span', { class: 'advisor-state', text: t(`advisor.state_${h.state}`, { agent: h.agent }) });
  strip.append(label);
  if (h.state === 'failed' && h.error) {
    strip.append(el('span', { class: 'advisor-error', dir: 'auto', text: h.error }));
  }
  if (!h.actionable || decoy) return strip;

  const sendBtn = el('button', { type: 'button', class: 'btn btn-primary advisor-send',
    text: t('advisor.send', { agent: h.agent }) });
  const dismissBtn = el('button', { type: 'button', class: 'btn advisor-dismiss',
    text: t('advisor.dismiss') });
  const act = async (fn, btn) => {
    sendBtn.disabled = dismissBtn.disabled = true;
    btn.classList.add('busy');
    try {
      await fn(h.id, h.messageId);
      // The server's message_update repaints the card; until it lands, say so.
      label.textContent = t(fn === send ? 'advisor.state_running' : 'advisor.state_dismissed',
        { agent: h.agent });
      sendBtn.remove();
      dismissBtn.remove();
    } catch (e) {
      sendBtn.disabled = dismissBtn.disabled = false;
      btn.classList.remove('busy');
      label.textContent = t('advisor.send_failed', { error: (e && e.message) || '' });
    }
  };
  sendBtn.addEventListener('click', () => act(send, sendBtn));
  dismissBtn.addEventListener('click', () => act(dismiss, dismissBtn));
  strip.append(el('span', { class: 'advisor-actions' }, [dismissBtn, sendBtn]));
  return strip;
}
