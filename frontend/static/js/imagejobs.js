// Image jobs — the bubble that is waiting for a picture.
//
// A bot can ask DisPatch for a generated image (POST /api/image-jobs). The
// answer is not the picture: it is a real assistant message saying one is
// coming, which the server rewrites in place when the render lands — into the
// picture, or into a visible "⚠️ image failed: …" line. The message id never
// changes, so the conversation reads as one thing that happened rather than a
// promise followed, minutes later, by an unrelated bubble.
//
// This module owns only the WAITING and FAILED states. A finished job is a
// perfectly ordinary message whose body is a `[[media:…]]` directive, and it
// is deliberately left to the normal render path: that is what gives it the
// lightbox, the origin ledger, Safe Mode's media strip and No-Image Mode's
// media-only row drop, all of it for free and all of it already tested. A card
// of our own here would be a second implementation of those four rules, which
// is exactly how they drift.
//
// Nothing in a pending or failed card is an image, so there is nothing for
// Safe Mode or NIM to hide — both see the same spinner or the same warning,
// and neither issues a network request for it.

import { el } from './util.js?v=10';

// The metadata contract, in one place. `kind` is what marks the row; `status`
// is the only field that changes over the job's life.
export const KIND = 'image_job';

/**
 * The image-job state of a message, or null if it is not one.
 *
 * Split out from the rendering so it can be tested without a DOM — the
 * frontend suite has no browser, and the interesting logic here is "which of
 * these four states am I in", not "what class name did I set".
 */
// The states a card may render. `cancelled` is a THIRD terminal outcome, not a
// flavour of failure: the render was withdrawn (a deadline, a deleted thread,
// somebody pressing Interrupt on the rig itself), and the server writes a
// different sentence for it. An unknown status renders nothing rather than a
// card labelled with a string this file has never heard of.
const STATUSES = ['queued', 'running', 'done', 'failed', 'cancelled'];

export function imageJobState(msg) {
  const meta = msg && msg.metadata;
  if (!meta || meta.kind !== KIND) return null;
  const status = String(meta.status || '');
  if (!STATUSES.includes(status)) return null;
  const p = (meta.progress && typeof meta.progress === 'object') ? meta.progress : null;
  const pct = p && typeof p.percent === 'number' && isFinite(p.percent)
    ? Math.max(0, Math.min(100, Math.round(p.percent)))
    : null;
  return {
    status,
    // queued and running are the same thing to a reader: it has not arrived.
    pending: status === 'queued' || status === 'running',
    jobId: String(meta.job_id || ''),
    error: String(meta.error || ''),
    caption: String(meta.caption || ''),
    // Per-STAGE progress: a workflow with two samplers runs 0→100 twice, so
    // this is a number to show, never something to derive an ETA from.
    percent: pct,
  };
}

/**
 * The card for a message that is waiting for (or failed to get) a picture.
 *
 * Returns null for anything else — including a FINISHED job, which the caller
 * must render normally. The caller tolerates null; that is how every
 * non-image-job message falls through.
 *
 * The label is `msg.content`, not a string built here: the server already
 * wrote the human-readable line ("🖼️ Generating an image… — a blue teapot",
 * "⚠️ image failed: not enough free VRAM"), it is what a device with no
 * JavaScript-rendered card shows, and it is what the thread-list preview
 * shows. Rebuilding it here would give the same row two different wordings
 * depending on where you looked at it.
 */
export function imageJobMessageEl(msg) {
  const job = imageJobState(msg);
  if (!job || job.status === 'done') return null;

  const card = el('div', {
    class: `image-job-card ${job.pending ? 'is-pending' : 'is-failed'}`,
    dataset: { jobId: job.jobId, status: job.status },
  });
  if (job.pending) {
    // Decorative only: the text next to it says the same thing, so a reader
    // who cannot see the animation loses nothing.
    card.append(el('span', { class: 'image-job-spinner', 'aria-hidden': 'true' }));
  }
  card.append(el('span', {
    class: 'image-job-text',
    text: msg.content || (job.pending ? '🖼️ Generating an image…' : '⚠️ image failed'),
  }));
  if (job.pending && job.percent !== null) {
    // Text, not a bar: the number can go backwards when a second stage
    // starts, and a bar that resets reads as a bug rather than as progress.
    card.append(el('span', { class: 'image-job-percent', text: `${job.percent}%` }));
  }
  return card;
}
