// Locale keys and the code, kept in sync in BOTH directions.
//
// Dead keys are not free: every one of them is eight files of translator time,
// and a reviewer reading en.json cannot tell which strings the app still shows.
// This pass found 24 keys that nothing had referenced in months (a whole
// `nav.dashboard_*` family from a gear rail that became tabs, `settings.security`
// from a modal that became a pane, nine unused `common.*` verbs).
//
// The other direction matters more: t('typo.key') does not throw. i18n.js logs
// one console warning and renders a humanised guess of the key, so a mistyped
// key ships as plausible-looking English and no test notices.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { blankNonCode } from './_source-scan.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const JS_DIR = join(STATIC, 'js');

// (Until 2.2.0 the root-level drive.js was scanned too. Voice chat is an
// add-on now: its strings live in the add-on's own bundles, never here.)
const SOURCE = [readFileSync(join(STATIC, 'index.html'), 'utf8')]
  .concat(readdirSync(JS_DIR).filter((f) => f.endsWith('.js'))
    .map((f) => readFileSync(join(JS_DIR, f), 'utf8')))
  .join('\n');

// The same source with comments removed. i18n.js documents its own API in a
// JSDoc block — `t('chat.with_bot')`, `data-i18n-attr="title=nav.lock"` — and a
// scan that reads documentation as code reports keys that do not exist.
//
// Comments go through tests/_source-scan.js rather than a sequence of regexes,
// with `keepStrings` so `t('some.key')` is still readable. Ordered passes had
// the same hole here as in callable.test.js: `accept: 'image/*'` in main.js
// opened a pseudo block comment that ran to the next real `*/`, taking dozens
// of live t() calls out of this scan — so the "every key in en.json is used"
// check was reporting clean on keys it could no longer see.
//
// HTML comments are stripped first and separately: the scanner is a JS lexer
// and index.html is part of SOURCE.
const CODE = blankNonCode(
  SOURCE.replace(/<!--[\s\S]*?-->/g, (m) => m.replace(/[^\n]/g, ' ')),
  { keepStrings: true },
);

const EN = JSON.parse(readFileSync(join(STATIC, 'locales', 'en.json'), 'utf8'));

function flatten(obj, prefix = '') {
  const out = [];
  for (const [k, v] of Object.entries(obj)) {
    if (k === '$meta') continue;
    // A plural object IS a leaf value, not a namespace.
    if (v && typeof v === 'object' && !Array.isArray(v) && v.other === undefined) {
      out.push(...flatten(v, `${prefix}${k}.`));
    } else {
      out.push(prefix + k);
    }
  }
  return out;
}

