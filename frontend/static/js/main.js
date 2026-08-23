// DisPatch Chat — frontend controller.
// One cohesive module: state, rendering, events, and WebSocket dispatch.
// Leaf modules (util/api/ws/markdown) hold no app state, so there are no cycles.

import { api, setOnLocked } from './api.js?v=18';
import { ChatSocket } from './ws.js?v=7';
import { renderMarkdown, enhanceContent, normalizeMediaUrl, isVideoUrl, installMarkdownHandlers, linkifyPlain, stripMediaSource, toPlainPreview } from './markdown.js?v=19';
import { el, escapeHtml, loadScript, loadStyle } from './util.js?v=10';
// The formatters come from i18n.js now, not util.js: they need the active
// locale (Intl) and translatable unit labels, which the old hand-rolled 'en-US'
// helpers could never provide. `fmtSize` was renamed `fileSize` on the way over.
import {
  t, tHtml, init as i18nInit, onChange as onI18nChange, languageSelect, localeLoadFailed, hasDictionary,
  relTime, clockTime, dayLabel, dayKey, fileSize,
  n as fmtNumber, percent as fmtPercent, list as fmtList,
} from './i18n.js?v=3';
import {
  initReactions, loadReactions, resetReactions, handleReactionFrame, handlePoolFrame,
  mountManager as mountReactionManager, closeManager as unmountReactionManager,
  managerOpen as reactionManagerOpen, repaintManager as repaintReactionManager,
  reactionMessageEl, botHasReactions,
} from './reactions.js?v=11';
import { mountDashboard, unmountDashboard, repaintDashboard } from './dashboard.js?v=5';
import {
  initLlmPanel, activateLlmPanel, closeLlmPanel, llmPanelOpen, repaintLlmPanel,
  firstRunCard,
} from './llm.js?v=2';
import { initPrivacy, privacyRow, allowsPersistentSession } from './privacy.js?v=3';
import { initNim, nimEnabled, setNim, canDisableNim, shouldDropMessage, nimRow } from './nim.js?v=3';
import { aboutRow } from './about.js?v=1';

// ===================== Popout mode =====================
// /?popout=1&thread=<id>&bot=<botId> boots straight into ONE conversation with
// all navigation chrome hidden — the target of the chat-header ⧉ button, so
// several conversations can run side by side in their own windows. The class
// goes on <body> before the first render so the rails never flash into view.
const POPOUT_PARAMS = new URLSearchParams(location.search);
const POPOUT = POPOUT_PARAMS.get('popout') === '1';
if (POPOUT) document.body.classList.add('popout');

// ===================== State =====================
const state = {
  bots: [],
  selectedBotId: null,
  threads: [],
  activeThreadId: null,
  activeThread: null,
  messages: [],
  thinking: {},        // thread_id -> bool
  threadBot: {},       // thread_id -> bot_id (for sidebar dots across bots)
  connected: false,
  attachments: [],     // [{url, name}]
  hasMoreOlder: false, // active thread has older pages to scroll back into
  streamingIds: new Set(), // message ids currently being streamed
  unread: {},          // thread_id -> ISO time of oldest unread bot message
  progress: {},        // thread_id -> live working items (thinking/tool calls)
  scrollPositions: {}, // thread_id -> last scrollTop when user navigated away
  auth: { pinSet: false, authenticated: false, decoy: false, lockTimeout: 600, minPin: 4, recoveryPath: '', configPath: '', rememberDays: 0, remembered: false, trustedDevices: 0 },
  decoy: false,        // Safe Mode (chat media hidden; safe bots' avatars shown)
  started: false,      // app has booted (bots loaded, socket connected)
  comfyEnabled: false, // server-side feature flag (LOCAL_CHAT_COMFY)
  comfy: { state: 'stopped', gatewayOn: false, flagsDirty: false },
  terminalEnabled: false, // server-side feature flag (LOCAL_CHAT_TERMINAL); full-session only
  terminal: { state: 'stopped', yolo: false, model: null, resume: 'none', pending: false },
  harnessEnabled: false, // server-side feature flag (DISPATCH_HARNESS); full-session only
  // DeepSeek Harness pane: last /api/harness/status payload (service + models)
  // and the headless-job ledger the server broadcasts.
  harness: { status: null, jobs: { running: false, current: null, history: [] } },
};

// The coding terminal is rendered as a pseudo-bot in the sidebar (unlocked
// mode only). Its id never collides with a real OpenClaw agent id.
const TERMINAL_ID = 'coding-terminal';
// Same for the DeepSeek Harness (dsh) pane.
const HARNESS_ID = 'deepseek-harness';
// Which bots appear in Safe Mode is a SERVER-side per-bot setting ("safe" in
// the Bot Manager, full mode only) — the server filters /api/bots and the WS
// hello for Safe-Mode sessions, so state.bots is already the right list.

// ===================== Unread tracking =====================
// An unread dot auto-dismisses once its triggering response is older than this:
// a pending dot that's been sitting for over a day has clearly been seen (or no
// longer matters), so it clears itself instead of lingering indefinitely.
const UNREAD_MAX_AGE_MS = 24 * 60 * 60 * 1000;   // unread >24h -> dot turns off

function isFreshUnread(iso) {
  if (!iso) return false;
  return (Date.now() - new Date(iso).getTime()) <= UNREAD_MAX_AGE_MS;
}

function unreadDotClass(iso) {
  return isFreshUnread(iso) ? ' unread' : '';
}

function syncUnreadFromThread(t) {
  if (!t || !t.id) return;
  if (t.unread_since && t.id !== state.activeThreadId) state.unread[t.id] = t.unread_since;
  else delete state.unread[t.id];
}

function oldestUnreadByBot() {
  const map = {};
  for (const [tid, since] of Object.entries(state.unread)) {
    // A stale unread (>24h) no longer lights its bot dot — but a fresher unread
    // on the SAME bot still does, so skip stale entries rather than the whole bot.
    if (!isFreshUnread(since)) continue;
    const b = state.threadBot[tid];
    if (!b) continue;
    if (!map[b] || since < map[b]) map[b] = since;
  }
  return map;
}

function markThreadRead(id) {
  if (!id) return;
  const had = !!state.unread[id];
  delete state.unread[id];
  api.markRead(id).catch(() => {});
  if (had) { renderSidebar(); renderThreads(); }
}

// Pull the cross-bot unread map (sidebar dots need threads we haven't loaded).
async function refreshUnread() {
  try {
    const r = await api.unread();
    state.unread = {};
    for (const u of (r.unread || [])) {
      if (u.thread_id === state.activeThreadId) continue;
      state.unread[u.thread_id] = u.unread_since;
      if (!state.threadBot[u.thread_id]) state.threadBot[u.thread_id] = u.bot_id;
    }
    renderSidebar(); renderThreads();
  } catch { /* non-critical */ }
}

const $ = (id) => document.getElementById(id);
const dom = {};
['app', 'bot-list', 'manage-bots', 'tl-avatar', 'tl-botname', 'tl-model', 'new-chat',
 'threads', 'back-btn', 'ch-avatar', 'ch-title', 'ch-sub', 'ch-model', 'popout-btn', 'thread-menu-btn',
 'thread-menu', 'messages', 'chat-empty', 'scroll-bottom', 'composer', 'input', 'send',
 'char-count', 'waiting', 'attach-btn', 'file-input', 'attach-preview', 'mobile-tabs',
 'retry-chip', 'retry-chip-btn',
 'botmanager-backdrop', 'bm-list', 'bm-close', 'bm-done', 'toast', 'reconnect',
 'collapse-threads', 'expand-threads', 'crop-backdrop', 'crop-img', 'crop-box',
 'crop-stage', 'crop-size', 'crop-save', 'crop-close', 'crop-title',
 'fs-chip', 'fileserver-backdrop', 'fs-list', 'fs-upload-btn', 'fs-close',
 'fs-file-input',
 // Settings tab container. The panes for Reactions and Health are empty mount
 // points; their modules build what goes inside.
 'settings-tabs', 'bm-footer', 'spane-bots', 'spane-reactions', 'spane-health',
 'spane-ai', 'spane-device', 'spane-security', 'sfoot-health', 'avatar-pool-panel',
 // Locked-side one-way drop
 'drop-btn', 'drop-backdrop', 'drop-close', 'drop-list', 'drop-input',
 'drop-more', 'drop-done',
 // Lock / security / decoy
 'lock-screen', 'lock-title', 'lock-sub', 'lock-dots', 'lock-error', 'keypad',
 'lock-submit', 'lock-forgot', 'lock-cancel', 'lock-recover', 'lock-recover-hint', 'recover-code',
 'recover-cancel', 'recover-submit', 'lock-now', 'bm-lock',
 'sec-current-wrap', 'sec-current', 'sec-new',
 'sec-new-label', 'sec-confirm', 'sec-error', 'sec-save', 'sec-remove',
 'sec-status-line',
 // The recovery note is rendered as ONE interpolated HTML string, so the two
 // <code> path elements inside it are created by the translation, not held here.
 'sec-note',
 'lock-remember-row', 'lock-remember', 'sec-remember-wrap', 'sec-remember-enable',
 'lock-nim-row', 'lock-nim',
 'sec-remember-label', 'sec-forget-devices',
 'bm-avatar-minimal', 'bm-avatar-style-row', 'bm-lang-row',
 // Safe-Mode companions panel (the gear when locked)
 'companions-backdrop', 'comp-list', 'comp-close', 'comp-more',
 // Redesign 2026: status, search, recovery, transcript bridge, a11y
 'conn-dot', 'sb-count', 'sr-live', 'sr-alert', 'search-btn',
 'search-backdrop', 'search-close', 'search-input', 'search-results',
 'open-recovery', 'recover-backdrop', 'recover-close', 'recover-all',
 'browse-sessions', 'recover-result', 'recover-done',
 'tx-backdrop', 'tx-close', 'tx-title', 'tx-summary', 'tx-filter',
 'tx-import', 'tx-list',
 // ComfyUI service panel
 'comfy-chip', 'comfy-dot', 'comfy-backdrop', 'comfy-close', 'comfy-dirty-banner',
 'comfy-status', 'comfy-start', 'comfy-restart', 'comfy-stop', 'comfy-gateway-toggle',
 'comfy-logs-btn', 'comfy-flags-list', 'comfy-flags-reset', 'comfy-flags-save',
 'comfy-flags-save-restart', 'comfy-logs-backdrop', 'comfy-logs-close',
 'comfy-logs-refresh', 'comfy-logs-content',
 'comfy-wf-hint', 'comfy-wf-import', 'comfy-wf-backup', 'comfy-wf-list', 'comfy-wf-file',
 // ComfyUI launch banner
 'comfy-launch', 'comfy-launch-glyph', 'comfy-launch-title', 'comfy-launch-secs',
 'comfy-launch-hint', 'comfy-launch-dismiss', 'comfy-launch-bar-fill',
 // Coding terminal
 'terminal-view', 'terminal-host', 'terminal-dot', 'terminal-status-label',
 'terminal-start', 'terminal-restart', 'terminal-stop',
 'terminal-yolo', 'terminal-model',
 'terminal-back', 'terminal-find-btn', 'terminal-find', 'terminal-find-input',
 'terminal-find-prev', 'terminal-find-next', 'terminal-find-close',
 'terminal-opts', 'terminal-opts-menu', 'terminal-yolo-val', 'terminal-model-val',
 'terminal-model-backdrop', 'terminal-model-close', 'terminal-model-hint',
 'terminal-model-list', 'terminal-model-input', 'terminal-model-clear', 'terminal-model-save',
 // DeepSeek Harness pane
 'harness-view', 'harness-back', 'harness-subtitle', 'harness-tab-ui', 'harness-tab-jobs',
 'harness-open', 'harness-pane-ui', 'harness-pane-jobs', 'harness-frame', 'harness-note',
 'harness-note-text', 'harness-note-hint', 'harness-job-form', 'harness-task', 'harness-cwd',
 'harness-run', 'harness-cancel', 'harness-jobs', 'harness-dot', 'harness-status-label',
 'harness-model', 'harness-start', 'harness-restart', 'harness-stop',
 // Locked-mode dedicated unlock button
 'unlock-btn',
].forEach((id) => { dom[id] = $(id); });

const botById = (id) => state.bots.find((b) => b.id === id);
const isMobile = () => window.matchMedia('(max-width: 768px)').matches;

// ===================== i18n helpers =====================
// One gigabyte figure, formatted by the locale (so 12.3 reads 12,3 in de/fr).
const gb = (bytes) => fmtNumber(bytes, { maximumFractionDigits: 1 });

/** Point a static node at a translation key AND write the text in one step.
 *
 *  Used wherever a node's key is STATE-dependent — Pin/Unpin, Transcript vs
 *  "<bot> — agent sessions", New PIN vs Create PIN. Writing bare text would work
 *  until the next language switch, when i18n's DOM pass re-renders the node from
 *  whichever key the markup shipped with and silently reverts the state. Moving
 *  the key instead keeps that pass correct forever.
 */
function setI18nText(node, key, vars) {
  if (!node) return;
  node.setAttribute('data-i18n', key);
  if (vars) node.setAttribute('data-i18n-vars', JSON.stringify(vars));
  else node.removeAttribute('data-i18n-vars');
  node.textContent = t(key, vars);
}

/** Same contract for the two strings that legitimately carry our own markup. */
function setI18nHtml(node, key, vars) {
  if (!node) return;
  node.setAttribute('data-i18n-html', key);
  if (vars) node.setAttribute('data-i18n-vars', JSON.stringify(vars));
  else node.removeAttribute('data-i18n-vars');
  // tHtml, not t: vars land in innerHTML here, so they are escaped by default
  // (see i18n.js). Call sites pass RAW values — escaping at the call site
  // would double-escape, and would be undone by applyDom's language-switch
  // repaint anyway.
  node.innerHTML = tHtml(key, vars);
}

// ===================== Avatars / safe-view helpers =====================
// state.bots is already mode-correct: the server filters the list down to
// safe-flagged bots for Safe-Mode sessions.
function visibleBots() {
  return state.bots;
}

function botLetter(bot) {
  const n = (bot && bot.name ? bot.name : '?').trim();
  return (n.charAt(0) || '?').toUpperCase();
}

// Deterministic hue per character so letter-block avatars read as distinct
// (each bot gets a stable colour from its id/name instead of one flat grey).
function letterAvatarHue(bot) {
  const s = String((bot && (bot.id || bot.name)) || '?');
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
}

// Colour a bot's NAME by its first letter (minimal-avatar mode). Mnemonic where a
// colour name exists (B→Blue, C→Cyan, F→Fuchsia, G→Green, R→Red, Y→Yellow…), a
// spread hue otherwise. Bots sharing a first letter share a hue by design ("go by
// first letter"). The returned hue feeds a light-dark() pair in CSS, so contrast
// holds in both themes (see app.css --name-hue). Non-A–Z names fall back to the
// id-hash hue so they still read as distinct.
const LETTER_HUE = {
  A: 25, B: 220, C: 190, D: 280, E: 150, F: 325, G: 130, H: 255,
  I: 245, J: 165, K: 50, L: 95, M: 305, N: 235, O: 35, P: 285,
  Q: 200, R: 0, S: 210, T: 175, U: 240, V: 270, W: 15, X: 315,
  Y: 55, Z: 110,
};
function nameHue(bot) {
  const L = botLetter(bot);
  return Object.prototype.hasOwnProperty.call(LETTER_HUE, L) ? LETTER_HUE[L] : letterAvatarHue(bot);
}
// A name <span> tinted by its first-letter hue (CSS reads --name-hue).
function nameSpan(cls, text, bot) {
  const s = el('span', { class: cls, text });
  s.style.setProperty('--name-hue', nameHue(bot));
  return s;
}
// A bot may pin an explicit avatar colour (config `color`, any CSS colour). When
// set it overrides the id-derived hue — used to pull a character clear of a
// neighbour it would otherwise clash with. Returns {base, dark, border}.
function avatarPalette(bot) {
  const custom = bot && typeof bot.color === 'string' ? bot.color.trim() : '';
  if (custom) {
    return {
      base: custom,
      dark: `color-mix(in srgb, ${custom}, #000 28%)`,
      border: `color-mix(in srgb, ${custom}, #fff 22%)`,
    };
  }
  const hue = letterAvatarHue(bot);
  return {
    base: `hsl(${hue} 55% 45%)`,
    dark: `hsl(${(hue + 38) % 360} 58% 32%)`,
    border: `hsl(${hue} 45% 55%)`,
  };
}
// Paint a letter-block <div> with its character colour. Inline styles override
// the flat .letter-avatar CSS default. Returns the node for chaining.
function tintLetterAvatar(node, bot) {
  const p = avatarPalette(bot);
  node.style.background = `linear-gradient(135deg, ${p.base}, ${p.dark})`;
  node.style.color = '#fff';
  node.style.borderColor = p.border;
  return node;
}
function letterAvatarNode(bot, baseClass) {
  return tintLetterAvatar(
    el('div', { class: `${baseClass} letter-avatar`, text: botLetter(bot) }), bot);
}

// An avatar node for a bot: the bot's avatar image, with a coloured letter-block
// fallback if there's no URL or the image fails to load. Works in Safe Mode too
// — the server only lists safe bots there and only serves THEIR avatar files, so
// rendering the picture leaks nothing the locked view doesn't already show.
/** The avatar for a THREAD row — the face the bot had when this chat started.
 *
 *  Avatars can rotate (this install swaps daily), so rendering every row
 *  against the CURRENT picture makes a month of conversations look identical.
 *  A thread carries `avatar_url` when the server captured a snapshot at
 *  creation; older threads have none and fall back to the live avatar, which
 *  is exactly what they showed before the feature existed.
 *
 *  NIM is honoured by delegating: no snapshot <img> is built when pictures are
 *  off, so nothing is fetched — same rule, one place.
 */
function threadAvatarNode(th, bot) {
  if (!th || !th.avatar_url || nimEnabled()) return avatarNode(bot, 'thread-avatar');
  // The thread's OWN historical avatar, full-resolution — not the bot's
  // current one. Clicking a chat from June must not open today's face.
  const img = el('img', {
    class: 'thread-avatar', src: th.avatar_url, alt: '', draggable: 'false',
    loading: 'lazy',
  });
  // Safe Mode is thumbnails-only, uniformly: the ?full=1 route 403s a decoy
  // session (even for safe bots), so don't offer a click that can only fail.
  if (!state.decoy) {
    setFullRes(img, `${th.avatar_url}${th.avatar_url.includes('?') ? '&' : '?'}full=1`);
  }
  // A snapshot can go missing (pruned, or the data dir moved). Fall back to
  // the live avatar rather than leaving a broken image in the list.
  img.addEventListener('error', () => {
    if (img.isConnected) img.replaceWith(avatarNode(bot, 'thread-avatar'));
  });
  return img;
}

// The face a thread WEARS, as opposed to the face its bot wears today.
//
// A conversation is a moment. Repainting it with the bot's current avatar
// rewrites that moment every time the picture changes — open a thread from
// June and it is wearing today's face, in the header and against every line
// the agent said. The thread list already pinned its snapshot; the chat did
// not, so entering a thread silently undid it.
//
// Returns null when there is no snapshot (or in No-Image Mode), which means
// "fall back to the live avatar" — the ordinary path for a new thread.
function threadFaceUrl(th) {
  if (!th || !th.avatar_url || nimEnabled()) return null;
  return th.avatar_url;
}

function threadFaceFullUrl(th) {
  const u = threadFaceUrl(th);
  return u ? `${u}${u.includes('?') ? '&' : '?'}full=1` : null;
}

function avatarNode(bot, baseClass, thread) {
  // A message avatar belongs to the conversation it is in, not to the roster.
  // `thread` is passed by the in-chat call sites; the left rail passes nothing
  // and keeps showing the bot's current face, which is what the rail is for.
  const pinned = thread ? threadFaceUrl(thread) : null;
  const url = pinned || (bot ? bot.avatar_url : '');
  // No avatar URL at all → letter block straight away (avoids an empty <img>
  // that fires a spurious error).
  if (!url) return letterAvatarNode(bot, baseClass);
  // No-Image Mode: return the letter block instead of an <img>. The CSS would
  // hide the picture either way, but building the <img> DOWNLOADS it — caught
  // by the end-to-end network assertion, which saw three avatar fetches on a
  // screen showing no avatars. Hiding a picture you already fetched is exactly
  // the dishonest implementation this feature is supposed to avoid, so the
  // element must never be created. Safe Mode is deliberately NOT included here:
  // it shows safe bots' avatars by design.
  if (nimEnabled()) return letterAvatarNode(bot, baseClass);
  const img = el('img', { class: baseClass, src: url, alt: bot ? bot.name : '', draggable: 'false' });
  // Every SHOWN avatar opens its full-res original; the left rail is the one
  // exception (those avatars ARE the button that opens the chat — navigation,
  // not subject). Gated on !state.decoy at the SOURCE, not per call site: the
  // full-res routes are PIN-gated so a Safe-Mode click could only 403, and the
  // capture-phase listener would swallow a thread-row click on the way. Two
  // call sites (typing indicator, thread-row error fallback) forgot the
  // per-site delete; one gate here can't be forgotten.
  if (bot && baseClass !== 'bot-avatar' && !state.decoy) {
    setFullRes(img, pinned ? threadFaceFullUrl(thread)
                           : `/api/bots/${encodeURIComponent(bot.id)}/avatar/full`);
  }
  // Fall back to a letter block only on a genuine load failure of a still-mounted
  // node. An aborted load from a re-render (node detached) must NOT downgrade —
  // the fresh render already created a new <img>, so acting here would be wrong.
  img.addEventListener('error', () => {
    if (img.isConnected) img.replaceWith(letterAvatarNode(bot, baseClass));
  });
  return img;
}

// Header avatars are fixed elements referenced by id. They render as an <img>
// (in BOTH full and Safe Mode now), with a letter <div> only as a no-URL
// fallback. The click-to-zoom handler is re-attached whenever we (re)create the
// <img> (replaceWith() discards the listener wired at startup), and is wired
// only when unlocked — the full-resolution route stays behind the PIN.
function headerAvatarClickBot(key) {
  return key === 'ch-avatar'
    ? (state.activeThread?.bot_id || state.selectedBotId)
    : state.selectedBotId;
}
function paintHeaderAvatar(key, bot, thread) {
  const cur = dom[key];
  if (!cur) return;
  const baseClass = cur.classList[0] || key;
  // `thread` is passed only by the CHAT header. The thread-list header sits
  // above every thread at once, so it keeps showing the bot's current face.
  const pinned = thread ? threadFaceUrl(thread) : null;
  // NIM takes the no-URL branch: a letter <div>, never an <img>. Same reason as
  // avatarNode — an <img> is a download, and CSS hiding it afterwards does not
  // un-send the request.
  const url = (bot && !nimEnabled()) ? (pinned || bot.avatar_url) : '';
  if (!url) {
    if (cur.tagName !== 'DIV') {
      const d = el('div', { id: cur.id, class: `${baseClass} letter-avatar` });
      cur.replaceWith(d);
      dom[key] = d;
    }
    dom[key].textContent = botLetter(bot);
    tintLetterAvatar(dom[key], bot);
  } else {
    if (cur.tagName !== 'IMG') {
      const img = el('img', { id: cur.id, class: baseClass, alt: bot ? bot.name : '' });
      cur.replaceWith(img);
      dom[key] = img;
    }
    // Click-to-zoom only when unlocked (the /full route is PIN-gated). Clear any
    // stale handler from a prior paint by cloning the listener-free state via a
    // guarded flag.
    if (!state.decoy) {
      // Re-pointed on EVERY paint, not latched behind a flag: the header is
      // reused across threads, so a one-shot wiring would leave the previous
      // thread's picture behind the click. The flag stays only so the
      // Safe-Mode branch below knows there is an affordance to strip.
      dom[key]._zoomWired = true;
      setFullRes(dom[key], pinned ? threadFaceFullUrl(thread)
        : `/api/bots/${encodeURIComponent(headerAvatarClickBot(key) || '')}/avatar/full`);
    } else if (state.decoy && dom[key]._zoomWired) {
      // Dropping to Safe Mode must REMOVE it, not just stop adding it.
      delete dom[key].dataset.full;   // Safe Mode: no full-res affordance
      dom[key]._zoomWired = null;
    }
    dom[key].src = url;
  }
}

// ===================== Toast / overlay =====================
let toastTimer;
function toast(msg, isError = false) {
  dom.toast.textContent = msg;
  dom.toast.className = 'toast' + (isError ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => dom.toast.classList.add('hidden'), 4000);
  // Toasts carry nearly every failure in the app (upload, delete, rename,
  // "not connected"). Route them to the live regions too, errors assertively,
  // or they are invisible to assistive tech.
  announce(msg, isError);
}

function setConnected(ok) {
  state.connected = ok;
  dom.reconnect.classList.toggle('hidden', ok);
  const dot = dom['conn-dot'];
  if (dot) {
    // Grey "offline" when the browser knows there's no network; amber pulse
    // while actively reconnecting on a live network.
    const offline = !ok && navigator.onLine === false;
    dot.classList.toggle('reconnecting', !ok && !offline);
    dot.classList.toggle('offline', offline);
    dot.title = t(ok ? 'conn.connected' : offline ? 'conn.offline' : 'conn.reconnecting');
  }
  if (state.started) reflectComposerState();
}

// Screen-reader announcements: new/streamed messages politely, errors assertively.
function announce(text, assertive = false) {
  const node = assertive ? dom['sr-alert'] : dom['sr-live'];
  if (!node || !text) return;
  // Toggling content forces AT to re-read even on identical text.
  node.textContent = '';
  setTimeout(() => { node.textContent = String(text).slice(0, 220); }, 30);
}

function announceMessage(msg) {
  if (!msg || msg.role === 'user' || (msg.metadata && msg.metadata.sub)) return;
  const bot = botById(state.threadBot[msg.thread_id]) || botById(state.activeThread?.bot_id);
  const who = (bot && bot.name) ? bot.name : t('common.assistant');
  const body = (msg.content || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  if (body) announce(t('msg.announce_reply', { name: who, text: body }));
}

// ===================== Mobile view switching =====================
function setView(view) {
  dom.app.dataset.view = view;
  dom['mobile-tabs'].querySelectorAll('.tab').forEach((t) => {
    const on = t.dataset.view === view;
    t.classList.toggle('active', on);
    if (on) t.setAttribute('aria-current', 'page'); else t.removeAttribute('aria-current');
  });
}

// History-aware navigation (mobile only). The bots screen is the root of the
// history stack, so the browser/hardware Back button walks chat → threads →
// bots instead of exiting the app. The stack always mirrors view depth:
// [bots, threads, chat][0..depth]. Desktop shows all panels — no history.
const VIEW_DEPTH = { bots: 0, threads: 1, chat: 2 };
const VIEW_AT_DEPTH = ['bots', 'threads', 'chat'];

function navigate(view) {
  if (!isMobile()) { setView(view); return; }
  const cur = VIEW_DEPTH[history.state?.view] ?? 0;
  const target = VIEW_DEPTH[view] ?? 0;
  if (target === cur) {
    history.replaceState({ view }, '');
    setView(view);
  } else if (target > cur) {
    // Push every intermediate level so Back never skips a screen
    // (e.g. the "Active" tab jumps bots → chat through threads).
    for (let d = cur + 1; d <= target; d++) {
      history.pushState({ view: VIEW_AT_DEPTH[d] }, '');
    }
    setView(view);
  } else {
    // Go shallower by popping real entries; the popstate handler renders.
    history.go(target - cur);
  }
}

window.addEventListener('popstate', (e) => {
  if (!isMobile()) return;
  setView(e.state?.view || 'bots');
});

// ===================== Sidebar =====================
let sidebarDragIdx = null;

async function persistSidebarOrder() {
  const payload = state.bots.map((b, i) => ({ id: b.id, order: i, visible: b.visible !== false }));
  try { await api.saveOrder(payload); }
  catch (e) { toast(e.message, true); }
}


/** Preserve scroll offset and keyboard focus across a full list repaint.
 *
 *  `wrap.innerHTML = ''` collapses the scroll height, which clamps scrollTop to
 *  0, and moves focus to <body>. Both are user-visible: the list jumps to the
 *  top and the keyboard user loses their place. Restoring is cheap next to
 *  making these renderers incremental, and it keeps one behaviour for the many
 *  call sites (heartbeat, unread sweep, and most WS frames).
 */
function repaintPreserving(wrap, focusSelector, paint) {
  const top = wrap.scrollTop;
  const active = document.activeElement;
  const keep = active && wrap.contains(active)
    ? active.closest(focusSelector)?.dataset.id : null;
  paint();
  if (top && wrap.scrollHeight > wrap.clientHeight) wrap.scrollTop = top;
  if (keep) {
    const again = wrap.querySelector(`${focusSelector}[data-id="${CSS.escape(keep)}"]`);
    if (again) again.focus({ preventScroll: true });
  }
}

function renderSidebar() {
  repaintPreserving(dom['bot-list'], '.bot-btn', () => renderSidebarInner());
}
function renderSidebarInner() {
  const list = dom['bot-list'];
  list.innerHTML = '';
  const thinkingBots = new Set(
    Object.entries(state.thinking).filter(([, v]) => v).map(([tid]) => state.threadBot[tid])
  );
  const unreadBots = oldestUnreadByBot();
  const bots = visibleBots();
  bots.forEach((bot, idx) => {
    const btn = el('button', {
      class: 'bot-btn' + (bot.id === state.selectedBotId ? ' active' : ''),
      // ONE dataset key: a duplicate literal key silently overwrites the
      // earlier one, which is how `id` went missing here for a while.
      //   id  — lets repaintPreserving restore keyboard focus across a repaint
      //   idx — the drag-reorder index
      dataset: { id: bot.id, idx },
      // aria-label only — a native title here doubles up with the styled
      // .bot-name-tip hover tip on desktop.
      'aria-label': bot.name,
      draggable: state.decoy ? 'false' : 'true',
      onclick: () => selectBot(bot.id),
    });
    btn.append(avatarNode(bot, 'bot-avatar'));
    btn.append(el('span', { class: 'bot-name-tip', text: bot.name }));
    btn.append(nameSpan('bot-name-label', bot.name, bot));
    const dotClass = thinkingBots.has(bot.id) ? ' thinking' : unreadDotClass(unreadBots[bot.id]);
    const dot = el('span', { class: 'bot-status-dot' + dotClass });
    btn.append(dot);

    // Drag to reorder (desktop, full view only). Persists via the order API.
    if (!state.decoy) {
      btn.addEventListener('dragstart', () => { sidebarDragIdx = idx; btn.classList.add('dragging'); });
      btn.addEventListener('dragend', () => {
        sidebarDragIdx = null;
        list.querySelectorAll('.bot-btn').forEach((b) => b.classList.remove('dragging', 'drop-above', 'drop-below'));
      });
      btn.addEventListener('dragover', (e) => {
        e.preventDefault();
        if (sidebarDragIdx === null || sidebarDragIdx === idx) return;
        const r = btn.getBoundingClientRect();
        const below = e.clientY > r.top + r.height / 2;
        btn.classList.toggle('drop-above', !below);
        btn.classList.toggle('drop-below', below);
      });
      btn.addEventListener('dragleave', () => btn.classList.remove('drop-above', 'drop-below'));
      btn.addEventListener('drop', (e) => {
        e.preventDefault();
        if (sidebarDragIdx === null || sidebarDragIdx === idx) return;
        const r = btn.getBoundingClientRect();
        let to = idx + (e.clientY > r.top + r.height / 2 ? 1 : 0);
        const [moved] = state.bots.splice(sidebarDragIdx, 1);
        if (sidebarDragIdx < to) to -= 1;
        state.bots.splice(Math.max(0, Math.min(to, state.bots.length)), 0, moved);
        sidebarDragIdx = null;
        renderSidebar();
        persistSidebarOrder();
      });
    }

    list.append(btn);
  });

  // Coding terminal pseudo-bot — unlocked mode only, and only when the
  // server exposes the feature. Never shown in Safe Mode (code execution).
  if (!state.decoy && state.terminalEnabled) {
    const tbtn = el('button', {
      class: 'bot-btn terminal-btn' + (state.selectedBotId === TERMINAL_ID ? ' active' : ''),
      'aria-label': t('terminal.sidebar_aria'),
      draggable: 'false',
      onclick: () => selectBot(TERMINAL_ID),
    });
    const rxName = t('terminal.name');
    tbtn.append(el('span', { class: 'bot-avatar terminal-avatar', text: '>_' }));
    tbtn.append(el('span', { class: 'bot-name-tip', text: rxName }));
    tbtn.append(nameSpan('bot-name-label', rxName, { name: rxName }));
    tbtn.append(el('span', { class: 'bot-status-dot terminal-sidedot ' + state.terminal.state }));
    list.append(tbtn);
  }
  // DeepSeek Harness pseudo-bot — same rules as the terminal (unlocked, feature on).
  if (!state.decoy && state.harnessEnabled) {
    const hbtn = el('button', {
      class: 'bot-btn terminal-btn harness-btn' + (state.selectedBotId === HARNESS_ID ? ' active' : ''),
      'aria-label': t('harness.sidebar_aria'),
      draggable: 'false',
      onclick: () => selectBot(HARNESS_ID),
    });
    const hName = t('harness.name');
    hbtn.append(el('span', { class: 'bot-avatar terminal-avatar harness-avatar', text: 'dsh' }));
    hbtn.append(el('span', { class: 'bot-name-tip', text: hName }));
    hbtn.append(nameSpan('bot-name-label', hName, { name: hName }));
    hbtn.append(el('span', { class: 'bot-status-dot terminal-sidedot harness-sidedot harness-' + harnessServiceState() }));
    list.append(hbtn);
  }
}

// ===================== Thread list =====================
function updateThreadListHeader() {
  const bot = botById(state.selectedBotId);
  if (!bot) return;
  paintHeaderAvatar('tl-avatar', bot);
  dom['tl-botname'].textContent = bot.name;
  dom['tl-model'].textContent = bot.model_hint || '';
}

function threadTitle(thread) {
  if (thread.title && thread.title.trim()) return thread.title;
  return t('threads.untitled');
}

function renderThreads() {
  repaintPreserving(dom['threads'], '.thread-item', () => renderThreadsInner());
}
function renderThreadsInner() {
  const wrap = dom['threads'];
  wrap.innerHTML = '';
  const bot = botById(state.selectedBotId);
  if (!bot) {
    wrap.append(el('div', { class: 'empty-list' }, [
      el('div', { class: 'empty-emoji', text: '🤖' }),
      el('p', { text: t('threads.empty_no_bot') }),
    ]));
    return;
  }
  if (!state.threads.length) {
    wrap.append(el('div', { class: 'empty-list' }, [
      el('div', { class: 'empty-emoji', text: '🗨️' }),
      el('p', { text: t('threads.empty_no_threads', { name: bot.name }) }),
      el('button', { class: 'btn-primary', text: t('threads.empty_cta'), onclick: newChat }),
    ]));
    return;
  }
  // `th`, not `t`: the translator is imported under that name and a thread
  // variable called `t` would shadow it for the whole loop body.
  for (const th of state.threads) {
    wrap.append(threadRowEl(th, bot));
  }
}

/** Build ONE thread row.
 *
 *  Extracted so a single changed thread can be repainted without rebuilding
 *  the list. Measured before this existed: one incoming message produced SIX
 *  full rebuilds (thinking x2, thread_update x3, message x1) — 107ms of
 *  blocking JS at 207 threads on a desktop, ~220ms on a 4x-slower tablet, plus
 *  a forced synchronous layout per repaint because repaintPreserving reads
 *  scrollTop right after innerHTML=''.
 *
 *  Deliberately the SAME function the full render uses. The obvious cheap fix
 *  is to poke the changed row's text nodes, but that hand-maintains row
 *  identity — active/pinned classes, the unread dot, the thinking preview, the
 *  aria-label, both listeners — in a second place that will drift from this
 *  one. Rebuilding a row through the identical code cannot drift, and is still
 *  O(1) against the list's O(n).
 */
function threadRowEl(th, bot) {
  {
    const thinking = !!state.thinking[th.id];
    // The row is a TEXT node, so the raw source would show the reader the
    // syntax instead of the message ("```python", "**Done**", "![](/media/…)").
    // toPlainPreview() reduces markdown to its words; it already collapses
    // whitespace, so only the length cap is left to apply here.
    const preview = thinking ? t('threads.preview_thinking')
      : (th.last_message ? (toPlainPreview(th.last_message).slice(0, 80) || t('threads.preview_empty'))
                         : t('threads.preview_empty'));
    const titleEl = el('div', { class: 'thread-title' });
    if (th.is_pinned) {
      titleEl.append(el('span', { class: 'thread-pin-icon', text: '📌' }));
    }
    // Own span so ONLY the text ellipsizes — a long title must not push the
    // unread dot outside the clipped title box.
    // dir="auto" — see the note on .ch-title in index.html. A thread title is
    // whatever the conversation opened with, so an English title in an Arabic
    // session must not be bidi-reordered (and the truncation ellipsis has to
    // land at the end of the TEXT, not at the end of the interface).
    titleEl.append(el('span', { class: 'thread-title-text', dir: 'auto', text: threadTitle(th) }));
    const unreadCls = unreadDotClass(state.unread[th.id]);
    if (unreadCls) titleEl.append(el('span', { class: 'thread-unread-dot' + unreadCls }));

    const row = el('div', {
      class: 'thread-item' + (th.id === state.activeThreadId ? ' active' : '') + (th.is_pinned ? ' pinned' : ''),
      dataset: { id: th.id },    // lets repaintPreserving restore keyboard focus
      tabindex: '0', role: 'button', 'aria-label': threadTitle(th),
      onclick: () => openThread(th.id),
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openThread(th.id); } },
    }, [
      threadAvatarNode(th, bot),
      el('div', { class: 'thread-main' }, [
        el('div', { class: 'thread-top' }, [
          titleEl,
          el('div', { class: 'thread-time', text: relTime(th.updated_at) }),
        ]),
        el('div', { class: 'thread-preview' + (thinking ? ' thinking' : ''), dir: 'auto', text: preview }),
      ]),
    ]);
    return row;
  }
}

