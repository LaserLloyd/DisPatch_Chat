// DisPatch Chat — frontend controller.
// One cohesive module: state, rendering, events, and WebSocket dispatch.
// Leaf modules (util/api/ws/markdown) hold no app state, so there are no cycles.

import { api, setOnLocked } from './api.js?v=29';
import { ChatSocket } from './ws.js?v=9';
import { renderMarkdown, enhanceContent, normalizeMediaUrl, isVideoUrl, installMarkdownHandlers, linkifyPlain, retargetLinks, markSpeech, markParens, stripMediaSource, toPlainPreview } from './markdown.js?v=32';
import { installChecklists, applyChecklistState } from './checklist.js?v=6';
import { classifyNotice, noticeHeadline } from './notice.js?v=3';
import { acquireInert, el, escapeHtml, glyphless, iconLabel, isMixedContent, loadScript, loadStyle, railIcon, releaseInert, RAIL_ICONS } from './util.js?v=19';
// The formatters come from i18n.js now, not util.js: they need the active
// locale (Intl) and translatable unit labels, which the old hand-rolled 'en-US'
// helpers could never provide. `fmtSize` was renamed `fileSize` on the way over.
import {
  t, tHtml, init as i18nInit, onChange as onI18nChange, languageSelect, localeLoadFailed, hasDictionary,
  relTime, clockTime, dayLabel, dayKey, fileSize,
  n as fmtNumber, percent as fmtPercent,
} from './i18n.js?v=3';
import {
  initReactions, loadReactions, resetReactions, handleReactionFrame, handlePoolFrame,
  mountManager as mountReactionManager, closeManager as unmountReactionManager,
  managerOpen as reactionManagerOpen, repaintManager as repaintReactionManager,
  reactionMessageEl, botHasReactions,
} from './reactions.js?v=19';
import { mountDashboard, unmountDashboard, repaintDashboard } from './dashboard.js?v=8';
import { initClients, showClientsTab, clientsTabNav, stopClientsPolling } from './clients.js?v=6';
import { mountJobs, unmountJobs } from './jobs.js?v=9';
import { openJobDetail, closeJobDetail } from './job-thread.js?v=10';
import {
  initLlmPanel, activateLlmPanel, closeLlmPanel, llmPanelOpen, repaintLlmPanel,
  firstRunCard,
} from './llm.js?v=6';
import { initPrivacy, privacyRow, allowsPersistentSession } from './privacy.js?v=8';
import { initNim, nimEnabled, setNim, canDisableNim, shouldDropMessage, nimRow, setMinimalAvatars } from './nim.js?v=5';
import { renderPinnedRail, pinToggle, isPinned } from './pins.js?v=10';
// thread-sections.js owns the Today / Older bucketing + section-header DOM.
// See the module's top comment for the rule set; this file only decides WHEN
// to render headers (suppressed on mobile, suppressed while search is open)
// and WHERE the buckets go in the threads list.
import {
  bucketThreads, filterSignature, shouldShowThreadSections, threadSectionHeadEl,
} from './thread-sections.js?v=2';
import { activeMenuBotIds, isMenuBot, toggleMenuBot, pruneMenuBots } from './menubots.js?v=1';
import {
  loadTools, renderToolRail, openTool, closeTool, wireTools, isToolId, railToolDot,
  openFromHash, mountToolsSettings, toolsSettingsDirty, rememberPrev,
} from './tools.js?v=3';
import { renderLinkRail, linksSection } from './links.js?v=6';
// The local viewer owns its own overlay (built like openLightbox, closed by the
// same closeAllOverlays route). main.js only decides WHEN it may open: never in
// Safe Mode, which is why isDecoy is a live callback rather than a boolean.
import { openViewer, installViewerHandlers, closeViewer, viewerOpen } from './viewer.js?v=4';
import { aboutRow } from './about.js?v=2';
import { imageJobMessageEl } from './imagejobs.js?v=4';
import {
  THINKING_LEVELS, normalizeModelOptions, meterText, prefsPatchFrom, latestContextBudget,
} from './modelchip.js?v=2';
import {
  createStore, createCachePainter, tierFor, TIER_FULL, MESSAGE_CACHE_ROWS,
} from './store.js?v=2';

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
  turnPhase: {},       // thread_id -> gateway phase string ('starting_model', 'tool:brave_search', …)
  // thread_id -> the gateway's own refusal sentence (AgentRefused), while it
  // stands. Set when an 'error' frame carries `refused: true`; cleared when a
  // new turn starts on that thread (a resend/correction is underway) or when
  // the operator applies a change from the picker. The preference itself is
  // never touched by this — see run_agent_turn's AgentRefused handling.
  modelChipWarn: {},
  scrollPositions: {}, // thread_id -> last scrollTop when user navigated away
  drafts: new Set(),   // thread ids with unsent composer text (the "Draft" label)
  auth: { pinSet: false, authenticated: false, decoy: false, lockTimeout: 600, minPin: 4, recoveryPath: '', configPath: '', rememberDays: 0, remembered: false, trustedDevices: 0 },
  decoy: false,        // Safe Mode (chat media hidden; safe bots' avatars shown)
  started: false,      // app has booted (bots loaded, socket connected)
  harnessEnabled: false, // server-side feature flag (DISPATCH_HARNESS); full-session only
  // DeepSeek Harness pane: last /api/harness/status payload (service + models),
  // the headless-job ledger the server broadcasts, and the LIVE SESSION list
  // (launched sessions; one leaves the list when it is stopped).
  harness: {
    status: null,
    jobs: { running: false, current: null, history: [] },
    sessions: { sessions: [], running: 0, limit: 4 },
  },
  mailEnabled: false, // server-side feature flag (mail_available()); full-session only
  clientsEnabled: false, // server-side feature flag (practice_available()); full-session only
  studioforgeEnabled: false, // server-side feature flag (DISPATCH_STUDIOFORGE + a URL); full-session only
  // StudioForge pane: last /api/studioforge/status payload, plus whether THIS
  // browser could reach the panel (a separate question from whether the server
  // could — see studioforgeProbe).
  studioforge: { status: null, clientReachable: null },
  // Today / Older section bucketing — cached across repaints. The signature
  // captures everything that can move a thread between buckets, so a WS frame
  // that doesn't change `state.threads` (the common heartbeat) skips
  // re-bucketing. Cleared implicitly when state.threads is reassigned
  // (different array reference → different signature by construction).
  _lastSectionSig: null,
  _lastSections: null,
  // threadId → 'user' | 'assistant' | 'system'. Currently unused for
  // bucketing — the backend does not expose role on the thread row — but the
  // map is wired in so the (b) "user-message within 7 days" rule can be
  // turned on by populating it from the WS message frames. See the open issue
  // in the report.
  lastMessageRole: new Map(),
  // The message a "Reply" tap has staged for the NEXT send, or null. Cleared
  // on send and on a real thread switch (see openThread) — a quote must not
  // silently follow the composer into a different conversation.
  // {id, role, text} — `text` is a client-side PREVIEW (see quotePreview);
  // the canonical reply_excerpt is computed server-side once the message
  // that quotes it actually exists.
  replyTarget: null,
};

// The DeepSeek Harness (dsh) pane is rendered as a pseudo-bot in the sidebar
// (unlocked mode only). Its id never collides with a real OpenClaw agent id.
const HARNESS_ID = 'deepseek-harness';
// And for the StudioForge control panel (the LLM rig's own web UI, embedded).
const STUDIOFORGE_ID = 'studioforge-panel';
// Emails tab (MailForge dashboard, embedded via its own launch URL).
const MAIL_ID = 'mail-panel';
// Clients tab ("WebBuilder" — the practice box's client pipeline, native UI).
const CLIENTS_ID = 'clients-panel';
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
['app', 'bot-list', 'manage-bots', 'theme-toggle',
 'tools-menu',
 'tools-bots', 'tools-bots-sep', 'tl-avatar', 'tl-botname', 'tl-model', 'new-chat',
 'threads', 'back-btn', 'ch-avatar', 'ch-title', 'ch-sub', 'ch-model', 'popout-btn', 'thread-menu-btn',
 'thread-menu', 'messages', 'chat-empty', 'scroll-bottom', 'composer', 'input', 'send', 'stop',
 // Per-thread model/thinking override chip + its picker (Feature 7).
 'ch-modelchip', 'ch-modelchip-label', 'model-picker', 'mp-model', 'mp-thinking-row',
 'mp-thinking', 'mp-context', 'mp-warning', 'mp-reset', 'mp-apply',
 'char-count', 'waiting', 'attach-btn', 'file-input', 'attach-preview', 'mobile-tabs',
 'job-board-host', 'tab-jobs',
 'retry-chip', 'retry-chip-btn',
 'reply-chip', 'reply-chip-label', 'reply-chip-excerpt', 'reply-chip-cancel',
 'botmanager-backdrop', 'bm-list', 'bm-close', 'bm-done', 'toast', 'reconnect',
 'collapse-threads', 'expand-threads', 'crop-backdrop', 'crop-img', 'crop-box',
 'crop-stage', 'crop-size', 'crop-save', 'crop-close', 'crop-title',
 'fs-chip', 'fileserver-backdrop', 'fs-list', 'fs-upload-btn', 'fs-close',
 'fs-file-input',
 // Settings tab container. The panes for Reactions and Health are empty mount
 // points; their modules build what goes inside.
 'settings-tabs', 'bm-footer', 'spane-bots', 'spane-reactions', 'spane-health',
 'spane-ai', 'spane-tools', 'spane-theme', 'spane-device', 'spane-security', 'sfoot-health', 'avatar-pool-panel',
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
 'conn-dot', 'sb-count', 'sr-live', 'sr-status', 'sr-alert', 'search-btn',
 'search-backdrop', 'search-close', 'search-input', 'search-results',
 'open-recovery', 'recover-backdrop', 'recover-close', 'recover-all',
 'browse-sessions', 'recover-result', 'recover-done',
 'tx-backdrop', 'tx-close', 'tx-title', 'tx-summary', 'tx-filter',
 'tx-import', 'tx-list',
 // DeepSeek Harness pane
 'harness-view', 'harness-back', 'harness-subtitle', 'harness-tab-ui', 'harness-tab-jobs',
 'harness-open', 'harness-pane-ui', 'harness-pane-jobs', 'harness-frame', 'harness-note',
 'harness-note-text', 'harness-note-hint', 'harness-job-form', 'harness-task', 'harness-cwd',
 'harness-run', 'harness-cancel', 'harness-jobs', 'harness-dot', 'harness-status-label',
 'harness-tab-sessions', 'harness-pane-sessions', 'harness-session-form',
 'harness-session-task', 'harness-session-cwd', 'harness-session-model',
 'harness-session-run', 'harness-sessions',
 'harness-model', 'harness-sf-link', 'harness-start', 'harness-restart', 'harness-stop',
 // StudioForge panel pane
 'studioforge-view', 'studioforge-back', 'studioforge-subtitle', 'studioforge-open',
 'studioforge-pane', 'studioforge-frame', 'studioforge-note', 'studioforge-note-text',
 'studioforge-note-hint', 'studioforge-dot', 'studioforge-status-label', 'studioforge-url',
 // Emails tab pane
 'mail-view', 'mail-back', 'mail-subtitle', 'mail-open', 'mail-pane', 'mail-frame',
 'mail-note', 'mail-note-text', 'mail-note-hint', 'mail-dot', 'mail-status-label',
 // Clients tab pane
 'clients-view', 'clients-back', 'clients-subtitle', 'clients-tabnav', 'clients-root',
 'clients-job-strip',
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
  // Any REAL repaint of the chat header (thread switch, avatar change, a NIM
  // toggle) must invalidate a mood-face flash's pending restore — see
  // flashHeaderMoodFace. Bumping here, unconditionally, means the restore
  // timer only ever fires when nothing legitimate has repainted the header
  // since it started, whether that something was another mood flash or this
  // ordinary paint.
  const baseClass = cur.classList[0] || key;
  // `thread` is passed only by the CHAT header. The thread-list header sits
  // above every thread at once, so it keeps showing the bot's current face.
  const pinned = thread ? threadFaceUrl(thread) : null;
  // NIM takes the no-URL branch: a letter <div>, never an <img>. Same reason as
  // avatarNode — an <img> is a download, and CSS hiding it afterwards does not
  // un-send the request.
  const url = (bot && !nimEnabled()) ? (pinned || bot.avatar_url) : '';
  // A mood face is showing and this paint would put the SAME base picture
  // back: keep the face. Every live reply is followed by a thread_update,
  // which repaints the header — so the 30 s face lasted 1–2 ms (measured:
  // avatar → face → avatar within one frame). Only a real change of picture
  // (thread switch, avatar change, NIM) ends it early.
  const keepMood = key === 'ch-avatar' && cur.tagName === 'IMG' && !!url
    && cur._moodActive && cur.dataset.base === url;
  if (key === 'ch-avatar' && !keepMood) moodHeaderEpoch += 1;
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
    dom[key].dataset.base = url;
    if (!keepMood) dom[key].src = url;
  }
}

// ===================== Mood face (feature 22) =====================
// A reply that earned metadata.mood (see _prepare_persist / avatar_pool.
// has_mood_face) swaps the header avatar and that bubble's own avatar to the
// mood's face for a short window, then reverts. Two DOM targets, one rule:
// never touch anything until the picture is confirmed to load — the ordinary
// avatar <img>s already carry an `error` handler that permanently downgrades
// them to a letter block on a genuine failure (see avatarNode / the
// `cur.tagName !== 'IMG'` branch above), and a missing mood face must not
// trip that and cost the bot its real picture. Preloading with a throwaway
// Image() means `.src` on the visible element is only ever set to a URL that
// is already known good.
const MOOD_FACE_MS = 30000;
let moodHeaderEpoch = 0;

// Applies to ONE message's own bubble avatar. Independent per element — each
// bubble is built once for that message, so there is nothing to invalidate
// beyond "is this node still on screen".
function flashBubbleMoodFace(imgEl, url) {
  const probe = new Image();
  probe.onload = () => {
    if (!imgEl.isConnected) return;
    const original = imgEl.src;
    imgEl.src = probe.src;
    setTimeout(() => {
      if (imgEl.isConnected && imgEl.src === probe.src) imgEl.src = original;
    }, MOOD_FACE_MS);
  };
  probe.src = url;
}

// Applies to the CHAT header, which is a fixed element reused across threads
// (paintHeaderAvatar repaints it in place). moodHeaderEpoch is what keeps a
// slow-arriving restore from a PREVIOUS mood flash — or from one whose
// message has scrolled out of relevance — clobbering whatever legitimately
// owns the header now: every real repaint AND every new flash bumps it, and
// a pending restore only acts while its own epoch is still current.
function flashHeaderMoodFace(hdr, url, threadId) {
  const probe = new Image();
  probe.onload = () => {
    // The header may belong to a different thread, or a different <img>
    // entirely (paintHeaderAvatar swaps DIV<->IMG), by the time this lands.
    if (dom['ch-avatar'] !== hdr || !hdr.isConnected) return;
    if (state.activeThreadId !== threadId) return;
    const original = hdr.src;
    const epoch = (moodHeaderEpoch += 1);
    hdr._moodActive = true;
    hdr.src = probe.src;
    setTimeout(() => {
      hdr._moodActive = false;
      // Restore to the picture the header WANTS now (dataset.base), which a
      // kept-through paint may have re-pointed, not the one from 30 s ago.
      if (epoch === moodHeaderEpoch && hdr.isConnected) hdr.src = hdr.dataset.base || original;
    }, MOOD_FACE_MS);
  };
  probe.src = url;
}