// Keys the code assembles at runtime, so no literal appears in the source.
// EXPLICIT, not a wildcard sweep: each entry names the exact expression that
// builds it, because an allowlist nobody can audit is how the dead keys got in.
const DYNAMIC = [
  // js/settings_schema.js  customUnits() / the Custom… unit picker:  t(`schema.unit_${u}`)
  { re: /^schema\.unit_(seconds|minutes|hours|days|megabytes|gigabytes)$/, built: 'settings_schema.js  t(`schema.unit_${u}`)' },
  // js/main.js  paintAccountChoices():  t(`acct.opt_${c}`) over accountChoices()
  { re: /^acct\.opt_(pin_change|pin_set|pin_remove|pw_set|pw_change|my_pin)$/, built: 'main.js  t(`acct.opt_${c}`)' },
  // js/plugins.js  lookupLabel():  `plugins.lookup_${id}` over the server's livetools names
  { re: /^plugins\.lookup_[a-z_]+$/, built: 'plugins.js  lookupLabel()' },
  // js/main.js  renderHarness():  t('harness.state_' + svc)
  { re: /^harness\.state_(running|stopped|starting|failed|not_installed|unknown)$/,
    built: "main.js  t('harness.state_' + svc)" },
  // js/main.js  renderHarness():  t(`harness.${noteKey}_text` / `_hint`)
  { re: /^harness\.(not_installed|stopped|remote)_(text|hint)$/,
    built: 'main.js  t(`harness.${noteKey}_text|_hint`)' },
  // js/main.js  harness service ops:  t('harness.toast_' + verb)
  { re: /^harness\.toast_(started|stopped|restarted)$/,
    built: "main.js  t('harness.toast_' + …)" },
  // js/main.js  renderStudioForge():  t('studioforge.state_' + sv)
  { re: /^studioforge\.state_(up|down|unreachable|blocked|insecure|checking|unknown)$/,
    built: "main.js  t('studioforge.state_' + sv)" },
  // js/main.js  renderStudioForge():  t(`studioforge.${noteKey}_text` / `_hint`)
  { re: /^studioforge\.(unconfigured|checking|down|unreachable|blocked|insecure)_(text|hint)$/,
    built: 'main.js  t(`studioforge.${noteKey}_text|_hint`)' },
  // js/advisor.js  handoffStripEl():  t(`advisor.state_${h.state}`)
  { re: /^advisor\.state_(proposed|running|done|failed|dismissed)$/,
    built: 'advisor.js  t(`advisor.state_${h.state}`)' },
  // js/i18n.js  fileSize():  t(`unit.${u}`)
  { re: /^unit\.(b|kb|mb|gb|tb|pb)$/, built: 'i18n.js  t(`unit.${u}`)' },
  // js/dashboard.js  paintBanner():  T(`dash.count_${lv}`)
  { re: /^dash\.count_(ok|warn|fail)$/, built: 'dashboard.js  T(`dash.count_${lv}`)' },
  // js/markdown.js  blockquote():  t(`md.callout_${kind}`), kind from CALLOUT_RE
  { re: /^msg\.callout_(note|tip|important|warning|caution)$/,
    built: 'markdown.js  t(`msg.callout_${kind}`)' },
  // js/main.js  feedbackReasonRow():  t(`msg.feedback_reason_${reason}`), set
  // matches FEEDBACK_REASONS — mirrors the backend's MessageFeedbackIn.reason
  // Literal enum exactly (models.py).
  { re: /^msg\.feedback_reason_(inaccurate|unhelpful|too_long|off_topic|tone|other)$/,
    built: 'main.js  t(`msg.feedback_reason_${reason}`)' },
  // js/main.js  openModelPicker():  t(`chat.thinking_${lvl}`), lvl drawn from
  // modelchip.js's THINKING_LEVELS (chat.thinking_default is a separate,
  // literal t() call for the picker's "bot default" option and needs no
  // entry here).
  { re: /^chat\.thinking_(off|low|medium|high|max|adaptive)$/,
    built: "main.js  t(`chat.thinking_${lvl}`)" },
  // js/main.js  noticeMessageEl():  t(`msg.notice_level_${notice.level}`), the
  // level set is notice.js's closed vocabulary (error | warn | ok | info).
  { re: /^msg\.notice_level_(error|warn|ok|info)$/,
    built: 'main.js  t(`msg.notice_level_${notice.level}`)' },
  // 3.0 profiles, roles and policy -- keys built from a closed vocabulary.
  // js/people.js + js/signin.js  roleLabel():  t(`role.${role}`), profiles.js ROLES
  { re: /^role\.(admin|user|basic)$/, built: 'people.js / signin.js  t(`role.${role}`)' },
  // js/people.js + js/signin.js  activity table:  t(`people.col_${c}`), profiles.js ACTIVITY_COLUMNS
  { re: /^people\.col_(login|turn|thread|upload|feedback)$/,
    built: 'people.js / signin.js  t(`people.col_${c}`)' },
  // js/people.js  auditKind():  t(`people.audit_${kind}`), dots in the kind read as `_`
  // (plugin.approve -> audit_plugin_approve). EXPLICIT list of the kinds the server writes
  // with category="audit" (people_api, main.py, recovery.py, cli.py, backup_api.py,
  // plugins_api.py, agents_api.py, agents_service.py); a kind without a label shows raw.
  // device_bound is no longer written (4.0 removed device binding) but stays as a history
  // label: upgraded installs still have 3.x rows of that kind in their activity table.
  { re: new RegExp('^people\\.audit_(' + [
    'login_failed', 'owner_created', 'owner_reset', 'owner_password_reset',
    'profile_created', 'profile_updated', 'profile_deleted', 'profile_avatar_set', 'profile_avatar_removed',
    'pin_set', 'pin_cleared', 'pin_changed', 'password_set', 'password_changed',
    'signed_out', 'device_removed', 'device_bound', 'recovered', 'recovery_failed',
    'api_token_changed', 'support_bundle', 'plugin_approve',
    'agent_created', 'agent_changed', 'agent_deleted', 'agent_credentials', 'agent_secret',
    'agent_settings', 'agent_task_started', 'agent_task_sent', 'agent_task', 'agent_approval',
  ].join('|') + ')$'), built: 'people.js  auditKind()' },
  // js/storage-card.js  t(`storage.${r.key}`), key from areaRows()
  { re: /^storage\.(database|media|reactions|imagestudio|avatars|avatar_pool|files|voices|backups|trash|stray|rest)$/,
    built: 'storage-card.js  t(`storage.${r.key}`)' },
  // js/people.js  typeLabel() / typeHint():  t(`people.type_${role}`), role from TYPES (user | basic | admin)
  { re: /^people\.type_(admin|user|basic)(_hint)?$/, built: 'people.js  t(`people.type_${role}`)' },
  // js/people-perms.js  permissionsPanel():  t(`perms.${r.perm}`), perm from PERM_ORDER
  { re: /^perms\.(use_chat|use_voice|attach_files|use_lookups|manage_history|use_notifications|files_upload|files_read|use_images|voice_lab|create_characters|choose_backend|dispatch_agents|approve_agent_actions)$/,
    built: 'people-perms.js  t(`perms.${r.perm}`)' },
];