/** Repaint just the row for `threadId`, or fall back to the whole list.
 *
 *  Falls back when the list SHAPE could have changed — the row is not on
 *  screen yet, or the new values would move it under sortThreads(). Getting
 *  that wrong is how pinned threads sink, which this file's own comments
 *  record as a real past bug, so the check is on position, not on a guess
 *  about which fields matter.
 *
 *  Returns true if it patched, false if it did a full render.
 */
function patchThreadRow(threadId) {
  const wrap = dom['threads'];
  const bot = botById(state.selectedBotId);
  if (!bot || !wrap) return false;

  const idx = state.threads.findIndex((x) => x.id === threadId);
  if (idx < 0) { renderThreads(); return false; }

  const existing = wrap.querySelector(`.thread-item[data-id="${CSS.escape(threadId)}"]`);
  // Position must be unchanged: same index in state AND same index in the DOM.
  const rows = wrap.querySelectorAll('.thread-item');
  if (!existing || rows.length !== state.threads.length || rows[idx] !== existing) {
    renderThreads();
    return false;
  }

  const hadFocus = document.activeElement === existing;
  const fresh = threadRowEl(state.threads[idx], bot);
  existing.replaceWith(fresh);
  if (hadFocus) fresh.focus();
  return true;
}

// Pinned-first, then most-recent — the SAME order the server uses
// (ORDER BY is_pinned DESC, updated_at DESC). Every client-side re-sort must
// go through here, or pinned threads sink on the next thread_update.
function sortThreads() {
  state.threads.sort((a, b) =>
    (b.is_pinned ? 1 : 0) - (a.is_pinned ? 1 : 0) ||
    (b.updated_at || '').localeCompare(a.updated_at || ''));
}

function upsertThread(t) {
  if (!t) return;
  state.threadBot[t.id] = t.bot_id;
  syncUnreadFromThread(t);
  if (t.bot_id !== state.selectedBotId) return;
  const idx = state.threads.findIndex((x) => x.id === t.id);
  if (idx >= 0) state.threads[idx] = { ...state.threads[idx], ...t };
  else state.threads.unshift(t);
  sortThreads();
}

function removeThread(id) {
  state.threads = state.threads.filter((x) => x.id !== id);
  delete state.thinking[id];
  delete state.threadBot[id];
  delete state.scrollPositions[id];
  delete state.unread[id];
  delete state.progress[id];
  renderSidebar();
  if (state.activeThreadId === id) clearChatView();
}

async function deleteMessage(msgId, threadId) {
  // Deleting a message is permanent with no undo — confirm, matching the
  // chat-delete pattern (was previously a silent one-tap loss).
  if (!await uiConfirm(t('msg.delete_confirm'), { danger: true, okText: t('common.delete') })) return;
  try {
    await api.deleteMessage(msgId);
    // WS broadcast handles DOM + state update
  } catch (e) { toast(e.message, true); }
}

// ===================== Chat view =====================
function clearChatView() {
  state.activeThreadId = null;
  state.activeThread = null;
  state.messages = [];
  state.hasMoreOlder = false;
  clearAttachments();
  dom['ch-title'].textContent = t('chat.select');
  dom['ch-sub'].textContent = '';
  dom['ch-model'].hidden = true;
  // No thread → the header's thread actions are dead weight; hide them.
  dom['popout-btn'].hidden = true;
  dom['thread-menu-btn'].hidden = true;
  // In Safe Mode the header avatar is a letter <div>; clear whichever it is.
  // The full-res pointer goes too — leaving it made the empty header open the
  // PREVIOUS thread's picture on a click.
  if (dom['ch-avatar'].tagName === 'IMG') dom['ch-avatar'].removeAttribute('src');
  else dom['ch-avatar'].textContent = '';
  delete dom['ch-avatar'].dataset.full;
  dom['ch-avatar']._zoomWired = null;
  dom['messages'].innerHTML = '';
  dom['messages'].append(el('div', { class: 'empty-state', id: 'chat-empty' },
    offerFirstRun()
      ? [firstRunCard(() => openLlm())]
      : [el('div', { class: 'empty-emoji', text: '💬' }),
         el('p', { text: t('chat.empty') })]));
  reflectComposerState();
}

// Should the empty chat area offer the "Connect an AI" card instead of the
// ordinary "pick a bot" prompt?
//
// Two states qualify, and both mean the same thing to the person looking at
// the screen: nothing here can answer you.
//   1. No bots at all — an emptied roster, nothing to even select.
//   2. Bots exist, but there is no agent CLI on the host AND no provider has
//      been connected — so every one of them is a thread that will never reply.
// Anything else (an agent backend is present, or a provider is already
// connected) is a working install and must not be nagged.
//
// Safe Mode never sees setup UI: the routes behind the card are full-session
// only, and a family tablet is not where a provider gets configured.
// `features` is absent for a limited session and unknown until /api/auth/status
// has answered — in both cases the honest answer is "don't offer", because
// guessing wrong here means showing a setup card to a working install.
function offerFirstRun() {
  if (state.decoy) return false;
  if (!state.bots.length) return true;
  const f = state.auth.features;
  if (!f || typeof f.agent === 'undefined') return false;
  return !f.agent && !(f.api_bots > 0);
}

function renderChatHeader() {
  const th = state.activeThread;
  if (!th) return;
  const bot = botById(th.bot_id) || botById(state.selectedBotId);
  if (bot) paintHeaderAvatar('ch-avatar', bot, th);
  dom['ch-title'].textContent = bot ? bot.name : t('common.chat');
  dom['ch-sub'].textContent = threadTitle(th);
  // Model badge: latest assistant message's actual model, else the bot's hint.
  const withModel = [...state.messages].reverse().find((m) => m.metadata && m.metadata.model);
  const model = (withModel && withModel.metadata.model) || (bot && bot.model_hint) || '';
  dom['ch-model'].textContent = model;
  dom['ch-model'].hidden = !model;
  // ⧉ only makes sense on a real open thread — and never inside a popout,
  // which would just spawn windows from windows.
  dom['popout-btn'].hidden = POPOUT || !th;
  dom['thread-menu-btn'].hidden = false;
  // A popout window titles itself after its conversation, so several of them
  // are tellable apart in the task bar / window switcher.
  if (POPOUT) document.title = `${threadTitle(th)} — ${bot ? bot.name : 'DisPatch Chat'}`;
}

// Images decode after layout; if the user was at the bottom, stay there.
let pinRaf = 0;
function pinOnImageLoad(scope) {
  scope.querySelectorAll('img').forEach((im) =>
    im.addEventListener('load', () => {
      // Coalesce every decode in this frame into ONE instant pin. The smooth
      // variant fought itself once several images landed together.
      if (pinRaf) return;
      pinRaf = requestAnimationFrame(() => {
        pinRaf = 0;
        if (isNearBottom(300)) scrollToBottom(true);
      });
    }, { once: true }));
}

// Build a clickable media element (image or video) for a normalized URL.
function mediaThumbEl(url) {
  // User attachments reach here as normalized URLs, NOT as markdown, so the
  // noMedia source-strip never sees them. Without this guard a NIM device
  // still downloads every pasted image. Returning null is what lets the
  // caller omit the node entirely rather than leave an empty box.
  if (nimEnabled()) return null;
  if (isVideoUrl(url)) {
    const v = el('video', { src: url, muted: '', loop: '', playsinline: '', preload: 'metadata' });
    v.muted = true;
    v.play().catch(() => {});
    v.addEventListener('click', () => openLightbox(url, { video: true }));
    return v;
  }
  // Chat media is stored once, so the thumbnail and the full view are the same
  // URL — the principle still holds, the "original" is simply itself.
  return setFullRes(el('img', { src: url, alt: t('msg.image_alt'), loading: 'lazy' }), url);
}

// User messages are plain text, but attachments travel inline as markdown
// images / [[media:...]] / [[doc:...]] directives. Pull those out so they
// render as real thumbnails / file cards instead of raw markdown source.
const USER_MEDIA_RE = /!\[[^\]]*\]\(([^)\s]+)\)|\[\[media:([^\]|]+)(?:\|[^\]]*)?\]\]/g;
const USER_DOC_RE = /\[\[doc:([^\]|]+)(?:\|([^\]]*))?\]\]/g;
function splitUserContent(content) {
  const media = [];
  const docs = [];
  let text = (content || '').replace(USER_MEDIA_RE, (_, mdUrl, dirPath) => {
    media.push(normalizeMediaUrl((mdUrl || dirPath || '').trim()));
    return '';
  });
  text = text.replace(USER_DOC_RE, (_, id, name) => {
    docs.push({ id: id.trim(), name: (name || id).trim() });
    return '';
  }).trim();
  return { text, media, docs };
}

// Themed in-app dialogs (replace native prompt()/confirm(), which look broken
// in an installed PWA). Return a Promise; Esc / backdrop / Cancel resolve to
// the negative result. Built on the native <dialog> for free focus-trap + a11y.
function uiDialog({ title, message, defaultValue, danger, okText, cancelText, prompt }) {
  return new Promise((resolve) => {
    const dlg = el('dialog', { class: 'ui-dialog' });
    const body = el('div', { class: 'ui-dialog-body' });
    if (title) body.append(el('h3', { class: 'ui-dialog-title', text: title }));
    if (message) body.append(el('p', { class: 'ui-dialog-msg', text: message }));
    let input = null;
    if (prompt) { input = el('input', { class: 'ui-dialog-input', type: 'text', value: defaultValue || '' }); body.append(input); }
    const cancel = el('button', { class: 'ui-dialog-btn', text: cancelText || t('common.cancel') });
    const ok = el('button', { class: 'ui-dialog-btn primary' + (danger ? ' danger' : ''), text: okText || t('common.ok') });
    body.append(el('div', { class: 'ui-dialog-foot' }, [cancel, ok]));
    dlg.append(body);
    document.body.append(dlg);
    let done = false;
    const finish = (val) => { if (done) return; done = true; try { dlg.close(); } catch { /* ignore */ } dlg.remove(); resolve(val); };
    const neg = () => finish(prompt ? null : false);
    cancel.addEventListener('click', neg);
    ok.addEventListener('click', () => finish(prompt ? (input ? input.value : '') : true));
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); neg(); });
    // Programmatic close (e.g. closeAllOverlays on lock) must still settle the
    // promise and remove the node; finish() is idempotent via `done`.
    dlg.addEventListener('close', () => neg());
    dlg.addEventListener('click', (e) => { if (e.target === dlg) neg(); });
    dlg.showModal();
    if (input) { input.focus(); input.select(); input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); finish(input.value); } }); }
    else ok.focus();
  });
}
function uiConfirm(message, opts = {}) { return uiDialog({ message, danger: opts.danger, okText: opts.okText || t('common.confirm'), cancelText: t('common.cancel'), title: opts.title }); }
function uiPrompt(title, defaultValue = '') { return uiDialog({ title, defaultValue, prompt: true, okText: t('common.save'), cancelText: t('common.cancel') }); }

function regenerateLast() {
  const tid = state.activeThreadId;
  if (tid && socket && socket.send) socket.send({ type: 'retry', thread_id: tid });
}
function editUserMessage(msg) {
  const inp = dom['input'];
  if (!inp) return;
  inp.value = msg.content || '';
  autosize(); updateSendEnabled(); inp.focus();
  try { inp.setSelectionRange(inp.value.length, inp.value.length); } catch { /* ignore */ }
}

// The ONE place that decides whether pictures render. Two reasons to hide
// them, one answer: Safe Mode (server-enforced, per-session) and No-Image Mode
// (device preference). Everything downstream — markdown's noMedia strip, the
// reaction trace thumb, media-only row dropping — asks this and nothing else,
// so the two features can never drift apart.
function mediaHidden() { return state.decoy || nimEnabled(); }

function messageEl(msg) {
  // NIM drops a picture-only message ENTIRELY: no bubble, no sender, no
  // timestamp. An empty bubble is still a visible trace of the image, which is
  // exactly what "omit completely" rules out. Returns null; renderMessages
  // skips it. Safe Mode deliberately does NOT do this — there the redaction is
  // the point and the gap is meant to be visible.
  if (nimEnabled() && shouldDropMessage(msg, stripMediaSource)) return null;
  const role = msg.role === 'user' ? 'user' : (msg.role === 'system' ? 'system' : 'assistant');
  const wrap = el('div', { class: `msg ${role}`, dataset: { id: msg.id } });

  if (role !== 'user') {
    const bot = botById(state.threadBot[msg.thread_id]) || botById(state.activeThread?.bot_id) || botById(state.selectedBotId);
    const av = avatarNode(bot, 'msg-avatar', state.activeThread);
    // avatarNode already set data-full; Safe Mode must not offer full-res.
    if (state.decoy) delete av.dataset.full;
    wrap.append(av);
  }

  const col = el('div', { class: 'msg-col' });
  // A reaction trace renders as a compact centred row that carries both the
  // collapsed chip and the embedded picture — a reaction lives in the chat,
  // pops up for its duration, then collapses back to the chip.
  const trace = reactionMessageEl(msg, { decoy: mediaHidden() });
  if (trace) return trace;
  const isSub = !!(msg.metadata && msg.metadata.sub);
  // Full-width assistant rows get a small name header; the model still shows in
  // the time line below. User / system / sub messages keep their compact look.
  if (role === 'assistant' && !isSub) {
    const hbot = botById(state.threadBot[msg.thread_id]) || botById(state.activeThread?.bot_id) || botById(state.selectedBotId);
    col.append(el('div', { class: 'msg-head' }, [
      nameSpan('msg-name', (hbot && hbot.name) ? hbot.name : t('common.assistant'), hbot),
    ]));
  }
  // dir="auto" on the bubble, not on the app: message text is CONTENT and picks
  // its own direction. In an Arabic session English prose was being reordered by
  // the bidi algorithm — trailing periods jumped to the front (".Server's up"),
  // "~1 minute" rendered "minute 1~" — because the paragraph inherited the RTL
  // base direction of the UI. dir="auto" maps to unicode-bidi: plaintext, so
  // EVERY bidi paragraph inside the bubble is resolved from its own first strong
  // character; one attribute covers markdown, streaming updates and quoted text
  // alike. Layout is untouched: the [dir="rtl"] rules all key off <html>.
  const bubble = el('div', { class: 'bubble' + (isSub ? ' sub' : ''), dir: 'auto' });
  let userMedia = [];
  // Safe Mode renders with noMedia: media is stripped from the markdown SOURCE
  // (so the browser never even requests it) and img/video tags are forbidden in
  // sanitization. The server already redacts decoy traffic — this is the
  // belt-and-suspenders layer, and it covers sub bubbles too.
  const mdOpts = { noMedia: mediaHidden() };
  if (isSub) {
    // Intermediate / working output: collapsed by default, click to expand.
    wrap.classList.add('sub-msg');
    const details = el('details', { class: 'sub-details' });
    details.append(el('summary', { text: t('msg.working') }));
    const inner = el('div', { class: 'sub-content' });
    inner.innerHTML = renderMarkdown(msg.content || '', mdOpts);
    enhanceContent(inner);
    details.append(inner);
    bubble.append(details);
  } else if (role === 'assistant') {
    bubble.innerHTML = renderMarkdown(msg.content || '', mdOpts);
    enhanceContent(bubble);
  } else {
    // User / system: plain text with preserved line breaks; attachments
    // extracted and rendered as thumbnails below the bubble.
    const { text, media, docs } = splitUserContent(msg.content);
    if (!state.decoy) userMedia = media;
    // Plain text with line breaks — but a pasted URL becomes a real link
    // (linkifyPlain escapes everything else exactly as escapeHtml did).
    bubble.innerHTML = linkifyPlain(text).replace(/\n/g, '<br>');
    if (!text && media.length && !state.decoy) bubble.classList.add('media-only');
    // Render document cards below the bubble.
    if (!state.decoy) {
      for (const doc of (docs || [])) {
        const name = doc.name || doc.id || t('common.file');
        const card = el('div', { class: 'doc-card' }, [
          el('span', { class: 'doc-icon', text: fileIcon(name) }),
          el('a', { class: 'doc-link', href: `/api/files/${doc.id}/download`, text: name, download: name, target: '_blank' }),
          el('a', { class: 'doc-preview-link', href: `/api/files/${doc.id}/raw`, text: '👁', target: '_blank', title: t('msg.view_raw') }),
        ]);
        col.append(card);
      }
    }
  }
  col.append(bubble);

  // No media thumbnails at all in safe view — or in NIM, where mediaThumbEl
  // returns null and the wrapper is skipped entirely rather than appended
  // empty (an empty .msg-media still reserves layout, which would show as a
  // gap exactly where the picture was).
  if (!mediaHidden()) {
    if (msg.media_url) userMedia.push(normalizeMediaUrl(msg.media_url));
    for (const url of userMedia) {
      if (!url) continue;
      const thumb = mediaThumbEl(url);
      if (!thumb) continue;
      const media = el('div', { class: 'msg-media' });
      media.append(thumb);
      col.append(media);
    }
  }

  const meta = msg.metadata || {};
  const timeText = clockTime(msg.created_at) + (meta.model ? ` · ${meta.model}` : '');
  col.append(el('div', { class: 'msg-time', text: timeText }));

  // Action row — revealed on hover (desktop), always tap-reachable (mobile).
  const actions = el('div', { class: 'msg-actions' });
  const actBtn = (label, title, fn) => {
    const b = el('button', { class: 'msg-act-btn', title });
    b.textContent = label;
    b.addEventListener('click', (e) => { e.stopPropagation(); fn(b); });
    return b;
  };
  if (role !== 'system') {
    actions.append(actBtn(t('msg.copy'), t('msg.copy_title'), async (b) => {
      try {
        await navigator.clipboard.writeText(msg.content || '');
        const o = b.textContent; b.textContent = t('msg.copied'); b.classList.add('ok');
        setTimeout(() => { b.textContent = o; b.classList.remove('ok'); }, 1200);
      } catch { /* clipboard unavailable */ }
    }));
  }
  // Safe Mode: Copy only. Regenerate/Delete are decoy-blocked server-side and
  // the "copy to composer" affordance would just advertise the lock — showing
  // dead buttons defeats the deniability model.
  if (!state.decoy) {
    if (role === 'assistant' && !isSub && state.messages[state.messages.length - 1]?.id === msg.id) {
      actions.append(actBtn(t('msg.regenerate'), t('msg.regenerate_title'), () => regenerateLast()));
    }
    if (role === 'user') {
      // Honest label: this only prefills the composer — sending creates a NEW
      // message, the original stays untouched.
      actions.append(actBtn(t('msg.to_composer'), t('msg.to_composer_title'), () => editUserMessage(msg)));
    }
    actions.append(actBtn(t('msg.delete'), t('msg.delete_title'), () => deleteMessage(msg.id, msg.thread_id)));
  }
  col.append(actions);

  wrap.append(col);

  // click-to-zoom for inline videos. Images are NOT wired here any more:
  // markdown gives every inline <img> a data-full, and the one delegated
  // lightbox listener owns that attribute. Wiring both opened TWO stacked
  // lightboxes per click — closing one revealed the other.
  bubble.querySelectorAll('video').forEach((v) =>
    v.addEventListener('click', () => openLightbox(v.getAttribute('src'), { video: true })));
  pinOnImageLoad(wrap);
  return wrap;
}

// Consecutive same-sender rows group: hide the repeated avatar/name + tighten
// the gap (iMessage/Slack feel). Only within ~5 min and the same calendar day.
function isGrouped(prev, msg) {
  if (!prev || !msg || prev.role !== msg.role) return false;
  if ((msg.metadata && msg.metadata.sub) || (prev.metadata && prev.metadata.sub)) return false;
  if (dayKey(prev.created_at) !== dayKey(msg.created_at)) return false;
  return Math.abs(new Date(msg.created_at) - new Date(prev.created_at)) <= 5 * 60 * 1000;
}

function prefillComposer(text) {
  const inp = dom['input'];
  if (!inp) return;
  inp.value = text;
  autosize(); updateSendEnabled(); inp.focus();
  try { inp.setSelectionRange(inp.value.length, inp.value.length); } catch { /* ignore */ }
}

// Shimmer placeholder rows while a thread's history loads (no blank flash).
function renderSkeleton() {
  const box = dom['messages'];
  box.innerHTML = '';
  const wrap = el('div', { class: 'skeleton-wrap' });
  const rows = [
    { me: false, w: ['62%', '40%'] }, { me: true, w: ['70%'] },
    { me: false, w: ['52%', '74%', '36%'] }, { me: true, w: ['48%'] },
  ];
  for (const r of rows) {
    wrap.append(el('div', { class: 'sk-row' + (r.me ? ' me' : '') }, [
      r.me ? null : el('div', { class: 'sk-av' }),
      el('div', { class: 'sk-lines' }, r.w.map((w) => {
        const l = el('div', { class: 'sk-line' }); l.style.width = w; return l;
      })),
    ].filter(Boolean)));
  }
  box.append(wrap);
}

function renderMessages(stick = true) {
  const box = dom['messages'];
  box.innerHTML = '';
  if (!state.messages.length) {
    const wbot = botById(state.activeThread?.bot_id) || botById(state.selectedBotId);
    const welcome = el('div', { class: 'empty-state welcome' }, [
      avatarNode(wbot, 'welcome-avatar'),
      el('h2', { class: 'welcome-title', text: wbot ? t('welcome.with_bot', { name: wbot.name }) : t('welcome.generic') }),
      el('p', { class: 'welcome-sub', text: wbot && wbot.model_hint ? `${wbot.emoji || ''} ${wbot.model_hint}`.trim() : t('welcome.say_hi') }),
    ]);
    // Suggestion chips prefill the composer (no auto-send — consistent with the
    // no-slash-command design); suppressed in Safe Mode.
    if (!state.decoy) {
      // welcome.chip_search keeps a TRAILING SPACE on purpose: the chip prefills
      // the composer and leaves the caret after it, ready for the query.
      const chips = [t('welcome.chip_help'), t('welcome.chip_summary'), t('welcome.chip_search')];
      welcome.append(el('div', { class: 'welcome-chips' }, chips.map((c) =>
        el('button', { class: 'welcome-chip', text: c.trim(), onclick: () => prefillComposer(c) }))));
    }
    box.append(welcome);
  } else {
    let lastDay = '';
    let prev = null;
    for (const msg of state.messages) {
      // Build the row BEFORE the date separator. In NIM a picture-only message
      // renders as null, and a day whose every message was a picture must not
      // leave a dangling "Tuesday" heading with nothing under it — so the
      // separator is only emitted once we know something will follow it.
      const node = messageEl(msg);
      // null = NIM dropped a picture-only row. Skip it WITHOUT touching `prev`,
      // so the dropped message can't break the grouping run either side of it —
      // two texts from the same sender with an image between them should still
      // read as one grouped run once the image is gone.
      if (!node) continue;
      const dk = dayKey(msg.created_at);
      if (dk && dk !== lastDay) {
        box.append(el('div', { class: 'date-sep', text: dayLabel(msg.created_at) }));
        lastDay = dk;
        prev = null;   // a date break ends a run, so the next row shows identity
      }
      if (isGrouped(prev, msg)) node.classList.add('grouped');
      box.append(node);
      prev = msg;
    }
  }
  if (state.thinking[state.activeThreadId]) box.append(typingEl());

  // Bug 1: Restore saved scroll position if available (and user wasn't at the
  // bottom), otherwise scroll to bottom if stick is true.
  const savedPos = state.scrollPositions[state.activeThreadId];
  if (savedPos !== undefined && !stick) {
    // Re-apply the saved offset until the layout settles — a single assignment
    // clamps to 0 while the rebuilt thread's height is still growing.
    settleScroll(() => savedPos);
  } else if (stick) {
    // Pin to the bottom until the thread's layout settles (see settleScrollBottom)
    // — a single jump lands short while images/code/fonts keep growing the height.
    settleScrollBottom();
  }
  // Clear consumed saved position so future renders (e.g. pagination) don't
  // re-apply a stale value.
  delete state.scrollPositions[state.activeThreadId];
}

function appendMessageToView(msg) {
  if (state.messages.some((m) => m.id === msg.id)) return;
  state.messages.push(msg);
  const box = dom['messages'];
  const emptyState = box.querySelector('.empty-state');
  if (emptyState) emptyState.remove();
  // Build the row first: in NIM a picture-only message renders as null, and
  // there is no point emitting a date separator for a row that will not exist.
  const node = messageEl(msg);
  if (!node) return;
  // Day separator if needed.
  const last = state.messages[state.messages.length - 2];
  const dk = dayKey(msg.created_at);
  const sameDay = last && dayKey(last.created_at) === dk;
  if (!sameDay) {
    box.insertBefore(el('div', { class: 'date-sep', text: dayLabel(msg.created_at) }), box.querySelector('.typing'));
  }
  // Group consecutive same-sender rows live too (matches the full re-render),
  // so a second reply doesn't show a redundant avatar/name until the next render.
  if (sameDay && isGrouped(last, msg)) node.classList.add('grouped');
  const typing = box.querySelector('.typing');
  if (typing) box.insertBefore(node, typing); else box.append(node);
  if (isNearBottom()) scrollToBottom();
  else { showScrollButton(true); if (msg.role !== 'user') bumpUnseen(); }
  if (msg.role !== 'user') announceMessage(msg);
}

// Whether the live "working" panel inside the typing bubble is expanded.
// Module-level so it survives typing-element re-renders during a turn.
let progressOpen = false;

function typingEl() {
  const bot = botById(state.activeThread?.bot_id) || botById(state.selectedBotId);
  const bubble = el('div', { class: 'bubble typing-bubble', title: t('progress.title') }, [
    el('span', { class: 'dots' }, [el('span'), el('span'), el('span')]),
    el('span', { class: 'typing-hint', text: t(progressOpen ? 'progress.hint_hide' : 'progress.hint_show') }),
  ]);
  const panel = el('div', { class: 'progress-panel' });
  if (!progressOpen) panel.hidden = true;
  bubble.addEventListener('click', () => {
    progressOpen = !progressOpen;
    panel.hidden = !progressOpen;
    bubble.querySelector('.typing-hint').textContent = t(progressOpen ? 'progress.hint_hide' : 'progress.hint_show');
    if (progressOpen) renderProgressPanel();
  });
  const wrap = el('div', { class: 'typing' }, [
    avatarNode(bot, 'msg-avatar', state.activeThread),
    el('div', { class: 'typing-col' }, [bubble, panel]),
  ]);
  if (progressOpen) setTimeout(renderProgressPanel, 0);
  return wrap;
}

// Live "what the model is doing" list inside the typing indicator.
function renderProgressPanel() {
  const panel = dom['messages'].querySelector('.progress-panel');
  if (!panel || panel.hidden) return;
  const items = state.progress[state.activeThreadId] || [];
  if (!items.length) {
    panel.replaceChildren(el('div', { class: 'progress-item idle', text: t('progress.idle') }));
    panel._painted = 0;
    return;
  }
  // Append-only: rebuilding all 200 rows per frame was the single biggest cost
  // on a tool-heavy turn. `_painted` tracks how many rows are already on screen;
  // a shrink (thread switch, reset) falls back to a full repaint.
  let from = panel._painted || 0;
  if (from > items.length || panel.querySelector('.progress-item.idle')) {
    panel.replaceChildren();
    from = 0;
  }
  if (from === items.length) return;
  const frag = document.createDocumentFragment();
  for (const it of items.slice(from)) {
    const icon = it.kind === 'tool' ? '🔧' : (it.kind === 'thinking' ? '🧠' : '💬');
    const label = it.kind === 'tool' ? `${it.name}  ${it.text}` : it.text;
    frag.append(el('div', { class: `progress-item ${it.kind}`, text: `${icon} ${label}` }));
  }
  panel.append(frag);
  panel._painted = items.length;
  panel.scrollTop = panel.scrollHeight;
}

function refreshTyping() {
  const box = dom['messages'];
  const existing = box.querySelector('.typing');
  // Never show the dots while a reply is actively streaming in.
  const want = !!state.thinking[state.activeThreadId] && !box.querySelector('.msg.streaming');
  if (want && !existing) { box.append(typingEl()); if (isNearBottom()) scrollToBottom(); }
  else if (!want && existing) existing.remove();
}

function appendErrorBubble(text, threadId) {
  const box = dom['messages'];
  const bubble = el('div', { class: 'bubble', dir: 'auto' });   // see renderMessage: content picks its own direction
  bubble.append(el('span', { text: `⚠ ${text}` }));

  // Retry button only if there's a last user message to replay
  const tid = threadId || state.activeThreadId;
  const retryBtn = el('button', { class: 'retry-btn', text: t('msg.retry') });
  retryBtn.addEventListener('click', () => {
    if (!tid) return;
    const ok = !!(socket && socket.send({ type: 'retry', thread_id: tid }));
    if (ok) {
      node.remove();
      state.thinking[tid] = true;
      reflectComposerState();
    }
  });
  bubble.append(retryBtn);

  const node = el('div', { class: 'msg error' }, [
    el('div', { class: 'msg-col' }, [bubble]),
  ]);
  box.append(node);
  scrollToBottom();
}

// ===================== Scroll handling =====================
function isNearBottom(px = 140) {
  const b = dom['messages'];
  return b.scrollHeight - b.scrollTop - b.clientHeight < px;
}
function scrollToBottom(instant = false) {
  const b = dom['messages'];
  if (instant) {
    // Temporarily override CSS scroll-behavior: smooth for an immediate jump.
    b.style.scrollBehavior = 'auto';
    b.scrollTop = b.scrollHeight;
    // Re-apply across the next couple of frames. On a fresh thread open the
    // avatar images and highlighted code blocks decode AFTER this first paint
    // and grow the scroll height — a single assignment lands short of the true
    // bottom (the reported "doesn't open at the bottom" bug). Mirrors the second
    // pass the smooth path already does.
    requestAnimationFrame(() => {
      b.scrollTop = b.scrollHeight;
      requestAnimationFrame(() => {
        b.scrollTop = b.scrollHeight;
        b.style.scrollBehavior = '';
      });
    });
  } else {
    // Use the standard scrollTo API for a smooth scroll that reliably reaches
    // the absolute bottom. A second rAF pass catches any layout shifts from
    // lazy-loaded images or code blocks that change height after the first frame.
    b.scrollTo({ top: b.scrollHeight, behavior: 'smooth' });
    requestAnimationFrame(() => {
      b.scrollTo({ top: b.scrollHeight, behavior: 'smooth' });
    });
  }
  showScrollButton(false);
}

