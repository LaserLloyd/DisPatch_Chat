// Job detail panel.
//
// 2026-09-16 rewrite: single render function, server-truth vote state
// (`job.last_vote`), a Feedback-for-Scout section, an Activity log, and a
// real modal contract (focus management, scroll lock, responsive sheet on
// narrow screens, Escape/backdrop close that cannot leak its listener).
//
// Clicking a job in the list opens THIS panel — a modal-style overlay with
// the full job metadata + voting controls + the score explanation + a way
// to send free-text feedback to the job-hunting agent (Scout). One card per
// job; closing returns focus to the row that opened it. The panel is
// mounted by main.js's __openJobDetail hook (set on window at init time).
//
// No thread reference here — the in-chat "header card above messages" path
// was retired in the 2026-09-15 redesign (see main.js renderMessages()).
// The board itself is the single entry point; this module is the modal.

import { t, fullTimestamp } from './i18n.js?v=3';
import { api } from './api.js?v=27';
import { validUrl } from './links.js?v=5';
import { acquireInert, el, iconLabel, railIcon, releaseInert, RAIL_ICONS } from './util.js?v=18';

const REASONS = [
  'wrong_location', 'too_senior', 'too_junior', 'compensation',
  'company', 'wrong_domain', 'already_applied', 'duplicate', 'other',
];

const SENIORITY_KNOWN = new Set(['junior', 'senior', 'staff', 'principal']);

// Event types the backend's job_events table can carry (database.py's
// column comment + the extra 'vote_undo' jobs.py derives at write time).
// Kept as a Set so an unrecognised future type degrades to the raw string
// instead of throwing.
const EVENT_TYPES = new Set([
  'comment', 'vote', 'vote_undo', 'state_change', 'tag_added',
  'tag_removed', 'repost', 'duplicate_detected', 'expired',
  'feedback', 'applied',
]);

function _salaryRange(j) {
  const a = j.salary_min, b = j.salary_max, c = j.salary_currency || 'USD';
  if (a && b) return `${a.toLocaleString()}–${b.toLocaleString()} ${c}`;
  if (a) return `${a.toLocaleString()} ${c}`;
  if (b) return `${b.toLocaleString()} ${c}`;
  return '';
}

function _toast(msg, isError) {
  if (typeof window.toast === 'function') window.toast(msg, isError);
}

// Tell the board list (if mounted) that a job changed — see jobs.js's listener.
//
// `job` is the fresh row when we have one, and passing it matters: with a job
// the board patches that single row and makes no request, without one it
// re-fetches the whole list. The vote and applied endpoints answer `{ok: true}`
// rather than the job, but the server ALSO broadcasts a `job_updated` frame
// carrying the full serialised row, and main.js forwards that here — so the
// common case is already covered by the socket and this call is the fallback
// for a tab whose socket is down.
//
// The reload below us has the same row in hand, so it hands it over rather
// than making the board go and ask for it again. Before that, one vote cost a
// POST plus up to five GETs and two complete board rebuilds.
function _notifyBoardChanged(job) {
  try {
    document.dispatchEvent(new CustomEvent('dispatch:jobs-changed', {
      detail: { job: job || null },
    }));
  } catch { /* ignore */ }
}

// ------------------------------------------------------------------ header

function _header(job) {
  const head = el('div', { class: 'job-card__head' });
  head.append(
    el('span', {
      class: `job-chip job-chip-state job-chip-state--${job.effective_state || job.state || 'pending'}`,
      text: t(`jobs.state.${job.effective_state || job.state || 'pending'}`),
    }),
    el('h3', { class: 'job-card__title', text: job.title || t('jobs.untitled') }),
  );
  if (job.is_expired) {
    head.append(el('span', { class: 'job-chip job-chip--expired', text: t('jobs.badge.expired') }));
  }
  if (job.duplicate_of) {
    head.append(el('span', { class: 'job-chip job-chip--dup', text: t('jobs.badge.duplicate') }));
  }
  return head;
}

function _meta(job) {
  const parts = [];
  if (job.company) parts.push(job.company);
  if (job.location) parts.push(job.location);
  if (job.remote_type && job.remote_type !== 'unknown') parts.push(t(`jobs.remote.${job.remote_type}`));
  if (job.seniority && SENIORITY_KNOWN.has(job.seniority)) parts.push(t(`jobs.seniority.${job.seniority}`));
  const salary = _salaryRange(job);
  if (salary) parts.push(salary);
  return el('div', { class: 'job-card__meta', text: parts.join(' · ') });
}

