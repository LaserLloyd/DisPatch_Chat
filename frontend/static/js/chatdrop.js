// Drop files on an open chat: choose "Insert into chat" or "Upload to File Server".
//
// Dragging FILES (never text or a link) over the chat view shows an overlay with
// two targets. "Insert into chat" is the paperclip's flow (main.js handleFiles:
// the same caps, the upload, the composer preview; the person still presses
// send). "Upload to File Server" is the File Server's own upload for a profile
// that has it. A target the profile may not use is not drawn; with
// neither there is no overlay at all. Dropping anywhere but on a target, or
// Escape, cancels.
//
// Flicker: dragenter/dragleave fire for every child the pointer crosses, so the
// overlay counts them (a depth counter) instead of trusting one dragleave.
// Touch screens never drag files, so phones see nothing new.

import { el, railIcon, RAIL_ICONS } from './util.js?v=21';
import { t } from './i18n.js?v=4';

/** Is this a drag of files (not text, a link or an element of the page)? */
export function dragHasFiles(e) {
  const types = e && e.dataTransfer && e.dataTransfer.types;
  if (!types) return false;
  return Array.from(types).includes('Files');
}

/** Which targets to draw: ['insert'] / ['server'] / both / []. */
export function dropTargets({ canInsert, canServer }) {
  return [canInsert ? 'insert' : null, canServer ? 'server' : null].filter(Boolean);
}

/**
 * opts:
 *   host         the chat view element the overlay covers
 *   canInsert()  may files be attached here now (a thread is open, upload allowed)
 *   canServer()  'fileserver' | 'send' | '' -- which store-on-host path this profile has
 *   onInsert(files), onServer(files)
 */
export function createChatDrop({ host, canInsert, canServer, onInsert, onServer }) {
  let depth = 0;
  let overlay = null;
  let cancelled = false;
  let hot = null;

  const hide = () => {
    if (overlay) { overlay.remove(); overlay = null; }
    hot = null;
  };
  const reset = () => { depth = 0; cancelled = false; hide(); };

  const target = (kind, server) => {
    const isInsert = kind === 'insert';
    const send = !isInsert && server === 'send';
    const title = t(isInsert ? 'chatdrop.insert_title' : send ? 'chatdrop.send_title' : 'chatdrop.server_title');
    const sub = t(isInsert ? 'chatdrop.insert_sub' : send ? 'chatdrop.send_sub' : 'chatdrop.server_sub');
    return el('div', { class: `chat-drop-target chat-drop-${kind}`, dataset: { dropTarget: kind }, role: 'group', 'aria-label': title }, [
      el('span', { class: 'chat-drop-icon', 'aria-hidden': 'true' }, [railIcon(isInsert ? RAIL_ICONS.attach : RAIL_ICONS.folder)]),
      el('strong', { class: 'chat-drop-title', text: title }),
      el('span', { class: 'chat-drop-sub', text: sub }),
    ]);
  };

  const show = () => {
    if (overlay) return true;
    const server = canServer() || '';
    const kinds = dropTargets({ canInsert: !!canInsert(), canServer: !!server });
    if (!kinds.length) return false;
    overlay = el('div', { class: 'chat-drop', role: 'region', 'aria-label': t('chatdrop.region'), 'aria-live': 'polite' }, [
      el('div', { class: 'chat-drop-targets' }, kinds.map((k) => target(k, server))),
      el('p', { class: 'chat-drop-hint', text: t('chatdrop.cancel_hint') }),
    ]);
    host.append(overlay);
    return true;
  };

  const mark = (e) => {
    if (!overlay) return;
    const tgt = e.target && e.target.closest ? e.target.closest('[data-drop-target]') : null;
    if (tgt === hot) return;
    if (hot) hot.classList.remove('is-hot');
    hot = tgt;
    if (hot) hot.classList.add('is-hot');
  };

  host.addEventListener('dragenter', (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    if (!cancelled) show();
    mark(e);
  });
  host.addEventListener('dragover', (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();       // or the browser opens the file in the tab
    if (e.dataTransfer) {
      const tgt = overlay && e.target.closest ? e.target.closest('[data-drop-target]') : null;
      try { e.dataTransfer.dropEffect = tgt ? 'copy' : 'none'; } catch { /* read-only in some engines */ }
    }
    mark(e);
  });
  host.addEventListener('dragleave', (e) => {
    if (!dragHasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) reset();
  });
  host.addEventListener('drop', (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    const tgt = overlay && e.target.closest ? e.target.closest('[data-drop-target]') : null;
    const kind = tgt ? tgt.dataset.dropTarget : null;
    const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
    reset();
    if (!files.length || !kind) return;          // outside both targets = cancel
    if (kind === 'insert') onInsert(files);
    else onServer(files);
  });
  const onKey = (e) => {
    if (e.key !== 'Escape' || !overlay) return;
    e.preventDefault();
    e.stopPropagation();
    cancelled = true;     // the rest of this drag stays cancelled
    hide();
  };
  window.addEventListener('keydown', onKey, true);
  window.addEventListener('dragend', reset);
  window.addEventListener('drop', () => { if (depth) reset(); });

  return { isOpen: () => !!overlay, reset };
}
