// DisPatch thread list — Today / Older section bucketing.
//
// First-cut desktop-only feature: split a bot's thread list into two visible
// sections ("Today" and "Older") so the eye can park on what was just touched.
// The split lives in this single file so the rules are auditable and unit-
// testable in isolation — main.js is allowed to be a controller, this file is
// allowed to be a function.
//
// Bucketing rules (from the brief, 2026-09-15):
//
//   Today :=   (a) updated_at is within local-today (midnight → now), OR
//              (c) is_pinned is true (regardless of date).
//
//   Older :=   everything else.
//
// Note on rule (b): the brief also lists "(b) latest message role === 'user'
// AND updated_at within last 7 days". That needs the role of the latest
// message, which the threads endpoint does not return — the backend constraint
// in the same brief was no schema changes — so the (b) criterion is parked as
// an open issue. The bucketing function below already takes a
// `lastMessageRoleById` map, so flipping (b) on later is a 2-line change once
// the backend exposes role on threads.
//
// Sort within each section:
//   - Pinned first (in the order the backend gave them — sortThreads() upstream
//     already preserves that).
//   - Then by updated_at descending.
//
// Pure: same inputs → same outputs. No DOM, no state, no globals. The DOM
// builder (`threadSectionHeadEl`) lives at the bottom and is the only place
// this file touches `document`, so the bucketing tests can run under plain
// node without jsdom.

// ===================== Date helpers =====================

/** Local midnight for `now`, in epoch ms. `tzOffsetMin` is "minutes east of UTC"
 *  in the same convention Intl.DateTimeFormat uses: JST = +540, PDT = -420,
 *  UTC-8 (PST) = -480. This is the OPPOSITE sign of Date.getTimezoneOffset(),
 *  which returns minutes WEST. The caller converts at the call site — keeping
 *  the conversion out of the helper means the function is sign-stable for tests.
 *
 *  Worked: PST user, tzOffsetMin = -480, now = 2026-09-15T15:30 PST.
 *    - now + tzOffsetMin*60_000 = local-equivalent UTC = 2026-09-15T15:30Z
 *    - mod 86_400_000 = 55_800_000 (15.5 h past UTC midnight)
 *    - now - 55_800_000 = 2026-09-15T08:00Z = 2026-09-15T00:00 PST  ✓
 *
 *  Edge: tzOffsetMin = 0 against a UTC clock yields UTC midnight, which is what
 *  tests typically want.
 */
export function localMidnight(now, tzOffsetMin) {
  const ms = now + tzOffsetMin * 60_000;
  return now - (ms % 86_400_000);
}

/** True iff `iso` (an ISO 8601 timestamp) parses and falls within local today.
 *  Anything that fails to parse is treated as "not today" — a malformed
 *  timestamp must not bounce the thread to the wrong bucket.
 */
export function isToday(iso, now, tzOffsetMin) {
  if (!iso) return false;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return false;
  const midnight = localMidnight(now, tzOffsetMin);
  return t >= midnight && t < midnight + 86_400_000;
}

/** True iff `iso` is within the last `days` days, anchored to `now`.
 *  Used by the (b) "user-oriented within 7 days" rule when it is wired up —
 *  not exercised today because role data is missing.
 */
export function withinDays(iso, now, days) {
  if (!iso) return false;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return false;
  return (now - t) <= days * 86_400_000 && t <= now;
}

// ===================== Bucketing =====================

/** Split a thread list into Today / Older.
 *
 *  @param {Array<object>} threads            — `state.threads` (already
 *                                              backend-sorted: pinned first,
 *                                              then updated_at desc).
 *  @param {number}        now                — epoch ms (callers pass Date.now()).
 *  @param {number}        tzOffsetMin        — minutes east of UTC for the
 *                                              user's local tz. (In production
 *                                              main.js converts from
 *                                              -new Date().getTimezoneOffset().)
 *  @param {Map<string,string>} [lastMessageRoleById]
 *                                            — threadId → 'user' | 'assistant' |
 *                                              'system'. Undefined in the first
 *                                              cut because the backend does not
 *                                              expose role on the thread row;
 *                                              see the module note.
 *  @returns {Array<{name:'today'|'older', items:Array<object>}>}
 *
 *  Always returns both sections, in a stable order (today first). The caller
 *  decides which headers to paint — typically "skip the header when the
 *  section is empty AND the other section is non-empty" (see the brief).
 */
