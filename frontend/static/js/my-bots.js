// Settings -> You -> My bots: the profile's own (personal) bots - list, simple editor,
// AI build, delete, and "Make my own copy" of a household bot (also offered from the chat
// header menu through copyMenuItem). 4.0 chat-first profiles, BUILD-PLAN-v2 §1.3.
// Owned by WP-FE-YOU.
//
//   GET    /api/me/characters                   -> {characters:[{...bot, chats}]}
//   DELETE /api/me/characters/{id}              -> {removed, deleted:{chats, pictures}}
//   GET    /api/characters                      household + own bots this profile may use
//   POST   /api/characters/{id}/clone           -> {bot, voice_cleared}   (agent bots: 422 not_copyable)
//
// main.js calls initMyBots() once at boot; settings-nav.js SECTION_HOOKS runs
// renderMyBots() when the section mounts. Nothing runs at import time.

import { t } from './i18n.js?v=5';
import { el } from './util.js?v=21';
import { isHidden } from './characters.js?v=4';
import {
  can, profileReq, renderAiBuild, renderBotEditor, loadEditorOptions, withShell, initBotEditor,
} from './bot-editor.js?v=1';

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

/** Confirmation text for deleting one of the caller's own bots. */
export function deleteMessage(bot) {
  return t('mybots.delete_confirm', { name: bot.name, count: Number(bot.chats || 0) });
}

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

// ------------------------------------------------------------------ the section

function botRow(bot, ctx) {
  const pic = bot.avatar_url
    ? el('img', { class: 'mb-avatar', src: bot.avatar_url, alt: '', width: '40', height: '40', loading: 'lazy' })
    : el('span', { class: 'mb-avatar mb-avatar-emoji', 'aria-hidden': 'true', text: bot.emoji || '•' });
  const chats = Number(bot.chats || 0);
  return el('li', { class: 'mb-row', dataset: { bot: bot.id } }, [
    pic,
    el('div', { class: 'mb-text' }, [
      el('strong', { class: 'mb-name', text: `${bot.emoji ? `${bot.emoji} ` : ''}${bot.name}` }),
      el('span', { class: 'mb-sub muted', text: bot.description || t('mybots.chats_count', { count: chats }) }),
    ]),
    el('div', { class: 'mb-actions' }, [
      el('button', { class: 'ui-btn', type: 'button', text: t('mybots.edit'), onclick: () => ctx.edit(bot) }),
      el('button', { class: 'ui-btn ui-btn--danger', type: 'button', text: t('mybots.delete'), onclick: () => ctx.remove(bot) }),
    ]),
  ]);
}

