// System-notice classification (notice.js).
//
// The shapes below are taken from real rows in the family app's database —
// every sender that was clogging threads — plus the replies that must NOT be
// swallowed. The second half matters as much as the first: a real bot answer
// hidden in a collapsed line is a worse bug than a noisy alert.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { classifyNotice, noticeHeadline } from '../static/js/notice.js';

const a = (content, metadata) => ({ id: 'm', role: 'assistant', content, metadata });

test('a failed run report is an error notice with a plain headline', () => {
  const n = classifyNotice(a(
    '❌ Run `bench-20260924-deepseek-pro` · `CrucibleForge all: deepseek-pro` failed — see report\n\n```text\nsee report\n```\n\nFull report: `/x/report.md`',
    { delivery_key: 'ab47', origin: 'inject' }));
  assert.equal(n.level, 'error');
  assert.equal(n.headline, 'Run bench-20260924-deepseek-pro · CrucibleForge all: deepseek-pro failed — see report');
});

test('warning run reports and box-smoke alerts are warn notices', () => {
  assert.equal(classifyNotice(a('⚠️ Run `w-1` · `Bench` partial', { delivery_key: 'k' })).level, 'warn');
  const smoke = classifyNotice(a('**⚠️ NEW FAIL: mailforge_mail**\n\n```\nNEWLY FAILING:\n```', { origin: 'inject' }));
  assert.equal(smoke.level, 'warn');
  assert.equal(smoke.headline, 'NEW FAIL: mailforge_mail');
  assert.equal(classifyNotice(a('⚠️ practice: practice-doctor.service failed', { origin: 'inject' })).level, 'warn');
});

test('a RECOVERED alert is an ok notice', () => {
  const n = classifyNotice(a('**✅ RECOVERED: task_drain**\n\n```\nRECOVERED:\n```', { origin: 'inject' }));
  assert.equal(n.level, 'ok');
  assert.equal(n.headline, 'RECOVERED: task_drain');
});

test('metadata-flagged failures are notices whatever the text', () => {
  assert.ok(classifyNotice(a('⚠️ My hourly picture didn’t go out', { doxy_pics: { failure: true, slot: 'hourly' } })));
  assert.equal(classifyNotice(a('⚠️ clawforge-watchdog.service FAILED', { source: 'watchdog-failure-notify' })).level, 'error');
  assert.equal(classifyNotice(a('⚠️ image failed: timed out', { kind: 'image_job', status: 'failed' })).level, 'error');
  assert.ok(classifyNotice(a('anything', { notice: true })));
  assert.equal(classifyNotice(a('anything', { notice: { level: 'info' } })).level, 'info');
});

test('a short gateway error reply is a notice', () => {
  assert.ok(classifyNotice(a('⚠️ 🧰 Process failed', { run_id: 'r', stop_reason: 'toolUse' })));
});

test('ordinary replies, successes and user messages are left alone', () => {
  assert.equal(classifyNotice(a('Sure! Here is the plan.', { origin: 'inject' })), null);
  assert.equal(classifyNotice(a('✅ Run `bench-x` done — Total 88.5', { delivery_key: 'k' })), null);
  assert.equal(classifyNotice(a('☀️ Morning brief — the fleet failed twice overnight', { origin: 'inject' })), null);
  assert.equal(classifyNotice({ role: 'user', content: '❌ this failed', metadata: {} }), null);
  // Sub rows already collapse on their own.
  assert.equal(classifyNotice(a('⚠️ Reaction didn’t fire', { sub: true })), null);
  // An image job that is still pending or finished is not a notice.
  assert.equal(classifyNotice(a('queued', { kind: 'image_job', status: 'queued' })), null);
});

test('an injected message that only OPENS with a warning glyph keeps its bubble', () => {
  // Agents deliver proactive replies through /api/inject too; the glyph
  // alone must not collapse them (review 2026-09-24).
  assert.equal(classifyNotice(a('⚠️ Just so you know, I moved your dentist appointment to Tuesday at 10, '
    + 'since the Monday slot clashed with the school run.', { origin: 'inject' })), null);
  // …while the real machine alerts, which all carry a failure word, still do.
  assert.equal(classifyNotice(a('⚠️ This box has lost its network connection — the rig (images) and the cloud models are unreachable.', { origin: 'inject' })).level, 'warn');
  assert.equal(classifyNotice(a('⚠️ ComfyUI on the rig cannot render [cards_vacating] — image generation is unavailable', { origin: 'inject' })).level, 'warn');
  assert.equal(classifyNotice(a('⚠️ My hourly picture didn’t go out — the rig is leased by a benchmark', { origin: 'inject' })).level, 'warn');
});

test('a bot opening a long, real answer with ⚠️ keeps its bubble', () => {
  const long = '⚠️ Heads up before you deploy: the backup drive is unplugged, so '
    + 'tonight’s mirror will fail. Here is what I would do instead. '.repeat(4);
  assert.equal(classifyNotice(a(long, { followup: true })), null);
  // …and a short one that is not error-worded stays too.
  assert.equal(classifyNotice(a('⚠️ Careful, it is hot today', {})), null);
});

test('headline falls back to the first non-empty line', () => {
  assert.equal(noticeHeadline('\n\n  ❌  **Deploy** failed\nmore'), 'Deploy failed');
  assert.equal(noticeHeadline(''), '');
});

// --- the metadata.notice convention (2026-09-25) ---------------------------

test('an explicit notice renders as a notice even though the server set sub', () => {
  for (const level of ['info', 'ok', 'warn', 'error']) {
    const n = classifyNotice(a('🔧 Run `bench-1` recovered after 3h01m', { origin: 'inject', sub: true, notice: { level } }));
    assert.ok(n, `level ${level} was swallowed by the sub early-return`);
    assert.equal(n.level, level);
  }
});

test('an explicit notice with no or unknown level falls back to the glyph, then info', () => {
  assert.equal(classifyNotice(a('📊 Daily summary', { sub: true, notice: true })).level, 'info');
  assert.equal(classifyNotice(a('❌ Run x failed', { sub: true, notice: { level: 'LOUD' } })).level, 'error');
  assert.equal(classifyNotice(a('plain words', { notice: {} })).level, 'info');
});

test('the notice headline is the first line, without markdown', () => {
  const n = classifyNotice(a('📊 **Daily summary** — 3 runs\n\nline two', { sub: true, notice: { level: 'info' } }));
  assert.equal(n.headline, '📊 Daily summary — 3 runs');
});

test('a plain sub row without notice is still not a notice', () => {
  assert.equal(classifyNotice(a('⚠️ 🛠️ Exec failed: `x` (exit 1)', { sub: true })), null);
});