// Entry point: called once, right when a live reply lands in view — never
// from a full re-render (reopening an old thread must not re-flash a mood
// face that fired minutes or days ago). `rowEl` is the message's own DOM
// node, already in the document.
//
// Gated on NIM only, matching avatarNode/paintHeaderAvatar's own avatar-
// visibility rule (Safe Mode DOES show a safe bot's avatar) — the server
// route enforces the actual Safe-Mode/bot check and simply 403s otherwise,
// which the probe's onerror (a no-op: nothing was ever touched) absorbs.
function maybeFlashMoodFace(msg, rowEl) {
  if (!msg || !msg.metadata || !msg.metadata.mood) return;
  if (nimEnabled() || !rowEl) return;
  const url = `/api/messages/${encodeURIComponent(msg.id)}/mood-face`;
  const avatarEl = rowEl.querySelector ? rowEl.querySelector('.msg-avatar') : null;
  if (avatarEl && avatarEl.tagName === 'IMG') flashBubbleMoodFace(avatarEl, url);
  if (msg.thread_id === state.activeThreadId) {
    const hdr = dom['ch-avatar'];
    if (hdr && hdr.tagName === 'IMG') flashHeaderMoodFace(hdr, url, msg.thread_id);
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
function announce(text, assertive = false, region = 'sr-live') {
  const node = assertive ? dom['sr-alert'] : dom[region];
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
  // Jobs board: when the sidebar Job Board button or the mobile Jobs tab
  // calls setView('jobs'), mount the board into its dedicated host slot
  // (#job-board-host is a sibling of #chatview, see index.html). The slot
  // is mutually exclusive with the chat panel, so the toolbar cannot leak
  // onto the chat view. The previous mount target was #chat (or its
  // parent), which is why the toolbar stayed painted under the chat on
  // mobile — see jobs(unmount).
  if (view === 'jobs') {
    // Symptom (jobs-fix: search-sticks): entering the Jobs view from any
    // state where the messages-search modal was open used to leave the
    // overlay painted across the board. Search is bound to the threads /
    // chat list surfaces, so a view change is an unambiguous "I'm done
    // searching" — dismiss the modal here so the user lands on a clean
    // board, not a board hidden behind the search chrome.
    closeSearch();
    const host = dom['job-board-host'];
    if (host) {
      host.hidden = false;
      if (!host.querySelector('[data-jobs-root]')) {
        mountJobs(host).catch(() => {});
      }
    }
  } else {
    // Always tear down the board when leaving — and HIDE the host so its
    // descendants (the toolbar, the list) cannot bleed through even if a
    // future refactor skipped unmountJobs. Both paths are belt-and-braces:
    // the unmount removes the [data-jobs-root]; the [hidden] hides the
    // empty shell.
    unmountJobs();
    if (dom['job-board-host']) dom['job-board-host'].hidden = true;
  }
}

// History-aware navigation (mobile only). The bots screen is the root of the
// history stack, so the browser/hardware Back button walks chat → threads →
// bots instead of exiting the app. The stack always mirrors view depth:
// [bots, threads, chat][0..depth]. Desktop shows all panels — no history.
const VIEW_DEPTH = { bots: 0, threads: 1, chat: 2 };
const VIEW_AT_DEPTH = ['bots', 'threads', 'chat'];

function navigate(view) {
  if (!isMobile()) { setView(view); return; }
  // A closed viewer can leave its own entry on top (a framed page that
  // navigated added joint entries its close() could not count). Reclaim it
  // as the view we are actually showing so the depth arithmetic stays true.
  if (history.state?.viewer && !viewerOpen()) {
    history.replaceState({ view: dom.app.dataset.view || 'bots' }, '');
  }
  // The Jobs view is a sibling of the chat-side tabs but not part of the
  // history stack's depth axis — opening it from Chats/Messages must NOT
  // collapse the back-stack to Bots via history.go(-N). Push a Jobs entry
  // (do NOT replaceState — replaceState drops the intermediate entry, so a
  // Back from Jobs lands on Bots instead of the view the user came from).
  // pushState keeps the stack as [bots, ..., jobs] so Back returns to the
  // previous view (correct UX for "tap Jobs from Chats → Back returns to
  // Chats").
  if (view === 'jobs') {
    history.pushState({ view: 'jobs' }, '');
    setView('jobs');
    return;
  }
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
  // Landed on a viewer entry with no viewer open: a stale one left behind by
  // a framed page's own navigations. Step over it instead of painting "bots".
  if (e.state?.viewer) { if (!viewerOpen()) history.back(); return; }
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
  const allVisible = visibleBots();
  // Placement is per-device and Safe Mode ignores it (see menubots.js): a bot
  // parked in the ⌥ menu would be unreachable on a locked tablet, not moved.
  pruneMenuBots(state.bots.map((b) => b.id));
  const menuIds = activeMenuBotIds(state.decoy);
  const bots = allVisible.filter((b) => !menuIds.has(b.id));
  const menuBots = allVisible.filter((b) => menuIds.has(b.id));
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
      onclick: () => {
        if (bot.id === 'jobboard') {
          // The jobboard bot gets its own entry-point: clicking it opens
          // the board list view rather than the daily-thread fallback.
          // A full session is required; decoy callers are refused by
          // the backend's _is_decoy redaction in GET /api/jobs.
          setView('jobs');
          return;
        }
        selectBot(bot.id);
      },
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

  // The ⌥ entry now holds ONLY bots parked from Settings → Bots. The four
  // operator tools that used to live in its menu moved to the Tools group in
  // the rail (#tool-list, js/tools.js), so the button exists only when there
  // is something parked. Never in Safe Mode (menubots.js ignores placement
  // there, so a parked bot would be unreachable on a locked tablet).
  const toolsAvailable = !state.decoy && menuBots.length > 0;
  if (toolsAvailable) {
    const toolsName = t('tools.more_bots');
    const selected = menuBots.some((b) => b.id === state.selectedBotId);
    const btn = el('button', {
      class: 'bot-btn terminal-btn tools-btn' + (selected ? ' active' : ''),
      id: 'tools-btn',
      'aria-label': t('tools.more_bots_aria'),
      'aria-haspopup': 'menu',
      'aria-expanded': 'false',
      draggable: 'false',
      onclick: (e) => { e.stopPropagation(); toggleToolsMenu(); },
    });
    btn.append(el('span', { class: 'bot-avatar terminal-avatar tools-avatar', text: '⌥' }));
    btn.append(el('span', { class: 'bot-name-tip', text: toolsName }));
    btn.append(nameSpan('bot-name-label', toolsName, { name: toolsName }));
    // One dot for the parked group: thinking beats unread beats resting.
    const menuDot = menuBots.some((b) => thinkingBots.has(b.id)) ? ' thinking'
      : (menuBots.map((b) => unreadDotClass(unreadBots[b.id])).find((c) => c) || '');
    btn.append(el('span', { class: 'bot-status-dot' + menuDot }));
    list.append(btn);
    dom['tools-btn'] = btn;
  } else {
    // The rail no longer offers it; a menu left open across a lock would
    // otherwise outlive its own button.
    dom['tools-btn'] = null;
    closeToolsMenu();
  }
  renderMenuBots(menuBots, thinkingBots, unreadBots);
  // The Tools group (#tool-list) sits under the roster and is repainted with
  // it, so its active tile and feature-gated builtins never go stale.
  renderToolRail();
}

/** Bots the user parked in the ⌥ menu — the only rows it has now.
 *
 *  Rebuilt wholesale on every sidebar repaint: these carry no dot that
 *  anything patches in place; their unread/thinking state is computed here
 *  from the same snapshot the rail uses, so the two can never disagree.
 */
function renderMenuBots(menuBots, thinkingBots, unreadBots) {
  const host = dom['tools-bots'];
  const sep = dom['tools-bots-sep'];
  if (!host) return;
  host.innerHTML = '';
  // Nothing sits above the parked bots any more, so the divider never shows.
  if (sep) sep.hidden = true;
  menuBots.forEach((bot) => {
    const row = el('button', {
      class: 'tools-item' + (bot.id === state.selectedBotId ? ' active' : ''),
      role: 'menuitem',
      type: 'button',
      onclick: () => { closeToolsMenu(); selectBot(bot.id); },
    });
    row.append(avatarNode(bot, 'bot-avatar'));
    row.append(el('span', { class: 'tools-item-label', text: bot.name }));
    const dotClass = thinkingBots.has(bot.id) ? ' thinking' : unreadDotClass(unreadBots[bot.id]);
    row.append(el('span', { class: 'bot-status-dot' + dotClass }));
    host.append(row);
  });
}

/** The status-dot class for a builtin tool's rail tile ('' = no dot).
 *  Same per-service colours the ⌥ menu rows used before they moved to the
 *  rail; renderHarness/renderStudioForge keep patching it in place. */
function builtinDot(feature) {
  if (feature === 'harness') return 'harness-sidedot harness-' + harnessServiceState();
  if (feature === 'studioforge') return 'studioforge-sidedot studioforge-' + studioforgeState();
  return '';
}

/** Is that builtin tool's pane available to this session right now? */
function builtinOn(feature) {
  if (state.decoy) return false;
  if (feature === 'harness') return !!state.harnessEnabled;
  if (feature === 'studioforge') return !!state.studioforgeEnabled;
  if (feature === 'mail') return !!state.mailEnabled;
  if (feature === 'practice') return !!state.clientsEnabled;
  return false;
}

/** Put the selection back after the generic tool pane closes (✕ / back).
 *  The chat pane was only covered, never torn down, so this is a repaint of
 *  the rail and thread list, not a reload. */
function restoreAfterTool(prev) {
  const botId = prev && prev.botId && !isToolId(prev.botId) && botById(prev.botId) ? prev.botId : null;
  if (!botId) {
    state.selectedBotId = null;
    renderSidebar();
    clearChatView();
    // updateThreadListHeader() paints nothing without a bot, so the tool's
    // placeholder header would stay; put the boot-time text back.
    dom['tl-botname'].textContent = t('app.name');
    dom['tl-model'].textContent = t('threads.pick_bot');
    renderThreads();
    return;
  }
  if (state.threads.some((th) => th.bot_id && th.bot_id !== botId)) {
    // The list belongs to someone else (it should not, but never show it
    // under the wrong bot) — reload properly.
    selectBot(botId);
    return;
  }
  state.selectedBotId = botId;
  renderSidebar();
  updateThreadListHeader();
  renderThreads();
  if (isMobile()) navigate(prev.threadId && state.activeThreadId === prev.threadId ? 'chat' : 'threads');
}

// ===================== Thread list =====================
/** Which bots' VISIBLE identity changed between two roster snapshots.
 *
 *  Only the fields that are painted somewhere: everything else on a bot is
 *  either invisible or already covered by renderSidebar(). Returns null when
 *  there is nothing to compare against (the first frame) — callers read that
 *  as "assume everything changed" and repaint exactly as they used to.
 */
function changedBotLooks(prev, next) {
  if (!Array.isArray(prev) || !prev.length) return null;
  const key = (b) => [b.avatar_url, b.name, b.emoji, b.model_hint, b.avatar_style].join(' ');
  const before = new Map(prev.map((b) => [b.id, key(b)]));
  const changed = new Set();
  for (const b of next) {
    if (!before.has(b.id) || before.get(b.id) !== key(b)) changed.add(b.id);
  }
  return changed;
}

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
      el('div', { class: 'empty-emoji' }, [railIcon(RAIL_ICONS.bots)]),
      el('p', { text: t('threads.empty_no_bot') }),
    ]));
    return;
  }
  if (!state.threads.length) {
    wrap.append(el('div', { class: 'empty-list' }, [
      el('div', { class: 'empty-emoji' }, [railIcon(RAIL_ICONS.messages)]),
      el('p', { text: t('threads.empty_no_threads', { name: bot.name }) }),
      el('button', { class: 'btn-primary', text: t('threads.empty_cta'), onclick: newChat }),
    ]));
    return;
  }
  // Desktop-only: split into Today / Older (suppressed on mobile and while the
  // search modal is open — see shouldShowThreadSections). The pure bucketing
  // function lives in thread-sections.js; this caller is just a controller.
  const showSections = shouldShowThreadSections({
    isMobile: isMobile(),
    // The search modal sits over the threads list on a wide viewport, so a
    // single Older-thread hit could otherwise appear under a misleading
    // "Older" header. Hide the chrome while the modal is up; it returns when
    // the user closes search.
    searchOpen: !!(dom['search-backdrop'] && !dom['search-backdrop'].classList.contains('hidden')),
  });
  if (!showSections) {
    // `th`, not `t`: the translator is imported under that name and a thread
    // variable called `t` would shadow it for the whole loop body.
    for (const th of state.threads) wrap.append(threadRowEl(th, bot));
    return;
  }
  const now = Date.now();
  // -new Date().getTimezoneOffset() is the convention thread-sections.js uses
  // for "minutes east of UTC" (the opposite sign of JS Date's API).
  const tzOffsetMin = -new Date().getTimezoneOffset();
  // Cache the bucketing across repaints that would produce the same buckets.
  // The full repaint fires on every WS frame; bucketThreads over 200 threads
  // is wasted work if the inputs haven't moved.
  const sig = filterSignature(state.threads, now, tzOffsetMin, state.lastMessageRole);
  let sections;
  if (state._lastSectionSig === sig && state._lastSections) {
    sections = state._lastSections;
  } else {
    sections = bucketThreads(state.threads, now, tzOffsetMin, state.lastMessageRole);
    state._lastSectionSig = sig;
    state._lastSections = sections;
  }
  for (const section of sections) {
    if (!section.items.length) continue;     // empty sections: no header, no list
    wrap.append(threadSectionHeadEl(section.name, t));
    for (const th of section.items) wrap.append(threadRowEl(th, bot));
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
      titleEl.append(el('span', { class: 'thread-pin-icon' }, [railIcon(RAIL_ICONS.pin)]));
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

    // "Draft" sits in front of the preview rather than replacing it: which
    // conversation the unsent words belong to is the useful half, and the last
    // message is what identifies the conversation.
    const previewEl = el('div', { class: 'thread-preview' + (thinking ? ' thinking' : ''), dir: 'auto' });
    if (!thinking && state.drafts.has(th.id)) {
      previewEl.append(el('span', { class: 'thread-draft', text: t('threads.draft') }));
    }
    previewEl.append(document.createTextNode(preview));

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
        previewEl,
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

/** Repaint one thread's row, but ONLY when that thread is in the list on
 *  screen. patchThreadRow() falls back to a full renderThreads() for a thread
 *  it cannot find, which is right when the row *should* be there and wrong
 *  here: a background bot's turn would rebuild the list you are reading (the
 *  107ms-at-207-threads repaint threadRowEl exists to avoid) for a row that is
 *  not even displayed. Nothing to repaint is not a reason to repaint
 *  everything. Returns true if a row was patched or the list was rebuilt.
 */
function touchThreadRow(threadId) {
  if (!threadId) return false;
  if (!state.threads.some((x) => x.id === threadId)) return false;
  patchThreadRow(threadId);
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
  dom['ch-modelchip'].hidden = true;
  toggleModelPicker(false);
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
      : [el('div', { class: 'empty-emoji' }, [railIcon(RAIL_ICONS.messages)]),
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
  renderModelChip();
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

// `regenerate`, not `retry`. They are two different things and both exist:
// retry re-asks inside the session the family already has (Safe Mode can do
// it, and the button under a failed reply still sends it); regenerate rewinds
// the session past the previous answer first, so the model is not writing a
// variation on the text it just wrote. Rewinding deletes rows, so it is
// unlocked-only — which is why this affordance lives behind `!state.decoy`.
function regenerateLast() {
  const tid = state.activeThreadId;
  if (tid && socket && socket.send) socket.send({ type: 'regenerate', thread_id: tid });
}

// The newest USER row, which is the only one the gateway can be rewound to:
// rewind addresses a session by the entry id of a user turn, and that is the
// one turn DisPatch can identify without guessing. Mirrors the server's
// get_last_user_message gate so the button is not offered where it 403s.
function isLastUserMessage(msg) {
  for (let i = state.messages.length - 1; i >= 0; i -= 1) {
    if (state.messages[i].role === 'user') return state.messages[i].id === msg.id;
  }
  return false;
}

// Is this thread's bot a direct-provider ("Connect an AI") bot? Hide-from-
// context is offered for those ONLY. For a gateway bot the transcript lives
// in the gateway and DisPatch cannot edit it, so the button would hide a row
// from view while the agent still remembered it — a control that reports
// success and does nothing, which is the exact defect class this app keeps
// getting burned by.
function threadIsApiBot(threadId) {
  const bot = botById(state.threadBot[threadId] || state.activeThread?.bot_id);
  return !!(bot && bot.api_provider);
}

async function toggleMessageHidden(msg, hidden) {
  try {
    await api.editMessage(msg.id, { hidden });
  } catch (e) { toast(e.message, true); }
}

/** Edit a message in place, optionally re-running the turn it started.
 *
 *  Save alone rewrites the row (and stamps the "edited" marker server-side).
 *  Save and rerun goes over the socket instead, because it is a turn: the
 *  server rewinds the session, drops the superseded reply and asks again.
 */
function openMessageEditor(msg, node) {
  const col = node.querySelector('.msg-col');
  const bubble = node.querySelector('.bubble');
  if (!col || !bubble || col.querySelector('.msg-editor')) return;
  const ta = el('textarea', { class: 'msg-editor-input', rows: '3', 'aria-label': t('msg.edit') });
  ta.value = msg.content || '';
  const close = () => { box.remove(); bubble.hidden = false; };
  const save = el('button', { class: 'msg-act-btn', text: t('common.save') });
  const rerun = isLastUserMessage(msg)
    ? el('button', { class: 'msg-act-btn', text: t('msg.save_rerun'),
                     title: t('msg.save_rerun_title') })
    : null;
  const cancel = el('button', { class: 'msg-act-btn', text: t('common.cancel') });
  save.addEventListener('click', async () => {
    const text = ta.value.trim();
    if (!text) return;
    try {
      await api.editMessage(msg.id, { content: text });
      // The message_update broadcast repaints the row; close explicitly too,
      // so a dropped socket does not leave the editor standing over a message
      // that was already saved.
      close();
    } catch (e) { toast(e.message, true); }
  });
  if (rerun) {
    rerun.addEventListener('click', () => {
      const text = ta.value.trim();
      if (!text) return;
      const ok = !!(socket && socket.send({
        type: 'edit_rerun', thread_id: msg.thread_id,
        message_id: msg.id, content: text,
      }));
      if (!ok) { toast(t('msg.edit_offline'), true); return; }
      close();
    });
  }
  cancel.addEventListener('click', close);
  ta.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  const box = el('div', { class: 'msg-editor' }, [
    ta,
    el('div', { class: 'msg-editor-row' }, [save, rerun, cancel].filter(Boolean)),
  ]);
  bubble.hidden = true;
  bubble.after(box);
  ta.focus();
  try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch { /* ignore */ }
}

/** The ‹ n/m › pager over a reply's superseded generations.
 *
 *  The LIVE reply is the last page — the one on screen when nothing has been
 *  clicked — and the alternates are the older answers in the order they were
 *  generated. Paging is a view, not a write: nothing is persisted, so a
 *  reload lands back on the live answer.
 */
function altPagerEl(msg, bubble, mdOpts) {
  const alts = (msg.metadata && Array.isArray(msg.metadata.alternates))
    ? msg.metadata.alternates : [];
  if (!alts.length) return null;
  const pages = alts.concat([{ content: msg.content || '' }]);
  let idx = pages.length - 1;
  const label = el('span', { class: 'alt-pos' });
  const prev = el('button', { class: 'alt-btn', type: 'button', title: t('msg.alt_prev'), 'aria-label': t('msg.alt_prev'), text: '‹' });
  const next = el('button', { class: 'alt-btn', type: 'button', title: t('msg.alt_next'), 'aria-label': t('msg.alt_next'), text: '›' });
  const paint = () => {
    // What Copy reads: the page on screen, not always the live reply.
    msg._shownContent = pages[idx].content || '';
    bubble.innerHTML = renderMarkdown(pages[idx].content || '', mdOpts);
    enhanceContent(bubble, { noLocal: state.decoy });
    label.textContent = t('msg.alt_pos', { n: idx + 1, total: pages.length });
    prev.disabled = idx === 0;
    next.disabled = idx === pages.length - 1;
  };
  prev.addEventListener('click', (e) => {
    e.stopPropagation(); if (idx > 0) { idx -= 1; paint(); }
  });
  next.addEventListener('click', (e) => {
    e.stopPropagation(); if (idx < pages.length - 1) { idx += 1; paint(); }
  });
  paint();
  return el('div', { class: 'alt-pager' }, [prev, label, next]);
}

function editUserMessage(msg) {
  const inp = dom['input'];
  if (!inp) return;
  inp.value = msg.content || '';
  autosize(); updateSendEnabled(); inp.focus();
  try { inp.setSelectionRange(inp.value.length, inp.value.length); } catch { /* ignore */ }
}

// ===================== Reply / quote (Feature 5) =====================
// A client-side PREVIEW only — a rough echo of the server's own media/doc
// strip (_strip_media_text / _quote_excerpt in main.py), used solely to show
// something sensible in the chip BEFORE the quoting message exists. The
// canonical reply_excerpt the server stores (and every OTHER device renders)
// is computed once, server-side, when the reply is actually sent — the two
// never need to match byte-for-byte.
const QUOTE_PREVIEW_MAX = 160;
function quotePreview(content) {
  let s = (content || '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[\[media:[^\]|]+(\|[^\]]*)?\]\]/g, '')
    .replace(/\[\[doc:[^\]|]+(\|[^\]]*)?\]\]/g, '')
    .trim();
  if (s.length > QUOTE_PREVIEW_MAX) s = `${s.slice(0, QUOTE_PREVIEW_MAX).trimEnd()}…`;
  return s;
}

function replyWhoLabel(role) {
  if (role === 'user') return t('common.you');
  const bot = botById(state.threadBot[state.activeThreadId])
    || botById(state.activeThread?.bot_id) || botById(state.selectedBotId);
  return (bot && bot.name) || t('common.assistant');
}

function renderReplyChip() {
  const chip = dom['reply-chip'];
  if (!chip) return;
  const target = state.replyTarget;
  chip.classList.toggle('hidden', !target);
  if (!target) return;
  dom['reply-chip-label'].textContent = t('composer.reply_label', { name: replyWhoLabel(target.role) });
  dom['reply-chip-excerpt'].textContent = toPlainPreview(target.text);
}

// Nothing worth quoting (a media-only message, say) silently declines rather
// than staging an empty chip nobody could make sense of.
function setReplyTarget(msg) {
  if (!msg || !msg.id) return;
  const preview = quotePreview(msg.content);
  if (!preview) return;
  state.replyTarget = { id: msg.id, role: msg.role, text: preview };
  renderReplyChip();
  dom['input'].focus();
}

function clearReplyTarget() {
  if (!state.replyTarget) return;
  state.replyTarget = null;
  renderReplyChip();
}

// A quoted row that is not currently painted (an older page not yet loaded,
// or the quote survives a delete) simply cannot be jumped to — silent no-op,
// same tolerance the rest of this feature gives a quote that no longer
// resolves to anything.
function jumpToMessage(id) {
  const target = dom['messages'].querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (!target) return;
  target.scrollIntoView({ block: 'center', behavior: 'smooth' });
  target.classList.add('flash');
  setTimeout(() => target.classList.remove('flash'), 1000);
}

function quoteBlockEl(meta) {
  const targetId = meta.reply_to;
  return el('div', {
    class: 'quote-block', role: 'button', tabindex: '0',
    title: t('msg.reply_jump_title'),
    onclick: () => jumpToMessage(targetId),
    onkeydown: (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); jumpToMessage(targetId); }
    },
  }, [
    el('div', { class: 'quote-block-label', text: replyWhoLabel(meta.reply_role) }),
    el('div', { class: 'quote-block-excerpt', text: toPlainPreview(meta.reply_excerpt) }),
  ]);
}

// ===================== Feedback (Feature 28) =====================
// Fixed vocabulary, matching MessageFeedbackIn.reason server-side exactly —
// there is no free-text path from this UI to the prompt.
const FEEDBACK_REASONS = ['inaccurate', 'unhelpful', 'too_long', 'off_topic', 'tone', 'other'];

async function sendFeedback(msg, vote, reason) {
  try {
    const body = reason ? { vote, reason } : { vote };
    const r = await api.messageFeedback(msg.id, body);
    if (!r || !r.feedback) return;
    const idx = state.messages.findIndex((m) => m.id === msg.id);
    const nextMsg = idx >= 0
      ? { ...state.messages[idx], metadata: { ...(state.messages[idx].metadata || {}), feedback: r.feedback } }
      : { ...msg, metadata: { ...(msg.metadata || {}), feedback: r.feedback } };
    if (idx >= 0) state.messages[idx] = nextMsg;
    const oldEl = dom['messages'].querySelector(`[data-id="${CSS.escape(msg.id)}"]`);
    if (oldEl) {
      const nextEl = messageEl(nextMsg);
      if (nextEl) {
        if (oldEl.classList.contains('grouped')) nextEl.classList.add('grouped');
        oldEl.replaceWith(nextEl);
      }
    }
    toast(t('msg.feedback_sent'));
  } catch {
    toast(t('msg.feedback_failed'), true);
  }
}

// The ⋯ menu's feedback entries. 👍 sends at once; 👎 swaps the menu to the
// fixed reason list (Skip sends a bare down-vote), so no reason row is ever
// left sitting under the message.
function feedbackMenuItems(msg) {
  const current = ((msg.metadata || {}).feedback || {}).vote || null;
  const reasons = () => [
    ...FEEDBACK_REASONS.map((reason) => ({
      label: t(`msg.feedback_reason_${reason}`),
      fn: () => sendFeedback(msg, 'down', reason),
    })),
    { label: t('msg.feedback_reason_skip'), muted: true, fn: () => sendFeedback(msg, 'down') },
  ];
  return [
    { icon: 'thumbsup', label: t('msg.feedback_good'), title: t('msg.feedback_up_title'),
      checkable: true, active: current === 'up', fn: () => sendFeedback(msg, 'up') },
    { icon: 'thumbsdown', label: t('msg.feedback_bad'), title: t('msg.feedback_down_title'),
      checkable: true, active: current === 'down', fn: () => reasons() },
  ];
}

// Copy is the one action left on screen, as an icon: a word under every
// bubble is exactly the clutter the ⋯ menu exists to remove.
function copyMsgBtn(msg) {
  const b = el('button', {
    class: 'msg-act-btn msg-icon-btn', type: 'button',
    title: t('msg.copy_title'), 'aria-label': t('msg.copy'),
  }, [railIcon(RAIL_ICONS.copy)]);
  b.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(
        (msg._shownContent != null ? msg._shownContent : msg.content) || '');
      b.replaceChildren(railIcon(RAIL_ICONS.tick)); b.classList.add('ok');
      setTimeout(() => { b.replaceChildren(railIcon(RAIL_ICONS.copy)); b.classList.remove('ok'); }, 1200);
    } catch { /* clipboard unavailable */ }
  });
  return b;
}

function moreMsgBtn(items) {
  const b = el('button', {
    class: 'msg-act-btn msg-icon-btn msg-more-btn', type: 'button',
    title: t('msg.more'), 'aria-label': t('msg.more'),
    'aria-haspopup': 'menu', 'aria-expanded': 'false',
  }, [railIcon(RAIL_ICONS.menu)]);
  b.addEventListener('click', (e) => {
    e.stopPropagation();
    if (msgMenu.anchor === b) closeMsgMenu(); else openMsgMenu(b, items);
  });
  return b;
}

