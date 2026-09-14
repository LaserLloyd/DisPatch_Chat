// Unit tests for the Today / Older section bucketing.
//
// The split is the load-bearing visual contract on the desktop thread list,
// so this file pins it: pinned threads always land in Today regardless of
// date, a thread updated within local-today lands in Today, an older thread
// lands in Older, and the (b) "user-message within 7 days" rule kicks in when
// role data is fed in.
//
// Run: node --test frontend/tests/thread-sections.test.js
//
// Pure functions in this file run under plain node. The DOM-builder test
// pulls jsdom the same way jobs.test.js does and skips if it is not installed.

import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
let jsdom = null;
try { jsdom = require('jsdom'); } catch { /* skipped DOM test */ }

const {
  localMidnight, isToday, withinDays,
  bucketThreads, filterSignature,
  shouldShowThreadSections, threadSectionHeadEl,
} = await import('../static/js/thread-sections.js?v=1');

// 2026-09-15T12:00:00Z — anchor for "today" tests. Pick something safely mid-day
// in a plausible timezone so midnight-rollover tests don't fight a DST cliff.
const NOW = Date.parse('2026-09-15T12:00:00Z');

// ===================== localMidnight =====================

test('localMidnight(UTC, 0) is UTC midnight', () => {
  const ms = localMidnight(NOW, 0);
  assert.equal(ms, Date.parse('2026-09-15T00:00:00Z'));
});

test('localMidnight at JST (+540) returns 15:00 UTC the prior day', () => {
  // 2026-09-15T12:00Z = 2026-09-15T21:00 JST. Local date is Sept 15 JST,
  // so local midnight = 2026-09-15T00:00 JST = 2026-09-14T15:00 UTC.
  const ms = localMidnight(NOW, 540);
  assert.equal(ms, Date.parse('2026-09-14T15:00:00Z'));
});

test('localMidnight at PST (-480) returns 08:00 UTC the same day', () => {
  // 2026-09-15T12:00Z = 2026-09-15T04:00 PST. Local date is Sept 15 PST,
  // so local midnight = 2026-09-15T00:00 PST = 2026-09-15T08:00 UTC.
  const ms = localMidnight(NOW, -480);
  assert.equal(ms, Date.parse('2026-09-15T08:00:00Z'));
});

test('localMidnight holds at the second before midnight local', () => {
  // One second before JST midnight (i.e. 23:59:59 JST on Sept 15) =
  // 14:59:59 UTC on the 15th. Local midnight is still the START of the
  // current local day (Sept 15) = 2026-09-14T15:00:00Z.
  const almost = Date.parse('2026-09-15T14:59:59Z');
  assert.equal(localMidnight(almost, 540), Date.parse('2026-09-14T15:00:00Z'));
});

test('localMidnight flips at the second of midnight local', () => {
  // Exactly JST midnight = 15:00 UTC on the 15th = 00:00 JST on Sept 16.
  // Local midnight is now the start of Sept 16 JST = 2026-09-15T15:00:00Z.
  const at = Date.parse('2026-09-15T15:00:00Z');
  assert.equal(localMidnight(at, 540), Date.parse('2026-09-15T15:00:00Z'));
});

// ===================== isToday =====================

test('isToday: now is today', () => {
  assert.equal(isToday('2026-09-15T12:00:00Z', NOW, 0), true);
});

test('isToday: yesterday is not today (UTC)', () => {
  assert.equal(isToday('2026-09-14T23:59:59Z', NOW, 0), false);
});

test('isToday: tomorrow is not today (UTC)', () => {
  assert.equal(isToday('2026-09-16T00:00:01Z', NOW, 0), false);
});

test('isToday: PST thread at 23:30 UTC is still "today PST"', () => {
  // 23:30 UTC = 15:30 PST same day → today for a PST user.
  assert.equal(isToday('2026-09-15T23:30:00Z', NOW, -480), true);
});

test('isToday: PST thread at 07:59 UTC is "yesterday PST"', () => {
  // 07:59 UTC = 23:59 PST on the 14th → yesterday for a PST user.
  assert.equal(isToday('2026-09-15T07:59:00Z', NOW, -480), false);
});

test('isToday: malformed date is not today', () => {
  assert.equal(isToday('not-a-date', NOW, 0), false);
  assert.equal(isToday('', NOW, 0), false);
  assert.equal(isToday(null, NOW, 0), false);
});

// ===================== withinDays =====================

test('withinDays: now is within 7 days', () => {
  assert.equal(withinDays('2026-09-15T11:00:00Z', NOW, 7), true);
});

test('withinDays: 6 days ago is within 7 days', () => {
  const six = new Date(NOW - 6 * 86_400_000).toISOString();
  assert.equal(withinDays(six, NOW, 7), true);
});

test('withinDays: 8 days ago is NOT within 7 days', () => {
  const eight = new Date(NOW - 8 * 86_400_000).toISOString();
  assert.equal(withinDays(eight, NOW, 7), false);
});

