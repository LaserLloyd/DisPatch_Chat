// No-Image Mode — render the app with no pictures at all.
//
// Not dimmed, not placeholdered, not lazy-loaded: OMITTED. Chat media, avatars
// and reaction images are all absent, and a message whose only content was a
// picture leaves no row behind.
//
// The important half is that nothing is FETCHED. A CSS `display: none` would
// hide every image while still downloading it, which for a feature whose whole
// claim is "these are not here" would be the dishonest implementation. So NIM
// rides the same chokepoint Safe Mode uses — renderMarkdown's `noMedia`, which
// strips media from the markdown SOURCE before any innerHTML assignment — and
// the browser never issues the request.
//
// What it deliberately does NOT do: hide anything from the server, or from
// anyone who opens devtools. See canDisableNim() below.

const FLAG_KEY = 'dispatch-nim';

// The live answer, mirrored in memory.
//
// localStorage can REFUSE a write — a browser with site data blocked for this
// origin throws on setItem — and the catch below cannot un-apply the CSS that
// has already been applied. That left the worst possible state: data-nim="on"
// hiding every picture while nimEnabled() read false, so every image was still
// FETCHED and merely hidden. Exactly the dishonest implementation this module
// exists to avoid. The mirror is the truth for this session; storage is how it
// survives a reload, and initNim() reconciles the two on the way in.
let active = null;

export function nimEnabled() {
  if (active !== null) return active;
  try { return localStorage.getItem(FLAG_KEY) === '1'; } catch { return false; }
}

/** Turn it on or off.
 *
 *  Enabling never needs a reload: the strip happens at render time, so the
 *  caller re-rendering the open thread is enough. Disabling is the same in
 *  reverse. The <html> attribute drives the CSS half (avatars) and is also
 *  what the no-FOUC <head> script sets, so the two paths agree.
 */
export function setNim(on) {
  active = !!on;
  try { localStorage.setItem(FLAG_KEY, on ? '1' : '0'); } catch { /* storage off */ }
  applyNim(active);
}

/** Set the two <html> attributes NIM drives.
 *
 *  NIM subsumes the Minimal-avatars setting by borrowing its ATTRIBUTE rather
 *  than duplicating ~20 CSS selectors (including the mobile-rail block inside a
 *  media query, which a copy would silently miss). The stored preference key is
 *  never written, so switching NIM off restores whatever the user actually
 *  chose — which is the whole reason to drive the attribute and not the key.
 */
function applyNim(on) {
  const html = document.documentElement;
  if (on) {
    html.setAttribute('data-nim', 'on');
    html.setAttribute('data-avatar-style', 'minimal');
    return;
  }
  html.removeAttribute('data-nim');
  let pref = null;
  try { pref = localStorage.getItem('dispatch-avatar-style'); } catch { /* storage off */ }
  if (pref === 'minimal') html.setAttribute('data-avatar-style', 'minimal');
  else html.removeAttribute('data-avatar-style');
}

/** Apply the stored flag to the DOM. The <head> script already did this before
 *  first paint; this exists so a storage-disabled browser still converges, and
 *  so boot order can't leave the attribute and the key disagreeing. */
export function initNim() {
  active = null;                 // re-read storage: a new page load is the
  const on = nimEnabled();       // only moment storage can be authoritative
  active = on;
  applyNim(on);
  return on;
}

/** May this session switch NIM OFF?
 *
 *  The point of the ratchet: enable NIM, hand someone the tablet, and no
 *  picture can come back without the PIN. Turning it ON is always allowed —
 *  a control that refuses to make things stricter would be absurd.
 *
 *  `decoy` is main.js's state.decoy, itself `pinSet && !authenticated`, so ONE
 *  check covers both agreed rules: an unlocked session may disable (being
 *  unlocked IS the credential — no second prompt), and a device with no PIN at
 *  all may disable (there is no credential to demand).
 *
 *  This is a UX gate, NOT a security boundary. The flag is localStorage and
 *  anyone with devtools can flip it. It stops a handed-over tablet; it does not
 *  stop a determined adult, and the UI copy must never imply otherwise. The
 *  real server-side protection is Safe Mode, which redacts decoy traffic.
 */
export function canDisableNim(decoy) {
  return !decoy;
}