// Open-a-thread scroll settle: re-apply a target scroll position across frames
// until the layout stops growing (or a deadline). A freshly rendered thread keeps
// growing for many frames — images decode, code highlights, web fonts swap — so a
// one-shot assignment lands short (stick) or clamps to 0 (restore, when the saved
// offset briefly exceeds the still-small scrollHeight). targetFn(box) returns the
// desired scrollTop each frame. The settle is cancelled the instant the user
// scrolls/keys with intent, so it never fights someone reading back up the thread.
let settleToken = 0;
function settleScroll(targetFn, maxMs = 1600) {
  const b = dom['messages'];
  const token = ++settleToken;             // supersede any in-flight settle
  const start = performance.now();
  let lastH = -1, stable = 0, done = false;
  b.style.scrollBehavior = 'auto';
  const finish = () => {
    if (done) return;
    done = true;
    b.style.scrollBehavior = '';
    b.removeEventListener('wheel', onUser);
    b.removeEventListener('touchstart', onUser);
    b.removeEventListener('keydown', onUser);
    showScrollButton(!isNearBottom());
  };
  const onUser = () => finish();           // user takes over → stop pinning
  b.addEventListener('wheel', onUser, { passive: true });
  b.addEventListener('touchstart', onUser, { passive: true });
  b.addEventListener('keydown', onUser);
  const step = () => {
    if (done || token !== settleToken) { finish(); return; }
    b.scrollTop = targetFn(b);
    const h = b.scrollHeight;
    stable = (h === lastH) ? stable + 1 : 0;
    lastH = h;
    // Four stable frames (~64ms) or the deadline ends the settle.
    if (stable >= 4 || performance.now() - start > maxMs) { finish(); return; }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function settleScrollBottom(maxMs = 1600) { settleScroll((b) => b.scrollHeight, maxMs); }
let unseenCount = 0;
function updateScrollBadge() {
  const b = dom['sb-count'];
  if (!b) return;
  if (unseenCount > 0) { b.textContent = unseenCount > 99 ? '99+' : String(unseenCount); b.classList.remove('hidden'); }
  else b.classList.add('hidden');
}
function bumpUnseen() { unseenCount += 1; updateScrollBadge(); }
function resetUnseen() { unseenCount = 0; updateScrollBadge(); }
function showScrollButton(show) {
  dom['scroll-bottom'].classList.toggle('hidden', !show);
  if (!show) resetUnseen();
}

// ===================== Composer =====================
function reflectComposerState() {
  const thinking = !!state.thinking[state.activeThreadId];
  const noThread = !state.activeThreadId;
  const offline = state.started && !state.connected;
  dom['input'].disabled = noThread;
  // With no thread open the composer was already INERT (the input is disabled
  // above, sendMessage() bails on !state.activeThreadId, and updateSendEnabled
  // keeps ↑ disabled) — it just didn't LOOK it: a full-strength row inviting a
  // click that does nothing. Say so instead: dim the whole row and swap the
  // placeholder for one that names the missing step. The attach ＋ is disabled
  // for the same reason — an attachment with nowhere to go is a dead end, and
  // sendMessage() would refuse it anyway.
  dom['composer'].classList.toggle('no-thread', noThread);
  dom['attach-btn'].disabled = noThread;
  dom['input'].setAttribute(
    'placeholder', t(noThread ? 'composer.placeholder_no_thread' : 'composer.placeholder'),
  );
  dom['waiting'].hidden = !thinking && !offline;
  dom['waiting'].textContent = t(offline ? 'composer.offline' : 'composer.waiting');
  dom['waiting'].classList.toggle('offline-note', offline && !thinking);
  updateSendEnabled();
  refreshTyping();
}

function updateSendEnabled() {
  // Multiple messages may be queued while a reply is pending — the server
  // serialises turns per thread — so we do NOT disable on "thinking".
  const hasContent = dom['input'].value.trim().length > 0 || state.attachments.length > 0;
  dom['send'].disabled = !hasContent || !state.activeThreadId;
}

function autosize() {
  const ta = dom['input'];
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 150) + 'px';
  const n = ta.value.length;
  dom['char-count'].hidden = n <= 4000;
  if (n > 4000) dom['char-count'].textContent = t('composer.chars', { count: n });
}

function sendMessage() {
  const text = dom['input'].value.trim();
  if ((!text && !state.attachments.length) || !state.activeThreadId) return;
  // Note: intentionally NOT blocked while a reply is pending — queued sends are
  // serialised server-side, so the user can fire off several in a row.

  let full = text;
  if (state.attachments.length) {
    // Images as markdown; videos as [[media:...]]; documents as [[doc:...]].
    const refs = state.attachments.map((a) => {
      if (a.kind === 'document') return `[[doc:${a.id}|${a.name}]]`;
      if (a.kind === 'video') return `[[media:${a.url}|${a.name}]]`;
      return `![${a.name}](${a.url})`;
    }).join('\n');
    full = text ? `${text}\n\n${refs}` : refs;
  }
  if (full.length > 65536) { toast(t('toast.too_long'), true); return; }
  const frame = { type: 'send', thread_id: state.activeThreadId, text: full, client_msg_id: newClientMsgId() };
  const ok = !!(socket && socket.send(frame));
  if (!ok) { toast(t('toast.offline'), true); return; }
  trackPendingSend(frame);

  dom['input'].value = '';
  clearAttachments();
  autosize();
  // Optimistic "thinking" — shows the typing bubble + waiting label right away
  // (the composer itself stays usable for queued follow-up messages).
  state.thinking[state.activeThreadId] = true;
  reflectComposerState();
}

// ===================== WS send-ack (guaranteed delivery) =====================
// socket.send() "succeeds" on a half-open TCP connection while frames vanish
// silently until the liveness watchdog fires (65–95s). Every 'send' carries a
// client_msg_id and stays in pendingSends until the server acks it (or its
// persisted broadcast echoes the id back). Pending frames are replayed
// unchanged on reconnect (server dedups by id), and anything undelivered >20s
// on a supposedly-open socket surfaces a Retry chip that restores the text.
const pendingSends = new Map();   // client_msg_id -> {frame, thread_id, text, sentAt}
const PENDING_RETRY_MS = 20000;
let pendingSweepTimer = null;
let restoredComposerText = null;  // text the Retry chip put back — auto-removed on delivery

function newClientMsgId() {
  try { return 'c-' + crypto.randomUUID(); }
  catch { return 'c-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10); }
}

function socketOpen() {
  return !!(socket && socket.ws && socket.ws.readyState === WebSocket.OPEN);
}

function trackPendingSend(frame) {
  pendingSends.set(frame.client_msg_id, {
    frame, thread_id: frame.thread_id, text: frame.text, sentAt: Date.now(),
  });
  if (!pendingSweepTimer) pendingSweepTimer = setInterval(updateRetryChip, 5000);
  updateRetryChip();
}

// Clear on ack OR on seeing our id in the persisted-message broadcast. Unknown
// ids (other tabs' sends) are ignored silently. Returns the entry, if any.
function clearPendingSend(clientMsgId) {
  if (!clientMsgId) return null;
  const p = pendingSends.get(clientMsgId);
  if (!p) return null;
  pendingSends.delete(clientMsgId);
  if (!pendingSends.size) {
    clearInterval(pendingSweepTimer); pendingSweepTimer = null;
    // Everything delivered: if the Retry chip restored this text and the user
    // hasn't typed over it, take it back out of the composer.
    if (restoredComposerText !== null && dom['input'].value === restoredComposerText) {
      dom['input'].value = ''; autosize(); updateSendEnabled();
    }
    restoredComposerText = null;
  }
  updateRetryChip();
  return p;
}

// Mode change / lock: a full-mode frame must never be replayed on a Safe-Mode
// socket, and restored text must not linger into a locked composer.
function dropAllPendingSends() {
  pendingSends.clear();
  clearInterval(pendingSweepTimer); pendingSweepTimer = null;
  restoredComposerText = null;
  updateRetryChip();
}

function updateRetryChip() {
  const chip = dom['retry-chip'];
  if (!chip) return;
  const now = Date.now();
  let stale = false;
  for (const p of pendingSends.values()) {
    if (now - p.sentAt > PENDING_RETRY_MS) { stale = true; break; }
  }
  // Only while the socket CLAIMS to be open — a visibly-down socket already
  // shows the reconnect overlay, and reconnect replays pendings automatically.
  chip.classList.toggle('hidden', !(stale && socketOpen()));
}

// Reconnect replay: identical frames (same client_msg_id) so the server dedups.
function resendPendingSends() {
  for (const p of pendingSends.values()) {
    p.sentAt = Date.now();
    if (socket) socket.send(p.frame);
  }
  updateRetryChip();
}

// Retry chip click: restore the text to the composer (so it cannot be lost)
// and re-send the frames unchanged. If delivery then confirms, the restored
// text is auto-removed (see clearPendingSend).
function retryPendingSends() {
  const now = Date.now();
  const stale = [...pendingSends.values()].filter((p) => now - p.sentAt > PENDING_RETRY_MS);
  if (!stale.length) { updateRetryChip(); return; }
  if (!dom['input'].value.trim()) {
    const text = stale.map((p) => p.text).join('\n\n');
    dom['input'].value = text;
    restoredComposerText = text;
    autosize(); updateSendEnabled();
  }
  for (const p of stale) { p.sentAt = now; if (socket) socket.send(p.frame); }
  updateRetryChip();
}

function handleSendRejected(p, reason) {
  // Neutral wording in Safe Mode — a reject reason could hint at the lock.
  toast((state.decoy || !reason) ? t('toast.not_sent') : t('toast.not_sent_reason', { reason }), true);
  // Undo the optimistic "thinking" flag. No turn started, so nothing else will
  // ever clear it.
  const tid = p && p.thread_id;
  if (tid) {
    delete state.thinking[tid];
    renderSidebar(); renderThreads();
    if (tid === state.activeThreadId) { renderMessages(false); reflectComposerState(); }
  }
  // The composer cleared optimistically — put the text back so nothing is lost.
  if (p && p.text && !dom['input'].value.trim()) {
    dom['input'].value = p.text;
    autosize(); updateSendEnabled();
    dom['input'].focus();
  }
}

// ===================== Attachments =====================
function renderAttachments() {
  const p = dom['attach-preview'];
  p.innerHTML = '';
  p.classList.toggle('hidden', state.attachments.length === 0);
  state.attachments.forEach((a, i) => {
    let thumb;
    const previewSrc = a.previewUrl || a.url;
    if (a.kind === 'document') {
      thumb = el('span', { class: 'chip-doc-icon', text: fileIcon(a.name) });
    } else if (a.kind === 'video') {
      thumb = el('video', { src: previewSrc, muted: '', loop: '', playsinline: '' });
      thumb.muted = true; thumb.play().catch(() => {});
    } else {
      thumb = el('img', { src: previewSrc, alt: a.name });
    }
    const label = a.kind === 'document'
      ? el('span', { class: 'chip-label', text: a.name.slice(0, 30) })
      : null;
    const children = [thumb];
    if (label) children.push(label);
    children.push(el('button', { class: 'rm', text: '✕', onclick: () => { const [rm] = state.attachments.splice(i, 1); if (rm && rm.previewUrl) URL.revokeObjectURL(rm.previewUrl); renderAttachments(); updateSendEnabled(); } }));
    const chip = el('div', { class: 'chip' + (a.kind === 'document' ? ' doc-chip' : '') }, children);
    p.append(chip);
  });
}
function clearAttachments() {
  state.attachments.forEach((a) => { if (a.previewUrl) URL.revokeObjectURL(a.previewUrl); });
  state.attachments = [];
  renderAttachments();
}

async function handleFiles(files) {
  for (const file of files) {
    const isImage = file.type.startsWith('image/');
    const isVideo = file.type.startsWith('video/');
    // Anything that isn't a renderable image/video is shared as a downloadable
    // file (the "＋" accepts any file type, in both full and Safe Mode).
    const isDoc = !isImage && !isVideo;
    // Reject oversize files BEFORE uploading (server enforces the same caps).
    const maxMB = isImage ? 25 : isVideo ? 200 : 50;
    if (file.size > maxMB * 1024 * 1024) {
      // {kind} is a separate key so translators can inflect it to fit the
      // carrier sentence rather than having three near-duplicate sentences.
      toast(t('files.too_large', {
        name: file.name,
        limit: maxMB,
        kind: t(isImage ? 'files.kind_images' : isVideo ? 'files.kind_videos' : 'files.kind_files'),
      }), true);
      continue;
    }
    try {
      const r = await api.upload(file);
      state.attachments.push({
        url: r.url,
        // Local object URL for the composer thumbnail so the preview works even
        // in Safe Mode (where /media/* is server-blocked). Revoked on clear.
        previewUrl: (isImage || isVideo) ? URL.createObjectURL(file) : null,
        name: file.name || t(isDoc ? 'common.file' : isVideo ? 'common.video' : 'common.image'),
        kind: r.kind || (isDoc ? 'document' : isVideo ? 'video' : 'image'),
        id: r.id,
        mime: r.mime,
        size: r.size,
      });
    } catch (e) { toast(t('files.upload_failed', { error: e.message }), true); }
  }
  renderAttachments();
  updateSendEnabled();
}

// ===================== Lightbox =====================
// Click outside (backdrop) or ✕ closes. Double-click/double-tap an image
// zooms in at that point; drag to pan while zoomed; double-click again to
// reset. Videos get native controls + sound.
function openLightbox(src, { video = false, downloadUrl = null, downloadName = null } = {}) {
  const media = video
    ? el('video', { src, controls: '', autoplay: '', loop: '', playsinline: '' })
    : el('img', { src, draggable: 'false', alt: downloadName || t('msg.fullsize_alt') });
  const closeBtn = el('button', { class: 'lightbox-close', text: '✕', 'aria-label': t('common.close') });
  // A modal dialog, declared as one: without role/aria-modal a screen reader
  // keeps reading the chat behind it, and without `inert` on #app a Tab walks
  // straight out of the picture into the composer underneath.
  const lb = el('div', {
    class: 'lightbox',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': downloadName || t('msg.fullsize_alt'),
    tabindex: '-1',
  }, [media, closeBtn]);
  if (downloadUrl) {
    const dl = el('a', { class: 'lightbox-download', href: downloadUrl, text: '⬇ ' + t('common.download') });
    if (downloadName) dl.setAttribute('download', downloadName);
    dl.addEventListener('click', (e) => e.stopPropagation());
    lb.append(dl);
  }

  const cleanups = [];
  // Whoever was focused when the picture opened gets focus back when it closes
  // — a keyboard user who opened a thumbnail lands back on that thumbnail, not
  // at the top of the document.
  const returnFocus = document.activeElement;
  const close = () => {
    try { if (video) media.pause(); } catch {}
    cleanups.forEach((f) => f());
    lb.remove();
    if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === 'function') {
      try { returnFocus.focus(); } catch { /* focus is best effort */ }
    }
  };
  // Published on the node so teardown paths that only have the DOM (see
  // closeAllOverlays) can run the real cleanup instead of just removing it.
  lb._close = close;
  closeBtn.addEventListener('click', close);
  lb.addEventListener('click', (e) => { if (e.target === lb) close(); });

  if (!video) {
    let scale = 1, tx = 0, ty = 0;
    const apply = () => {
      media.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
      media.classList.toggle('zoomed', scale > 1);
    };
    media.addEventListener('dblclick', (e) => {
      e.preventDefault();
      if (scale > 1) { scale = 1; tx = ty = 0; }
      else {
        scale = 2.5;
        // Zoom centered on the click point: shift so that point stays put.
        const r = media.getBoundingClientRect();
        tx = (r.left + r.width / 2 - e.clientX) * (scale - 1);
        ty = (r.top + r.height / 2 - e.clientY) * (scale - 1);
      }
      apply();
    });
    // Drag (mouse or touch) to pan while zoomed.
    let dragging = false, lx = 0, ly = 0;
    const down = (x, y) => { if (scale > 1) { dragging = true; lx = x; ly = y; } };
    const move = (x, y) => {
      if (!dragging) return;
      tx += x - lx; ty += y - ly; lx = x; ly = y; apply();
    };
    media.addEventListener('mousedown', (e) => { e.preventDefault(); down(e.clientX, e.clientY); });
    // Pan tracks on window (so drags survive leaving the image), but the
    // handlers must die with the lightbox or every open leaks two listeners.
    const onMove = (e) => move(e.clientX, e.clientY);
    const onUp = () => { dragging = false; };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    cleanups.push(() => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    });
    media.addEventListener('touchstart', (e) => { if (e.touches.length === 1) down(e.touches[0].clientX, e.touches[0].clientY); }, { passive: true });
    media.addEventListener('touchmove', (e) => { if (e.touches.length === 1) move(e.touches[0].clientX, e.touches[0].clientY); }, { passive: true });
    media.addEventListener('touchend', () => { dragging = false; });
    // Mouse wheel zoom.
    lb.addEventListener('wheel', (e) => {
      e.preventDefault();
      const prev = scale;
      scale = Math.min(6, Math.max(1, scale * (e.deltaY < 0 ? 1.18 : 0.85)));
      if (scale === 1) { tx = ty = 0; }
      else { const f = scale / prev; tx *= f; ty *= f; }
      apply();
    }, { passive: false });
  }
  const app = dom['app'] || document.getElementById('app');
  if (app) {
    app.inert = true;
    cleanups.push(() => { app.inert = false; });
  }
  document.body.append(lb);
  closeBtn.focus();
}

// Full-resolution avatar viewer (click any bot avatar).
// ===================== Thumbnails and their full-resolution originals =====
//
// CORE PRINCIPLE: every thumbnail in this app is a VIEW of a larger image, and
// clicking it opens THAT image at full resolution. Not today's version of it,
// not a similar one — the same picture.
//
// It is enforced by convention rather than by remembering: a thumbnail carries
// `data-full="<url>"`, and ONE delegated listener below opens the lightbox. No
// call site wires its own handler, so a new thumbnail cannot be added with the
// click quietly missing — which is exactly how thread avatars shipped
// unclickable, and how a restored avatar came to show a DIFFERENT image at full
// size (the restore wrote the face crop and left the previous avatar's
// full-resolution file in place).
//
// A thumbnail with no larger version simply omits the attribute and is inert.
// frontend/tests/thumbnails.test.js asserts every producer here sets it.

function setFullRes(node, url) {
  if (node && url) node.dataset.full = url;
  return node;
}

/** One listener for every thumbnail, present and future.
 *
 * CAPTURE phase, and that is load-bearing: a document-level bubble listener is
 * the LAST stop on the propagation path, so its stopPropagation() cancelled
 * nothing — a thread row's own click handler had already fired and the app
 * both opened the conversation AND showed the picture. Capturing runs this
 * before any bubble handler on the row (or on the image itself), so a click on
 * a thumbnail means "show me the picture" and nothing else.
 */
function installThumbnailLightbox() {
  document.addEventListener('click', (e) => {
    const thumb = e.target.closest?.('[data-full]');
    if (!thumb) return;
    // Belt to the braces above: if a thumbnail ever ends up inside a control
    // whose own job is to navigate, the control wins. A picture that is a
    // button is a button.
    if (thumb.closest('.bot-btn')) return;
    e.preventDefault();
    e.stopPropagation();
    const url = thumb.dataset.full;
    if (url) openLightbox(url, { video: isVideoUrl(url) });
  }, true);
}

function openAvatarLightbox(botId) {
  if (!botId) return;
  openLightbox(`/api/bots/${encodeURIComponent(botId)}/avatar/full`);
}

// ===================== Actions =====================
async function selectBot(id) {
  if (!id) return;
  if (id === TERMINAL_ID) { openTerminalView(); return; }
  if (id === HARNESS_ID) { openHarnessView(); return; }
  // Leaving the terminal for a real bot tears the view/socket down cleanly.
  if (terminalOpen) closeTerminalView();
  if (harnessOpen) closeHarnessView();
  state.selectedBotId = id;
  renderSidebar();
  updateThreadListHeader();
  try {
    const r = await api.threads(id);
    if (state.selectedBotId !== id) return;   // user switched while loading
    state.threads = r.threads || [];
    state.threads.forEach((t) => {
      state.threadBot[t.id] = t.bot_id;
      syncUnreadFromThread(t);
      // Reconcile authoritative "thinking" state from the server thread status
      // (so a stuck optimistic flag clears, e.g. after a WS reconnect).
      if (t.status === 'thinking') state.thinking[t.id] = true;
      else delete state.thinking[t.id];
    });
  } catch (e) {
    if (state.selectedBotId !== id) return;   // stale failure — don't clobber
    state.threads = []; toast(e.message, true);
  }
  if (state.selectedBotId !== id) return;
  renderThreads();
  renderSidebar();
  if (!isMobile() && state.threads.length) openThread(state.threads[0].id);
  else { clearChatView(); if (isMobile()) navigate('threads'); }
}

// `background: true` refreshes a thread in place (reconnect re-sync) without
// stealing the view, focus, or the user's scroll position.
let suppressScrollSave = false;
// Released on a later frame so the programmatic scrolls this function performs
// aren't recorded as the user's position. Must run on EVERY exit path: the flag
// used to be cleared only on the happy path, so a thread switch that landed on
// one of the "switched away while loading" early returns stuck it TRUE for the
// rest of the session — scroll positions silently stopped being saved and every
// thread reopened at the bottom.
function releaseScrollSave() {
  requestAnimationFrame(() => setTimeout(() => { suppressScrollSave = false; }, 0));
}
async function openThread(id, { background = false } = {}) {
  const switching = state.activeThreadId !== id;
  suppressScrollSave = true;
  // The jump-to-new counter is per-view: a real switch starts it fresh so it
  // never carries thread A's count into thread B (which restores above-bottom).
  if (switching && !background) resetUnseen();
  // Save the outgoing thread's scroll position before switching — but only if it
  // was scrolled UP. At/near the bottom we clear instead (mirroring the scroll
  // handler), so reopening sticks to the latest messages rather than restoring a
  // stale offset. Without this, a thread you last viewed at the bottom would
  // reopen part-way up (the reported "doesn't open at the bottom" bug).
  if (state.activeThreadId) {
    if (isNearBottom()) delete state.scrollPositions[state.activeThreadId];
    else state.scrollPositions[state.activeThreadId] = dom['messages'].scrollTop;
  }
  const t = state.threads.find((x) => x.id === id);
  state.activeThreadId = id;
  state.activeThread = t || state.activeThread;
  // One-directional reconcile from the LOCAL cache: only ever SET the flag.
  // Clearing here could race a just-sent message (the cached status lags the
  // server); clears come from thinking/stopped events and server-fetched
  // reconciliation in selectBot()/resync().
  if (t && t.status === 'thinking') state.thinking[id] = true;
  if (switching && !background) clearAttachments();
  renderThreads();
  renderChatHeader();
  // Paint shimmer placeholders while the history request is in flight (only on a
  // real switch — a background re-sync must not blow away the current view).
  if (switching && !background) renderSkeleton();

  const box = dom['messages'];
  const wasNearBottom = isNearBottom();
  const savedTop = box.scrollTop;

  try {
    const r = await api.messages(id);
    if (state.activeThreadId !== id) { releaseScrollSave(); return; }   // switched away
    const fetched = r.messages || [];
    // Merge, don't replace: keep any live WS messages that landed while the
    // fetch was in flight (they may post-date the HTTP snapshot).
    const ids = new Set(fetched.map((m) => m.id));
    const extras = state.messages.filter((m) => m.thread_id === id && !ids.has(m.id));
    state.messages = fetched.concat(extras)
      .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
    state.hasMoreOlder = !!r.has_more;
  } catch (e) {
    if (state.activeThreadId !== id) { releaseScrollSave(); return; }
    state.messages = []; state.hasMoreOlder = false; toast(e.message, true);
  }
  // Bug 1: Don't scroll to bottom if we have a saved position for the target
  // thread — restore that instead.
  const hasSavedPos = state.scrollPositions[id] !== undefined;
  renderMessages((!background || wasNearBottom) && !hasSavedPos);
  if (background && !wasNearBottom) box.scrollTop = savedTop;
  // Let the swap-induced scroll events (clamp + restore/stick) flush before
  // re-enabling position saving.
  releaseScrollSave();
  renderChatHeader();        // refresh model pill now that messages are loaded
  reflectComposerState();
  markThreadRead(id);        // opening a thread clears its unread indicator
  if (!background) {
    if (isMobile()) navigate('chat');
    setTimeout(() => dom['input'].focus(), 50);
  }
}

// Scroll-back pagination: load the previous page when the user nears the top.
let loadingOlder = false;
async function loadOlderMessages() {
  const id = state.activeThreadId;
  if (!id || loadingOlder || !state.hasMoreOlder || !state.messages.length) return;
  loadingOlder = true;
  const box = dom['messages'];
  const prevHeight = box.scrollHeight;
  const prevTop = box.scrollTop;
  try {
    const r = await api.messages(id, state.messages[0].id);
    if (state.activeThreadId !== id) return;
    const older = r.messages || [];
    state.hasMoreOlder = !!r.has_more;
    if (older.length) {
      const ids = new Set(state.messages.map((m) => m.id));
      state.messages = older.filter((m) => !ids.has(m.id)).concat(state.messages);
      // Drop any saved scroll offset first: with it set, renderMessages(false)
      // would launch settleScroll(() => savedPos) and re-pin the viewport near
      // the top every frame — overriding the manual anchor below and, because
      // that keeps scrollTop < 80, re-triggering loadOlderMessages in a runaway
      // cascade that burst-loads the whole history. Cleared → renderMessages
      // runs neither scroll branch and the anchor is authoritative.
      delete state.scrollPositions[id];
      renderMessages(false);
      // Keep the viewport anchored on what the user was reading.
      box.scrollTop = box.scrollHeight - prevHeight + prevTop;
    }
  } catch (e) { toast(e.message, true); }
  finally { loadingOlder = false; }
}

// Re-sync everything after a dropped+restored WebSocket connection.
async function resync() {
  const botId = state.selectedBotId;
  if (!botId) return;
  try {
    const r = await api.threads(botId);
    if (state.selectedBotId !== botId) return;   // selection changed mid-resync
    state.threads = r.threads || [];
    state.threads.forEach((t) => {
      state.threadBot[t.id] = t.bot_id;
      syncUnreadFromThread(t);
      if (t.status === 'thinking') state.thinking[t.id] = true;
      else delete state.thinking[t.id];
    });
  } catch { return; /* will retry on next reconnect */ }
  if (state.selectedBotId !== botId) return;
  renderThreads();
  renderSidebar();
  if (state.activeThreadId && state.threads.find((t) => t.id === state.activeThreadId)) {
    await openThread(state.activeThreadId, { background: true });
  } else {
    reflectComposerState();
  }
}

async function newChat() {
  if (!state.selectedBotId) return;
  try {
    const t = await api.createThread(state.selectedBotId);
    upsertThread(t);
    renderThreads();
    await openThread(t.id);
  } catch (e) { toast(e.message, true); }
}

// ===================== Thread menu (rename / archive / delete) =====================
function toggleThreadMenu(show) {
  dom['thread-menu'].hidden = show === undefined ? !dom['thread-menu'].hidden : !show;
  const btn = dom['thread-menu-btn'];
  if (btn) btn.setAttribute('aria-expanded', String(!dom['thread-menu'].hidden));
}
async function threadAction(act) {
  toggleThreadMenu(false);
  const id = state.activeThreadId;
  if (!id) return;
  try {
    if (act === 'pin') {
      const pinned = !(state.activeThread && state.activeThread.is_pinned);
      await api.pin(id, pinned);
      // Optimistic update — WS thread_update will confirm
      if (state.activeThread) state.activeThread.is_pinned = pinned;
      const th = state.threads.find((x) => x.id === id);
      if (th) th.is_pinned = pinned;
      sortThreads();
      renderThreads();
    } else if (act === 'rename') {
      const cur = state.activeThread ? threadTitle(state.activeThread) : '';
      const title = await uiPrompt(t('chat.rename_title'), cur);
      if (title && title.trim()) { await api.rename(id, title.trim()); }
    } else if (act === 'sync') {
      await syncThreadFromOpenClaw(id);
    } else if (act === 'transcript') {
      await openThreadTranscript(id);
    } else if (act === 'archive') {
      // Archiving hides the chat from the list (it isn't deleted). There's no
      // in-app "archived" view yet, so confirm + acknowledge rather than let it
      // silently vanish like a delete.
      if (await uiConfirm(t('chat.archive_confirm'), { okText: t('chat.archive') })) {
        await api.archive(id);
        toast(t('chat.archived'));
      }
    } else if (act === 'delete') {
      if (await uiConfirm(t('chat.delete_confirm'), { danger: true })) await api.remove(id);
    }
  } catch (e) { toast(e.message, true); }
}

// ===================== Bot Manager =====================
// The language <select> is built by i18n.js rather than declared in markup, so
// the option list can never drift from the locales that actually ship. It is
// rebuilt on every open because the control reflects the active language at
// construction time. Like the avatar-style toggle beside it, this is a device
// preference: it applies instantly and stays outside the Save/dirty flow.
function mountLanguagePicker() {
  const row = dom['bm-lang-row'];
  if (!row) return;
  const existing = row.querySelector('.lang-select');
  if (existing) existing.remove();
  row.prepend(languageSelect({ id: 'bm-lang' }));
  // Privacy mode sits with it: also per-device, also instant, also nothing to
  // do with the bot roster that Save applies.
  const oldRow = document.getElementById('privacy-row');
  if (oldRow) oldRow.remove();
  const pr = privacyRow(t);
  pr.id = 'privacy-row';
  row.after(pr);
  // No-Image Mode sits with them: third device preference, same instant-apply
  // rules, nothing to do with the bot roster that Save applies. Rebuilt on each
  // open so the ratchet's disabled state reflects the CURRENT session — a
  // device that has since locked must not still offer an operable switch.
  const oldNim = document.getElementById('nim-row');
  if (oldNim) oldNim.remove();
  const nr = nimRow(t, { decoy: state.decoy, pinSet: !!state.auth.pinSet, onChange: applyNimChange });
  nr.id = 'nim-row';
  pr.after(nr);
  // About: version + the AGPL §13 source offer. In the Device pane on purpose —
  // it is the one settings tab a Safe-Mode session can open, and §13 owes the
  // offer to every user of the running program, not just the operator.
  const oldAbout = document.getElementById('about-row');
  if (oldAbout) oldAbout.remove();
  const ab = aboutRow(t);
  ab.id = 'about-row';
  // LAST in the pane, under the action buttons: it is reference information,
  // not a control, and inserting it between two toggles reads as a setting.
  (dom['spane-device'] || nr.parentNode).append(ab);
}

// Keep the Minimal-avatars control honest about who is driving it.
//
// NIM implies minimal avatars, so the box is ticked AND locked while NIM is on,
// with the reason spelled out — a control that silently ignores you is worse
// than one that explains why it cannot move.
function syncMinimalAvatarRow() {
  const box = dom['bm-avatar-minimal'];
  if (!box) return;
  const nim = nimEnabled();
  box.checked = nim
    || document.documentElement.getAttribute('data-avatar-style') === 'minimal';
  box.disabled = nim;
  const row = dom['bm-avatar-style-row'];
  if (row) {
    row.classList.toggle('is-forced', nim);
    let note = row.querySelector('.bm-forced-note');
    if (nim && !note) {
      note = el('span', { class: 'bm-forced-note', text: ` — ${t('nim.controls_avatars')}` });
      row.querySelector('span')?.append(note);
    } else if (!nim && note) {
      note.remove();
    }
  }
}

// Re-render everything NIM touches. Called from both controls so the lock
// screen and Settings can never diverge in what they refresh.
function applyNimChange() {
  // Every surface NIM touches, rebuilt in place so toggling it takes effect
  // without a reload.
  //
  // This function used to call renderBots() and renderThreadList(), NEITHER OF
  // WHICH EXISTS — the real names are renderSidebar() and renderThreads(). It
  // therefore threw ReferenceError on its first line and re-rendered NOTHING;
  // the flag and the CSS applied, so a reload looked correct and every
  // end-to-end test passed, because every test reloaded. Toggling NIM in a
  // live session did nothing until the next refresh.
  renderSidebar();
  renderThreads();
  renderChatHeader();
  if (state.activeThreadId) renderMessages(false);
  // The Bot Manager is built once when the modal opens and has no tab-change
  // rebuild, so a NIM flip from the Device tab left avatars and "Change photo"
  // live on the Bots tab of the SAME modal. Rebuild it while it is open.
  if (!dom['botmanager-backdrop'].classList.contains('hidden')) renderBotManager();
  if (reactionManagerOpen()) repaintReactionManager();
  syncMinimalAvatarRow();
  syncLockNimRow();
}

// The lock-screen row. Always shown (a Safe-Mode device must be able to turn
// pictures OFF without a PIN); disabled only when NIM is already on and this
// session cannot turn it off — showing it checked-and-dimmed explains the
// missing pictures, where hiding the row would just look like a bug.
function syncLockNimRow() {
  const row = dom['lock-nim-row'];
  const box = dom['lock-nim'];
  if (!row || !box) return;
  const on = nimEnabled();
  const locked = on && !canDisableNim(state.decoy);
  box.checked = on;
  box.disabled = locked;
  row.classList.toggle('is-locked', locked);
  const span = row.querySelector('span');
  if (span) {
    const key = locked ? 'nim.lock_locked' : 'nim.lock_label';
    span.setAttribute('data-i18n', key);
    span.textContent = t(key);
  }
}

// ===================== Settings tabs =====================
// Six subjects that used to be spread across the gear rail (⚡ 🩺 🔌) and one
// endless Settings scroll. Each is a pane; three of them are admin surfaces
// that a Safe-Mode session must not even see — the same rule the rail buttons
// they replace carried, enforced in applyAuthChrome().
//
// A tab OWNS its pane's lifecycle: activating mounts, leaving unmounts. That
// matters for exactly one of them — the dashboard polls every 5s — and the
// leave path has to fire on every way out of the modal, which is why
// deactivation hangs off a MutationObserver on the backdrop rather than being
// sprinkled through the seven places that hide it.
const SETTINGS_TABS = ['bots', 'reactions', 'health', 'ai', 'device', 'security'];
// The three that live behind the PIN. `admin-only` in the markup is the class;
// this is the list applyAuthChrome() walks.
const ADMIN_TABS = ['reactions', 'health', 'ai'];
let settingsTab = 'bots';

const settingsTabBtn = (id) => document.getElementById(`stab-${id}`);
const settingsPane = (id) => dom[`spane-${id}`];

/** Is the Settings modal open, on tab `id` (or on any tab, if `id` is null)? */
function settingsOpen(id = null) {
  if (dom['botmanager-backdrop'].classList.contains('hidden')) return false;
  return id === null || settingsTab === id;
}

/** Tear down whatever the ACTIVE tab has running. Idempotent by design: it is
 *  called on every tab switch and again when the modal closes. */
function deactivateSettingsTab() {
  if (settingsTab === 'health') unmountDashboard();
  if (settingsTab === 'reactions') unmountReactionManager();
}

/** Bring `id` on screen: swap the tablist state, swap the panes, swap the
 *  footer group, then let the tab's module take over its pane. */
