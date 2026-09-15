// Job detail panel.
//
// 2026-09-15 redesign: the board is no longer a chat. Clicking a job
// in the list opens THIS panel — a modal-style overlay with the full
// job metadata + voting controls + the score explanation. One card
// per job; closing returns the operator to the board list. The panel
// is mounted by main.js's __openJobDetail hook (set on window at init
// time), and unmounts itself when the user dismisses.
//
// No thread reference here — the chat-style "header card above
// messages" path (mountJobCard) is preserved as a no-op stub for
// backwards compatibility with any caller that still wires it. The
// board itself uses the modal path exclusively.

import { t } from './i18n.js?v=3';
import { api } from './api.js?v=22';

const REASONS = [
  'wrong_location', 'too_senior', 'too_junior', 'compensation',
  'company', 'wrong_domain', 'already_applied', 'duplicate', 'other',
];

// Job-id → last-known vote signal. Lets the vote buttons render in their
// selected state across modal open/close cycles without an extra fetch.
const voteState = new Map();

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
  if (score.embedding_unavailable) {
    summary.textContent = t('jobs.score.embed_unavailable', { score: score.score });
  } else {
    summary.textContent = t('jobs.score.why', { score: score.score });
  }
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
    b.dataset.signal = sig;
    b.addEventListener('click', async () => {
      if (sig === 'no') {
        const reason = await _pickReason();
        if (!reason) return;
        await _castVote(job.job_id, 'no', reason, refreshCard);
      } else {
        await _castVote(job.job_id, sig, null, refreshCard);
      }
    });
    return b;
  };

  const yes = makeBtn('yes', t('jobs.vote.yes'), 'job-vote__btn--yes');
  const no = makeBtn('no', t('jobs.vote.no'), 'job-vote__btn--no');
  const maybe = makeBtn('maybe', t('jobs.vote.maybe'), 'job-vote__btn--maybe');
  const applied = makeBtn('applied', t('jobs.vote.applied'), 'job-vote__btn--applied');
  wrap.appendChild(yes);
  wrap.appendChild(no);
  wrap.appendChild(maybe);
  wrap.appendChild(applied);

  const undo = document.createElement('button');
  undo.type = 'button';
  undo.className = 'job-vote__btn job-vote__btn--undo';
  undo.textContent = t('jobs.vote.undo');
  undo.addEventListener('click', async () => {
    await _castVote(job.job_id, 'undo', null, refreshCard);
  });
  wrap.appendChild(undo);

  // Reflect the last-known vote state on the right button.
  const last = voteState.get(job.job_id);
  if (last && last !== 'undo') {
    const sel = wrap.querySelector(`[data-signal="${last}"]`);
    if (sel) sel.classList.add('job-vote__btn--active');
  }

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

async function _castVote(jobId, signal, reason, refreshCard) {
  try {
    await api.jobs.vote(jobId, { signal, reason_tag: reason, comment: null });
    voteState.set(jobId, signal === 'undo' ? null : signal);
    await refreshCard();
  } catch (e) {
    if (window.toast) window.toast(e.message || String(e), true);
  }
}

async function _loadJob(jobId) {
  try {
    return await api.jobs.get(jobId);
  } catch (_) {
    return null;
  }
}

// -------------------- public surface ---------------------------------- //

let _activeOverlay = null;

function _close() {
  if (_activeOverlay) {
    _activeOverlay.remove();
    _activeOverlay = null;
    document.body.classList.remove('job-modal-open');
  }
}

/**
 * Open the job detail overlay for `jobId`.
 * Idempotent: a second call while the overlay is up replaces it.
 */
export async function openJobDetail(jobId) {
  if (!jobId) return;
  _close();
  const data = await _loadJob(jobId);
  if (!data || !data.job) {
    if (window.toast) window.toast(t('jobs.detail.not_found'), true);
    return;
  }
  const overlay = document.createElement('div');
  overlay.className = 'job-modal-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', data.job.title || t('jobs.untitled'));
  const card = document.createElement('div');
  card.className = 'job-modal';

  // Close button
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'job-modal__close';
  close.textContent = '×';
  close.setAttribute('aria-label', t('jobs.detail.close'));
  close.addEventListener('click', _close);
  card.appendChild(close);

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

  card.appendChild(_voteButtons(data.job, async () => {
    // Reload the card body in place; the modal stays open.
    const fresh = await _loadJob(jobId);
    if (!fresh || !fresh.job) return;
    // Replace children except the close button (first child).
    const closeBtn = card.firstChild;
    card.replaceChildren();
    card.appendChild(closeBtn);
    card.appendChild(_header(fresh.job));
    card.appendChild(_meta(fresh.job));
    card.appendChild(_tags(fresh.job));
    if (fresh.job.brief) {
      const brief = document.createElement('p');
      brief.className = 'job-card__brief';
      brief.textContent = fresh.job.brief;
      card.appendChild(brief);
    }
    if (fresh.job.url) {
      const link = document.createElement('a');
      link.className = 'job-card__link';
      link.href = fresh.job.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = t('jobs.link.apply');
      card.appendChild(link);
    }
    const why2 = _why(fresh.score);
    if (why2) card.appendChild(why2);
    card.appendChild(_voteButtons(fresh.job, () => Promise.resolve()));
  }));

  overlay.appendChild(card);
  // Dismiss on backdrop click
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) _close();
  });
  // Dismiss on Escape
  const onKey = (e) => {
    if (e.key === 'Escape') {
      _close();
      document.removeEventListener('keydown', onKey);
    }
  };
  document.addEventListener('keydown', onKey);

  document.body.appendChild(overlay);
  document.body.classList.add('job-modal-open');
  _activeOverlay = overlay;
}

export function closeJobDetail() { _close(); }