function _tags(job) {
  const wrap = el('div', { class: 'job-card__tags' });
  for (const tag of (job.tags || [])) wrap.append(el('span', { class: 'job-chip', text: tag }));
  return wrap;
}

// Posted / first-seen / source-agent line. Absent fields are simply
// skipped — a job posted straight into the current month may have no
// distinct "first seen" yet.
function _dates(job) {
  const bits = [];
  if (job.posted_at) bits.push(`${t('jobs.detail.posted')}: ${fullTimestamp(job.posted_at)}`);
  if (job.first_seen && job.first_seen !== job.posted_at) {
    bits.push(`${t('jobs.detail.first_seen')}: ${fullTimestamp(job.first_seen)}`);
  }
  if (job.source_agent) bits.push(`${t('jobs.detail.source')}: ${job.source_agent}`);
  if (!bits.length) return null;
  return el('div', { class: 'job-card__dates' }, bits.map((b) => el('span', { text: b })));
}

function _brief(job) {
  if (!job.brief) return null;
  // white-space: pre-wrap (CSS) preserves the line breaks the source posting
  // had; textContent keeps it safe regardless.
  return el('p', { class: 'job-card__brief', text: job.brief });
}

// Safe "Open posting" link. `validUrl` (links.js) only accepts http(s) — a
// stored scheme like javascript:/data: (however it got there — a bad
// scrape, a hand-edited row) is refused and the link is simply omitted
// rather than ever reaching an <a href>.
function _link(job) {
  if (!job.url || !validUrl(job.url)) return null;
  return el('a', {
    class: 'job-card__link', href: job.url, target: '_blank', rel: 'noopener noreferrer',
    text: t('jobs.link.apply'),
  });
}

function _why(score) {
  if (!score) return null;
  const wrap = el('details', { class: 'job-card__why' });
  const summary = el('summary', {
    class: 'job-card__why-summary',
    text: score.embedding_unavailable
      ? t('jobs.score.embed_unavailable', { score: score.score })
      : t('jobs.score.why', { score: score.score }),
  });
  wrap.append(summary);
  if (score.explanation && score.explanation.length) {
    wrap.append(el('ul', {}, score.explanation.map((line) => el('li', { text: line }))));
  }
  if (score.embedding_unavailable) {
    wrap.append(el('p', { class: 'job-card__why-note', text: t('jobs.score.embed_unavailable_note') }));
  }
  return wrap;
}

// -------------------------------------------------------------- reason picker

// Rendered into `document.body` (not the card) so its own overlay sits
// above the job modal regardless of where in the card the vote button
// lives. z-index is authored in app.css to sit above .job-modal-overlay
// but below the toast layer.
function _pickReason() {
  return new Promise((resolve) => {
    const close = (val) => {
      overlay.remove();
      document.removeEventListener('keydown', onKey, true);
      resolve(val);
    };
    // Capture phase + stopImmediatePropagation: this picker sits ON TOP of the
    // job modal, and both bind a document-level Escape handler. Without this,
    // one Escape dismissed the picker AND the card underneath it.
    const onKey = (e) => {
      if (e.key !== 'Escape') return;
      e.stopImmediatePropagation();
      e.preventDefault();
      close(null);
    };
    const modal = el('div', { class: 'job-reason' }, [
      el('h4', { text: t('jobs.vote.no_reason') }),
      ...REASONS.map((reason) => el('button', {
        type: 'button', class: 'job-reason__opt', text: t(`jobs.reasons.${reason}`),
        onclick: () => close(reason),
      })),
      el('button', {
        type: 'button', class: 'job-reason__cancel', text: t('jobs.vote.cancel'),
        onclick: () => close(null),
      }),
    ]);
    const overlay = el('div', { class: 'job-reason-overlay' }, [modal]);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(overlay);
    const first = modal.querySelector('.job-reason__opt');
    if (first) first.focus();
  });
}

/** Why a job could not be loaded, in the reader's terms.
 *
 *  Every failure used to read "could not be found" — a 500, a 403 after a drop
 *  to Safe Mode and an offline phone all told the operator the job was gone,
 *  which is the one thing that had not happened. `status` is undefined when the
 *  request never reached the server at all.
 */
function _loadFailureMessage(payload) {
  const err = payload && payload.__error;
  const status = err && err.status;
  if (status == null) return t('jobs.detail.offline');
  if (status === 404) return t('jobs.detail.not_found');
  if (status === 401 || status === 403) return t('jobs.detail.locked');
  return t('jobs.error', { msg: (err && err.message) || String(status) });
}

// -------------------------------------------------------------- data + state

async function _loadJob(jobId) {
  try {
    return await api.jobs.get(jobId);
  } catch (e) {
    return { __error: e };
  }
}