async function setSettingsTab(id, { focusTab = false } = {}) {
  if (!SETTINGS_TABS.includes(id)) id = 'bots';
  // A hidden (Safe Mode) tab is not a destination — fall back to the roster.
  // Resolve the button AFTER that swap, or focus and scroll-into-view would
  // both aim at the tab we just refused to open.
  let btn = settingsTabBtn(id);
  if (!btn || btn.classList.contains('hidden')) { id = 'bots'; btn = settingsTabBtn(id); }
  if (id !== settingsTab) deactivateSettingsTab();
  settingsTab = id;

  for (const tab of SETTINGS_TABS) {
    const b = settingsTabBtn(tab);
    const p = settingsPane(tab);
    const on = tab === id;
    if (b) {
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', String(on));
      // Roving tabindex: one stop for the whole tablist, arrows do the rest.
      b.tabIndex = on ? 0 : -1;
    }
    if (p) p.classList.toggle('hidden', !on);
  }
  // Contextual footer. An empty group means no actions for this tab, and the
  // footer collapses rather than showing an empty bar.
  let anyAction = false;
  dom['bm-footer'].querySelectorAll('.settings-actions').forEach((g) => {
    const on = g.dataset.tab === id;
    g.classList.toggle('hidden', !on);
    if (on) anyAction = true;
  });
  dom['bm-footer'].classList.toggle('hidden', !anyAction);

  if (focusTab && btn) { try { btn.focus(); } catch { /* ignore */ } }
  // On a phone the strip is wider than the screen, so the tab you just chose
  // (with an arrow key, or by opening Settings straight onto it) can be off it.
  if (btn && btn.scrollIntoView) {
    try { btn.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch { /* ignore */ }
  }
  updateTabStripEdges();

  // Reset the pane's scroll: arriving halfway down the previous tab's scroll
  // offset reads as a broken page.
  const pane = settingsPane(id);
  if (pane) pane.scrollTop = 0;

  switch (id) {
    case 'reactions':
      await mountReactionManager(pane);
      break;
    case 'health':
      await mountDashboard(pane, dom['sfoot-health'], dashCtx());
      break;
    case 'ai':
      await activateLlmPanel(llmCtx());
      break;
    case 'device':
      mountLanguagePicker();
      // Reflect the LIVE attribute, not just localStorage — the theme module
      // can have changed it since this modal was last opened.
      //
      // EXCEPT under No-Image Mode, which BORROWS this attribute to reuse the
      // minimal-avatar rules (see nim.js applyNim). Reflecting the borrowed
      // value made this checkbox claim a stored preference the user never set,
      // and worse, unticking it wrote that lie to disk AND stripped the
      // attribute out from under NIM — leaving a desktop bot rail sized for
      // pictures with every picture hidden, i.e. a blank column. So while NIM
      // is on the control shows what is true (avatars are minimal) and refuses
      // to be the thing that changes it.
      syncMinimalAvatarRow();
      break;
    case 'security':
      activateSecurityPane();
      break;
  }
}

/** Fade whichever end of the tab strip has more tabs beyond it.
 *
 *  Six tabs are ~570px of strip against a 390px phone, so two of them —
 *  Device and Security — are simply not on screen, and a hard-cut edge reads
 *  as "that's all of them". The fade is measured, not assumed: it appears only
 *  when there is genuinely something to scroll to, in either direction, which
 *  also makes it correct under RTL without a second rule.
 */
function updateTabStripEdges() {
  const list = dom['settings-tabs'];
  if (!list) return;
  const slack = list.scrollWidth - list.clientWidth;
  if (slack <= 1) {
    list.classList.remove('fade-start', 'fade-end');
    return;
  }
  // scrollLeft is negative in an RTL container in every engine we target.
  const pos = Math.abs(list.scrollLeft);
  list.classList.toggle('fade-start', pos > 2);
  list.classList.toggle('fade-end', pos < slack - 2);
}

function wireSettingsTabs() {
  const list = dom['settings-tabs'];
  list.querySelectorAll('.settings-tab').forEach((b) => {
    b.addEventListener('click', () => setSettingsTab(b.dataset.tab));
  });
  list.addEventListener('scroll', updateTabStripEdges, { passive: true });
  window.addEventListener('resize', updateTabStripEdges);
  // Arrow-key navigation over the VISIBLE tabs only: in Safe Mode the three
  // admin tabs aren't rendered, and stepping onto one would be a dead stop.
  list.addEventListener('keydown', (e) => {
    const keys = ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End'];
    if (!keys.includes(e.key)) return;
    const tabs = Array.from(list.querySelectorAll('.settings-tab'))
      .filter((b) => !b.classList.contains('hidden'));
    if (!tabs.length) return;
    const cur = Math.max(0, tabs.findIndex((b) => b.dataset.tab === settingsTab));
    // ← and → follow the writing direction, so an RTL locale's "next" is left.
    const rtl = document.documentElement.getAttribute('dir') === 'rtl';
    const fwd = (e.key === 'ArrowDown') || (e.key === (rtl ? 'ArrowLeft' : 'ArrowRight'));
    const back = (e.key === 'ArrowUp') || (e.key === (rtl ? 'ArrowRight' : 'ArrowLeft'));
    let next = cur;
    if (fwd) next = (cur + 1) % tabs.length;
    else if (back) next = (cur - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabs.length - 1;
    e.preventDefault();
    setSettingsTab(tabs[next].dataset.tab, { focusTab: true });
  });

  // The one place deactivation is wired, so no exit path can leak the
  // dashboard's 5s poll: closeAllOverlays(), a failed-save hide, the File
  // Server takeover and reboot() all hide this backdrop directly.
  new MutationObserver(() => {
    if (dom['botmanager-backdrop'].classList.contains('hidden')) deactivateSettingsTab();
  }).observe(dom['botmanager-backdrop'], { attributes: true, attributeFilter: ['class'] });
}

let bmBots = [];
// Order / visibility / Safe edits live only in bmBots until "Save" — track a
// dirty flag so ✕ / backdrop / Esc can warn instead of silently discarding.
let bmDirty = false;
async function openBotManager(tab = 'bots') {
  try {
    const r = await api.allBots();
    bmBots = r.bots.slice();
  } catch (e) { toast(e.message, true); return; }
  bmDirty = false;
  renderBotManager();
  refreshAvatarPoolPanel();     // async; fills in the Avatar pools section
  dom['botmanager-backdrop'].classList.remove('hidden');
  // After the reveal: setSettingsTab mounts panes, and a pane that measures
  // itself (the dashboard's cards) needs a laid-out box to measure.
  await setSettingsTab(tab);
}
// Dismiss without saving; confirms first when there are unsaved edits.
async function closeBotManager() {
  if (bmDirty && !dom['botmanager-backdrop'].classList.contains('hidden')) {
    if (!await uiConfirm(t('settings.discard_confirm'), { okText: t('common.discard'), danger: true })) return;
  }
  bmDirty = false;
  dom['botmanager-backdrop'].classList.add('hidden');   // observer deactivates
}
/** Open Settings straight onto a tab. This is what the modules' legacy entry
 *  points (openManager / openDashboard / openLlmPanel) and the first-run card
 *  now route through, so every old caller lands in the right place. */
function openSettingsTab(tab) {
  // Silent in Safe Mode, not a "unlock to do that" toast. A locked device
  // cannot reach any of these callers anyway (the gear routes to Companions),
  // and the whole point of the locked view is that it gives no sign there is
  // an admin surface behind it.
  if (state.decoy) return;
  if (settingsOpen()) { setSettingsTab(tab); return; }
  openBotManager(tab);
}
function renderBotManager() {
  const list = dom['bm-list'];
  list.innerHTML = '';
  bmBots.forEach((bot, idx) => {
    // The avatar itself is the primary "change photo" affordance — clicking it
    // opens the file picker + crop flow (discoverable without hunting for a
    // tiny icon). A letter-block fallback covers a missing/broken image.
    //
    // In No-Image Mode this is a letter block with NO picker attached. Settings
    // was the one screen that still showed every bot's photograph while the
    // rest of the app showed none — the picture was the button, so hiding the
    // picture and keeping the click would have left an invisible control.
    // Offering "change photo" on a device that cannot display photos is
    // incoherent anyway, so the affordance goes with it. Nothing is lost: turn
    // NIM off and the picker is back.
    let avatarImg;
    if (nimEnabled()) {
      avatarImg = tintLetterAvatar(
        el('div', { class: 'bm-avatar-pick letter-avatar', text: botLetter(bot) }), bot);
    } else {
      avatarImg = el('img', { src: bot.avatar_url, alt: '', title: t('settings.change_photo_short'), class: 'bm-avatar-pick' });
      avatarImg.addEventListener('click', () => pickAvatarFile(bot.id));
      avatarImg.addEventListener('error', () => {
        const fb = tintLetterAvatar(el('div', { class: 'bm-avatar-pick letter-avatar', text: botLetter(bot), title: t('settings.change_photo_short') }), bot);
        fb.addEventListener('click', () => pickAvatarFile(bot.id));
        avatarImg.replaceWith(fb);
      });
    }
    // Per-bot Safe Mode toggle — only reachable here (the Bot Manager opens
    // only when unlocked), so what Safe Mode can see is decided in full mode.
    const safeBtn = el('button', {
      class: 'bm-safe-btn' + (bot.safe ? ' on' : ''),
      text: t('settings.safe_badge'),
      title: t('settings.safe_title'),
      onclick: (e) => {
        e.stopPropagation();
        bmBots[idx].safe = !bmBots[idx].safe;
        safeBtn.classList.toggle('on', bmBots[idx].safe);
        bmDirty = true;
      },
    });
    // ▲/▼ move buttons: HTML5 DnD never fires on touch, so these are the
    // reorder affordance that works everywhere (drag still works on desktop).
    const moveBtn = (dir) => {
      const b = el('button', {
        class: 'bm-move-btn', text: dir < 0 ? '▲' : '▼',
        title: t(dir < 0 ? 'settings.move_up' : 'settings.move_down'),
        'aria-label': t(dir < 0 ? 'settings.move_up_aria' : 'settings.move_down_aria', { name: bot.name }),
      });
      if ((dir < 0 && idx === 0) || (dir > 0 && idx === bmBots.length - 1)) b.disabled = true;
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        const to = idx + dir;
        if (to < 0 || to >= bmBots.length) return;
        const [moved] = bmBots.splice(idx, 1);
        bmBots.splice(to, 0, moved);
        bmDirty = true;
        renderBotManager();
      });
      return b;
    };
    // Per-bot reaction images. Off for everyone but Nova by default — a
    // reaction interrupts every screen in the house, so it's opt-in per bot.
    const rxBtn = el('button', {
      class: 'bm-safe-btn' + (bot.reactions ? ' on' : ''),
      text: t('settings.react_badge'),
      title: t('settings.react_title'),
      onclick: (e) => {
        e.stopPropagation();
        bmBots[idx].reactions = !bmBots[idx].reactions;
        rxBtn.classList.toggle('on', bmBots[idx].reactions);
        bmDirty = true;
      },
    });
    // Per-bot avatar pool: new chats draw a one-shot face of their own. Also
    // opt-in — a pool only makes sense for a companion with a curated look.
    const apBtn = el('button', {
      class: 'bm-safe-btn' + (bot.avatar_pool ? ' on' : ''),
      text: t('settings.pool_badge'),
      title: t('settings.pool_title'),
      onclick: (e) => {
        e.stopPropagation();
        bmBots[idx].avatar_pool = !bmBots[idx].avatar_pool;
        apBtn.classList.toggle('on', bmBots[idx].avatar_pool);
        bmDirty = true;
      },
    });
    const row = el('div', { class: 'bm-row', draggable: 'true', dataset: { idx } }, [
      el('span', { class: 'bm-handle', text: '⠿' }),
      el('div', { class: 'bm-move' }, [moveBtn(-1), moveBtn(1)]),
      avatarImg,
      el('span', { class: 'bm-name', text: bot.name }),
      safeBtn,
      rxBtn,
      apBtn,
      // "Change photo" is omitted entirely in No-Image Mode — not disabled,
      // omitted. Offering to set a picture on a device that will not display
      // one is incoherent, and it is the last route into the crop dialog, so
      // dropping it keeps the whole photo flow unreachable rather than
      // half-reachable. el() ignores a null child, so this composes cleanly.
      nimEnabled() ? null : el('button', {
        class: 'bm-avatar-btn', text: t('settings.change_photo'), title: t('settings.change_photo_title'),
        onclick: (e) => { e.stopPropagation(); pickAvatarFile(bot.id); },
      }),
      (() => {
        const lbl = el('label', { class: 'switch', title: t('settings.show_in_sidebar') });
        const cb = el('input', { type: 'checkbox', 'aria-label': t('settings.show_in_sidebar') });
        cb.checked = bot.visible;
        cb.addEventListener('change', () => { bmBots[idx].visible = cb.checked; bmDirty = true; });
        lbl.append(cb, el('span', { class: 'slider' }));
        return lbl;
      })(),
    ]);
    wireDrag(row);
    list.append(row);
  });
}
let dragIdx = null;
function wireDrag(row) {
  row.addEventListener('dragstart', () => { dragIdx = +row.dataset.idx; row.classList.add('dragging'); });
  row.addEventListener('dragend', () => { dragIdx = null; row.classList.remove('dragging'); document.querySelectorAll('.bm-row').forEach(r => r.classList.remove('drop-target')); });
  row.addEventListener('dragover', (e) => { e.preventDefault(); row.classList.add('drop-target'); });
  row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
  row.addEventListener('drop', (e) => {
    e.preventDefault();
    let to = +row.dataset.idx;
    if (dragIdx === null || dragIdx === to) return;
    const rect = row.getBoundingClientRect();
    const dropBelow = e.clientY > rect.top + rect.height / 2;
    const [moved] = bmBots.splice(dragIdx, 1);
    if (dragIdx < to) to -= 1;          // indices shifted after removal
    if (dropBelow) to += 1;             // drop on lower half -> place after target
    to = Math.max(0, Math.min(to, bmBots.length));
    bmBots.splice(to, 0, moved);
    bmDirty = true;
    renderBotManager();
  });
}
// ---- Avatar pools: per-bot one-shot thread faces (status + prompt banks) ----
// Server truth only: the panel lists pools whose avatar_pool flag is SAVED, so
// a just-toggled row appears after Save — same moment the pool goes live.
let apPools = null;          // {bot_id: status} from the last fetch / WS frame
let apOpenPrompts = null;    // bot_id whose prompt editor is expanded
async function refreshAvatarPoolPanel() {
  if (state.decoy) return;
  try {
    const r = await api.avatarPools();
    apPools = r.pools || {};
  } catch { apPools = null; }
  renderAvatarPoolPanel();
}
function renderAvatarPoolPanel() {
  const box = dom['avatar-pool-panel'];
  if (!box) return;
  box.innerHTML = '';
  const pools = apPools || {};
  const ids = Object.keys(pools);
  box.classList.toggle('hidden', !ids.length);
  if (!ids.length) return;
  box.append(el('h3', { class: 'ap-title', text: t('settings.pool_section') }));
  ids.forEach((bid) => {
    const p = pools[bid];
    const bot = bmBots.find((b) => b.id === bid);
    let stats = t('settings.pool_stats', { ready: p.ready, target: p.target, spent: p.spent });
    if (p.batch_date) stats += ' · ' + t('settings.pool_last_fill', { date: p.batch_date });
    const row = el('div', { class: 'ap-row' }, [
      el('span', { class: 'ap-name', text: (bot && bot.name) || bid }),
      el('span', { class: 'ap-stats muted', text: stats }),
      p.has_prompts ? null : el('span', { class: 'ap-warn', text: t('settings.pool_no_prompts') }),
      el('button', {
        class: 'bm-avatar-btn', text: t('settings.pool_refill'),
        onclick: async () => {
          try { await api.avatarPoolRefill(bid); toast(t('settings.pool_refill_kicked')); }
          catch (e) { toast(e.message, true); }
        },
      }),
      el('button', {
        class: 'bm-avatar-btn', text: t('settings.pool_prompts'),
        onclick: () => {
          apOpenPrompts = apOpenPrompts === bid ? null : bid;
          renderAvatarPoolPanel();
        },
      }),
    ]);
    box.append(row);
    if (apOpenPrompts === bid) box.append(buildAvatarPromptEditor(bid));
  });
}
function buildAvatarPromptEditor(bid) {
  const base = el('textarea', { class: 'ap-ta', rows: '3', placeholder: t('settings.pool_base_ph') });
  const vars = el('textarea', { class: 'ap-ta', rows: '4', placeholder: t('settings.pool_vars_ph') });
  const wrap = el('div', { class: 'ap-editor' }, [base, vars]);
  api.avatarPoolPrompts(bid).then((r) => {
    base.value = (r.prompts && r.prompts.base) || '';
    vars.value = ((r.prompts && r.prompts.variations) || []).join('\n');
  }).catch((e) => toast(e.message, true));
  wrap.append(el('button', {
    class: 'bm-avatar-btn', text: t('settings.pool_save'),
    onclick: async () => {
      try {
        await api.saveAvatarPoolPrompts(bid, {
          base: base.value.trim(),
          variations: vars.value.split('\n').map((s) => s.trim()).filter(Boolean),
        });
        toast(t('settings.pool_saved'));
        apOpenPrompts = null;
        refreshAvatarPoolPanel();
      } catch (e) { toast(e.message, true); }
    },
  }));
  return wrap;
}

async function saveBotManager() {
  const payload = bmBots.map((b, i) => ({
    id: b.id, order: i, visible: b.visible, safe: !!b.safe, reactions: !!b.reactions,
    avatar_pool: !!b.avatar_pool,
  }));
  try {
    const r = await api.saveOrder(payload);
    state.bots = r.bots.filter((b) => b.visible);
    const selectedHidden = !botById(state.selectedBotId);
    const activeBotHidden = state.activeThread && !botById(state.activeThread.bot_id);
    if (selectedHidden && state.bots.length) state.selectedBotId = state.bots[0].id;
    renderSidebar();
    updateThreadListHeader();
    if (selectedHidden || activeBotHidden) {
      if (state.bots.length) await selectBot(state.selectedBotId);
      else { state.threads = []; renderThreads(); clearChatView(); }
    }
    // Hide only on SUCCESS — a failed save must keep the modal (and the
    // user's edits) on screen instead of silently dropping them.
    bmDirty = false;
    loadReactions(true);          // reaction_bots may have changed
    dom['botmanager-backdrop'].classList.add('hidden');
  } catch (e) { toast(e.message, true); }
}

// ===================== Thread list collapse (desktop) =====================
function setThreadListCollapsed(collapsed) {
  dom.app.classList.toggle('tl-collapsed', collapsed);
  dom['expand-threads'].hidden = !collapsed;
  try { localStorage.setItem('tl-collapsed', collapsed ? '1' : '0'); } catch {}
}

// ===================== Avatar crop modal =====================
// Crop state: displayed image rect + a draggable square. Fractions of the
// source image are sent to the server, which crops from the full-res original.
const crop = { botId: null, file: null, boxX: 0, boxY: 0, boxSize: 0 };

function openCropModal(botId, file) {
  const bot = botById(botId) || (bmBots.find((b) => b.id === botId));
  crop.botId = botId;
  crop.file = file;
  dom['crop-title'].textContent = t('crop.title_for', { name: bot ? bot.name : botId });
  const url = URL.createObjectURL(file);
  dom['crop-img'].onload = () => {
    URL.revokeObjectURL(url);
    initCropBox();
  };
  dom['crop-img'].src = url;
  dom['crop-backdrop'].classList.remove('hidden');
}

function imgRect() {
  // The displayed image rect, relative to the stage.
  const stage = dom['crop-stage'].getBoundingClientRect();
  const img = dom['crop-img'].getBoundingClientRect();
  return { left: img.left - stage.left, top: img.top - stage.top, w: img.width, h: img.height };
}

function initCropBox() {
  const r = imgRect();
  const frac = dom['crop-size'].value / 100;
  crop.boxSize = Math.min(r.w, r.h) * frac;
  crop.boxX = r.left + (r.w - crop.boxSize) / 2;
  crop.boxY = r.top + (r.h - crop.boxSize) / 2;
  applyCropBox();
}

function applyCropBox() {
  const b = dom['crop-box'];
  b.style.width = b.style.height = `${crop.boxSize}px`;
  b.style.left = `${crop.boxX}px`;
  b.style.top = `${crop.boxY}px`;
}

function clampCropBox() {
  const r = imgRect();
  crop.boxSize = Math.min(crop.boxSize, Math.min(r.w, r.h));
  crop.boxX = Math.max(r.left, Math.min(crop.boxX, r.left + r.w - crop.boxSize));
  crop.boxY = Math.max(r.top, Math.min(crop.boxY, r.top + r.h - crop.boxSize));
}

function wireCropModal() {
  const box = dom['crop-box'];
  let dragging = false, lx = 0, ly = 0;
  const down = (x, y) => { dragging = true; lx = x; ly = y; };
  const move = (x, y) => {
    if (!dragging) return;
    crop.boxX += x - lx; crop.boxY += y - ly; lx = x; ly = y;
    clampCropBox(); applyCropBox();
  };
  box.addEventListener('mousedown', (e) => { e.preventDefault(); down(e.clientX, e.clientY); });
  window.addEventListener('mousemove', (e) => move(e.clientX, e.clientY));
  window.addEventListener('mouseup', () => { dragging = false; });
  box.addEventListener('touchstart', (e) => { if (e.touches.length === 1) down(e.touches[0].clientX, e.touches[0].clientY); }, { passive: true });
  box.addEventListener('touchmove', (e) => { if (e.touches.length === 1) { e.preventDefault(); move(e.touches[0].clientX, e.touches[0].clientY); } }, { passive: false });
  box.addEventListener('touchend', () => { dragging = false; });

  dom['crop-size'].addEventListener('input', () => {
    const r = imgRect();
    const cx = crop.boxX + crop.boxSize / 2, cy = crop.boxY + crop.boxSize / 2;
    crop.boxSize = Math.min(r.w, r.h) * (dom['crop-size'].value / 100);
    crop.boxX = cx - crop.boxSize / 2; crop.boxY = cy - crop.boxSize / 2;
    clampCropBox(); applyCropBox();
  });

  const closeCrop = () => { dom['crop-backdrop'].classList.add('hidden'); crop.file = null; };
  dom['crop-close'].addEventListener('click', closeCrop);
  dom['crop-backdrop'].addEventListener('click', (e) => { if (e.target === dom['crop-backdrop']) closeCrop(); });

  dom['crop-save'].addEventListener('click', async () => {
    if (!crop.file || !crop.botId) return;
    const r = imgRect();
    // Fractions of the source image (same for displayed and natural size).
    const fx = (crop.boxX - r.left) / r.w;
    const fy = (crop.boxY - r.top) / r.h;
    const fs = crop.boxSize / Math.min(r.w, r.h);
    dom['crop-save'].disabled = true;
    try {
      await api.uploadAvatar(crop.botId, crop.file, fx, fy, fs);
      toast(t('crop.saved'));
      closeCrop();
      // Refresh Bot Manager or Companions panel if open.
      if (!dom['botmanager-backdrop'].classList.contains('hidden')) {
        const res = await api.allBots(); bmBots = res.bots.slice(); renderBotManager();
      }
      if (!dom['companions-backdrop'].classList.contains('hidden')) {
        renderCompanions();
      }
    } catch (e) { toast(e.message, true); }
    finally { dom['crop-save'].disabled = false; }
  });
}

function pickAvatarFile(botId) {
  const inp = el('input', { type: 'file', accept: 'image/*' });
  inp.addEventListener('change', () => {
    if (inp.files && inp.files[0]) openCropModal(botId, inp.files[0]);
  });
  inp.click();
}

// ===================== File Server =====================
const FILE_ICONS = [
  [/\.(zip|tar|gz|tgz|bz2|xz|7z|rar)$/i, '📦'],
  [/\.(pdf)$/i, '📕'],
  [/\.(txt|md|log|csv|json|ya?ml|xml)$/i, '📝'],
  [/\.(mp3|flac|wav|ogg|m4a|opus)$/i, '🎵'],
  [/\.(py|js|ts|sh|c|cpp|rs|go|java|html|css)$/i, '💻'],
  [/\.(iso|img|qcow2)$/i, '💿'],
  [/\.(apk)$/i, '🤖'],
  [/\.(exe|msi|appimage|deb|rpm|flatpak)$/i, '⚙️'],
];
function fileIcon(name) {
  for (const [re, icon] of FILE_ICONS) if (re.test(name)) return icon;
  return '📄';
}

function openFileServer() {
  dom['botmanager-backdrop'].classList.add('hidden');
  dom['fileserver-backdrop'].classList.remove('hidden');
  renderFileServer();
}

async function renderFileServer() {
  const list = dom['fs-list'];
  list.innerHTML = '';
  let files = [];
  try { files = (await api.files()).files || []; }
  catch (e) { toast(e.message, true); return; }

  if (!files.length) {
    list.append(el('div', { class: 'empty-list' }, [
      el('div', { class: 'empty-emoji', text: '📂' }),
      el('p', { text: t('files.empty') }),
    ]));
    return;
  }

  // Group by local day, newest first (API already sorts created_at DESC).
  const groups = [];
  for (const f of files) {
    const key = dayKey(f.created_at);
    let g = groups[groups.length - 1];
    if (!g || g.key !== key) { g = { key, label: dayLabel(f.created_at), files: [] }; groups.push(g); }
    g.files.push(f);
  }

  groups.forEach((g, gi) => {
    // Count this group + all older groups for the wipe button.
    const olderCount = groups.slice(gi).reduce((n, x) => n + x.files.length, 0);
    // Cutoff = the newest timestamp in this group (inclusive wipe).
    const cutoff = g.files[0].created_at;
    const wipe = el('button', {
      class: 'fs-wipe-btn',
      text: t('files.wipe_button', { count: olderCount }),
      title: t('files.wipe_title'),
      onclick: async () => {
        if (!await uiConfirm(t('files.wipe_confirm', { count: olderCount, day: g.label }), { danger: true })) return;
        try {
          const r = await api.wipeFiles(cutoff);
          toast(t('files.deleted', { count: r.deleted }));
          renderFileServer();
        } catch (e) { toast(e.message, true); }
      },
    });
    list.append(el('div', { class: 'fs-day' }, [
      el('span', { class: 'fs-day-label', text: g.label }),
      wipe,
    ]));

    for (const f of g.files) {
      const mime = f.mime || '';
      const isImage = mime.startsWith('image/') && mime !== 'image/svg+xml';
      const isVideo = mime.startsWith('video/');
      const isText = mime.startsWith('text/') || /\.(txt|md|log|csv|json|ya?ml|xml|py|js|ts|sh|c|cpp|rs|go|java|html|css)$/i.test(f.name);
      const rawUrl = `/api/files/${f.id}/raw`;
      const dlUrl = `/api/files/${f.id}/download`;
      const canPreview = isImage || isVideo || isText;

      // The File Server is full-access only (locked sessions can't reach it), so
      // an unlocked viewer gets real thumbnails + view + download for everything.
      // No-Image Mode is the exception: it must download NOTHING image-like, so
      // an image/video record falls back to a file-type icon instead of building
      // an <img>/<video> whose src fetches on construction. Download links still
      // work — NIM omits pictures, it doesn't lock the drive.
      let thumb;
      if (isImage && !nimEnabled()) {
        thumb = el('div', { class: 'fs-thumb' },
                   [setFullRes(el('img', { src: rawUrl, alt: f.name, loading: 'lazy' }), rawUrl)]);
        thumb.style.cursor = 'zoom-in';
        thumb.onclick = () => openLightbox(rawUrl, { downloadUrl: dlUrl, downloadName: f.name });
      } else if (isVideo && !nimEnabled()) {
        thumb = el('div', { class: 'fs-thumb' }, [el('video', { src: rawUrl, muted: '', playsinline: '', preload: 'metadata' })]);
        thumb.style.cursor = 'zoom-in';
        thumb.onclick = () => openLightbox(rawUrl, { video: true, downloadUrl: dlUrl, downloadName: f.name });
      } else {
        thumb = el('div', { class: 'fs-thumb fs-thumb-icon', text: fileIcon(f.name) });
      }

      const actions = [];
      if (canPreview && !isImage && !isVideo) {
        // Text/doc preview opens the raw endpoint in a new tab.
        actions.push(el('a', { class: 'fs-act-btn', text: '👁', title: t('files.view'), href: rawUrl, target: '_blank', rel: 'noopener' }));
      }
      actions.push(el('a', { class: 'fs-act-btn', text: '⬇', title: t('files.download'), href: dlUrl, download: f.name }));
      actions.push(el('button', {
        class: 'fs-del-btn', text: '✕', title: t('files.delete'),
        onclick: async () => {
          try { await api.deleteFile(f.id); renderFileServer(); }
          catch (e) { toast(e.message, true); }
        },
      }));

      const row = el('div', { class: 'fs-row' }, [
        thumb,
        el('div', { class: 'fs-info' }, [
          el('div', { class: 'fs-name', text: f.name, title: f.name }),
          el('div', { class: 'fs-detail', text: `${fileSize(f.size)} · ${clockTime(f.created_at)}${mime ? ' · ' + mime : ''}` }),
        ]),
        el('div', { class: 'fs-actions' }, actions),
      ]);
      list.append(row);
    }
  });
}

// ===================== Locked-side one-way drop =====================
// The only upload entry point a Safe-Mode device has of its own. Nothing can be
// read back from that device, so this sheet is the sender's ONLY confirmation
// that a file arrived — it keeps a durable per-file row instead of a transient
// toast, and reports each failure against the file it belongs to.

let dropBusy = 0;

function dropRow(file) {
  const bar = el('i');
  const detail = el('div', { class: 'drop-detail', text: t('drop.waiting') });
  const row = el('div', { class: 'drop-row' }, [
    el('div', { class: 'drop-icon', text: fileIcon(file.name) }),
    el('div', { class: 'drop-info' }, [
      el('div', { class: 'drop-name', text: file.name, title: file.name }),
      detail,
      el('div', { class: 'drop-bar' }, [bar]),
    ]),
  ]);
  dom['drop-list'].append(row);
  return {
    progress(pct) { bar.style.width = `${pct}%`; detail.textContent = t('drop.sending', { percent: fmtPercent(pct) }); },
    ok(res) {
      row.classList.add('done');
      row.querySelector('.drop-icon').textContent = '✅';
      detail.textContent = t(res && res.notice === false ? 'drop.sent_no_thread' : 'drop.sent',
                             { size: fileSize(file.size) });
    },
    fail(msg) {
      row.classList.add('failed');
      row.querySelector('.drop-icon').textContent = '⚠️';
      // Strip the leading "413: " status the API client prefixes — the sender
      // needs the sentence, not the status code.
      detail.textContent = String(msg || t('drop.failed')).replace(/^\d{3}:\s*/, '');
    },
  };
}

async function sendDrops(files) {
  if (!files.length) return;
  const empty = dom['drop-list'].querySelector('.drop-empty');
  if (empty) empty.remove();
  dropBusy += files.length;
  updateDropBusy();
  for (const f of files) {
    const row = dropRow(f);
    try {
      const res = await api.drop(f, state.activeThreadId || undefined,
        (loaded, total) => row.progress(total ? Math.floor((loaded / total) * 100) : 0));
      row.ok(res);
    } catch (e) {
      row.fail(e.message);
    } finally {
      dropBusy--;
      updateDropBusy();
    }
  }
}

function updateDropBusy() {
  // Closing mid-upload would hide the only feedback the sender gets, and the
  // XHR would keep running invisibly — so the sheet holds until every file
  // settles. The picker is disabled meanwhile to keep the queue predictable.
  const busy = dropBusy > 0;
  dom['drop-done'].disabled = busy;
  dom['drop-more'].disabled = busy;
  dom['drop-done'].textContent = t(busy ? 'drop.sending_short' : 'common.done');
}

function openDrop() {
  dom['drop-list'].replaceChildren(
    el('div', { class: 'drop-empty', text: t('drop.empty') }));
  dropBusy = 0;
  updateDropBusy();
  dom['drop-backdrop'].classList.remove('hidden');
  dom['drop-input'].click();
}

function closeDrop() {
  if (dropBusy > 0) return;   // never strand an in-flight upload
  dom['drop-backdrop'].classList.add('hidden');
  dom['drop-list'].replaceChildren();
}

function wireDrop() {
  dom['drop-btn'].addEventListener('click', openDrop);
  dom['drop-more'].addEventListener('click', () => dom['drop-input'].click());
  dom['drop-close'].addEventListener('click', closeDrop);
  dom['drop-done'].addEventListener('click', closeDrop);
  dom['drop-backdrop'].addEventListener('click', (e) => {
    if (e.target === dom['drop-backdrop']) closeDrop();
  });
  dom['drop-input'].addEventListener('change', (e) => {
    const files = [...e.target.files];
    e.target.value = '';          // re-picking the same file must re-fire
    if (files.length) sendDrops(files);
  });
}

async function uploadToFileServer(files) {
  let done = 0;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    const label = files.length > 1
      ? t('files.upload_progress_label', { index: i + 1, total: files.length, name: f.name })
      : f.name;
    try {
      // Live per-file progress. toast() resets its own auto-hide timer on every
      // call, so the bar stays visible for the whole (possibly long) upload.
      await api.uploadFile(f, (loaded, total) => {
        const pct = total ? Math.floor((loaded / total) * 100) : 0;
        toast(t('files.uploading', { name: label, percent: fmtPercent(pct) }));
      });
      done++;
    } catch (e) { toast(t('files.named_error', { name: f.name, error: e.message }), true); }
  }
  if (done) toast(t('files.uploaded', { count: done }));
  renderFileServer();
}

function wireFileServer() {
  dom['fs-chip'].addEventListener('click', openFileServer);
  dom['fs-close'].addEventListener('click', () => dom['fileserver-backdrop'].classList.add('hidden'));
  dom['fileserver-backdrop'].addEventListener('click', (e) => {
    if (e.target === dom['fileserver-backdrop']) dom['fileserver-backdrop'].classList.add('hidden');
  });
  dom['fs-upload-btn'].addEventListener('click', () => dom['fs-file-input'].click());
  dom['fs-file-input'].addEventListener('change', (e) => {
    uploadToFileServer([...e.target.files]); e.target.value = '';
  });
  const modal = dom['fileserver-backdrop'].querySelector('.fs-modal');
  modal.addEventListener('dragover', (e) => { e.preventDefault(); modal.classList.add('drop-hot'); });
  modal.addEventListener('dragleave', () => modal.classList.remove('drop-hot'));
  modal.addEventListener('drop', (e) => {
    e.preventDefault(); modal.classList.remove('drop-hot');
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) uploadToFileServer(files);
  });
}

// ===================== ComfyUI service panel =====================
let comfyPanelOpen = false;
let comfyPollTimer = null;
let comfyLaunchBusy = false;
let comfyFlagsDraft = {};
let comfyKnownGood = {};

function comfyStateFromStatus(cs) {
  if (cs.healthy) return 'running';
  const as = cs.unit && cs.unit.active_state;
  if (as === 'activating') return 'starting';
  // Active-but-unhealthy WITH dirty flags = the running instance predates a
  // saved flags change (e.g. a new port), so the health probe hits the wrong
  // target. It's not starting — it's running with stale flags; a restart
  // applies them. Without this, a healthy service shows 'Starting…' forever.
  if (as === 'active' && cs.flags_dirty) return 'running';
  // Type=simple: the unit is 'active' the instant the process spawns, but
  // ComfyUI takes up to ~60s to answer /system_stats on a cold start. Show
  // that window as amber "Starting…", not grey "Stopped".
  if (as === 'active') return 'starting';
  if (as === 'deactivating') return 'stopping';
  if (as === 'failed') return 'error';
  return 'stopped';
}

function renderComfyChip() {
  const dot = dom['comfy-dot'];
  if (dot) dot.className = 'comfy-dot ' + state.comfy.state;
}

function applyComfyStatus(cs) {
  state.comfy.state = comfyStateFromStatus(cs);
  state.comfy.gatewayOn = !!(cs.gateway && cs.gateway.on);
  state.comfy.flagsDirty = !!cs.flags_dirty;
  renderComfyChip();
}

// Prefer unit.start_epoch (clean int, added server-side) — systemd's locale
// string ('Tue 2026-07-07 14:26:59 JST') is unparseable by new Date() in V8,
// so the string is only a fallback for pre-restart backends / weird payloads.
function comfyUptime(unit) {
  const epoch = unit && unit.start_epoch;
  if (typeof epoch === 'number' && isFinite(epoch) && epoch > 0) {
    return relTime(new Date(epoch * 1000).toISOString());
  }
  const startStr = unit && unit.start_timestamp;
  if (!startStr) return '';
  const d = new Date(startStr);
  return isNaN(d.getTime()) ? startStr : relTime(d.toISOString());
}

async function copyComfyUrl(url) {
  try { await navigator.clipboard.writeText(url); toast(t('comfy.url_copied')); }
  catch { toast(t('comfy.copy_failed'), true); }
}

function renderComfyStatusCard(cs) {
  const box = dom['comfy-status'];
  if (!box) return;
  box.innerHTML = '';
  const unit = cs.unit || {};
  const stateKeys = { running: 'comfy.state_running', starting: 'comfy.state_starting',
                      stopping: 'comfy.state_stopping', stopped: 'comfy.state_stopped',
                      error: 'comfy.state_error' };
  // Saved-but-unapplied flags on a live unit: say what's actually going on
  // instead of a perpetual 'Starting…' (the probe targets the NEW config).
  const staleFlags = !!cs.flags_dirty && unit.active_state === 'active' && !cs.healthy;
  const rows = [
    el('div', { class: 'comfy-row' }, [
      el('span', { class: 'comfy-row-label', text: t('comfy.row_status') }),
      el('span', { class: 'comfy-row-val comfy-state-' + state.comfy.state,
                   text: staleFlags ? t('comfy.state_stale')
                     : (stateKeys[state.comfy.state] ? t(stateKeys[state.comfy.state])
                        : (unit.active_state || t('common.unknown'))) }),
    ]),
  ];
  if (unit.start_epoch || unit.start_timestamp) {
    rows.push(el('div', { class: 'comfy-row' }, [
      el('span', { class: 'comfy-row-label', text: t('comfy.row_uptime') }),
      el('span', { class: 'comfy-row-val', text: comfyUptime(unit),
                   title: unit.start_timestamp || '' }),
    ]));
  }
  rows.push(el('div', { class: 'comfy-row' }, [
    el('span', { class: 'comfy-row-label', text: t('comfy.row_restarts') }),
    el('span', { class: 'comfy-row-val', text: String(unit.n_restarts ?? 0) }),
  ]));
  const dev = cs.stats && Array.isArray(cs.stats.devices) && cs.stats.devices[0];
  if (dev && dev.vram_total) {
    // Numbers, not toFixed() strings: t() runs numeric vars through
    // Intl.NumberFormat, so the decimal separator follows the locale.
    const used = (dev.vram_total - dev.vram_free) / 1073741824;
    const total = dev.vram_total / 1073741824;
    rows.push(el('div', { class: 'comfy-row' }, [
      el('span', { class: 'comfy-row-label', text: t('comfy.row_vram') }),
      el('span', { class: 'comfy-row-val', text: t('comfy.memory_value', { used: gb(used), total: gb(total) }) }),
    ]));
  }
  const sys = cs.stats && cs.stats.system;
  if (sys && sys.ram_total) {
    const used = (sys.ram_total - sys.ram_free) / 1073741824;
    const total = sys.ram_total / 1073741824;
    rows.push(el('div', { class: 'comfy-row' }, [
      el('span', { class: 'comfy-row-label', text: t('comfy.row_ram') }),
      el('span', { class: 'comfy-row-val', text: t('comfy.memory_value', { used: gb(used), total: gb(total) }) }),
    ]));
  }
  const gw = cs.gateway || {};
  rows.push(el('div', { class: 'comfy-row' }, [
    el('span', { class: 'comfy-row-label', text: t('comfy.row_gateway') }),
    el('span', { class: 'comfy-row-val' + (gw.error ? ' comfy-state-error' : ''),
                 text: gw.error ? t('comfy.gateway_unavailable') : t(gw.on ? 'common.on' : 'common.off'),
                 title: gw.error || '' }),
  ]));
  box.append(...rows);
  if (gw.on && gw.url) {
    box.append(el('div', { class: 'comfy-url-row' }, [
      el('a', { class: 'comfy-url-link', href: gw.url, target: '_blank', rel: 'noopener', text: gw.url }),
      el('button', { class: 'comfy-copy-btn', text: '⧉', title: t('comfy.copy_url'), onclick: () => copyComfyUrl(gw.url) }),
    ]));
  }

  // Explicit verb so the button reads as an ACTION, not a state (it sits right
  // next to the 'Gateway: On/Off' status row). Neutral + disabled while the
  // gateway itself is unavailable (toggling it would just error).
  const gwBtn = dom['comfy-gateway-toggle'];
  gwBtn.textContent = gw.error ? t('comfy.gateway_menu') : t(gw.on ? 'comfy.gateway_off' : 'comfy.gateway_on');
  gwBtn.disabled = !!gw.error;
  dom['comfy-dirty-banner'].classList.toggle('hidden', !cs.flags_dirty);

  // 'starting' covers both a genuine cold start AND a hung active-but-unhealthy
  // service — Stop/Restart must stay available so a stuck start can be killed.
  const s = state.comfy.state;
  dom['comfy-start'].disabled = s === 'running' || s === 'starting' || s === 'stopping';
  dom['comfy-stop'].disabled = s === 'stopped' || s === 'stopping';
  dom['comfy-restart'].disabled = s === 'stopping';
}

async function refreshComfyPanel() {
  try {
    const cs = await api.comfyServiceStatus();
    applyComfyStatus(cs);
    renderComfyStatusCard(cs);
  } catch (e) { /* transient poll failure — keep last known state on screen */ }
}

