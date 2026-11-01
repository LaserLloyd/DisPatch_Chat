// E2E fake voice add-on: the frontend half (see ../__init__.py).
// Registers bots.rowExtra (a labelled Voice <select>) and one chat-header
// button with visible text, so the matrix can check both without the real add-on.
export const EXT = { id: 'voice', api: [1, 1] };

const VOICES = ['', 'builtin:e2e_alto', 'builtin:e2e_bass'];

export async function mount(shell) {
  const t = (k) => (shell.i18n && shell.i18n.t ? shell.i18n.t(k) : k);
  const disposers = [];
  if (shell.bots && shell.bots.rowExtra) {
    disposers.push(shell.bots.rowExtra((bot, onChange) => {
      if (!bot) return null;
      const id = `e2e-voice-${String(bot.id).replace(/[^a-zA-Z0-9_-]/g, '_')}`;
      const sel = document.createElement('select');
      sel.id = id;
      sel.className = 'e2e-voice-select';
      sel.setAttribute('aria-label', `${t('fakevoice.voice')}: ${bot.name || bot.id}`);
      for (const v of VOICES) {
        const o = document.createElement('option');
        o.value = v;
        o.textContent = v ? v.split(':')[1] : t('fakevoice.default');
        sel.append(o);
      }
      sel.value = typeof bot.voice === 'string' ? bot.voice : '';
      sel.addEventListener('change', () => onChange({ voice: sel.value }));
      const wrap = document.createElement('span');
      wrap.className = 'e2e-voice-host';
      wrap.append(sel);
      return wrap;
    }));
  }
  if (shell.chatHeader && shell.chatHeader.addAction) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'icon-btn e2e-voice-btn';
    b.textContent = t('fakevoice.call');
    b.title = t('fakevoice.call_hint');
    b.addEventListener('click', () => shell.toast && shell.toast(t('fakevoice.call_hint')));
    disposers.push(shell.chatHeader.addAction(b));
  }
  if (shell.settings && shell.settings.pane) {
    disposers.push(shell.settings.pane('voice', {
      render: (host) => {
        const p = document.createElement('p');
        p.className = 'e2e-voice-pane';
        p.textContent = t('fakevoice.pane');
        host.replaceChildren(p);
      },
    }));
  }
  return {
    unmount: () => { for (const d of disposers.splice(0)) { try { typeof d === 'function' ? d() : d && d.remove && d.remove(); } catch { /* best effort */ } } },
    onLocale: () => {},
  };
}
