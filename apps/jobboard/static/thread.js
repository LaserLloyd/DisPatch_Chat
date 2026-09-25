// Job Board — the thread hook (docs/design/2026-09-25-apps.md).
//
// The shell imports this module when a thread belonging to the `jobboard` bot
// (one per month) is on screen and calls mount() with a host element between
// the chat header and the messages. It paints the job panel there: a picker
// of the jobs filed under this thread and the selected job's full detail —
// state, metadata, the posting link, the score explanation, vote / applied /
// undo, tags, archive, an optional note, Feedback for Scout and the activity
// log. It replaces the modal the old js/job-thread.js opened from the board:
// the detail now sits with the conversation about the job.
//
// It imports nothing from the shell but /static/js/app-sdk.js, and everything
// app-shaped (api, t, openThread, toast) arrives in mount()'s argument, bound
// by the shell to this app. mount() returns {unmount, onFrame}; the shell
// calls unmount() when the thread is left and onFrame() for every live
// `app:jobboard:*` WebSocket frame while the panel is up.
//
// Carried over from job-thread.js, with the reasons:
//   * "Applied" posts to /applied — /vote 422s signal:'applied';
//   * the vote highlight is server truth (job.last_vote, either shape);
//   * a posting URL that is not http(s) is never put in an href;
//   * a failed load says WHY (offline / locked / gone), never just "not found";
//   * a slow reload that lost a race never paints.

import { el, iconLabel, railIcon, RAIL_ICONS } from '/static/js/app-sdk.js?v=1';

export const API_BASE = '/api/apps/jobboard';
// Bump with board.css: the panel links the board's stylesheet into the shell.
const CSS_HREF = '/apps/jobboard/board.css?v=1';

const REASONS = [
  'wrong_location', 'too_senior', 'too_junior', 'compensation',
  'company', 'wrong_domain', 'already_applied', 'duplicate', 'other',
];
// Literal keys throughout (never t(`x.${y}`)) so tests can prove every key
// the panel can ask for exists — see apps/jobboard/tests/locales.test.js.
const REASON_KEY = {
  wrong_location: 'reasons.wrong_location', too_senior: 'reasons.too_senior',
  too_junior: 'reasons.too_junior', compensation: 'reasons.compensation',
  company: 'reasons.company', wrong_domain: 'reasons.wrong_domain',
  already_applied: 'reasons.already_applied', duplicate: 'reasons.duplicate', other: 'reasons.other',
};
const STATE_KEY = {
  pending: 'state.pending', yes: 'state.yes', no: 'state.no', maybe: 'state.maybe',
  applied: 'state.applied', archived: 'state.archived', duplicate: 'state.duplicate',
};
const REMOTE_KEY = { remote: 'remote.remote', hybrid: 'remote.hybrid', onsite: 'remote.onsite' };
const SENIORITY_KEY = {
  junior: 'seniority.junior', senior: 'seniority.senior', staff: 'seniority.staff', principal: 'seniority.principal',
};
const EVENT_KEY = {
  comment: 'activity.type.comment', vote: 'activity.type.vote', vote_undo: 'activity.type.vote_undo',
  state_change: 'activity.type.state_change', tag_added: 'activity.type.tag_added',
  tag_removed: 'activity.type.tag_removed', repost: 'activity.type.repost',
  duplicate_detected: 'activity.type.duplicate_detected', expired: 'activity.type.expired',
  feedback: 'activity.type.feedback', applied: 'activity.type.applied',
};
export const USED_KEYS = [
  ...Object.values(REASON_KEY), ...Object.values(STATE_KEY), ...Object.values(REMOTE_KEY),
  ...Object.values(SENIORITY_KEY), ...Object.values(EVENT_KEY),
];

// `job.last_vote` is either the flat signal or the live backend's object
// ({signal: 'vote_yes'|…|'applied', …}); both normalise to a bare signal.
const VOTE_SIGNAL_MAP = { vote_yes: 'yes', vote_no: 'no', vote_maybe: 'maybe', yes: 'yes', no: 'no', maybe: 'maybe', applied: 'applied' };
export function lastVoteSignal(job) {
  const lv = job && job.last_vote;
  if (!lv) return null;
  const raw = typeof lv === 'string' ? lv : lv.signal;
  return (raw && VOTE_SIGNAL_MAP[raw]) || null;
}