function openComfyPanel() {
  if (state.decoy || !state.comfyEnabled) return;
  comfyPanelOpen = true;
  dom['comfy-backdrop'].classList.remove('hidden');
  refreshComfyPanel();
  loadComfyFlags();
  refreshComfyWorkflows();
  clearInterval(comfyPollTimer);
  comfyPollTimer = setInterval(refreshComfyPanel, 5000);
}
function closeComfyPanel() {
  comfyPanelOpen = false;
  dom['comfy-backdrop'].classList.add('hidden');
  clearInterval(comfyPollTimer); comfyPollTimer = null;
}

async function comfyAction(action) {
  // Disable immediately — the request itself can take up to ~60s (cold start
  // health wait), and a stale-state re-click mid-transition would race it.
  dom['comfy-start'].disabled = true;
  dom['comfy-stop'].disabled = true;
  dom['comfy-restart'].disabled = true;
  try { await api.comfyServiceAction(action); }
  catch (e) { toast(cleanErr(e), true); }
  await refreshComfyPanel();   // resolves real button state from the outcome
}

async function toggleComfyGateway() {
  try { await api.comfyGateway(!state.comfy.gatewayOn); }
  catch (e) { toast(cleanErr(e), true); }
  await refreshComfyPanel();
}

function openComfyLogs() {
  dom['comfy-logs-backdrop'].classList.remove('hidden');
  refreshComfyLogs();
}
function closeComfyLogs() { dom['comfy-logs-backdrop'].classList.add('hidden'); }
async function refreshComfyLogs() {
  try {
    const r = await api.comfyLogs(200);
    dom['comfy-logs-content'].textContent = (r.lines || []).join('\n');
    dom['comfy-logs-content'].scrollTop = dom['comfy-logs-content'].scrollHeight;
  } catch (e) { dom['comfy-logs-content'].textContent = t('comfy.logs_failed', { error: e.message }); }
}

// ---- Workflow manager (list / download / import / trash / backup) ----
// Source of truth is ComfyUI's own workflows dir; the backend does the file
// ops. Only reachable from the panel, which is decoy-gated already.
let comfyWorkflows = [];

function comfyWfEmpty(text) {
  const box = dom['comfy-wf-list'];
  box.innerHTML = '';
  box.append(el('div', { class: 'comfy-wf-empty', text }));
}

async function refreshComfyWorkflows() {
  try {
    const r = await api.comfyWorkflows();
    comfyWorkflows = r.workflows || [];
    renderComfyWorkflows(r);
  } catch (e) {
    comfyWorkflows = [];
    dom['comfy-wf-hint'].textContent = '';
    // 404 = the routes ship with a pending service update — say so, don't scare.
    comfyWfEmpty(e.status === 404
      ? t('comfy.wf_unavailable')
      : t('comfy.wf_load_failed', { error: cleanErr(e) }));
  }
}

function renderComfyWorkflows(r) {
  const hint = dom['comfy-wf-hint'];
  const epoch = r.last_backup_epoch;
  hint.textContent = (typeof epoch === 'number' && epoch > 0)
    ? t('comfy.wf_last_backup', { when: relTime(new Date(epoch * 1000).toISOString()) })
    : t('comfy.wf_never_backed_up');
  const box = dom['comfy-wf-list'];
  box.innerHTML = '';
  if (!comfyWorkflows.length) {
    comfyWfEmpty(t('comfy.wf_empty'));
    return;
  }
  for (const wf of comfyWorkflows) {
    const display = wf.name.replace(/\.json$/i, '');
    const modified = (typeof wf.modified_epoch === 'number' && wf.modified_epoch > 0)
      ? relTime(new Date(wf.modified_epoch * 1000).toISOString()) : '';
    box.append(el('div', { class: 'comfy-wf-row' }, [
      el('div', { class: 'comfy-wf-info' }, [
        el('div', { class: 'comfy-wf-name', text: display, title: wf.name }),
        el('div', { class: 'comfy-wf-meta', text: [modified, fileSize(wf.size)].filter(Boolean).join(' · ') }),
      ]),
      el('a', { class: 'fs-act-btn', text: '⬇', title: t('common.download'), 'aria-label': t('comfy.wf_download_aria', { name: display }),
                href: api.comfyWorkflowUrl(wf.name), download: wf.name }),
      el('button', {
        class: 'fs-del-btn', text: '🗑', title: t('comfy.wf_trash'), 'aria-label': t('comfy.wf_trash_aria', { name: display }),
        onclick: async () => {
          if (!await uiConfirm(t('comfy.wf_trash_confirm', { name: display }), { danger: true, okText: t('comfy.wf_trash_ok') })) return;
          try { await api.comfyDeleteWorkflow(wf.name); toast(t('comfy.wf_trashed', { name: display })); }
          catch (e) { toast(cleanErr(e), true); }
          refreshComfyWorkflows();
        },
      }),
    ]));
  }
}

async function importComfyWorkflow(file) {
  if (!file) return;
  let name = (file.name || '').trim();
  if (!/\.json$/i.test(name)) { toast(t('comfy.wf_must_be_json'), true); return; }
  if (file.size > 5 * 1024 * 1024) { toast(t('comfy.wf_too_large'), true); return; }
  if (comfyWorkflows.some((w) => w.name === name)) {
    const ok = await uiConfirm(t('comfy.wf_overwrite', { name }), { okText: t('common.overwrite') });
    if (!ok) return;
  }
  try {
    const text = await file.text();
    await api.comfyImportWorkflow(name, text);
    toast(t('comfy.wf_imported', { name }));
  } catch (e) { toast(t('comfy.wf_import_failed', { error: cleanErr(e) }), true); }
  refreshComfyWorkflows();
}

async function backupComfyWorkflows() {
  dom['comfy-wf-backup'].disabled = true;
  try {
    const r = await api.comfyBackupWorkflows();
    toast(t('comfy.wf_backed_up', { count: r.count }));
  } catch (e) { toast(t('comfy.wf_backup_failed', { error: cleanErr(e) }), true); }
  finally { dom['comfy-wf-backup'].disabled = false; }
  refreshComfyWorkflows();
}

// Rendered directly from the backend schema (GET .../flags) — no client-side
// knowledge of individual flag names, so the control set stays in one place.
function comfyControlEl(spec, value) {
  const wrap = el('div', { class: 'comfy-flag-row' });
  let input;
  if (spec.kind === 'bool') {
    input = el('input', { class: 'comfy-flag-input', type: 'checkbox' });
    input.checked = !!value;
    wrap.append(el('label', { class: 'comfy-flag-label' }, [input, document.createTextNode(' ' + spec.label)]));
    input.addEventListener('change', () => { comfyFlagsDraft[spec.key] = input.checked; });
  } else {
    wrap.append(el('label', { class: 'comfy-flag-label', text: spec.label }));
    if (spec.kind === 'enum') {
      input = el('select', { class: 'comfy-flag-input' });
      spec.choices.forEach((c) => {
        const opt = el('option', { value: c, text: c === '' ? t('comfy.flags_default') : c });
        if (c === value) opt.selected = true;
        input.append(opt);
      });
      input.addEventListener('change', () => { comfyFlagsDraft[spec.key] = input.value; });
    } else if (spec.kind === 'int' || spec.kind === 'float') {
      input = el('input', { class: 'comfy-flag-input', type: 'number', step: spec.kind === 'int' ? '1' : 'any' });
      if (spec.min != null) input.min = String(spec.min);
      if (spec.max != null) input.max = String(spec.max);
      input.value = value == null ? '' : value;
      input.addEventListener('change', () => {
        if (input.value === '') { comfyFlagsDraft[spec.key] = spec.kind === 'int' ? spec.default : null; return; }
        comfyFlagsDraft[spec.key] = spec.kind === 'int' ? parseInt(input.value, 10) : parseFloat(input.value);
      });
    } else {
      input = el('input', { class: 'comfy-flag-input', type: 'text' });
      input.value = value || '';
      input.addEventListener('change', () => { comfyFlagsDraft[spec.key] = input.value; });
    }
    wrap.append(input);
  }
  if (spec.note) wrap.append(el('div', { class: 'comfy-flag-note', text: spec.note }));
  return wrap;
}

function renderComfyFlagsForm(schema, values, unmanaged) {
  const box = dom['comfy-flags-list'];
  box.innerHTML = '';
  (schema || []).forEach((spec) => box.append(comfyControlEl(spec, values[spec.key])));
  const entries = Object.entries(unmanaged || {});
  if (entries.length) {
    box.append(el('div', { class: 'comfy-unmanaged-head', text: t('comfy.flags_unmanaged') }));
    entries.forEach(([k, v]) => {
      box.append(el('div', { class: 'comfy-flag-row comfy-unmanaged-row' }, [
        el('span', { class: 'comfy-flag-label', text: k }),
        el('span', { class: 'comfy-flag-readonly', text: v }),
      ]));
    });
  }
}

async function loadComfyFlags() {
  try {
    const r = await api.comfyFlags();
    comfyFlagsDraft = { ...r.values };
    comfyKnownGood = r.known_good || {};
    renderComfyFlagsForm(r.schema, r.values, r.unmanaged);
  } catch (e) { toast(t('comfy.flags_load_failed', { error: e.message }), true); }
}

async function saveComfyFlags(restart) {
  if (restart) {
    dom['comfy-start'].disabled = true;
    dom['comfy-stop'].disabled = true;
    dom['comfy-restart'].disabled = true;
  }
  try {
    await api.comfySaveFlags(comfyFlagsDraft, restart);
    toast(t(restart ? 'comfy.flags_saved_restarted' : 'comfy.flags_saved'));
    await loadComfyFlags();
  } catch (e) { toast(cleanErr(e), true); }
  await refreshComfyPanel();
}

async function resetComfyKnownGood() {
  if (!await uiConfirm(t('comfy.flags_reset_confirm'),
                        { okText: t('comfy.flags_reset_ok'), danger: true })) return;
  dom['comfy-start'].disabled = true;
  dom['comfy-stop'].disabled = true;
  dom['comfy-restart'].disabled = true;
  try {
    await api.comfySaveFlags(comfyKnownGood, true);
    toast(t('comfy.flags_reset_done'));
    await loadComfyFlags();
  } catch (e) { toast(cleanErr(e), true); }
  await refreshComfyPanel();
}

// ---- Desktop notifications (Web Notifications API) ----
// True OS-level popups so the user knows ComfyUI is ready even when DisPatch
// isn't the focused tab. Degrades silently where unsupported/denied — the
// in-app launch banner is always the primary, permission-free feedback.
let comfyNotifyAsked = false;
function notifySupported() { return 'Notification' in window; }
function ensureNotifyPermission() {
  // Called from within the launch click (a user gesture) so the browser
  // permission prompt is allowed. Fire-and-forget; we never block on it.
  if (!notifySupported()) return;
  if (Notification.permission !== 'default' || comfyNotifyAsked) return;
  comfyNotifyAsked = true;
  try { Notification.requestPermission().catch(() => {}); } catch { /* older API */ }
}
async function desktopNotify(title, body, { tag = 'dispatch', silent = false } = {}) {
  try {
    if (!notifySupported() || Notification.permission !== 'granted') return;
    const opts = { body, tag, renotify: true, silent, icon: '/static/icon-192.png', badge: '/static/favicon-32.png' };
    // Mobile Chrome forbids `new Notification()` — it requires the SW path.
    const reg = navigator.serviceWorker && await navigator.serviceWorker.getRegistration();
    if (reg && reg.showNotification) { await reg.showNotification(title, opts); return; }
    new Notification(title, opts);
  } catch { /* ignore — banner already covers it */ }
}

// ---- In-app launch banner ----
let comfyLaunchHideTimer = null;
const COMFY_LAUNCH_GLYPH = { starting: '🖼', ready: '✅', error: '⚠️' };
function showComfyLaunch(phase, title, hint) {
  const box = dom['comfy-launch'];
  if (!box) return;
  clearTimeout(comfyLaunchHideTimer);
  box.classList.remove('hidden', 'starting', 'ready', 'error', 'clickable');
  box.classList.add(phase);
  delete box.dataset.url;
  delete box.dataset.panel;
  dom['comfy-launch-glyph'].textContent = COMFY_LAUNCH_GLYPH[phase] || '🖼';
  dom['comfy-launch-title'].textContent = title;
  dom['comfy-launch-hint'].textContent = hint || '';
  if (phase !== 'starting') dom['comfy-launch-secs'].textContent = '';
}
function hideComfyLaunch() {
  clearTimeout(comfyLaunchHideTimer);
  const box = dom['comfy-launch'];
  if (box) { box.classList.add('hidden'); delete box.dataset.url; delete box.dataset.panel; box.classList.remove('clickable'); }
}

// The chip IS the primary action: click launches (start if needed, ensure the
// gateway, open the tab); long-press/right-click opens the control panel.
async function comfyLaunchFlow() {
  if (state.decoy || !state.comfyEnabled || comfyLaunchBusy) return;
  comfyLaunchBusy = true;
  const chip = dom['comfy-chip'];
  chip.classList.add('busy');
  chip.setAttribute('aria-busy', 'true');
  // Reflect "starting" on the chip's own status dot immediately (amber pulse)
  // instead of leaving the stale pre-click colour for the whole cold start.
  state.comfy.state = 'starting';
  renderComfyChip();
  // Ask for desktop-notification permission on this user gesture (first launch
  // only); the banner works regardless of the answer.
  ensureNotifyPermission();
  const startedAt = Date.now();
  showComfyLaunch('starting', t('comfy.launch_starting'), t('comfy.launch_hint'));
  announce(t('comfy.launch_announce_start'), true);
  const elTimer = setInterval(() => {
    const s = Math.round((Date.now() - startedAt) / 1000);
    dom['comfy-launch-secs'].textContent = t('comfy.launch_seconds', { seconds: s });
    chip.title = t('comfy.launch_chip_busy', { seconds: s });
  }, 1000);
  // Open a blank tab SYNCHRONOUSLY in the click handler so popup blockers
  // (mobile Safari/Firefox especially) don't treat the post-await open as an
  // untrusted navigation. Paint an interim holding page so it doesn't read as
  // a broken blank tab during the (up to ~60s) cold start.
  const win = window.open('', '_blank');
  if (win) { try { win.document.write(comfyHoldingPage()); win.document.close(); } catch { /* opaque */ } }
  try {
    const r = await api.comfyLaunch();
    // The launch can take up to ~60s; if the session locked mid-flight, the
    // now-Safe-Mode tab must not be handed the gateway URL.
    if (state.decoy) { if (win) win.close(); hideComfyLaunch(); return; }
    const secs = Math.round((Date.now() - startedAt) / 1000);
    if (win) {
      showComfyLaunch('ready', t('comfy.launch_ready'), t('comfy.launch_opening'));
      win.location.href = r.url;
      comfyLaunchHideTimer = setTimeout(hideComfyLaunch, 4000);
    } else {
      // Popup blocked — turn the ready banner into the open affordance.
      showComfyLaunch('ready', t('comfy.launch_ready'), t('comfy.launch_tap'));
      const box = dom['comfy-launch'];
      box.classList.add('clickable');
      box.dataset.url = r.url;
    }
    announce(t('comfy.launch_announce_ready'), true);
    desktopNotify(t('comfy.launch_ready'), t('comfy.launch_notify_ready', { seconds: secs }), { tag: 'comfy-launch' });
  } catch (e) {
    if (win) win.close();
    showComfyLaunch('error', t('comfy.launch_failed'), cleanErr(e));
    const box = dom['comfy-launch'];
    box.classList.add('clickable');       // click opens the control panel + logs
    box.dataset.panel = '1';
    announce(t('comfy.launch_announce_failed'), true);
    desktopNotify(t('comfy.launch_announce_failed'), cleanErr(e), { tag: 'comfy-launch' });
    comfyLaunchHideTimer = setTimeout(hideComfyLaunch, 10000);
  } finally {
    clearInterval(elTimer);
    chip.title = t('comfy.chip_title');
    chip.classList.remove('busy');
    chip.removeAttribute('aria-busy');
    comfyLaunchBusy = false;
    refreshComfyChipOnce();
  }
}
async function refreshComfyChipOnce() {
  try { applyComfyStatus(await api.comfyServiceStatus()); } catch { /* ignore */ }
}

// Static holding page shown in the freshly-opened tab while ComfyUI cold-starts.
// Self-contained (no network) so it paints instantly even before the gateway is
// up; the real ComfyUI URL replaces it once launch() resolves.
// A function rather than a constant so its copy is resolved in the language
// that is active AT LAUNCH, and so the new tab inherits this document's lang/dir
// (an RTL session must not get an LTR holding page).
function comfyHoldingPage() {
  const root = document.documentElement;
  const lang = escapeHtml(root.getAttribute('lang') || 'en');
  const dir = escapeHtml(root.getAttribute('dir') || 'ltr');
  const title = escapeHtml(t('comfy.launch_starting'));
  const hint = escapeHtml(t('comfy.holding_page_hint'));
  return `<!doctype html><html lang="${lang}" dir="${dir}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title>
<style>html,body{height:100%;margin:0}body{display:flex;flex-direction:column;align-items:center;
justify-content:center;gap:18px;font:16px/1.5 system-ui,sans-serif;background:#0f0f1a;color:#e8e8f0}
.g{font-size:56px}.s{width:34px;height:34px;border:3px solid rgba(255,255,255,.18);
border-top-color:#8b7cff;border-radius:50%;animation:sp .8s linear infinite}
.h{color:#9a9ab0;font-size:13px}@keyframes sp{to{transform:rotate(360deg)}}</style></head>
<body><div class="g">🖼</div><div class="s"></div><div>${title}</div>
<div class="h">${hint}</div></body></html>`;
}

function wireComfyPanel() {
  let pressTimer = null;
  let longPressed = false;
  dom['comfy-chip'].addEventListener('pointerdown', () => {
    longPressed = false;
    pressTimer = setTimeout(() => { longPressed = true; openComfyPanel(); }, 550);
  });
  ['pointerup', 'pointerleave', 'pointercancel'].forEach((ev) =>
    dom['comfy-chip'].addEventListener(ev, () => {
      clearTimeout(pressTimer);
      // The suppressed click (if any) fires synchronously right after
      // pointerup; if the release landed OFF the chip (finger slid away, or
      // the just-opened panel backdrop swallowed it) no click ever comes, and
      // a stale longPressed=true would eat the NEXT tap. Reset a beat later.
      if (longPressed) setTimeout(() => { longPressed = false; }, 80);
    }));
  dom['comfy-chip'].addEventListener('click', () => {
    if (longPressed) { longPressed = false; return; }
    comfyLaunchFlow();
  });
  dom['comfy-chip'].addEventListener('contextmenu', (e) => { e.preventDefault(); openComfyPanel(); });

  dom['comfy-close'].addEventListener('click', closeComfyPanel);
  dom['comfy-backdrop'].addEventListener('click', (e) => { if (e.target === dom['comfy-backdrop']) closeComfyPanel(); });
  dom['comfy-start'].addEventListener('click', () => comfyAction('start'));
  dom['comfy-stop'].addEventListener('click', () => comfyAction('stop'));
  dom['comfy-restart'].addEventListener('click', () => comfyAction('restart'));
  dom['comfy-gateway-toggle'].addEventListener('click', toggleComfyGateway);
  dom['comfy-logs-btn'].addEventListener('click', openComfyLogs);
  dom['comfy-logs-close'].addEventListener('click', closeComfyLogs);
  dom['comfy-logs-backdrop'].addEventListener('click', (e) => { if (e.target === dom['comfy-logs-backdrop']) closeComfyLogs(); });
  dom['comfy-logs-refresh'].addEventListener('click', refreshComfyLogs);
  dom['comfy-wf-import'].addEventListener('click', () => dom['comfy-wf-file'].click());
  dom['comfy-wf-file'].addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    importComfyWorkflow(f);
  });
  dom['comfy-wf-backup'].addEventListener('click', backupComfyWorkflows);
  dom['comfy-flags-save'].addEventListener('click', () => saveComfyFlags(false));
  dom['comfy-flags-save-restart'].addEventListener('click', () => saveComfyFlags(true));
  dom['comfy-flags-reset'].addEventListener('click', resetComfyKnownGood);

  // Launch banner: dismiss button, and click-to-act when it's an affordance
  // (ready-but-popup-blocked → open ComfyUI; error → open the control panel).
  dom['comfy-launch-dismiss'].addEventListener('click', (e) => { e.stopPropagation(); hideComfyLaunch(); });
  dom['comfy-launch'].addEventListener('click', () => {
    const box = dom['comfy-launch'];
    if (box.dataset.url) { window.open(box.dataset.url, '_blank', 'noopener'); hideComfyLaunch(); }
    else if (box.dataset.panel) { hideComfyLaunch(); openComfyPanel(); openComfyLogs(); }
  });
}

// ===================== Coding terminal =====================
// A server-side PTY running the configured coding CLI, mirrored over /ws/terminal into
// an xterm.js instance. Selecting the terminal pseudo-bot replaces the chat
// pane with the terminal; switching away detaches (the session keeps running,
// reattach replays scrollback). Full-session only — the pseudo-bot never shows
// in Safe Mode and the server 403s/refuses every terminal surface there.
let terminalOpen = false;
let termInstance = null;
let termFit = null;
let termSock = null;
let termReconnectTimer = null;
let termReconnectDelay = 500;
let termResizeObs = null;
let termSearch = null;   // xterm search addon (find box)
let termEarlyFits = 0;   // remaining "refit on next output frame" passes

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function termTheme() {
  // Our palette vars use light-dark()/color-mix(), so reading the raw custom
  // property yields a literal "light-dark(...)" string that xterm can't parse —
  // it silently falls back to BLACK (invisible on the dark app, a glaring black
  // box on the light one). Apply each var to a probe element and read back the
  // COMPUTED colour, which resolves against the active color-scheme to real rgb.
  const dark = document.documentElement.getAttribute('data-theme') !== 'light';
  const probe = document.createElement('span');
  probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none';
  document.body.appendChild(probe);
  const resolve = (varName, fb) => {
    probe.style.color = 'transparent';
    probe.style.color = `var(${varName})`;
    const c = getComputedStyle(probe).color;
    return (c && c !== 'transparent' && c !== 'rgba(0, 0, 0, 0)') ? c : fb;
  };
  const theme = {
    background: resolve('--bg-tertiary', dark ? '#1e1e3a' : '#ffffff'),
    foreground: resolve('--text-primary', dark ? '#e8e8f0' : '#1c1c28'),
    cursor: resolve('--accent', '#7c3aed'),
    selectionBackground: resolve('--accent-muted', 'rgba(124,58,237,.3)'),
  };
  probe.remove();
  return theme;
}

// Accepts a status/frame object {state, options, pending_options}. A bare
// string is tolerated (older call sites) and treated as the state only.
function applyTerminalStatus(obj) {
  if (!obj) return;
  if (typeof obj === 'string') obj = { state: obj };
  const st = obj.state || state.terminal.state;
  state.terminal.state = st;
  if (obj.options && typeof obj.options === 'object') {
    state.terminal.yolo = !!obj.options.yolo;
    state.terminal.model = obj.options.model || null;
    state.terminal.resume = obj.options.resume || 'none';
  }
  if (typeof obj.pending_options === 'boolean') state.terminal.pending = obj.pending_options;
  const stateKeys = { running: 'terminal.state_running', stopped: 'terminal.state_stopped',
                      exited: 'terminal.state_exited' };
  if (dom['terminal-status-label']) {
    dom['terminal-status-label'].textContent = stateKeys[st] ? t(stateKeys[st]) : st;
  }
  if (dom['terminal-dot']) dom['terminal-dot'].className = 'terminal-dot ' + st;
  const running = st === 'running';
  if (dom['terminal-start']) dom['terminal-start'].disabled = running;
  if (dom['terminal-stop']) dom['terminal-stop'].disabled = !running;
  if (dom['terminal-restart']) dom['terminal-restart'].disabled = false;
  renderTerminalOptions();
  // Keep the sidebar side-dot in sync without a full re-render churn.
  const sd = dom['bot-list'] && dom['bot-list'].querySelector('.terminal-sidedot');
  if (sd) sd.className = 'bot-status-dot terminal-sidedot ' + st;
}

// Resume-mode presentation. The value set maps 1:1 to the CLI's spawn flags on
// the server (none / -c / --resume / --copy); we only render labels here.
const RESUME_KEYS = { none: 'terminal.resume_none', continue: 'terminal.resume_continue',
                      resume: 'terminal.resume_resume', copy: 'terminal.resume_copy' };

// Reflect the current spawn options on the bottom bar: YOLO toggle label +
// warning colour when on, the Model button's short label, and the
// "applies on restart" hint on Restart when a running session has pending opts.
function renderTerminalOptions() {
  const mode = state.terminal.resume || 'none';
  const yolo = !!state.terminal.yolo;
  const model = state.terminal.model;
  // YOLO row (menuitemcheckbox) in the popover.
  const yb = dom['terminal-yolo'];
  if (yb) yb.setAttribute('aria-checked', yolo ? 'true' : 'false');
  if (dom['terminal-yolo-val']) dom['terminal-yolo-val'].textContent = t(yolo ? 'common.on' : 'common.off');
  // Model row.
  if (dom['terminal-model-val']) {
    dom['terminal-model-val'].textContent = model ? shortModel(model) : t('terminal.model_default');
    dom['terminal-model-val'].title = model ? model : t('terminal.model_default_long');
  }
  // Session resume radios.
  const menu = dom['terminal-opts-menu'];
  if (menu) menu.querySelectorAll('button[data-resume]').forEach((b) =>
    b.setAttribute('aria-checked', b.dataset.resume === mode ? 'true' : 'false'));
  // ⚙ Options button badges when anything non-default is armed.
  const ob = dom['terminal-opts'];
  if (ob) {
    const armed = yolo || !!model || mode !== 'none';
    ob.classList.toggle('opts-set', armed);
    const bits = [];
    if (yolo) bits.push(t('terminal.options_yolo_on'));
    if (model) bits.push(shortModel(model));
    if (mode !== 'none') bits.push(t('terminal.options_session', { mode: RESUME_KEYS[mode] ? t(RESUME_KEYS[mode]) : mode }));
    // Intl.ListFormat rather than ', ' — the conjunction and the separator are
    // both language-specific.
    ob.title = bits.length
      ? t('terminal.options_title_set', { summary: fmtList(bits) })
      : t('terminal.options_title');
  }
  const rb = dom['terminal-restart'];
  if (rb) {
    const pending = !!state.terminal.pending;
    rb.classList.toggle('pending', pending);
    rb.title = pending ? t('terminal.restart_pending') : '';
  }
}

// Compact model label for the narrow bar: keep the part after the last "/".
function shortModel(m) {
  const s = String(m);
  const i = s.lastIndexOf('/');
  return i >= 0 ? s.slice(i + 1) : s;
}

// The terminal is a pseudo-bot with no chat threads. Selecting it must not leave the
// previous bot's thread list (a separate panel the terminal overlay doesn't
// cover) on screen — replace it with an admin-session placeholder. selectBot()
// restores the real header + threads when switching back to a normal bot.
function renderTerminalSessionPanel() {
  dom['tl-botname'].textContent = t('terminal.name');
  dom['tl-model'].textContent = t('terminal.session_panel_title');
  const wrap = dom['threads'];
  wrap.innerHTML = '';
  wrap.append(el('div', { class: 'empty-list terminal-session-note' }, [
    el('div', { class: 'empty-emoji', text: '›_' }),
    el('p', { text: t('terminal.session_panel_heading') }),
    el('p', { class: 'muted', text: t('terminal.session_panel_body') }),
  ]));
}

// xterm + its four addons + xterm.css are 317KB — more than half the app's
// entire vendor payload — and only the full-session terminal ever uses them.
// Fetched on first open instead of on every page load.
let _xtermPromise = null;
function ensureXterm() {
  if (window.Terminal) return Promise.resolve(true);
  if (!_xtermPromise) {
    _xtermPromise = loadStyle('/static/vendor/xterm.css')
      .then(() => loadScript('/static/vendor/xterm.js'))
      // Addons attach to the core, so they must land after it. Each is
      // individually optional — the call sites below already guard on the
      // global — so one failure degrades a feature rather than the terminal.
      .then(() => Promise.all([
        loadScript('/static/vendor/xterm-addon-fit.js'),
        loadScript('/static/vendor/xterm-addon-unicode11.js'),
        loadScript('/static/vendor/xterm-addon-web-links.js'),
        loadScript('/static/vendor/xterm-addon-search.js'),
      ].map((pr) => pr.catch(() => {}))))
      .then(() => !!window.Terminal)
      .catch(() => false);
  }
  return _xtermPromise;
}

async function openTerminalView() {
  if (state.decoy || !state.terminalEnabled) return;
  if (!window.Terminal) {
    if (!await ensureXterm()) { toast('Could not load the terminal', true); return; }
    // Awaiting yields to the event loop: the user may have navigated away or
    // the session may have locked while the bundle was in flight.
    if (state.decoy || !state.terminalEnabled) return;
  }
  if (harnessOpen) closeHarnessView();
  state.selectedBotId = TERMINAL_ID;
  terminalOpen = true;
  renderSidebar();
  renderTerminalSessionPanel();
  dom['terminal-view'].classList.remove('hidden');
  document.body.classList.add('terminal-active');
  if (isMobile()) navigate('chat');
  if (!termInstance) {
    termInstance = new window.Terminal({
      convertEol: false,
      cursorBlink: true,
      fontFamily: "var(--font-mono), ui-monospace, 'JetBrains Mono', monospace",
      fontSize: 13,
      scrollback: 8000,          // generous history for long TUI runs
      fastScrollModifier: 'alt', // Alt+wheel scrolls a page at a time
      allowProposedApi: true,    // required by the unicode11 addon
      theme: termTheme(),
    });
    termFit = new window.FitAddon.FitAddon();
    termInstance.loadAddon(termFit);
    // Best-practice addons (all vendored locally, no CDN at runtime):
    //  - unicode11: correct width for emoji/CJK/box-drawing so a TUI
    //    columns line up. activeVersion must be set AFTER loading.
    //  - web-links: OSC-8 + plain URLs the CLI prints become clickable.
    //  - search: powers the find box (Ctrl/⌘+F).
    try {
      if (window.Unicode11Addon) {
        termInstance.loadAddon(new window.Unicode11Addon.Unicode11Addon());
        termInstance.unicode.activeVersion = '11';
      }
    } catch (e) { /* non-fatal: fall back to default widths */ }
    try {
      if (window.WebLinksAddon) termInstance.loadAddon(new window.WebLinksAddon.WebLinksAddon());
    } catch (e) { /* non-fatal */ }
    try {
      if (window.SearchAddon) { termSearch = new window.SearchAddon.SearchAddon(); termInstance.loadAddon(termSearch); }
    } catch (e) { termSearch = null; }
    termInstance.open(dom['terminal-host']);
    termInstance.onData((d) => {
      if (termSock && termSock.readyState === WebSocket.OPEN) {
        termSock.send(JSON.stringify({ type: 'input', data: d }));
      }
    });
    // Copy-on-select (standard terminal UX): whenever a selection exists, mirror
    // it to the clipboard. localhost is a secure context so writeText is allowed.
    termInstance.onSelectionChange(() => {
      const sel = termInstance.getSelection();
      if (sel && navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(sel).catch(() => {});
      }
    });
    // Key handling: intercept ONLY Find, explicit Copy (Ctrl/⌘+Shift+C) and
    // Paste (Ctrl/⌘+V). Everything else — crucially plain Ctrl+C — falls through
    // to the PTY, so Ctrl+C still delivers SIGINT rather than being hijacked.
    termInstance.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown') return true;
      const mod = e.ctrlKey || e.metaKey;
      if (!mod) return true;
      const k = e.key.toLowerCase();
      if (k === 'f' && !e.shiftKey) { toggleTermFind(true); return false; }
      if (k === 'c' && e.shiftKey) { termCopySelection(); return false; }
      if (k === 'v' && !e.shiftKey) { termPaste(); return false; }
      return true;
    });
    // Observe BOTH the host and the action bar. A bar whose height changes
    // (e.g. it briefly wraps, or a long status label reflows) shrinks the host,
    // and re-fitting keeps the terminal's own bottom line clear of the bar.
    termResizeObs = new ResizeObserver(() => fitTerminal());
    termResizeObs.observe(dom['terminal-host']);
    const bar = dom['terminal-view'].querySelector('.terminal-bar');
    if (bar) termResizeObs.observe(bar);
  }
  // Fit only after layout has actually settled: a single rAF can run before the
  // freshly-unhidden view has its final size. Double-rAF + a fonts.ready pass
  // (cell metrics depend on the mono font) makes the row count correct so a TUI
  // like one that draws its own bottom status line, isn't clipped.
  termEarlyFits = 6;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    fitTerminal();
    termInstance && termInstance.focus();
  }));
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => { if (terminalOpen) fitTerminal(); });
  }
  connectTerminal();
}

function closeTerminalView() {
  terminalOpen = false;
  document.body.classList.remove('terminal-active');
  dom['terminal-view'].classList.add('hidden');
  toggleTermFind(false);
  closeOptsMenu();
  if (termReconnectTimer) { clearTimeout(termReconnectTimer); termReconnectTimer = null; }
  if (termSock) { try { termSock.close(); } catch {} termSock = null; }
  if (termResizeObs) { try { termResizeObs.disconnect(); } catch {} termResizeObs = null; }
  if (termInstance) { try { termInstance.dispose(); } catch {} termInstance = null; termFit = null; termSearch = null; }
}

// ---- Copy / paste / find helpers ----
function termCopySelection() {
  if (!termInstance) return;
  const sel = termInstance.getSelection();
  if (sel && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(sel).catch(() => {});
  }
}

function termPaste() {
  if (!termInstance) return;
  // Route through term.paste() so bracketed-paste + onData forwarding is handled
  // uniformly; fall back to xterm's native textarea paste if the clipboard API
  // is unavailable (non-secure context).
  if (navigator.clipboard && navigator.clipboard.readText) {
    navigator.clipboard.readText().then((t) => { if (t) termInstance.paste(t); }).catch(() => {});
  }
}

function toggleTermFind(show) {
  const box = dom['terminal-find'];
  if (!box) return;
  if (show && termSearch) {
    box.classList.remove('hidden');
    const inp = dom['terminal-find-input'];
    if (inp) { inp.focus(); inp.select(); }
  } else {
    box.classList.add('hidden');
    if (termSearch) { try { termSearch.clearDecorations && termSearch.clearDecorations(); } catch {} }
    if (termInstance) termInstance.focus();
  }
}

function termFind(dir) {
  if (!termSearch) return;
  const q = (dom['terminal-find-input'] && dom['terminal-find-input'].value) || '';
  if (!q) return;
  const opts = { incremental: false };
  try {
    if (dir < 0) termSearch.findPrevious(q, opts);
    else termSearch.findNext(q, opts);
  } catch { /* addon may reject on an empty buffer */ }
}

// ---- Options popover (YOLO / Model / Session resume-mode) ----
function toggleOptsMenu() {
  const menu = dom['terminal-opts-menu'];
  if (!menu) return;
  if (menu.hasAttribute('hidden')) openOptsMenu(); else closeOptsMenu();
}
function openOptsMenu() {
  const menu = dom['terminal-opts-menu'];
  const btn = dom['terminal-opts'];
  if (!menu || !btn) return;
  menu.removeAttribute('hidden');
  btn.setAttribute('aria-expanded', 'true');
  // Fixed positioning (coords here) so the pop-up escapes the bar's overflow
  // clip. Open upward + left-aligned to the button; fall back to below when
  // there isn't room above (very short viewports).
  const r = btn.getBoundingClientRect();
  const mw = menu.offsetWidth || 232;
  const mh = menu.offsetHeight || 260;
  menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - mw - 8)) + 'px';
  const above = r.top - mh - 6;
  menu.style.top = (above >= 8 ? above : r.bottom + 6) + 'px';
  // Close on the next outside click.
  setTimeout(() => document.addEventListener('click', _optsOutside, { once: true }), 0);
}
function closeOptsMenu() {
  const menu = dom['terminal-opts-menu'];
  if (menu) menu.setAttribute('hidden', '');
  if (dom['terminal-opts']) dom['terminal-opts'].setAttribute('aria-expanded', 'false');
}
function _optsOutside(e) {
  const wrap = dom['terminal-opts'] && dom['terminal-opts'].closest('.term-opts-wrap');
  const menu = dom['terminal-opts-menu'];
  // The menu is position:fixed, so it may not be a DOM descendant hit-box under
  // the wrap for contains() — check both the wrap and the menu explicitly.
  if ((wrap && wrap.contains(e.target)) || (menu && menu.contains(e.target))) {
    if (menu && !menu.hasAttribute('hidden')) {
      document.addEventListener('click', _optsOutside, { once: true });
    }
    return;
  }
  closeOptsMenu();
}

