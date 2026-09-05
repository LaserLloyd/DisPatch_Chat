// Privacy mode — make this device hold nothing locally.
//
// For a shared tablet, or a phone that travels. When it is on, the client keeps
// no application cache, no preferences that survive the tab, no drafts, and no
// persistent session cookie.
//
// What it deliberately does NOT do: hide anything from the server. The server
// still stores the full history — deleting that is what the delete controls are
// for. Claiming otherwise would be the dishonest version of this feature.
//
// The setting itself is the one thing that must persist, or turning it on would
// forget itself on reload and the device would silently start caching again.
// It is stored alone, under a key that reveals nothing about the account.

const FLAG_KEY = 'dispatch-privacy';

// App keys wiped when the tab goes away. Anything the app persists belongs
// here; a key not listed simply survives, which is the failure this list
// exists to prevent.
//
// DERIVE THIS LIST, DO NOT GUESS AT IT. Before editing, run:
//
//     grep -rn "localStorage.setItem" frontend/static/js frontend/static/index.html
//
// and reconcile — every key that appears there except FLAG_KEY belongs below.
// The list had drifted both ways: three keys that nothing has ever written
// (dispatch-draft, dispatch-thread-collapsed, dispatch-last-bot) padded it out
// while two real ones were missing, so they survived the purge — including
// `lc-remember`, the remembered-unlock preference, which is the single worst
// thing to leave behind on a shared tablet.
// It drifted AGAIN after that, which is why frontend/tests/privacy-keys.test.js
// now derives the writer list mechanically and fails on anything missing:
// pins.js shipped 'dispatch-pinned-settings' and nobody added it here, so a
// wiped device still advertised which settings the last user had pinned.
export const APP_KEYS = [
  'dispatch-theme',           // theme.js
  'dispatch-avatar-style',    // main.js (Settings → minimal avatars)
  'dispatch-lang',            // i18n.js
  'dispatch-pinned-settings', // pins.js (which settings are on the rail)
  'dispatch-custom-links',    // links.js (custom link buttons on the rail)
  'dispatch-menu-bots',       // menubots.js (which bots were parked in the ⌥ menu)
  'dispatch-viewer-recent',   // main.js (recent local-viewer paths in the palette)
  'tl-collapsed',             // main.js (thread list collapsed, desktop)
  'lc-remember',              // main.js (unlock keypad "keep this device unlocked")
  // DELIBERATELY ABSENT: 'dispatch-nim'. No-Image Mode is a RATCHET — once on,
  // it takes the PIN to turn off (js/nim.js canDisableNim). Wiping it here
  // would make "clear this device" a one-click way around that gate on a
  // handed-over tablet, which is the exact scenario NIM exists for. It is not
  // an oversight; do not "fix" it by adding the key.
];

export function privacyEnabled() {
  try { return localStorage.getItem(FLAG_KEY) === '1'; } catch { return false; }
}

/** Drop the offline application cache and stop the service worker.
 *
 *  The worker only ever caches the app shell (never messages or media — see
 *  sw.js), but the shell still shows that DisPatch was used on this device,
 *  and an installed PWA keeps working offline, which is itself a trace.
 */
async function purgeServiceWorker() {
  try {
    if ('caches' in window) {
      const names = await caches.keys();
      await Promise.all(names.map((n) => caches.delete(n)));
    }
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map((r) => r.unregister()));
    }
  } catch { /* best effort: a browser that refuses is not a reason to fail */ }
}

function wipeAppKeys() {
  try {
    for (const k of APP_KEYS) localStorage.removeItem(k);
  } catch { /* storage disabled entirely — nothing to wipe */ }
}

/** Wipe on the way out.
 *
 *  `pagehide` rather than `beforeunload`: it is the only event that reliably
 *  fires when a mobile browser discards a backgrounded tab, which is exactly
 *  how a phone usually "closes" an app. `visibilitychange` would fire on every
 *  app switch and wipe the theme while the user is still using it.
 */
function armWipe() {
  if (armWipe._armed) return;
  armWipe._armed = true;
  window.addEventListener('pagehide', wipeAppKeys);
}

/** Call once at boot, before anything reads a preference. */
export async function initPrivacy() {
  if (!privacyEnabled()) return false;
  await purgeServiceWorker();
  armWipe();
  document.documentElement.setAttribute('data-privacy', 'on');
  return true;
}

/** Turn it on or off. Returns true when the page must reload to take effect. */
export async function setPrivacy(on) {
  try { localStorage.setItem(FLAG_KEY, on ? '1' : '0'); } catch { return false; }
  if (on) {
    await purgeServiceWorker();
    armWipe();
    wipeAppKeys();          // don't wait for the tab to close to clear history
    document.documentElement.setAttribute('data-privacy', 'on');
    return false;
  }
  document.documentElement.removeAttribute('data-privacy');
  // Re-registering the worker needs a fresh page load; the caller reloads.
  return true;
}

/** Should this session be remembered across restarts?
 *
 *  Consulted by the unlock flow. In privacy mode a persistent cookie is exactly
 *  the artefact we are avoiding, so the "keep me signed in" option is refused
 *  rather than merely hidden — hiding a control that still works is how a
 *  privacy feature ends up lying.
 */
export function allowsPersistentSession() {
  return !privacyEnabled();
}

/** The Settings row. Self-contained so it can be appended by the settings
 *  modal without that modal needing to know how any of this works. */
export function privacyRow(t) {
  const label = document.createElement('label');
  label.className = 'bm-avatar-style';
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.id = 'privacy-toggle';
  box.checked = privacyEnabled();
  const text = document.createElement('span');
  const strong = document.createElement('strong');
  strong.textContent = t ? t('privacy.title') : 'Privacy mode';
  text.append(strong, document.createTextNode(
    t ? ` — ${t('privacy.hint')}`
      : ' — keep nothing on this device: no offline cache, no saved settings,'
        + ' no staying signed in. Slower to start, and no offline access.'));
  box.addEventListener('change', async () => {
    const needsReload = await setPrivacy(box.checked);
    if (needsReload) location.reload();
  });
  label.append(box, text);
  return label;
}