/** http(s) only. A stored javascript:/data: URL (a bad scrape, a hand-edited
 *  row) is refused, and the caller omits the link rather than neuter it. */
export function safeHttpUrl(u) {
  try {
    const url = new URL(String(u || ''));
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch { return null; }
}

function salaryRange(j) {
  const a = j.salary_min, b = j.salary_max, c = j.salary_currency || 'USD';
  if (a && b) return `${a.toLocaleString()}–${b.toLocaleString()} ${c}`;
  if (a) return `${a.toLocaleString()} ${c}`;
  if (b) return `${b.toLocaleString()} ${c}`;
  return '';
}

function ensureCss() {
  if (document.querySelector('link[data-app-css="jobboard"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = CSS_HREF;
  link.dataset.appCss = 'jobboard';
  document.head.append(link);
}

/** Mount the job panel. See the file header for the contract. */
export async function mount(ctx) {
  const { threadEl, thread, api, t } = ctx;
  const dateTime = typeof ctx.dateTime === 'function' ? ctx.dateTime : (iso) => String(iso || '');
  const toast = typeof ctx.toast === 'function' ? ctx.toast : () => {};
  const openThread = typeof ctx.openThread === 'function' ? ctx.openThread : () => {};
  if (!threadEl || !thread || !thread.id) return { unmount() {} };
  ensureCss();

  const st = {
    alive: true,
    jobs: [],                      // this thread's jobs
    selected: (ctx.hint && typeof ctx.hint.job_id === 'string') ? ctx.hint.job_id : null,
    open: !!(ctx.hint && ctx.hint.job_id),
    data: null,                    // {job, events, score} for the selected job
    loadSeq: 0,
    busy: false,
    picking: false,                // the inline "why no?" row is up
    feedbackResult: null,
    note: '',
  };

  const root = el('section', { class: 'jb-thread', 'aria-label': t('title') });
  const bar = el('div', { class: 'jb-thread__bar' });
  const label = el('span', { class: 'jb-thread__label' });
  const pick = el('select', { class: 'jb-thread__pick', 'aria-label': t('thread.pick') });
  const toggle = el('button', { type: 'button', class: 'jb-thread__toggle', 'aria-expanded': 'false' });
  const status = el('p', { class: 'jb-thread__status', role: 'status', hidden: '' });
  const detail = el('div', { class: 'jb-thread__detail' });
  bar.append(railIcon(RAIL_ICONS.jobs), label, pick, toggle);
  root.append(bar, status, detail);
  threadEl.replaceChildren(root);

  const setStatus = (text, isError = false) => {
    status.textContent = text || '';
    status.hidden = !text;
    status.classList.toggle('jb-thread__status--error', !!isError);
  };

  const failureText = (err) => {
    const s = err && err.status;
    if (err && err.decoy) return t('detail.locked');
    if (s == null) return t('detail.offline');
    if (s === 404) return t('detail.not_found');
    if (s === 401 || s === 403) return t('detail.locked');
    return t('error', { msg: (err && err.message) || String(s) });
  };

  function paintBar() {
    const n = st.jobs.length;
    label.textContent = n === 1 ? t('thread.count', { n }) : t('thread.count_plural', { n });
    pick.replaceChildren(el('option', { value: '', text: t('thread.pick') }));
    for (const j of st.jobs) {
      pick.append(el('option', {
        value: j.job_id,
        text: `${j.title || t('untitled')}${j.company ? ` — ${j.company}` : ''}`,
      }));
    }
    pick.value = st.selected || '';
    pick.hidden = n === 0;
    toggle.hidden = !st.selected;
    toggle.textContent = st.open ? t('thread.hide') : t('thread.show');
    toggle.setAttribute('aria-expanded', String(st.open));
  }

  // ---------------------------------------------------------------- data

  async function loadJobs() {
    try {
      const r = await api(API_BASE, { query: { thread_id: thread.id, limit: 500 } });
      if (!st.alive) return;
      st.jobs = (r && Array.isArray(r.jobs)) ? r.jobs : [];
      st.jobs.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
      if (!st.jobs.length) setStatus(t('thread.empty'));
    } catch (e) {
      if (!st.alive) return;
      setStatus(failureText(e), true);
    }
    paintBar();
  }

  /** Fetch the selected job and paint it. Returns the payload, or null when
   *  it lost a race or failed (a failure is shown, never swallowed). */
  async function loadDetail() {
    const id = st.selected;
    const mine = ++st.loadSeq;
    if (!id) { st.data = null; paintDetail(); return null; }
    try {
      const data = await api(`${API_BASE}/${encodeURIComponent(id)}`);
      if (!st.alive || mine !== st.loadSeq || st.selected !== id) return null;
      if (!data || !data.job) throw Object.assign(new Error('empty'), { status: 404 });
      st.data = data;
      // Keep the picker row fresh too (title/state may have moved).
      const i = st.jobs.findIndex((j) => j.job_id === id);
      if (i >= 0) st.jobs[i] = data.job; else st.jobs.unshift(data.job);
      setStatus('');
      paintBar();
      paintDetail();
      return data;
    } catch (e) {
      if (!st.alive || mine !== st.loadSeq) return null;
      st.data = null;
      paintDetail();
      setStatus(failureText(e), true);
      return null;
    }
  }

  // ---------------------------------------------------------------- actions

  async function act(fn) {
    if (st.busy) return;
    st.busy = true;
    detail.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    try {
      await fn();
      await loadDetail();
    } catch (e) {
      toast(failureText(e), true);
      paintDetail();
    } finally {
      st.busy = false;
      detail.querySelectorAll('button').forEach((b) => { b.disabled = false; });
    }
  }

  const jobPath = (suffix) => `${API_BASE}/${encodeURIComponent(st.selected)}/${suffix}`;

  function vote(signal, reasonTag = null) {
    const comment = st.note.trim() || null;
    return act(async () => {
      if (signal === 'applied') {
        await api(jobPath('applied'), { method: 'POST', body: { comment } });
      } else {
        await api(jobPath('vote'), { method: 'POST', body: { signal, reason_tag: reasonTag, comment } });
      }
      st.note = '';
      st.picking = false;
    });
  }

  function editTags(add, remove) {
    return act(() => api(jobPath('tags'), { method: 'POST', body: { add, remove } }));
  }

  function archive() {
    return act(async () => {
      await api(jobPath('archive'), { method: 'POST' });
      toast(t('archive.done'));
    });
  }

  // ---------------------------------------------------------------- render

  function header(job) {
    const s = job.effective_state || job.state || 'pending';
    const head = el('div', { class: 'job-card__head' }, [
      el('span', { class: `job-chip job-chip-state job-chip-state--${s}`, text: t(STATE_KEY[s] || STATE_KEY.pending) }),
      el('h3', { class: 'job-card__title', text: job.title || t('untitled') }),
    ]);
    if (job.is_expired) head.append(el('span', { class: 'job-chip job-chip--expired', text: t('badge.expired') }));
    if (job.duplicate_of) head.append(el('span', { class: 'job-chip job-chip--dup', text: t('badge.duplicate') }));
    return head;
  }

  function meta(job) {
    const parts = [];
    if (job.company) parts.push(job.company);
    if (job.location) parts.push(job.location);
    if (REMOTE_KEY[job.remote_type]) parts.push(t(REMOTE_KEY[job.remote_type]));
    if (SENIORITY_KEY[job.seniority]) parts.push(t(SENIORITY_KEY[job.seniority]));
    const salary = salaryRange(job);
    if (salary) parts.push(salary);
    return el('div', { class: 'job-card__meta', text: parts.join(' · ') });
  }

  function tags(job) {
    const wrap = el('div', { class: 'job-card__tags' });
    for (const tag of (job.tags || [])) {
      wrap.append(el('span', { class: 'job-chip' }, [
        tag,
        el('button', {
          type: 'button', class: 'job-chip__remove', text: '×',
          'aria-label': t('tags.remove', { tag }), title: t('tags.remove', { tag }),
          onclick: () => editTags([], [tag]),
        }),
      ]));
    }
    const input = el('input', {
      type: 'text', class: 'job-card__tag-input', maxlength: '40',
      placeholder: t('tags.add_placeholder'), 'aria-label': t('tags.add_placeholder'),
    });
    input.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Enter') return;
      ev.preventDefault();
      const v = input.value.trim().toLowerCase();
      if (v) editTags([v], []);
    });
    wrap.append(input);
    return wrap;
  }

  function dates(job) {
    const bits = [];
    if (job.posted_at) bits.push(`${t('detail.posted')}: ${dateTime(job.posted_at)}`);
    if (job.first_seen && job.first_seen !== job.posted_at) bits.push(`${t('detail.first_seen')}: ${dateTime(job.first_seen)}`);
    if (job.source_agent) bits.push(`${t('detail.source')}: ${job.source_agent}`);
    return bits.length ? el('div', { class: 'job-card__dates' }, bits.map((b) => el('span', { text: b }))) : null;
  }

  function actions(job) {
    const row = el('div', { class: 'job-card__actions' });
    const href = safeHttpUrl(job.url);
    if (href) {
      row.append(el('a', {
        class: 'job-card__link', href, target: '_blank', rel: 'noopener noreferrer', text: t('link.apply'),
      }));
    }
    if ((job.effective_state || job.state) !== 'archived') {
      const b = el('button', { type: 'button', class: 'job-card__archive', onclick: () => archive() });
      b.append(...iconLabel(RAIL_ICONS.archive, t('archive.button')));
      row.append(b);
    }
    return row;
  }

  function why(score) {
    if (!score) return null;
    const wrap = el('details', { class: 'job-card__why' }, [
      el('summary', {
        class: 'job-card__why-summary',
        text: score.embedding_unavailable
          ? t('score.embed_unavailable', { score: score.score })
          : t('score.why', { score: score.score }),
      }),
    ]);
    if (Array.isArray(score.explanation) && score.explanation.length) {
      wrap.append(el('ul', {}, score.explanation.map((line) => el('li', { text: String(line) }))));
    }
    if (score.embedding_unavailable) wrap.append(el('p', { class: 'job-card__why-note', text: t('score.embed_unavailable_note') }));
    return wrap;
  }

  function voteRow(job) {
    const wrap = el('div', { class: 'job-vote' });
    const btn = (sig, key, cls, icon, onclick) => {
      const b = el('button', { type: 'button', class: `job-vote__btn ${cls}`, dataset: { signal: sig }, onclick });
      if (icon) b.append(...iconLabel(icon, t(key))); else b.textContent = t(key);
      return b;
    };
    wrap.append(
      btn('yes', 'vote.yes', 'job-vote__btn--yes', RAIL_ICONS.thumbsup, () => vote('yes')),
      btn('no', 'vote.no', 'job-vote__btn--no', RAIL_ICONS.thumbsdown, () => { st.picking = !st.picking; paintDetail(); }),
      btn('maybe', 'vote.maybe', 'job-vote__btn--maybe', RAIL_ICONS.help, () => vote('maybe')),
      btn('applied', 'vote.applied', 'job-vote__btn--applied', null, () => vote('applied')),
      btn('undo', 'vote.undo', 'job-vote__btn--undo', null, () => vote('undo')),
    );
    const last = lastVoteSignal(job);
    if (last) {
      for (const b of wrap.querySelectorAll('[data-signal]')) {
        if (b.dataset.signal === last) b.classList.add('job-vote__btn--active');
      }
    }
    return wrap;
  }

  // "Why no?" is a row in the panel now, not a second overlay on a modal.
  function reasonRow() {
    const cancel = () => { st.picking = false; paintDetail(); };
    const row = el('div', { class: 'job-reason', role: 'group', 'aria-label': t('vote.no_reason') }, [
      el('p', { class: 'job-reason__title', text: t('vote.no_reason') }),
      ...REASONS.map((r) => el('button', {
        type: 'button', class: 'job-reason__opt', dataset: { reason: r }, text: t(REASON_KEY[r]),
        onclick: () => vote('no', r),
      })),
      el('button', { type: 'button', class: 'job-reason__cancel', text: t('vote.cancel'), onclick: cancel }),
    ]);
    row.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); cancel(); } });
    return row;
  }

  function noteField() {
    const input = el('textarea', {
      class: 'job-note__input', rows: '2',
      placeholder: t('note.placeholder'), 'aria-label': t('note.placeholder'),
    });
    input.value = st.note;
    input.addEventListener('input', () => { st.note = input.value; });
    return el('div', { class: 'job-note' }, [input]);
  }

  function feedbackSection() {
    const wrap = el('section', { class: 'job-feedback' }, [
      el('h4', { class: 'job-feedback__title', text: t('feedback.title') }),
    ]);
    if (st.feedbackResult) {
      const r = st.feedbackResult;
      const confirm = el('div', { class: 'job-feedback__confirm' }, [
        el('p', { text: r.dispatched ? t('feedback.sent') : t('feedback.sent_no_agent') }),
      ]);
      // Scout replies in the job's thread. That is usually THIS thread; only
      // offer a hop when the server says it went somewhere else.
      if (r.thread_id && r.thread_id !== thread.id) {
        confirm.append(el('button', {
          type: 'button', class: 'btn-secondary job-feedback__open-thread', text: t('feedback.open_thread'),
          onclick: () => openThread(r.thread_id),
        }));
      }
      wrap.append(confirm);
      return wrap;
    }
    const textarea = el('textarea', {
      class: 'job-feedback__input', rows: '3', maxlength: '2000',
      placeholder: t('feedback.placeholder'), 'aria-label': t('feedback.title'),
    });
    const reason = el('select', { class: 'job-feedback__reason', 'aria-label': t('feedback.reason_label') }, [
      el('option', { value: '', text: t('feedback.reason_none') }),
      ...REASONS.map((r) => el('option', { value: r, text: t(REASON_KEY[r]) })),
    ]);
    const err = el('p', { class: 'job-feedback__error', role: 'alert', hidden: '' });
    const send = el('button', { type: 'button', class: 'btn-primary job-feedback__send', text: t('feedback.send') });
    send.addEventListener('click', async () => {
      const comment = textarea.value.trim();
      err.hidden = true; err.textContent = '';
      if (!comment) {
        err.textContent = t('feedback.need_comment');
        err.hidden = false;
        textarea.focus();
        return;
      }
      send.disabled = true;
      try {
        const r = await api(jobPath('feedback'), { method: 'POST', body: { comment, reason_tag: reason.value || null } });
        st.feedbackResult = r || { dispatched: false };
        await loadDetail();
      } catch (e) {
        err.textContent = e && e.status === 404
          ? t('feedback.not_available')
          : t('feedback.error', { msg: (e && e.message) || String(e) });
        err.hidden = false;
        send.disabled = false;
      }
    });
    wrap.append(
      el('div', { class: 'job-feedback__row' }, [textarea]),
      el('div', { class: 'job-feedback__row job-feedback__row--controls' }, [reason, send]),
      err,
    );
    return wrap;
  }

  function actorLabel(actor) {
    if (!actor || actor === 'system') return t('activity.actor.system');
    if (actor === 'user' || actor.startsWith('user:')) return t('activity.actor.you');
    if (actor.startsWith('agent:')) return actor.slice('agent:'.length) || t('activity.actor.system');
    return actor;
  }

  function activity(events) {
    const wrap = el('section', { class: 'job-activity' }, [
      el('h4', { class: 'job-activity__title', text: t('activity.title') }),
    ]);
    const list = (events || []).slice().sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    if (!list.length) {
      wrap.append(el('p', { class: 'job-activity__empty', text: t('activity.empty') }));
      return wrap;
    }
    wrap.append(el('ul', { class: 'job-activity__list' }, list.map((ev) => {
      const li = el('li', { class: 'job-activity__item' }, [
        el('span', { class: 'job-activity__time', text: dateTime(ev.created_at) }),
        el('span', { class: 'job-activity__actor', text: actorLabel(ev.actor) }),
        el('span', { class: 'job-activity__type', text: EVENT_KEY[ev.type] ? t(EVENT_KEY[ev.type]) : String(ev.type || '') }),
      ]);
      if (ev.from_state && ev.to_state) {
        const from = STATE_KEY[ev.from_state] ? t(STATE_KEY[ev.from_state]) : ev.from_state;
        const to = STATE_KEY[ev.to_state] ? t(STATE_KEY[ev.to_state]) : ev.to_state;
        li.append(el('span', { class: 'job-activity__states', text: `${from} → ${to}` }));
      }
      if (ev.reason_tag) {
        li.append(el('span', { class: 'job-activity__reason', text: REASON_KEY[ev.reason_tag] ? t(REASON_KEY[ev.reason_tag]) : ev.reason_tag }));
      }
      if (ev.comment) li.append(el('p', { class: 'job-activity__comment', text: String(ev.comment) }));
      return li;
    })));
    return wrap;
  }

  function paintDetail() {
    const focusedSignal = document.activeElement && detail.contains(document.activeElement)
      ? document.activeElement.dataset && (document.activeElement.dataset.signal || document.activeElement.dataset.reason)
      : null;
    detail.replaceChildren();
    toggle.hidden = !st.selected;
    toggle.textContent = st.open ? t('thread.hide') : t('thread.show');
    toggle.setAttribute('aria-expanded', String(st.open));
    if (!st.open || !st.data || !st.data.job) return;
    const job = st.data.job;
    const card = el('article', { class: 'job-card', 'aria-label': job.title || t('untitled') });
    card.append(header(job), meta(job), tags(job));
    const d = dates(job); if (d) card.append(d);
    if (job.brief) card.append(el('p', { class: 'job-card__brief', text: String(job.brief) }));
    card.append(actions(job));
    const w = why(st.data.score); if (w) card.append(w);
    card.append(voteRow(job));
    if (st.picking) card.append(reasonRow());
    card.append(noteField());
    card.append(el('hr', { class: 'job-card__divider' }), feedbackSection());
    card.append(el('hr', { class: 'job-card__divider' }), activity(st.data.events));
    detail.append(card);
    if (st.picking) card.querySelector('.job-reason__opt')?.focus();
    else if (focusedSignal) {
      for (const b of card.querySelectorAll('[data-signal]')) if (b.dataset.signal === focusedSignal) b.focus();
    }
  }

  // ---------------------------------------------------------------- wiring

  pick.addEventListener('change', () => {
    st.selected = pick.value || null;
    st.open = !!st.selected;
    st.picking = false;
    st.feedbackResult = null;
    st.note = '';
    st.data = null;
    paintDetail();
    loadDetail();
  });
  toggle.addEventListener('click', () => {
    st.open = !st.open;
    paintDetail();
    if (st.open && !st.data) loadDetail();
  });

  paintBar();
  await Promise.all([loadJobs(), st.selected ? loadDetail() : Promise.resolve()]);

  return {
    unmount() {
      st.alive = false;
      st.loadSeq += 1;
      threadEl.replaceChildren();
    },
    /** A live `app:jobboard:*` frame: keep the picker and the open job current
     *  without asking the server again when the frame carries the row. */
    onFrame(frame) {
      if (!st.alive || !frame || typeof frame.type !== 'string') return;
      if (!/(?:^|:)job_(?:created|updated)$/.test(frame.type)) return;
      const job = frame.job;
      if (!job || job.thread_id !== thread.id) return;
      const i = st.jobs.findIndex((j) => j.job_id === job.job_id);
      if (i >= 0) st.jobs[i] = job; else st.jobs.unshift(job);
      setStatus('');
      paintBar();
      if (st.selected === job.job_id && st.data && !st.busy) {
        st.data = { ...st.data, job };
        paintDetail();
      }
    },
  };
}
