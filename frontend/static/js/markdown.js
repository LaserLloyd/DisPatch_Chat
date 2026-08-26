// Markdown -> sanitized HTML, plus code highlighting and image handling.
// Relies on globals provided by vendored scripts: marked and DOMPurify load
// with the document; hljs is fetched on demand (see ensureHighlighter).
import { loadScript, loadStyle, escapeHtml } from './util.js?v=10';
// markdown.js builds HTML as STRINGS rather than DOM nodes, so the two
// user-facing attributes below can't be reached by the data-i18n pass — they are
// translated inline instead. i18n.js imports nothing, so there is no cycle.
import { t } from './i18n.js?v=3';
//
// The rendering rules here are a port of the OpenClaw Control UI's markdown
// module (openclaw 2026.7.1, `dist/control-ui/assets/markdown-*.js`) so the same
// agent text looks the same in DisPatch as it does in the gateway's own chat:
// code blocks get a header + copy button, JSON collapses behind a <details>,
// raw HTML from the model is escaped rather than executed, block art keeps its
// alignment, and oversized text degrades to plain text instead of choking the
// parser. Verified against the live Control UI renderer: on a real 29-message
// session, 26/29 messages already matched tag-for-tag; these rules close the
// remaining three (file links, code-block chrome, raw-HTML escaping).
//
// Two deliberate divergences from upstream, both because DisPatch is not the
// Control UI:
//   - images: upstream only inlines data: URIs and drops the rest. DisPatch's
//     whole media pipeline ([[media:…]], /media/<uuid>) depends on real images,
//     so they stay.
//   - file links: upstream opens a side panel DisPatch doesn't have, so the
//     link markup is kept (for visual parity) but clicking copies the path.

// --- DOMPurify hooks: open links in a new tab safely, and keep data: URIs to
// real media. Registered LAZILY (from sanitize(), once) rather than at module
// eval: purify.min.js is a `defer` script, so a module that evaluates before it
// lands would silently skip registration and ship an unhooked sanitizer.
let _hooksReady = false;
function ensureHooks() {
  if (_hooksReady || !window.DOMPurify) return;
  _hooksReady = true;
  // data: is allowed on img/video/source via ADD_DATA_URI_TAGS so inline
  // pictures work. That switch is per-TAG, not per-TYPE: `data:text/html,…` in
  // an <img src> (or the data-full we copy it into) is still a document, and a
  // document is not a picture. Reject anything that isn't image/video/audio.
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    for (const attr of ['src', 'href', 'poster', 'data-full']) {
      const v = node.getAttribute && node.getAttribute(attr);
      if (!v) continue;
      if (/^\s*data:/i.test(v) && !/^\s*data:(?:image|video|audio)\//i.test(v)) {
        node.removeAttribute(attr);
      }
    }
  });
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      // A file link is not navigable — it carries a path, not an href.
      // Attribute order/value mirrors the Control UI so the two are diffable.
      if (!node.hasAttribute('data-file-path')) {
        node.setAttribute('rel', 'noreferrer noopener');
        node.setAttribute('target', '_blank');
      }
    }
    if (node.tagName === 'IMG') {
      node.setAttribute('loading', 'lazy');
    }
    // Task-list classes (upstream's markdown-it-task-lists convention): tag the
    // item and its list off the rendered checkbox, so a checklist matches the
    // Control UI without reimplementing marked's list rendering.
    if (node.tagName === 'INPUT' && node.classList.contains('task-list-item-checkbox')) {
      const li = node.closest('li');
      if (li) {
        li.classList.add('task-list-item');
        const list = li.parentElement;
        if (list && (list.tagName === 'UL' || list.tagName === 'OL')) {
          list.classList.add('contains-task-list');
        }
      }
    }
  });
}

// marked v18: configure GFM + line breaks — matches the Control UI's
// markdown-it options ({ html: true, breaks: true, linkify: true }); `html` is
// emulated by escaping raw HTML in the renderer overrides below, exactly as
// upstream does with its html_block/html_inline rules.
// `typeof window` rather than a bare `window.marked`: this module is otherwise
// pure at eval time, and the guard is what lets the test suite import it under
// plain node (no DOM shim) to exercise toPlainPreview() below.
if (typeof window !== 'undefined' && window.marked) {
  marked.setOptions({ gfm: true, breaks: true });
}

// Upstream limits (markdown-*.js): parse ceiling, plain-text fallback ceiling.
const MAX_RENDER_CHARS = 140000;   // hard truncate above this
const PLAIN_TEXT_CHARS = 40000;    // above this: don't parse markdown at all

// escapeHtml lives in util.js (one definition, imported above) — it used to be
// duplicated here with an identical body, which is one edit away from two
// different escapers. Re-exported so this module's public surface is unchanged.
export { escapeHtml };

/** Markdown -> a one-line plain-text summary, for the thread-list preview.
 *
 *  The preview is a TEXT node (thread rows never render markup), so pasting the
 *  raw source in showed the reader the syntax instead of the message: rows read
 *  "```python", "**Done** - [x] deploy", "![](/media/…)". This is a small
 *  reducer, not a parser — it strips the markers a preview would otherwise
 *  show and keeps the words, in the source order they appear.
 *
 *  Deliberately NOT `marked.parse()` + textContent: that pulls the whole parser
 *  (and a DOM) onto the thread-list paint path, which runs on every WS frame.
 */
