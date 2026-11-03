// "Make my own copy" of a household bot (the chat header menu, through copyMenuItem) and the
// pure rules behind it. 4.0 chat-first profiles, BUILD-PLAN-v2 §1.3; the profile's own bots are
// listed and edited on the Characters page (js/characters-page.js, Mine tab) since 4.2.
//
//   POST   /api/characters/{id}/clone           -> {bot, voice_cleared}   (agent bots: 422 not_copyable)
//
// main.js calls initMyBots() once at boot (it also hands the shell to js/bot-editor.js).
// Nothing runs at import time.

import { t } from './i18n.js?v=5';
import { el } from './util.js?v=23';
import { can, profileReq, withShell, initBotEditor } from './bot-editor.js?v=6';

let shell = {};
/** Called once at boot with the shell's helpers ({ t, toast, confirm, api, state }). */
export function initMyBots(deps = {}) {
  shell = deps || {};
  initBotEditor(deps);
}

// ------------------------------------------------------------------ pure helpers

/** May this household/own bot row be copied into the caller's profile? Agent bots never;
 *  a personal bot is already someone's own. */
export function isCopyable(bot) {
  if (!bot || !bot.id) return false;
  if (bot.scope === 'personal') return false;
  // /api/me/characters rows carry backend_kind; /api/characters rows (the plain public bot
  // record) carry `agent` (an OpenClaw seat) and `advisor` / `api_provider` (a direct bot).
  const kind = bot.backend_kind || (bot.backend && bot.backend.kind) || (typeof bot.backend === 'string' ? bot.backend : '');
  if (kind) return kind === 'direct' || kind === 'legacy-api';
  if (bot.agent) return false;
  return !!(bot.advisor || bot.api_provider);
}

/** The "Make my own copy" menu entry is shown to a profile that may make bots, for a bot
 *  it can copy, and not for one it already copied (`cloned_from` matches) - agent bots
 *  never get it. */
export function canCopyBot(bot, deps = {}, mine = []) {
  if (!isCopyable(bot) || !can('create_characters', withShell(deps))) return false;
  return !mine.some((m) => m && m.cloned_from === bot.id);
}

/** Is this row kept up to date by the household (4.1 managed bots)? */
export const isManaged = (bot) => !!(bot && bot.managed);

// ------------------------------------------------------------------ actions

/** Copy household bot `bot` into the caller's profile. Resolves to the new bot row, or
 *  null after showing why not. */
export async function copyHouseholdBot(bot, deps = {}) {
  const d = withShell({ ...shell, ...deps });
  if (!isCopyable(bot)) { d.toast && d.toast(t('mybots.copy_agent_refused')); return null; }
  try {
    const out = await profileReq('POST', `/api/characters/${encodeURIComponent(bot.id)}/clone`);
    if (d.toast) {
      d.toast(t('mybots.copied', { name: out.bot.name }));
      if (out.voice_cleared) d.toast(t('copybot.voice_cleared'));
    }
    return out.bot;
  } catch (e) {
    if (d.toast) d.toast(e.code === 'not_copyable' ? t('mybots.copy_agent_refused') : e.message);
    return null;
  }
}

/** A button for the chat header menu (null when this bot may not be copied by this
 *  profile). `onDone(newBot)` runs after a successful copy. */
export function copyMenuItem(bot, deps = {}, { mine = [], onDone = null, className = '' } = {}) {
  if (!canCopyBot(bot, deps, mine)) return null;
  return el('button', { class: className, type: 'button', role: 'menuitem', id: 'thread-menu-copy',
    text: t('mybots.copy'),
    onclick: async () => { const nb = await copyHouseholdBot(bot, deps); if (nb && onDone) onDone(nb); } });
}
