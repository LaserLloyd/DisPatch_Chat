// Settings -> Characters -> a household bot's Details -> "Copies & chats" (4.1 managed bots,
// app/kits.py). Admins only. A kit makes the household bot a template: copies follow the
// changes their owners did not override, and the chats listed here reach every copy once.
//
//   GET    /api/characters/{id}/kit        -> {managed, shipped, copyable, auto_clone, chats, copies}
//   PUT    /api/characters/{id}/kit        {auto_clone, chats:[{id?, title, body, audience, resend}]}
//   DELETE /api/characters/{id}/kit        stop managing (not for Atlas: 409 kit_shipped)
//   POST   /api/characters/{id}/kit/push   sync every copy now -> {..., posted}
//
// The copy count is a number; nothing here names or reads anyone else's bots or chats.
// Nothing runs at import time.

import { t } from './i18n.js?v=5';
import { el } from './util.js?v=23';
import { profileReq } from './bot-editor.js?v=6';
import { askDialog } from './people-perms.js?v=9';

const base = (id) => `/api/characters/${encodeURIComponent(id)}/kit`;

export const kitCalls = {
  get: (id) => profileReq('GET', base(id)),
  save: (id, body) => profileReq('PUT', base(id), { json: body }),
  remove: (id) => profileReq('DELETE', base(id)),
  push: (id) => profileReq('POST', `${base(id)}/push`),
};

// ------------------------------------------------------------------ pure

/** The PUT body for the kit `kit` with `chats` (the household ones; shipped ones are never
 *  sent). `resend` on an edited chat says whether every copy gets the new version.
 *  `auto_clone` is sent only when the caller passes `autoClone` (the switch): a chat save
 *  must not touch who the character is given to (4.2: absent = leave `give_to` alone). */
export function kitBody(kit, { autoClone, chats = null } = {}) {
  const list = (chats || (kit.chats || []).filter((c) => !c.builtin)).map((c) => {
    const out = { title: c.title, body: c.body, audience: c.audience === 'admins' ? 'admins' : 'everyone' };
    if (c.id) out.id = c.id;
    if (c.resend === false) out.resend = false;
    return out;
  });
  return autoClone === undefined ? { chats: list } : { auto_clone: !!autoClone, chats: list };
}

/** Who a chat reaches, as the list shows it. */
export const audienceLabel = (a) => (a === 'admins' ? t('kits.aud_admins') : t('kits.aud_everyone'));

// ------------------------------------------------------------------ DOM

function chatForm(chat, { onSave, onCancel }) {
  const uid = Math.random().toString(36).slice(2, 8);
  const title = el('input', { type: 'text', class: 'ui-input', id: `kit-title-${uid}`, maxlength: '80', value: chat ? chat.title : '' });
  const body = el('textarea', { class: 'ui-input kit-body', id: `kit-body-${uid}`, rows: '6', maxlength: '20000', dir: 'auto' });
  body.value = chat ? chat.body : '';
  const aud = el('select', { class: 'ui-select', id: `kit-aud-${uid}` }, [
    el('option', { value: 'everyone', text: t('kits.aud_everyone') }),
    el('option', { value: 'admins', text: t('kits.aud_admins') })]);
  aud.value = chat && chat.audience === 'admins' ? 'admins' : 'everyone';
  const resend = el('input', { type: 'checkbox', class: 'ui-checkbox', id: `kit-resend-${uid}` });
  resend.checked = true;
  const err = el('p', { class: 'lock-error', role: 'alert', hidden: '' });
  const save = el('button', { type: 'button', class: 'btn-primary', text: t('kits.chat_save') });
  const cancel = el('button', { type: 'button', class: 'btn-secondary', text: t('common.cancel') });
  save.addEventListener('click', async () => {
    if (!title.value.trim() || !body.value.trim()) { err.textContent = t('kits.chat_needs'); err.hidden = false; return; }
    save.disabled = true;
    try {
      await onSave({ id: chat ? chat.id : undefined, title: title.value.trim(), body: body.value,
        audience: aud.value, resend: chat ? resend.checked : true });
    } catch (e) { err.textContent = (e && e.message) || String(e); err.hidden = false; } finally { save.disabled = false; }
  });
  cancel.addEventListener('click', onCancel);
  const kids = [
    el('div', { class: 'sec-field people-field' }, [el('label', { for: title.id, text: t('kits.chat_title') }), title]),
    el('div', { class: 'sec-field people-field' }, [el('label', { for: body.id, text: t('kits.chat_body') }), body,
      el('span', { class: 'people-field-hint', text: t('kits.chat_body_hint') })]),
    el('div', { class: 'sec-field people-field' }, [el('label', { for: aud.id, text: t('kits.chat_audience') }), aud]),
  ];
  if (chat) {
    kids.push(el('div', { class: 'dev-row' }, [
      el('label', { class: 'dev-row-text', for: resend.id }, [el('strong', { text: t('kits.resend') }),
        el('span', { class: 'dev-row-hint', text: t('kits.resend_hint') })]), resend]));
  }
  kids.push(err, el('div', { class: 'cxn-actions' }, [save, cancel]));
  return el('div', { class: 'kit-form dev-group-body', role: 'group', 'aria-label': chat ? t('kits.chat_edit') : t('kits.chat_add') }, kids);
}

