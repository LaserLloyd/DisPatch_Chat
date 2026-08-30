// Image-job state classification.
//
// The rendering needs a DOM, but the decision that actually matters does not:
// which of a message's four states am I in, and — the load-bearing one — is a
// FINISHED job left alone so the ordinary markdown path renders its picture?
// That is what keeps the lightbox, Safe Mode's media strip and No-Image Mode's
// media-only row drop working for image jobs without a second implementation
// of any of them, and it is the thing a refactor would quietly break.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { imageJobState, KIND } from '../static/js/imagejobs.js';

const msg = (metadata, content = 'x') => ({ id: 'm1', role: 'assistant', content, metadata });

test('a message with no metadata is not an image job', () => {
  assert.equal(imageJobState({ id: 'm1', role: 'assistant', content: 'hi' }), null);
  assert.equal(imageJobState(msg(null)), null);
  assert.equal(imageJobState(msg({})), null);
});

test('another metadata kind is not an image job', () => {
  assert.equal(imageJobState(msg({ kind: 'reaction', status: 'done' })), null);
  assert.equal(imageJobState(msg({ sub: true })), null);
});

test('queued and running both read as pending', () => {
  for (const status of ['queued', 'running']) {
    const job = imageJobState(msg({ kind: KIND, status, job_id: 'abc' }));
    assert.ok(job, `${status} should classify`);
    assert.equal(job.pending, true);
    assert.equal(job.status, status);
    assert.equal(job.jobId, 'abc');
  }
});

test('a failed job is classified but not pending, and carries its reason', () => {
  const job = imageJobState(msg({ kind: KIND, status: 'failed', error: 'no free VRAM' }));
  assert.equal(job.pending, false);
  assert.equal(job.status, 'failed');
  assert.equal(job.error, 'no free VRAM');
});

test('a finished job is classified as done — the caller renders it normally', () => {
  const job = imageJobState(msg({ kind: KIND, status: 'done' }, '[[media:/media/a.png]]'));
  assert.equal(job.status, 'done');
  assert.equal(job.pending, false);
});

test('an unknown status is refused rather than rendered as a mystery card', () => {
  assert.equal(imageJobState(msg({ kind: KIND, status: 'pondering' })), null);
  assert.equal(imageJobState(msg({ kind: KIND })), null);
});

test('metadata fields are coerced, so a mangled row cannot inject a non-string', () => {
  const job = imageJobState(msg({ kind: KIND, status: 'failed', job_id: 7, error: null }));
  assert.equal(typeof job.jobId, 'string');
  assert.equal(job.jobId, '7');
  assert.equal(job.error, '');
});