// -------------------------------------------------------------- vote controls

function _voteButtons(jobId, job, ctx) {
  const wrap = el('div', { class: 'job-vote' });
  const buttons = [];

  const setBusy = (busy) => {
    for (const b of buttons) b.disabled = busy;
  };

  const runVote = async (signal, reasonTag) => {
    if (ctx.busy) return;
    ctx.busy = true;
    setBusy(true);
    try {
      const comment = ctx.noteInput ? ctx.noteInput.value.trim() : '';
      if (signal === 'applied') {
        await api.jobs.applied(jobId, { comment: comment || null });
      } else {
        await api.jobs.vote(jobId, { signal, reason_tag: reasonTag || null, comment: comment || null });
      }
      const fresh = await ctx.reload();
      _notifyBoardChanged(fresh && fresh.job ? fresh.job : null);
    } catch (e) {
      _toast(e.message || String(e), true);
    } finally {
      ctx.busy = false;
      setBusy(false);
    }
  };

  const makeBtn = (sig, label, cls, icon) => {
    const b = el('button', {
      type: 'button', class: `job-vote__btn ${cls}`,
      dataset: { signal: sig },
      ...(icon ? {} : { text: label }),
      onclick: async () => {
        if (sig === 'no') {
          const reason = await _pickReason();
          if (!reason) return;
          await runVote('no', reason);
        } else {
          await runVote(sig, null);
        }
      },
    });
    if (icon) b.append(...iconLabel(icon, label));
    buttons.push(b);
    return b;
  };

  wrap.append(
    makeBtn('yes', t('jobs.vote.yes'), 'job-vote__btn--yes', RAIL_ICONS.thumbsup),
    makeBtn('no', t('jobs.vote.no'), 'job-vote__btn--no', RAIL_ICONS.thumbsdown),
    makeBtn('maybe', t('jobs.vote.maybe'), 'job-vote__btn--maybe', RAIL_ICONS.help),
    makeBtn('applied', t('jobs.vote.applied'), 'job-vote__btn--applied'),
  );
  const undo = makeBtn('undo', t('jobs.vote.undo'), 'job-vote__btn--undo');
  undo.addEventListener('click', () => runVote('undo', null));
  wrap.append(undo);

  // Server truth, not a client-side guess: job.last_vote is authoritative.
  const last = _lastVoteSignal(job);
  if (last) {
    const sel = wrap.querySelector(`[data-signal="${last}"]`);
    if (sel) sel.classList.add('job-vote__btn--active');
  }

  return wrap;
}

// Normalises `job.last_vote` to a bare signal ('yes'|'no'|'maybe'|'applied'
// | null). Handles both shapes seen on this box: the flat string the
// original contract specified, and the richer object the live backend
// actually returns as of 2026-09-16
// (`{signal:'vote_yes'|'vote_no'|'vote_maybe'|'applied', reason_tag,
// comment, actor, created_at}` — see jobs.py's `_last_vote_from_feedback`).
// Tolerating both means a future backend change either way keeps working
// without another round of "the highlight silently stopped working".
const VOTE_SIGNAL_MAP = { vote_yes: 'yes', vote_no: 'no', vote_maybe: 'maybe', yes: 'yes', no: 'no', maybe: 'maybe', applied: 'applied' };
function _lastVoteSignal(job) {
  const lv = job.last_vote;
  if (!lv) return null;
  const raw = typeof lv === 'string' ? lv : lv.signal;
  return (raw && VOTE_SIGNAL_MAP[raw]) || null;
}

function _noteField(ctx) {
  const wrap = el('div', { class: 'job-note' });
  const input = el('textarea', {
    class: 'job-note__input', rows: '2',
    placeholder: t('jobs.note.placeholder'),
    'aria-label': t('jobs.note.placeholder'),
  });
  ctx.noteInput = input;
  wrap.append(input);
  return wrap;
}

// -------------------------------------------------------------- feedback