test('withinDays: future timestamp is NOT within window', () => {
  const future = new Date(NOW + 60_000).toISOString();
  assert.equal(withinDays(future, NOW, 7), false);
});

// ===================== bucketThreads =====================

const T_YESTERDAY = '2026-09-14T12:00:00Z';
const T_TODAY = '2026-09-15T10:00:00Z';
const T_OLD = '2026-09-10T12:00:00Z';

function mk(id, updated_at, opts = {}) {
  return { id, updated_at, is_pinned: false, title: id, ...opts };
}

test('bucketThreads: empty input → empty buckets, both names present', () => {
  const b = bucketThreads([], NOW, 0);
  assert.equal(b.length, 2);
  assert.equal(b[0].name, 'today');
  assert.equal(b[1].name, 'older');
  assert.equal(b[0].items.length, 0);
  assert.equal(b[1].items.length, 0);
});

test('bucketThreads: today thread → Today, older thread → Older (UTC)', () => {
  const threads = [mk('a', T_TODAY), mk('b', T_OLD)];
  const b = bucketThreads(threads, NOW, 0);
  assert.deepEqual(b[0].items.map((t) => t.id), ['a']);
  assert.deepEqual(b[1].items.map((t) => t.id), ['b']);
});

test('bucketThreads: pinned thread ALWAYS in Today, regardless of date', () => {
  const threads = [
    mk('old-but-pinned', T_OLD, { is_pinned: true }),
    mk('recent-unpinned', T_TODAY),
  ];
  const b = bucketThreads(threads, NOW, 0);
  assert.deepEqual(b[0].items.map((t) => t.id), ['old-but-pinned', 'recent-unpinned']);
  assert.deepEqual(b[1].items.map((t) => t.id), []);
});

test('bucketThreads: rule (b) — last-role=user within 7 days → Today', () => {
  const six = new Date(NOW - 6 * 86_400_000).toISOString();
  const threads = [mk('user-thread', six, { is_pinned: false })];
  const roles = new Map([['user-thread', 'user']]);
  const b = bucketThreads(threads, NOW, 0, roles);
  assert.deepEqual(b[0].items.map((t) => t.id), ['user-thread']);
  assert.deepEqual(b[1].items.map((t) => t.id), []);
});

test('bucketThreads: rule (b) — last-role=assistant within 7 days is NOT Today', () => {
  // Without rule (a) or (c), an assistant-only update within 7 days is Older.
  // This is the conservative first cut — see the open issue note in the module.
  const six = new Date(NOW - 6 * 86_400_000).toISOString();
  const threads = [mk('bot-thread', six, { is_pinned: false })];
  const roles = new Map([['bot-thread', 'assistant']]);
  const b = bucketThreads(threads, NOW, 0, roles);
  assert.deepEqual(b[0].items.map((t) => t.id), []);
  assert.deepEqual(b[1].items.map((t) => t.id), ['bot-thread']);
});

test('bucketThreads: rule (b) — last-role=user but 8 days old → Older', () => {
  const eight = new Date(NOW - 8 * 86_400_000).toISOString();
  const threads = [mk('stale-user', eight, { is_pinned: false })];
  const roles = new Map([['stale-user', 'user']]);
  const b = bucketThreads(threads, NOW, 0, roles);
  assert.deepEqual(b[0].items.map((t) => t.id), []);
  assert.deepEqual(b[1].items.map((t) => t.id), ['stale-user']);
});

test('bucketThreads: a thread is in EXACTLY ONE bucket', () => {
  // Three threads that each qualify for Today under a different rule — verify
  // none of them lands in Older.
  const threads = [
    mk('pinned-old', T_OLD, { is_pinned: true }),
    mk('pinned-today', T_TODAY, { is_pinned: true }),
    mk('today', T_TODAY),
    mk('older', T_OLD),
  ];
  const b = bucketThreads(threads, NOW, 0);
  const inBoth = b[0].items.filter((t) => b[1].items.includes(t));
  assert.equal(inBoth.length, 0);
  assert.equal(b[0].items.length + b[1].items.length, threads.length);
});

test('bucketThreads: a thread with no updated_at falls into Older', () => {
  // A thread that has never been touched (impossible in practice, but a
  // malformed thread must not be silently elevated to Today).
  const threads = [mk('fresh-id', null), mk('empty-updated', '')];
  const b = bucketThreads(threads, NOW, 0);
  assert.deepEqual(b[0].items, []);
  assert.deepEqual(b[1].items.map((t) => t.id), ['fresh-id', 'empty-updated']);
});

