// What the listener did NOT hear of a spoken reply (voice chat, 2.2.0).
//
// When someone talks over a reply in a voice chat, the voice add-on records how
// far it got: `metadata.voice_heard` on the assistant row (written by DisPatch's
// note_heard hook, CONTRACTS §B.1 / F.6):
//   { upto: <source offset into the stored content>, interrupted: true,
//     heard: <tail of what was heard>, unheard: <head of what was not> }
// An advisor/API reply that was cut while streaming is stored as the spoken
// prefix and carries `{ interrupted: true }` without `upto`.
//
// The chat then shows the reply as it was received: the heard part normally,
// the rest muted behind a small mic-off mark ("Not heard in voice chat"), or —
// for a reply that was stored only up to the cut — a one-line "Cut off in voice
// chat" note. Pure helpers plus one DOM builder; DisPatch-owned strings only
// (msg.not_heard, msg.voice_cut): the shell never reads an add-on's keys.

/** lucide-style "mic-off" line art (24×24), drawn through util.railIcon. */
export const MIC_OFF_ICON = [
  'M2 2l20 20',
  'M18.89 13.23A7 7 0 0 0 19 12v-2',
  'M5 10v2a7 7 0 0 0 12 5',
  'M15 9.34V5a3 3 0 0 0-5.68-1.33',
  'M9 9v3a3 3 0 0 0 5.12 2.12',
  'M12 19v3',
];

/** How many times `needle` occurs in `s` (non-overlapping). */
function count(s, needle) {
  let n = 0;
  for (let i = s.indexOf(needle); i !== -1; i = s.indexOf(needle, i + needle.length)) n += 1;
  return n;
}

/**
 * Move a split point back so neither half breaks a markdown construct that
 * would otherwise render as literal punctuation: an open code fence, an open
 * inline code span, an open `**bold**`, or an unclosed `[link](…)`. The add-on
 * already cuts at a word boundary of the STORED markdown (SPEC §4.8, hardening
 * M6); this only guards the rare case where that word sits inside markup.
 */
export function safeSplit(content, upto) {
  let cut = Math.max(0, Math.min(Math.floor(upto), content.length));
  for (let guard = 0; guard < 8; guard += 1) {
    const head = content.slice(0, cut);
    const fences = count(head, '```');
    if (fences % 2 === 1) { cut = head.lastIndexOf('```'); continue; }
    const noFences = head.replace(/```[\s\S]*?```/g, '');
    const ticks = count(noFences, '`');
    if (ticks % 2 === 1) { cut = head.lastIndexOf('`'); continue; }
    const bold = count(head, '**');
    if (bold % 2 === 1) { cut = head.lastIndexOf('**'); continue; }
    const open = head.lastIndexOf('[');
    if (open > head.lastIndexOf(']') || /\]\([^)]*$/.test(head)) {
      cut = /\]\([^)]*$/.test(head) ? head.lastIndexOf('[', head.lastIndexOf('](')) : open;
      if (cut < 0) cut = 0;
      continue;
    }
    break;
  }
  return Math.max(0, cut);
}

/**
 * The parts to draw for a reply, or null when it carries no voice mark.
 *   { heard, unheard }          the listener talked over it at `upto`
 *   { heard, cut: true }        stored only up to the cut (no `upto`, or
 *                               nothing after it): one "Cut off" line
 */
export function voiceHeardParts(content, metadata) {
  const vh = metadata && typeof metadata === 'object' ? metadata.voice_heard : null;
  if (!vh || typeof vh !== 'object' || vh.interrupted !== true) return null;
  const text = String(content || '');
  const upto = Number(vh.upto);
  if (!Number.isFinite(upto) || upto >= text.trimEnd().length) return { heard: text, cut: true };
  const at = safeSplit(text, upto);
  const unheard = text.slice(at);
  if (!unheard.trim()) return { heard: text, cut: true };
  return { heard: text.slice(0, at), unheard };
}

/**
 * Fill `bubble` with a voice-marked reply. `render(md)` is the shell's markdown
 * renderer (it returns sanitised HTML); `enhance(node)` its post-processing;
 * `el`/`railIcon`/`t` the shell's DOM and string helpers. Returns true when it
 * drew something (false: the caller renders the reply as usual).
 */
export function renderVoiceHeard(bubble, content, metadata, { render, enhance, el, railIcon, t }) {
  const parts = voiceHeardParts(content, metadata);
  if (!parts) return false;
  const heard = el('div', { class: 'msg-heard' });
  heard.innerHTML = parts.heard ? render(parts.heard) : '';
  if (enhance) enhance(heard);
  bubble.replaceChildren(heard);
  if (parts.cut) {
    bubble.append(el('div', { class: 'msg-voice-cut', dir: 'auto' }, [
      railIcon(MIC_OFF_ICON), document.createTextNode(` ${t('msg.voice_cut')}`),
    ]));
    return true;
  }
  const label = t('msg.not_heard');
  const mark = el('span', { class: 'msg-unheard-mark', role: 'img', 'aria-label': label, title: label },
    [railIcon(MIC_OFF_ICON)]);
  const rest = el('div', { class: 'msg-unheard' });
  rest.innerHTML = render(parts.unheard);
  if (enhance) enhance(rest);
  // The mark sits at the start of the first block of the unheard text, inline.
  const first = rest.firstElementChild;
  if (first && /^(P|LI|H[1-6]|BLOCKQUOTE)$/.test(first.tagName)) first.prepend(mark, ' ');
  else rest.prepend(mark, ' ');
  bubble.append(rest);
  return true;
}
