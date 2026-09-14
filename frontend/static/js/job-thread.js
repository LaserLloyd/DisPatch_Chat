// Job header card — prepended above messages in the chat when the active
// thread's bot_id is 'jobboard'. A no-op for every other bot (mirrors
// dashboard mount pattern, main.js:24 / 2905).
//
// One card per thread. The card shows the structured metadata + vote
// controls + a "why" expand for the score explanation. The existing
// composer and message renderer stay unchanged — the card lives above the
// messages list and pushes everything down, not over.

import { t } from './i18n.js?v=3';
import { api } from './api.js?v=22';

const REASONS = [
  'wrong_location', 'too_senior', 'too_junior', 'compensation',
  'company', 'wrong_domain', 'already_applied', 'duplicate', 'other',
];

const voteState = new WeakMap();   // threadId -> 'pending'|'yes'|'no'|'maybe'|'applied'|'archived'

function _salaryRange(j) {
  const a = j.salary_min, b = j.salary_max, c = j.salary_currency || 'USD';
  if (a && b) return `${a.toLocaleString()}–${b.toLocaleString()} ${c}`;
  if (a) return `${a.toLocaleString()} ${c}`;
  if (b) return `${b.toLocaleString()} ${c}`;
  return '';
}

function _header(job) {
  const head = document.createElement('div');
  head.className = 'job-card__head';

  const chip = document.createElement('span');
  chip.className = `job-chip job-chip-state job-chip-state--${job.effective_state || job.state || 'pending'}`;
  chip.textContent = t(`jobs.state.${job.effective_state || job.state || 'pending'}`);
  head.appendChild(chip);

  const title = document.createElement('h3');
  title.className = 'job-card__title';
  title.textContent = job.title || t('jobs.untitled');
  head.appendChild(title);

  if (job.is_expired) {
    const badge = document.createElement('span');
    badge.className = 'job-chip job-chip--expired';
    badge.textContent = t('jobs.badge.expired');
    head.appendChild(badge);
  }
  return head;
}

function _meta(job) {
  const m = document.createElement('div');
  m.className = 'job-card__meta';
  const parts = [];
  if (job.company) parts.push(job.company);
  if (job.location) parts.push(job.location);
  if (job.remote_type && job.remote_type !== 'unknown') parts.push(job.remote_type);
  const salary = _salaryRange(job);
  if (salary) parts.push(salary);
  m.textContent = parts.join(' · ');
  return m;
}

function _tags(job) {
  const wrap = document.createElement('div');
  wrap.className = 'job-card__tags';
  for (const tag of (job.tags || [])) {
    const c = document.createElement('span');
    c.className = 'job-chip';
    c.textContent = tag;
    wrap.appendChild(c);
  }
  return wrap;
}

function _why(score) {
  if (!score) return null;
  const wrap = document.createElement('details');
  wrap.className = 'job-card__why';
  const summary = document.createElement('summary');
  summary.className = 'job-card__why-summary';
  if (score.embedding_unavailable) summary.textContent = t('jobs.score.embed_unavailable', { score: score.score });
  else summary.textContent = t('jobs.score.why', { score: score.score });
  wrap.appendChild(summary);
  if (score.explanation && score.explanation.length) {
    const list = document.createElement('ul');
    for (const line of score.explanation) {
      const li = document.createElement('li');
      li.textContent = line;
      list.appendChild(li);
    }
    wrap.appendChild(list);
  }
  if (score.embedding_unavailable) {
    const note = document.createElement('p');
    note.className = 'job-card__why-note';
    note.textContent = t('jobs.score.embed_unavailable_note');
    wrap.appendChild(note);
  }
  return wrap;
}

function _voteButtons(job, refreshCard) {
  const wrap = document.createElement('div');
  wrap.className = 'job-vote';

  const makeBtn = (sig, label, cls) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `job-vote__btn ${cls}`;
    b.textContent = label;
    b.addEventListener('click', async () => {
      if (sig === 'no') {
        const reason = await _pickReason();
        if (!reason) return;
        await _castVote(job.thread_id, 'no', reason, refreshCard);
      } else {
        await _castVote(job.thread_id, sig, null, refreshCard);
      }
    });
    return b;
  };

  wrap.appendChild(makeBtn('yes', t('jobs.vote.yes'), 'job-vote__btn--yes'));
  wrap.appendChild(makeBtn('no', t('jobs.vote.no'), 'job-vote__btn--no'));
  wrap.appendChild(makeBtn('maybe', t('jobs.vote.maybe'), 'job-vote__btn--maybe'));
  wrap.appendChild(makeBtn('applied', t('jobs.vote.applied'), 'job-vote__btn--applied'));

  const undo = document.createElement('button');
  undo.type = 'button';
  undo.className = 'job-vote__btn job-vote__btn--undo';
  undo.textContent = t('jobs.vote.undo');
  undo.addEventListener('click', async () => {
    await _castVote(job.thread_id, 'undo', null, refreshCard);
  });
  wrap.appendChild(undo);
  return wrap;
}

function _pickReason() {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'job-reason-overlay';
    const modal = document.createElement('div');
    modal.className = 'job-reason';
    const h = document.createElement('h4');
    h.textContent = t('jobs.vote.no_reason');
    modal.appendChild(h);
    const close = (val) => { overlay.remove(); resolve(val); };
    for (const reason of REASONS) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'job-reason__opt';
      b.textContent = t(`jobs.reasons.${reason}`);
      b.addEventListener('click', () => close(reason));
      modal.appendChild(b);
    }
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'job-reason__cancel';
    cancel.textContent = t('jobs.vote.cancel');
    cancel.addEventListener('click', () => close(null));
    modal.appendChild(cancel);
    overlay.appendChild(modal);
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) close(null);
    });
    document.body.appendChild(overlay);
  });
}

async function _castVote(threadId, signal, reason, refreshCard) {
  try {
    await api.jobs.vote(threadId, { signal, reason_tag: reason, comment: null });
    voteState.set(threadId, signal === 'undo' ? null : signal);
    await refreshCard();
  } catch (e) {
    if (window.toast) window.toast(e.message || String(e), true);
  }
}

async function _loadJob(threadId) {
  try {
    return await api.jobs.get(threadId);
  } catch (_) {
    return null;
  }
}

async function _renderCard(container, threadId) {
  let data;
  try {
    data = await _loadJob(threadId);
  } catch (_) {
    return;
  }
  if (!data || !data.job) return;

  const card = document.createElement('div');
  card.className = 'job-card';

  card.appendChild(_header(data.job));
  card.appendChild(_meta(data.job));
  card.appendChild(_tags(data.job));
  if (data.job.brief) {
    const brief = document.createElement('p');
    brief.className = 'job-card__brief';
    brief.textContent = data.job.brief;
    card.appendChild(brief);
  }
  if (data.job.url) {
    const link = document.createElement('a');
    link.className = 'job-card__link';
    link.href = data.job.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = t('jobs.link.apply');
    card.appendChild(link);
  }
  const why = _why(data.score);
  if (why) card.appendChild(why);
  card.appendChild(_voteButtons(data.job, () => _renderCard(container, threadId)));

  container.replaceChildren(card);
}

export async function mountJobCard(container) {
  const threadId = container.dataset.threadId || window.__activeThreadId;
  if (!threadId) return;
  await _renderCard(container, threadId);
}

export function unmountJobCard() {
  // Cleanup is handled by the container being replaced.
}