function fitTerminal() {
  if (!termFit || !termInstance || !terminalOpen) return;
  try { termFit.fit(); } catch { return; }
  // FitAddon floors by the CSS cell height, but the browser renders each row
  // rounded UP to a whole pixel — so the grid can end up a few px taller than
  // the host's content box and spill its bottom row under the action bar. Shrink
  // rows using the ACTUAL rendered row height so the grid always fits inside the
  // padded host (this is what keeps a full-screen TUI's bottom line clear).
  const host = dom['terminal-host'];
  const rowsEl = host && host.querySelector('.xterm-rows');
  const firstRow = rowsEl && rowsEl.children[0];
  if (host && firstRow) {
    const cs = getComputedStyle(host);
    const avail = host.clientHeight - parseFloat(cs.paddingTop || 0) - parseFloat(cs.paddingBottom || 0);
    // A single rendered row's height is the true cell height (constant for the
    // font, and unaffected by an in-flight row-count change on .xterm-rows).
    const rowH = firstRow.getBoundingClientRect().height;
    if (rowH > 0) {
      const maxRows = Math.max(2, Math.floor(avail / rowH));
      if (termInstance.rows > maxRows) termInstance.resize(termInstance.cols, maxRows);
    }
  }
  const dims = { cols: termInstance.cols, rows: termInstance.rows };
  if (termSock && termSock.readyState === WebSocket.OPEN) {
    termSock.send(JSON.stringify({ type: 'resize', cols: dims.cols, rows: dims.rows }));
  }
}

function connectTerminal() {
  if (!terminalOpen) return;
  if (termSock && (termSock.readyState === WebSocket.OPEN || termSock.readyState === WebSocket.CONNECTING)) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let sock;
  try { sock = new WebSocket(`${proto}://${location.host}/ws/terminal`); }
  catch { scheduleTerminalReconnect(); return; }
  termSock = sock;
  sock.onopen = () => {
    termReconnectDelay = 500;
    fitTerminal();
  };
  sock.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'output') {
      if (termInstance) termInstance.write(b64ToBytes(msg.data || ''));
      // A full-screen TUI redraws to whatever size it thinks it has; re-fit on
      // the first few output frames so the size it settles on is the true one.
      if (termEarlyFits > 0) { termEarlyFits--; requestAnimationFrame(() => fitTerminal()); }
    }
    else if (msg.type === 'state') applyTerminalStatus(msg);
    else if (msg.type === 'locked') { handleLocked(); }
  };
  sock.onclose = () => {
    if (termSock === sock) termSock = null;
    if (terminalOpen) scheduleTerminalReconnect();
  };
  sock.onerror = () => { try { sock.close(); } catch {} };
}

function scheduleTerminalReconnect() {
  if (!terminalOpen || termReconnectTimer) return;
  if (termInstance) termInstance.write('\r\n\x1b[2m' + t('terminal.disconnected') + '\x1b[0m\r\n');
  termReconnectTimer = setTimeout(() => {
    termReconnectTimer = null;
    connectTerminal();
  }, termReconnectDelay);
  termReconnectDelay = Math.min(termReconnectDelay * 2, 10000);
}

async function terminalControl(action) {
  dom['terminal-start'].disabled = true;
  dom['terminal-stop'].disabled = true;
  dom['terminal-restart'].disabled = true;
  try {
    const st = await api.terminalAction(action);
    applyTerminalStatus(st);
    // Restart from an exited session needs a live socket again; a stopped→exit
    // transition just updates buttons. The server does not push output on a
    // control POST, so (re)connect if we somehow lost the socket.
    if (terminalOpen && !termSock) connectTerminal();
  } catch (e) {
    toast(cleanErr(e), true);
    applyTerminalStatus(state.terminal.state);
  }
}

// ---- Spawn options: YOLO toggle + model picker (applied on next start/restart) ----
async function setTerminalOptions(payload) {
  try {
    const st = await api.terminalOptions(payload);
    applyTerminalStatus(st);
  } catch (e) {
    toast(cleanErr(e), true);
  }
}

function toggleTerminalYolo() {
  setTerminalOptions({ yolo: !state.terminal.yolo });
}

async function openModelPicker() {
  dom['terminal-model-input'].value = '';
  const box = dom['terminal-model-list'];
  box.innerHTML = '';
  box.append(el('div', { class: 'muted term-model-loading', text: t('terminal.picker_loading') }));
  dom['terminal-model-backdrop'].classList.remove('hidden');
  let data = { models: [], current_default: null };
  try { data = await api.terminalModels(); } catch { /* best-effort; free-text still works */ }
  box.innerHTML = '';
  const cur = state.terminal.model;
  // "Default" radio (clears the override).
  box.append(modelRow('', data.current_default
    ? t('terminal.model_default_named', { name: shortModel(data.current_default) })
    : t('terminal.model_default_long'), cur === null));
  (data.models || []).forEach((m) => box.append(modelRow(m, m, cur === m)));
  if (!(data.models || []).length) {
    box.append(el('p', { class: 'muted term-model-empty', text: t('terminal.picker_empty') }));
  }
}

function modelRow(value, label, checked) {
  const row = el('label', { class: 'term-model-row' });
  const radio = el('input', { type: 'radio', name: 'term-model', value });
  radio.checked = !!checked;
  row.append(radio, el('span', { text: label }));
  return row;
}

function closeModelPicker() { dom['terminal-model-backdrop'].classList.add('hidden'); }

function saveModelPicker() {
  const typed = dom['terminal-model-input'].value.trim();
  let model;
  if (typed) model = typed;
  else {
    const sel = dom['terminal-model-list'].querySelector('input[name="term-model"]:checked');
    model = sel ? sel.value : '';   // '' → clear to default
  }
  closeModelPicker();
  setTerminalOptions({ model: model || null });
}

function wireTerminal() {
  if (dom['terminal-start']) dom['terminal-start'].addEventListener('click', () => terminalControl('start'));
  if (dom['terminal-restart']) dom['terminal-restart'].addEventListener('click', () => terminalControl('restart'));
  if (dom['terminal-stop']) dom['terminal-stop'].addEventListener('click', () => terminalControl('stop'));
  // Options popover: ⚙ button toggles it; YOLO toggles in place; Model opens the
  // picker (closing the popover); Session radios apply + close.
  if (dom['terminal-opts']) dom['terminal-opts'].addEventListener('click', (e) => { e.stopPropagation(); toggleOptsMenu(); });
  if (dom['terminal-yolo']) dom['terminal-yolo'].addEventListener('click', (e) => { e.stopPropagation(); toggleTerminalYolo(); });
  if (dom['terminal-model']) dom['terminal-model'].addEventListener('click', (e) => { e.stopPropagation(); closeOptsMenu(); openModelPicker(); });
  if (dom['terminal-opts-menu']) dom['terminal-opts-menu'].addEventListener('click', (e) => {
    const b = e.target.closest('button[data-resume]');
    if (!b) return;
    closeOptsMenu();
    setTerminalOptions({ resume: b.dataset.resume });
  });
  // Find box
  if (dom['terminal-find-btn']) dom['terminal-find-btn'].addEventListener('click', () => toggleTermFind(!dom['terminal-find'] || dom['terminal-find'].classList.contains('hidden')));
  if (dom['terminal-find-next']) dom['terminal-find-next'].addEventListener('click', () => termFind(1));
  if (dom['terminal-find-prev']) dom['terminal-find-prev'].addEventListener('click', () => termFind(-1));
  if (dom['terminal-find-close']) dom['terminal-find-close'].addEventListener('click', () => toggleTermFind(false));
  if (dom['terminal-find-input']) dom['terminal-find-input'].addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); termFind(e.shiftKey ? -1 : 1); }
    else if (e.key === 'Escape') { e.preventDefault(); toggleTermFind(false); }
  });
  // Mobile back affordance (hidden on desktop via .back-btn CSS).
  // The terminal has no thread list, so Back returns to the bot list (not 'threads',
  // which would surface the previous bot's stale admin-session placeholder).
  if (dom['terminal-back']) dom['terminal-back'].addEventListener('click', () => navigate('bots'));
  wireHarnessView();
  if (dom['terminal-model-close']) dom['terminal-model-close'].addEventListener('click', closeModelPicker);
  if (dom['terminal-model-clear']) dom['terminal-model-clear'].addEventListener('click', () => { closeModelPicker(); setTerminalOptions({ model: null }); });
  if (dom['terminal-model-save']) dom['terminal-model-save'].addEventListener('click', saveModelPicker);
  if (dom['terminal-model-backdrop']) dom['terminal-model-backdrop'].addEventListener('click', (e) => { if (e.target === dom['terminal-model-backdrop']) closeModelPicker(); });
  if (dom['terminal-model-input']) dom['terminal-model-input'].addEventListener('keydown', (e) => { if (e.key === 'Enter') saveModelPicker(); });
  // Keep the xterm palette in sync when the app theme toggles while the terminal
  // is open. theme.js only flips data-theme; the canvas theme is otherwise fixed
  // at construction (the bar re-themes for free via its CSS vars).
  new MutationObserver(() => { if (termInstance) termInstance.options.theme = termTheme(); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
}

/** Make sure `state.auth.features` describes the session we are actually in.
 *
 *  The server discloses the optional-subsystem inventory only to a FULL
 *  session, so a locked landing gets `{}` — present, truthy, and empty. Every
 *  probe below reads it as "not disclosed" and asks the endpoint instead,
 *  which is right when the answer is genuinely unknown and wrong the moment
 *  the session has since been unlocked: reboot() goes straight to startApp(),
 *  so nothing had re-read /api/auth/status and the stale `{}` survived.
 *
 *  One cheap request, and only when the inventory is missing or empty.
 *  Never throws — an unreachable status endpoint just leaves us probing, which
 *  is the old behaviour.
 */
async function ensureFeatures() {
  const f = state.auth.features;
  if (f && Object.keys(f).length) return;
  try {
    const s = await api.authStatus();
    if (s && s.features && Object.keys(s.features).length) state.auth.features = s.features;
  } catch { /* fall back to probing */ }
}

// Probe the terminal feature flag at startup (same pattern as the ComfyUI
// chip): only 404 (disabled) / 403 (no access) hide the pseudo-bot.
async function refreshTerminalFeature() {
  if (state.decoy) { state.terminalEnabled = false; return; }
  // The server tells us up front now. Probing a disabled feature still worked
  // (404 -> off) but logged a failed request on every boot of a default
  // install, where it IS off.
  if (state.auth.features && state.auth.features.terminal === false) {
    state.terminalEnabled = false; return;
  }
  try {
    const st = await api.terminalStatus();
    state.terminalEnabled = true;
    applyTerminalStatus(st);
  } catch (e) {
    if (e.status === 404 || e.status === 403) state.terminalEnabled = false;
  }
  renderSidebar();
}

// ===================== DeepSeek Harness (dsh) =====================
// dsh ships no TUI, so unlike the coding terminal this pane is not a PTY: it embeds the
// `dsh web` UI (loopback :3080, controlled as a systemd --user unit), offers the
// default-model switch (settings.yaml, hot-reloaded by dsh) and runs one-shot
// headless jobs whose final answer is shown in a short history. Full-session
// only — the pseudo-bot never renders in Safe Mode and every route 403s there.
let harnessOpen = false;
let harnessTab = 'ui';          // 'ui' | 'jobs'
let harnessTick = null;         // 1s repaint while a job runs (elapsed seconds)
let harnessBusy = false;        // a service op is in flight (buttons disabled)

function harnessServiceState() {
  const st = state.harness.status;
  if (!st) return 'unknown';
  if (st.installed === false) return 'not_installed';
  if (harnessBusy) return 'starting';
  if (st.healthy) return 'running';
  const a = st.unit && st.unit.active_state;
  if (a === 'activating') return 'starting';
  if (a === 'failed') return 'failed';
  return 'stopped';
}

// The frame can only load when THIS browser can reach the host's loopback:
// DisPatch opened on the host itself. Over the tailnet / LAN the UI is not
// reachable (dsh binds 127.0.0.1 only, on purpose).
function harnessLocalReachable() {
  const h = location.hostname;
  return h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '[::1]';
}

async function refreshHarnessFeature() {
  if (state.decoy) { state.harnessEnabled = false; return; }
  if (state.auth.features && state.auth.features.harness === false) {
    state.harnessEnabled = false; return;
  }
  try {
    const st = await api.harnessStatus();
    state.harnessEnabled = true;
    applyHarnessStatus(st);
  } catch (e) {
    if (e.status === 404 || e.status === 403) state.harnessEnabled = false;
  }
  renderSidebar();
}

function applyHarnessStatus(st) {
  if (!st || typeof st !== 'object') return;
  state.harness.status = st;
  if (st.jobs) state.harness.jobs = st.jobs;
  renderHarness();
}

function applyHarnessFrame(data) {
  if (data.jobs) state.harness.jobs = data.jobs;
  if (data.service) state.harness.status = Object.assign({}, state.harness.status || {}, data.service);
  if (data.models && state.harness.status) state.harness.status.models = data.models;
  renderHarness();
}

function renderHarnessSessionPanel() {
  dom['tl-botname'].textContent = t('harness.name');
  dom['tl-model'].textContent = t('harness.session_panel_title');
  const wrap = dom['threads'];
  wrap.innerHTML = '';
  wrap.append(el('div', { class: 'empty-list terminal-session-note' }, [
    el('div', { class: 'empty-emoji', text: 'dsh' }),
    el('p', { text: t('harness.session_panel_heading') }),
    el('p', { class: 'muted', text: t('harness.session_panel_body') }),
  ]));
}

function openHarnessView() {
  if (state.decoy || !state.harnessEnabled) return;
  if (terminalOpen) closeTerminalView();
  state.selectedBotId = HARNESS_ID;
  harnessOpen = true;
  renderSidebar();
  renderHarnessSessionPanel();
  dom['harness-view'].classList.remove('hidden');
  document.body.classList.add('terminal-active');
  if (isMobile()) navigate('chat');
  renderHarness();
  // Fresh facts on open (the WS only carries deltas).
  api.harnessStatus().then(applyHarnessStatus).catch(() => {});
}

function closeHarnessView() {
  harnessOpen = false;
  document.body.classList.remove('terminal-active');
  dom['harness-view'].classList.add('hidden');
  // Unload the frame: a hidden iframe keeps dsh's websocket + HMR stream alive.
  if (dom['harness-frame']) { dom['harness-frame'].classList.add('hidden'); dom['harness-frame'].removeAttribute('src'); }
  if (harnessTick) { clearInterval(harnessTick); harnessTick = null; }
}

function setHarnessTab(tab) {
  harnessTab = tab === 'jobs' ? 'jobs' : 'ui';
  const ui = harnessTab === 'ui';
  dom['harness-tab-ui'].classList.toggle('active', ui);
  dom['harness-tab-ui'].setAttribute('aria-selected', ui ? 'true' : 'false');
  dom['harness-tab-jobs'].classList.toggle('active', !ui);
  dom['harness-tab-jobs'].setAttribute('aria-selected', ui ? 'false' : 'true');
  dom['harness-pane-ui'].classList.toggle('hidden', !ui);
  dom['harness-pane-jobs'].classList.toggle('hidden', ui);
  renderHarness();
  if (!ui && dom['harness-task']) dom['harness-task'].focus();
}

// Paints everything from state: bar (dot/label/buttons/model select), the
// UI pane (frame vs. note) and the jobs pane. Cheap; called on every change.
function renderHarness() {
  const st = state.harness.status;
  const svc = harnessServiceState();
  // Sidebar dot.
  const sd = dom['bot-list'] && dom['bot-list'].querySelector('.harness-sidedot');
  if (sd) sd.className = 'bot-status-dot terminal-sidedot harness-sidedot harness-' + svc;
  if (!harnessOpen) return;
  const port = (st && st.port) || 3080;
  const url = (st && st.url) || `http://127.0.0.1:${port}/`;
  if (dom['harness-subtitle']) dom['harness-subtitle'].textContent = t('harness.subtitle', { port: String(port) });
  if (dom['harness-open']) {
    dom['harness-open'].href = url;
    dom['harness-open'].classList.toggle('hidden', !harnessLocalReachable() || svc !== 'running');
  }
  // Bar.
  if (dom['harness-dot']) dom['harness-dot'].className = 'terminal-dot harness-' + svc;
  if (dom['harness-status-label']) dom['harness-status-label'].textContent = t('harness.state_' + svc);
  const running = svc === 'running';
  const installed = !(st && st.installed === false);
  if (dom['harness-start']) dom['harness-start'].disabled = harnessBusy || running || !installed;
  if (dom['harness-stop']) dom['harness-stop'].disabled = harnessBusy || !running;
  if (dom['harness-restart']) dom['harness-restart'].disabled = harnessBusy || !installed;
  renderHarnessModelSelect();
  // UI pane: frame when reachable + running; otherwise an explanatory note.
  const frame = dom['harness-frame'], note = dom['harness-note'];
  if (frame && note) {
    let noteKey = null;
    if (!installed) noteKey = 'not_installed';
    else if (!harnessLocalReachable()) noteKey = 'remote';
    else if (!running) noteKey = 'stopped';
    if (noteKey) {
      if (!frame.classList.contains('hidden')) { frame.classList.add('hidden'); frame.removeAttribute('src'); }
      dom['harness-note-text'].textContent = t(`harness.${noteKey}_text`);
      dom['harness-note-hint'].textContent = t(`harness.${noteKey}_hint`);
      note.classList.remove('hidden');
    } else {
      note.classList.add('hidden');
      if (harnessTab === 'ui') {
        if (frame.getAttribute('src') !== url) frame.setAttribute('src', url);
        frame.classList.remove('hidden');
      }
    }
  }
  renderHarnessJobs();
}

function renderHarnessModelSelect() {
  const sel = dom['harness-model'];
  if (!sel) return;
  const models = state.harness.status && state.harness.status.models;
  const cur = models && models.current;
  const curKey = cur ? `${cur.provider} ${cur.model}` : '';
  sel.innerHTML = '';
  if (!models || !Array.isArray(models.providers)) { sel.disabled = true; return; }
  let matched = false;
  for (const pr of models.providers) {
    if (!pr || !Array.isArray(pr.models) || !pr.models.length) continue;
    const grp = el('optgroup', { label: pr.name || pr.id });
    for (const m of pr.models) {
      const key = `${pr.id} ${m.id}`;
      const opt = el('option', { value: key, text: m.name && m.name !== m.id ? `${m.name}` : m.id });
      opt.title = `${pr.id} / ${m.id}`;
      if (key === curKey) { opt.selected = true; matched = true; }
      grp.append(opt);
    }
    sel.append(grp);
  }
  // A current default that is not in any catalog (hand-edited settings) still
  // shows up, so the picker never lies about what dsh will use.
  if (cur && !matched) {
    const opt = el('option', { value: curKey, text: `${cur.provider} / ${cur.model}` });
    opt.selected = true; sel.prepend(opt);
  }
  sel.disabled = harnessBusy || !sel.options.length;
}

function fmtHarnessWhen(ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); } catch { return ''; }
}

function harnessJobLabel(j) {
  const secs = j.duration_s != null ? Math.round(j.duration_s) : 0;
  switch (j.state) {
    case 'running': return t('harness.job_running', { secs });
    case 'done': return t('harness.job_done', { secs });
    case 'failed': return t('harness.job_failed', { code: j.exit_code, secs });
    case 'cancelled': return t('harness.job_cancelled');
    case 'timeout': return t('harness.job_timeout');
    default: return t('harness.job_queued');
  }
}

// The jobs ledger from the server holds summaries (no bodies); bodies for the
// visible history are fetched lazily and cached per job id.
const harnessJobBodies = new Map();
let harnessJobFetch = null;

function renderHarnessJobs() {
  const wrap = dom['harness-jobs'];
  if (!wrap || harnessTab !== 'jobs') return;
  const jobs = state.harness.jobs || { running: false, history: [] };
  const running = !!jobs.running;
  if (dom['harness-run']) dom['harness-run'].disabled = running;
  if (dom['harness-cancel']) dom['harness-cancel'].classList.toggle('hidden', !running);
  if (dom['harness-task']) dom['harness-task'].disabled = running;
  if (dom['harness-cwd']) dom['harness-cwd'].disabled = running;
  // Elapsed-seconds ticker while something runs.
  if (running && !harnessTick) {
    harnessTick = setInterval(() => {
      if (harnessOpen && harnessTab === 'jobs') renderHarnessJobs();
      else if (harnessTick) { clearInterval(harnessTick); harnessTick = null; }
    }, 1000);
  }
  if (!running && harnessTick) { clearInterval(harnessTick); harnessTick = null; }
  const list = jobs.history || [];
  wrap.innerHTML = '';
  if (!list.length) {
    wrap.append(el('div', { class: 'empty-list' }, [el('p', { class: 'muted', text: t('harness.job_empty') })]));
    return;
  }
  // Fetch bodies for finished jobs we have not seen yet (one request).
  const missing = list.filter((j) => j.state !== 'running' && j.state !== 'queued' && !harnessJobBodies.has(j.id));
  if (missing.length && !harnessJobFetch) {
    harnessJobFetch = api.harnessJobs().then((r) => {
      for (const j of (r.jobs || [])) if (j.state !== 'running' && j.state !== 'queued') harnessJobBodies.set(j.id, j);
    }).catch(() => {}).finally(() => { harnessJobFetch = null; if (harnessOpen) renderHarnessJobs(); });
  }
  for (const j of list) {
    // Elapsed for a running job is computed client-side from started_at.
    const view = Object.assign({}, j);
    if (j.state === 'running' && j.started_at) view.duration_s = Math.max(0, Date.now() / 1000 - j.started_at);
    const body = harnessJobBodies.get(j.id);
    const dotCls = j.state === 'running' ? 'harness-running'
      : j.state === 'done' ? 'running'
      : (j.state === 'failed' || j.state === 'timeout') ? 'harness-failed' : 'stopped';
    const card = el('div', { class: 'harness-job harness-job-' + j.state });
    card.append(el('div', { class: 'harness-job-head' }, [
      el('span', { class: 'terminal-dot ' + dotCls }),
      el('span', { class: 'harness-job-state', text: harnessJobLabel(view) }),
      el('span', { class: 'harness-job-cwd', text: t('harness.job_cwd', { cwd: shortHome(j.cwd) }) }),
      el('span', { class: 'harness-job-when', text: fmtHarnessWhen(j.started_at) }),
    ]));
    card.append(el('div', { class: 'harness-job-task', text: j.task }));
    if (body) {
      const out = (body.output || '').trim();
      const outEl = el('div', { class: 'harness-job-out markdown-body' + (out ? '' : ' empty') });
      if (out) { outEl.innerHTML = renderMarkdown(out); enhanceContent(outEl); }
      else outEl.textContent = '(no output)';
      card.append(outEl);
      if ((body.error || '').trim()) {
        card.append(el('details', { class: 'harness-job-err' }, [
          el('summary', { text: t('harness.job_show_stderr') }),
          el('pre', { text: body.error.trim().slice(-4000) }),
        ]));
      }
    }
    wrap.append(card);
  }
}

function shortHome(p) {
  if (!p) return '~';
  const dshHome = state.harness.status && state.harness.status.home;
  const home = dshHome ? dshHome.replace(/[/\\]\.dsh$/, '') : null;
  if (home && p.startsWith(home)) return '~' + p.slice(home.length) || '~';
  return p;
}

async function harnessServiceAction(action) {
  if (harnessBusy) return;
  harnessBusy = true; renderHarness();
  try {
    const st = await api.harnessAction(action);
    harnessBusy = false;
    applyHarnessStatus(Object.assign({}, state.harness.status || {}, st));
    toast(t('harness.toast_' + (action === 'start' ? 'started' : action === 'stop' ? 'stopped' : 'restarted')));
  } catch (e) {
    harnessBusy = false;
    toast(cleanErr(e), true);
    api.harnessStatus().then(applyHarnessStatus).catch(() => {});
  }
}

async function harnessSaveModel() {
  const sel = dom['harness-model'];
  if (!sel || !sel.value) return;
  const [provider, model] = sel.value.split(' ');
  try {
    const r = await api.harnessSetModel(provider, model);
    if (state.harness.status) state.harness.status.models = r.models;
    toast(t('harness.model_saved', { model }));
  } catch (e) {
    toast(cleanErr(e), true);
  }
  renderHarness();
}

async function harnessSubmitJob(ev) {
  if (ev) ev.preventDefault();
  const task = (dom['harness-task'].value || '').trim();
  if (!task) { dom['harness-task'].focus(); return; }
  const cwd = (dom['harness-cwd'].value || '').trim();
  try {
    await api.harnessSubmitJob(task, cwd || null);
    dom['harness-task'].value = '';
    toast(t('harness.toast_job_started'));
    // The server broadcasts harness_state on start; pull once too in case the
    // socket is mid-reconnect.
    api.harnessStatus().then(applyHarnessStatus).catch(() => {});
  } catch (e) {
    toast(e.status === 409 ? t('harness.job_busy') : cleanErr(e), true);
  }
}

async function harnessCancelJob() {
  try { await api.harnessCancelJob(); toast(t('harness.toast_job_cancelled')); }
  catch (e) { toast(cleanErr(e), true); }
}

function wireHarnessView() {
  if (!dom['harness-view']) return;
  dom['harness-back'].addEventListener('click', () => navigate('bots'));
  dom['harness-tab-ui'].addEventListener('click', () => setHarnessTab('ui'));
  dom['harness-tab-jobs'].addEventListener('click', () => setHarnessTab('jobs'));
  dom['harness-start'].addEventListener('click', () => harnessServiceAction('start'));
  dom['harness-stop'].addEventListener('click', () => harnessServiceAction('stop'));
  dom['harness-restart'].addEventListener('click', () => harnessServiceAction('restart'));
  dom['harness-model'].addEventListener('change', harnessSaveModel);
  dom['harness-job-form'].addEventListener('submit', harnessSubmitJob);
  dom['harness-cancel'].addEventListener('click', harnessCancelJob);
  // Ctrl/Cmd+Enter submits from the textarea.
  dom['harness-task'].addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); harnessSubmitJob(); }
  });
}

// ===================== Live streaming render =====================
// Render the accumulating reply as MARKDOWN while it streams (rAF-coalesced),
// so it looks identical to the finalized row — no plain-text→formatted snap.
const streamBuffers = {};
const streamPending = new Set();
let streamRaf = 0;
const STREAM_PAINT_MS = 100;      // ~10 repaints/sec: reads as smooth, costs 6x less
const STREAM_PAINT_CHARS = 160;  // …but a burst that big repaints immediately
const streamPainted = new Map(); // id -> { at, len } of the last paint
function scheduleStreamRender(id) {
  streamPending.add(id);
  if (streamRaf) return;
  streamRaf = requestAnimationFrame(() => {
    streamRaf = 0;
    const now = Date.now();
    const ids = [...streamPending]; streamPending.clear();
    for (const sid of ids) {
      const last = streamPainted.get(sid) || { at: 0, len: 0 };
      const len = (streamBuffers[sid] || '').length;
      if (now - last.at < STREAM_PAINT_MS && len - last.len < STREAM_PAINT_CHARS) {
        // Too soon and too little new text — keep it queued rather than drop it,
        // or a stream that ends inside the window would never paint its tail.
        streamPending.add(sid);
        continue;
      }
      streamPainted.set(sid, { at: now, len });
      renderStreamMarkdown(sid);
    }
    if (streamPending.size) setTimeout(() => scheduleStreamRender([...streamPending][0]), STREAM_PAINT_MS);
  });
}
function renderStreamMarkdown(id) {
  const msgEl = dom['messages'].querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (!msgEl) return;
  const md = msgEl.querySelector('.stream-md');
  if (!md) return;
  // noMedia in Safe Mode (same redaction as final). Code highlight is deferred
  // to stream_done (messageEl + enhanceContent) — too costly per frame.
  md.innerHTML = renderMarkdown(streamBuffers[id] || '', { noMedia: mediaHidden() });
  // Instant scroll — smooth would fight itself across rapid streaming frames.
  if (isNearBottom(220)) scrollToBottom(true);
}

// ===================== WebSocket dispatch =====================
function handleWs(data) {
  switch (data.type) {
    case 'hello':
    case 'bots':
      if (typeof data.decoy === 'boolean') {
        // Server says Safe Mode but we thought we were unlocked → the session
        // lapsed (e.g. expiry during a WS reconnect). Do a clean full reset so
        // no already-loaded images linger, rather than just re-skinning.
        if (data.decoy && state.auth.pinSet && !state.decoy && state.auth.authenticated) {
          handleLocked();
          break;
        }
        state.decoy = data.decoy;
        state.auth.decoy = data.decoy;
        applyAuthChrome();
      }
      if (Array.isArray(data.bots)) {
        state.bots = data.bots.filter((b) => b.visible);
        const vb = visibleBots();
        // Only repair the selection if one existed and its bot vanished —
        // never auto-select on a fresh landing (the bots list IS the landing).
        if (state.selectedBotId && !vb.find((b) => b.id === state.selectedBotId)) {
          if (vb.length) selectBot(vb[0].id);
          else { state.selectedBotId = null; state.threads = []; renderSidebar(); renderThreads(); clearChatView(); }
        } else {
          renderSidebar();
          // An avatar change broadcasts through here — and everything painted
          // from bot.avatar_url must follow, not just the rail. Repainting only
          // the sidebar left the headers' thumbnails on the OLD ?v= URL while
          // their data-full pointed at the (unversioned) new full-res: click a
          // header and the lightbox opened a different picture than the thumb.
          updateThreadListHeader();
          renderChatHeader();
          // Thread-list ROWS too: a snapshotless thread (every pre-feature one)
          // renders the live avatar in its row, so its thumbnail must repaint or
          // it drifts from its own (no-cache) full-res the same way the header
          // did. Rows with a pinned snapshot are immune.
          renderThreads();
          // Message avatars in a SNAPSHOTLESS thread render the live avatar
          // too. Threads with a pin are immune — their faces are frozen.
          if (state.activeThread && !state.activeThread.avatar_snapshot) {
            renderMessages(false);
          }
        }
      }
      break;
    case 'thread_created':
      if (!data.thread) break;
      upsertThread(data.thread);
      if (data.thread.bot_id === state.selectedBotId) renderThreads();
      break;
    case 'thread_update':
      if (!data.thread) break;
      upsertThread(data.thread);
      if (data.thread.bot_id === state.selectedBotId) renderThreads();
      if (data.thread.id === state.activeThreadId) {
        state.activeThread = { ...state.activeThread, ...data.thread };
        renderChatHeader();
      }
      break;
    case 'thread_deleted':
      removeThread(data.thread_id);
      renderThreads();
      break;
    case 'ack': {
      // Server ack for a 'send' we originated. Unknown ids (other tabs,
      // already-cleared duplicates) are ignored silently.
      const p = clearPendingSend(data.client_msg_id);
      if (p && data.status === 'rejected') handleSendRejected(p, data.reason);
      break;
    }
    case 'message': {
      if (!data.message) break;
      // Secondary ack: our own send echoed back as the persisted broadcast.
      clearPendingSend(data.client_msg_id || data.message.client_msg_id);
      const isFinalReply = data.message.role === 'assistant'
        && !(data.message.metadata && data.message.metadata.sub);
      // A delivered reply ends the visible "working" state immediately —
      // don't keep the typing bubble up waiting for the stopped event.
      if (isFinalReply && state.thinking[data.thread_id]) {
        state.thinking[data.thread_id] = false;
        renderSidebar(); renderThreads();
      }
      if (data.thread_id === state.activeThreadId) {
        appendMessageToView(data.message);
        if (isFinalReply) reflectComposerState();
        // Reading it live — keep the server's read marker current.
        if (data.message.role !== 'user' && document.visibilityState === 'visible') {
          api.markRead(data.thread_id).catch(() => {});
        }
      } else if (data.message.role !== 'user') {
        // Background-bot reply: remember which bot owns this thread so the
        // sidebar unread dot can resolve (oldestUnreadByBot needs threadBot).
        // The frame always carries bot_id; guard mirrors refreshUnread().
        if (data.bot_id && !state.threadBot[data.thread_id]) state.threadBot[data.thread_id] = data.bot_id;
        if (!state.unread[data.thread_id]) {
          state.unread[data.thread_id] = data.message.created_at;
          renderSidebar(); renderThreads();
        }
      }
      break;
    }
    case 'thinking': {
      if (!data.thread_id) break;
      const on = data.status === 'started';
      state.thinking[data.thread_id] = on;
      if (on) state.progress[data.thread_id] = [];   // fresh turn, fresh log
      renderSidebar();
      // Only repaint the thread list when the turn belongs to the bot whose
      // list is on screen. A background agent's turn used to rebuild the list
      // you were reading — measured at ~7ms per repaint at 207 threads, twice
      // per turn (start and stop), for a thread not even shown.
      if (state.threadBot[data.thread_id] === state.selectedBotId
          || state.activeThreadId === data.thread_id) {
        // One thread's preview flipped to "thinking" — patch that row, and
        // let patchThreadRow fall back to a full render if the list shape
        // could have moved.
        patchThreadRow(data.thread_id);
      }
      if (data.thread_id === state.activeThreadId) reflectComposerState();
      break;
    }
    case 'progress': {
      if (!data.thread_id || !data.item) break;
      const list = (state.progress[data.thread_id] = state.progress[data.thread_id] || []);
      list.push(data.item);
      if (list.length > 200) list.shift();
      if (data.thread_id === state.activeThreadId) renderProgressPanel();
      break;
    }
    case 'error':
      toast(data.message + (data.detail ? ` (${data.detail})` : ''), true);
      announce(data.message, true);
      // Freeze any half-streamed bubble for this thread: keep the partial text,
      // drop the blinking cursor so it doesn't sit "alive" above the error.
      if (data.thread_id === state.activeThreadId) {
        dom['messages'].querySelectorAll('.msg.streaming').forEach((m) => {
          m.classList.remove('streaming');
          const c = m.querySelector('.stream-cursor'); if (c) c.remove();
          // No stream_done will follow this turn — release the buffer now.
          delete streamBuffers[m.dataset.id];
          state.streamingIds.delete(m.dataset.id);
        });
      }
      // Clear the working state for ANY thread — a background-thread error
      // would otherwise leave that bot's sidebar dot pulsing forever.
      if (data.thread_id && state.thinking[data.thread_id]) {
        state.thinking[data.thread_id] = false;
        renderSidebar(); renderThreads();
      }
      if (data.thread_id === state.activeThreadId) {
        reflectComposerState();
        appendErrorBubble(data.message, data.thread_id);
      }
      break;
    case 'stream_start': {
      if (!data.thread_id || !data.message_id) break;
      if (data.thread_id !== state.activeThreadId) break;
      state.streamingIds.add(data.message_id);
      streamBuffers[data.message_id] = '';
      const box = dom['messages'];
      // Remove the typing indicator — streaming is the new visual feedback.
      const typing = box.querySelector('.typing');
      const bot = botById(state.activeThread?.bot_id) || botById(state.selectedBotId);
      const av = avatarNode(bot, 'msg-avatar', state.activeThread);
      // Same rule as the persisted-message avatar: Safe Mode gets no full-res
      // affordance (the route 403s a decoy session — the click could only fail).
      if (state.decoy) delete av.dataset.full;
      const contentDiv = el('div', { class: 'stream-md' });
      // dir="auto" here too, or a streaming reply flips direction the moment it
      // is replaced by the final persisted bubble (which has it).
      const streamBubble = el('div', { class: 'bubble', dir: 'auto' }, [contentDiv, el('span', { class: 'stream-cursor' })]);
      const msgEl = el('div', { class: 'msg assistant streaming', dataset: { id: data.message_id } }, [
        av,
        el('div', { class: 'msg-col' }, [
          el('div', { class: 'msg-head' }, [
            nameSpan('msg-name', (bot && bot.name) ? bot.name : t('common.assistant'), bot),
          ]),
          streamBubble,
          el('div', { class: 'msg-time stream-time', text: clockTime(new Date().toISOString()) }),
        ]),
      ]);
      if (typing) { box.insertBefore(msgEl, typing); typing.remove(); }
      else box.append(msgEl);
      // Instant snap to the streaming bubble — smooth would be cancelled by
      // the first chunk arriving on the next rAF.
      if (isNearBottom()) scrollToBottom(true);
      else { showScrollButton(true); bumpUnseen(); }
      break;
    }
    case 'stream_chunk': {
      if (!data.thread_id || !data.message_id) break;
      if (data.thread_id !== state.activeThreadId) break;
      if (data.message_id in streamBuffers) {
        streamBuffers[data.message_id] += data.text || '';
        scheduleStreamRender(data.message_id);
      }
      break;
    }
    case 'stream_done': {
      if (!data.message_id) break;
      const fullMsg = data.message;
      if (!fullMsg) break;
      state.streamingIds.delete(data.message_id);
      delete streamBuffers[data.message_id];
      announceMessage(fullMsg);
      // Streamed reply delivered — clear the working state right away.
      if (state.thinking[data.thread_id]) {
        state.thinking[data.thread_id] = false;
        renderSidebar(); renderThreads();
        if (data.thread_id === state.activeThreadId) reflectComposerState();
      }
      if (data.thread_id === state.activeThreadId) {
        const existingEl = dom['messages'].querySelector(`[data-id="${CSS.escape(data.message_id)}"]`);
        if (existingEl) {
          // Only the ACTIVE thread's array gets the message — pushing a
          // background thread's reply here would bleed it into this chat on the
          // next full re-render. Push BEFORE building the node: messageEl only
          // offers "Regenerate" when the message is the last one in state.
          if (!state.messages.some((m) => m.id === data.message_id)) {
            state.messages.push(fullMsg);
            state.messages.sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
          }
          // messageEl wires its own image-load scroll pinning.
          // NIM: a picture-only final message has no row. Drop the streaming
          // placeholder rather than replacing it with nothing.
          const finalEl = messageEl(fullMsg);
          if (finalEl) existingEl.replaceWith(finalEl); else existingEl.remove();
          // The final rendered message may be taller than the streaming
          // placeholder (syntax-highlighted code blocks, full markdown).
          if (isNearBottom()) scrollToBottom(true);
        } else {
          // Missed stream_start (reconnect mid-stream) — treat like a new
          // message (appendMessageToView pushes into state.messages itself).
          appendMessageToView(fullMsg);
        }
      } else if (fullMsg.role !== 'user' && !state.unread[data.thread_id]) {
        // Streamed reply landed in a background thread — mark it unread, same
        // as the plain 'message' path does. Backfill threadBot first so the
        // sidebar dot resolves for bots whose threads we never loaded.
        if (data.bot_id && !state.threadBot[data.thread_id]) state.threadBot[data.thread_id] = data.bot_id;
        state.unread[data.thread_id] = fullMsg.created_at;
        renderSidebar(); renderThreads();
      }
      break;
    }
    case 'message_deleted': {
      const mid = data.message_id;
      if (!mid) break;
      state.messages = state.messages.filter((m) => m.id !== mid);
      if (data.thread_id === state.activeThreadId) {
        const msgEl = dom['messages'].querySelector(`[data-id="${CSS.escape(mid)}"]`);
        if (msgEl) {
          // Remove an orphaned date separator that immediately preceded this message.
          const prev = msgEl.previousElementSibling;
          msgEl.remove();
          if (prev && prev.classList.contains('date-sep')) {
            const next = prev.nextElementSibling;
            if (!next || next.classList.contains('date-sep') || !next.dataset?.id) {
              prev.remove();
            }
          }
        }
      }
      break;
    }
    case 'locked':
      // The full session expired server-side → fall back to Safe Mode.
      handleLocked();
      break;
    case 'comfy_service': {
      if (!data.state) break;
      state.comfy.state = data.state;
      state.comfy.gatewayOn = !!(data.gateway && data.gateway.on);
      renderComfyChip();
      if (comfyPanelOpen) refreshComfyPanel();
      break;
    }
    case 'terminal_state': {
      // Full-session only — Safe-Mode clients never receive this frame (the
      // server redactor drops it). Keeps the sidebar dot + open view live when
      // the session changes from another tab.
      if (!data.state) break;
      applyTerminalStatus(data);
      break;
    }
    case 'harness_state': {
      // Same posture. Carries whichever slice changed: jobs / service / models.
      applyHarnessFrame(data);
      break;
    }
    // A reaction overlay: transient, broadcast to every device, never stored.
    case 'reaction':
      handleReactionFrame(data);
      break;

    case 'reaction_pool':
      handlePoolFrame(data);
      break;

    // Avatar-pool telemetry — the background top-up ticking the Bots pane.
    case 'avatar_pool':
      apPools = data.pools || {};
      renderAvatarPoolPanel();
      break;

    case 'pong':
      break;
  }
}