function _feedbackSection(jobId, ctx) {
  const wrap = el('section', { class: 'job-feedback' });
  wrap.append(el('h4', { class: 'job-feedback__title', text: t('jobs.feedback.title') }));

  if (ctx.feedbackResult) {
    const r = ctx.feedbackResult;
    const confirm = el('div', { class: 'job-feedback__confirm' }, [
      el('p', { text: r.dispatched ? t('jobs.feedback.sent') : t('jobs.feedback.sent_no_agent') }),
    ]);
    if (r.thread_id) {
      confirm.append(el('button', {
        type: 'button', class: 'btn-secondary job-feedback__open-thread',
        text: t('jobs.feedback.open_thread'),
        onclick: () => {
          ctx.close();
          if (typeof window.__openThread === 'function') {
            window.__openThread(r.thread_id, { botId: 'jobboard' });
          }
        },
      }));
    }
    wrap.append(confirm);
    return wrap;
  }

  const textarea = el('textarea', {
    class: 'job-feedback__input', rows: '3',
    placeholder: t('jobs.feedback.placeholder'),
    'aria-label': t('jobs.feedback.title'),
    maxlength: '2000',
  });
  const reasonSelect = el('select', { class: 'job-feedback__reason', 'aria-label': t('jobs.feedback.reason_label') });
  reasonSelect.append(el('option', { value: '', text: t('jobs.feedback.reason_none') }));
  for (const reason of REASONS) {
    reasonSelect.append(el('option', { value: reason, text: t(`jobs.reasons.${reason}`) }));
  }
  const err = el('p', { class: 'job-feedback__error', role: 'alert', hidden: '' });
  const sendBtn = el('button', {
    type: 'button', class: 'btn-primary job-feedback__send', text: t('jobs.feedback.send'),
  });
  sendBtn.addEventListener('click', async () => {
    const comment = textarea.value.trim();
    err.hidden = true; err.textContent = '';
    if (!comment) {
      err.textContent = t('jobs.feedback.need_comment');
      err.hidden = false;
      textarea.focus();
      return;
    }
    sendBtn.disabled = true;
    try {
      const result = await api.jobs.feedback(jobId, {
        comment, reason_tag: reasonSelect.value || null,
      });
      ctx.feedbackResult = result || { dispatched: false };
      const fresh = await ctx.reload();
      _notifyBoardChanged(fresh && fresh.job ? fresh.job : null);
    } catch (e) {
      if (e.status === 404) {
        err.textContent = t('jobs.feedback.not_available');
      } else {
        err.textContent = t('jobs.feedback.error', { msg: e.message || String(e) });
      }
      err.hidden = false;
      sendBtn.disabled = false;
    }
  });

  wrap.append(
    el('div', { class: 'job-feedback__row' }, [textarea]),
    el('div', { class: 'job-feedback__row job-feedback__row--controls' }, [reasonSelect, sendBtn]),
    err,
  );
  return wrap;
}

// -------------------------------------------------------------- activity log

function _actorLabel(actor) {
  // The documented shape is 'user:<id>' | 'agent:<id>' | 'system'
  // (database.py), but the live writer only ever stamps the bare 'user' /
  // 'agent:<id>' / 'system' (no id on 'user') — handle both.
  if (!actor || actor === 'system') return t('jobs.activity.actor.system');
  if (actor === 'user' || actor.startsWith('user:')) return t('jobs.activity.actor.you');
  if (actor.startsWith('agent:')) return actor.slice('agent:'.length) || t('jobs.activity.actor.system');
  return actor;
}

function _eventLine(ev) {
  const type = EVENT_TYPES.has(ev.type) ? ev.type : null;
  const li = el('li', { class: 'job-activity__item' });
  li.append(el('span', { class: 'job-activity__time', text: fullTimestamp(ev.created_at) }));
  li.append(el('span', { class: 'job-activity__actor', text: _actorLabel(ev.actor) }));
  li.append(el('span', {
    class: 'job-activity__type',
    text: type ? t(`jobs.activity.type.${type}`) : ev.type,
  }));
  if (ev.from_state && ev.to_state) {
    li.append(el('span', {
      class: 'job-activity__states',
      text: `${t(`jobs.state.${ev.from_state}`)} → ${t(`jobs.state.${ev.to_state}`)}`,
    }));
  }
  if (ev.reason_tag) {
    li.append(el('span', { class: 'job-activity__reason', text: t(`jobs.reasons.${ev.reason_tag}`) }));
  }
  if (ev.comment) {
    li.append(el('p', { class: 'job-activity__comment', text: ev.comment }));
  }
  return li;
}

function _activity(events) {
  const wrap = el('section', { class: 'job-activity' });
  wrap.append(el('h4', { class: 'job-activity__title', text: t('jobs.activity.title') }));
  const list = (events || [])
    .slice()
    .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  if (!list.length) {
    wrap.append(el('p', { class: 'job-activity__empty', text: t('jobs.activity.empty') }));
    return wrap;
  }
  wrap.append(el('ul', { class: 'job-activity__list' }, list.map(_eventLine)));
  return wrap;
}

// -------------------------------------------------------------- main render