// A key counts as used only where it appears as a whole token in CODE (comments
// stripped, strings kept): `SOURCE.includes(key)` let a key live on through a
// comment that mentioned it, or as the prefix of a longer key.
// index.html is NOT run through the JS lexer here (an apostrophe in its text
// would open a pseudo string); only its HTML comments are blanked.
const USE_CODE = [readFileSync(join(STATIC, 'index.html'), 'utf8').replace(/<!--[\s\S]*?-->/g, ' ')]
  .concat(readdirSync(JS_DIR).filter((f) => f.endsWith('.js'))
    .map((f) => blankNonCode(readFileSync(join(JS_DIR, f), 'utf8'), { keepStrings: true, keepTemplates: true })))
  .join('\n');
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const usedInCode = (key) => new RegExp(`(?<![\\w.])${escapeRe(key)}(?![\\w])`).test(USE_CODE);

test('every key in en.json is used by the app', () => {
  const dead = flatten(EN).filter((key) => {
    if (usedInCode(key)) return false;
    return !DYNAMIC.some((d) => d.re.test(key));
  });
  assert.deepEqual(dead, [],
    '\nThese keys are in en.json (and therefore in all eight locales) but no\n'
    + 'code references them. Delete them, or — if they are built at runtime —\n'
    + 'add the expression that builds them to DYNAMIC in this file:\n'
    + dead.map((k) => `  ${k}`).join('\n'));
});

test('every key the code asks for exists in en.json', () => {
  const have = new Set(flatten(EN));
  // Namespaces count as "known" for prefix-style lookups (has('nim') etc.).
  for (const k of [...have]) {
    const parts = k.split('.');
    for (let i = 1; i < parts.length; i++) have.add(parts.slice(0, i).join('.'));
  }
  const missing = new Set();
  // t('a.b') / T('a.b') / has('a.b') / data-i18n="a.b" / data-i18n-html / attr specs.
  const patterns = [
    /\b[tT]\(\s*'([a-z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+)'/g,
    /\bhas\(\s*'([a-z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+)'/g,
    /data-i18n(?:-html)?="([a-z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+)"/g,
    /data-i18n-attr="([^"]+)"/g,
  ];
  for (const [i, re] of patterns.entries()) {
    for (const m of CODE.matchAll(re)) {
      const found = i === 3
        ? m[1].split(/[;,]/).map((p) => p.split('=')[1]).filter(Boolean).map((s) => s.trim())
        : [m[1]];
      // A key that ends in `_` is a PREFIX being concatenated at runtime
      // (t('harness.state_' + svc)); the DYNAMIC table above owns those.
      for (const key of found) {
        if (key.endsWith('_') || key.endsWith('.')) continue;
        if (!have.has(key)) missing.add(key);
      }
    }
  }
  assert.deepEqual([...missing].sort(), [],
    '\nThese keys are asked for in code but are not in en.json. t() does not\n'
    + 'throw on a missing key — it logs once and renders a humanised guess — so\n'
    + 'nothing else would tell you:\n' + [...missing].sort().map((k) => `  ${k}`).join('\n'));
});