// ONE shared popover for every message's ⋯. Fixed-positioned on <body> so
// no bubble's overflow clips it, and so a thread of 500 messages carries 500
// buttons, not 500 hidden menus. An item's fn may return another item list,
// which replaces the menu in place (👎 → reasons).
const msgMenu = { el: null, anchor: null };
function closeMsgMenu() {
  if (msgMenu.anchor) msgMenu.anchor.setAttribute('aria-expanded', 'false');
  msgMenu.anchor = null;
  if (msgMenu.el) msgMenu.el.hidden = true;
  document.removeEventListener('click', _msgMenuOutside, true);
  window.removeEventListener('resize', closeMsgMenu);
  if (dom['messages']) dom['messages'].removeEventListener('scroll', closeMsgMenu);
}
function _msgMenuOutside(e) {
  if (msgMenu.el && msgMenu.el.contains(e.target)) return;
  if (msgMenu.anchor && msgMenu.anchor.contains(e.target)) return;
  closeMsgMenu();
}
function fillMsgMenu(items) {
  const m = msgMenu.el;
  m.replaceChildren();
  items.forEach((it, i) => {
    if (it.sep && i > 0) m.append(el('div', { class: 'msg-menu-sep', role: 'separator' }));
    const btn = el('button', {
      type: 'button', role: it.checkable ? 'menuitemradio' : 'menuitem',
      class: [it.danger ? 'danger' : '', it.active ? 'active' : '', it.muted ? 'muted' : ''].filter(Boolean).join(' '),
      title: it.title || '',
      ...(it.checkable ? { 'aria-checked': String(!!it.active) } : {}),
    }, [
      it.icon && RAIL_ICONS[it.icon] ? railIcon(RAIL_ICONS[it.icon]) : el('span', { class: 'msg-menu-noicon' }),
      el('span', { text: it.label }),
    ]);
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const next = it.fn();
      if (Array.isArray(next)) { fillMsgMenu(next); placeMsgMenu(); return; }
      const a = msgMenu.anchor;
      closeMsgMenu();
      // Focus goes back to the ⋯ that opened the menu, so a keyboard user is
      // not dropped on <body> after every pick.
      if (a && a.isConnected) a.focus({ preventScroll: true });
    });
    m.append(btn);
  });
  const first = m.querySelector('button');
  if (first) first.focus({ preventScroll: true });
}
function placeMsgMenu() {
  const m = msgMenu.el; const a = msgMenu.anchor;
  if (!m || !a) return;
  const r = a.getBoundingClientRect();
  const mw = m.offsetWidth || 200; const mh = m.offsetHeight || 200;
  const vw = window.innerWidth; const vh = window.innerHeight;
  // Align to the button's end edge for a user bubble (right side), start
  // edge otherwise; flip above when there is no room below.
  const alignEnd = !!a.closest('.msg.user') !== (document.documentElement.dir === 'rtl');
  let left = alignEnd ? r.right - mw : r.left;
  left = Math.max(8, Math.min(left, vw - mw - 8));
  let top = r.bottom + 4;
  if (top + mh > vh - 8) top = Math.max(8, r.top - mh - 4);
  m.style.left = left + 'px';
  m.style.top = top + 'px';
}
function openMsgMenu(anchor, items) {
  closeMsgMenu();
  if (!msgMenu.el) {
    msgMenu.el = el('div', { class: 'menu msg-menu', role: 'menu' });
    // Tab out of the menu (or a click that focuses something else) closes it,
    // as a real popover does; the mouse-outside listener below does not see
    // keyboard focus moves.
    msgMenu.el.addEventListener('focusout', (e) => {
      const to = e.relatedTarget;
      if (!to || msgMenu.el.contains(to) || (msgMenu.anchor && msgMenu.anchor.contains(to))) return;
      closeMsgMenu();
    });
    msgMenu.el.addEventListener('keydown', (e) => {
      const btns = [...msgMenu.el.querySelectorAll('button')];
      const i = btns.indexOf(document.activeElement);
      if (e.key === 'Escape') { const a = msgMenu.anchor; closeMsgMenu(); if (a) a.focus(); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); btns[(i + 1) % btns.length]?.focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); btns[(i - 1 + btns.length) % btns.length]?.focus(); }
    });
    document.body.append(msgMenu.el);
  }
  msgMenu.anchor = anchor;
  anchor.setAttribute('aria-expanded', 'true');
  msgMenu.el.hidden = false;
  fillMsgMenu(items);
  placeMsgMenu();
  setTimeout(() => {
    document.addEventListener('click', _msgMenuOutside, true);
    window.addEventListener('resize', closeMsgMenu);
    if (dom['messages']) dom['messages'].addEventListener('scroll', closeMsgMenu, { passive: true });
  }, 0);
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

  // A reaction trace renders as a compact centred row that carries both the
  // collapsed chip and the embedded picture — a reaction lives in the chat,
  // pops up for its duration, then collapses back to the chip.
  //
  // Decided BEFORE the avatar is built, and that order is load-bearing:
  // avatarNode() constructs an <img>, which starts a network request the
  // moment it gets a src. Building it first and then returning the trace row
  // threw the node away but not the fetch — every reaction trace in a thread
  // pulled the bot's avatar for a row that never showed one.
  const trace = reactionMessageEl(msg, { decoy: mediaHidden() });
  if (trace) return trace;

  // Machine-posted alerts (failed runs, watchdogs, missed pictures) collapse
  // to one quiet line — see notice.js. Also decided before the avatar, for
  // the same wasted-fetch reason as the trace row above.
  const notice = classifyNotice(msg);
  if (notice) return noticeMessageEl(msg, notice);

  const wrap = el('div', { class: `msg ${role}`, dataset: { id: msg.id } });

  if (role !== 'user') {
    const bot = botById(state.threadBot[msg.thread_id]) || botById(state.activeThread?.bot_id) || botById(state.selectedBotId);
    const av = avatarNode(bot, 'msg-avatar', state.activeThread);
    // avatarNode already set data-full; Safe Mode must not offer full-res.
    if (state.decoy) delete av.dataset.full;
    wrap.append(av);
  }

  const col = el('div', { class: 'msg-col' });
  const isSub = !!(msg.metadata && msg.metadata.sub);
  // Full-width assistant rows get a small name header; the model still shows in
  // the time line below. User / system / sub messages keep their compact look.
  if (role === 'assistant' && !isSub) {
    const hbot = botById(state.threadBot[msg.thread_id]) || botById(state.activeThread?.bot_id) || botById(state.selectedBotId);
    col.append(el('div', { class: 'msg-head' }, [
      nameSpan('msg-name', (hbot && hbot.name) ? hbot.name : t('common.assistant'), hbot),
    ]));
  }
  // Feature 5 (quote/reply): a message that quotes another renders a small
  // tappable block above its own bubble. reply_excerpt is ALREADY computed
  // and stored server-side (composer send or the gateway's own directive —
  // see main.py's _quote_excerpt), so this is a pure read, no client-side
  // lookup of the (possibly unloaded, possibly unsafe-bot) original.
  if (msg.metadata && msg.metadata.reply_to && msg.metadata.reply_excerpt) {
    col.append(quoteBlockEl(msg.metadata));
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
  // noLocal is the same idea one tier up: local-path affordances (bare paths,
  // [[view:]] cards, local markdown links) are an unlocked-operator feature, so
  // a Safe-Mode render must not produce them at all. mediaHidden() is the wrong
  // gate here — No-Image Mode hides pictures, it does not lock the device.
  const mdOpts = { noMedia: mediaHidden(), noLocal: state.decoy };
  if (isSub) {
    // Intermediate / working output: collapsed by default, click to expand.
    wrap.classList.add('sub-msg');
    const details = el('details', { class: 'sub-details' });
    // The row's own first line is its summary ("🛠️ Exec failed: …", a
    // demoted narration) — a fixed "Working…" on every collapsed row made them
    // indistinguishable. "Working…" stays only for a row with no text.
    details.append(el('summary', { dir: 'auto', text: subSummary(msg.content) }));
    const inner = el('div', { class: 'sub-content' });
    inner.innerHTML = renderMarkdown(msg.content || '', mdOpts);
    enhanceContent(inner, { noLocal: state.decoy });
    details.append(inner);
    bubble.append(details);
  } else if (role === 'assistant') {
    // A picture the bot asked for but has not received yet (or never will):
    // a distinct card in place of the body. A FINISHED image job returns null
    // here on purpose and falls through to the markdown path below — its body
    // is an ordinary media directive, and rendering it any other way would
    // fork the lightbox / Safe-Mode / No-Image-Mode rules.
    const jobCard = imageJobMessageEl(msg);
    if (jobCard) {
      bubble.classList.add('image-job-bubble');
      bubble.append(jobCard);
    } else {
      bubble.innerHTML = renderMarkdown(msg.content || '', mdOpts);
      enhanceContent(bubble, { noLocal: state.decoy });
    }
  } else {
    // User / system: plain text with preserved line breaks; attachments
    // extracted and rendered as thumbnails below the bubble.
    const { text, media, docs } = splitUserContent(msg.content);
    if (!state.decoy) userMedia = media;
    // Plain text with line breaks — but a pasted URL becomes a real link
    // (linkifyPlain escapes everything else exactly as escapeHtml did).
    bubble.innerHTML = linkifyPlain(text).replace(/\n/g, '<br>');
    retargetLinks(bubble);
    markSpeech(bubble);
    markParens(bubble);
    if (!text && media.length && !state.decoy) bubble.classList.add('media-only');
    // Render document cards below the bubble.
    if (!state.decoy) {
      for (const doc of (docs || [])) {
        const name = doc.name || doc.id || t('common.file');
        // Percent-encode: an id is ONE path segment, never a path. Interpolated
        // raw, a crafted `[[doc:../../api/export?format=json]]` built a card
        // whose "download" link walked out of /api/files/ and fetched a full
        // chat export instead. Same fix as markdown.js's expandDocDirectives.
        const did = encodeURIComponent(doc.id);
        const card = el('div', { class: 'doc-card' }, [
          el('span', { class: 'doc-icon' }, [fileIcon()]),
          el('a', { class: 'doc-link', href: `/api/files/${did}/download`, text: name, download: name, target: '_blank' }),
          el('a', { class: 'doc-preview-link', href: `/api/files/${did}/raw`, target: '_blank', title: t('msg.view_raw') }, [railIcon(RAIL_ICONS.eye)]),
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
  const timeLine = el('div', { class: 'msg-time', text: timeText });
  // A transcript that can be rewritten in place and shows no sign of it is a
  // transcript that lies. The marker is the feature, not decoration — and the
  // pre-edit text is what it shows on hover, which is the only thing
  // `metadata.original` is kept for.
  if (meta.edited_at) {
    timeLine.append(el('span', {
      class: 'msg-edited',
      title: meta.original
        ? t('msg.edited_was', { text: String(meta.original).slice(0, 200) })
        : t('msg.edited_title'),
      text: ` · ${t('msg.edited')}`,
    }));
  }
  if (meta.hidden) {
    wrap.classList.add('ctx-hidden');
    timeLine.append(el('span', {
      class: 'msg-ctx-hidden', title: t('msg.hidden_title'),
      text: ` · ${t('msg.hidden_marker')}`,
    }));
  }
  // Time and the Copy/⋯ buttons share one line: a separate action row
  // cost every message an extra line of height for two small icons.
  const foot = el('div', { class: 'msg-foot' }, [timeLine]);
  col.append(foot);

  // Superseded generations, if this reply replaced one.
  if (role === 'assistant' && !isSub && !bubble.classList.contains('image-job-bubble')) {
    const pager = altPagerEl(msg, bubble, mdOpts);
    if (pager) col.append(pager);
  }

  // Action row: Copy + a ⋯ menu, nothing else on screen. Every other action
  // lives in the menu (msgMenu below) so a long thread is not a wall of
  // buttons — on a phone the row is always visible, so each button costs a
  // line under EVERY message. Revealed on hover (desktop), always
  // tap-reachable (touch).
  const actions = el('div', { class: 'msg-actions' });
  const items = [];
  if (role !== 'system') actions.append(copyMsgBtn(msg));
  // Reply/quote (Feature 5) and thumbs feedback (Feature 28) are BOTH allowed
  // in Safe Mode — quoting is send-shaped (a locked device may already send)
  // and rating is exactly what the family exists to do — so neither sits
  // inside the `!state.decoy` block below, unlike regenerate/to-composer/
  // delete, which stay full-session (mutations on history, not conversation).
  if (!isSub) {
    if (role !== 'system' && quotePreview(msg.content)) {
      items.push({ icon: 'reply', label: t('msg.reply'), title: t('msg.reply_action_title'), fn: () => setReplyTarget(msg) });
    }
    if (role === 'assistant') items.push(...feedbackMenuItems(msg));
  }
  // Safe Mode: Copy/Reply/Feedback only. Regenerate/Delete are decoy-blocked
  // server-side and the "copy to composer" affordance would just advertise
  // the lock — showing dead buttons defeats the deniability model.
  if (!state.decoy) {
    const mut = [];
    if (role === 'assistant' && !isSub && state.messages[state.messages.length - 1]?.id === msg.id) {
      mut.push({ icon: 'regenerate', label: t('msg.regenerate'), title: t('msg.regenerate_title'), fn: () => regenerateLast() });
    }
    if (role === 'user') {
      // Edit rewrites the stored row; the newest user message can also be
      // re-run from inside the editor. Distinct from the item below it,
      // whose honest label says it only prefills the composer — sending that
      // creates a NEW message and the original stays untouched.
      mut.push({ icon: 'rename', label: t('msg.edit'), title: t('msg.edit_title'), fn: () => openMessageEditor(msg, wrap) });
      mut.push({ icon: 'forward', label: t('msg.to_composer'), title: t('msg.to_composer_title'), fn: () => editUserMessage(msg) });
    }
    // API bots only — see threadIsApiBot.
    if (role !== 'system' && threadIsApiBot(msg.thread_id)) {
      const hidden = !!meta.hidden;
      mut.push({
        icon: 'eye',
        label: hidden ? t('msg.show_context') : t('msg.hide_context'),
        title: hidden ? t('msg.show_context_title') : t('msg.hide_context_title'),
        fn: () => toggleMessageHidden(msg, !hidden),
      });
    }
    mut.push({ icon: 'trash', label: t('msg.delete'), title: t('msg.delete_title'), danger: true, sep: true,
      fn: () => deleteMessage(msg.id, msg.thread_id) });
    items.push(...mut);
  }
  if (items.length) actions.append(moreMsgBtn(items));
  if (actions.childElementCount) foot.append(actions);

  wrap.append(col);

  // click-to-zoom for inline videos. Images are NOT wired here any more:
  // markdown gives every inline <img> a data-full, and the one delegated
  // lightbox listener owns that attribute. Wiring both opened TWO stacked
  // lightboxes per click — closing one revealed the other.
  bubble.querySelectorAll('video').forEach((v) =>
    v.addEventListener('click', () => openLightbox(v.getAttribute('src'), { video: true })));
  pinOnImageLoad(wrap);
  // Interactive ```checklist tables: inject checkboxes + persistence. Safe Mode
  // gets the widgets read-only (VIEW + SEND only — checking is a mutation).
  installChecklists(wrap, { message: msg, readonly: state.decoy });
  return wrap;
}

// A collapsed sub row's one-line summary: its first line, plain, capped.
const SUB_SUMMARY_MAX = 120;
function subSummary(content) {
  const line = noticeHeadline(content);
  if (!line) return glyphless(t('msg.working'));
  return line.length > SUB_SUMMARY_MAX ? `${line.slice(0, SUB_SUMMARY_MAX - 1).trimEnd()}…` : line;
}

// One collapsed line for a system notice: a level dot, the headline and the
// time; the full text (rendered markdown) only when opened. No avatar, no
// name, no bubble — status, not conversation. Copy/Delete live inside the
// opened body so the closed row stays a single line.
function noticeMessageEl(msg, notice) {
  const wrap = el('div', { class: `msg notice notice-${notice.level}`, dataset: { id: msg.id } });
  const details = el('details', { class: 'notice-details' });
  const time = clockTime(msg.created_at);
  details.append(el('summary', { class: 'notice-summary' }, [
    el('span', { class: 'notice-dot', 'aria-hidden': 'true' }),
    // The level is a colour on the dot for sighted readers; say it in words
    // for everyone else.
    el('span', { class: 'sr-only', text: t(`msg.notice_level_${notice.level}`) }),
    el('span', { class: 'notice-text', dir: 'auto', text: notice.headline || t('msg.notice') }),
    time ? el('span', { class: 'notice-time', text: time }) : null,
  ]));
  const body = el('div', { class: 'notice-body', dir: 'auto' });
  // Filled on first open: a thread full of closed notices renders no markdown.
  details.addEventListener('toggle', () => {
    if (!details.open || body.dataset.filled) return;
    body.dataset.filled = '1';
    body.innerHTML = renderMarkdown(msg.content || '', { noMedia: mediaHidden(), noLocal: state.decoy });
    enhanceContent(body, { noLocal: state.decoy });
    const actions = el('div', { class: 'msg-actions notice-actions' }, [copyMsgBtn(msg)]);
    if (!state.decoy) {
      actions.append(moreMsgBtn([{ icon: 'trash', label: t('msg.delete'), title: t('msg.delete_title'),
        danger: true, fn: () => deleteMessage(msg.id, msg.thread_id) }]));
    }
    body.append(actions);
  });
  details.append(body);
  wrap.append(details);
  return wrap;
}

// Consecutive same-sender rows group: hide the repeated avatar/name + tighten
// the gap (iMessage/Slack feel). Only within ~5 min and the same calendar day.
function isGrouped(prev, msg) {
  if (!prev || !msg || prev.role !== msg.role) return false;
  // A collapsed notice shows no avatar, so a reply after it must show its own.
  if (classifyNotice(prev) || classifyNotice(msg)) return false;
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
  closeMsgMenu();
  const box = dom['messages'];
  box.innerHTML = '';   // takes any cached-tail rows with it
  cachedPainted.delete(state.activeThreadId);
  // NOTE: the 2026-09-15 redesign dropped the in-chat job-card header
  // in favour of the modal-based detail panel (openJobDetail in
  // job-thread.js, surfaced by the Jobs board list). The board list
  // is the single entry point for browsing jobs; clicking a row opens
  // the detail modal, which carries the structured metadata + voting
  // controls. There is no in-chat card to mount any more — keeping the
  // placeholder-mountJobCard() path would render an empty <div id="job-card-host">
  // into every jobboard chat thread for no reason.
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
    const icon = it.kind === 'tool' ? RAIL_ICONS.tools : (it.kind === 'thinking' ? RAIL_ICONS.brain : RAIL_ICONS.messages);
    const label = it.kind === 'tool' ? `${it.name}  ${it.text}` : it.text;
    frag.append(el('div', { class: `progress-item ${it.kind}` }, [railIcon(icon), document.createTextNode(` ${label}`)]));
  }
  panel.append(frag);
  panel._painted = items.length;
  panel.scrollTop = panel.scrollHeight;
}

// ===================== Turn status (gateway phase) =====================
// The gateway reports what a turn is DOING before any token exists —
// preparing context, loading a model, running a tool. On a cold local model
// that is a minute of animated dots saying nothing; this turns it into a line
// that says which minute it is.
//
// Literal keys, not t('turn.phase_' + phase): the phase strings come off the
// wire, so a concatenated lookup would let the server name any key in the
// catalogue (and tests/i18n-keys.test.js could not see which keys are live).
// An unknown phase falls back to the generic label rather than rendering the
// server's own word for it.
const TURN_PHASE_KEYS = {
  preparing_context: 'turn.phase_preparing_context',
  preparing_workspace: 'turn.phase_preparing_workspace',
  starting_model: 'turn.phase_starting_model',
  loading_model: 'turn.phase_starting_model',
  waiting_model: 'turn.phase_waiting_model',
  streaming: 'turn.phase_streaming',
  finishing: 'turn.phase_finishing',
};

function turnPhaseText(phase) {
  if (!phase || typeof phase !== 'string') return '';
  if (phase.startsWith('tool:')) {
    const name = phase.slice(5).trim();
    // A tool name is server-supplied text in a UI string — el()/textContent
    // escape it, and the cap stops one long name from pushing the layout.
    if (name) return t('turn.phase_tool', { name: name.slice(0, 40) });
  }
  const key = TURN_PHASE_KEYS[phase];
  return key ? t(key) : t('turn.phase_generic');
}

/** Paint (or clear) the active thread's phase line.
 *
 *  It rides the typing indicator until the first token arrives, then the
 *  streaming bubble — the typing indicator is REMOVED at stream_start, so a
 *  line parented to it would vanish exactly when a tool call mid-reply makes
 *  it most useful.
 */
function paintTurnPhase() {
  const box = dom['messages'];
  if (!box) return;
  const phase = state.activeThreadId ? state.turnPhase[state.activeThreadId] : null;
  const text = turnPhaseText(phase);
  const host = box.querySelector('.msg.streaming .msg-col') || box.querySelector('.typing-col');
  let line = box.querySelector('.turn-phase');
  if (!text || !host) { if (line) line.remove(); return; }
  // The typing element is rebuilt wholesale by refreshTyping(), and the
  // streaming bubble replaces it — so re-home the line rather than assuming
  // the node it was appended to still exists.
  if (!line || line.parentElement !== host) {
    if (line) line.remove();
    line = el('div', { class: 'turn-phase' });
    host.append(line);
  }
  if (line.textContent !== text) line.textContent = text;
}

function setTurnPhase(threadId, phase) {
  if (!threadId) return;
  if (phase) state.turnPhase[threadId] = phase;
  else delete state.turnPhase[threadId];
  if (threadId === state.activeThreadId) paintTurnPhase();
}

function refreshTyping() {
  const box = dom['messages'];
  const existing = box.querySelector('.typing');
  // Never show the dots while a reply is actively streaming in.
  const want = !!state.thinking[state.activeThreadId] && !box.querySelector('.msg.streaming');
  if (want && !existing) {
    box.append(typingEl());
    if (isNearBottom()) scrollToBottom();
    // The animated dots are the sighted reader's "it heard you". Nothing said
    // so for a screen reader: the next announcement was the finished reply,
    // which for a slow local model is a minute of silence after pressing
    // send. Its own short announcement, separate from the reply's — WCAG
    // 4.1.3, and the reason the content region is NOT reused for it.
    announceResponding();
  } else if (!want && existing) {
    existing.remove();
    // Empty the status region when the wait ends, so a reader who tabs back to
    // it later is not told something is still in progress that finished long
    // ago. aria-atomic + empty text announces nothing.
    if (dom['sr-status']) dom['sr-status'].textContent = '';
  }
  // Either branch can leave the phase line parented to a node that just went
  // away (or arrive at a fresh typing bubble that needs it back).
  paintTurnPhase();
}

function announceResponding() {
  const bot = botById(state.activeThread?.bot_id) || botById(state.selectedBotId);
  const who = (bot && bot.name) ? bot.name : t('common.assistant');
  // 'sr-status', not the message log: this is a transient state, and
  // filing it into the transcript region leaves a reader re-reading
  // "X is responding" between every pair of real messages.
  announce(t('msg.announce_responding', { name: who }), false, 'sr-status');
}

function appendErrorBubble(text, threadId) {
  const box = dom['messages'];
  const bubble = el('div', { class: 'bubble', dir: 'auto' });   // see renderMessage: content picks its own direction
  bubble.append(el('span', { class: 'bubble-error-text' }, iconLabel(RAIL_ICONS.alert, text)));

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
  reflectStopButton();
  refreshTyping();
}

// ===================== Stop (abort a running turn) =====================
// Which thread has an abort in flight. One at a time is enough: the button
// belongs to the active thread and the state clears the moment the turn does.
let stopRequestedThread = null;

function clearStopRequest(threadId) {
  if (!threadId || stopRequestedThread !== threadId) return;
  stopRequestedThread = null;
  reflectStopButton();
}

// Unlocked only. Safe Mode never shows it: the server 403s an abort from a
// decoy session, so the button could only ever fail — and offering a control
// that does nothing is exactly the tell the locked mode exists to avoid.
function canStopReply() {
  return !!(state.activeThreadId && state.thinking[state.activeThreadId] && !state.decoy);
}

function stopReply() {
  if (!canStopReply()) return;
  const tid = state.activeThreadId;
  const ok = !!(socket && socket.send({ type: 'abort', thread_id: tid }));
  if (!ok) { toast(t('toast.offline'), true); return; }
  stopRequestedThread = tid;
  reflectStopButton();
}

function reflectStopButton() {
  const btn = dom['stop'];
  if (!btn) return;
  const show = canStopReply();
  btn.classList.toggle('hidden', !show);
  if (!show) { btn.disabled = false; return; }
  // Disabled until the turn actually ends (or the server refuses, which comes
  // back as an 'error' frame and releases it) — a second click would only
  // send a duplicate abort for a turn already being torn down.
  const pending = stopRequestedThread === state.activeThreadId;
  btn.disabled = pending;
  btn.setAttribute('aria-label', t(pending ? 'composer.stopping' : 'composer.stop'));
  btn.title = t(pending ? 'composer.stopping' : 'composer.stop');
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
  const draftThread = state.activeThreadId;   // the thread this text belongs to
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
  // Included in the frame itself (not a separate call) so a reconnect replay
  // — which resends this exact object unchanged — carries the quote too.
  if (state.replyTarget) frame.reply_to = state.replyTarget.id;
  const ok = !!(socket && socket.send(frame));
  if (!ok) { toast(t('toast.offline'), true); return; }
  trackPendingSend(frame);
  // Show what was just sent, immediately. Before this the composer emptied and
  // nothing appeared until the server's broadcast came back — on a slow link
  // (or a stalled turn) that reads as "my message was eaten", and the Retry
  // chip fired 20s later against a chat that showed no trace of it.
  showOptimisticSend(frame.client_msg_id, state.activeThreadId, full);

  dom['input'].value = '';
  // The words are in the outbox now, so the draft copy of them is no longer a
  // safety net — it is a second copy that would reappear in the composer the
  // next time this thread is opened.
  clearDraftFor(draftThread);
  clearAttachments();
  clearReplyTarget();
  autosize();
  // Optimistic "thinking" — shows the typing bubble + waiting label right away
  // (the composer itself stays usable for queued follow-up messages).
  state.thinking[state.activeThreadId] = true;
  reflectComposerState();
}

// ===================== Local store (drafts / outbox / offline reading) ======
// js/store.js keeps three things in IndexedDB: the composer text you never
// sent, the sends the server has not acked, and the tail of the threads you
// opened. All three are conveniences; none of them is authoritative.
//
// TIER. The store is scoped to the tier this session is running in, and the
// instance is REBUILT whenever that changes. A locked session gets a store
// that can only compose safe-tier keys, so the unlocked tier's cache is not
// something it is trusted not to read — it is something it cannot address.
// Dropping to Safe Mode then deletes the unlocked tier outright (goSafe).
let localStore = null;
let cachePainter = null;
// Thread ids whose cached tail actually reached the screen. Only used to
// decide whether a FAILED fetch may leave those rows up instead of replacing a
// readable history with an empty state.
const cachedPainted = new Set();

function ensureLocalStore() {
  const tier = tierFor(state.decoy);
  if (localStore && localStore.tier === tier) return localStore;
  if (localStore) localStore.close();
  localStore = createStore({ tier });
  cachePainter = createCachePainter({
    store: localStore,
    // "Nothing real is on screen" — state.messages is reassigned per thread, so
    // an empty one means the fetch has not landed and no live frame has been
    // appended.
    isEmpty: (tid) => tid === state.activeThreadId && !state.messages.length,
    paint: paintCachedMessages,
  });
  cachedPainted.clear();
  return localStore;
}

/** Load what this tier persisted. Called at boot BEFORE the socket connects, so
 *  restored outbox frames are in pendingSends by the time the first 'open'
 *  fires and the normal replay path carries them — no second send mechanism,
 *  and no window where a queued message is neither on screen nor in flight. */
async function restoreLocalState() {
  const store = ensureLocalStore();
  state.drafts = new Set();
  try {
    for (const id of await store.draftThreadIds()) state.drafts.add(id);
  } catch { /* storage refused — the app works, the drafts are gone */ }
  try {
    for (const row of await store.listOutbox()) {
      const cmid = row.frame && row.frame.client_msg_id;
      if (!cmid || pendingSends.has(cmid)) continue;
      // sentAt is stamped NOW, not when it was typed: it measures how long this
      // has been waiting on THIS socket, and a frame queued yesterday would
      // otherwise raise the Retry chip before a socket had even opened.
      pendingSends.set(cmid, {
        frame: row.frame, thread_id: row.thread_id, text: row.text, sentAt: Date.now(),
      });
    }
  } catch { /* same */ }
  if (pendingSends.size && !pendingSweepTimer) {
    pendingSweepTimer = setInterval(updateRetryChip, 5000);
  }
  updateRetryChip();
  renderThreads();
}

// -- drafts ------------------------------------------------------------------
const DRAFT_DEBOUNCE_MS = 400;
let draftTimer = null;
let draftPending = null;   // {tid, text} — the thread it was TYPED in

function scheduleDraftSave() {
  const tid = state.activeThreadId;
  if (!tid) return;
  // The thread id is captured here rather than read inside the timeout: a
  // switch during the debounce window would otherwise file one thread's words
  // under another thread's name.
  draftPending = { tid, text: dom['input'].value };
  clearTimeout(draftTimer);
  draftTimer = setTimeout(flushDraft, DRAFT_DEBOUNCE_MS);
}

function flushDraft() {
  clearTimeout(draftTimer); draftTimer = null;
  const p = draftPending; draftPending = null;
  if (p) saveDraft(p.tid, p.text);
}

function saveDraft(tid, text) {
  if (!tid) return;
  const has = !!(text && text.trim());
  const changed = has !== state.drafts.has(tid);
  if (has) state.drafts.add(tid); else state.drafts.delete(tid);
  if (changed) touchThreadRow(tid);
  ensureLocalStore().setDraft(tid, has ? text : '').catch(() => {});
}

/** Drop every local write still waiting on a timer. Called the moment the
 *  tier flips (goSafe) and on reboot: a write that was scheduled in one tier
 *  must never land in the next. */
function cancelLocalWrites() {
  clearTimeout(draftTimer); draftTimer = null; draftPending = null;
  clearTimeout(cacheRefreshTimer); cacheRefreshTimer = null;
}

function clearDraftFor(tid) {
  if (!tid) return;
  clearTimeout(draftTimer); draftTimer = null; draftPending = null;
  const had = state.drafts.delete(tid);
  if (had) touchThreadRow(tid);
  ensureLocalStore().clearDraft(tid).catch(() => {});
}

/** Put a thread's saved draft back in the composer, unless the user has already
 *  started typing into it while the read was in flight. */
async function restoreDraft(threadId) {
  let text = '';
  try { text = await ensureLocalStore().getDraft(threadId); } catch { return; }
  if (!text) return;
  if (state.activeThreadId !== threadId) return;   // switched away mid-read
  if (dom['input'].value) return;                  // already typing — never clobber
  dom['input'].value = text;
  autosize(); updateSendEnabled();
}

// -- offline reading ---------------------------------------------------------

/** Paint cached rows. DOM ONLY — nothing here enters state.messages.
 *
 *  Same reasoning as the optimistic send bubbles: a row in state.messages needs
 *  a real message id (Delete, Regenerate and the "is this the last message"
 *  check all key off it), and a cached row may since have been deleted on the
 *  server. So the id is stripped, the action row is removed, and the next
 *  renderMessages() clears the box out from under all of it.
 */
function paintCachedMessages(threadId, rows) {
  if (threadId !== state.activeThreadId) return;
  const box = dom['messages'];
  if (!box) return;
  const frag = document.createDocumentFragment();
  let painted = 0;
  for (const m of rows) {
    const node = messageEl(m);
    if (!node) continue;              // NIM dropped a picture-only row
    delete node.dataset.id;           // never addressable as a live message
    node.dataset.cached = '1';
    const acts = node.querySelector('.msg-actions');
    if (acts) acts.remove();
    frag.append(node);
    painted += 1;
  }
  if (!painted) return;
  box.innerHTML = '';                 // replaces the shimmer
  box.append(frag);
  cachedPainted.add(threadId);
  scrollToBottom();
}

/** Ask the cache to paint, if the network has not already spoken. Never
 *  awaited by its caller: a slow disk must not hold up the fetch. */
function paintCachedIfUseful(threadId) {
  ensureLocalStore();
  return cachePainter.paintCached(threadId).catch(() => false);
}

// The cached tail is refreshed on a short timer rather than on every frame:
// a thread being read live can take a burst of messages, and the useful thing
// to keep is where the conversation GOT to, not each step on the way.
const CACHE_REFRESH_MS = 1200;
let cacheRefreshTimer = null;

/** Re-cache the active thread's tail after it changes.
 *
 *  Without this the cache only ever holds what a thread looked like when it
 *  was OPENED, so the messages you just read — the ones you would most want to
 *  see again on a dead connection — are exactly the ones missing from it.
 */
function scheduleCacheRefresh(threadId) {
  if (!threadId || threadId !== state.activeThreadId) return;
  clearTimeout(cacheRefreshTimer);
  cacheRefreshTimer = setTimeout(() => {
    cacheRefreshTimer = null;
    const tid = state.activeThreadId;
    if (!tid || tid !== threadId) return;
    const rows = state.messages.filter((m) => m.thread_id === tid);
    ensureLocalStore().cacheMessages(tid, rows.slice(-MESSAGE_CACHE_ROWS)).catch(() => {});
  }, CACHE_REFRESH_MS);
}

/** The network answered for this thread — well or badly — so the cache is now
 *  behind and must never paint for it again.
 *
 *  Goes through ensureLocalStore() rather than touching cachePainter directly:
 *  openThread() is reachable from the jobs board before the first startApp()
 *  has built either, and a bare `cachePainter.markFresh(...)` would throw on
 *  exactly the path that has no cache to worry about.
 */
function markThreadFresh(threadId) {
  if (!threadId) return;
  ensureLocalStore();
  cachePainter.markFresh(threadId);
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

// ===================== Optimistic user bubbles =====================
// client_msg_id -> { el, threadId, text, at }. DOM-only on purpose: these rows
// are NOT pushed into state.messages. A row there needs a real message id —
// Delete, Regenerate and the "is this the last message" check all key off it —
// and a placeholder id would either 404 against the API or survive into the
// next full render as a duplicate of the persisted message. The node carries
// data-cmid instead of data-id, so nothing that addresses messages by id can
// ever find it.
const optimisticSends = new Map();

function showOptimisticSend(cmid, threadId, text) {
  if (!cmid || !threadId) return;
  const box = dom['messages'];
  if (!box) return;
  const node = messageEl({
    id: '', thread_id: threadId, role: 'user', content: text,
    created_at: new Date().toISOString(), metadata: {},
  });
  // NIM returns null for a picture-only message — it has no row when it is
  // persisted either, so there is nothing to preview.
  if (!node) return;
  delete node.dataset.id;              // never addressable as a message
  node.dataset.cmid = cmid;
  node.classList.add('pending');
  // The action row acts on a message id this row does not have.
  const acts = node.querySelector('.msg-actions');
  if (acts) acts.remove();
  const emptyState = box.querySelector('.empty-state');
  if (emptyState) emptyState.remove();
  const typing = box.querySelector('.typing');
  if (typing) box.insertBefore(node, typing); else box.append(node);
  optimisticSends.set(cmid, { el: node, threadId, text, at: Date.now() });
  if (isNearBottom()) scrollToBottom();
}

function dropOptimistic(cmid) {
  const entry = optimisticSends.get(cmid);
  if (!entry) return false;
  optimisticSends.delete(cmid);
  if (entry.el && entry.el.isConnected) entry.el.remove();
  return true;
}

function clearOptimisticSends() {
  for (const cmid of [...optimisticSends.keys()]) dropOptimistic(cmid);
}

/** Reconcile a persisted 'message' frame against an optimistic bubble.
 *
 *  The server echoes client_msg_id on the frame it broadcasts for the send it
 *  just persisted, so that is the identity used. The content fallback exists
 *  because the persist chokepoint can REWRITE the text on the way through (a
 *  typed `:react:x:` marker is stripped), and because an older server build
 *  may not echo the id at all — in which case the only thing left is "same
 *  thread, same author, same words, just now".
 */
function reconcileOptimistic(frame) {
  const msg = frame && frame.message;
  if (!msg || msg.role !== 'user') return;
  const cmid = frame.client_msg_id || msg.client_msg_id;
  if (cmid && dropOptimistic(cmid)) return;
  if (!optimisticSends.size) return;
  const body = (msg.content || '').trim();
  for (const [key, entry] of optimisticSends) {
    // A node wiped by a full re-render (thread switch, avatar repaint) is not
    // a bubble any more; matching against it would consume the entry and leave
    // the real pending one on screen.
    if (!entry.el || !entry.el.isConnected) { optimisticSends.delete(key); continue; }
    if (entry.threadId !== frame.thread_id) continue;
    const mine = (entry.text || '').trim();
    // startsWith, not equality: the marker strip only ever SHORTENS the text.
    if (!(mine === body || (body && mine.startsWith(body)))) continue;
    // "Recent" is measured against OUR OWN clock — how long this bubble has
    // been waiting — never against msg.created_at. The server stamps that, and
    // this app is reached from tablets over Tailscale whose clocks drift by
    // minutes; comparing the two made the fallback fail exactly when the two
    // machines disagreed, which is a condition the user cannot see and cannot
    // fix. The window is only here to stop a much older pending send matching
    // an unrelated identical message.
    if (Date.now() - entry.at > 120000) continue;
    dropOptimistic(key);
    return;
  }
}

// A bubble whose send has been pending too long is marked, not removed: the
// text stays on screen (it is the only copy the user can see) and the existing
// Retry chip is the affordance. Also prunes entries whose node was wiped by a
// full re-render — a thread switch, for instance.
function sweepOptimistic() {
  const now = Date.now();
  for (const [cmid, entry] of [...optimisticSends]) {
    if (!entry.el || !entry.el.isConnected) { optimisticSends.delete(cmid); continue; }
    const stale = pendingSends.has(cmid) && now - entry.at > PENDING_RETRY_MS;
    entry.el.classList.toggle('failed', stale);
  }
}

function trackPendingSend(frame) {
  const entry = {
    frame, thread_id: frame.thread_id, text: frame.text, sentAt: Date.now(),
  };
  pendingSends.set(frame.client_msg_id, entry);
  // ...and on disk, so a reload does not lose it. Keyed by the SAME
  // client_msg_id the server dedups on, which is what lets the restored frame
  // be replayed unchanged: a duplicate is re-acked, never stored twice.
  ensureLocalStore().queueSend(entry).catch(() => {});
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
  ensureLocalStore().dropSend(clientMsgId).catch(() => {});
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
//
// IN MEMORY ONLY, on purpose. The persisted outbox is partitioned by tier, so
// the frames this drops are not reachable from the session that replaces this
// one anyway; and on a LOCK they are deleted outright a moment earlier by
// goSafe's wipe. Clearing the store here as well would mean an UNLOCK — which
// is not a privacy event — silently threw away a Safe-Mode device's queued
// messages.
function dropAllPendingSends() {
  pendingSends.clear();
  clearOptimisticSends();
  clearInterval(pendingSweepTimer); pendingSweepTimer = null;
  restoredComposerText = null;
  updateRetryChip();
}

function updateRetryChip() {
  sweepOptimistic();
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
  // The message was refused, so its optimistic bubble is a lie. Dropped by id
  // rather than left to the renderMessages() below: that call only happens for
  // the ACTIVE thread, and an entry whose node is gone but whose text is still
  // in the map is exactly what makes a later reconcile match the wrong send.
  if (p && p.frame) dropOptimistic(p.frame.client_msg_id);
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
    // The composer was the last surface still building an <img>/<video> in
    // No-Image Mode. Every other one guards (mediaThumbEl, the Bot Manager,
    // Companions, the File Server, reactions) — this one drew a real thumbnail
    // of the picture you had just attached, on a device whose whole claim is
    // that it shows none. A local object URL fetches nothing over the network,
    // but "nothing is downloaded" is only half the promise: NIM says pictures
    // are OMITTED, and one on screen is one on screen. So an image/video chip
    // degrades to exactly what a document chip already is — a file-type glyph
    // plus the filename, which still tells you what is queued to send.
    const asFile = a.kind === 'document' || nimEnabled();
    if (asFile) {
      thumb = el('span', { class: 'chip-doc-icon' }, [fileIcon()]);
    } else if (a.kind === 'video') {
      thumb = el('video', { src: previewSrc, muted: '', loop: '', playsinline: '' });
      thumb.muted = true; thumb.play().catch(() => {});
    } else {
      thumb = el('img', { src: previewSrc, alt: a.name });
    }
    const label = asFile
      ? el('span', { class: 'chip-label', text: a.name.slice(0, 30) })
      : null;
    const children = [thumb];
    if (label) children.push(label);
    // The glyph is aria-hidden (railIcon marks it so), which left this button
    // with no accessible name at all — a screen reader announced "button" and
    // nothing else, and with several files queued there was no way to tell
    // which one it removed. The file name goes in the label for that reason.
    children.push(el('button', {
      class: 'rm',
      'aria-label': t('composer.remove_attachment', { name: a.name || t('composer.attachment') }),
      title: t('composer.remove_attachment', { name: a.name || t('composer.attachment') }),
      onclick: () => { const [rm] = state.attachments.splice(i, 1); if (rm && rm.previewUrl) URL.revokeObjectURL(rm.previewUrl); renderAttachments(); updateSendEnabled(); },
    }, [railIcon(RAIL_ICONS.close)]));
    const chip = el('div', { class: 'chip' + (asFile ? ' doc-chip' : '') }, children);
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
  const closeBtn = el('button', { class: 'lightbox-close', 'aria-label': t('common.close') }, [railIcon(RAIL_ICONS.close)]);
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
    const dl = el('a', { class: 'lightbox-download', href: downloadUrl }, iconLabel(RAIL_ICONS['viewer-download'], t('common.download')));
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
  // Ref-counted: a lightbox opened OVER another modal (an avatar from the Bot
  // Manager) must not re-enable that modal's page when it closes.
  const inertToken = acquireInert();
  cleanups.push(() => releaseInert(inertToken));
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
  // Every tool — the four builtins and anything in tools.yaml — opens
  // through tools.js, which routes builtins back to their own openers.
  if (isToolId(id)) { openTool(id); return; }
  // Leaving a tool pane for a real bot tears the view down cleanly.
  closeToolPanes();
  // Symptom (jobs-fix: view-stuck-on-bot-switch): if we're sitting on the
  // Jobs view when the user clicks another bot, the rest of this function
  // still loads threads AND opens the first one — but the jobs CSS keeps
  // #chatview hidden. The user sees the board stay put, no chats appear,
  // and the title bar stays "Job Board" forever. Mirror what the trailing
  // branches already do for the other views (desktop → chat, mobile →
  // threads) so the openThread / navigate call at the bottom lands in a
  // panel the user can actually see.
  if (dom.app.dataset.view === 'jobs') {
    setView(isMobile() ? 'threads' : 'chat');
  }
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
async function openThread(id, { background = false, botId = null } = {}) {
  const switching = state.activeThreadId !== id;
  // Whatever is half-typed belongs to the thread being LEFT. Flush it before
  // the active id moves, or the debounce timer files it under the new one.
  if (switching && !background) flushDraft();
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
  // From the jobs board (or any other path that hands a thread_id without
  // first calling selectBot()): state.threads only holds the previously-
  // selected bot's threads, so `t` is undefined and the stub above leaves
  // state.activeThread as null. The chat header's "Job Board · Senior Eng"
  // title and the .job-card auto-mount in renderMessages() both gate on
  // state.activeThread.bot_id === 'jobboard' — without a stub they silently
  // never fire. Board callers pass botId='jobboard' so we can stub the
  // minimum needed for routing; the async fetch below patches in the real
  // title/avatar_snapshot once /api/jobs/<id> resolves, without blocking the
  // first paint. Existing threads (Bits, etc.) are unaffected: they
  // always come through selectBot() first, so `t` is found and this branch
  // is skipped. The local `t` above shadows the imported i18n `t`, so the
  // stub title is a hardcoded English fallback — this is NOT localised
  // (deliberately, to keep the shadowing local): the WS-fetched title
  // patches it within a round-trip, so non-English locales see a transient
  // English header for one fetch then the real localised title.
  if (!t && botId) {
    state.activeThread = { id, bot_id: botId, title: 'New Chat' };
    api.jobs.get(id).then((r) => {
      if (state.activeThreadId !== id || !r || !r.thread) return;
      state.activeThread = { ...state.activeThread, ...r.thread };
      renderChatHeader();
    }).catch(() => {});
  }
  // One-directional reconcile from the LOCAL cache: only ever SET the flag.
  // Clearing here could race a just-sent message (the cached status lags the
  // server); clears come from thinking/stopped events and server-fetched
  // reconciliation in selectBot()/resync().
  if (t && t.status === 'thinking') state.thinking[id] = true;
  if (switching && !background) {
    clearAttachments();
    clearReplyTarget();
    // The composer belongs to a thread now. Empty it and put this thread's own
    // draft back (asynchronously — a slow disk must not delay the first paint,
    // and restoreDraft re-checks that we are still here and still empty).
    dom['input'].value = '';
    autosize(); updateSendEnabled();
    void restoreDraft(id);
  }
  renderThreads();
  renderChatHeader();
  // Paint shimmer placeholders while the history request is in flight (only on a
  // real switch — a background re-sync must not blow away the current view).
  if (switching && !background) {
    renderSkeleton();
    // ...and, if we have read this thread before, put the cached tail up in
    // place of the shimmer. Not awaited: the fetch below is the real answer and
    // must not wait on a disk read. The painter refuses to paint once that
    // fetch has resolved, including when it resolves mid-read.
    void paintCachedIfUseful(id);
  }

  const box = dom['messages'];
  const wasNearBottom = isNearBottom();
  const savedTop = box.scrollTop;

  let keptCachedView = false;
  const tierAtFetch = tierFor(state.decoy);
  try {
    const r = await api.messages(id);
    if (state.activeThreadId !== id) { releaseScrollSave(); return; }   // switched away
    // A lock during the fetch: these rows were served to the UNLOCKED tier
    // and must not be cached into the safe one.
    if (tierFor(state.decoy) !== tierAtFetch) { releaseScrollSave(); return; }
    // The network has spoken: from here the cache is behind by definition and
    // must never paint for this thread again.
    markThreadFresh(id);
    const fetched = r.messages || [];
    // Merge, don't replace: keep any live WS messages that landed while the
    // fetch was in flight (they may post-date the HTTP snapshot).
    const ids = new Set(fetched.map((m) => m.id));
    const extras = state.messages.filter((m) => m.thread_id === id && !ids.has(m.id));
    state.messages = fetched.concat(extras)
      .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
    state.hasMoreOlder = !!r.has_more;
    // Keep the tail for the next cold open. These are the rows THE SERVER SENT
    // TO THIS TIER — in Safe Mode that is the redacted view, already redacted
    // upstream. Nothing here un-redacts anything.
    ensureLocalStore().cacheMessages(id, fetched.slice(-MESSAGE_CACHE_ROWS)).catch(() => {});
  } catch (e) {
    if (state.activeThreadId !== id) { releaseScrollSave(); return; }
    toast(e.message, true);
    // The thread's state is emptied either way: leaving the PREVIOUS thread's
    // messages in state.messages while showing this one is how a cached row
    // and a live row end up disagreeing about which conversation you are in.
    state.messages = []; state.hasMoreOlder = false;
    // A failed fetch is NOT fresh data — it is NO data, which is the one
    // moment the cache is allowed to be the answer rather than a preview. So
    // markThreadFresh is deliberately NOT called here: the door stays open for
    // the cache now, and for the first live frame or a later retry to close it
    // properly. Awaited, unlike the optimistic paint above, because nothing is
    // waiting on us any more and an offline open should land on a readable
    // thread rather than an empty state.
    if (cachedPainted.has(id) || await paintCachedIfUseful(id)) keptCachedView = true;
  }
  // Bug 1: Don't scroll to bottom if we have a saved position for the target
  // thread — restore that instead.
  const hasSavedPos = state.scrollPositions[id] !== undefined;
  if (!keptCachedView) renderMessages((!background || wasNearBottom) && !hasSavedPos);
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
    if (act === 'model') {
      openModelPicker();
    } else if (act === 'pin') {
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

// ===================== Model / thinking chip (Feature 7) =====================
// A thread's per-bot model+thinking override, plus a context-used meter, both
// read from data the app already has (state.activeThread.prefs arrives on
// every thread_update; the meter comes off the latest assistant message's
// metadata — see modelchip.js's latestContextBudget). Opening the picker is
// the only time this fetches anything, and that fetch (the allowed model
// list) is answered from the SERVER's own short cache, not re-issued here.

function toggleModelPicker(show) {
  const menu = dom['model-picker'];
  if (!menu) return;
  const wasOpen = !menu.hidden;
  menu.hidden = show === undefined ? !menu.hidden : !show;
  const btn = dom['ch-modelchip'];
  if (btn) btn.setAttribute('aria-expanded', String(!menu.hidden));
  if (!menu.hidden) {
    // A dialog that opens without taking focus is invisible to a keyboard.
    const first = menu.querySelector('select, button, input');
    if (first) first.focus({ preventScroll: true });
  } else if (wasOpen && menu.contains(document.activeElement)) {
    // Hand focus back to whichever control opened it (the chip when it is
    // showing, else the thread ⋯ button).
    const back = (btn && !btn.hidden) ? btn : dom['thread-menu-btn'];
    if (back) back.focus({ preventScroll: true });
  }
}

// Rebuilds the chip label + warning state from state alone — no request.
// Called from renderChatHeader() (thread open, thread_update) and from the
// WS 'error'/'thinking' handlers that flip state.modelChipWarn.
function renderModelChip() {
  const btn = dom['ch-modelchip'];
  if (!btn) return;
  const th = state.activeThread;
  const bot = th && (botById(th.bot_id) || botById(state.selectedBotId));
  if (!th || !bot || state.decoy) {
    btn.hidden = true;
    toggleModelPicker(false);
    return;
  }
  const prefs = th.prefs || {};
  const meter = meterText(latestContextBudget(state.messages));
  const parts = [prefs.model, prefs.thinking, meter].filter(Boolean);
  dom['ch-modelchip-label'].textContent = parts.join(' · ');
  const warn = state.modelChipWarn[th.id];
  // The chip only takes header room when it has something to SAY — an
  // override, a context reading or a refusal. Otherwise the picker is one
  // tap away in the ⋯ thread menu ("Model & thinking"), and a bare icon in
  // the header was just one more thing squeezing the bot's name.
  btn.hidden = !(parts.length || warn);
  btn.classList.toggle('warning', !!warn);
  btn.title = warn || t('chat.model_chip');
}

// Fills the two <select>s from the bot's allowed-model list + the thread's
// current prefs, and the warning box from any standing refusal. Runs every
// open — the model list is server-cached briefly (see main.py's
// _BOT_MODELS_CACHE), so this is cheap on the common "open it again" path.
async function openModelPicker() {
  const th = state.activeThread;
  const bot = th && (botById(th.bot_id) || botById(state.selectedBotId));
  if (!th || !bot) return;
  toggleThreadMenu(false);
  toggleModelPicker(true);

  const prefs = th.prefs || {};
  const modelSel = dom['mp-model'];
  const thinkingSel = dom['mp-thinking'];
  modelSel.disabled = true;
  modelSel.replaceChildren(el('option', { value: '', text: t('common.loading') }));

  // An API bot (Connect an AI) has no reasoning-effort concept — the OpenClaw
  // gateway is what "thinking" means, and a direct-provider bot never talks
  // to it. Hiding the row rather than disabling it: an option nothing will
  // ever read is worse than one that is not offered.
  const isApiBot = !!bot.api_provider;
  dom['mp-thinking-row'].hidden = isApiBot;

  thinkingSel.replaceChildren(
    el('option', { value: '', text: t('chat.thinking_default') }),
    ...THINKING_LEVELS.map((lvl) => el('option', { value: lvl, text: t(`chat.thinking_${lvl}`) })));
  thinkingSel.value = prefs.thinking || '';

  const warn = state.modelChipWarn[th.id];
  dom['mp-warning'].hidden = !warn;
  dom['mp-warning'].textContent = warn || '';

  const meter = meterText(latestContextBudget(state.messages));
  dom['mp-context'].hidden = !meter;
  dom['mp-context'].textContent = meter ? t('chat.model_picker_context', { used: meter }) : '';

  try {
    const { models } = await api.botModels(bot.id);
    // The thread may have navigated away (or the picker closed) while this
    // was in flight — a stale response must not repaint a picker for the
    // WRONG thread, or fill in a model that got applied to nobody sees.
    if (state.activeThreadId !== th.id || dom['model-picker'].hidden) return;
    const options = normalizeModelOptions(models);
    modelSel.replaceChildren(
      el('option', { value: '', text: t('chat.model_picker_bot_default') }),
      ...options.map((m) => el('option', { value: m.id, text: m.label })));
    // The saved model might not be in the (possibly since-narrowed) allowed
    // list — offer it anyway rather than silently dropping the operator's
    // choice back to blank, which would look like Apply had cleared it.
    if (prefs.model && !options.some((m) => m.id === prefs.model)) {
      modelSel.append(el('option', { value: prefs.model, text: prefs.model }));
    }
    modelSel.value = prefs.model || '';
  } catch (e) {
    if (state.activeThreadId !== th.id || dom['model-picker'].hidden) return;
    modelSel.replaceChildren(el('option', { value: prefs.model || '', text: prefs.model || t('chat.model_picker_bot_default') }));
    toast(e.message, true);
  } finally {
    if (state.activeThreadId === th.id) modelSel.disabled = false;
  }
}

async function applyModelPicker() {
  const th = state.activeThread;
  if (!th) return;
  const patch = prefsPatchFrom(dom['mp-model'].value, dom['mp-thinking'].value);
  try {
    await api.setThreadPrefs(th.id, patch);
    // Optimistic — the WS thread_update confirms and repaints the chip for
    // real, but the picker should not sit on stale values until it arrives.
    state.activeThread.prefs = { ...(state.activeThread.prefs || {}) };
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) delete state.activeThread.prefs[k];
      else state.activeThread.prefs[k] = v;
    }
    // Applying is how a refusal gets "corrected" — clear the warning now
    // rather than waiting for the next turn to prove the new choice works,
    // so the chip does not keep showing a sentence about the OLD choice.
    delete state.modelChipWarn[th.id];
    renderModelChip();
    toggleModelPicker(false);
  } catch (e) { toast(e.message, true); }
}

function wireModelChip() {
  dom['ch-modelchip'].addEventListener('click', (e) => {
    e.stopPropagation();
    if (dom['model-picker'].hidden) openModelPicker(); else toggleModelPicker(false);
  });
  // Clicks inside the panel (the <select>s especially) must not bubble to the
  // document-level "close any open menu" listener below — that listener is
  // what makes clicking elsewhere close it, and without this guard the SAME
  // click that opens a native <select> dropdown would also hide the panel
  // it belongs to.
  dom['model-picker'].addEventListener('click', (e) => e.stopPropagation());
  dom['mp-apply'].addEventListener('click', () => { applyModelPicker(); });
  dom['mp-reset'].addEventListener('click', () => {
    dom['mp-model'].value = '';
    dom['mp-thinking'].value = '';
    applyModelPicker();
  });
  document.addEventListener('click', () => toggleModelPicker(false));
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
  // A 📌 on each pinnable row puts that switch on the rail. It lives on the
  // row rather than in a list of its own so the thing you pin and the pin
  // control are the same object — nothing to keep in step.
  for (const [row, pinId] of [[pr, 'privacy'], [nr, 'nim']]) {
    const pin = pinToggle(pinId, { t, onChange: renderPins });
    if (pin) row.append(pin);
  }
  // Minimal avatars is pinnable too. Its row is static markup (unlike the two
  // built above), so the 📌 from the previous mount must go before a new one
  // lands — this function runs on every Device-tab open.
  const avRow = dom['bm-avatar-style-row'];
  if (avRow) {
    avRow.querySelector('.pin-toggle')?.remove();
    const avPin = pinToggle('avatars', { t, onChange: renderPins });
    if (avPin) avRow.append(avPin);
  }
  // The 🎨 rail button, as a row whose ONLY control is its 📌. It ships pinned
  // (pins.js `pinnedByDefault`), so unlike the rows above there is no switch to
  // pin — the pin IS the setting. A <div>, not a <label>: there is no checkbox
  // for a label to belong to. Unlocked only, matching the button itself, which
  // Safe Mode hides because the palette picker is a full-session surface.
  document.getElementById('theme-pin-row')?.remove();
  if (!state.decoy) {
    const tRow = document.createElement('div');
    tRow.id = 'theme-pin-row';
    tRow.className = 'bm-avatar-style';
    const txt = document.createElement('span');
    const strong = document.createElement('strong');
    strong.textContent = t ? t('nav.theme') : 'Theme';
    txt.append(strong, document.createTextNode(
      t ? ` — ${t('pins.theme_hint')}` : ' — keep the theme button on the bar.',
    ));
    tRow.append(txt);
    const tPin = pinToggle('theme', { t, onChange: renderPins });
    if (tPin) tRow.append(tPin);
    (avRow || nr).after(tRow);
  }
  // Custom link buttons: fourth device preference, same instant-apply rules —
  // add a link here and it is on the rail before the modal closes (renderPins
  // is the onChange). Unlocked sessions only: the editor is simply not built
  // in Safe Mode, matching the rail, so a locked device neither sees the
  // buttons nor the URLs behind them.
  const oldLinks = document.getElementById('links-row');
  if (oldLinks) oldLinks.remove();
  // Local viewer roots: fifth device preference, same rules, rebuilt on every
  // open so its contents reflect the CURRENT session and the current server
  // config rather than whatever was loaded the last time Settings was opened.
  const oldViewer = document.getElementById('viewer-row');
  if (oldViewer) oldViewer.remove();
  if (!state.decoy) {
    const lr = linksSection(t, { onChange: renderPins });
    nr.after(lr);
    lr.after(viewerSection());
  }
  // About: version + the source link. In the Device pane on purpose — it is the
  // one settings tab a Safe-Mode session can open, so every user of the running
  // program can see what it is, not just the operator.
  const oldAbout = document.getElementById('about-row');
  if (oldAbout) oldAbout.remove();
  const ab = aboutRow(t);
  ab.id = 'about-row';
  // LAST in the pane, under the action buttons: it is reference information,
  // not a control, and inserting it between two toggles reads as a setting.
  (dom['spane-device'] || nr.parentNode).append(ab);
}

/** Settings → Device: the local viewer's served roots.
 *
 *  Built here rather than in viewer.js because it is a settings control, not
 *  part of the overlay — viewer.js stays the module that shows files, and this
 *  is the module that decides which files it may show.
 *
 *  Unlocked sessions only. Not "disabled in Safe Mode": not built at all, like
 *  the links editor beside it. A locked device must not learn which folders of
 *  the host this app can serve, and the server 403s the routes anyway, so a
 *  built-but-empty section would only be a broken control that leaks a fact.
 *
 *  Every change saves immediately (PUT /api/local/config) — same instant-apply
 *  contract as the rest of the pane, nothing to press Save on.
 */
function viewerSection() {
  const wrap = el('div', { class: 'bm-links bm-viewer', id: 'viewer-row' });
  wrap.append(el('span', {}, [
    el('strong', { text: t('viewer.settings_title') }),
    ` — ${t('viewer.settings_hint')}`,
  ]));

  const list = el('div', { class: 'bm-links-list' });
  const status = el('p', { class: 'muted bm-viewer-status' });
  const err = el('p', { class: 'bm-links-error', role: 'alert', hidden: '' });
  const showErr = (msg) => { err.textContent = msg || ''; err.hidden = !msg; };

  // Last loaded server state. The pane is a view of it; a failed save leaves
  // this untouched so the next edit is not built on a value the server refused.
  let cfg = { roots: [], show_hidden: false, enabled: false };

  const hidden = el('input', { type: 'checkbox', id: 'viewer-show-hidden' });
  const path = el('input', {
    class: 'bm-links-in-url', type: 'text', id: 'viewer-root-input',
    placeholder: t('viewer.root_path'), 'aria-label': t('viewer.root_path'),
    autocomplete: 'off', spellcheck: 'false',
  });

  const paint = () => {
    list.innerHTML = '';
    for (const root of cfg.roots) {
      list.append(el('div', { class: 'bm-links-item' }, [
        el('span', { class: 'bm-links-glyph', 'aria-hidden': 'true' }, [railIcon(RAIL_ICONS['viewer-folder'])]),
        // The path is the whole row: no label to give it, and truncating it
        // would hide exactly the segment that decides what is served.
        el('span', { class: 'bm-links-url', text: root.path }),
        el('button', {
          type: 'button',
          class: 'bm-links-remove',
          title: t('viewer.remove_root'),
          'aria-label': `${t('viewer.remove_root')}: ${root.path}`,
          onclick: () => save({ roots: cfg.roots.filter((r) => r.path !== root.path).map((r) => r.path) }),
        }, [railIcon(RAIL_ICONS.close)]),
      ]));
    }
    hidden.checked = !!cfg.show_hidden;
    const n = cfg.roots.length;
    // Both `n` (the {n} placeholder) and `count` (which form i18n picks) —
    // pass only `n` and every locale falls back to its plural `other`.
    status.textContent = n ? t('viewer.roots_count', { n, count: n }) : t('viewer.roots_none');
  };

  /** Send a config change and adopt whatever the server says the state now is.
   *  `patch` carries only what changed; the rest is re-sent from `cfg`. */
  async function save(patch) {
    showErr('');
    const body = {
      roots: patch.roots !== undefined ? patch.roots : cfg.roots.map((r) => r.path),
      show_hidden: patch.show_hidden !== undefined ? patch.show_hidden : !!cfg.show_hidden,
    };
    try {
      cfg = await api.saveLocalConfig(body);
      paint();
      return true;
    } catch (e) {
      // 400 is the server's one specific verdict — a root that is not an
      // existing folder, or one the built-in deny list always refuses. It
      // deliberately does not say WHICH, so neither do we.
      const msg = e.status === 400 ? t('viewer.bad_root') : t('viewer.save_failed', { error: e.message });
      showErr(msg);
      toast(msg, true);
      paint();          // the checkbox must snap back to the saved value
      return false;
    }
  }

  hidden.addEventListener('change', () => save({ show_hidden: hidden.checked }));

  const addRoot = async () => {
    const value = path.value.trim();
    if (!value) { path.focus(); return; }
    const ok = await save({ roots: [...cfg.roots.map((r) => r.path), value] });
    if (ok) path.value = '';
  };
  path.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); addRoot(); } });

  wrap.append(
    list,
    el('div', { class: 'bm-links-addrow' }, [
      path,
      el('button', { type: 'button', class: 'btn-secondary bm-links-add', text: t('viewer.add_root'), onclick: addRoot }),
    ]),
    el('label', { class: 'bm-links-viewer-toggle bm-viewer-hidden' }, [
      hidden, el('span', { text: t('viewer.show_hidden') }),
    ]),
    status,
    err,
  );

  // Async load after the section is in the DOM: the pane must not wait on a
  // request to appear, and a failure here leaves the honest "off" state up
  // rather than an empty box.
  api.localRoots().then((r) => { cfg = r; paint(); }).catch((e) => {
    showErr(t('viewer.save_failed', { error: e.message }));
  });
  paint();
  return wrap;
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
  // #tl-avatar has exactly one painter, and it is not any of the above. Without
  // this the thread-list header kept whatever it was last painted with: turning
  // NIM OFF left the letter block sitting there until an unrelated repaint (a
  // bot select, a bots WS frame) happened to come along. Same omission this
  // function's own comment records for renderBots/renderThreadList.
  updateThreadListHeader();
  // Composer chips are painted once, when a file is attached — so a NIM flip
  // with something already queued left its thumbnail on screen. Same class of
  // omission as #tl-avatar above.
  renderAttachments();
  if (state.activeThreadId) renderMessages(false);
  // The Bot Manager is built once when the modal opens and has no tab-change
  // rebuild, so a NIM flip from the Device tab left avatars and "Change photo"
  // live on the Bots tab of the SAME modal. Rebuild it while it is open.
  if (!dom['botmanager-backdrop'].classList.contains('hidden')) renderBotManager();
  if (reactionManagerOpen()) repaintReactionManager();
  syncMinimalAvatarRow();
  syncLockNimRow();
  // The rail too: a pinned 🙈 changes state, and a pinned 👤 flips between
  // operable and "No-Image Mode controls this" when NIM claims the attribute.
  renderPins();
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
const SETTINGS_TABS = ['bots', 'reactions', 'health', 'ai', 'tools', 'theme', 'device', 'security'];
// The four that live behind the PIN (Tools joined 2026-09-25). `admin-only` in the markup is the class;
// this is the list applyAuthChrome() walks.
const ADMIN_TABS = ['reactions', 'health', 'ai', 'tools'];
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
    case 'tools':
      // tools.js owns the table, the draft and the Save (PUT /api/tools).
      await mountToolsSettings(pane);
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
  if ((bmDirty || toolsSettingsDirty()) && !dom['botmanager-backdrop'].classList.contains('hidden')) {
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
      title: t('settings.safe_title'),
      onclick: (e) => {
        e.stopPropagation();
        bmBots[idx].safe = !bmBots[idx].safe;
        safeBtn.classList.toggle('on', bmBots[idx].safe);
        bmDirty = true;
      },
    }, iconLabel(RAIL_ICONS.shield, t('settings.safe_badge')));
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
    // Per-bot reaction images. Off for every bot by default — a
    // reaction interrupts every screen in the house, so it's opt-in per bot.
    const rxBtn = el('button', {
      class: 'bm-safe-btn' + (bot.reactions ? ' on' : ''),
      title: t('settings.react_title'),
      onclick: (e) => {
        e.stopPropagation();
        bmBots[idx].reactions = !bmBots[idx].reactions;
        rxBtn.classList.toggle('on', bmBots[idx].reactions);
        bmDirty = true;
      },
    }, iconLabel(RAIL_ICONS.bolt, t('settings.react_badge')));
    // Per-bot avatar pool: new chats draw a one-shot face of their own. Also
    // opt-in — a pool only makes sense for a companion with a curated look.
    const apBtn = el('button', {
      class: 'bm-safe-btn' + (bot.avatar_pool ? ' on' : ''),
      title: t('settings.pool_title'),
      onclick: (e) => {
        e.stopPropagation();
        bmBots[idx].avatar_pool = !bmBots[idx].avatar_pool;
        apBtn.classList.toggle('on', bmBots[idx].avatar_pool);
        bmDirty = true;
      },
    }, iconLabel(RAIL_ICONS.images, t('settings.pool_badge')));
    // Placement badge. DEVICE-local, unlike its neighbours: it writes straight
    // to localStorage and repaints, rather than joining bmDirty and waiting for
    // Save, because there is nothing server-side to save. Off in Safe Mode —
    // the ⌥ menu does not exist there, so offering to move a bot into it would
    // promise something the tier cannot deliver.
    const mbBtn = state.decoy ? null : el('button', {
      class: 'bm-safe-btn' + (isMenuBot(bot.id) ? ' on' : ''),
      text: t('settings.menu_badge'),
      title: t('settings.menu_title'),
      onclick: (e) => {
        e.stopPropagation();
        mbBtn.classList.toggle('on', toggleMenuBot(bot.id));
        renderSidebar();
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
      mbBtn,
      // "Change photo" is omitted entirely in No-Image Mode — not disabled,
      // omitted. Offering to set a picture on a device that will not display
      // one is incoherent, and it is the last route into the crop dialog, so
      // dropping it keeps the whole photo flow unreachable rather than
      // half-reachable. el() ignores a null child, so this composes cleanly.
      // Icon-only: tapping the picture does the same thing, so a worded
      // button on every row was the loudest control for the rarest action.
      nimEnabled() ? null : el('button', {
        class: 'bm-avatar-btn bm-avatar-btn--icon', title: t('settings.change_photo_title'),
        'aria-label': t('settings.change_photo_short'),
        onclick: (e) => { e.stopPropagation(); pickAvatarFile(bot.id); },
      }, [railIcon(RAIL_ICONS.camera)]),
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
// A single generic file glyph in currentColor — replaced the per-extension
// colour-emoji map (📦📕📝🎵💻💿🤖⚙️📄, once kept in sync with markdown.js's
// old _DOC_ICONS) on 2026-09-18. See markdown.js's expandDocDirectives for
// the fuller reasoning: the filename next to it already says the type.
function fileIcon() { return railIcon(RAIL_ICONS['viewer-file']); }

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
      el('div', { class: 'empty-emoji' }, [railIcon(RAIL_ICONS.folder)]),
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
      title: t('files.wipe_title'),
      onclick: async () => {
        if (!await uiConfirm(t('files.wipe_confirm', { count: olderCount, day: g.label }), { danger: true })) return;
        try {
          const r = await api.wipeFiles(cutoff);
          toast(t('files.deleted', { count: r.deleted }));
          renderFileServer();
        } catch (e) { toast(e.message, true); }
      },
    }, iconLabel(RAIL_ICONS.trash, t('files.wipe_button', { count: olderCount })));
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
        thumb = el('div', { class: 'fs-thumb fs-thumb-icon' }, [fileIcon()]);
      }

      const actions = [];
      if (canPreview && !isImage && !isVideo) {
        // Text/doc preview opens the raw endpoint in a new tab.
        actions.push(el('a', { class: 'fs-act-btn', title: t('files.view'), href: rawUrl, target: '_blank', rel: 'noopener' }, [railIcon(RAIL_ICONS.eye)]));
      }
      actions.push(el('a', { class: 'fs-act-btn', title: t('files.download'), href: dlUrl, download: f.name }, [railIcon(RAIL_ICONS['viewer-download'])]));
      actions.push(el('button', {
        class: 'fs-del-btn', title: t('files.delete'),
        onclick: async () => {
          try { await api.deleteFile(f.id); renderFileServer(); }
          catch (e) { toast(e.message, true); }
        },
      }, [railIcon(RAIL_ICONS.trash)]));

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
    el('div', { class: 'drop-icon' }, [fileIcon()]),
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
      row.querySelector('.drop-icon').replaceChildren(railIcon(RAIL_ICONS.check));
      detail.textContent = t(res && res.notice === false ? 'drop.sent_no_thread' : 'drop.sent',
                             { size: fileSize(file.size) });
    },
    fail(msg) {
      row.classList.add('failed');
      row.querySelector('.drop-icon').replaceChildren(railIcon(RAIL_ICONS.alert));
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

// ---- Operator tools popover (DeepSeek Harness / StudioForge) ----
// Click-driven, not hover. This app is used on family Android tablets and as an
// installed PWA, where a hover-only menu is unreachable; desktop hover is added
// on top in wireToolsMenu() for pointers that actually have it.
let toolsPinned = false;   // opened by a click, not by hover
/** Click behaviour, reconciled with hover-open.
 *
 *  On a desktop the pointer opens this menu on hover, so a plain toggle made the
 *  button look dead: hover opened it, the click closed it again. A click now
 *  PINS an already-open menu instead of closing it, and only a second click (or
 *  an outside click, or Escape) dismisses it. On touch there is no hover, so
 *  this is an ordinary open/close toggle.
 */
function toggleToolsMenu() {
  const menu = dom['tools-menu'];
  if (!menu) return;
  if (menu.hasAttribute('hidden')) { toolsPinned = true; openToolsMenu(); }
  else if (!toolsPinned) { toolsPinned = true; }
  else closeToolsMenu();
}
function openToolsMenu() {
  const menu = dom['tools-menu'];
  const btn = dom['tools-btn'];
  // Never open a menu with nothing in it, and never in Safe Mode.
  const rows = dom['tools-bots'] ? dom['tools-bots'].childElementCount : 0;
  if (!menu || !btn || state.decoy || rows === 0) return;
  menu.removeAttribute('hidden');
  btn.setAttribute('aria-expanded', 'true');
  // Fixed positioning so the pop-up escapes the rail's overflow clip. The rail
  // is on the inline-start edge, so open to its side and clamp to the viewport.
  const r = btn.getBoundingClientRect();
  const mw = menu.offsetWidth || 232;
  const mh = menu.offsetHeight || 160;
  const rtl = document.documentElement.dir === 'rtl';
  const beside = rtl ? r.left - mw - 6 : r.right + 6;
  const fits = rtl ? beside >= 8 : beside + mw <= window.innerWidth - 8;
  menu.style.left = Math.max(8, Math.min(
    fits ? beside : Math.min(r.left, window.innerWidth - mw - 8),
    window.innerWidth - mw - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(r.top, window.innerHeight - mh - 8)) + 'px';
  // Capture phase, added on open and removed on close. The sibling popovers
  // use a deferred { once: true } listener; that re-arms itself by hand on
  // every inside click and silently stops closing the menu if any handler
  // between the target and document stops propagation. A plain capture
  // listener sees the click before anything can swallow it.
  setTimeout(() => document.addEventListener('click', _toolsOutside, true), 0);
}
function closeToolsMenu() {
  const menu = dom['tools-menu'];
  toolsPinned = false;
  document.removeEventListener('click', _toolsOutside, true);
  if (menu) menu.setAttribute('hidden', '');
  const btn = dom['tools-btn'];
  if (btn) btn.setAttribute('aria-expanded', 'false');
}
function _toolsOutside(e) {
  const btn = dom['tools-btn'];
  const menu = dom['tools-menu'];
  // position:fixed means the menu is not inside the button's hit-box — check both.
  // A click on either is handled by their own listeners; only a click elsewhere
  // dismisses. No re-arming: the listener stays until closeToolsMenu removes it.
  if ((btn && btn.contains(e.target)) || (menu && menu.contains(e.target))) return;
  closeToolsMenu();
}
function wireToolsMenu() {
  // Escape closes and returns focus to the button, like every other popover here.
  const menu = dom['tools-menu'];
  if (menu) {
    menu.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      closeToolsMenu();
      if (dom['tools-btn']) dom['tools-btn'].focus();
    });
  }
  // Desktop convenience only: open on hover where a real pointer exists, and
  // close when the cursor leaves both the button and the menu. Guarded so touch
  // devices never get a menu that opens on an accidental tap-and-hold.
  if (!window.matchMedia || !window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
  let leaveTimer = null;
  const cancelLeave = () => { if (leaveTimer) { clearTimeout(leaveTimer); leaveTimer = null; } };
  const scheduleLeave = () => {
    cancelLeave();
    // Short grace period so the diagonal trip from button to menu doesn't close it.
    leaveTimer = setTimeout(() => { if (!toolsPinned) closeToolsMenu(); }, 220);
  };
  document.addEventListener('mouseover', (e) => {
    const btn = dom['tools-btn'];
    if (btn && btn.contains(e.target)) { cancelLeave(); openToolsMenu(); }
  });
  document.addEventListener('mouseout', (e) => {
    const btn = dom['tools-btn'];
    const m = dom['tools-menu'];
    const to = e.relatedTarget;
    const inside = (n) => n && ((btn && btn.contains(n)) || (m && m.contains(n)));
    if (inside(e.target) && !inside(to)) scheduleLeave();
  });
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

// ===================== DeepSeek Harness (dsh) =====================
// dsh ships no TUI, so this pane is not a PTY: it embeds the
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
  if (st.sessions) state.harness.sessions = st.sessions;
  renderHarness();
}

function applyHarnessFrame(data) {
  if (data.jobs) state.harness.jobs = data.jobs;
  if (data.sessions) state.harness.sessions = data.sessions;
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

/** Close whichever tool pane is open, whoever is about to open one.
 *
 *  The four panes are mutually exclusive — they share the same slot and the
 *  same z-index, so two painted at once means the later sibling wins and the
 *  other is invisible but still "open": its flag stays true, its polling keeps
 *  running, and state.selectedBotId names a pane you cannot see. Harness and
 *  StudioForge each closed only ONE of the other three, so Clients -> Harness
 *  and Mail -> Harness both stacked.
 *
 *  `except` keeps an opener from closing itself when it is already open, which
 *  is the re-entrant case (selectBot on the pane you are looking at).
 */
function closeToolPanes(except) {
  if (except !== 'tool') closeTool({ restore: false });
  if (harnessOpen && except !== 'harness') closeHarnessView();
  if (studioforgeOpen && except !== 'studioforge') closeStudioForgeView();
  if (mailOpen && except !== 'mail') closeMailView();
  if (clientsOpen && except !== 'clients') closeClientsView();
}

function openHarnessView() {
  if (state.decoy || !state.harnessEnabled) return;
  closeToolPanes('harness');
  rememberPrev();   // before the selection changes: ✕ in a later tool lands here
  state.selectedBotId = HARNESS_ID;
  harnessOpen = true;
  renderSidebar();
  renderHarnessSessionPanel();
  dom['harness-view'].classList.remove('hidden');
  document.body.classList.add('tool-full');
  if (isMobile()) navigate('chat');
  renderHarness();
  // Fresh facts on open (the WS only carries deltas).
  api.harnessStatus().then(applyHarnessStatus).catch(() => {});
}

function closeHarnessView() {
  harnessOpen = false;
  document.body.classList.remove('tool-full');
  dom['harness-view'].classList.add('hidden');
  // Unload the frame: a hidden iframe keeps dsh's websocket + HMR stream alive.
  if (dom['harness-frame']) { dom['harness-frame'].classList.add('hidden'); dom['harness-frame'].removeAttribute('src'); }
  if (harnessTick) { clearInterval(harnessTick); harnessTick = null; }
  stopHarnessSessionPoll();
}

function setHarnessTab(tab) {
  harnessTab = (tab === 'jobs' || tab === 'sessions') ? tab : 'ui';
  const tabs = [['ui', 'harness-tab-ui', 'harness-pane-ui'],
                ['jobs', 'harness-tab-jobs', 'harness-pane-jobs'],
                ['sessions', 'harness-tab-sessions', 'harness-pane-sessions']];
  for (const [name, tabId, paneId] of tabs) {
    if (!dom[tabId] || !dom[paneId]) continue;
    const on = harnessTab === name;
    dom[tabId].classList.toggle('active', on);
    dom[tabId].setAttribute('aria-selected', on ? 'true' : 'false');
    dom[paneId].classList.toggle('hidden', !on);
  }
  if (harnessTab !== 'sessions') stopHarnessSessionPoll();
  renderHarness();
  if (harnessTab === 'jobs' && dom['harness-task']) dom['harness-task'].focus();
  if (harnessTab === 'sessions' && dom['harness-session-task']) dom['harness-session-task'].focus();
}

// Paints everything from state: bar (dot/label/buttons/model select), the
// UI pane (frame vs. note) and the jobs pane. Cheap; called on every change.
function renderHarness() {
  const st = state.harness.status;
  const svc = harnessServiceState();
  // Sidebar dot.
  const sd = railToolDot(HARNESS_ID);
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
  renderHarnessSessions();
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
  let hasStudioForge = false;   // scrub-ok: public project, article live on laserlloyd.com
  for (const pr of models.providers) {
    if (!pr || !Array.isArray(pr.models) || !pr.models.length) continue;
    // The house LLM server gets a ★ — and it unhides the docs link below.
    const sf = /studioforge/i.test(`${pr.name || ''} ${pr.id || ''}`);   // scrub-ok: public project name
    if (sf) hasStudioForge = true;
    const grp = el('optgroup', { label: `${sf ? '★ ' : ''}${pr.name || pr.id}` });
    for (const m of pr.models) {
      const key = `${pr.id} ${m.id}`;
      // Live state from the server's /models decoration (StudioForge / LM
      // Studio dialect): ● resident now, ◌ mid-load. No badge = cold, and
      // picking it is legitimate — the first request just pays the load.
      const badge = m.state === 'loaded' ? ' ●' : m.state === 'loading' ? ' ◌' : '';
      const label = m.name && m.name !== m.id ? m.name : m.id;
      const opt = el('option', { value: key, text: `${label}${badge}` });
      opt.title = `${pr.id} / ${m.id}`
        + (m.state === 'loaded' ? ` — ${t('harness.model_loaded')}${m.ctx ? ` (ctx ${m.ctx.toLocaleString()})` : ''}`
          : m.state === 'loading' ? ` — ${t('harness.model_loading')}` : '');
      if (key === curKey) { opt.selected = true; matched = true; }
      grp.append(opt);
    }
    sel.append(grp);
  }
  if (dom['harness-sf-link']) dom['harness-sf-link'].hidden = !hasStudioForge;
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
      // noMedia like every other renderMarkdown call site (messageEl, the sub
      // bubble, the streaming painter). This one was missed, so a dsh answer
      // containing an image built a real <img> and fetched it on a No-Image /
      // Safe-Mode device — the one surface where the source-strip chokepoint
      // was bypassed.
      if (out) { outEl.innerHTML = renderMarkdown(out, { noMedia: mediaHidden(), noLocal: state.decoy }); enhanceContent(outEl, { noLocal: state.decoy }); }
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

// ===================== DeepSeek Harness — live sessions =====================
// The Jobs tab runs ONE task and only shows the answer once it has ended. This
// tab runs several at once and shows them WHILE they work: dsh writes its
// session log incrementally and the server projects that into a small event
// stream (tool calls, tool results, assistant text, step boundaries), which
// this pane polls for whichever session you have open. Polling with `after=<n>`
// ships only what is new, so an open session costs a few hundred bytes a second.
//
// Stopping is the only correction dsh offers — there is no resume and no
// mid-flight steering — so Stop kills the process and the row leaves the list.
// A session that finishes on its own stays until Clear, so the last thing it
// said is still readable.
let harnessSessionId = null;      // the session whose live log is expanded
let harnessSessionEvents = [];
let harnessSessionNext = 0;
let harnessSessionTimer = null;   // in-flight poll (setTimeout chain)
let harnessSessionTick = null;    // 1s elapsed-time repaint while any is live
let harnessSessionBusy = false;   // a launch is in flight

function stopHarnessSessionPoll() {
  if (harnessSessionTimer) { clearTimeout(harnessSessionTimer); harnessSessionTimer = null; }
  if (harnessSessionTick) { clearInterval(harnessSessionTick); harnessSessionTick = null; }
  // Clearing the timers is not enough: a request already in flight re-arms
  // from its own callback, and the only thing those callbacks check is whether
  // harnessSessionId still matches. Nulling it is what makes the stop stick.
  harnessSessionId = null;
}

// State labels reuse the service-state and job keys — 'running' and 'finished'
// already have translated strings, and inventing a parallel set would be eight
// more keys per language for no new meaning.
function harnessSessionLabel(s) {
  const secs = s.duration_s != null ? Math.round(s.duration_s) : 0;
  switch (s.state) {
    case 'starting': return t('harness.state_starting');
    case 'running': return t('harness.job_running', { secs });
    case 'stopping': return t('harness.state_stopped');
    case 'exited': return t('harness.job_done', { secs });
    case 'failed': return t('harness.job_failed', { code: s.exit_code, secs });
    case 'stopped': return t('harness.state_stopped');
    default: return t('harness.state_unknown');
  }
}

// The optional per-session model. Empty means "whatever the Model select says".
// The server applies a chosen one through a per-session scratch DSH_HOME, so it
// never rewrites the shared settings.yaml.
function renderHarnessSessionModelSelect() {
  const sel = dom['harness-session-model'];
  if (!sel) return;
  const keep = sel.value;
  const models = state.harness.status && state.harness.status.models;
  sel.innerHTML = '';
  sel.append(el('option', { value: '', text: t('harness.session_model_default') }));
  if (models && Array.isArray(models.providers)) {
    for (const pr of models.providers) {
      if (!pr || !Array.isArray(pr.models) || !pr.models.length) continue;
      const grp = el('optgroup', { label: pr.name || pr.id });
      for (const m of pr.models) {
        grp.append(el('option', {
          value: `${pr.id}/${m.id}`,
          text: (m.name && m.name !== m.id) ? m.name : m.id,
        }));
      }
      sel.append(grp);
    }
  }
  if (keep) sel.value = keep;
}

function sessionEventEl(ev) {
  switch (ev.k) {
    case 'tool':
      return el('div', { class: 'hs-ev hs-tool' }, [
        el('span', { class: 'hs-ev-tag' }, [railIcon(RAIL_ICONS.tools), document.createTextNode(` ${ev.name}`)]),
        el('span', { class: 'hs-ev-text', text: ev.args || '' }),
      ]);
    case 'result':
      return el('div', { class: 'hs-ev ' + (ev.ok ? 'hs-ok' : 'hs-err') }, [
        el('span', { class: 'hs-ev-tag', text: ev.ok ? '↳ ok' : '↳ error' }),
        el('span', { class: 'hs-ev-text', text: ev.text || '' }),
      ]);
    case 'say':
      return el('div', { class: 'hs-ev hs-say', text: ev.text });
    case 'step':
      return el('div', { class: 'hs-ev hs-step', text: `▸ step ${ev.step}` });
    case 'end':
      return el('div', { class: 'hs-ev hs-end', text: `— ${ev.reason}` });
    case 'model':
      return el('div', { class: 'hs-ev hs-model', text: `${ev.provider} / ${ev.model}` });
    default:
      return null;      // title/user are already on the card
  }
}

function harnessSessionCard(s) {
  const live = s.state === 'running' || s.state === 'starting';
  const dotCls = live ? 'harness-running'
    : s.state === 'failed' ? 'harness-failed' : 'stopped';
  const card = el('div', { class: 'harness-job harness-session harness-session-' + s.state });
  card.append(el('div', { class: 'harness-job-head' }, [
    el('span', { class: 'terminal-dot ' + dotCls }),
    el('span', { class: 'harness-job-state', text: harnessSessionLabel(s) }),
    el('span', { class: 'harness-job-cwd', text: t('harness.job_cwd', { cwd: shortHome(s.cwd) }) }),
    el('span', { class: 'harness-job-when', text: fmtHarnessWhen(s.started_at) }),
  ]));
  card.append(el('div', { class: 'harness-job-task', text: s.title || s.task }));
  const actions = el('div', { class: 'harness-session-actions' });
  if (s.active) {
    const stop = el('button', { class: 'btn-secondary danger-text', type: 'button', text: t('harness.stop') });
    stop.addEventListener('click', () => harnessStopSession(s.id));
    actions.append(stop);
  } else {
    const clear = el('button', { class: 'btn-secondary', type: 'button', text: t('common.close') });
    clear.addEventListener('click', () => harnessClearSession(s.id));
    actions.append(clear);
  }
  const open = el('button', { class: 'btn-secondary', type: 'button', text: t('harness.session_open') });
  open.addEventListener('click', () => harnessOpenSession(s.id));
  actions.append(open);
  card.append(actions);
  if (harnessSessionId === s.id) {
    const log = el('div', { class: 'harness-job-out harness-session-log' });
    if (!harnessSessionEvents.length) {
      log.append(el('div', { class: 'muted', text: t('harness.session_waiting') }));
    } else {
      for (const ev of harnessSessionEvents) {
        const row = sessionEventEl(ev);
        if (row) log.append(row);
      }
    }
    card.append(log);
  }
  return card;
}

function renderHarnessSessions() {
  const wrap = dom['harness-sessions'];
  if (!wrap || harnessTab !== 'sessions') return;
  renderHarnessSessionModelSelect();
  const box = state.harness.sessions || { sessions: [], running: 0, limit: 4 };
  const list = box.sessions || [];
  const full = box.running >= box.limit;
  if (dom['harness-session-run']) dom['harness-session-run'].disabled = harnessSessionBusy || full;
  for (const id of ['harness-session-task', 'harness-session-cwd', 'harness-session-model']) {
    if (dom[id]) dom[id].disabled = harnessSessionBusy;
  }
  // Elapsed seconds tick while anything is live; the timer also dies with the
  // pane so a closed tab is not polling forever.
  const anyLive = list.some((s) => s.active);
  if (anyLive && !harnessSessionTick) {
    harnessSessionTick = setInterval(() => {
      if (harnessOpen && harnessTab === 'sessions') renderHarnessSessions();
      else stopHarnessSessionPoll();
    }, 1000);
  }
  if (!anyLive && harnessSessionTick) { clearInterval(harnessSessionTick); harnessSessionTick = null; }
  wrap.innerHTML = '';
  if (!list.length) {
    wrap.append(el('div', { class: 'empty-list' }, [
      el('p', { class: 'muted', text: t('harness.session_empty') }),
    ]));
    return;
  }
  for (const s of list) wrap.append(harnessSessionCard(s));
}

function harnessOpenSession(id) {
  if (harnessSessionId === id) {          // clicking Open again collapses it
    harnessSessionId = null;
    stopHarnessSessionPoll();
    renderHarnessSessions();
    return;
  }
  harnessSessionId = id;
  harnessSessionEvents = [];
  harnessSessionNext = 0;
  stopHarnessSessionPoll();
  renderHarnessSessions();
  harnessSessionPoll();
}

function harnessSessionPoll() {
  const id = harnessSessionId;
  if (!id) return;
  api.harnessSession(id, harnessSessionNext).then((d) => {
    if (harnessSessionId !== id) return;
    if (!harnessOpen) return;          // pane closed while this was in flight
    if (Array.isArray(d.events) && d.events.length) {
      harnessSessionEvents = harnessSessionEvents.concat(d.events).slice(-500);
      harnessSessionNext = d.next || harnessSessionNext;
    }
    renderHarnessSessions();
    harnessSessionTimer = d.active ? setTimeout(harnessSessionPoll, 1000) : null;
  }).catch((e) => {
    if (harnessSessionId !== id) return;
    if (!harnessOpen) return;          // pane closed while this was in flight
    // 403 alongside 404: a drop to Safe Mode makes every harness route refuse,
    // and retrying it every 2s for the rest of the session — from a device
    // somebody has just locked — is exactly the loop the Clients poll had.
    // Both are terminal for this session, and both are worth SHOWING rather
    // than leaving the card spinning on a request that will never succeed.
    if (e && (e.status === 404 || e.status === 403)) {
      harnessSessionId = null;
      harnessSessionTimer = null;
      renderHarnessSessions();
      return;
    }
    harnessSessionTimer = setTimeout(harnessSessionPoll, 2000);
  });
}

async function harnessLaunchSession(ev) {
  if (ev) ev.preventDefault();
  if (harnessSessionBusy) return;
  const task = (dom['harness-session-task'].value || '').trim();
  if (!task) { dom['harness-session-task'].focus(); return; }
  const cwd = (dom['harness-session-cwd'].value || '').trim();
  const model = (dom['harness-session-model'].value || '').trim();
  harnessSessionBusy = true;
  renderHarnessSessions();
  try {
    const s = await api.harnessSessionLaunch(task, cwd || null, model || null);
    dom['harness-session-task'].value = '';
    harnessSessionBusy = false;
    toast(t('harness.session_launched'));
    if (s && s.id) harnessOpenSession(s.id);
  } catch (e) {
    harnessSessionBusy = false;
    toast(e.status === 409 ? t('harness.session_busy') : cleanErr(e), true);
  }
  refreshHarnessSessions();
  renderHarnessSessions();
}

// The server broadcasts on every launch/stop/finish, but a socket mid-reconnect
// would miss it — so an action always pulls the list once too.
function refreshHarnessSessions() {
  api.harnessSessions().then((d) => {
    state.harness.sessions = d;
    renderHarness();
  }).catch(() => {});
}

async function harnessStopSession(id) {
  if (harnessSessionBusy) return;
  harnessSessionBusy = true;
  renderHarnessSessions();
  try {
    await api.harnessSessionStop(id);
    if (harnessSessionId === id) { harnessSessionId = null; stopHarnessSessionPoll(); }
    toast(t('harness.session_stopped'));
  } catch (e) {
    toast(cleanErr(e), true);
  }
  harnessSessionBusy = false;
  refreshHarnessSessions();
  renderHarnessSessions();
}

async function harnessClearSession(id) {
  try {
    await api.harnessSessionDismiss(id);
    if (harnessSessionId === id) { harnessSessionId = null; stopHarnessSessionPoll(); }
    toast(t('harness.session_cleared'));
  } catch (e) {
    toast(cleanErr(e), true);
  }
  refreshHarnessSessions();
  renderHarnessSessions();
}

function wireHarnessView() {
  if (!dom['harness-view']) return;
  dom['harness-back'].addEventListener('click', () => navigate('bots'));
  dom['harness-tab-ui'].addEventListener('click', () => setHarnessTab('ui'));
  dom['harness-tab-jobs'].addEventListener('click', () => setHarnessTab('jobs'));
  dom['harness-tab-sessions'].addEventListener('click', () => setHarnessTab('sessions'));
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
  dom['harness-session-form'].addEventListener('submit', harnessLaunchSession);
  dom['harness-session-task'].addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); harnessLaunchSession(); }
  });
}

// ===================== StudioForge control panel =====================
// The LLM rig's own web UI, embedded. DisPatch manages nothing here: the rig is
// a separate machine, so there is no service control, no jobs pane and no
// credential — a frame, a link and two reachability facts.
//
// There are TWO reachability questions and they have different answers. The
// server's (/api/studioforge/status) says the panel is up at all. The client's
// says whether THIS device can get to it, which depends on the device being on
// the rig's network — a phone on mobile data cannot, while the host can. Only
// the client's answer decides whether the frame is worth loading, and it cannot
// be guessed from the hostname. It also cannot be read off the iframe: `onload`
// fires for the browser's own error page too, and the document is cross-origin
// and unreadable. So we probe with fetch() first and only then set src.
//
// Deliberately NOT built: a reverse proxy or a socket relay. Framed, the viewer
// must already be able to reach the rig; proxied, every device on the LAN port
// and everything behind the Tailscale-Serve front door would inherit admin on a
// panel that has no password of its own.
let studioforgeOpen = false;
let studioforgeProbing = false;

/** Would the browser refuse this URL purely because of how DisPatch itself was
 *  loaded? An HTTPS document may not embed — or even fetch — an http:// origin.
 *
 *  This is THE failure mode over Tailscale and it used to be invisible.
 *  Tailscale Serve fronts DisPatch over HTTPS on a tailnet name while the rig is
 *  configured as a bare http:// address, so the iframe is blocked as mixed
 *  content and the pane drew an empty rectangle. Reached over plain http on
 *  the LAN the very same build works, which is exactly why it reads as
 *  "typically does not load over Tailscale".
 *
 *  Checked BEFORE the network probe because no probe can see it: the browser
 *  blocks mixed content at the fetch layer and a no-cors fetch still resolves
 *  (see probeStudioForgeFromClient), so the request "succeeds" having fetched
 *  nothing. */
function studioforgeInsecure(url) {
  return isMixedContent(window.location.href, url);
}

function studioforgeState() {
  const st = state.studioforge.status;
  if (!st) return 'unknown';
  // Ahead of `checking`: a scheme mismatch is a certainty, not a measurement,
  // so there is nothing to wait for and no point spinning.
  if (st.url && studioforgeInsecure(st.url)) return 'insecure';
  if (studioforgeProbing) return 'checking';
  if (st.reachable === false) return 'down';
  if (state.studioforge.clientReachable === false) return 'unreachable';
  // Reachable, but it will not be framed. Distinct from 'up' because it is a
  // permanent property of the panel, not a network condition — and it is the
  // state the panel has ALWAYS been in (StudioForge sends X-Frame-Options:
  // DENY), which is why the embedded pane only ever drew an empty rectangle.
  if (st.framable === false) return 'blocked';
  return 'up';
}

async function refreshStudioForgeFeature() {
  if (state.decoy) { state.studioforgeEnabled = false; return; }
  if (state.auth.features && state.auth.features.studioforge === false) {
    state.studioforgeEnabled = false; return;
  }
  try {
    const st = await api.studioforgeStatus();
    state.studioforgeEnabled = true;
    applyStudioForgeStatus(st);
  } catch (e) {
    // 404 = the feature is off or has no address configured; 403 = Safe Mode or
    // no PIN. Either way there is nothing to show and no reason to retry.
    if (e.status === 404 || e.status === 403) state.studioforgeEnabled = false;
  }
  renderSidebar();
}

function applyStudioForgeStatus(st) {
  if (!st || typeof st !== 'object') return;
  state.studioforge.status = st;
  renderStudioForge();
}

function renderStudioForgeSessionPanel() {
  dom['tl-botname'].textContent = t('studioforge.name');
  dom['tl-model'].textContent = t('studioforge.session_panel_title');
  const wrap = dom['threads'];
  wrap.innerHTML = '';
  wrap.append(el('div', { class: 'empty-list terminal-session-note' }, [
    el('div', { class: 'empty-emoji', text: 'SF' }),
    el('p', { text: t('studioforge.session_panel_heading') }),
    el('p', { class: 'muted', text: t('studioforge.session_panel_body') }),
  ]));
}

function openStudioForgeView() {
  if (state.decoy || !state.studioforgeEnabled) return;
  closeToolPanes('studioforge');
  rememberPrev();   // before the selection changes: ✕ in a later tool lands here
  state.selectedBotId = STUDIOFORGE_ID;
  studioforgeOpen = true;
  renderSidebar();
  renderStudioForgeSessionPanel();
  dom['studioforge-view'].classList.remove('hidden');
  document.body.classList.add('tool-full');
  if (isMobile()) navigate('chat');
  renderStudioForge();
  api.studioforgeStatus().then((st) => {
    applyStudioForgeStatus(st);
    probeStudioForgeFromClient();
  }).catch(() => {});
}

function closeStudioForgeView() {
  studioforgeOpen = false;
  document.body.classList.remove('tool-full');
  dom['studioforge-view'].classList.add('hidden');
  // Unload the frame: the panel holds a live socket.io telemetry stream, and a
  // hidden iframe would keep it (and the rig's per-view session) alive.
  const f = dom['studioforge-frame'];
  if (f) { f.classList.add('hidden'); f.removeAttribute('src'); }
}

// Can THIS browser reach the panel? A no-cors GET tells us nothing about the
// response body — that is fine, we only want "did the request go out, or did
// the network refuse it". Short timeout so a black-holed address does not
// leave the pane spinning.
//
// KNOWN LIMIT, and the reason for the guard below: a no-cors fetch yields an
// OPAQUE response, and an opaque response RESOLVES in cases where nothing was
// actually retrieved. Measured on this build: from an https:// origin, a
// no-cors fetch of the http:// rig resolves while DevTools logs the request as
// blocked mixed content (net::ERR_ABORTED). Trusting it set clientReachable =
// true, every note branch was skipped, and the blocked iframe painted an empty
// box. So the scheme case is decided before we get here and the probe is not
// run at all; what remains is a genuine network question.
async function probeStudioForgeFromClient() {
  const st = state.studioforge.status;
  const url = st && st.url;
  if (!url) return;
  // Nothing to measure: the browser will refuse this URL whatever the network
  // says, and a probe would answer "fine" (see above).
  if (studioforgeInsecure(url)) { state.studioforge.clientReachable = null; renderStudioForge(); return; }
  studioforgeProbing = true;
  renderStudioForge();
  let ok = false;
  try {
    await fetch(url, { mode: 'no-cors', cache: 'no-store', signal: AbortSignal.timeout(5000) });
    ok = true;
  } catch (e) {
    ok = false;
  }
  studioforgeProbing = false;
  // The user may have left the pane while the probe was in flight.
  state.studioforge.clientReachable = ok;
  renderStudioForge();
}

function renderStudioForge() {
  const st = state.studioforge.status;
  const sv = studioforgeState();
  const sd = railToolDot(STUDIOFORGE_ID);
  if (sd) sd.className = 'bot-status-dot terminal-sidedot studioforge-sidedot studioforge-' + sv;
  if (!studioforgeOpen) return;
  const url = (st && st.url) || '';
  if (dom['studioforge-dot']) dom['studioforge-dot'].className = 'terminal-dot studioforge-' + sv;
  if (dom['studioforge-status-label']) dom['studioforge-status-label'].textContent = t('studioforge.state_' + sv);
  // The address is shown, not hidden: it is the one fact an operator needs when
  // the frame will not load, and this pane is unlocked-only anyway.
  if (dom['studioforge-url']) dom['studioforge-url'].textContent = url;
  if (dom['studioforge-open']) {
    if (url) dom['studioforge-open'].href = url; else dom['studioforge-open'].removeAttribute('href');
    dom['studioforge-open'].classList.toggle('hidden', !url);
  }
  const frame = dom['studioforge-frame'], note = dom['studioforge-note'];
  if (!frame || !note) return;
  let noteKey = null;
  if (!url) noteKey = 'unconfigured';
  // First, because it is the only one that is certain rather than measured,
  // and because every other branch below would misdiagnose it: the server can
  // reach the rig (it is on the same tailnet over plain http), the client
  // probe resolves, `framable` may well be true — and the frame is still
  // refused, by this page's own origin.
  else if (sv === 'insecure') noteKey = 'insecure';
  else if (sv === 'checking') noteKey = 'checking';
  // Server first: if DisPatch's own host cannot reach the panel either, the rig
  // is down — a stronger and more useful signal than "this device can't". The
  // client-network answer is only the right one when the server CAN reach it.
  else if (st && st.reachable === false) noteKey = 'down';
  else if (state.studioforge.clientReachable === false) noteKey = 'unreachable';
  // Last, because it is the least urgent of the four: the panel is up and this
  // device can reach it — only the frame is refused, and the ↗ link above works.
  else if (st && st.framable === false) noteKey = 'blocked';
  if (noteKey) {
    if (!frame.classList.contains('hidden')) { frame.classList.add('hidden'); frame.removeAttribute('src'); }
    dom['studioforge-note-text'].textContent = t(`studioforge.${noteKey}_text`);
    dom['studioforge-note-hint'].textContent = t(`studioforge.${noteKey}_hint`);
    note.classList.remove('hidden');
  } else {
    note.classList.add('hidden');
    // sandbox/referrerpolicy are already on the element in the markup — set
    // BEFORE src, which is the whole point of not writing src there too.
    if (frame.getAttribute('src') !== url) frame.setAttribute('src', url);
    frame.classList.remove('hidden');
  }
}

function wireStudioForgeView() {
  if (!dom['studioforge-view']) return;
  dom['studioforge-back'].addEventListener('click', () => navigate('bots'));
}

// ===================== Emails tab (MailForge dashboard) =====================
// See backend/app/mailforge_bridge.py for why this is an iframe pointed at
// MailForge's own launch URL rather than a same-origin reverse proxy.
let mailOpen = false;

async function refreshMailFeature() {
  if (state.decoy) { state.mailEnabled = false; return; }
  if (state.auth.features && state.auth.features.mail === false) {
    state.mailEnabled = false; return;
  }
  try {
    await api.mailStatus();
    state.mailEnabled = true;
  } catch (e) {
    if (e.status === 404 || e.status === 403) state.mailEnabled = false;
  }
  renderSidebar();
}

function renderMailSessionPanel() {
  dom['tl-botname'].textContent = t('mail.name');
  dom['tl-model'].textContent = t('mail.session_panel_title');
  const wrap = dom['threads'];
  wrap.innerHTML = '';
  wrap.append(el('div', { class: 'empty-list terminal-session-note' }, [
    el('div', { class: 'empty-emoji', text: '✉' }),
    el('p', { text: t('mail.session_panel_heading') }),
    el('p', { class: 'muted', text: t('mail.session_panel_body') }),
  ]));
}

function openMailView() {
  if (state.decoy || !state.mailEnabled) return;
  closeToolPanes('mail');
  rememberPrev();   // before the selection changes: ✕ in a later tool lands here
  state.selectedBotId = MAIL_ID;
  mailOpen = true;
  renderSidebar();
  renderMailSessionPanel();
  dom['mail-view'].classList.remove('hidden');
  document.body.classList.add('tool-full');
  if (isMobile()) navigate('chat');
  renderMail();
}

function closeMailView() {
  mailOpen = false;
  document.body.classList.remove('tool-full');
  dom['mail-view'].classList.add('hidden');
  const f = dom['mail-frame'];
  if (f) { f.classList.add('hidden'); f.removeAttribute('src'); }
}

async function renderMail() {
  const note = dom['mail-note'];
  const frame = dom['mail-frame'];
  const dot = dom['mail-dot'];
  const label = dom['mail-status-label'];
  if (!note || !frame) return;
  dot.className = 'terminal-dot';
  label.textContent = t('mail.checking');
  let st;
  try {
    st = await api.mailStatus();
  } catch (e) {
    dot.classList.add('stopped');
    label.textContent = t('mail.state_unreachable');
    note.classList.remove('hidden');
    frame.classList.add('hidden');
    frame.removeAttribute('src');
    dom['mail-note-text'].textContent = t('mail.unreachable_text');
    dom['mail-note-hint'].textContent = t('mail.unreachable_hint');
    return;
  }
  if (!st.installed) {
    dot.classList.add('stopped');
    label.textContent = t('mail.state_not_installed');
    note.classList.remove('hidden');
    frame.classList.add('hidden');
    frame.removeAttribute('src');
    dom['mail-note-text'].textContent = t('mail.not_installed_text');
    dom['mail-note-hint'].textContent = t('mail.not_installed_hint');
    return;
  }
  if (!st.reachable || !st.launch_url) {
    dot.classList.add('stopped');
    label.textContent = t('mail.state_down');
    note.classList.remove('hidden');
    frame.classList.add('hidden');
    frame.removeAttribute('src');
    dom['mail-note-text'].textContent = t('mail.down_text');
    dom['mail-note-hint'].textContent = t('mail.down_hint');
    return;
  }
  dot.classList.add('running');
  label.textContent = t('mail.state_running');
  note.classList.add('hidden');
  // Hidden without a URL, the way the StudioForge pane's own link is. The
  // markup ships href="#" target="_blank", so when MailForge answers without a
  // launch_url the arrow opened a second, blank copy of DisPatch in a new tab
  // — an affordance that looks like it works and does something unrelated.
  if (st.launch_url) dom['mail-open'].href = st.launch_url;
  else dom['mail-open'].removeAttribute('href');
  dom['mail-open'].classList.toggle('hidden', !st.launch_url);
  if (st.launch_url && frame.getAttribute('src') !== st.launch_url) frame.setAttribute('src', st.launch_url);
  frame.classList.remove('hidden');
}

function wireMailView() {
  if (!dom['mail-view']) return;
  dom['mail-back'].addEventListener('click', () => navigate('bots'));
}

// ===================== Clients tab ("WebBuilder") =====================
// Native DisPatch UI against the practice box's client-pipeline API, proxied
// through /api/practice/*. All rendering lives in js/clients.js.
let clientsOpen = false;

async function refreshClientsFeature() {
  if (state.decoy) { state.clientsEnabled = false; return; }
  if (state.auth.features && state.auth.features.practice === false) {
    state.clientsEnabled = false; return;
  }
  try {
    await api.practiceGet('board');
    state.clientsEnabled = true;
  } catch (e) {
    if (e.status === 404 || e.status === 403 || e.status === 502) state.clientsEnabled = false;
  }
  renderSidebar();
}

function renderClientsSessionPanel() {
  dom['tl-botname'].textContent = t('clients.name');
  dom['tl-model'].textContent = t('clients.session_panel_title');
  const wrap = dom['threads'];
  wrap.innerHTML = '';
  wrap.append(el('div', { class: 'empty-list terminal-session-note' }, [
    el('div', { class: 'empty-emoji', text: '👥' }),
    el('p', { text: t('clients.session_panel_heading') }),
    el('p', { class: 'muted', text: t('clients.session_panel_body') }),
  ]));
}

function openClientsPanel() {
  if (state.decoy || !state.clientsEnabled) return;
  closeToolPanes('clients');
  rememberPrev();   // before the selection changes: ✕ in a later tool lands here
  state.selectedBotId = CLIENTS_ID;
  clientsOpen = true;
  renderSidebar();
  renderClientsSessionPanel();
  dom['clients-view'].classList.remove('hidden');
  document.body.classList.add('tool-full');
  if (isMobile()) navigate('chat');
  initClients(dom['clients-root'], dom['clients-job-strip']);
  showClientsTab('overview');
}

function closeClientsView() {
  clientsOpen = false;
  document.body.classList.remove('tool-full');
  dom['clients-view'].classList.add('hidden');
  // Hiding the view is not stopping it: the job poll reschedules itself, so
  // without this it kept polling the practice box for the rest of the session
  // — including after a drop to Safe Mode, where the pane it would report into
  // no longer exists.
  stopClientsPolling();
}

function wireClientsView() {
  if (!dom['clients-view']) return;
  dom['clients-back'].addEventListener('click', () => navigate('bots'));
  clientsTabNav(dom['clients-tabnav']);
  window.addEventListener('clients:open-thread', (ev) => {
    const threadId = ev && ev.detail && ev.detail.threadId;
    if (!threadId) return;
    closeClientsView();
    openThread(threadId);
  });
}

// ===================== Live streaming render =====================
// Render the accumulating reply as MARKDOWN while it streams (rAF-coalesced),
// so it looks identical to the finalized row — no plain-text→formatted snap.
const streamBuffers = {};
// Ids whose stream_done already landed. A late stream_start/stream_chunk for
// one of these (the router's terminal flush racing the persisted row) must
// NOT rebuild the bubble the stream_done just retired — that is exactly the
// duplicate, cursor-bearing copy of a reply seen on staging. Bounded so a
// long session cannot grow it; 64 covers any plausible in-flight overlap.
const settledStreamIds = [];
const SETTLED_STREAM_CAP = 64;
function markStreamSettled(id) {
  if (!id) return;
  const at = settledStreamIds.indexOf(id);
  if (at !== -1) settledStreamIds.splice(at, 1);
  settledStreamIds.push(id);
  while (settledStreamIds.length > SETTLED_STREAM_CAP) settledStreamIds.shift();
}
function streamIsSettled(id) { return settledStreamIds.includes(id); }
const streamPending = new Set();
let streamRaf = 0;
const STREAM_PAINT_MS = 100;      // ~10 repaints/sec: reads as smooth, costs 6x less
const STREAM_PAINT_CHARS = 160;  // …but a burst that big repaints immediately
// id -> { at, len } of the last paint. Pruned wherever streamBuffers and
// state.streamingIds are (stream_done, stream error, reboot) — it is paint
// bookkeeping for a message id that no longer exists, and a long-lived tab
// otherwise accumulates one small entry per reply, forever.
const streamPainted = new Map();
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
/** Open a streaming bubble for `id` in the ACTIVE thread.
 *
 *  `id` may be a provisional "run:<runId>" — the live token stream starts
 *  before any row is persisted — or a real message id (a chunked replay of a
 *  row that already exists). Nothing here cares which: it is a DOM key and a
 *  buffer key, and stream_done says which persisted message finally replaces
 *  it. It is deliberately NOT put in state.messages.
 *
 *  Extracted from the stream_start handler so a stream_chunk that arrives
 *  with no start (reconnect mid-turn) can open the same bubble, built by the
 *  same code, instead of a hand-rolled second version that would drift.
 */
function beginStream(id) {
  const box = dom['messages'];
  if (!box) return;
  // Already open (a duplicate start, or a chunk racing its own start).
  if (id in streamBuffers) return;
  state.streamingIds.add(id);
  streamBuffers[id] = '';
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
  const msgEl = el('div', { class: 'msg assistant streaming', dataset: { id } }, [
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
  // The phase line was parented to the typing indicator that just went away.
  paintTurnPhase();
  // Instant snap to the streaming bubble — smooth would be cancelled by
  // the first chunk arriving on the next rAF.
  if (isNearBottom()) scrollToBottom(true);
  else { showScrollButton(true); bumpUnseen(); }
}

function renderStreamMarkdown(id) {
  const msgEl = dom['messages'].querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (!msgEl) return;
  const md = msgEl.querySelector('.stream-md');
  if (!md) return;
  // noMedia in Safe Mode (same redaction as final). Code highlight is deferred
  // to stream_done (messageEl + enhanceContent) — too costly per frame.
  md.innerHTML = renderMarkdown(streamBuffers[id] || '', { noMedia: mediaHidden(), noLocal: state.decoy });
  // Instant scroll — smooth would fight itself across rapid streaming frames.
  if (isNearBottom(220)) scrollToBottom(true);
}

// ===================== WebSocket dispatch =====================
function handleWs(data) {
  switch (data.type) {
    // An agent posted or someone voted on a job. The server already put the
    // whole serialised job in this frame, so pass it along: the board patches
    // that one row instead of re-fetching the entire list, which is what it
    // used to do on every vote.
    case 'job_created':
    case 'job_updated':
      document.dispatchEvent(new CustomEvent('dispatch:jobs-changed', {
        detail: { type: data.type, job_id: data.job_id, job: data.job || null },
      }));
      return;
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
        // The reverse: we assumed Safe Mode (the boot happened offline, so
        // /api/auth/status never answered) and the server says the session
        // is full. Re-skinning is not enough — the local store was opened on
        // the SAFE tier, so the unlocked outbox was never restored and a send
        // queued before the offline reload sat unsent until the next online
        // reload. Reboot into the real tier; restoreLocalState replays it.
        if (!data.decoy && state.decoy && state.auth.pinSet) {
          state.decoy = false;
          state.auth.decoy = false;
          state.auth.authenticated = true;
          reboot();
          break;
        }
        state.decoy = data.decoy;
        state.auth.decoy = data.decoy;
        applyAuthChrome();
      }
      if (Array.isArray(data.bots)) {
        // The server's decoy redactor already drops non-safe bots from a
        // locked session's frames; this re-checks on the client for the same
        // reason reactions.js does before firing an overlay — a frame that
        // slips through (a race with a lock, a mode flip mid-flight) must not
        // be able to put a full-access bot on a Safe-Mode rail. `safe` is
        // always present on the wire (Bot.to_dict), so this cannot empty the
        // list for a legitimate frame.
        const prevBots = state.bots;
        state.bots = data.bots.filter((b) => b.visible && (!state.decoy || b.safe));
        const vb = visibleBots();
        // Only repair the selection if one existed and its bot vanished —
        // never auto-select on a fresh landing (the bots list IS the landing).
        // A TOOL is never in the roster, so it never "vanished": without the
        // isToolId guard every bots frame (the hello on connect, an avatar
        // change) threw an open tool — builtin or tools.yaml — back to a bot.
        if (state.selectedBotId && !isToolId(state.selectedBotId)
            && !vb.find((b) => b.id === state.selectedBotId)) {
          if (vb.length) selectBot(vb[0].id);
          else { state.selectedBotId = null; state.threads = []; renderSidebar(); renderThreads(); clearChatView(); }
        } else {
          renderSidebar();
          // An avatar change broadcasts through here — and everything painted
          // from bot.avatar_url must follow, not just the rail. Repainting only
          // the sidebar left the headers' thumbnails on the OLD ?v= URL while
          // their data-full pointed at the (unversioned) new full-res: click a
          // header and the lightbox opened a different picture than the thumb.
          //
          // …but ONLY for the bots whose look actually changed. This frame is
          // re-broadcast for every bot edit on the box (a model hint, a safe
          // flag, another bot's nightly avatar draw), and it was repainting the
          // thread list AND every message in the open chat each time. `changed`
          // is computed against the roster we were holding a moment ago; on the
          // first frame (nothing to compare with) it is null and everything
          // repaints, which keeps the boot path exactly as it was.
          const changed = changedBotLooks(prevBots, state.bots);
          if (changed === null || changed.has(state.selectedBotId)) {
            updateThreadListHeader();
            // Thread-list ROWS too: a snapshotless thread (every pre-feature
            // one) renders the live avatar in its row, so its thumbnail must
            // repaint or it drifts from its own (no-cache) full-res the same
            // way the header did. Rows with a pinned snapshot are immune.
            renderThreads();
          }
          const activeBotId = state.activeThread?.bot_id || state.selectedBotId;
          if (changed === null || changed.has(activeBotId)) {
            renderChatHeader();
            // Message avatars in a SNAPSHOTLESS thread render the live avatar
            // too. Threads with a pin are immune — their faces are frozen.
            // Only the OPEN thread's own bot can change them, so another bot's
            // nightly avatar draw no longer rebuilds this transcript.
            if (state.activeThread && !state.activeThread.avatar_snapshot) {
              renderMessages(false);
            }
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
      // upsertThread() has already re-sorted, so patchThreadRow's position
      // check is the authority on whether this was a cheap change (title,
      // preview, updated_at while the row was already on top) or one that
      // moves the row — it rebuilds the list itself in that case. A plain
      // renderThreads() here was one of six full repaints per incoming
      // message; threadRowEl's own comment measures what that cost.
      if (data.thread.bot_id === state.selectedBotId) patchThreadRow(data.thread.id);
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
      // A live frame is fresher than anything on disk, so the cache must not
      // paint over it — including a cache read that is in flight right now.
      markThreadFresh(data.thread_id);
      // Secondary ack: our own send echoed back as the persisted broadcast.
      clearPendingSend(data.client_msg_id || data.message.client_msg_id);
      // …and take down the optimistic bubble BEFORE appendMessageToView runs,
      // or the day separator and the consecutive-sender grouping below would
      // both be computed around a row that is about to disappear.
      reconcileOptimistic(data);
      const isFinalReply = data.message.role === 'assistant'
        && !(data.message.metadata && data.message.metadata.sub);
      // A delivered reply ends the visible "working" state immediately —
      // don't keep the typing bubble up waiting for the stopped event.
      if (isFinalReply && state.thinking[data.thread_id]) {
        state.thinking[data.thread_id] = false;
        setTurnPhase(data.thread_id, null);
        renderSidebar(); touchThreadRow(data.thread_id);
      }
      if (data.thread_id === state.activeThreadId) {
        appendMessageToView(data.message);
        scheduleCacheRefresh(data.thread_id);
        if (isFinalReply) {
          reflectComposerState();
          const rowEl = dom['messages'].querySelector(`[data-id="${CSS.escape(data.message.id)}"]`);
          maybeFlashMoodFace(data.message, rowEl);
        }
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
          renderSidebar(); touchThreadRow(data.thread_id);
        }
      }
      break;
    }
    case 'thinking': {
      if (!data.thread_id) break;
      const on = data.status === 'started';
      state.thinking[data.thread_id] = on;
      if (on) state.progress[data.thread_id] = [];   // fresh turn, fresh log
      // A fresh turn is a fresh attempt — drop any standing warning from a
      // PREVIOUS refusal on this thread now rather than let the chip keep
      // showing a sentence about a choice that may no longer even be active.
      if (on && state.modelChipWarn[data.thread_id]) {
        delete state.modelChipWarn[data.thread_id];
        if (data.thread_id === state.activeThreadId) renderModelChip();
      }
      // A finished turn has no phase; a starting one has not reported its
      // first phase yet, and the previous turn's must not be left standing.
      setTurnPhase(data.thread_id, null);
      if (!on) clearStopRequest(data.thread_id);
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
    case 'turn_status': {
      // What the gateway is doing right now, before (and between) tokens.
      // Kept per-thread so a background bot's phase does not paint into the
      // chat you are reading, and dropped for a thread that is not working —
      // a phase line under nothing would outlive its turn.
      // No "is this thread working" guard is needed: paintTurnPhase only
      // renders where a typing indicator or a streaming bubble exists, so a
      // phase for a finished (or background) turn has nowhere to land.
      if (!data.thread_id) break;
      setTurnPhase(data.thread_id, data.phase || null);
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
      // AgentRefused — a verdict on the thread's model/thinking choice, not
      // an outage (see main.py's err_frame['refused']). The preference is
      // untouched server-side; this just puts the chip in its warning state
      // so the operator notices and can open the picker to correct it.
      if (data.thread_id && data.refused) {
        state.modelChipWarn[data.thread_id] = data.message;
        if (data.thread_id === state.activeThreadId) renderModelChip();
      }
      // Freeze any half-streamed bubble for this thread: keep the partial text,
      // drop the blinking cursor so it doesn't sit "alive" above the error.
      if (data.thread_id === state.activeThreadId) {
        dom['messages'].querySelectorAll('.msg.streaming').forEach((m) => {
          m.classList.remove('streaming');
          const c = m.querySelector('.stream-cursor'); if (c) c.remove();
          // No stream_done will follow this turn — release the buffer now.
          delete streamBuffers[m.dataset.id];
          streamPainted.delete(m.dataset.id);
          state.streamingIds.delete(m.dataset.id);
        });
      }
      // Clear the working state for ANY thread — a background-thread error
      // would otherwise leave that bot's sidebar dot pulsing forever.
      if (data.thread_id && state.thinking[data.thread_id]) {
        state.thinking[data.thread_id] = false;
        renderSidebar(); touchThreadRow(data.thread_id);
      }
      // A refused/failed abort must give the button back rather than leaving
      // it disabled until the next turn.
      clearStopRequest(data.thread_id || state.activeThreadId);
      setTurnPhase(data.thread_id || state.activeThreadId, null);
      if (data.thread_id === state.activeThreadId) {
        reflectComposerState();
        appendErrorBubble(data.message, data.thread_id);
      }
      break;
    case 'stream_start': {
      if (!data.thread_id || !data.message_id) break;
      if (data.thread_id !== state.activeThreadId) break;
      if (streamIsSettled(data.message_id)) break;
      beginStream(data.message_id);
      break;
    }
    case 'stream_chunk': {
      if (!data.thread_id || !data.message_id) break;
      if (data.thread_id !== state.activeThreadId) break;
      if (streamIsSettled(data.message_id)) break;
      // A chunk for an id we never saw start is a reconnect landing in the
      // middle of a live turn: the stream_start went to a socket that no
      // longer exists. Build the bubble now rather than dropping every token
      // until the turn ends — the old code silently discarded the whole reply
      // and only the final persisted message appeared, all at once.
      if (!(data.message_id in streamBuffers)) beginStream(data.message_id);
      // `replace` carries the FULL text so far (the server re-sends the whole
      // buffer when it cannot know what this client already has — a resumed
      // stream, a repaired truncation). Appending it would duplicate
      // everything before the gap.
      if (data.replace) streamBuffers[data.message_id] = data.text || '';
      else streamBuffers[data.message_id] += data.text || '';
      scheduleStreamRender(data.message_id);
      break;
    }
    case 'stream_done': {
      // The bubble on screen is keyed by whatever id its chunks arrived
      // under: a PROVISIONAL "run:<id>" while the reply was only tokens in
      // flight, or the real message id for a replayed/chunked persisted row.
      // provisional_id is how the server says "the row you have been painting
      // is this one, and its real id is message_id".
      const streamId = data.provisional_id || data.message_id;
      if (!streamId) break;
      const fullMsg = data.message || null;
      // Release BOTH ids: message_id is unused as a buffer key in the
      // provisional case, but a defensive delete costs nothing and a leaked
      // streamPainted entry is exactly the per-reply leak its comment warns of.
      for (const id of new Set([streamId, data.message_id].filter(Boolean))) {
        state.streamingIds.delete(id);
        delete streamBuffers[id];
        streamPainted.delete(id);
        markStreamSettled(id);
      }
      setTurnPhase(data.thread_id, null);
      clearStopRequest(data.thread_id);
      if (fullMsg) announceMessage(fullMsg);
      // Streamed reply delivered — clear the working state right away.
      if (state.thinking[data.thread_id]) {
        state.thinking[data.thread_id] = false;
        renderSidebar(); touchThreadRow(data.thread_id);
        if (data.thread_id === state.activeThreadId) reflectComposerState();
      }
      if (data.thread_id === state.activeThreadId) {
        const existingEl = dom['messages'].querySelector(`[data-id="${CSS.escape(streamId)}"]`);
        if (!fullMsg) {
          // Aborted or errored run: there is no persisted row to swap in, and
          // half a sentence left sitting under a live cursor reads as a reply
          // still arriving. Take the placeholder away; if the server made a
          // failed-reply row it arrives on its own as an ordinary 'message'.
          if (existingEl) existingEl.remove();
          refreshTyping();
          break;
        }
        if (existingEl) {
          // Only the ACTIVE thread's array gets the message — pushing a
          // background thread's reply here would bleed it into this chat on the
          // next full re-render. Push BEFORE building the node: messageEl only
          // offers "Regenerate" when the message is the last one in state.
          // Note the id used here is the REAL one (fullMsg.id / message_id) —
          // a provisional id must never reach state.messages.
          if (!state.messages.some((m) => m.id === fullMsg.id)) {
            state.messages.push(fullMsg);
            state.messages.sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
          }
          // messageEl wires its own image-load scroll pinning.
          // NIM: a picture-only final message has no row. Drop the streaming
          // placeholder rather than replacing it with nothing.
          const finalEl = messageEl(fullMsg);
          if (finalEl) {
            existingEl.replaceWith(finalEl);
            maybeFlashMoodFace(fullMsg, finalEl);
          } else {
            existingEl.remove();
          }
          // The final rendered message may be taller than the streaming
          // placeholder (syntax-highlighted code blocks, full markdown).
          if (isNearBottom()) scrollToBottom(true);
        } else {
          // Missed stream_start (reconnect mid-stream) — treat like a new
          // message (appendMessageToView pushes into state.messages itself).
          appendMessageToView(fullMsg);
          const rowEl = dom['messages'].querySelector(`[data-id="${CSS.escape(fullMsg.id)}"]`);
          maybeFlashMoodFace(fullMsg, rowEl);
        }
      } else if (fullMsg && fullMsg.role !== 'user' && !state.unread[data.thread_id]) {
        // Streamed reply landed in a background thread — mark it unread, same
        // as the plain 'message' path does. Backfill threadBot first so the
        // sidebar dot resolves for bots whose threads we never loaded.
        if (data.bot_id && !state.threadBot[data.thread_id]) state.threadBot[data.thread_id] = data.bot_id;
        state.unread[data.thread_id] = fullMsg.created_at;
        renderSidebar(); touchThreadRow(data.thread_id);
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
    case 'message_update': {
      // A message that is already on screen has been rewritten server-side —
      // today, an image job's placeholder becoming the picture or the ⚠️ line.
      // Replace the node rather than appending: the id is unchanged, and a
      // second bubble would read as the bot saying it twice.
      const mid = data.message_id || (data.message && data.message.id);
      if (!mid || !data.message) break;
      const idx = state.messages.findIndex((x) => x.id === mid);
      if (idx >= 0) state.messages[idx] = data.message;
      if (data.thread_id !== state.activeThreadId) break;
      const oldEl = dom['messages'].querySelector(`[data-id="${CSS.escape(mid)}"]`);
      if (!oldEl) break;
      const stick = isNearBottom();
      const nextEl = messageEl(data.message);
      if (!nextEl) {
        // No-Image Mode: the finished picture is a media-only row, which NIM
        // omits entirely. The placeholder that stood in for it has to go with
        // it — leaving "Generating an image…" behind would be a permanent
        // trace of exactly the thing NIM removed.
        oldEl.remove();
        break;
      }
      if (oldEl.classList.contains('grouped')) nextEl.classList.add('grouped');
      oldEl.replaceWith(nextEl);
      pinOnImageLoad(nextEl);
      if (stick) scrollToBottom();
      break;
    }
    case 'checklist_update': {
      // Another device checked/unchecked a row. Update the in-memory message
      // (so a re-render keeps the state) and repaint the live widget.
      const mid = data.message_id;
      if (!mid) break;
      const m = state.messages.find((x) => x.id === mid);
      if (m) m.metadata = { ...(m.metadata || {}), checklist: data.checklist };
      if (data.thread_id === state.activeThreadId) {
        const msgEl = dom['messages'].querySelector(`[data-id="${CSS.escape(mid)}"]`);
        if (msgEl) applyChecklistState(msgEl, data.checklist);
      }
      break;
    }
    case 'message_feedback': {
      // Feature 28: another device (or this one's own request — the sender
      // already updated its local state in sendFeedback, so this is a no-op
      // there beyond a harmless re-render) rated a reply. Same
      // update-state-then-repaint shape as checklist_update above.
      const mid = data.message_id;
      if (!mid || !data.feedback) break;
      const idx = state.messages.findIndex((x) => x.id === mid);
      if (idx >= 0) {
        state.messages[idx] = { ...state.messages[idx],
          metadata: { ...(state.messages[idx].metadata || {}), feedback: data.feedback } };
      }
      if (data.thread_id === state.activeThreadId && idx >= 0) {
        const oldEl = dom['messages'].querySelector(`[data-id="${CSS.escape(mid)}"]`);
        if (oldEl) {
          const nextEl = messageEl(state.messages[idx]);
          if (nextEl) {
            if (oldEl.classList.contains('grouped')) nextEl.classList.add('grouped');
            oldEl.replaceWith(nextEl);
          }
        }
      }
      break;
    }
    case 'locked':
      // The full session expired server-side → fall back to Safe Mode.
      handleLocked();
      break;
    case 'harness_state': {
      // Full-session only — Safe-Mode clients never receive this frame (the
      // server redactor drops it). Carries whichever slice changed: jobs /
      // service / models.
      applyHarnessFrame(data);
      break;
    }
    case 'harness_sessions': {
      // The live-session LIST (launch / stop / finish). Events are not
      // broadcast: the open session polls its own endpoint for those.
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
  // The lock screen belongs in this set, not in a second implementation of it.
  // It is a role="dialog" aria-modal="true" surface shown by toggling .hidden,
  // exactly like the backdrops — and aria-modal is a promise the markup cannot
  // keep: without inert, Tab walks out of the PIN card into the roster and the
  // gear behind it, on an app that has not been unlocked.
  //
  // It has to be the SAME owner because the only route to it is
  // `hideCompanions(); showUnlock()` — two class changes in one task. A
  // separate owner set inert in showUnlock and then this observer, firing
  // afterwards and seeing the companions backdrop closed, lifted it again.
  const backdrops = Array.from(document.querySelectorAll('.modal-backdrop, .lock-screen'));
  if (!backdrops.length) return;
  const isOpen = (b) => !b.classList.contains('hidden');
  const anyOpen = () => backdrops.some(isOpen);
  const shown = new WeakMap();
  backdrops.forEach((b) => shown.set(b, isOpen(b)));
  // One hold for "some watched surface is open", taken and released through
  // the shared counter so the lightbox and the job overlay can hold their own
  // at the same time. Writing the attribute directly here is what let a
  // backdrop closing anywhere strip another surface's trap.
  let guardToken = null;
  const setInert = (on) => {
    if (on && !guardToken) guardToken = acquireInert();
    else if (!on && guardToken) { releaseInert(guardToken); guardToken = null; }
  };
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
  dom['stop'].addEventListener('click', stopReply);
  dom['retry-chip-btn'].addEventListener('click', retryPendingSends);
  dom['reply-chip-cancel'].addEventListener('click', clearReplyTarget);
  dom['input'].addEventListener('input', () => {
    autosize(); updateSendEnabled(); scheduleDraftSave();
  });
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
  // The rail's theme button is a shortcut straight to Settings → Theme, the
  // ONE place a palette can be changed. It used to cycle Dark/Light itself;
  // that switch is gone (a theme is now a single fixed palette, not a pair).
  // Wired here rather than in theme.js because opening Settings is main.js's
  // job — theme.js stays a self-contained appearance module.
  dom['theme-toggle'].addEventListener('click', () => openSettingsTab('theme'));
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
  wireToolsMenu();
  wireTools({
    state,
    openers: {
      harness: openHarnessView,
      studioforge: openStudioForgeView,
      mail: openMailView,
      practice: openClientsPanel,
    },
    closeToolPanes,
    builtinOn,
    builtinDot,
    restoreView: restoreAfterTool,
    // The Job Board hides #chatview on every width, which is where every tool
    // pane lives — leave it first or the pane opens invisibly.
    beforeOpen: () => { if (dom.app.dataset.view === 'jobs') setView('chat'); },
    paintPlaceholder: (title, icon) => {
      dom['tl-botname'].textContent = title;
      dom['tl-model'].textContent = t('tools.session_panel_title');
      const wrap = dom['threads'];
      wrap.innerHTML = '';
      wrap.append(el('div', { class: 'empty-list terminal-session-note' }, [
        el('div', { class: 'empty-emoji', text: icon }),
        el('p', { text: t('tools.session_panel_body') }),
      ]));
    },
    renderSidebar,
    isMobile,
    navigate,
    toast,
    isBotId: (id) => !!botById(id),
    // A builtin switched on/off in Settings → Tools flips its server feature
    // flag; re-probe so the rail and panes agree with the server.
    onSaved: () => {
      state.auth.features = null;
      ensureFeatures().finally(() => {
        refreshHarnessFeature();
        refreshStudioForgeFeature();
        refreshMailFeature();
        refreshClientsFeature();
      });
    },
  });

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
    // setMinimalAvatars refuses under NIM for the same reason.
    if (nimEnabled()) { syncMinimalAvatarRow(); return; }
    setMinimalAvatars(dom['bm-avatar-minimal'].checked);
    renderPins();   // a pinned 👤 shows the same state this checkbox writes
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
    // The label is a child <span> now (the icon span next to it is not
    // i18n-controlled and must survive a language switch untouched).
    const pinLabel = document.querySelector('#thread-menu-pin [data-i18n]');
    // Re-point the key rather than writing bare text: the next language switch
    // re-runs the DOM pass, which would otherwise reset this to "Pin".
    if (pinLabel) setI18nText(pinLabel, (state.activeThread && state.activeThread.is_pinned) ? 'chat.unpin' : 'chat.pin');
    toggleThreadMenu();
  });
  dom['thread-menu'].querySelectorAll('button').forEach((b) =>
    b.addEventListener('click', (e) => {
      // The picker this opens closes on any document click — including the
      // very click that asked for it, unless it stops here.
      if (b.dataset.act === 'model') e.stopPropagation();
      threadAction(b.dataset.act);
    }));
  document.addEventListener('click', () => toggleThreadMenu(false));
  wireModelChip();

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
      const lb = [...document.querySelectorAll('.lightbox')].at(-1);
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
      // Every open, first connect included — that is what carries the outbox
      // restored from disk at boot, which onReconnect would never see.
      onOpen: () => resendPendingSends(),
      onReconnect: () => {
        toast(t('toast.reconnected')); resync(); refreshUnread();
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
  // Mobile Jobs tab: shown only when the server has a 'jobboard' bot
  // configured AND the user is unlocked (the route is full-session only —
  // /api/jobs returns the empty shape for a decoy, see backend/app/jobs.py).
  // The tab starts hidden in index.html, so the default boot of a no-jobs
  // install does not show a dead button.
  const hasJobboard = state.bots.some((b) => b.id === 'jobboard');
  if (dom['tab-jobs']) {
    const show = !state.decoy && hasJobboard;
    dom['tab-jobs'].classList.toggle('hidden', !show);
    dom['tab-jobs'].hidden = !show;
  }
  dom['lock-now'].classList.toggle('hidden', !full);
  dom['bm-lock'].classList.toggle('hidden', !full);
  dom['attach-btn'].classList.remove('hidden');
  document.body.classList.toggle('decoy-mode', state.decoy);
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
  // The palette picker is a Settings tab now, and Settings is a full-session
  // surface (the gear routes the locked side to Companions), so the rail
  // shortcut goes with it. The language picker — the other purely cosmetic
  // device preference — sits behind the same door for the same reason. Left
  // visible, this would be a button whose only effect is a modal that never
  // opens. The previously-shipped Dark/Light toggle DID work in Safe Mode; a
  // palette is a bigger choice than a one-bit toggle, so it moved behind the
  // same gate as every other device preference rather than getting its own.
  // 🎨 is a pin like any other, just one that ships pinned and keeps its own
  // markup (pins.js `kind: 'rail'`). Unpinning it in Settings → Device hides
  // the button; the Theme tab itself is untouched, so nothing becomes
  // unreachable — it is a rail shortcut, not the only door.
  dom['theme-toggle'].classList.toggle('hidden', state.decoy || !isPinned('theme'));
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
  // Pinned settings live on the same rail and follow the same rule: rebuilt on
  // every tier change, so a pin that Safe Mode may not have disappears the
  // moment the device locks rather than lingering as a dead button.
  renderPins();
}

// Draw the pinned settings onto the rail. Kept as one call site so every
// path that changes a pinned setting, pins one, or crosses the lock boundary
// repaints the same way.
function renderPins() {
  // 🎨 is drawn by the markup, not by renderPinnedRail, so its pin has to be
  // applied here too — otherwise unpinning it in Settings leaves the button on
  // the rail until the next tier change repaints the chrome.
  if (dom['theme-toggle']) {
    dom['theme-toggle'].classList.toggle('hidden', state.decoy || !isPinned('theme'));
  }
  renderPinnedRail(document.getElementById('gear-row'), {
    decoy: state.decoy,
    t,
    onChange: (id) => {
      // A pinned toggle changes the same device setting the Settings pane
      // shows, so anything open must be repainted or the two disagree.
      if (id === 'nim') applyNimChange(nimEnabled());
      // A rail toggle of minimal avatars must repaint its Settings checkbox
      // (the CSS attribute swap itself needs no re-render).
      if (id === 'avatars') syncMinimalAvatarRow();
      if (document.getElementById('nim-row')) mountLanguagePicker();
    },
  });
  // Custom link buttons share the rail and the rebuild-on-tier-change rule,
  // but not the registry: they are unlocked-only (the URLs are exactly what
  // Safe Mode must not advertise) and links.js enforces that on every render.
  renderLinkRail(document.getElementById('gear-row'), { decoy: state.decoy, openViewer });
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
      row.append(el('span', { class: 'comp-react', title: t('companions.reacts') }, [railIcon(RAIL_ICONS.bolt)]));
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
  // Focus and inert are NOT set here. initModalFocusGuard watches this element
  // alongside the .modal-backdrop set and owns both, which matters because the
  // only route to this screen is `hideCompanions(); showUnlock()` — two class
  // changes in one task. With a second owner here, the guard's observer fired
  // afterwards, saw the companions backdrop closed, and lifted the inert this
  // function had just set.
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
  // The tool panes are full-session only — never leave a view alive across
  // a drop to Safe Mode.
  //
  // All FOUR of them. Mail and Clients were missing here while selectBot()
  // (which closes the same set on an ordinary view change) already listed
  // them, so a session that dropped to Safe Mode with the Emails or Clients
  // tab open kept that pane painted — an iframe of the MailForge dashboard,
  // or the client pipeline, still on screen on a device that has just been
  // locked. The panes' own APIs refuse in Safe Mode, so nothing new could be
  // fetched, but the last frame stayed visible, which is exactly what Safe
  // Mode exists to prevent.
  closeToolPanes();
  // The job detail overlay is none of the three shapes swept below: it is a
  // body-level sibling built at click time, not a .modal-backdrop, not a
  // .lightbox and not a <dialog>. So it survived a drop to Safe Mode with the
  // full job detail and its vote controls on screen, and kept #app inert until
  // somebody closed it by hand. Same leak class as the two panes above.
  closeJobDetail();
  // Never n.remove() a lightbox directly: that skips pausing the video and
  // unbinding its window-level pan listeners.
  document.querySelectorAll('.lightbox').forEach((n) => {
    if (typeof n._close === 'function') n._close(); else n.remove();
  });
  // The viewer IS a .lightbox and the sweep above closes it — this is the
  // belt-and-braces half: if its node ever left the DOM by another route, the
  // module would still hold it as the open overlay (and keep app.inert set).
  // A no-op when nothing is open.
  closeViewer();
  // Native <dialog>s (rename/confirm prompts, Cmd+K palette) live in the
  // browser top layer, above everything — they too must never survive a lock.
  document.querySelectorAll('dialog[open]').forEach((d) => { try { d.close(); } catch { /* ignore */ } });
  toggleThreadMenu(false);
  bmBots = [];
  bmDirty = false;

  // Hiding a backdrop is not emptying it. Every panel above keeps its content
  // in the DOM and its data in a module-level cache, so a lock left full-access
  // material one `classList.remove('hidden')` — or one devtools inspection —
  // away: transcript lines, search snippets with message text, the file list,
  // harness job output, avatar-pool status. bmBots was already cleared for
  // exactly this reason; the rest were missed. Same posture as the lightbox
  // teardown above: assume a frame slips through and leave nothing behind it.
  for (const k of ['tx-list', 'search-results', 'fs-list', 'harness-jobs', 'avatar-pool-panel']) {
    if (dom[k]) dom[k].innerHTML = '';
  }
  txState = { items: [], filter: null, threadId: null, sessionKey: null, botId: null, missing: 0 };
  searchHits = [];
  harnessJobBodies.clear();
  apPools = null;
  const searchBox = dom['search-input'];
  if (searchBox) searchBox.value = '';

  // An API key typed into the AI-models pane is a secret sitting in an input
  // value, exactly like the PIN and the recovery code — both of which are
  // wiped after use for this reason. This one survived a lock, still populated
  // behind the lock screen (and in the next screenshot or bug report).
  const llmKey = document.getElementById('llm-key');
  if (llmKey) llmKey.value = '';
}

// Rebuild the app for the current mode: drop the socket + sensitive caches and
// reconnect, so the WebSocket re-handshakes at the right access level and
// messages are re-fetched (full or redacted accordingly).
async function reboot() {
  state.started = false;
  cancelLocalWrites();
  if (socket) { socket.stop(); socket = null; }
  // A mode/session change invalidates unacked sends — never replay them on
  // the new (possibly Safe-Mode) socket.
  dropAllPendingSends();
  // The tier may have changed under us; drop the store and the paint gate so
  // the next startApp() rebuilds both against the tier it is actually in.
  if (localStore) { localStore.close(); localStore = null; cachePainter = null; }
  cachedPainted.clear();
  state.drafts = new Set();
  state.messages = []; state.threads = [];
  state.activeThreadId = null; state.activeThread = null;
  state.unread = {}; state.thinking = {}; state.progress = {};
  state.streamingIds = new Set();
  // Partial reply text must not survive a drop to Safe Mode.
  Object.keys(streamBuffers).forEach((k) => delete streamBuffers[k]);
  streamPending.clear();
  streamPainted.clear();
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
  // Still synchronous: a draft debounce or cache refresh armed while unlocked
  // would otherwise fire AFTER state.decoy flipped and write the unlocked
  // text into the SAFE tier, where no later wipeTier(TIER_FULL) reaches it.
  cancelLocalWrites();
  // The unlocked tier's local cache goes with the session. Not being able to
  // READ it is the weaker half of the promise; on a device somebody has just
  // handed over, the drafts, the queued sends and the cached messages have to
  // stop existing. ensureLocalStore() has already flipped to the safe tier by
  // the time this runs (state.decoy is set above), so this is the locked
  // session deleting what the unlocked one left — the one thing wipeTier is
  // allowed to reach across for.
  try { await ensureLocalStore().wipeTier(TIER_FULL); } catch { /* best effort */ }
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
  // Coming back to a backgrounded app.
  //
  // The idle-lock timer is a plain setTimeout, and browsers freeze those on a
  // backgrounded tab — so on a phone it commonly never fires and this check is
  // the ONLY thing that re-validates an unlocked session against the server's
  // own idle clock. It used to do that with the previous session still painted:
  // thread list, message text, avatars, whatever was open when the phone went
  // into a pocket, readable by whoever picked it up, for as long as the request
  // took. `.catch(() => {})` meant a network error on the first tick of a radio
  // reconnect left it there indefinitely.
  //
  // Now: cover first, synchronously, before anything is awaited. Then a
  // time-boxed check. Then fail CLOSED — a check that did not succeed is
  // treated as locked, because the alternative is treating "I could not ask"
  // as "yes, still unlocked".
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (!(state.auth.pinSet && state.started)) return;
    document.body.classList.add('revalidating');
    api.authStatus()
      .then((s) => { if (!s.authenticated) handleLocked(); })
      .catch(() => { handleLocked(); })
      .finally(() => { document.body.classList.remove('revalidating'); });
  });
}

// ===================== Boot =====================

// The rail's static buttons ship emoji/text glyphs in the markup (a readable
// no-JS fallback); swap them for the shared line-icon set so the whole rail —
// built-ins, pins and links alike — speaks one visual language. The theme
// button is theme.js's (it sets the swatch icon once at boot; it no longer
// repaints, because there is no per-theme state left to draw), and the buttons
// carry only data-i18n-attr, so no translation pass ever writes text over the
// SVG.
function iconifyRail() {
  for (const [id, icon] of [
    ['lock-now', RAIL_ICONS.lock],
    ['unlock-btn', RAIL_ICONS.unlock],
    ['drop-btn', RAIL_ICONS.upload],
    ['fs-chip', RAIL_ICONS.folder],
    ['manage-bots', RAIL_ICONS.gear],
  ]) {
    const b = document.getElementById(id);
    if (b) b.replaceChildren(railIcon(icon));
  }
}

// The same swap, for the chrome outside the rail (2026-09). The tab bar, the
// settings tab strip, the chat header and the composer shipped colour emoji,
// which the platform draws in its own palette: they cannot theme, and beside
// the rail's line art they read as another app's furniture. Everything here
// keeps its emoji in the markup as the no-JS fallback, exactly like the rail.
//
// Two rules this function must not break:
//  1. NEVER replace the whole button when it also holds a text label. The
//     tabs and the settings tabs carry a translated <span> that i18n rewrites
//     on every language change; only the GLYPH span is swapped, or the label
//     disappears the first time someone switches language.
//  2. Only touch a glyph that is still the shipped emoji. Re-running this (a
//     re-render, a tier change) must be a no-op rather than nesting an <svg>
//     inside the last one.
function iconifyChrome() {
  // [element, icon, glyphSelector] — glyphSelector picks the span to replace
  // when the control is more than its glyph; null means the whole element is
  // the glyph.
  const swaps = [
    // Mobile tabs: the glyph is the first <span>, the label is the second.
    ['.mobile-tabs .tab[data-view="bots"]', RAIL_ICONS.bots, 'span:not(.tab-label)'],
    ['.mobile-tabs .tab[data-view="threads"]', RAIL_ICONS.chats, 'span:not(.tab-label)'],
    ['.mobile-tabs .tab[data-view="chat"]', RAIL_ICONS.messages, 'span:not(.tab-label)'],
    ['#tab-jobs', RAIL_ICONS.jobs, 'span:not(.tab-label)'],
    // Settings tab strip.
    ['#stab-bots', RAIL_ICONS.bots, '.stab-glyph'],
    ['#stab-reactions', RAIL_ICONS.bolt, '.stab-glyph'],
    ['#stab-health', RAIL_ICONS.pulse, '.stab-glyph'],
    ['#stab-ai', RAIL_ICONS.chip, '.stab-glyph'],
    ['#stab-tools', RAIL_ICONS.tools, '.stab-glyph'],
    ['#stab-theme', RAIL_ICONS.palette, '.stab-glyph'],
    ['#stab-device', RAIL_ICONS.device, '.stab-glyph'],
    ['#stab-security', RAIL_ICONS.shield, '.stab-glyph'],
    // Chat header + thread list.
    ['#back-btn', RAIL_ICONS.back, null],
    ['#expand-threads', RAIL_ICONS.forward, null],
    ['#collapse-threads', RAIL_ICONS.back, null],
    ['#popout-btn', RAIL_ICONS.popout, null],
    ['#thread-menu-btn', RAIL_ICONS.menu, null],
    ['#search-btn', RAIL_ICONS.search, null],
    ['#new-chat', RAIL_ICONS.plus, null],
    ['#scroll-bottom', RAIL_ICONS['chevron-down'], null],
    // Composer.
    ['#attach-btn', RAIL_ICONS.attach, null],
    ['#send', RAIL_ICONS.send, null],
    ['#stop', RAIL_ICONS.stop, null],
    // The two big decorative glyphs: the empty chat and the lock face. Scoped
    // to #chat-empty on purpose — the harness and StudioForge panes reuse
    // .empty-emoji for their "dsh" and "SF" wordmarks, which are text, not
    // emoji, and must stay as they are.
    ['#chat-empty .empty-emoji', RAIL_ICONS.messages, null],
    ['.lock-emoji', RAIL_ICONS.lock, null],
  ];
  for (const [sel, icon, glyphSel] of swaps) {
    const host = document.querySelector(sel);
    if (!host || !icon) continue;
    const target = glyphSel ? host.querySelector(glyphSel) : host;
    if (!target || target.querySelector('svg')) continue;   // already iconified
    target.replaceChildren(railIcon(icon));
  }
  // Every modal in the app closes with the same ✕, on eight different ids.
  // Matching the GLYPH rather than listing the ids is both shorter and harder
  // to forget to extend: a ninth modal gets the icon for free. textContent is
  // compared trimmed and exactly, so a button that merely contains a ✕ inside
  // a longer label is left alone.
  for (const b of document.querySelectorAll('.icon-btn')) {
    if (b.textContent.trim() === '✕') b.replaceChildren(railIcon(RAIL_ICONS.close));
  }
}

// The generic form of the two swaps above, for markup written AFTER them
// (2026-09-18 sweep: the thread menu, the recovery/File-Server/search panel
// titles, the settings recovery+lock rows). A `<span data-icon="pin">`
// anywhere in `root` gets that RAIL_ICONS entry — no id list to keep in sync,
// so a new menu item just needs the attribute. Safe to call again on freshly
// inserted DOM (a modal's contents, say): already-filled spans are skipped
// the same way iconifyChrome skips an already-swapped glyph.
function iconifyDataIcons(root = document) {
  for (const host of root.querySelectorAll('[data-icon]')) {
    const icon = RAIL_ICONS[host.dataset.icon];
    if (!icon || host.querySelector('svg')) continue;
    host.replaceChildren(railIcon(icon));
  }
}

async function startApp() {
  if (state.started) return;
  state.started = true;
  iconifyRail();
  iconifyChrome();
  iconifyDataIcons();
  // Delegated code-block / file-path copy handlers (idempotent, survives
  // re-render + streaming since it binds on document, not per message).
  // A plain click on a file path opens it in the local viewer; Shift/Alt-click
  // and a long-press still copy the path (markdown.js owns that split). The
  // decoy check is belt-and-braces — openViewer refuses in Safe Mode too, and
  // Safe-Mode markdown never renders a file link in the first place.
  installMarkdownHandlers(toast, {
    onOpenFile: (path) => { if (!state.decoy) openViewer({ path }); },
  });
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
    // truthy but says nothing. `features.harness === false` was therefore
    // false, the probes ran, and an install with the harness switched off
    // answered 404 on every unlock. The requests are
    // harmless; the console errors the browser writes for them are not — they
    // are the first thing anyone looks at when something else breaks.
    ensureFeatures().finally(async () => {
      // Still non-blocking for first paint (this whole chain is detached);
      // awaited only so a `#tool=<id>` deep link to a builtin sees its
      // feature flag before it tries to open it.
      await Promise.allSettled([
        refreshHarnessFeature(),
        refreshStudioForgeFeature(),
        refreshMailFeature(),
        refreshClientsFeature(),
        loadTools(),
      ]);
      if (!state.decoy) openFromHash();
    });
  } else {
    state.harnessEnabled = false;
    state.studioforgeEnabled = false;
    state.mailEnabled = false;
    state.clientsEnabled = false;
    // Safe Mode still gets the tools the server marked `safe` (the API
    // filters); no deep link here — that is an unlocked convenience.
    loadTools();
  }
  applyAuthChrome();
  renderSidebar();
  // Land on the bots list — nothing preselected; chats load when the user
  // picks a bot.
  clearChatView();
  renderThreads();

  // A device that boots already locked (session cookie gone, server session
  // expired) never went through goSafe, so the unlocked tier's drafts, queued
  // sends and cached rows would stay on disk indefinitely. Same promise as
  // goSafe: a locked session deletes what the unlocked one left.
  if (state.decoy && state.auth.pinSet) {
    try { await ensureLocalStore().wipeTier(TIER_FULL); } catch { /* best effort */ }
  }
  // BEFORE the socket: restored outbox frames have to be in pendingSends by the
  // time the first 'open' fires, so the ordinary replay path sends them and
  // nothing needs a second delivery mechanism.
  await restoreLocalState();

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
  // Tells the index.html fail-safe that boot reached its end, so it stops
  // watching and never uncovers a half-built shell.
  window.__bootDone = true;
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

/** Re-ask for the auth state shortly, after a failed check.
 *
 *  The failed check assumed "locked", which is the safe direction but also the
 *  wrong one for an operator whose network merely blinked -- without this they
 *  would sit in Safe-Mode chrome until something else happened to re-check.
 *  One delayed retry, and only if the answer actually differs is anything
 *  re-rendered.
 */
let _authRecheckTimer = null;
function authRecheckSoon(delayMs = 3000) {
  if (_authRecheckTimer) return;
  _authRecheckTimer = setTimeout(async () => {
    _authRecheckTimer = null;
    let s;
    try { s = await api.authStatus(); } catch { authRecheckSoon(8000); return; }
    const wasDecoy = state.decoy;
    state.auth.pinSet = !!s.pin_set;
    state.auth.authenticated = !!s.authenticated;
    state.decoy = state.auth.pinSet && !state.auth.authenticated;
    if (state.decoy !== wasDecoy) {
      applyAuthChrome();
      renderSidebar();
    }
  }, delayMs);
}

async function refreshAuthAndBoot() {
  let s;
  try { s = await api.authStatus(); }
  catch (e) {
    toast(t('toast.auth_failed', { error: e.message }), true);
    // FAIL CLOSED. This used to synthesise {pin_set: false, authenticated:
    // true} -- "there is no PIN anywhere and you are fully unlocked" -- which
    // is the most permissive answer available, chosen for the one case where
    // we know the least. On an Android cold start the radio is often still
    // reconnecting, so this is the ordinary path, not an exotic one.
    //
    // It also disabled its own repair: two separate self-heals (the WS
    // hello/bots handler and the visibilitychange re-check) are gated on
    // `state.auth.pinSet`, so declaring pinSet false switched BOTH off and
    // nothing forced another look.
    //
    // The server never trusted any of this -- every route and every broadcast
    // gates on the real session cookie -- so no conversation was served. What
    // was wrong was the CHROME: a device the server treats as Safe Mode drew
    // the File Server button and the admin settings tabs.
    //
    // Assume locked instead, and let the retry below correct it.
    s = { pin_set: true, authenticated: false };
    authRecheckSoon();
  }
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
  // Suppress the Today / Older section heads while the search modal is up
  // (the threads list is visible behind it on desktop, so a single Older-
  // thread hit could otherwise appear under a misleading "Older" header).
  if (dom['threads']) renderThreads();
  setTimeout(() => dom['search-input'].focus(), 30);
}
function closeSearch() {
  dom['search-backdrop'].classList.add('hidden');
  // The thread list is visible behind the search modal on wide viewports,
  // and section heads are suppressed while the modal is open. Re-render now
  // so the heads return the moment the user closes search — without this,
  // the list stays flat until the next WS frame or user action repaints it.
  if (dom['threads']) renderThreads();
}
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
const TX_KINDS = [['text', 'transcript.filter_text', RAIL_ICONS.messages], ['note', 'transcript.filter_note', RAIL_ICONS.rename],
  ['user', 'transcript.filter_user', RAIL_ICONS.user], ['tool', 'transcript.filter_tool', RAIL_ICONS.tools],
  ['tool_result', 'transcript.filter_tool_result', RAIL_ICONS['viewer-download']],
  ['thinking', 'transcript.filter_thinking', RAIL_ICONS.brain]];
let txState = { items: [], filter: null, threadId: null, sessionKey: null, botId: null, missing: 0 };
function txDefaultFilter() { return new Set(TX_KINDS.map(([k]) => k)); }

async function openThreadTranscript(threadId) {
  if (state.decoy) { toast(t('toast.not_available'), true); return; }  // neutral: no lock hint in Safe Mode
  const th = state.threads.find((x) => x.id === threadId) || state.activeThread;
  const botId = (th && th.bot_id) || state.selectedBotId;
  setI18nText(dom['tx-title'].querySelector('[data-i18n]') || dom['tx-title'], 'transcript.title');
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
  for (const [k, labelKey, icon] of TX_KINDS) {
    const chip = el('button', { class: 'tx-chip' + (txState.filter.has(k) ? ' on' : '') }, iconLabel(icon, t(labelKey)));
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
  setI18nText(dom['tx-title'].querySelector('[data-i18n]') || dom['tx-title'], 'sessions.title', { name: (bot && bot.name) || botId });
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
// Two of these reuse a key that already says exactly the right word in all
// eight locales (nav.settings "Settings", nav.theme "Theme") — a second key
// with identical text is just another thing to keep translated. The rest are
// new nav.label_* keys, short on purpose.
const RAIL_LABELS = {
  'manage-bots': 'nav.settings',
  'theme-toggle': 'nav.theme',
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

// Paths the operator has opened in the local viewer, newest first, so the
// second visit to a report is a keystroke instead of a retyped path. Per
// device (like every other preference here) and on the privacy wipe list —
// a list of host paths says plenty about what this machine does.
const VIEWER_RECENT_KEY = 'dispatch-viewer-recent';
const VIEWER_RECENT_MAX = 8;
function viewerRecent() {
  // Safe Mode never reads it back: the palette entry does not exist there, and
  // a locked device must not be able to surface host paths from storage.
  if (state.decoy) return [];
  try {
    const raw = JSON.parse(localStorage.getItem(VIEWER_RECENT_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter((p) => typeof p === 'string' && p).slice(0, VIEWER_RECENT_MAX) : [];
  } catch { return []; }
}
function rememberViewerPath(path) {
  const p = String(path || '').trim();
  if (!p) return;
  const next = [p, ...viewerRecent().filter((x) => x !== p)].slice(0, VIEWER_RECENT_MAX);
  try { localStorage.setItem(VIEWER_RECENT_KEY, JSON.stringify(next)); } catch { /* storage off */ }
}
/** Ask for a path, remember it, open it. Shared by the palette entry and its
 *  recent-path rows so "remember" can never be attached to one and not the other. */
async function openLocalPath(path) {
  if (state.decoy) return;
  let target = path;
  if (!target) {
    target = await uiPrompt(t('cmdk.open_local_prompt'));
    if (!target) return;
  }
  target = String(target).trim();
  if (!target) return;
  rememberViewerPath(target);
  openViewer({ path: target });
}
function cmdkActions() {
  const a = [
    { icon: RAIL_ICONS.plus, label: t('cmdk.action_new_chat'), run: () => newChat() },
    { icon: RAIL_ICONS.palette, label: t('cmdk.action_theme'), run: () => openSettingsTab('theme') },
  ];
  if (!state.decoy) {
    a.push({ icon: RAIL_ICONS.search, label: t('cmdk.action_search'), run: () => openSearch() });
    a.push({ icon: RAIL_ICONS.lifering, label: t('cmdk.action_recovery'), run: () => openRecovery() });
  }
  if (!state.decoy) {
    // The Settings tabs and the File Server are unlocked-only surfaces, so
    // the palette offers them only where the rail would — a locked device
    // must get no hint that they exist.
    a.push({ icon: RAIL_ICONS.gear, label: t('cmdk.action_settings'), run: () => openSettingsTab('device') });
    a.push({ icon: RAIL_ICONS.pulse, label: t('cmdk.action_health'), run: () => openSettingsTab('health') });
    a.push({ icon: RAIL_ICONS.folder, label: t('cmdk.action_files'), run: () => openFileServer() });
    // Open a path on the DisPatch host in the viewer. Unlocked only, with the
    // rest of the admin surface: Safe Mode gets no hint the feature exists.
    a.push({ icon: RAIL_ICONS.eye, label: t('cmdk.open_local'), run: () => openLocalPath() });
    for (const p of viewerRecent()) {
      a.push({ icon: RAIL_ICONS.eye, label: p, sub: t('cmdk.open_local'), run: () => openLocalPath(p) });
    }
  } else {
    a.push({ icon: RAIL_ICONS.upload, label: t('cmdk.action_send_file'), run: () => openDrop() });
  }
  // canStopReply() already carries the Safe-Mode rule and the "is anything
  // running" one, so the palette cannot offer a Stop the composer would not.
  if (canStopReply()) a.push({ icon: RAIL_ICONS.stop, label: t('cmdk.action_stop'), run: () => stopReply() });
  if (state.auth && state.auth.pinSet && !state.decoy) a.push({ icon: RAIL_ICONS.lock, label: t('cmdk.action_lock'), run: () => lockNow() });
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
  for (const th of state.threads) { const title = threadTitle(th); if (m(title)) items.push({ icon: RAIL_ICONS.messages, label: title, sub: t('cmdk.sub_chat'), group: t('cmdk.group_chats'), run: () => openThread(th.id) }); }
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
      el('span', { class: 'ic' }, [Array.isArray(it.icon) ? railIcon(it.icon) : document.createTextNode(it.icon || '')]),
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
  const cmdkDlg = $('cmdk');
  if (cmdkDlg && cmdkDlg.open) { cmdkItems = cmdkBuild($('cmdk-input').value); cmdkRender(); }
}

async function init() {
  // Translations FIRST: every renderer below reads t() synchronously, so the
  // dictionaries have to be in memory before the first paint. i18n.init() is
  // time-boxed internally, so a hung fetch degrades to English rather than
  // holding the boot veil up.
  await i18nInit();
  // Expose openThread + setView + unmountJobs for cross-module callers
  // (the jobs board title links dispatch into the same composer/mobile-tab
  // flow that built-in thread rows use; without __openThread, clicking a
  // job silently does nothing. __setView lets the board swap the chat
  // panel back to the messages list before the thread opens, otherwise
  // the toolbar stays painted under the job card).
  window.__openThread = openThread;
  window.__setView = setView;
  window.unmountJobs = unmountJobs;
  // Open the job detail modal from anywhere — the Jobs board list uses
  // this to surface each card's full metadata + voting controls.
  window.__openJobDetail = openJobDetail;
  // job-thread.js reports vote/feedback errors through window.toast rather
  // than importing main.js (which would pull in the entire app graph for a
  // one-line call) — without this assignment those toasts were silently
  // dropped (window.toast was never set, so `typeof window.toast ===
  // 'function'` was always false and every job-panel error vanished).
  window.toast = toast;
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
  // The local viewer, same shape: it holds no app state, it just needs the
  // app's toast and a live read of the tier. The default fetch-based api is
  // fine — /api/local/{stat,ls} are plain GETs with no client-side state.
  installViewerHandlers({ onToast: toast, isDecoy: () => state.decoy });
  // The viewer's feature-off pane offers a way to Settings rather than telling
  // the operator to go find it. It cannot import openSettingsTab (that would be
  // a cycle back into main.js), so it asks by event and this is the answer.
  document.addEventListener('dispatch:open-settings', (ev) => {
    const tab = (ev.detail && ev.detail.tab) || 'device';
    closeViewer();          // Settings is a modal too — do not stack them
    openSettingsTab(tab);
  });
  wireAuthEvents();
  wireHarnessView();          // harness pane buttons (start/stop/restart/model/jobs) — orphaned by the 2026-09-10 terminal-pane removal
  wireStudioForgeView();      // sister: studioforge-back button. same regression. wired here so neither pane is read-only.
  wireMailView();
  wireClientsView();
  wireRecoveryUi();
  setOnLocked(() => handleLocked());
  // jobs(unmount) backstop — see also setView(). A MutationObserver on
  // #app watches data-view and tears the board down when it flips OFF
  // jobs. The primary path is the else-branch in setView() and the
  // classic click to setView('chat'); this catches the rest:
  //   * a future refactor that mutates data-view directly,
  //   * a boot-time data-view=jobs written by a hash router that never
  //     existed but might, and
  //   * any code path that forgets the explicit unmount (the symptom the
  //     lead reported on 2026-09-15 — toolbar painted across view
  //     changes).
  // Cheap: one attribute, no subtree.
  if ('MutationObserver' in window && dom.app) {
    let lastView = dom.app.dataset.view || '';
    new MutationObserver(() => {
      const v = dom.app.dataset.view || '';
      if (v === lastView) return;
      lastView = v;
      if (v !== 'jobs') {
        // Same teardown as setView else branch.
        unmountJobs();
        if (dom['job-board-host']) dom['job-board-host'].hidden = true;
      }
    }).observe(dom.app, { attributes: true, attributeFilter: ['data-view'] });
  }
  // Periodic re-render: unread dots flip red at the 24h mark, and the thread
  // list's relative timestamps ("5m") drift.
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