export function toPlainPreview(md) {
  let s = String(md == null ? '' : md);
  // Fenced code: keep the code, drop the fence lines (and the language tag).
  s = s.replace(/^[ \t]*(?:```|~~~)[^\n]*$/gm, ' ');
  // Our own directives: a document shows its name, a picture shows nothing.
  s = s.replace(/\[\[doc:[^\]|]*\|([^\]]*)\]\]/g, '$1');
  s = s.replace(/\[\[(?:doc|media|image):[^\]]*\]\]/g, ' ');
  // Images before links — ![alt](url) would otherwise leave a stray "!".
  s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');
  s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
  // Reference-style links and bare autolinks.
  s = s.replace(/\[([^\]]*)\]\[[^\]]*\]/g, '$1');
  s = s.replace(/<((?:https?|mailto):[^>\s]+)>/g, '$1');
  // Block markers at the start of a line: heading, quote, list bullet,
  // ordered-list number, task-list checkbox, horizontal rule.
  s = s.replace(/^[ \t]*#{1,6}[ \t]+/gm, '');
  s = s.replace(/^[ \t]*>[ \t]?/gm, '');
  s = s.replace(/^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?/gm, '');
  s = s.replace(/^[ \t]*(?:[-*_][ \t]*){3,}$/gm, ' ');
  // Table pipes and the |---|---| separator row.
  s = s.replace(/^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$/gm, ' ');
  s = s.replace(/[ \t]*\|[ \t]*/g, ' ');
  // Inline: code spans, bold/italic/strike. Emphasis runs are unwrapped rather
  // than deleted wholesale so "**Done**" reads "Done", not "".
  s = s.replace(/`+([^`]*)`+/g, '$1');
  s = s.replace(/\*\*\*(\S(?:[\s\S]*?\S)?)\*\*\*/g, '$1');
  s = s.replace(/\*\*(\S(?:[\s\S]*?\S)?)\*\*/g, '$1');
  s = s.replace(/\*(\S(?:[\s\S]*?\S)?)\*/g, '$1');
  // Underscore emphasis only at word boundaries — markdown itself does not
  // emphasise intraword underscores, and eating them turns snake_case_name
  // into snakecasename in the preview.
  s = s.replace(/(^|[^\w`])(___|__|_)(\S(?:[\s\S]*?\S)?)\2(?!\w)/g, '$1$3');
  s = s.replace(/~~(\S(?:[\s\S]*?\S)?)~~/g, '$1');
  // Escaped punctuation: \* was never meant to be seen.
  s = s.replace(/\\([\\`*_{}\[\]()#+\-.!>~|])/g, '$1');
  return s.replace(/\s+/g, ' ').trim();
}

// Escape plain text into HTML with bare http(s) URLs turned into real links.
// User/system bubbles render as escaped plain text (no markdown), which made a
// pasted URL dead — the #1 ask was "let me click the preview link an agent (or
// I) dropped in chat". Tokenize BEFORE escaping so entity-encoding inside the
// URL (&) can't split the match; the href is attribute-escaped separately.
const BARE_URL_RE = /https?:\/\/[^\s<>"']+/g;
export function linkifyPlain(text) {
  const s = String(text ?? '');
  let out = '', last = 0, m;
  BARE_URL_RE.lastIndex = 0;
  while ((m = BARE_URL_RE.exec(s))) {
    let url = m[0];
    // Trailing punctuation belongs to the sentence, not the URL. A closing
    // paren is only trimmed when the URL itself has no opening one, so
    // wiki-style /Foo_(bar) links survive while "(see http://x)" doesn't
    // swallow the paren.
    let trimmed = url.replace(/[.,;:!?…]+$/, '');
    while (trimmed.endsWith(')') && !trimmed.includes('(')) trimmed = trimmed.slice(0, -1);
    out += escapeHtml(s.slice(last, m.index));
    out += `<a href="${trimmed.replace(/&/g, '&amp;').replace(/"/g, '&quot;')}" target="_blank" rel="noopener noreferrer">${escapeHtml(trimmed)}</a>`;
    out += escapeHtml(url.slice(trimmed.length));
    last = m.index + url.length;
  }
  out += escapeHtml(s.slice(last));
  return out;
}

// --- text-level cleanup (ported) -------------------------------------------
// Search-result citation markers: private-use delimiters (U+E200 open, U+E202
// separator, U+E201 close) the models emit around a citation.
const CITATION_LINE_RE = /[ \t]*cite(?:[^]*)?(?=\r?\n|$)/g;
const CITATION_RE = /cite(?:[^]*)?/g;
function stripCitations(text) {
  return text.replace(CITATION_LINE_RE, '').replace(CITATION_RE, '');
}

// Internal runtime scaffolding — a defense-in-depth mirror of the backend's
// openclaw_text sanitizer. The backend already strips this before storing new
// content, so this only fires for content stored RAW before that existed (e.g.
// the pre-mirror history of a native thread) or any path that reaches the
// browser unsanitized. Matches what the OpenClaw Control UI shows: the gateway
// strips these server-side, so its chat never renders them.
const RC_BEGIN = '<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>';
const RC_END = '<<<END_OPENCLAW_INTERNAL_CONTEXT>>>';
const RC_NOTICE = 'This context is runtime-generated, not user-authored. Keep internal details private.';
const LEGACY_HEADER = `OpenClaw runtime context (internal):\n${RC_NOTICE}\n\n`;
const LEGACY_EVENT = '[Internal task completion event]';
const SCAFFOLD_BLOCK_RE = /<\s*(system-reminder|previous_response)\b[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi;
const SCAFFOLD_TAG_RE = /<\s*\/?\s*(?:system-reminder|previous_response)\b[^>]*>/gi;

function standaloneLine(token) {
  return new RegExp(`(?:^|\\r?\\n)[ \\t]*${token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[ \\t]*(?=\\r?\\n|$)`, 'g');
}
function stripDelimitedBlock(text, begin, end) {
  const closed = new RegExp(`${standaloneLine(begin).source}[\\s\\S]*?${standaloneLine(end).source}`, 'g');
  const unmatched = new RegExp(`${standaloneLine(begin).source}[\\s\\S]*$`, 'g');
  return text.replace(closed, '').replace(unmatched, '').replace(standaloneLine(end), '');
}
function stripLegacyInternalContext(text) {
  let out = text;
  for (let guard = 0; guard < 50; guard++) {
    const h = out.indexOf(LEGACY_HEADER);
    if (h === -1) break;
    const evStart = h + LEGACY_HEADER.length;
    if (!out.startsWith(LEGACY_EVENT, evStart)) { out = out.slice(0, h) + out.slice(evStart); continue; }
    const para = out.indexOf('\n\n', evStart + LEGACY_EVENT.length);
    const end = para === -1 ? out.length : para;
    const before = out.slice(0, h).replace(/\s+$/, '');
    const after = out.slice(end).replace(/^\s+/, '');
    out = before && after ? `${before}\n\n${after}` : `${before}${after}`;
  }
  return out;
}
function stripInternalScaffolding(text) {
  if (!text) return text;
  let out = stripDelimitedBlock(text, RC_BEGIN, RC_END);
  out = stripLegacyInternalContext(out);
  out = out.replace(SCAFFOLD_BLOCK_RE, '').replace(SCAFFOLD_TAG_RE, '');
  return out;
}

// --- block art (ported) ----------------------------------------------------
// Half-block box drawing: proportional/markdown treatment would shred the
// alignment, so it's rendered verbatim in a <pre>.
const BLOCK_ART_LINE_RE = /^[\t  ▀▄█]+$/u;
const BLOCK_ART_CHAR_RE = /[▀▄█]/u;
export function isBlockArt(text) {
  const lines = text.replace(/\r\n?/g, '\n').split('\n').filter((l) => l.trim().length > 0);
  if (lines.length < 2) return false;
  let count = 0;
  for (const line of lines) {
    if (!BLOCK_ART_LINE_RE.test(line) || !BLOCK_ART_CHAR_RE.test(line)) return false;
    count += [...line].filter((ch) => BLOCK_ART_CHAR_RE.test(ch)).length;
  }
  return count >= 8;
}

// Normalize a media reference into a browser-loadable URL.
// Local absolute paths are served through the backend's validated /api/media.
export function normalizeMediaUrl(src) {
  if (!src) return src;
  if (/^(https?:|data:|blob:)/.test(src)) return src;
  if (src.startsWith('/media/') || src.startsWith('/static/') || src.startsWith('/api/media')) return src;
  if (src.startsWith('file://')) src = src.slice(7);
  if (src.startsWith('/')) return `/api/media?path=${encodeURIComponent(src)}`;
  return src;
}

// Video formats rendered as <video> (gifs stay <img> — they self-animate).
const VIDEO_RE = /\.(mp4|webm|mov|m4v|ogv|ogg|mkv|avi)(\?|#|$)/i;
export function isVideoUrl(url) {
  if (!url) return false;
  // /api/media?path=... — test the encoded path too.
  try {
    const u = String(url);
    if (VIDEO_RE.test(u)) return true;
    const m = u.match(/[?&]path=([^&]+)/);
    return m ? VIDEO_RE.test(decodeURIComponent(m[1])) : false;
  } catch { return false; }
}

// OpenClaw agents post media as [[media:/path|caption]]. Images become inline
// markdown images; videos become <video> elements (muted+loop = gif feel).
const attrEscape = (s) => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;');

// DisPatch generates its own HTML (video elements, doc cards) from directives.
// Because raw HTML in the SOURCE is now escaped (Control UI parity), that HTML
// can't be inlined before parsing — it is parked behind a placeholder token and
// substituted back into the parsed output. The token uses private-use
// codepoints so no model text can forge one, and any unclaimed token is scrubbed.
const PLACEHOLDER_OPEN = '';
const PLACEHOLDER_CLOSE = '';
const PLACEHOLDER_RE = /(\d+)/g;

// One pass over the parsed HTML, in order: every tag, and every placeholder
// that sits in the text stream between tags. Group 1 = the tag's leading '/',
// group 2 = its name, group 3 = a placeholder index. A token INSIDE a tag is
// swallowed by the tag branch, which is how restore() spots an attribute.
const TAG_OR_PLACEHOLDER_RE = /<(\/?)([a-zA-Z][^\s/>]*)[^>]*>|(\d+)/g;

function makeHtmlParker() {
  const parked = [];
  return {
    park(html) {
      parked.push(html);
      return `${PLACEHOLDER_OPEN}${parked.length - 1}${PLACEHOLDER_CLOSE}`;
    },
    // Restore after parse. A placeholder is parked at BLOCK level (wrapped in
    // blank lines), so it normally lands between <p>s and injecting raw HTML
    // there is exactly right.
    //
    // A blanket string replace is NOT safe on its own, though: if a token ever
    // lands inside a tag (an attribute value) or inside a <code>/<pre> body,
    // substituting unescaped HTML breaks out of that attribute and reparents
    // real elements — a <video> ended up as a child of the copy button, whose
    // autoplay then fetched the quoted path for real. DOMPurify runs after this
    // and strips scripts, so it was never stored XSS, but the DOM was corrupt.
    // The fence-regex fix removed the only known way to get there; this keeps
    // it structurally impossible rather than merely unreached. In those two
    // contexts the parked HTML is restored ESCAPED: visible, inert, and an
    // obvious symptom instead of silent corruption.
    restore(rendered) {
      if (!rendered) return rendered;
      const take = (i) => parked[Number(i)] ?? '';
      let out = '';
      let pos = 0;
      let codeDepth = 0;
      TAG_OR_PLACEHOLDER_RE.lastIndex = 0;
      for (let m; (m = TAG_OR_PLACEHOLDER_RE.exec(rendered)) !== null;) {
        out += rendered.slice(pos, m.index);
        pos = m.index + m[0].length;
        if (m[3] !== undefined) {              // a placeholder in the text stream
          out += codeDepth > 0 ? escapeHtml(take(m[3])) : take(m[3]);
          continue;
        }
        const tag = (m[2] || '').toLowerCase();
        if (tag === 'code' || tag === 'pre') codeDepth += m[1] ? -1 : 1;
        if (codeDepth < 0) codeDepth = 0;
        // A token inside the tag itself is inside an attribute value.
        out += m[0].replace(PLACEHOLDER_RE, (_, i) => escapeHtml(take(i)));
      }
      return out + rendered.slice(pos).replace(
        PLACEHOLDER_RE, (_, i) => (codeDepth > 0 ? escapeHtml(take(i)) : take(i)));
    },
  };
}

// Code spans and fenced blocks are QUOTED text: a `[[media:…]]` (or `[[doc:…]]`)
// inside one is being SHOWN, not sent — the same contract the backend keeps in
// openclaw_text.sub_outside_code() when it salvages media refs. Expanding here
// would paste an image into the middle of someone's quoted JSON/curl example
// (and, because the expansion inserts blank lines, it silently BREAKS an inline
// backtick span so the image actually renders — how an agent's inject-result echoes
// used to leak pictures into the gateway mirror thread). Skip code spans first.
// The fence alternation ends at its closing line OR at end of STRING. That
// second branch must be `(?![\s\S])` — a bare `$` under /m matches the end of
// ANY line, so the lazy body stops at the end of the FIRST content line and
// everything from line 2 down is treated as outside-code (quoted directives
// get expanded, and a parked placeholder lands inside the copy button's
// data-code attribute). The backend this mirrors uses `\Z` for exactly this
// reason; see openclaw_text.CODE_SPAN_RE.
const CODE_SPAN_RE = /(^[ \t]*(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^[ \t]*\2[ \t]*$|(?![\s\S])))|(`+)(?:(?!\3)[\s\S])*?\3/gm;

// Apply fn to the non-code segments of text; code spans/fences pass through
// verbatim. Mirrors backend/app/openclaw_text.py sub_outside_code().
function subOutsideCode(text, fn) {
  if (!text) return text;
  let out = '';
  let pos = 0;
  for (const m of text.matchAll(CODE_SPAN_RE)) {
    out += fn(text.slice(pos, m.index));
    out += m[0]; // quoted text stays untouched
    pos = m.index + m[0].length;
  }
  return out + fn(text.slice(pos));
}

function expandMediaDirectives(text, parker) {
  return subOutsideCode(text, (seg) => seg.replace(/\[\[media:([^\]|]+)(?:\|([^\]]*))?\]\]/g, (_, path, caption) => {
    const url = normalizeMediaUrl(path.trim());
    const cap = (caption || '').trim();
    if (isVideoUrl(url)) {
      return `\n\n${parker.park(`<video class="inline-video" src="${attrEscape(url)}" muted loop playsinline preload="metadata" title="${attrEscape(cap || 'video')}"></video>`)}\n\n`;
    }
    return `\n\n![${cap || 'image'}](${url})\n\n`;
  }));
}

// Doc icon map — kept in sync with main.js FILE_ICONS.
const _DOC_ICONS = [
  [/\.(zip|tar|gz|tgz|bz2|xz|7z|rar)$/i, '📦'],
  [/\.(pdf)$/i, '📕'],
  [/\.(txt|md|log|csv|json|ya?ml|xml)$/i, '📝'],
  [/\.(mp3|flac|wav|ogg|m4a|opus)$/i, '🎵'],
  [/\.(py|js|ts|sh|bash|zsh|c|cpp|h|hpp|rs|go|java|kt|swift|rb|php)$/i, '💻'],
  [/\.(html|css|scss|less)$/i, '🌐'],
  [/\.(toml|ini|cfg|conf)$/i, '⚙️'],
  [/\.(rst|tex|org|adoc)$/i, '📄'],
];
function _docIcon(name) {
  for (const [re, icon] of _DOC_ICONS) if (re.test(name)) return icon;
  return '📄';
}

function expandDocDirectives(text, parker) {
  return subOutsideCode(text, (seg) => seg.replace(/\[\[doc:([^\]|]+)(?:\|([^\]]*))?\]\]/g, (_, id, name) => {
    const fid = id.trim();
    const fname = (name || fid).trim();
    const icon = _docIcon(fname);
    const dlUrl = `/api/files/${attrEscape(fid)}/download`;
    const rawUrl = `/api/files/${attrEscape(fid)}/raw`;
    return `\n\n${parker.park(`<div class="doc-card"><span class="doc-icon">${icon}</span><a href="${dlUrl}" download="${attrEscape(fname)}" target="_blank" class="doc-link">${attrEscape(fname)}</a><a href="${rawUrl}" target="_blank" class="doc-preview-link" title="${attrEscape(t('msg.view_raw'))}">👁</a></div>`)}\n\n`;
  }));
}

// Strip media from the markdown SOURCE — for Safe Mode. Removing media before
// parsing matters: assigning innerHTML containing an <img> starts the network
// fetch immediately, even on a detached node, so post-render removal would
// still hit the server / disk cache.
const MEDIA_SRC_RE = /!\[[^\]]*\]\([^)]*\)|\[\[media:[^\]]*\]\]/g;
export function stripMediaSource(text) {
  return (text || '').replace(MEDIA_SRC_RE, '');
}

// --- file links (ported) ---------------------------------------------------
// Inline code that is recognisably a file path becomes a file link, matching
// the Control UI. Upstream's path grammar, condensed to the same shape.
const FL_SEG = '[A-Za-z0-9_.@#+-]+';
const FL_EXT = '[A-Za-z0-9]{1,8}';
const FL_LINE = ':\\d{1,6}(?::\\d{1,6})?';
const FL_FILE = `${FL_SEG}\\.${FL_EXT}`;
const FL_ROOTED = `(?:~\\/|\\.\\.\\/|\\.\\/|\\/)(?:${FL_SEG}\\/)*${FL_FILE}`;
const FL_RELATIVE = `${FL_SEG}(?:\\/${FL_SEG})*\\/${FL_FILE}`;
const FL_PATH_RE = new RegExp(`^(?:${FL_ROOTED}|${FL_RELATIVE})(?:${FL_LINE})?$`);
const FL_BARE_RE = new RegExp(`^${FL_SEG}\\.(${FL_EXT})${`(?:${FL_LINE})?`}$`);
const FL_LINE_SUFFIX_RE = /:(\d{1,6})(?::\d{1,6})?$/;
const FL_BARE_EXTS = new Set(('astro.bash.c.cc.cfg.cjs.conf.cpp.cs.css.diff.fish.go.h.hpp.htm.html.ini.java.js.json.jsonc.jsx.kt.kts.less.lock.log.markdown.md.mdx.mjs.patch.plist.proto.py.rb.rs.scss.sh.sql.svelte.svg.swift.toml.ts.tsx.txt.vue.xml.yaml.yml.zsh').split('.'));

function splitFileLine(value) {
  const m = FL_LINE_SUFFIX_RE.exec(value);
  return m ? { path: value.slice(0, m.index), line: Number.parseInt(m[1], 10) }
           : { path: value, line: null };
}
function isBareFilename(value) {
  if (value.includes('/') || value.includes('\\')) return false;
  const m = FL_BARE_RE.exec(value);
  return !!(m && m[1] && FL_BARE_EXTS.has(m[1].toLowerCase()));
}
export function parseFilePath(value) {
  // Not `t` — that name is the i18n import at module scope. Harmless here (this
  // function doesn't call it), but keeping zero bare-`t` locals makes the
  // shadow invariant absolute and the static guard simple.
  const trimmed = (value || '').trim();
  if (!FL_PATH_RE.test(trimmed) && !isBareFilename(trimmed)) return null;
  return splitFileLine(trimmed);
}

// --- code blocks (ported) --------------------------------------------------
// Upstream registers exactly these languages and auto-detects only among them,
// so an untagged fence can't be mis-detected as something exotic.
const HLJS_SUBSET = ['bash', 'cpp', 'css', 'diff', 'go', 'java', 'javascript',
  'json', 'markdown', 'python', 'rust', 'typescript', 'xml', 'yaml'];
const HLJS_ALIASES = { 'c++': 'cpp', cxx: 'cpp', js: 'javascript', jsx: 'javascript',
  md: 'markdown', sh: 'bash', shell: 'bash', ts: 'typescript', tsx: 'typescript' };

function highlightCode(code, lang) {
  const raw = (lang || '').trim().toLowerCase();
  const norm = HLJS_ALIASES[raw] || raw;
  try {
    if (norm && window.hljs && hljs.getLanguage(norm)) {
      return { html: hljs.highlight(code, { language: norm, ignoreIllegals: true }).value, lang: norm };
    }
    // No language tag: auto-detect within the subset, but only trust a
    // confident match (upstream's relevance >= 2 gate).
    if (!norm && code.trim() && window.hljs) {
      const subset = HLJS_SUBSET.filter((l) => hljs.getLanguage(l));
      const auto = hljs.highlightAuto(code, subset);
      if (auto.relevance >= 2) return { html: auto.value, lang: norm };
    }
  } catch { /* fall through to escaped */ }
  return { html: escapeHtml(code), lang: norm };
}

let _hljsPromise = null;
/** Fetch highlight.js the first time a code block needs it. Resolves to the
 *  library, or null if it could not be loaded — callers must treat highlighting
 *  as optional, exactly as they already did when it was a possibly-absent
 *  global. */
export function ensureHighlighter() {
  if (window.hljs) return Promise.resolve(window.hljs);
  if (!_hljsPromise) {
    _hljsPromise = Promise.all([
      loadStyle('/static/vendor/github-dark.min.css'),
      loadScript('/static/vendor/highlight.min.js'),
    ]).then(() => window.hljs || null).catch(() => null);
  }
  return _hljsPromise;
}

/** Re-highlight already-rendered blocks once the library lands.
 *
 *  Highlighting normally happens during markdown parsing. With hljs arriving
 *  later, the first paint is plain — correct, just uncoloured — and this walks
 *  the DOM to colour it in place rather than re-parsing the message. */
function rehighlight(container) {
  container.querySelectorAll('.code-block-wrapper pre > code').forEach((codeEl) => {
    if (codeEl.dataset.hl === '1' || codeEl.classList.contains('markdown-block-art')) return;
    codeEl.dataset.hl = '1';
    const lang = (codeEl.className.match(/language-([\w+#.-]+)/) || [])[1] || '';
    const { html } = highlightCode(codeEl.textContent || '', lang);
    if (html.includes('hljs-')) {
      codeEl.innerHTML = html;
      codeEl.classList.add('hljs');
    }
  });
}

// Block art is copied through a JSON-encoded attribute (upstream's
// `openclaw:block-art-code:` + data-code-encoding) because an HTML attribute
// normalises the runs of whitespace the art is made of.
const BLOCK_ART_CODE_PREFIX = 'openclaw:block-art-code:';
const BLOCK_ART_ENCODING = 'block-art-json';
export function decodeCopyPayload(value, encoding) {
  if (encoding !== BLOCK_ART_ENCODING || !value.startsWith(BLOCK_ART_CODE_PREFIX)) return value;
  try {
    const parsed = JSON.parse(value.slice(BLOCK_ART_CODE_PREFIX.length));
    return typeof parsed === 'string' ? parsed : value;
  } catch { return value; }
}

// Cheap shape check first so a large non-JSON block never reaches JSON.parse.
// Bounded because a message can carry a very big fence and this runs per block.
const JSON_SNIFF_MAX = 512 * 1024;
function isParseableJson(trimmed) {
  if (!trimmed || trimmed.length > JSON_SNIFF_MAX) return false;
  const first = trimmed[0];
  const last = trimmed[trimmed.length - 1];
  if (!((first === '{' && last === '}') || (first === '[' && last === ']'))) return false;
  try {
    const parsed = JSON.parse(trimmed);
    return parsed !== null && typeof parsed === 'object';
  } catch { return false; }
}

// `code` carries the fence's trailing newline (markdown-it keeps it, marked
// strips it) so the line count and copy payload match upstream exactly.
function renderCodeBlock(code, lang, { copyText } = {}) {
  const art = isBlockArt(code);
  const copy = copyText ?? (code.endsWith('\n') ? code.slice(0, -1) : code);
  const codeHtml = art
    ? `<pre><code class="markdown-block-art">${escapeHtml(code)}</code></pre>`
    : (() => {
        const { html } = highlightCode(code, lang);
        const cls = [html.includes('hljs-') ? 'hljs' : '', lang ? `language-${lang}` : '']
          .filter(Boolean).join(' ');
        return `<pre><code${cls ? ` class="${escapeHtml(cls)}"` : ''}>${html}</code></pre>`;
      })();
  const langLabel = lang ? `<span class="code-block-lang">${escapeHtml(lang)}</span>` : '';
  const dataCode = art ? BLOCK_ART_CODE_PREFIX + JSON.stringify(copy) : copy;
  const encAttr = art ? ` data-code-encoding="${BLOCK_ART_ENCODING}"` : '';
  const header = `<div class="code-block-header">${langLabel}` +
    `<button type="button" class="code-block-copy" data-code="${escapeHtml(dataCode)}"${encAttr} aria-label="Copy code">` +
    `<span class="code-block-copy__idle">${escapeHtml(t('msg.copy'))}</span>` +
    `<span class="code-block-copy__done">${escapeHtml(t('msg.copied'))}</span></button></div>`;
  const body = `<div class="code-block-wrapper">${header}${codeHtml}</div>`;
  // JSON collapses behind a <details> — upstream's rule, and the thing the operator
  // noticed wasn't collapsing: a lang-tagged json fence OR an untagged fence
  // whose content is a bare object/array.
  // NOT `t` — that is the module-level i18n function used for the copy button
  // labels a few lines up. Shadowing it here put those earlier calls in the
  // const temporal dead zone, so renderCodeBlock threw ReferenceError on EVERY
  // fenced block: code degraded to plain text and block-art broke the whole
  // message. (Regression from the initial release; caught in review.)
  // An UNTAGGED fence has to actually parse as JSON before it is treated as
  // JSON. Brace-matching alone is a shape test, and plenty of non-JSON has that
  // shape: a bash block that opens with `{`, a C/Rust/JS function body pasted
  // without its signature, array-ish command output. Those were being hidden
  // behind a collapsed widget labelled "JSON", so the reader saw a folded
  // container over code that is not JSON and never opens by habit. A
  // lang-tagged ```json fence still collapses on the tag alone — that is the
  // author saying what it is, and it should fold even when it is truncated or
  // malformed.
  const trimmed = code.trim();
  const looksJson = lang === 'json' || (!lang && isParseableJson(trimmed));
  if (looksJson) {
    const lines = code.split('\n').length;
    const label = lines > 1 ? `JSON &middot; ${lines} lines` : 'JSON';
    return `<details class="json-collapse"><summary>${label}</summary>${body}</details>`;
  }
  return body;
}

// A ```checklist fence renders as an interactive table, NOT as a code block.
// The inner text is a normal GFM table (plus an optional leading caption line),
// so it is parsed with the same marked pipeline and parked behind a marker
// class for checklist.js. Everything still passes through DOMPurify: the
// widget is generated HTML, and the table inside it is exactly what a plain
// markdown table would have produced. The checkbox column itself is injected
// by checklist.js AFTER sanitization, so it never needs to trust model text.
function renderChecklist(md) {
  let inner;
  try {
    inner = marked.parse(String(md || ''));
  } catch (e) {
    console.warn('[markdown] checklist parse failed:', e && e.message);
    inner = escapeHtml(String(md || ''));
  }
  return `<div class="checklist-widget">${inner}</div>`;
}

// --- marked renderer overrides (Control UI parity) -------------------------
let _rendererReady = false;
function ensureRenderer() {
  if (_rendererReady || !window.marked) return;
  _rendererReady = true;
  const renderer = {
    // Raw HTML from a model is shown, never executed — upstream escapes both
    // html_block and html_inline. Our own generated HTML never travels through
    // here: it is parked behind a placeholder and restored after parsing.
    html(token) { return escapeHtml(token.text ?? token.raw ?? ''); },
    code(token) {
      const text = token.text ?? '';
      const lang = (token.lang || '').trim().split(/\s+/)[0] || '';
      // A ```checklist fence is a table the reader interacts with, not quoted
      // code: parse its inner markdown (a GFM table) and hand the widget to
      // checklist.js, which injects the checkbox column and persistence.
      if (lang === 'checklist') return renderChecklist(text);
      // Re-attach the newline marked strips, so line counts + copy payload
      // match the Control UI byte for byte.
      return renderCodeBlock(text.endsWith('\n') ? text : `${text}\n`,
                             lang,
                             { copyText: text });
    },
    // Upstream's task-list plugin tags the checkbox and adds a trailing space
    // before the label; the `contains-task-list` / `task-list-item` classes on
    // the surrounding <ul>/<li> are added by a DOMPurify hook (below) so
    // marked's own list machinery — ordered lists, nesting, loose items — is
    // left untouched.
    checkbox(token) {
      return `<input class="task-list-item-checkbox"${token.checked ? ' checked=""' : ''} disabled="" type="checkbox"> `;
    },
    codespan(token) {
      const raw = token.text ?? '';
      const html = `<code>${escapeHtml(raw)}</code>`;
      // marked has already entity-escaped the token text; decode before path
      // matching or `&amp;` in a filename would never match the grammar.
      const decoded = raw.replace(/&amp;/g, '&').replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
      const f = parseFilePath(decoded);
      if (!f) return html;
      const line = f.line === null ? '' : ` data-file-line="${escapeHtml(String(f.line))}"`;
      return `<a class="markdown-file-link" data-file-path="${escapeHtml(f.path)}"${line}>${html}</a>`;
    },
    // Upstream renders <s>; marked defaults to <del>.
    del(token) { return `<s>${this.parser.parseInline(token.tokens)}</s>`; },
  };
  marked.use({ renderer });
}

function plainTextFallback(text) {
  return `<div class="markdown-plain-text-fallback">${escapeHtml(text.replace(/\r\n?/g, '\n'))}</div>`;
}

// Upstream's truncation notice, same wording.
function truncate(text) {
  if (text.length <= MAX_RENDER_CHARS) return text;
  const cut = text.slice(0, MAX_RENDER_CHARS);
  return `${cut}\n\n… truncated (${text.length} chars, showing first ${cut.length}).`;
}

export function renderMarkdown(text, { noMedia = false } = {}) {
  // Scrub placeholder codepoints from the SOURCE first: a model that emitted
  // them verbatim could otherwise forge a token and pull in parked HTML.
  const cleaned = stripInternalScaffolding(stripCitations(String(text || '')))
    .split(PLACEHOLDER_OPEN).join('').split(PLACEHOLDER_CLOSE).join('')
    .replace(/\r\n?/g, '\n');
  const src0 = noMedia ? stripMediaSource(cleaned) : cleaned;
  if (!src0.trim()) return '';
  if (isBlockArt(src0)) return sanitize(renderCodeBlock(src0, ''), noMedia);
  const src = truncate(src0);
  // Oversized text: skip the parser entirely rather than let it grind.
  if (src.length > PLAIN_TEXT_CHARS) return sanitize(plainTextFallback(src), noMedia);

  const parker = makeHtmlParker();
  const expanded = expandDocDirectives(expandMediaDirectives(src, parker), parker);
  if (!window.marked) return sanitize(plainTextFallback(expanded), noMedia);
  ensureRenderer();
  let rawHtml;
  try {
    rawHtml = marked.parse(expanded);
  } catch (e) {
    console.warn('[markdown] parse failed, falling back to plain text:', e && e.message);
    return sanitize(plainTextFallback(src), noMedia);
  }
  return sanitize(parker.restore(rawHtml), noMedia);
}

// Tag/attr allowlist mirrors the Control UI's DOMPurify config, plus the media
// tags DisPatch needs (upstream has no video/media pipeline) and the doc-card.
// h5/h6 were missing: DOMPurify kept the text but dropped the tag, so a deep
// heading rendered as a bare run of words glued to the next line.
const ALLOWED_TAGS = ('a b blockquote br button code del details div em h1 h2 h3 h4 h5 h6 hr i ' +
  'input li ol p pre s span strong summary table tbody td th thead tr ul img ' +
  'video source').split(' ');
const ALLOWED_ATTR = ['checked', 'class', 'disabled', 'href', 'rel', 'target', 'title',
  'start', 'src', 'alt', 'data-code', 'data-code-encoding', 'data-file-line',
  'data-file-path', 'type', 'aria-label', 'loading', 'controls', 'muted', 'loop',
  'playsinline', 'preload', 'poster', 'download', 'open',
  // marked emits `<td align="center">` for a `|:-:|` column; without it here
  // every table rendered left-aligned no matter what the author asked for.
  'align'];

// Attributes that are NOT URIs. This list is load-bearing, not decoration:
// DOMPurify validates every attribute that isn't URI-safe, data-* or aria-*
// against ALLOWED_URI_REGEXP, so our (deliberately strict) scheme allowlist was
// silently deleting ordinary values — type="checkbox", download="notes.txt",
// loading="lazy", preload="metadata", controls. Declaring them URI-safe skips
// that check while href/src/poster stay fully URI-validated.
const URI_SAFE_ATTR = ['type', 'start', 'controls', 'muted', 'loop', 'playsinline',
  'preload', 'loading', 'download', 'open', 'checked', 'disabled', 'rel', 'target',
  'aria-label', 'data-code-encoding', 'align'];

function sanitize(html, noMedia) {
  if (window.DOMPurify) {
    ensureHooks();
    return DOMPurify.sanitize(html, {
      ALLOWED_TAGS: noMedia
        ? ALLOWED_TAGS.filter((t) => !['img', 'video', 'source'].includes(t))
        : ALLOWED_TAGS,
      ALLOWED_ATTR,
      ADD_URI_SAFE_ATTR: URI_SAFE_ATTR,
      // data: is confined to media tags via ADD_DATA_URI_TAGS below, so it is
      // NOT in the general scheme allowlist — this drops `<a href="data:…">`
      // (a data:text/html navigation vector) while inline data: images/video pass.
      // `[/.#?]` used to accept a bare leading `/` — which also matched a
      // PROTOCOL-RELATIVE `//evil.example/beacon.gif`, i.e. an off-box request
      // from a chat message. `\/(?!\/)` keeps root-relative paths and drops the
      // scheme-inheriting form.
      ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel):|[.#?]|\/(?!\/))/i,
      ADD_DATA_URI_TAGS: ['img', 'video', 'source'],
    });
  }
  // Fail CLOSED: if the sanitizer somehow failed to load, never inject raw
  // model/tool/web HTML (stored-XSS sink) — render it as escaped text instead.
  return `<div class="markdown-plain-text-fallback">${escapeHtml(html)}</div>`;
}

// --- tables: sort by header, resize by dragging the header edge --------------
// Deliberately tiny and dependency-free (~90 lines): markdown tables are the
// thing agents use for comparisons, and "which one is cheapest" means sorting.
// Runs AFTER sanitization on the live DOM — like the .table-scroll wrapper
// above — so nothing here travels through DOMPurify, and a re-render simply
// rebuilds from the original markdown (sort/width state is per-paint, on purpose).
//
// Sort: tap/click a header cycles ascending → descending → original order.
// Numbers (with thousands separators, currency signs, %, units like "42 ms")
// sort numerically, everything else by locale; blanks always sink to the end.
// Resize: a handle on each header's trailing edge; pointer events so one code
// path covers mouse, pen and touch (touch-action:none on the handle keeps the
// page from scrolling instead). The first drag locks every column to its
// current width and switches the table to fixed layout, so dragging one edge
// moves only that edge.
const NUMERIC_RE = /^[-+]?[$€£¥]?\s*(\d{1,3}(?:[,\s]\d{3})+|\d+)?(\.\d+)?\s*(%|[a-zA-Z]{1,4})?$/;
// Exported so checklist.js reuses the ONE comparator (numbers with currency /
// units / separators sort numerically, blanks last) instead of drifting its own.
export function cellSortValue(text) {
  const s = (text || '').trim();
  if (!s) return { num: null, str: '' };
  if (NUMERIC_RE.test(s)) {
    const n = Number.parseFloat(s.replace(/[$€£¥,\s]/g, ''));
    if (Number.isFinite(n)) return { num: n, str: s };
  }
  return { num: null, str: s.toLowerCase() };
}
export function compareCells(a, b) {
  if (!a.str && !b.str) return 0;
  if (!a.str) return 1;   // blanks last regardless of direction…
  if (!b.str) return -1;
  if (a.num !== null && b.num !== null) return a.num - b.num;
  if (a.num !== null) return -1;   // …numbers before words
  if (b.num !== null) return 1;
  return a.str.localeCompare(b.str, undefined, { numeric: true, sensitivity: 'base' });
}

/** Sort a table's body rows by column `col`. dir: 'ascending' | 'descending' |
 *  null (restore the authored order). Exported for tests. */
export function sortTable(table, col, dir) {
  const tbody = table.tBodies[0];
  if (!tbody) return;
  const rows = Array.from(tbody.rows);
  rows.forEach((r, i) => { if (r.dataset.origIndex === undefined) r.dataset.origIndex = String(i); });
  let sorted;
  if (!dir) {
    sorted = rows.slice().sort((a, b) => Number(a.dataset.origIndex) - Number(b.dataset.origIndex));
  } else {
    const keyed = rows.map((r, i) => ({ r, i, v: cellSortValue(r.cells[col]?.textContent) }));
    const sign = dir === 'descending' ? -1 : 1;
    keyed.sort((a, b) => {
      // Blanks stay last in BOTH directions — flipping the sign only applies
      // to real values, so a descending sort never leads with empty cells.
      const blank = (!a.v.str ? 1 : 0) - (!b.v.str ? 1 : 0);
      if (blank) return blank;
      return sign * compareCells(a.v, b.v) || a.i - b.i;   // stable
    });
    sorted = keyed.map((k) => k.r);
  }
  sorted.forEach((r) => tbody.appendChild(r));
  Array.from(table.tHead?.rows[0]?.cells || []).forEach((th, i) => {
    if (dir && i === col) th.setAttribute('aria-sort', dir);
    else th.removeAttribute('aria-sort');
  });
}

const MIN_COL_PX = 40;
function lockColumnWidths(table) {
  if (table.dataset.fixed === '1') return;
  const ths = Array.from(table.tHead?.rows[0]?.cells || []);
  // Measure every column FIRST, then write: a write in the loop would re-layout
  // and shift the widths still waiting to be read.
  const widths = ths.map((th) => Math.max(MIN_COL_PX, Math.round(th.getBoundingClientRect().width)));
  ths.forEach((th, i) => { th.style.width = `${widths[i]}px`; });
  table.style.width = `${widths.reduce((a, b) => a + b, 0)}px`;
  table.style.tableLayout = 'fixed';
  table.dataset.fixed = '1';
}

function enhanceTable(table) {
  const head = table.tHead?.rows[0];
  if (!head || table.dataset.enhanced === '1') return;
  table.dataset.enhanced = '1';
  table.classList.add('md-table');
  Array.from(head.cells).forEach((th, col) => {
    th.classList.add('md-th');
    th.tabIndex = 0;
    th.setAttribute('role', 'columnheader');
    const sortNext = () => {
      const cur = th.getAttribute('aria-sort');
      sortTable(table, col, cur === 'ascending' ? 'descending' : cur === 'descending' ? null : 'ascending');
    };
    th.addEventListener('click', (e) => { if (!e.target.closest('.md-th-resize')) sortNext(); });
    th.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sortNext(); }
    });

    const grip = document.createElement('span');
    grip.className = 'md-th-resize';
    grip.setAttribute('aria-hidden', 'true');
    grip.addEventListener('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      e.preventDefault(); e.stopPropagation();
      lockColumnWidths(table);
      const startX = e.clientX;
      const startW = Number.parseFloat(th.style.width) || th.getBoundingClientRect().width;
      const startT = Number.parseFloat(table.style.width) || 0;
      const rtl = window.getComputedStyle(table).direction === 'rtl';
      table.classList.add('is-resizing');
      try { grip.setPointerCapture(e.pointerId); } catch { /* synthetic/unknown pointer id: document listeners below still track */ }
      const move = (ev) => {
        const dx = (ev.clientX - startX) * (rtl ? -1 : 1);
        const w = Math.max(MIN_COL_PX, Math.round(startW + dx));
        th.style.width = `${w}px`;
        table.style.width = `${startT - startW + w}px`;
      };
      const up = () => {
        table.classList.remove('is-resizing');
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
        document.removeEventListener('pointercancel', up);
      };
      // Document-level, not grip-level: with pointer capture the events reach
      // the grip anyway, and without it (a finger that wanders off a 12px
      // handle) the drag still tracks instead of sticking half-done.
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
      document.addEventListener('pointercancel', up);
    });
    // A click that ends a drag must not also sort.
    grip.addEventListener('click', (e) => e.stopPropagation());
    th.appendChild(grip);
  });
}

// Highlight code blocks and add copy buttons inside a rendered container.
export function enhanceContent(container) {
  if (!container) return;
  // Wrap markdown tables in a scroll container so a wide table scrolls sideways
  // instead of collapsing its columns (see app.css), then make them sortable +
  // resizable. Idempotent: skips tables already inside a .table-scroll.
  container.querySelectorAll('table').forEach((table) => {
    if (table.parentElement && table.parentElement.classList.contains('table-scroll')) return;
    const wrap = document.createElement('div');
    wrap.className = 'table-scroll';
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);
    // A checklist table is wired by checklist.js (checkbox column + a sort that
    // keeps completed rows pinned at the bottom). It still gets the shared
    // .md-table look; only the generic sort/resize wiring is skipped.
    if (table.closest('.checklist-widget')) {
      table.classList.add('md-table');
    } else {
      enhanceTable(table);
    }
  });
  // First code block on the page pulls highlight.js in; everything already
  // rendered gets coloured once it arrives.
  if (container.querySelector('.code-block-wrapper pre > code:not([data-hl])')) {
    ensureHighlighter().then((h) => { if (h) rehighlight(container); });
  }
  // Code blocks now arrive pre-highlighted with their own header/copy button
  // (Control UI parity). Any bare <pre> without our chrome — e.g. the plain-text
  // fallback path — still gets a copy affordance.
  container.querySelectorAll('pre').forEach((pre) => {
    if (pre.closest('.code-block-wrapper') || pre.querySelector('.copy-btn')) return;
    const block = pre.querySelector('code');
    if (!block || block.classList.contains('markdown-block-art')) return;
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.textContent = 'Copy';
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(block.innerText);
        btn.textContent = 'Copied'; btn.classList.add('copied');
        setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1400);
      } catch { /* clipboard unavailable */ }
    });
    pre.appendChild(btn);
  });
  // Agents habitually write `http://…` in backticks; that renders as an inert
  // <code> span, which reads as a link and clicks as nothing. Wrap any inline
  // code whose entire text is one http(s) URL in a real anchor. Built via DOM
  // APIs after sanitization, scheme-checked, so nothing unvetted reaches href.
  container.querySelectorAll('code').forEach((code) => {
    if (code.closest('pre, a') || code.dataset.linked) return;
    const txt = (code.textContent || '').trim();
    if (!/^https?:\/\/\S+$/.test(txt)) return;
    code.dataset.linked = '1';
    const a = document.createElement('a');
    a.href = txt;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'code-url-link';
    code.replaceWith(a);
    a.appendChild(code);
  });
  // Rewrite any inline markdown image whose src is a bare local path.
  container.querySelectorAll('img').forEach((img) => {
    const raw = img.getAttribute('src') || '';
    const norm = normalizeMediaUrl(raw);
    if (norm !== raw) img.setAttribute('src', norm);
    // EVERY thumbnail opens its full-resolution original — including the ones
    // markdown builds. Attachments got this from mediaThumbEl(); an image the
    // AGENT sent arrives as markdown instead and was landing here with no
    // data-full, so the delegated listener ignored it and clicking a generated
    // picture did nothing at all. Chat media is stored once, so the original
    // is the same URL — the affordance is what was missing, not the file.
    //
    // Nothing to gate on here: when media is not allowed (Safe Mode, NIM) the
    // sanitizer has already stripped <img> from the allowed tags, so an image
    // that exists at this point is one the viewer is permitted to open.
    if (!img.dataset.full && norm) img.dataset.full = norm;
  });
  // Inline videos: normalize src and give them the gif treatment (muted
  // autoplay loop). Sound/controls live in the lightbox (click to open).
  container.querySelectorAll('video').forEach((v) => {
    const raw = v.getAttribute('src') || '';
    const norm = normalizeMediaUrl(raw);
    if (norm !== raw) v.setAttribute('src', norm);
    v.muted = true; v.loop = true; v.playsInline = true;
    v.play().catch(() => {});
  });
}

// Delegated handlers for the ported chrome: code-block copy + file-path copy.
// Registered once, on document, so streamed/re-rendered content is covered
// without re-binding per message.
let _delegatedReady = false;
export function installMarkdownHandlers(onToast) {
  if (_delegatedReady) return;
  _delegatedReady = true;
  document.addEventListener('click', async (e) => {
    const copyBtn = e.target.closest?.('.code-block-copy');
    if (copyBtn) {
      try {
        const raw = copyBtn.dataset.code || '';
        const text = decodeCopyPayload(raw, copyBtn.dataset.codeEncoding);
        await navigator.clipboard.writeText(text);
        copyBtn.classList.add('copied');
        setTimeout(() => copyBtn.classList.remove('copied'), 1500);
      } catch { /* clipboard unavailable */ }
      return;
    }
    // Upstream opens a side panel here; DisPatch has none, so the useful
    // equivalent is handing the path over.
    const fileLink = e.target.closest?.('a[data-file-path]');
    if (fileLink) {
      e.preventDefault();
      const path = fileLink.dataset.filePath || '';
      const line = fileLink.dataset.fileLine;
      const full = line ? `${path}:${line}` : path;
      try {
        await navigator.clipboard.writeText(full);
        onToast?.(`Copied ${full}`);
      } catch { /* clipboard unavailable */ }
    }
  });
}