// index.html resolves the language and direction in a no-FOUC script, BEFORE
// any module loads, and js/i18n.js re-derives exactly the same thing at
// module-eval time. Two copies of one list: if they drift, the app paints one
// frame in the wrong language or the wrong direction and then flips.
test('the no-FOUC language lists match js/i18n.js', () => {
  const html = readFileSync(join(STATIC, 'index.html'), 'utf8');
  const i18n = readFileSync(join(JS_DIR, 'i18n.js'), 'utf8');

  const htmlSupported = /var SUPPORTED = \[([^\]]*)\]/.exec(html);
  const htmlRtl = /var RTL = \[([^\]]*)\]/.exec(html);
  assert.ok(htmlSupported && htmlRtl, 'the no-FOUC direction script changed shape');
  const list = (s) => [...s.matchAll(/'([a-z-]+)'/g)].map((m) => m[1]).sort();

  // i18n.js derives SUPPORTED from its LANGUAGES table.
  const langs = /const LANGUAGES = \[([\s\S]*?)\];/.exec(i18n);
  assert.ok(langs, 'could not find LANGUAGES in i18n.js');
  const codes = [...langs[1].matchAll(/code:\s*'([a-z-]+)'/g)].map((m) => m[1]).sort();
  assert.deepEqual(list(htmlSupported[1]), codes,
    'index.html SUPPORTED and i18n.js LANGUAGES disagree');

  const rtl = /const RTL_LANGS = new Set\(\[([^\]]*)\]\)/.exec(i18n);
  assert.ok(rtl, 'could not find RTL_LANGS in i18n.js');
  assert.deepEqual(list(htmlRtl[1]), list(rtl[1]),
    'index.html RTL and i18n.js RTL_LANGS disagree');
});

// The About row shows a version number to every user, including
// Safe-Mode sessions that cannot reach /api/dashboard. It is therefore a
// constant in the frontend, and a constant drifts.
test('about.js APP_VERSION matches the backend', () => {
  const about = readFileSync(join(JS_DIR, 'about.js'), 'utf8');
  const front = /export const APP_VERSION = '([^']+)'/.exec(about);
  assert.ok(front, 'about.js no longer exports APP_VERSION');
  const init = readFileSync(join(HERE, '..', '..', 'backend', 'app', '__init__.py'), 'utf8');
  const back = /__version__\s*=\s*"([^"]+)"/.exec(init);
  assert.ok(back, 'backend/app/__init__.py no longer defines __version__');
  assert.equal(front[1], back[1],
    'frontend/static/js/about.js APP_VERSION and backend/app/__init__.py __version__ disagree');
});

test('the source URL is a single constant, used nowhere else by hand', () => {
  const about = readFileSync(join(JS_DIR, 'about.js'), 'utf8');
  assert.match(about, /export const SOURCE_URL = 'https:\/\/[^']+'/);
  const others = readdirSync(JS_DIR)
    .filter((f) => f.endsWith('.js') && f !== 'about.js')
    .filter((f) => /github\.com\/[A-Za-z]/.test(readFileSync(join(JS_DIR, f), 'utf8')));
  assert.deepEqual(others, [],
    `these files hard-code a repository URL instead of importing SOURCE_URL: ${others}`);
});
