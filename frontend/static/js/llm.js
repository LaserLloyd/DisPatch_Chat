// Settings -> AI & Connections: the quick setup and image generation, and the
// first-run "Connect an AI" card.
//
// Same contract as dashboard.js and reactions.js: this module owns no app
// state. main.js hands it a small `ctx` (toast, the bot-selection callback,
// the lock handler) and everything else it needs it fetches itself.
//
// 3.2: the per-bot "Bot to connect" form (provider, base URL, key, model) is
// gone. It described a connection from the character's side while
// Connections described the same server from its own, and nobody could tell
// which one won. Now a server is a connection (Connections), and a character
// picks one in its Details (Characters). What stays here is the sixty-second
// path for a fresh install — one provider, one key, everything set up
// (js/integrations.js mountQuickSetup) — and the image-generation provider.

import { el, railIcon, RAIL_ICONS } from './util.js?v=21';
import { t } from './i18n.js?v=5';
import { mountQuickSetup, mountImageSection } from './integrations.js?v=3';

let ctx = {
  toast: () => {},
  onLocked: () => {},
  onConnected: async () => {},   // main.js: refresh roster, select bot, open chat
  // Settings owns the dialog: these are how this module asks for its section,
  // gets out of the way, and answers "am I the section on screen?".
  openSettingsTab: () => {},
  closeSettings: () => {},
  settingsTabActive: () => false,
  linkTo: null,                  // main.js: an "Open ‹Section›" button (settingsLink)
};

const $ = (id) => document.getElementById(id);

/** Every entry point that used to open the old AI models panel (the first-run
 *  card in an empty chat): it opens Settings -> AI & Connections -> Quick setup. */
export async function openLlmPanel(next = {}) {
  ctx = { ...ctx, ...next };
  ctx.openSettingsTab('connections#quick-setup');
}

/** AI & Connections came on screen: the quick setup and image generation, each
 *  loading its own state (a failure there costs only that block).
 *  `quickOpen`: open the quick setup (the empty state: no connection yet). */
export async function activateLlmPanel(next = {}) {
  ctx = { ...ctx, ...next };
  await mountQuickSetup($('llm-quick-host'), {
    open: !!ctx.quickOpen,
    onConfigured: async (r) => {
      if (r && r.bot) await ctx.onConnected(r.bot, { keepOpen: true });
      mountImageSection($('llm-image-host'), { toast: ctx.toast });
    },
  });
  await mountImageSection($('llm-image-host'), { toast: ctx.toast });
}

/** The id a new bot gets from its name: lowercase, [a-z0-9_-], 1–32 chars. */
export function slugForBot(name) {
  const s = String(name || '').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
  return s || 'assistant';
}

/** "I am done with this screen": close Settings, but only when it is on AI &
 *  Connections (a sign-out teardown must not close somebody else's). */
export function closeLlmPanel() {
  if (llmPanelOpen()) ctx.closeSettings();
}

export function llmPanelOpen() {
  return !!ctx.settingsTabActive('connections');
}

/** Language switch while open: both blocks rebuild from their own state. */
export function repaintLlmPanel() {
  if (!llmPanelOpen()) return;
  activateLlmPanel();
}

// ===================== First-run card =====================

// Rendered into the chat empty state by main.js when there is nothing to talk
// to yet. Returns a node (never touches the DOM itself) so the caller keeps
// full control of when and where it appears.
export function firstRunCard(onOpen) {
  return el('div', { class: 'llm-firstrun' }, [
    el('div', { class: 'empty-emoji' }, [railIcon(RAIL_ICONS.plug)]),
    el('p', { class: 'llm-firstrun-title', 'data-i18n': 'llm.card_title',
      text: t('llm.card_title') }),
    el('p', { class: 'llm-firstrun-body', 'data-i18n': 'llm.card_body',
      text: t('llm.card_body') }),
    // The quickest start: Settings -> AI & Connections, on the one-key box.
    el('button', { class: 'btn-primary', id: 'llm-firstrun-btn',
      'data-i18n': 'llm.card_button', text: t('llm.card_button'),
      onclick: async (e) => {
        await onOpen(e);
        setTimeout(() => { const k = document.getElementById('qs-key'); if (k) k.focus(); }, 400);
      } }),
  ]);
}

// ===================== Wiring =====================

export function initLlmPanel(next = {}) {
  ctx = { ...ctx, ...next };
}