// ===================== Event wiring =====================
// ---- Modal focus management (a11y) ----
// The custom .modal-backdrop dialogs (unlike the native <dialog> palette/prompt)
// neither trap focus nor restore it on close. Centralize both: while any modal
// backdrop is open, mark the main app + tab bar `inert` (so Tab can't escape
// behind it), move focus into the modal, and return focus to the trigger when
// the last modal closes. Hidden backdrops are display:none, so only genuinely
// open ones matter — no per-open-function changes required.
let modalFocusReturn = null;
function modalFocusables(root) {
  return Array.from(root.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter((elm) => elm.offsetParent !== null || elm === document.activeElement);
}
function focusIntoModal(backdrop) {
  const foci = modalFocusables(backdrop);
  // Skip a leading Close/Dismiss ✕ so the user doesn't land on "close".
  // aria-label is translated now, so it cannot be string-matched. The element id
  // and the translation KEY are the two identifiers that survive a locale switch.
  const target = foci.find((f) => !/close|dismiss/i.test(f.id || '')
      && !/=common\.(close|dismiss)\b/.test(f.getAttribute('data-i18n-attr') || ''))
    || foci[0];
  if (target) { try { target.focus(); } catch { /* ignore */ } return; }
  const modal = backdrop.querySelector('.modal') || backdrop;
  modal.setAttribute('tabindex', '-1');
  try { modal.focus(); } catch { /* ignore */ }
}
function initModalFocusGuard() {
  const backdrops = Array.from(document.querySelectorAll('.modal-backdrop'));
  if (!backdrops.length) return;
  const isOpen = (b) => !b.classList.contains('hidden');
  const anyOpen = () => backdrops.some(isOpen);
  const shown = new WeakMap();
  backdrops.forEach((b) => shown.set(b, isOpen(b)));
  const setInert = (on) => [dom.app, dom['mobile-tabs']].forEach((elm) => {
    if (!elm) return;
    if (on) elm.setAttribute('inert', ''); else elm.removeAttribute('inert');
  });
  const obs = new MutationObserver((records) => {
    let opened = null, changed = false;
    for (const r of records) {
      const b = r.target, now = isOpen(b);
      if (now === shown.get(b)) continue;
      shown.set(b, now); changed = true;
      if (now) opened = b;
    }
    if (!changed) return;
    if (anyOpen()) {
      if (modalFocusReturn == null) modalFocusReturn = document.activeElement;
      setInert(true);
      if (opened) focusIntoModal(opened);
    } else {
      setInert(false);
      const ret = modalFocusReturn; modalFocusReturn = null;
      if (ret && ret.focus && ret.isConnected && !ret.closest('[inert]')) { try { ret.focus(); } catch { /* ignore */ } }
    }
  });
  backdrops.forEach((b) => obs.observe(b, { attributes: true, attributeFilter: ['class'] }));
}

function wireEvents() {
  initModalFocusGuard();
  dom['send'].addEventListener('click', sendMessage);
  dom['retry-chip-btn'].addEventListener('click', retryPendingSends);
  dom['input'].addEventListener('input', () => { autosize(); updateSendEnabled(); });
  dom['input'].addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    // Enter sends (always). Ctrl/Cmd+Enter inserts newline.
    // Shift+Enter is native newline in textarea.
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      const ta = dom['input'];
      ta.setRangeText('\n', ta.selectionStart, ta.selectionEnd, 'end');
      autosize();
      updateSendEnabled();
      return;
    }
    if (e.shiftKey) return;
    e.preventDefault();
    sendMessage();
  });
  dom['input'].addEventListener('paste', (e) => {
    if (state.decoy) return;   // no image attachments in safe view
    const items = [...(e.clipboardData?.items || [])].filter((it) => it.type.startsWith('image/'));
    if (items.length) { e.preventDefault(); handleFiles(items.map((it) => it.getAsFile()).filter(Boolean)); }
  });

  dom['new-chat'].addEventListener('click', newChat);
  // Settings: in Safe Mode the gear opens the innocuous companions list (the
  // "More" button there reveals the keypad); unlocked, it's the full Bot Manager.
  dom['manage-bots'].addEventListener('click', () => {
    if (state.decoy) openCompanions();
    else openBotManager();
  });
  dom['comp-close'].addEventListener('click', hideCompanions);
  dom['companions-backdrop'].addEventListener('click', (e) => {
    if (e.target === dom['companions-backdrop']) hideCompanions();
  });
  dom['comp-more'].addEventListener('click', () => { hideCompanions(); showUnlock(); });

  // Collapse / expand the thread list (desktop).
  dom['collapse-threads'].addEventListener('click', () => setThreadListCollapsed(true));
  dom['expand-threads'].addEventListener('click', () => setThreadListCollapsed(false));

  // Header avatar click-to-zoom is wired (and Safe-Mode gated) exclusively by
  // paintHeaderAvatar's _zoomWired path — wiring it here too double-opened the
  // lightbox when unlocked and hit the PIN-gated /full route while locked.

  wireCropModal();
  wireSettingsTabs();
  wireFileServer();
  wireDrop();
  wireComfyPanel();
  wireTerminal();

  // Foldable / rotation: when the viewport crosses the mobile breakpoint
  // (fold/unfold), normalize the view so panels don't vanish or stack oddly.
  let wasMobile = isMobile();
  window.addEventListener('resize', () => {
    const nowMobile = isMobile();
    if (nowMobile === wasMobile) return;
    wasMobile = nowMobile;
    if (nowMobile) {
      navigate(state.activeThreadId ? 'chat' : 'threads');
    } else {
      setThreadListCollapsed(dom.app.classList.contains('tl-collapsed'));
    }
  });
  dom['bm-close'].addEventListener('click', () => closeBotManager());
  dom['bm-done'].addEventListener('click', saveBotManager);
  // Minimal-avatar toggle: pure CSS attribute swap, applies instantly and is
  // deliberately outside the Save/dirty flow (it's a device preference, not
  // roster state). Same keys the no-FOUC <head> script reads at boot.
  dom['bm-avatar-minimal'].addEventListener('change', () => {
    // Belt to syncMinimalAvatarRow's braces: `disabled` is a DOM property
    // anyone can clear, and this handler writes a stored preference.
    if (nimEnabled()) { syncMinimalAvatarRow(); return; }
    const on = dom['bm-avatar-minimal'].checked;
    if (on) document.documentElement.setAttribute('data-avatar-style', 'minimal');
    else document.documentElement.removeAttribute('data-avatar-style');
    try {
      if (on) localStorage.setItem('dispatch-avatar-style', 'minimal');
      else localStorage.removeItem('dispatch-avatar-style');
    } catch { /* private mode */ }
  });
  dom['botmanager-backdrop'].addEventListener('click', (e) => {
    if (e.target === dom['botmanager-backdrop']) closeBotManager();
  });

  dom['back-btn'].addEventListener('click', () => navigate('threads'));
  dom['scroll-bottom'].addEventListener('click', () => scrollToBottom());
  dom['messages'].addEventListener('scroll', () => {
    showScrollButton(!isNearBottom());
    if (dom['messages'].scrollTop < 80) loadOlderMessages();
    // Bug 1: Save scroll position on user scroll so it's restored on return.
    // Suppressed while openThread() swaps content (the innerHTML wipe clamps
    // scrollTop to 0 and would record a bogus position for the NEW thread).
    // At/near the bottom we clear instead of save: reopening should stick to
    // the latest messages, not a stale offset above replies that arrived since.
    if (!suppressScrollSave && state.activeThreadId) {
      if (isNearBottom()) delete state.scrollPositions[state.activeThreadId];
      else state.scrollPositions[state.activeThreadId] = dom['messages'].scrollTop;
    }
  });

  // ⧉ pops the current conversation into its own window. The window is NAMED
  // per thread, so clicking again re-targets the existing popout instead of
  // stacking duplicates; several different threads = several windows.
  dom['popout-btn'].addEventListener('click', () => {
    const th = state.activeThread;
    if (!th) return;
    const url = `/?popout=1&thread=${encodeURIComponent(th.id)}`
      + `&bot=${encodeURIComponent(th.bot_id || state.selectedBotId || '')}`;
    window.open(url, `dispatch-pop-${th.id}`, 'popup=yes,width=560,height=760');
  });

  dom['thread-menu-btn'].addEventListener('click', (e) => {
    e.stopPropagation();
    const pinBtn = document.getElementById('thread-menu-pin');
    // Re-point the key rather than writing bare text: the next language switch
    // re-runs the DOM pass, which would otherwise reset this to "Pin".
    if (pinBtn) setI18nText(pinBtn, (state.activeThread && state.activeThread.is_pinned) ? 'chat.unpin' : 'chat.pin');
    toggleThreadMenu();
  });
  dom['thread-menu'].querySelectorAll('button').forEach((b) =>
    b.addEventListener('click', () => threadAction(b.dataset.act)));
  document.addEventListener('click', () => toggleThreadMenu(false));

  dom['attach-btn'].addEventListener('click', () => dom['file-input'].click());
  // ⚡ pack curation, 🩺 host dashboard and 🔌 Connect an AI are Settings tabs
  // now — see wireSettingsTabs() and openSettingsTab().
  dom['file-input'].addEventListener('change', (e) => { handleFiles([...e.target.files]); e.target.value = ''; });

  // Drag & drop images onto the chat view.
  const cv = $('chatview');
  cv.addEventListener('dragover', (e) => e.preventDefault());
  cv.addEventListener('drop', (e) => {
    e.preventDefault();
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) handleFiles(files);
  });

  dom['mobile-tabs'].querySelectorAll('.tab').forEach((t) =>
    t.addEventListener('click', () => navigate(t.dataset.view)));

  window.addEventListener('keydown', (e) => {
    // The unlock overlay owns the keyboard while it's open (handled elsewhere).
    if (!dom['lock-screen'].classList.contains('hidden')) return;
    if (e.key === 'Escape') {
      const lb = document.querySelector('.lightbox');
      // Route through the ✕ button so the lightbox's own close() runs
      // (pauses video, detaches its window pan listeners).
      if (lb) { const x = lb.querySelector('.lightbox-close'); if (x) x.click(); else lb.remove(); return; }
      closeBotManager();   // confirms first if there are unsaved edits
      dom['fileserver-backdrop'].classList.add('hidden');
      dom['crop-backdrop'].classList.add('hidden');
      dom['search-backdrop'].classList.add('hidden');
      dom['recover-backdrop'].classList.add('hidden');
      dom['tx-backdrop'].classList.add('hidden');
      dom['companions-backdrop'].classList.add('hidden');
      if (!dom['terminal-model-backdrop'].classList.contains('hidden')) closeModelPicker();
      if (!dom['comfy-launch'].classList.contains('hidden')) hideComfyLaunch();
      if (!dom['comfy-logs-backdrop'].classList.contains('hidden')) closeComfyLogs();
      else if (!dom['comfy-backdrop'].classList.contains('hidden')) closeComfyPanel();
      toggleThreadMenu(false);
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') { e.preventDefault(); newChat(); }
  });
}

// ===================== Connect an AI =====================
// The panel module owns its own fetches; this is the whole of its coupling to
// the app — the hooks it needs to answer "am I allowed?", "what happens after
// a provider is saved?" and, since it became a Settings tab, "where do I live?".
function llmCtx() {
  return {
    toast,
    isDecoy: () => state.decoy,
    onLocked: () => handleLocked(),
    onConnected: (bot) => afterProviderConnected(bot),
    openSettingsTab,
    closeSettings: () => closeBotManager(),
    settingsTabActive: (tab) => settingsOpen(tab),
  };
}

// Same idea for the host dashboard: hooks in, no app state out.
function dashCtx() {
  return {
    t,
    toast,
    isDecoy: () => state.decoy,
    onLocked: () => handleLocked(),
    openSettingsTab,
  };
}

// The first-run "Connect an AI" card in an empty chat, and anything else that
// wants provider setup: one door, and it is the 🔌 Settings tab.
function openLlm() {
  openSettingsTab('ai');
}

// A connected provider is a real bot, so finish the job the way a person would
// expect: it appears in the sidebar, it is selected, and there is a chat open
// with the cursor in it. Landing back on an empty screen after "Save & chat"
// would make the whole flow feel like it had not worked.
async function afterProviderConnected(bot) {
  if (!bot || !bot.id) return;
  // Keep the first-run test honest without another round trip to
  // /api/auth/status: we know a provider now exists because we just made one.
  if (state.auth.features) state.auth.features.api_bots = (state.auth.features.api_bots || 0) + 1;
  try {
    const r = await api.bots();
    state.bots = r.bots || [];
  } catch { /* non-fatal: the server's own `bots` WS frame catches us up */ }
  renderSidebar();
  await selectBot(bot.id);
  // selectBot opens the newest existing thread on desktop; a brand-new bot has
  // none, so make one rather than leaving the operator on the empty state.
  if (!state.activeThreadId) await newChat();
  if (isMobile()) navigate('chat');
  try { dom['input'].focus(); } catch { /* focus is best-effort */ }
}

// ===================== Lock / PIN / safe-view =====================
let socket = null;

function getSocket() {
  if (!socket) {
    socket = new ChatSocket({
      onMessage: handleWs,
      onStatus: (ok) => setConnected(ok),
      onReconnect: () => {
        toast(t('toast.reconnected')); resync(); refreshUnread();
        resendPendingSends();   // WS send-ack protocol: replay unacked sends
        // The chip can go stale while disconnected (service changed outside
        // DisPatch) — comfy_service frames are only broadcast on control ops.
        if (state.comfyEnabled && !state.decoy) refreshComfyChipOnce();
      },
    });
  }
  return socket;
}

// Lock button shows only in full (unlocked) mode; the attach "＋" shows in BOTH
// modes (Safe Mode can upload too — media is stripped from the locked *view*,
// not from sending); body class lets CSS adapt the safe view.
function applyAuthChrome() {
  const full = state.auth.pinSet && !state.decoy;
  // The visible pack differs between Safe Mode and full access, so re-fetch it
  // (and drop any overlay mid-flight) every time that boundary is crossed.
  if (applyAuthChrome._lastDecoy !== state.decoy) {
    applyAuthChrome._lastDecoy = state.decoy;
    resetReactions();
    loadReactions(true);
  }
  dom['lock-now'].classList.toggle('hidden', !full);
  dom['bm-lock'].classList.toggle('hidden', !full);
  dom['attach-btn'].classList.remove('hidden');
  document.body.classList.toggle('decoy-mode', state.decoy);
  // ComfyUI is full-access only, regardless of PIN being set at all.
  dom['comfy-chip'].classList.toggle('hidden', state.decoy || !state.comfyEnabled);
  // File Server is full-access only too. It used to sit inside the Bot Manager,
  // which a locked session can never open (the gear routes to Companions), so
  // moving it to the sidebar would have exposed it in Safe Mode. /api/files* is
  // already barred server-side for decoy sessions; this keeps the UI in step
  // instead of showing family devices a button that only ever 403s.
  dom['fs-chip'].classList.toggle('hidden', state.decoy);
  // The one-way drop is the LOCKED side's counterpart to that File Server
  // button: it exists only in Safe Mode, because an unlocked session already
  // has both 📁 and the composer ＋. Server-side /api/drop is deliberately
  // reachable without a session — this is just where you press it.
  dom['drop-btn'].classList.toggle('hidden', !state.decoy);
  // The three admin Settings tabs — pack curation (upload/edit/delete), the
  // host dashboard, and provider setup that writes an API key to disk. All
  // three are full-session only server-side, so a locked device would collect
  // nothing but 403s; the rail buttons they replaced were hidden by exactly
  // this rule, and the tabs inherit it.
  //
  // In practice a locked session never sees this modal at all (the gear routes
  // to the Companions panel — see the #manage-bots handler), but the idle lock
  // can land while it is open, and "hidden" has to mean not-in-the-tab-order
  // too. Hence: hide, then fall back to a tab that is still there.
  for (const tab of ADMIN_TABS) {
    const b = document.getElementById(`stab-${tab}`);
    if (b) b.classList.toggle('hidden', state.decoy);
  }
  if (state.decoy) {
    unmountDashboard();
    unmountReactionManager();
    if (ADMIN_TABS.includes(settingsTab)) setSettingsTab('bots');
  }
  // Dedicated unlock padlock: visible ONLY in locked/Safe Mode so family
  // devices have an obvious way into admin mode (opens the existing PIN
  // overlay). Hidden once unlocked — the 🔒 lock-now button takes over.
  if (dom['unlock-btn']) dom['unlock-btn'].classList.toggle('hidden', !state.decoy);
}

// ---- Idle auto-lock (full mode only → drops back to Safe Mode) ----
let idleTimer = null;
let lastBump = 0;
function armIdleTimer() {
  if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
  if (!state.auth.pinSet || state.decoy) return;   // only while unlocked
  if (state.auth.remembered) return;   // remembered device: no auto-lock
  const ms = Math.max(30, state.auth.lockTimeout || 600) * 1000;
  idleTimer = setTimeout(lockNow, ms);
}
function bumpIdle() {
  if (!state.auth.pinSet || state.decoy || !state.started) return;
  const now = Date.now();
  if (now - lastBump < 5000) return;   // throttle re-arming
  lastBump = now;
  armIdleTimer();
}

// ---- Lock screen + PIN entry ----
let lockPin = '';

function renderLockDots() {
  const d = dom['lock-dots'];
  d.innerHTML = '';
  const n = lockPin.length;
  for (let i = 0; i < Math.max(n, 4); i++) {
    d.append(el('span', { class: 'lock-dot' + (i < n ? ' filled' : '') }));
  }
}
function setLockError(m) {
  dom['lock-error'].textContent = m || '';
  dom['lock-error'].hidden = !m;
  // Wrong-PIN shake on the dots row (CSS no-ops it under reduced motion).
  if (m) {
    const d = dom['lock-dots'];
    d.classList.remove('shake');
    void d.offsetWidth;   // restart the animation on repeated errors
    d.classList.add('shake');
    setTimeout(() => d.classList.remove('shake'), 400);
  }
}
function cleanErr(e) { return (e && e.message ? e.message : String(e)).replace(/^\d+:\s*/, ''); }

// Safe-Mode "settings": clicking the gear when locked opens a plain companions
// list — no keypad, no lock hint — so a casual snoop sees only an innocuous
// roster. The discreet "More" button at the bottom is the only path to the PIN
// keypad; someone who doesn't know it's there won't realise the app unlocks.
function renderCompanions() {
  const list = dom['comp-list'];
  list.innerHTML = '';
  const bots = visibleBots();   // server already filtered to safe bots
  if (!bots.length) {
    list.append(el('p', { class: 'muted comp-empty', text: t('companions.empty') }));
    return;
  }
  bots.forEach((bot) => {
    // View-only: Safe Mode is VIEW + SEND, so unlike the Bot Manager the
    // avatar here is NOT a "change photo" affordance (the server 403s the
    // POST for decoy sessions anyway).
    let av;
    // NIM: fall through to the letter block below — no <img>, no request.
    const url = nimEnabled() ? '' : bot.avatar_url;
    if (url) {
      // No data-full here: Companions is a Safe-Mode screen and the full-res
      // route is PIN-gated, so the affordance was a guaranteed 403.
      av = el('img', { class: 'comp-avatar', src: url, alt: bot.name,
                       draggable: 'false' });
      av.addEventListener('error', () => {
        if (!av.isConnected) return;
        const fb = tintLetterAvatar(
          el('div', { class: 'comp-avatar letter-avatar',
                      text: botLetter(bot) }), bot);
        av.replaceWith(fb);
      });
    } else {
      av = tintLetterAvatar(
        el('div', { class: 'comp-avatar letter-avatar',
                    text: botLetter(bot) }), bot);
    }
    const row = el('div', { class: 'comp-row' }, [
      av,
      el('span', { class: 'comp-name', text: bot.name }),
    ]);
    // Which companions carry reaction images. Read-only here: Safe Mode is
    // VIEW + SEND, and /api/bots/order is decoy-blocked server-side — the flag
    // is set in the Bot Manager, this just shows the family what's on.
    if (botHasReactions(bot.id)) {
      row.append(el('span', { class: 'comp-react', text: '⚡', title: t('companions.reacts') }));
    }
    list.append(row);
  });
}
function openCompanions() {
  renderCompanions();
  dom['companions-backdrop'].classList.remove('hidden');
}
function hideCompanions() {
  dom['companions-backdrop'].classList.add('hidden');
}

// The lock card is used for two different jobs — "Locked" (the Safe-Mode face)
// and "Unlock" (deliberate PIN entry) — so its heading pair is state, not
// markup. Move the keys rather than the text; see setI18nText.
function setLockScreenLabels(titleKey, subKey) {
  setI18nText(dom['lock-title'], titleKey);
  setI18nText(dom['lock-sub'], subKey);
}

// The unlock overlay floats over the running Safe-Mode app (no teardown).
function showUnlock() {
  lockPin = '';
  renderLockDots();
  setLockError('');
  hideRecover();
  setLockScreenLabels('lock.unlock_title', 'lock.unlock_sub');
  // Remember-this-device: only offered when the server has the feature on;
  // the checkbox re-arms to this device's last choice.
  const offer = (state.auth.rememberDays || 0) > 0 && allowsPersistentSession();
  dom['lock-remember-row'].classList.toggle('hidden', !offer);
  if (offer) {
    let pref = false;
    try { pref = localStorage.getItem('lc-remember') === '1'; } catch {}
    dom['lock-remember'].checked = pref;
  }
  syncLockNimRow();
  dom['lock-screen'].classList.remove('hidden');
}
function hideUnlock() {
  dom['lock-screen'].classList.add('hidden');
  setLockScreenLabels('lock.locked', 'lock.enter_pin');
  lockPin = '';
  renderLockDots();
  setLockError('');
  hideRecover();
}

// Close every overlay that can float above the app — locking must never leave
// Bot Manager avatars, File Server thumbnails, or a lightboxed image on screen.
function closeAllOverlays() {
  // Settings carries Security, Reactions, Health and AI models as tabs now, so
  // hiding its one backdrop closes all five — the tab observer stops whatever
  // the active pane had running.
  ['botmanager-backdrop', 'fileserver-backdrop', 'crop-backdrop',
   'companions-backdrop', 'search-backdrop', 'recover-backdrop', 'tx-backdrop',
   'drop-backdrop']
    .forEach((k) => dom[k] && dom[k].classList.add('hidden'));
  // Close the ComfyUI overlays through their own close paths — raw-hiding the
  // backdrops left comfyPanelOpen + the 5s poll timer alive across a lock
  // (a locked device polling /status forever, 403 each time).
  closeComfyPanel();
  closeComfyLogs();
  // The terminal is full-session only — never leave its socket/view alive
  // across a drop to Safe Mode.
  if (terminalOpen) closeTerminalView();
  if (harnessOpen) closeHarnessView();
  // Never n.remove() a lightbox directly: that skips pausing the video and
  // unbinding its window-level pan listeners.
  document.querySelectorAll('.lightbox').forEach((n) => {
    if (typeof n._close === 'function') n._close(); else n.remove();
  });
  // Native <dialog>s (rename/confirm prompts, Cmd+K palette) live in the
  // browser top layer, above everything — they too must never survive a lock.
  document.querySelectorAll('dialog[open]').forEach((d) => { try { d.close(); } catch { /* ignore */ } });
  toggleThreadMenu(false);
  bmBots = [];
  bmDirty = false;
}

// Rebuild the app for the current mode: drop the socket + sensitive caches and
// reconnect, so the WebSocket re-handshakes at the right access level and
// messages are re-fetched (full or redacted accordingly).
async function reboot() {
  state.started = false;
  if (socket) { socket.stop(); socket = null; }
  // A mode/session change invalidates unacked sends — never replay them on
  // the new (possibly Safe-Mode) socket.
  dropAllPendingSends();
  state.messages = []; state.threads = [];
  state.activeThreadId = null; state.activeThread = null;
  state.unread = {}; state.thinking = {}; state.progress = {};
  state.streamingIds = new Set();
  // Partial reply text must not survive a drop to Safe Mode.
  Object.keys(streamBuffers).forEach((k) => delete streamBuffers[k]);
  streamPending.clear();
  progressOpen = false;
  closeAllOverlays();
  clearChatView();
  await startApp();
}

// Drop to Safe Mode (manual lock, idle auto-lock, or server-signalled expiry).
async function goSafe(announce) {
  // Flip the mode SYNCHRONOUSLY: every trigger (idle timer, REST 401, WS
  // locked frame) guards on state.decoy, so setting it before any await
  // prevents concurrent goSafe → double reboot → leaked WebSocket.
  state.auth.authenticated = false;
  state.auth.decoy = true;
  state.auth.remembered = false;   // server-side, locking also forgets the device
  state.decoy = true;
  if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
  closeAllOverlays();
  try { await api.lock(); } catch { /* best-effort: clears the cookie/session */ }
  await reboot();
  if (announce) toast(announce);
}

async function lockNow() {
  if (!state.auth.pinSet || state.decoy) return;
  await goSafe(t('lock.locked_toast'));
}

function handleLocked() {
  if (!state.auth.pinSet || state.decoy) return;   // already safe / no lock
  goSafe(t('lock.auto_locked_toast'));
}

function keypadPress(k) {
  setLockError('');
  if (k === 'back') lockPin = lockPin.slice(0, -1);
  else if (k === 'clear') lockPin = '';
  else if (/^[0-9]$/.test(k) && lockPin.length < 32) lockPin += k;
  renderLockDots();
}

async function submitUnlock() {
  if (!lockPin) return;
  dom['lock-submit'].disabled = true;
  try {
    const remember = (state.auth.rememberDays || 0) > 0
      && allowsPersistentSession()          // never persist on a privacy device
      && dom['lock-remember'].checked;
    try { localStorage.setItem('lc-remember', remember ? '1' : '0'); } catch {}
    const r = await api.unlock(lockPin, remember);
    state.auth.authenticated = true;
    state.auth.decoy = false;
    state.auth.remembered = !!(r && r.remembered);
    state.decoy = false;
    lockPin = '';
    hideUnlock();
    await reboot();
    armIdleTimer();
    toast(t(state.auth.remembered ? 'lock.unlocked_remembered' : 'lock.unlocked'));
  } catch (e) {
    lockPin = '';
    renderLockDots();
    setLockError(cleanErr(e) || t('lock.wrong_pin'));
  } finally {
    dom['lock-submit'].disabled = false;
  }
}

function showRecover() {
  dom['lock-recover'].classList.remove('hidden');
  dom['lock-recover-hint'].textContent =
    t('lock.recover_hint', { path: state.auth.recoveryPath || 'RECOVERY-CODE.txt' });
  dom['recover-code'].value = '';
  setTimeout(() => dom['recover-code'].focus(), 30);
}
function hideRecover() { dom['lock-recover'].classList.add('hidden'); }

async function submitRecover() {
  const code = dom['recover-code'].value.trim();
  if (!code) return;
  dom['recover-submit'].disabled = true;
  try {
    await api.recover(code);
    state.auth.pinSet = false;
    state.auth.authenticated = true;
    state.auth.decoy = false;
    state.decoy = false;
    hideUnlock();
    await reboot();
    toast(t('lock.pin_reset'));
  } catch (e) {
    setLockError(cleanErr(e) || t('lock.wrong_code'));
  } finally {
    // Never leave the secret sitting in the DOM: a recovery code is single-use
    // and one-shot, and the field is otherwise still populated behind the lock
    // screen (and in the next screenshot / bug report).
    dom['recover-code'].value = '';
    dom['recover-submit'].disabled = false;
  }
}

// ---- Security modal (set / change / remove PIN) ----
function setSecError(m) { dom['sec-error'].textContent = m || ''; dom['sec-error'].hidden = !m; }

// Every string in the Security modal that depends on server state: the two
// mode-dependent labels, the two counted sentences, and the recovery note.
// Split out of activateSecurityPane() so a language switch can repaint the tab
// in place without re-running the focus/value side effects.
function renderSecurityStrings() {
  const pinSet = state.auth.pinSet;
  setI18nText(dom['sec-new-label'], pinSet ? 'security.new_pin' : 'security.create_pin');
  setI18nText(dom['sec-save'], pinSet ? 'security.change_pin' : 'security.set_pin');
  setI18nText(dom['sec-status-line'], pinSet
    ? (state.auth.remembered ? 'security.status_remembered' : 'security.status_set')
    : 'security.status_none');
  setI18nText(dom['sec-remember-label'], 'security.remember_label',
    { days: state.auth.rememberDays || 30 });
  setI18nText(dom['sec-forget-devices'], 'security.forget_devices',
    { count: state.auth.trustedDevices || 0 });
  // security.note is one of the two strings that carry markup we own — a <br>
  // and the two <code id="sec-…-path"> elements the recovery instructions point
  // at. It therefore goes in as HTML; the SERVER-supplied paths below are passed
  // RAW because setI18nHtml escapes every non-number var for us now.
  setI18nHtml(dom['sec-note'], 'security.note', {
    minutes: Math.round((state.auth.lockTimeout || 600) / 60),
    recoveryPath: state.auth.recoveryPath || 'RECOVERY-CODE.txt',
    configPath: state.auth.configPath || 'security.yaml',
  });
}

// The Security & PIN surface is the 🔐 tab in Settings, not a modal of its
// own: it was already a plain field stack with two footer buttons, which is
// exactly what a pane is. This is what the tab controller calls when it
// becomes visible — populate, clear, focus the first field somebody types in.
function activateSecurityPane() {
  const pinSet = state.auth.pinSet;
  dom['sec-current-wrap'].hidden = !pinSet;
  dom['sec-remove'].hidden = !pinSet;
  // Remember-device controls: only meaningful once a PIN exists.
  dom['sec-remember-wrap'].hidden = !pinSet;
  dom['sec-remember-enable'].checked = (state.auth.rememberDays || 0) > 0;
  dom['sec-forget-devices'].hidden = !(pinSet && (state.auth.trustedDevices || 0) > 0);
  renderSecurityStrings();
  dom['sec-current'].value = dom['sec-new'].value = dom['sec-confirm'].value = '';
  setSecError('');
  setTimeout(() => (pinSet ? dom['sec-current'] : dom['sec-new']).focus(), 40);
}

async function saveSecurity() {
  const cur = dom['sec-current'].value;
  const np = dom['sec-new'].value;
  const cf = dom['sec-confirm'].value;
  const minPin = state.auth.minPin || 4;
  if (np.length < minPin) { setSecError(t('security.too_short', { count: minPin })); return; }
  if (np !== cf) { setSecError(t('security.mismatch')); return; }
  dom['sec-save'].disabled = true;
  try {
    await api.setupPin(np, cur);
    // A new full session cookie was just issued; reconnect so the live socket
    // uses it (the old session was revoked) and the idle timer (re)starts.
    state.auth.pinSet = true;
    state.auth.authenticated = true;
    state.auth.decoy = false;
    state.decoy = false;
    dom['botmanager-backdrop'].classList.add('hidden');
    await reboot();
    armIdleTimer();
    toast(t('security.saved'));
  } catch (e) {
    setSecError(cleanErr(e));
  } finally {
    // Same reason as the recovery code: PINs do not linger in input values,
    // success or failure. A retry retypes them.
    dom['sec-current'].value = dom['sec-new'].value = dom['sec-confirm'].value = '';
    dom['sec-save'].disabled = false;
  }
}

async function removePin() {
  const cur = dom['sec-current'].value;
  if (!cur) { setSecError(t('security.need_current')); return; }
  if (!await uiConfirm(t('security.remove_confirm'), { danger: true })) return;
  dom['sec-remove'].disabled = true;
  try {
    await api.setupPin('', cur);   // empty new PIN = remove the lock
    state.auth.pinSet = false;
    state.auth.authenticated = true;
    state.auth.decoy = false;
    if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
    dom['botmanager-backdrop'].classList.add('hidden');
    await reboot();                // reconnect cleanly with the lock gone
    toast(t('security.removed'));
  } catch (e) {
    setSecError(cleanErr(e));
  } finally {
    dom['sec-current'].value = dom['sec-new'].value = dom['sec-confirm'].value = '';
    dom['sec-remove'].disabled = false;
  }
}

