// The simple personal-bot editor and the AI build flow: name, picture (upload / pick /
// generate when the profile may make images), personality preset or own text (at most
// 4,000 characters), voice (household voices plus the caller's own clones), backend
// (when more than one is allowed). Owned by WP-FE-YOU.
//
// Foundation scaffolding: wired once (main.js calls initBotEditor() at boot), does
// nothing yet. Nothing runs at import time.

/** Called once at boot with the shell's helpers. */
export function initBotEditor(deps = {}) {
  void deps;
}

/** Open the editor for `bot` (null = a new bot) inside `host`. Resolves to the saved
 *  bot, or null when the editor was closed without saving. */
export async function renderBotEditor(host, bot = null, deps = {}) {
  void host; void bot; void deps;
  return null;
}