/** The "Copies & chats" panel for one household bot. `ctx`: { toast, ask?, chatsOnly? } (`chatsOnly`:
 *  4.2 template Details, who gets a copy lives in "Who can use it", so only the chats show) (`ask` replaces
 *  the confirm dialog in tests); `calls` replaces the network calls. Resolves to the element. */
export async function kitSection(bot, ctx = {}, calls = kitCalls) {
  const box = el('div', { class: 'chr-kit dev-group-body', role: 'group', 'aria-label': t('kits.title') });
  const say = (m, bad) => ctx.toast && ctx.toast(m, bad);
  const ask = ctx.ask || ((o) => askDialog(o));
  let kit = null;

  async function load() {
    try { kit = await calls.get(bot.id); } catch (e) { box.replaceChildren(el('p', { class: 'muted', text: (e && e.message) || String(e) })); return; }
    paint();
  }

  async function save(body, msg) {
    kit = await calls.save(bot.id, body);
    say(msg || t('kits.saved'));
    paint();
  }

  function paint(stage = null) {
    const head = el('h4', { class: 'chr-drawer-title', text: t(ctx.chatsOnly ? 'kits.chats' : 'kits.title') });
    if (!kit.copyable && !kit.managed && !ctx.chatsOnly) {
      box.replaceChildren(head, el('p', { class: 'muted', text: t('copybot.agent_refused') }));
      return;
    }
    if (!kit.managed && !ctx.chatsOnly) {
      const on = el('button', { type: 'button', class: 'btn-secondary', text: t('kits.start') });
      on.addEventListener('click', async () => {
        on.disabled = true;
        try { await save({ auto_clone: false, chats: [] }); } catch (e) { say((e && e.message) || String(e), true); on.disabled = false; }
      });
      box.replaceChildren(head, el('p', { class: 'people-field-hint', text: t('kits.what') }), el('div', { class: 'cxn-actions' }, [on]));
      return;
    }
    const sw = ctx.chatsOnly ? null : (() => {
      const sw = el('input', { type: 'checkbox', role: 'switch', class: 'ui-checkbox', id: `kit-auto-${bot.id}`.replace(/[^a-zA-Z0-9_-]/g, '-') });
      sw.checked = !!kit.auto_clone;
      sw.addEventListener('change', async () => {
        sw.disabled = true;
        try { await save(kitBody(kit, { autoClone: sw.checked })); } catch (e) { sw.checked = !sw.checked; say((e && e.message) || String(e), true); } finally { sw.disabled = false; }
      });
      return sw;
    })();
    const rows = (kit.chats || []).map((c) => {
      const acts = [];
      if (!c.builtin) {
        acts.push(el('button', { type: 'button', class: 'btn-secondary', text: t('kits.chat_edit'), onclick: () => paint(c) }));
        acts.push(el('button', { type: 'button', class: 'btn-secondary', text: t('kits.chat_remove'), onclick: async () => {
          const ok = await ask({ title: t('kits.chat_remove'), body: t('kits.chat_remove_confirm', { title: c.title }), okText: t('kits.chat_remove') });
          if (!ok) return;
          try { await save(kitBody(kit, { chats: kit.chats.filter((x) => !x.builtin && x.id !== c.id) })); } catch (e) { say((e && e.message) || String(e), true); }
        } }));
      }
      return el('li', { class: 'mb-row kit-chat', dataset: { chat: c.id } }, [
        el('div', { class: 'mb-text' }, [
          el('strong', { text: c.title }),
          el('span', { class: 'muted', text: c.builtin ? `${audienceLabel(c.audience)} · ${t('kits.builtin')}` : audienceLabel(c.audience) })]),
        acts.length ? el('div', { class: 'mb-actions' }, acts) : null]);
    });
    const list = el('ul', { class: 'mb-list kit-chats', 'aria-label': t('kits.chats') }, rows);
    const add = el('button', { type: 'button', class: 'btn-secondary', text: t('kits.chat_add'), onclick: () => paint('new') });
    const push = el('button', { type: 'button', class: 'btn-primary', text: t('kits.push') });
    push.addEventListener('click', async () => {
      push.disabled = true;
      try { kit = await calls.push(bot.id); say(t('kits.pushed', { count: kit.posted || 0 })); paint(); } catch (e) { say((e && e.message) || String(e), true); } finally { push.disabled = false; }
    });
    const tail = [push, add];
    if (!kit.shipped && !ctx.chatsOnly) {
      tail.push(el('button', { type: 'button', class: 'btn-secondary', text: t('kits.stop'), onclick: async () => {
        const ok = await ask({ title: t('kits.stop'), body: t('kits.stop_confirm'), okText: t('kits.stop') });
        if (!ok) return;
        try { kit = await calls.remove(bot.id); say(t('kits.stopped')); paint(); } catch (e) { say((e && e.message) || String(e), true); }
      } }));
    }
    const kids = ctx.chatsOnly
      ? [head, el('p', { class: 'people-field-hint', text: t('kits.chats_what') }),
        rows.length ? list : el('p', { class: 'muted', text: t('kits.no_chats') })]
      : [head, el('p', { class: 'people-field-hint', text: t('kits.what') }),
        el('div', { class: 'dev-row chr-feature', dataset: { feature: 'auto_clone' } }, [
          el('label', { class: 'dev-row-text', for: sw.id }, [el('strong', { text: t('kits.auto_clone') }),
            el('span', { class: 'dev-row-hint', text: t('kits.auto_clone_hint') })]), sw]),
        el('p', { class: 'muted kit-copies', text: t('kits.copies', { count: kit.copies || 0 }) }),
        el('h5', { class: 'kit-sub', text: t('kits.chats') }),
        rows.length ? list : el('p', { class: 'muted', text: t('kits.no_chats') })];
    if (stage) {
      const editing = stage === 'new' ? null : stage;
      kids.push(chatForm(editing, {
        onSave: async (c) => {
          const others = kit.chats.filter((x) => !x.builtin);
          const chats = editing ? others.map((x) => (x.id === editing.id ? c : x)) : [...others, c];
          await save(kitBody(kit, { chats }), t('kits.chat_saved'));
        },
        onCancel: () => paint(),
      }));
    } else {
      kids.push(el('div', { class: 'cxn-actions kit-actions' }, tail));
    }
    box.replaceChildren(...kids);
  }

  box.append(el('p', { class: 'muted', text: t('chars.loading') }));
  await load();
  return box;
}