/** Fill `host` (the section's pane, or a container inside it). */
export function renderMyBots(host, deps = {}) {
  if (!host) return;
  const d = withShell({ ...shell, ...deps });
  const canMake = can('create_characters', d);
  const list = el('ul', { class: 'mb-list', 'aria-label': t('mybots.title') });
  const copyList = el('ul', { class: 'mb-list mb-copy-list' });
  const copyGroup = el('section', { class: 'dev-group mb-group', hidden: 'hidden' }, [
    el('h3', { class: 'dev-group-title', text: t('mybots.copy') }),
    copyList,
  ]);
  const stage = el('div', { class: 'mb-stage', hidden: 'hidden' });   // editor / AI build live here
  const empty = el('p', { class: 'muted mb-empty', text: t('mybots.empty'), hidden: 'hidden' });
  const banner = el('div', { class: 'yo-error', role: 'alert', hidden: 'hidden' });
  const main = el('div', { class: 'mb-main' });

  let mine = [];
  let household = [];

  const say = (msg) => { if (d.toast) d.toast(msg); else { banner.textContent = msg; banner.hidden = false; } };

  async function load() {
    try {
      const [own, all] = await Promise.all([
        profileReq('GET', '/api/me/characters'),
        profileReq('GET', '/api/characters').catch(() => ({ characters: [] })),
      ]);
      mine = own.characters || [];
      const ownIds = new Set(mine.map((m) => m.id));
      household = (all.characters || all.bots || []).filter((b) => b && b.scope !== 'personal' && !ownIds.has(b.id));
      banner.hidden = true;
    } catch (e) {
      banner.textContent = e.message; banner.hidden = false;
      mine = []; household = [];
    }
    paint();
  }

  function paint() {
    list.replaceChildren(...mine.map((b) => botRow(b, ctx)));
    empty.hidden = mine.length > 0;
    // A household bot an Admin hid is not offered here (hidden = kept off the lists); one on
    // the profile's rail can still be copied from its chat's menu.
    const copyable = household.filter((b) => !isHidden(b) && canCopyBot(b, d, mine));
    copyList.replaceChildren(...copyable.map((b) => el('li', { class: 'mb-row', dataset: { bot: b.id } }, [
      b.avatar_url ? el('img', { class: 'mb-avatar', src: b.avatar_url, alt: '', width: '40', height: '40', loading: 'lazy' })
        : el('span', { class: 'mb-avatar mb-avatar-emoji', 'aria-hidden': 'true', text: b.emoji || '•' }),
      el('div', { class: 'mb-text' }, [el('strong', { class: 'mb-name', text: b.name })]),
      el('div', { class: 'mb-actions' }, [
        el('button', { class: 'ui-btn', type: 'button', text: t('mybots.copy'), onclick: async () => {
          const nb = await copyHouseholdBot(b, d);
          if (nb) await load();
        } }),
      ]),
    ])));
    copyGroup.hidden = copyable.length === 0 || !canMake;
  }

  async function openStage(run) {
    main.hidden = true; stage.hidden = false;
    let saved = null;
    try { saved = await run(stage); } finally {
      stage.hidden = true; stage.replaceChildren(); main.hidden = false;
    }
    if (saved) await load();
  }

  const ctx = {
    edit: (bot) => openStage(async (box) => {
      let full = bot;
      try { full = await profileReq('GET', `/api/me/characters/${encodeURIComponent(bot.id)}`); } catch { /* the list row will do */ }
      return renderBotEditor(box, { ...bot, ...full }, { ...d, householdAvatars: household });
    }),
    create: () => openStage((box) => renderBotEditor(box, null, { ...d, householdAvatars: household })),
    build: () => openStage(async (box) => {
      const options = await loadEditorOptions(d);
      return renderAiBuild(box, { ...d, options, householdAvatars: household });
    }),
    async remove(bot) {
      const ok = d.confirm ? await d.confirm(deleteMessage(bot), { danger: true, okText: t('mybots.delete'), title: bot.name }) : true;
      if (!ok) return;
      try {
        await profileReq('DELETE', `/api/me/characters/${encodeURIComponent(bot.id)}`);
        say(t('mybots.deleted', { name: bot.name }));
        await load();
      } catch (e) { say(e.message); }
    },
  };

  const head = el('div', { class: 'mb-head' }, [
    el('p', { class: 'muted mb-private', text: t('mybots.private_note') }),
    canMake ? el('div', { class: 'mb-toolbar' }, [
      el('button', { class: 'ui-btn ui-btn--primary', type: 'button', id: 'mb-new', text: t('mybots.new'), onclick: () => ctx.create() }),
      el('button', { class: 'ui-btn', type: 'button', id: 'mb-build', text: t('mybots.ai_build'), onclick: () => ctx.build() }),
    ]) : null,
  ]);

  main.append(head, banner, empty, list, copyGroup);
  const group = el('section', { class: 'dev-group mb-section', 'data-anchor': 'mybots', 'aria-labelledby': 'mb-h' }, [
    el('h3', { class: 'dev-group-title', id: 'mb-h', text: t('mybots.title') }),
    main, stage,
  ]);
  host.replaceChildren(group);
  load();
}