test('bucketThreads: pinned-then-recent order is preserved (backend-sorted)', () => {
  // The backend returns pinned-first, updated_at-desc within each pin state;
  // bucketThreads() must NOT re-sort — re-sorting would let a stale pinned row
  // sink below a fresh non-pinned one in the Today bucket.
  const fixedThreads = [
    mk('p1', '2026-09-01T10:00:00Z', { is_pinned: true }),
    mk('p2', '2026-08-15T10:00:00Z', { is_pinned: true }),
    mk('r1', '2026-09-15T10:00:00Z'),
    mk('r2', '2026-09-15T11:00:00Z'),
  ];
  const b = bucketThreads(fixedThreads, NOW, 0);
  // Today should be [p1, p2, r1, r2] in input order (pins before recents;
  // within recents, r1 then r2 by input).
  assert.deepEqual(b[0].items.map((t) => t.id), ['p1', 'p2', 'r1', 'r2']);
});

// ===================== filterSignature =====================

test('filterSignature is stable across identical inputs', () => {
  const threads = [mk('a', T_TODAY), mk('b', T_OLD)];
  const s1 = filterSignature(threads, NOW, 0);
  const s2 = filterSignature(threads, NOW, 0);
  assert.equal(s1, s2);
});

test('filterSignature changes when a thread moves between buckets', () => {
  const before = [mk('a', T_OLD)];   // → Older
  const after = [mk('a', T_TODAY)];   // → Today
  assert.notEqual(filterSignature(before, NOW, 0), filterSignature(after, NOW, 0));
});

test('filterSignature changes when a thread is pinned/unpinned', () => {
  const before = [mk('a', T_OLD, { is_pinned: false })];
  const after = [mk('a', T_OLD, { is_pinned: true })];
  assert.notEqual(filterSignature(before, NOW, 0), filterSignature(after, NOW, 0));
});

test('filterSignature tolerates clock jitter within 60s', () => {
  const threads = [mk('a', T_TODAY)];
  const s1 = filterSignature(threads, NOW, 0);
  const s2 = filterSignature(threads, NOW + 30_000, 0);  // 30s later, same minute
  assert.equal(s1, s2);
});

test('filterSignature changes when the role map gains an entry', () => {
  const threads = [mk('a', T_OLD)];
  const noRoles = new Map();
  const withRoles = new Map([['a', 'user']]);
  assert.notEqual(
    filterSignature(threads, NOW, 0, noRoles),
    filterSignature(threads, NOW, 0, withRoles),
  );
});

// ===================== shouldShowThreadSections =====================

test('shouldShowThreadSections: desktop, no search → true', () => {
  assert.equal(shouldShowThreadSections({ isMobile: false, searchOpen: false }), true);
});

test('shouldShowThreadSections: mobile → false (CSS hides too, but JS skips the work)', () => {
  assert.equal(shouldShowThreadSections({ isMobile: true, searchOpen: false }), false);
});

test('shouldShowThreadSections: desktop + search open → false', () => {
  assert.equal(shouldShowThreadSections({ isMobile: false, searchOpen: true }), false);
});

test('shouldShowThreadSections: mobile + search open → false (either gate trips it)', () => {
  assert.equal(shouldShowThreadSections({ isMobile: true, searchOpen: true }), false);
});

// ===================== DOM builder (jsdom) =====================

test('threadSectionHeadEl builds a desktop header with the right label', { skip: jsdom ? false : 'jsdom is not installed' }, () => {
  const { JSDOM } = jsdom;
  const dom = new JSDOM('<!doctype html><html><body></body></html>');
  globalThis.document = dom.window.document;

  const t = (key) => ({ 'threads.section.today': 'Today', 'threads.section.older': 'Older' })[key] || key;

  const today = threadSectionHeadEl('today', t);
  assert.equal(today.getAttribute('data-section'), 'today');
  // data-i18n lives on the label span, not the head wrapper, so a language
  // switch repaints the label without re-rendering the wrapper (which is
  // chrome and should not flash).
  const todayLabel = today.querySelector('[data-i18n]');
  assert.ok(todayLabel, 'header should contain a [data-i18n] label');
  assert.equal(todayLabel.textContent, 'Today');
  assert.equal(todayLabel.getAttribute('data-i18n'), 'threads.section.today');

  const older = threadSectionHeadEl('older', t);
  assert.equal(older.getAttribute('data-section'), 'older');
  const olderLabel = older.querySelector('[data-i18n]');
  assert.ok(olderLabel, 'header should contain a [data-i18n] label');
  assert.equal(olderLabel.textContent, 'Older');
  assert.equal(olderLabel.getAttribute('data-i18n'), 'threads.section.older');

  // The head is NOT a thread-item — repaintPreserving()'s focus selector must
  // not catch it, otherwise focus restoration on a thread row could land on
  // the chrome.
  assert.equal(today.matches('.thread-item'), false);
  assert.equal(older.matches('.thread-item'), false);
});

test('threadSectionHeadEl falls back to English without a translator', { skip: jsdom ? false : 'jsdom is not installed' }, () => {
  const { JSDOM } = jsdom;
  const dom = new JSDOM('<!doctype html><html><body></body></html>');
  globalThis.document = dom.window.document;
  const today = threadSectionHeadEl('today');
  assert.equal(today.querySelector('[data-i18n]').textContent, 'Today');
});
