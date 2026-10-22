// People & Access -> one profile's permissions: toggles from the role table (items the
// role can never have are not offered), the agent-bot warning, the lock-target fence
// (agents and voice cloning shown locked, with the reason), household bots (all / pick)
// and allowed backends. Owned by WP-FE-PEOPLE.
//
// Foundation scaffolding: wired once (main.js calls initPeoplePerms() at boot;
// settings-nav.js SECTION_HOOKS runs renderPeoplePerms() when People mounts), does
// nothing yet. Nothing runs at import time.

/** Called once at boot with the shell's helpers. */
export function initPeoplePerms(deps = {}) {
  void deps;
}

/** Add the permissions UI to the People pane `host`. */
export function renderPeoplePerms(host, deps = {}) {
  void host; void deps;
}