export function bucketThreads(threads, now, tzOffsetMin, lastMessageRoleById) {
  const today = [];
  const older = [];
  const roles = lastMessageRoleById || new Map();

  for (const th of threads || []) {
    if (!th) continue;
    const pinned = !!th.is_pinned;
    const updatedToday = isToday(th.updated_at, now, tzOffsetMin);
    // Rule (b): wired up but inert without role data. Kept here so a future
    // backend change that exposes role can flip it on without re-reading the
    // bucketing logic.
    const lastRole = roles.get(th.id);
    const userOrientedRecently =
      lastRole === 'user' && withinDays(th.updated_at, now, 7);

    if (pinned || updatedToday || userOrientedRecently) {
      today.push(th);
    } else {
      older.push(th);
    }
  }
  // The backend already sorts (is_pinned DESC, updated_at DESC), and
  // sortThreads() in main.js preserves that on upsert. Today's `pinned` items
  // land before non-pinned because of that incoming order — no extra sort
  // needed here. The Today bucket does, however, separate pins from non-pins
  // visually via the .pinned class on each row.
  return [
    { name: 'today', items: today },
    { name: 'older', items: older },
  ];
}

/** A stable signature for the inputs that change the section layout. Same
 *  signature on consecutive renders means the cached buckets are still valid
 *  and main.js can skip re-bucketing — repaintPreserving rebuilds the DOM
 *  every WS frame, and re-bucketing 200 threads each time is wasted work.
 *
 *  Composed from:
 *    - thread identity + updated_at + is_pinned (anything that moves a row)
 *    - now (rounded to 60s — finer grain would just thrash the cache)
 *    - tzOffsetMin
 *    - role map size (its content is harder to hash cheaply; size is enough
 *      for the first cut because role data is empty in practice today)
 */
export function filterSignature(threads, now, tzOffsetMin, lastMessageRoleById) {
  const minutes = Math.floor(now / 60_000);
  const head = threads.length + '|' + minutes + '|' + tzOffsetMin + '|';
  const tail = (lastMessageRoleById && lastMessageRoleById.size) || 0;
  let body = '';
  // updated_at + is_pinned is enough — title / preview / status don't move
  // a thread between buckets. A loop, not a join, keeps it cheap.
  for (let i = 0; i < threads.length; i++) {
    const th = threads[i];
    body += (th.id || '') + ':' + (th.updated_at || '') + ':' + (th.is_pinned ? '1' : '0') + ';';
  }
  return head + body + '|' + tail;
}

// ===================== Visibility gate =====================

/** True iff the section headers should render. The desktop binary split is
 *  the whole point of this feature; on mobile the list is intentionally flat.
 *  And while the search modal is open on a wide viewport, the threads list is
 *  visible behind it — a single Older-thread hit could otherwise appear under
 *  a misleading "Older" header.
 */
export function shouldShowThreadSections({ isMobile, searchOpen }) {
  if (isMobile) return false;
  if (searchOpen) return false;
  return true;
}

// ===================== DOM builder =====================

/** The section header element. Desktop-only: CSS hides it under 769px (see
 *  app.css .thread-section-head + @media). The hairline rule is a CSS
 *  gradient on the parent — there is no separate <hr>.
 *
 *  Uses a placeholder text node when `t` is not provided so a render before
 *  i18n.init() lands still paints something legible (the brief expects this
 *  to never happen — init() resolves before the first render — but a missing
 *  translator would otherwise render blank chrome on a delayed frame).
 */
export function threadSectionHeadEl(name, t) {
  const key = name === 'today' ? 'threads.section.today' : 'threads.section.older';
  const label = (typeof t === 'function') ? t(key) : (name === 'today' ? 'Today' : 'Older');
  const span = document.createElement('span');
  span.className = 'thread-section-head__label';
  span.setAttribute('data-i18n', key);
  span.textContent = label;

  const head = document.createElement('div');
  head.className = 'thread-section-head';
  head.setAttribute('data-section', name);
  head.setAttribute('role', 'presentation');  // chrome, not a heading landmark
  head.appendChild(span);
  return head;
}