function wireAuthEvents() {
  // Keypad + lock screen
  dom['keypad'].querySelectorAll('button').forEach((b) =>
    b.addEventListener('click', () => keypadPress(b.dataset.k)));
  dom['lock-submit'].addEventListener('click', submitUnlock);
  // NIM from the lock screen. Enabling is always allowed; the disabled state
  // (set by syncLockNimRow) is what stops a locked device turning it back off,
  // and the guard below repeats that check because `disabled` is a DOM
  // property anyone can clear from devtools — cheap, and keeps the rule in one
  // place rather than trusting the markup.
  dom['lock-nim'].addEventListener('change', () => {
    const want = dom['lock-nim'].checked;
    if (!want && !canDisableNim(state.decoy)) { syncLockNimRow(); return; }
    setNim(want);
    applyNimChange();
  });
  dom['lock-forgot'].addEventListener('click', showRecover);
  dom['lock-cancel'].addEventListener('click', hideUnlock);
  dom['recover-cancel'].addEventListener('click', hideRecover);
  dom['recover-submit'].addEventListener('click', submitRecover);
  dom['recover-code'].addEventListener('keydown', (e) => { if (e.key === 'Enter') submitRecover(); });
  // Click the dark area (outside the card) or press Esc to dismiss the unlock.
  dom['lock-screen'].addEventListener('click', (e) => { if (e.target === dom['lock-screen']) hideUnlock(); });
  window.addEventListener('keydown', (e) => {
    if (dom['lock-screen'].classList.contains('hidden')) return;
    if (e.key === 'Escape') { hideUnlock(); return; }
    if (!dom['lock-recover'].classList.contains('hidden')) return;  // typing a code
    if (/^[0-9]$/.test(e.key)) keypadPress(e.key);
    else if (e.key === 'Backspace') { e.preventDefault(); keypadPress('back'); }
    else if (e.key === 'Enter') submitUnlock();
  });

  // Lock / logout buttons
  dom['lock-now'].addEventListener('click', lockNow);
  dom['bm-lock'].addEventListener('click', lockNow);
  // Dedicated unlock padlock (locked mode) → the existing PIN overlay.
  if (dom['unlock-btn']) dom['unlock-btn'].addEventListener('click', showUnlock);

  // Remember-device toggle + forget-all (Security modal)
  dom['sec-remember-enable'].addEventListener('change', async () => {
    const on = dom['sec-remember-enable'].checked;
    try {
      const r = await api.rememberConfig(on ? 30 : 0);
      state.auth.rememberDays = (r && r.remember_days) || 0;
      if (!on) {
        // Server forgot every device; this session is demoted to idle-expiry.
        state.auth.remembered = false;
        state.auth.trustedDevices = 0;
        dom['sec-forget-devices'].hidden = true;
        armIdleTimer();
      }
      renderSecurityStrings();
      toast(t(on ? 'security.remember_on' : 'security.remember_off'));
    } catch (e) {
      dom['sec-remember-enable'].checked = !on;   // revert on failure
      setSecError(cleanErr(e));
    }
  });
  dom['sec-forget-devices'].addEventListener('click', async () => {
    if (!await uiConfirm(t('security.forget_confirm'), { danger: true })) return;
    try {
      await api.forgetDevices();
      state.auth.remembered = false;
      state.auth.trustedDevices = 0;
      dom['sec-forget-devices'].hidden = true;
      armIdleTimer();   // this session is now a normal idle-expiring one
      renderSecurityStrings();
      toast(t('security.forgotten'));
    } catch (e) { setSecError(cleanErr(e)); }
  });

  // Security & PIN — the 🔐 Settings tab. No open/close wiring of its own any
  // more: the tab controller activates it, and the Settings dialog's ✕ /
  // Escape / backdrop close it like every other pane.
  dom['sec-save'].addEventListener('click', saveSecurity);
  dom['sec-remove'].addEventListener('click', removePin);
  [dom['sec-current'], dom['sec-new'], dom['sec-confirm']].forEach((inp) =>
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveSecurity(); }));

  // Idle auto-lock activity
  ['mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll', 'wheel'].forEach((ev) =>
    window.addEventListener(ev, bumpIdle, { passive: true }));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (state.auth.pinSet && state.started) {
      api.authStatus().then((s) => { if (!s.authenticated) handleLocked(); }).catch(() => {});
    }
    // Returning to the tab: the ComfyUI service may have changed outside
    // DisPatch (CLI stop, crash) with no WS frame — resync the chip.
    if (state.started && state.comfyEnabled && !state.decoy) refreshComfyChipOnce();
  });
}

// ===================== Boot =====================
// Resolve the ComfyUI feature flag + first chip state in the background.
// Only 404 (feature off) / 403 (no access) mean "hide the chip"; a transient
// 502/network blip must not disable the feature for the whole session — show
// the chip in its error state instead.
async function refreshComfyFeature() {
  // The server now advertises this in /api/auth/status, so a default install
  // (feature off) no longer discovers it by making a request that 404s.
  if (state.auth.features && state.auth.features.comfy === false) {
    state.comfyEnabled = false;
    return;
  }
  try {
    state.comfyEnabled = true;
    applyComfyStatus(await api.comfyServiceStatus());
  } catch (e) {
    if (e.status === 404 || e.status === 403) state.comfyEnabled = false;
    else { state.comfy.state = 'error'; renderComfyChip(); }
  }
  applyAuthChrome();   // chip visibility reflects the resolved flag
}

async function startApp() {
  if (state.started) return;
  state.started = true;
  // Delegated code-block / file-path copy handlers (idempotent, survives
  // re-render + streaming since it binds on document, not per message).
  installMarkdownHandlers(toast);
  // Stale unread dots (>24h) auto-dismiss at render time; navigation and WS
  // events already re-render, but a re-paint on an idle, untouched tab lets a
  // dot that has just crossed the 24h line clear itself without interaction.
  // Only fires while something is actually unread, so an idle app stays quiet.
  if (!state.staleUnreadTimer) {
    state.staleUnreadTimer = setInterval(() => {
      if (Object.keys(state.unread).length) { renderSidebar(); renderThreads(); }
    }, 15 * 60 * 1000);
  }
  try {
    const r = await api.bots();   // server filters to safe bots for Safe Mode
    state.bots = r.bots || [];
  } catch (e) {
    // A 401 here means the server is treating us as Safe Mode — fall into it
    // and keep going (the WS `hello` will deliver the bot list). Never leave
    // the app dead with no socket.
    if (e.status === 401) {
      state.decoy = true; state.auth.decoy = true; state.auth.authenticated = false;
    } else {
      toast(t('toast.bots_failed', { error: e.message }), true);
    }
  }
  // Feature flag: skip entirely for a Safe-Mode landing (would just 403) —
  // reboot() re-runs startApp() after a real unlock. NON-BLOCKING: the status
  // probe shells out to systemctl server-side and can take seconds on a slow
  // boot — it must never hold the first paint (and the boot veil) hostage.
  if (!state.decoy) {
    // Refresh the feature inventory FIRST, and only then probe. reboot() calls
    // startApp() directly rather than refreshAuthAndBoot(), so after an unlock
    // `state.auth.features` was still the LOCKED payload — `{}`, which is
    // truthy but says nothing. `features.terminal === false` was therefore
    // false, the probes ran, and an install with the terminal and the harness
    // switched off answered 404 twice on every unlock. The requests are
    // harmless; the console errors the browser writes for them are not — they
    // are the first thing anyone looks at when something else breaks.
    ensureFeatures().finally(() => {
      refreshComfyFeature();
      refreshTerminalFeature();
      refreshHarnessFeature();
    });
  } else {
    state.comfyEnabled = false;
    state.terminalEnabled = false;
    state.harnessEnabled = false;
  }
  applyAuthChrome();
  renderSidebar();
  // Land on the bots list — nothing preselected; chats load when the user
  // picks a bot.
  clearChatView();
  renderThreads();

  getSocket().connect();

  if (POPOUT) {
    await bootPopout();
  } else {
    // Anchor the history root on the bots screen (the app's main screen) so
    // Back from deeper views never leaves the app in one step.
    history.replaceState({ view: 'bots' }, '');
    if (isMobile()) setView('bots');
  }
  try { setThreadListCollapsed(localStorage.getItem('tl-collapsed') === '1'); } catch {}
  refreshUnread();
  armIdleTimer();
  dismissBootVeil();   // first real render is on screen — fade the veil away
}

// The boot veil (index.html) hides the assembling UI until the first real
// render. Fades out via the .gone transition, then leaves the DOM entirely.
// Safe to call repeatedly (reboot() re-runs startApp after lock/unlock).
function dismissBootVeil() {
  const v = document.getElementById('boot-veil');
  if (!v) return;
  v.classList.add('gone');
  setTimeout(() => v.remove(), 500);
}

// Popout boot: open exactly the requested conversation. The window is narrow,
// so the mobile view logic kicks in — pin the view to the chat pane; the
// popout CSS hides every other affordance. Safe Mode works too: the popout
// inherits whatever tier this device's session has.
async function bootPopout() {
  const botId = POPOUT_PARAMS.get('bot');
  const threadId = POPOUT_PARAMS.get('thread');
  if (botId) await selectBot(botId);
  if (threadId && state.threads.some((th) => th.id === threadId)) {
    await openThread(threadId);
  } else if (threadId) {
    // Thread gone (deleted, or a stale link) — selectBot already landed on the
    // bot's newest chat on desktop; say why this isn't the one asked for.
    toast(t('chat.popout_gone'));
  }
  if (isMobile()) setView(state.activeThreadId ? 'chat' : 'threads');
  history.replaceState({ view: 'chat' }, '');
}

async function refreshAuthAndBoot() {
  let s;
  try { s = await api.authStatus(); }
  catch (e) { toast(t('toast.auth_failed', { error: e.message }), true); s = { pin_set: false, authenticated: true }; }
  state.auth = {
    pinSet: !!s.pin_set,
    authenticated: !!s.authenticated,
    decoy: !!s.decoy,
    lockTimeout: s.lock_timeout_seconds || 600,
    minPin: s.min_pin_length || 4,
    recoveryPath: s.recovery_path || '',
    configPath: s.config_path || '',
    rememberDays: s.remember_days || 0,
    remembered: !!s.remembered,
    trustedDevices: s.trusted_devices || 0,
    // Optional subsystems this build has on. Absent for a limited session
    // (both are admin-only), which the probes below treat as "unknown" and
    // fall back to asking.
    features: s.features || null,
  };
  // No blocking gate: Safe Mode is the default landing when a PIN is set but
  // we're not unlocked. Full access requires a valid session.
  state.decoy = state.auth.pinSet && !state.auth.authenticated;
  await startApp();
}

// ===================== Search messages =====================
let searchTimer = null;
let searchHits = [];
function openSearch() {
  if (state.decoy) { toast(t('toast.not_available'), true); return; }  // neutral: no lock hint in Safe Mode
  dom['search-backdrop'].classList.remove('hidden');
  dom['search-input'].value = '';
  dom['search-results'].innerHTML = '';
  dom['search-results'].append(el('div', { class: 'cmdk-empty', text: t('search.prompt') }));
  searchHits = [];
  setTimeout(() => dom['search-input'].focus(), 30);
}
function closeSearch() { dom['search-backdrop'].classList.add('hidden'); }
let searchSeq = 0;
async function runSearch(q) {
  q = (q || '').trim();
  // Sequence token: a slow earlier query resolving after a newer one must not
  // overwrite the newer results (Enter would then open a hit from stale text).
  const seq = ++searchSeq;
  const box = dom['search-results'];
  box.innerHTML = '';
  if (!q) { box.append(el('div', { class: 'cmdk-empty', text: t('search.prompt') })); return; }
  try {
    const r = await api.search(q);
    if (seq !== searchSeq) return;
    searchHits = r.results || [];
    renderSearchResults(q);
  } catch (e) { if (seq === searchSeq) box.append(el('div', { class: 'cmdk-empty', text: e.message })); }
}
// Backend wraps FTS matches with the U+E000 / U+E001 Private Use markers
// (database.py FTS_MARK_OPEN/CLOSE); turn them into <mark>. These replaced the
// old U+2068/U+2069 bidi isolates, which carried real directional semantics and
// would have misrendered right-to-left search results.
function highlightSnippet(snippet) {
  const span = el('span');
  String(snippet || '').split(/[\ue000\ue001]/).forEach((p, i) => {
    if (i % 2 === 1) span.append(el('mark', { text: p }));
    else span.append(document.createTextNode(p));
  });
  return span;
}
function renderSearchResults(q) {
  const box = dom['search-results']; box.innerHTML = '';
  if (!searchHits.length) { box.append(el('div', { class: 'cmdk-empty', text: t('search.no_results', { query: q }) })); return; }
  for (const h of searchHits) {
    const bot = botById(h.bot_id);
    const row = el('div', { class: 'search-hit', tabindex: '0', role: 'button' }, [
      el('div', { class: 'sh-top' }, [
        el('span', { class: 'sh-bot', text: (bot && bot.name) || h.bot_id }),
        el('span', { text: t('search.hit_meta', { title: h.title || t('cmdk.sub_chat'), when: relTime(h.created_at) }) }),
      ]),
      el('div', { class: 'sh-snip' }, [highlightSnippet(h.snippet)]),
    ]);
    row.addEventListener('click', () => openSearchHit(h));
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openSearchHit(h); }
    });
    box.append(row);
  }
}
async function openSearchHit(h) {
  closeSearch();
  if (state.selectedBotId !== h.bot_id) await selectBot(h.bot_id);
  await openThread(h.thread_id);
}

// ===================== Recovery & Export =====================
function openRecovery() {
  if (state.decoy) { toast(t('toast.not_available'), true); return; }  // neutral: no lock hint in Safe Mode
  dom['botmanager-backdrop'].classList.add('hidden');
  dom['recover-result'].textContent = '';
  dom['recover-backdrop'].classList.remove('hidden');
}
function closeRecovery() { dom['recover-backdrop'].classList.add('hidden'); }
function doExport(fmt) {
  // Content-Disposition makes this download without navigating away.
  window.location.href = api.exportUrl(fmt);
}
async function doRecoverAll() {
  dom['recover-result'].textContent = t('recovery.scanning');
  try {
    const r = await api.recoverAll();
    dom['recover-result'].textContent =
      t('recovery.recovered_summary', { count: r.recovered, threads: r.threads_scanned });
    if (r.recovered && state.selectedBotId) await resync();
  } catch (e) { dom['recover-result'].textContent = e.message; }
}

// ===================== Transcript bridge (raw viewer + session browser) =====================
// [kind, translation key] — the chip labels are resolved at render time so a
// language switch repaints them without rebuilding this table.
const TX_KINDS = [['text', 'transcript.filter_text'], ['note', 'transcript.filter_note'],
  ['user', 'transcript.filter_user'], ['tool', 'transcript.filter_tool'],
  ['tool_result', 'transcript.filter_tool_result'], ['thinking', 'transcript.filter_thinking']];
let txState = { items: [], filter: null, threadId: null, sessionKey: null, botId: null, missing: 0 };
function txDefaultFilter() { return new Set(TX_KINDS.map(([k]) => k)); }

async function openThreadTranscript(threadId) {
  if (state.decoy) { toast(t('toast.not_available'), true); return; }  // neutral: no lock hint in Safe Mode
  const th = state.threads.find((x) => x.id === threadId) || state.activeThread;
  const botId = (th && th.bot_id) || state.selectedBotId;
  setI18nText(dom['tx-title'], 'transcript.title');
  dom['tx-import'].hidden = true;
  dom['tx-filter'].innerHTML = '';
  dom['tx-list'].innerHTML = '';
  dom['tx-list'].append(el('div', { class: 'cmdk-empty', text: t('transcript.loading') }));
  dom['tx-backdrop'].classList.remove('hidden');
  try {
    const r = await api.ocTranscript({ botId, threadId });
    txState = { items: r.items || [], filter: txDefaultFilter(), threadId,
      sessionKey: r.session_key, botId, missing: r.missing_from_dispatch || 0 };
    renderTranscript(r);
  } catch (e) {
    dom['tx-list'].innerHTML = '';
    dom['tx-list'].append(el('div', { class: 'cmdk-empty', text: e.message }));
  }
}
function renderTranscript(meta) {
  dom['tx-summary'].textContent = meta.found
    ? t('transcript.summary', { items: txState.items.length, missing: meta.missing_from_dispatch || 0 })
    : t('transcript.none');
  const fwrap = dom['tx-filter']; fwrap.innerHTML = '';
  for (const [k, labelKey] of TX_KINDS) {
    const chip = el('button', { class: 'tx-chip' + (txState.filter.has(k) ? ' on' : ''), text: t(labelKey) });
    chip.addEventListener('click', () => {
      if (txState.filter.has(k)) txState.filter.delete(k); else txState.filter.add(k);
      chip.classList.toggle('on'); renderTxList();
    });
    fwrap.append(chip);
  }
  const imp = dom['tx-import'];
  if (txState.threadId && txState.missing > 0) { imp.hidden = false; setI18nText(imp, 'transcript.import_count', { count: txState.missing }); }
  else imp.hidden = true;
  renderTxList();
}
function renderTxList() {
  const box = dom['tx-list']; box.innerHTML = '';
  const items = txState.items.filter((it) => txState.filter.has(it.kind));
  if (!items.length) { box.append(el('div', { class: 'cmdk-empty', text: t('transcript.filter_empty') })); return; }
  for (const it of items) {
    const deliverable = it.kind === 'text' || it.kind === 'note';
    const head = el('div', { class: 'tx-head' }, [
      el('span', { class: `tx-kind ${it.kind}`, text: it.kind.replace('_', ' ') }),
      it.name ? el('span', { text: it.name }) : null,
      (deliverable && !it.in_db)
        ? el('span', { class: 'tx-badge-new', text: t('transcript.not_saved'), title: t('transcript.not_saved_title') })
        : null,
    ].filter(Boolean));
    box.append(el('div', {
      class: 'tx-item' + (it.kind === 'text' ? ' is-text' : '') + (it.in_db ? ' indb' : ''),
    }, [head, el('div', { class: 'tx-body', text: it.text || '' })]));
  }
}
function closeTx() { dom['tx-backdrop'].classList.add('hidden'); }
async function txImportMissing() {
  if (!txState.threadId) return;
  dom['tx-import'].disabled = true;
  try {
    const r = await api.recoverThread(txState.threadId);
    toast(r.recovered ? t('transcript.imported', { count: r.recovered }) : t('transcript.nothing_missing'));
    if (r.recovered && txState.threadId === state.activeThreadId) {
      await openThread(txState.threadId, { background: true });
    }
    await openThreadTranscript(txState.threadId);   // refresh flags
  } catch (e) { toast(e.message, true); }
  finally { dom['tx-import'].disabled = false; }
}
async function syncThreadFromOpenClaw(threadId) {
  if (state.decoy) { toast(t('toast.not_available'), true); return; }  // neutral: no lock hint in Safe Mode
  toast(t('recovery.pulling'));
  try {
    const r = await api.recoverThread(threadId);
    toast(r.recovered ? t('recovery.recovered', { count: r.recovered }) : t('recovery.up_to_date'));
    if (r.recovered && threadId === state.activeThreadId) {
      await openThread(threadId, { background: true });
    }
  } catch (e) { toast(e.message, true); }
}

// Session browser: lists ALL of an agent's OpenClaw sessions — including cron /
// subagent / dashboard / main sessions DisPatch never created — and lets you
// view or import any of them. The doorway to messages that never reach DisPatch.
async function openSessionBrowser() {
  const botId = state.selectedBotId;
  if (!botId) { toast(t('sessions.pick_bot_first'), true); return; }
  dom['recover-backdrop'].classList.add('hidden');
  const bot = botById(botId);
  setI18nText(dom['tx-title'], 'sessions.title', { name: (bot && bot.name) || botId });
  dom['tx-summary'].textContent = t('sessions.summary');
  dom['tx-filter'].innerHTML = '';
  dom['tx-import'].hidden = true;
  dom['tx-list'].innerHTML = '';
  dom['tx-list'].append(el('div', { class: 'cmdk-empty', text: t('sessions.loading') }));
  dom['tx-backdrop'].classList.remove('hidden');
  try {
    const r = await api.ocSessions(botId);
    renderSessionList(botId, r.sessions || []);
  } catch (e) {
    dom['tx-list'].innerHTML = '';
    dom['tx-list'].append(el('div', { class: 'cmdk-empty', text: e.message }));
  }
}
function renderSessionList(botId, sessions) {
  const box = dom['tx-list']; box.innerHTML = '';
  if (!sessions.length) { box.append(el('div', { class: 'cmdk-empty', text: t('sessions.empty') })); return; }
  for (const s of sessions) {
    let when = '';
    try { when = relTime(new Date(s.mtime * 1000).toISOString()); } catch { /* ignore */ }
    const head = el('div', { class: 'tx-head' }, [
      el('span', { class: `tx-kind ${(s.kind === 'thread' || s.kind === 'daily') ? 'text' : 'tool'}`, text: s.kind }),
      el('span', { text: t('sessions.meta', { when, size: fileSize(s.size) }) }),
      // Classes, not an inline `style` attribute: those are blocked by the
      // page's CSP (style-src 'self', no 'unsafe-inline'). See index.html.
      el('span', { class: `tx-badge-new${s.in_dispatch ? ' is-saved' : ''}`,
        text: t(s.in_dispatch ? 'transcript.saved' : 'transcript.not_saved'),
        title: t(s.in_dispatch ? 'transcript.saved_title' : 'transcript.not_saved_title') }),
    ]);
    const actions = el('div', { class: 'recover-row tx-item-actions' }, [
      el('button', { class: 'btn-secondary', text: t('sessions.view'), onclick: () => viewSession(botId, s.session_key) }),
      s.in_dispatch ? null
        : el('button', { class: 'btn-primary', text: t('sessions.import'), onclick: () => importSession(botId, s.session_key) }),
    ].filter(Boolean));
    box.append(el('div', { class: 'tx-item' },
      [head, el('div', { class: 'tx-body', text: s.session_key }), actions]));
  }
}
async function viewSession(botId, sessionKey) {
  dom['tx-import'].hidden = true;
  dom['tx-list'].innerHTML = '';
  dom['tx-list'].append(el('div', { class: 'cmdk-empty', text: t('transcript.loading') }));
  try {
    const r = await api.ocTranscript({ botId, sessionKey });
    txState = { items: r.items || [], filter: txDefaultFilter(), threadId: null, sessionKey, botId, missing: 0 };
    renderTranscript(r);
  } catch (e) {
    dom['tx-list'].innerHTML = '';
    dom['tx-list'].append(el('div', { class: 'cmdk-empty', text: e.message }));
  }
}
async function importSession(botId, sessionKey) {
  try {
    const r = await api.importSession(botId, sessionKey);
    toast(t('sessions.imported', { count: r.imported }));
    if (botId === state.selectedBotId) await resync();
    closeTx();
  } catch (e) { toast(e.message, true); }
}

// The search button advertises the chord for THIS platform — ⌘ doesn't exist off
// macOS/iOS. The chord itself is syntax; only the sentence around it is
// translated, so the title is rebuilt from JS instead of by the DOM pass.
let searchIsApple = false;
function applySearchShortcutTitle() {
  const btn = dom['search-btn'];
  if (!btn) return;
  btn.title = t('threads.search_shortcut', { shortcut: (searchIsApple ? '⌘' : 'Ctrl+') + '/' });
}

// ===================== Gear-rail labels (phone "Bots" page) =====================
// On phones the gear rail becomes a stack of full-width buttons and each one
// grows a text label beside its glyph. Those labels used to be hard-coded
// ENGLISH strings in app.css (`content: "Settings"`) — untranslatable by
// construction, and two buttons (⚡ Reactions, 🩺 Dashboard) never got one at
// all, so they read as bare glyphs next to six labelled neighbours.
//
// The CSS now renders `content: attr(data-label)`; this is what fills that
// attribute in, from the same locale files as everything else. It runs on boot
// and again on every language switch (reRenderForLocale), so the labels track
// the picker. Desktop pays nothing: the ::after is only shown by the phone
// media query, but the attribute is harmless there.
// Three of these reuse a key that already says exactly the right word in all
// eight locales (nav.settings "Settings", nav.theme "Theme", comfy.name
// "ComfyUI") — a second key with identical text is just another thing to keep
// translated. The rest are new nav.label_* keys, short on purpose.
const RAIL_LABELS = {
  'manage-bots': 'nav.settings',
  'theme-toggle': 'nav.theme',
  'comfy-chip': 'comfy.name',
  'lock-now': 'nav.label_lock',
  'unlock-btn': 'nav.label_unlock',
  'fs-chip': 'nav.label_files',
  'drop-btn': 'nav.label_send_file',
  // nav.label_reactions / label_dashboard / label_connect are no longer
  // referenced — ⚡ 🩺 🔌 left the rail for Settings tabs. The KEYS stay in all
  // eight locale files: deleting them churns every translation for nothing.
};
function applyRailLabels() {
  // No dictionary at all (both fetches failed) => leave the buttons as their
  // glyphs. t() would hand back humanized keys — "Label send file" — which is
  // worse than the unlabelled state these buttons shipped with.
  if (!hasDictionary()) return;
  for (const [id, key] of Object.entries(RAIL_LABELS)) {
    const btn = dom[id] || document.getElementById(id);
    if (btn) btn.setAttribute('data-label', t(key));
  }
}

function wireRecoveryUi() {
  searchIsApple = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
  applySearchShortcutTitle();
  dom['search-btn'].addEventListener('click', openSearch);
  dom['search-close'].addEventListener('click', closeSearch);
  dom['search-backdrop'].addEventListener('click', (e) => { if (e.target === dom['search-backdrop']) closeSearch(); });
  dom['search-input'].addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => runSearch(dom['search-input'].value), 220);
  });
  dom['search-input'].addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && searchHits.length) openSearchHit(searchHits[0]);
  });
  dom['open-recovery'].addEventListener('click', openRecovery);
  dom['recover-close'].addEventListener('click', closeRecovery);
  dom['recover-done'].addEventListener('click', closeRecovery);
  dom['recover-backdrop'].addEventListener('click', (e) => { if (e.target === dom['recover-backdrop']) closeRecovery(); });
  dom['recover-all'].addEventListener('click', doRecoverAll);
  dom['browse-sessions'].addEventListener('click', openSessionBrowser);
  dom['recover-backdrop'].querySelectorAll('[data-export]').forEach((b) =>
    b.addEventListener('click', () => doExport(b.dataset.export)));
  dom['tx-close'].addEventListener('click', closeTx);
  dom['tx-backdrop'].addEventListener('click', (e) => { if (e.target === dom['tx-backdrop']) closeTx(); });
  dom['tx-import'].addEventListener('click', txImportMissing);
  // ⌘/ (or Ctrl+/) opens message search.
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === '/') { e.preventDefault(); openSearch(); }
  });
}

// ===================== Command palette (Cmd/Ctrl+K) =====================
// Client-only launcher: fuzzy-jump bots/chats and run app actions. Never sends
// model commands (DisPatch has no slash-command bot triggers by design).
let cmdkItems = [];
let cmdkSel = 0;
function cmdkActions() {
  const a = [
    { icon: '＋', label: t('cmdk.action_new_chat'), run: () => newChat() },
    { icon: '◐', label: t('cmdk.action_theme'), run: () => document.getElementById('theme-toggle')?.click() },
  ];
  if (!state.decoy) {
    a.push({ icon: '🔍', label: t('cmdk.action_search'), run: () => openSearch() });
    a.push({ icon: '🛟', label: t('cmdk.action_recovery'), run: () => openRecovery() });
  }
  if (state.auth && state.auth.pinSet && !state.decoy) a.push({ icon: '🔒', label: t('cmdk.action_lock'), run: () => lockNow() });
  return a;
}
function cmdkBuild(q) {
  const ql = (q || '').toLowerCase();
  const m = (s) => !ql || (s || '').toLowerCase().includes(ql);
  const items = [];
  for (const a of cmdkActions()) if (m(a.label)) items.push({ ...a, group: t('cmdk.group_actions') });
  for (const b of state.bots) if (m(b.name)) items.push({ icon: b.emoji || '🤖', label: b.name, sub: b.model_hint || '', group: t('cmdk.group_bots'), run: () => selectBot(b.id) });
  // threadTitle() so untitled threads read 'New Chat' here too — the palette
  // must name a thread exactly like the thread list does.
  for (const th of state.threads) { const title = threadTitle(th); if (m(title)) items.push({ icon: '💬', label: title, sub: t('cmdk.sub_chat'), group: t('cmdk.group_chats'), run: () => openThread(th.id) }); }
  return items.slice(0, 40);
}
function cmdkRender() {
  const list = $('cmdk-list'); if (!list) return;
  list.innerHTML = '';
  if (!cmdkItems.length) { list.append(el('div', { class: 'cmdk-empty', text: t('cmdk.empty') })); return; }
  let group = null;
  cmdkItems.forEach((it, i) => {
    if (it.group !== group) { group = it.group; list.append(el('div', { class: 'cmdk-group', text: group })); }
    const row = el('div', { class: 'cmdk-item' + (i === cmdkSel ? ' sel' : '') }, [
      el('span', { class: 'ic', text: it.icon || '' }),
      el('span', { class: 'lbl', text: it.label }),
      it.sub ? el('span', { class: 'sub', text: it.sub }) : null,
    ].filter(Boolean));
    row.addEventListener('click', () => cmdkRun(i));
    list.append(row);
  });
  const sel = list.querySelector('.cmdk-item.sel');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}
function cmdkRun(i) {
  const it = cmdkItems[i]; if (!it) return;
  closePalette();
  try { it.run(); } catch (e) { /* ignore */ }
}
function openPalette() {
  const dlg = $('cmdk'); const inp = $('cmdk-input');
  if (!dlg || !inp || dlg.open) return;
  inp.value = ''; cmdkSel = 0; cmdkItems = cmdkBuild('');
  cmdkRender();
  dlg.showModal();
  inp.focus();
}
function closePalette() { const dlg = $('cmdk'); if (dlg && dlg.open) dlg.close(); }
function wireCmdk() {
  const dlg = $('cmdk'); const inp = $('cmdk-input');
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); openPalette(); }
  });
  if (!dlg || !inp) return;
  inp.addEventListener('input', () => { cmdkItems = cmdkBuild(inp.value); cmdkSel = 0; cmdkRender(); });
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); cmdkSel = Math.min(cmdkSel + 1, cmdkItems.length - 1); cmdkRender(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); cmdkSel = Math.max(cmdkSel - 1, 0); cmdkRender(); }
    else if (e.key === 'Enter') { e.preventDefault(); cmdkRun(cmdkSel); }
  });
  dlg.addEventListener('click', (e) => { if (e.target === dlg) closePalette(); });   // backdrop click
}

// ===================== Locale change =====================
/** Repaint everything this module BUILT after a language switch.
 *
 *  i18n's own DOM pass has already re-translated the static markup by the time
 *  this runs — but every node main.js created holds a plain string that was
 *  resolved once, and nothing would ever revisit it. This also puts back the
 *  handful of STATIC nodes whose text is really owned by JS (the chat header,
 *  the thread-list header, the search shortcut): the pass has just reset them to
 *  their markup key, and the renderers below are what make them right again.
 *
 *  Open panels are repainted only while open — rebuilding a hidden File Server
 *  would fire an API request nobody asked for.
 */
function reRenderForLocale() {
  applySearchShortcutTitle();
  applyRailLabels();
  setConnected(state.connected);
  renderSidebar();
  updateThreadListHeader();
  renderThreads();
  if (state.activeThreadId) { renderChatHeader(); renderMessages(false); }
  else clearChatView();
  reflectComposerState();
  updateDropBusy();
  renderTerminalOptions();
  applyTerminalStatus(state.terminal.state);
  if (terminalOpen) renderTerminalSessionPanel();
  if (harnessOpen) { renderHarnessSessionPanel(); renderHarness(); }
  const open = (id) => dom[id] && !dom[id].classList.contains('hidden');
  // Settings repaints per TAB: only the visible pane's renderer has anything to
  // put back, and running the others would fetch for a screen nobody is on.
  if (open('botmanager-backdrop')) {
    renderBotManager();
    if (settingsTab === 'device') mountLanguagePicker();
    if (settingsTab === 'security') renderSecurityStrings();
    // The Health pane is built inside dashboard.js (same story as the Reaction
    // Manager): nothing in `dom` describes its labels, so it repaints itself.
    if (settingsTab === 'health') repaintDashboard();
  }
  if (open('companions-backdrop')) renderCompanions();
  if (open('fileserver-backdrop')) renderFileServer();
  // The Reaction Manager's contents are built inside reactions.js, so nothing
  // in `dom` describes them and the open() helper above cannot see them —
  // which is exactly how ~40 strings kept sitting there in the previous
  // language while every other open modal repainted.
  if (reactionManagerOpen()) repaintReactionManager();
  // Same story for Connect an AI: its labels come from the DOM pass, but the
  // key placeholder and the two state-dependent notes are written from JS.
  repaintLlmPanel();
  if (comfyPanelOpen) refreshComfyPanel();
  const cmdkDlg = $('cmdk');
  if (cmdkDlg && cmdkDlg.open) { cmdkItems = cmdkBuild($('cmdk-input').value); cmdkRender(); }
}

async function init() {
  // Translations FIRST: every renderer below reads t() synchronously, so the
  // dictionaries have to be in memory before the first paint. i18n.init() is
  // time-boxed internally, so a hung fetch degrades to English rather than
  // holding the boot veil up.
  await i18nInit();
  onI18nChange(reRenderForLocale);
  applyRailLabels();
  // Privacy mode, if this device has it on: drop the offline cache, unregister
  // the service worker, and arm the on-close wipe. Must run before anything
  // re-registers the worker or persists a preference.
  await initPrivacy();
  initNim();
  installThumbnailLightbox();
  wireEvents();
  wireCmdk();
  // Reactions hold no app state of their own — everything they need about the
  // current view is read back through these hooks.
  initReactions({
    toast,
    isDecoy: () => state.decoy,
    confirm: (msg) => uiConfirm(msg),
    threadCtx: () => ({
      thread_id: state.activeThreadId || null,
      bot_id: state.activeThread?.bot_id || state.selectedBotId || null,
    }),
    onChanged: () => { if (state.decoy) renderCompanions(); },
    // Reaction pictures open in the same lightbox as chat media.
    lightbox: (src) => openLightbox(src),
    // Pack curation lives in Settings; this is how the module's own
    // openManager() gets there.
    openSettingsTab,
  });
  initLlmPanel(llmCtx());
  wireAuthEvents();
  wireRecoveryUi();
  setOnLocked(() => handleLocked());
  // Periodic re-render: unread dots flip red at the 24h mark, and the thread
  // list's relative timestamps ("5m") drift. Piggyback a slow ComfyUI chip
  // refresh — external service changes (CLI stop, crash) never broadcast a
  // comfy_service frame, so the chip would otherwise stay stale indefinitely.
  let lastTick = '';
  setInterval(() => {
    if (!state.started) return;
    // Only repaint when something a repaint would CHANGE has changed. The
    // timer exists for two drifting things — relative timestamps and the 24h
    // unread colour — so hash exactly those. An idle tab used to pay a full
    // double repaint (plus a forced layout) every minute, forever, on every
    // open device.
    const tick = state.threads.map((t) =>
      `${t.id}:${relTime(t.updated_at)}:${t.unread_since || ''}`).join('|');
    if (tick !== lastTick) {
      lastTick = tick;
      renderSidebar();
      renderThreads();
    }
    if (state.comfyEnabled && !state.decoy) refreshComfyChipOnce();
  }, 60_000);
  await refreshAuthAndBoot();

  // Say it out loud when NO dictionary loaded at all. i18n keeps the shipped
  // English markup in that case (applyDom declines to run), which is the right
  // degradation but is indistinguishable from "the language picker is broken"
  // if nobody mentions it. Toasted here, after boot, because anything raised
  // during init() would be painted behind the boot veil.
  if (localeLoadFailed()) toast(t('error.locale_load'), true);
}

init();
