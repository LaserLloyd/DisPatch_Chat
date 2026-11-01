// Two small character helpers shared by the Characters page and the copy menu (4.3: what was left
// of js/characters.js once the old Settings roster went). Pure: no DOM, no fetch, no imports.

/** Is the character kept off the bot lists? (`hidden` alone: the 3.x `hide_unlocked` /
 *  `audience` keys are inert data since migration 010 and never read here.) */
export function isHidden(bot) {
  if (!bot) return false;
  if (typeof bot.hidden === 'boolean') return bot.hidden;
  return !!(bot.flags && bot.flags.hidden === true);
}

/** Hash the voice section may read to open on this character's voice. Documented in docs/characters.md. */
export const voiceHash = (id) => `#voice/voices/${encodeURIComponent(id)}`;