/** Is a message nothing but pictures?
 *
 *  Such a row is dropped entirely in NIM — sender, timestamp and all — because
 *  an empty bubble is still a visible trace of the image. The message is of
 *  course still on the server and returns when NIM goes off.
 *
 *  THE SHAPE MATTERS, and the first version of this function got it wrong: it
 *  looked for `msg.attachments` / `msg.metadata.attachments`, and DisPatch has
 *  no such field. MessageOut (backend/app/models.py) is
 *  {id, thread_id, role, content, created_at, media_url, metadata} — media
 *  travels as `media_url` or as `[[media:…]]` / `![](…)` inside `content`.
 *  So the check could never return true and picture-only rows were never
 *  dropped. The unit tests did not catch it because they were written against
 *  the same imagined shape, so they tested a fiction and passed.
 *
 *  A non-image attachment keeps its row: `[[doc:…]]` directives are NOT
 *  stripped by stripMediaSource, so they survive as text and the first check
 *  below returns false — which is the behaviour we want, expressed as a
 *  consequence of how the strip works rather than as a separate special case.
 */
export function isMediaOnly(msg, stripSource) {
  if (!msg) return false;
  const raw = msg.content || '';
  const stripped = stripSource ? stripSource(raw) : raw;
  // Anything readable left over — prose, a [[doc:…]] card, a caption — means
  // this message is not purely a picture.
  if (stripped.trim()) return false;

  // No text. Now require a POSITIVE media signal, so a genuinely empty message
  // is left to whatever already handles empty messages rather than being
  // silently eaten by this feature.
  if (msg.media_url) return true;
  if (stripSource && stripped !== raw) return true;   // had [[media:…]] / ![](…)

  // Forward-compatible: if an attachments array ever appears, honour it.
  const atts = (msg.metadata && msg.metadata.attachments) || msg.attachments || [];
  if (!atts.length) return false;
  return atts.every((a) => {
    const mime = String((a && (a.mime || a.type)) || '');
    return mime.startsWith('image/') || mime.startsWith('video/');
  });
}

/** Should this message leave NO row at all in No-Image Mode?
 *
 *  Two kinds of message are pure image footprint:
 *
 *   - a picture-only message (isMediaOnly above), and
 *   - a REACTION TRACE. A reaction is a picture; its trace message exists only
 *     to caption one. Declining to build the special trace row is not enough —
 *     the message then falls through to ordinary system rendering and shows its
 *     raw content ("⚡ <bot> reacted · Check In") in a bubble, which is exactly
 *     the visible trace NIM removes. That happened in use: the pictures were
 *     gone and their captions were still lining the thread.
 *
 *  One predicate so both callers and the tests agree on what "gone" means.
 */
export function shouldDropMessage(msg, stripSource) {
  if (!msg) return false;
  if (msg.metadata && msg.metadata.kind === 'reaction') return true;
  return isMediaOnly(msg, stripSource);
}

/** The Settings row. Self-contained, like privacyRow(), so the settings modal
 *  needs to know nothing about how any of this works.
 *
 *  `decoy` decides whether the box is operable; `onChange` re-renders. When it
 *  is locked we still show it CHECKED — hiding the control would leave the user
 *  wondering why there are no pictures.
 *
 *  `pinSet` picks the hint. Three states, three sentences: locked ("unlock with
 *  your PIN"), PIN set ("once on, it takes the PIN to turn off"), and no PIN at
 *  all — where the ratchet is vacuous (canDisableNim returns true with no PIN)
 *  and promising a PIN gate would be a lie. That third string (nim.hint_nopin)
 *  was translated into all eight locales and then never rendered.
 */
export function nimRow(t, { decoy = false, pinSet = true, onChange } = {}) {
  const on = nimEnabled();
  const locked = on && !canDisableNim(decoy);
  const label = document.createElement('label');
  label.className = 'bm-avatar-style';
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.id = 'nim-toggle';
  box.checked = on;
  box.disabled = locked;
  const text = document.createElement('span');
  const strong = document.createElement('strong');
  strong.textContent = t ? t('nim.title') : 'No-Image Mode';
  const hintKey = locked ? 'nim.hint_locked' : (pinSet ? 'nim.hint' : 'nim.hint_nopin');
  const hintEn = locked
    ? ' — pictures are hidden. Unlock with your PIN to turn this off.'
    : ' — hide every picture: chat images, avatars and reactions. Nothing is'
      + ' downloaded.' + (pinSet
        ? ' Once on, it takes the PIN to turn off.'
        : ' Set a PIN to stop this being switched off.');
  text.append(strong, document.createTextNode(t ? ` — ${t(hintKey)}` : hintEn));
  label.append(box, text);
  box.addEventListener('change', () => {
    setNim(box.checked);
    if (onChange) onChange(box.checked);
  });
  return label;
}
