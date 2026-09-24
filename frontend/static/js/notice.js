// System notices: machine-posted alerts that render as ONE quiet collapsed
// line instead of a full chat bubble.
//
// Background jobs post straight into threads through /api/inject: run reports
// from runs-deliver ("❌ Run … failed — see report"), box-smoke's NEW FAIL /
// RECOVERED pair, the watchdog failure notifier, doxy-pics' missed hourly
// picture, ComfyUI outages, failed image jobs. Each one arrived as a
// full-size message from the bot — avatar, name, bubble, code block — so a
// bad night of benchmarks buried the actual conversation. They are status,
// not conversation: still there, one tap to expand, but not shouting.
//
// Pure module — no DOM — so the classification can be tested on its own. The
// decision is made from data every row already carries; nothing server-side
// changed, which is also why the rows already in the database get the new
// look without a migration.
//
// A sender can opt in explicitly with metadata.notice = true (or a
// metadata.notice object with a `level`) instead of relying on the text rule.

// A leading alert glyph, optionally inside the **bold** box-smoke uses.
const LEAD = /^\s*(?:\*\*)?\s*(⚠️|⚠|❌|🚨|✅\s*RECOVERED\b)/u;
// Just the leading glyph — the headline keeps words like RECOVERED.
const GLYPH = /^\s*(?:\*\*)?\s*(?:⚠️|⚠|❌|🚨|✅)\s*/u;
// Words that make a SHORT un-injected reply read as an error rather than a
// bot deliberately opening with a warning sign.
const ERRORISH = /\b(fail(?:ed|ing|ure)?|error|timed out|unavailable|unreachable|crash(?:ed)?)\b/i;
// A gateway-side error ("⚠️ 🧰 Process failed") is short. A real reply that
// happens to start with ⚠️ is usually not, and must stay a real reply.
const SHORT_REPLY = 240;
// An injected post is machine-posted MOST of the time, but agents deliver
// proactive messages through /api/inject too ("⚠️ Just so you know, I moved
// your dentist appointment…"). So the glyph alone is not enough there
// either: the text has to read as a failure. This list is wider than
// ERRORISH because machine alerts are terse ("lost its network connection",
// "cannot render", "didn't go out"); checked against every injected alert in
// the family app's database on 2026-09-24 — none of the 111 stopped
// collapsing.
const INJECT_ERRORISH = /\b(fail(?:ed|ing|ure|s)?|error|timed out|timeout|unavailable|unreachable|crash(?:ed)?|down|lost|cannot|can't|missed|didn.t|refused|denied|stale|stranded|partial|degraded|never finished|not (?:running|found|reachable))\b/i;

function levelFor(glyph) {
  if (!glyph) return 'warn';
  if (glyph.startsWith('❌') || glyph.startsWith('🚨')) return 'error';
  if (glyph.startsWith('✅')) return 'ok';
  return 'warn';
}

/** First non-empty line, stripped to plain text for the one-line summary. */
export function noticeHeadline(content) {
  const line = String(content || '').split('\n').map((l) => l.trim()).find(Boolean) || '';
  return line
    .replace(GLYPH, '')
    .replace(/\*\*|__|`/g, '')
    .replace(/^[\s:·—-]+/, '')
    .trim();
}

/** null for an ordinary message; otherwise { level, headline }. */
export function classifyNotice(msg) {
  if (!msg || msg.role === 'user') return null;
  const meta = msg.metadata || {};
  // Sub rows (working output, "reaction didn't fire") already collapse.
  if (meta.sub) return null;
  const content = String(msg.content || '');
  const m = LEAD.exec(content);
  const headline = noticeHeadline(content);

  if (meta.notice) {
    const lvl = typeof meta.notice === 'object' && meta.notice.level;
    return { level: ['error', 'warn', 'ok', 'info'].includes(lvl) ? lvl : levelFor(m && m[1]), headline };
  }
  if (meta.kind === 'image_job' && meta.status === 'failed') return { level: 'error', headline };
  if (meta.doxy_pics && meta.doxy_pics.failure) return { level: 'warn', headline };
  if (meta.source === 'watchdog-failure-notify') return { level: 'error', headline };

  if (!m) return null;
  // Machine-posted (inject / run delivery): the glyph plus a failure word.
  // A RECOVERED line is the one glyph-only exception — it is box-smoke's own
  // pair to NEW FAIL and carries no error word by design.
  const injected = meta.origin === 'inject' || !!meta.delivery_key;
  if (injected) {
    if (m[1].startsWith('✅') || INJECT_ERRORISH.test(content)) {
      return { level: levelFor(m[1]), headline };
    }
    return null;
  }
  // Anything else is a bot's own reply: only a short, error-worded one counts.
  if (content.length <= SHORT_REPLY && ERRORISH.test(content)) {
    return { level: levelFor(m[1]), headline };
  }
  return null;
}