// Single render function — no duplicated markup between the initial open
// and a post-vote/post-feedback refresh. `ctx` carries mutable per-modal
// state (in-flight flag, the feedback confirmation once sent, the close
// callback) across renders.
function _renderCard(card, jobId, data, ctx) {
  card.replaceChildren();
  ctx.busy = false;
  ctx.noteInput = null;

  const close = el('button', {
    type: 'button', class: 'job-modal__close', text: '×',
    'aria-label': t('jobs.detail.close'),
    onclick: ctx.close,
  });
  card.append(close);
  card.append(_header(data.job));
  card.append(_meta(data.job));
  card.append(_tags(data.job));
  const dates = _dates(data.job);
  if (dates) card.append(dates);
  const brief = _brief(data.job);
  if (brief) card.append(brief);
  const link = _link(data.job);
  if (link) card.append(link);
  const why = _why(data.score);
  if (why) card.append(why);

  card.append(_voteButtons(jobId, data.job, ctx));
  card.append(_noteField(ctx));
  card.append(el('hr', { class: 'job-modal__divider' }));
  card.append(_feedbackSection(jobId, ctx));
  card.append(el('hr', { class: 'job-modal__divider' }));
  card.append(_activity(data.events));

  return close;
}

// -------------------------------------------------------------- public surface

let _activeOverlay = null;
let _returnFocusTo = null;
let _onKey = null;

/** Make everything behind the dialog unreachable, or reachable again.
 *
 *  Ref-counted through util.js rather than written here. The card declares
 *  role="dialog" aria-modal="true", moves focus in and restores it on close —
 *  but nothing stopped Tab walking out of it, which is what aria-modal
 *  PROMISES and cannot enforce. `inert` is how main.js's modal guard does it,
 *  and going through the shared counter is what keeps the two from undoing
 *  each other: the drop backdrop toggling during a drag used to make that
 *  guard decide no modal was open and strip this overlay's trap.
 */
let _inertToken = null;
function _setBackgroundInert(on) {
  if (on) { if (!_inertToken) _inertToken = acquireInert(); return; }
  if (_inertToken) { releaseInert(_inertToken); _inertToken = null; }
}

function _close() {
  if (_onKey) { document.removeEventListener('keydown', _onKey); _onKey = null; }
  if (_activeOverlay) {
    _activeOverlay.remove();
    _activeOverlay = null;
    document.body.classList.remove('job-modal-open');
  }
  // Always, even if there was no overlay: a stuck `inert` on #app is an app
  // nobody can click, which is far worse than an extra attribute removal.
  _setBackgroundInert(false);
  if (_returnFocusTo && typeof _returnFocusTo.focus === 'function') {
    try { _returnFocusTo.focus(); } catch { /* element may be gone */ }
  }
  _returnFocusTo = null;
}

/**
 * Open the job detail overlay for `jobId`.
 * Idempotent: a second call while the overlay is up replaces it.
 */
export async function openJobDetail(jobId) {
  if (!jobId) return;
  const invoker = document.activeElement;
  _close();
  const data = await _loadJob(jobId);
  if (!data || data.__error || !data.job) {
    _toast(_loadFailureMessage(data), true);
    return;
  }

  const overlay = el('div', {
    class: 'job-modal-overlay', role: 'dialog', 'aria-modal': 'true',
    'aria-label': data.job.title || t('jobs.untitled'),
  });
  const card = el('div', { class: 'job-modal', tabindex: '-1' });
  overlay.append(card);

  const ctx = {
    busy: false,
    noteInput: null,
    feedbackResult: null,
    close: () => _close(),
    // Returns the payload it fetched, so callers can hand the fresh row to
    // the board instead of making it go and ask for the same thing again.
    reload: async () => {
      const fresh = await _loadJob(jobId);
      if (!fresh || fresh.__error || !fresh.job) {
        _toast(_loadFailureMessage(fresh), true);
        _close();
        return null;
      }
      _renderCard(card, jobId, fresh, ctx);
      return fresh;
    },
  };

  const closeBtn = _renderCard(card, jobId, data, ctx);

  overlay.addEventListener('click', (e) => { if (e.target === overlay) _close(); });
  _onKey = (e) => { if (e.key === 'Escape') _close(); };
  document.addEventListener('keydown', _onKey);

  document.body.appendChild(overlay);
  document.body.classList.add('job-modal-open');
  _setBackgroundInert(true);
  _activeOverlay = overlay;
  _returnFocusTo = invoker;
  // Focus moves into the panel — the close button is always present and is
  // a sane, discoverable first stop (screen readers announce the dialog
  // role + label first anyway).
  closeBtn.focus();
}

export function closeJobDetail() { _close(); }
