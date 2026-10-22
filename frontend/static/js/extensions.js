// The extension loader: installed add-ons' frontends, mounted into the shell.
//
// Contract: docs/extensions.md and CONTRACTS §E.1. The loader reads
// GET /api/extensions (only when /api/auth/status says at least
// one extension is `ok` — a fresh install costs no request), and for each `ok`
// one, in this order:
//
//   1. its locale bundle → i18n.mergeBundle (active language + English)
//   2. its stylesheets   → <link>, ?v=<assets hash>
//   3. import('/ext/<id>/<entry>?v=<assets hash>')
//   4. the JS handshake  → EXT.id matches and EXT.api = [lo, hi] spans EXT_API
//   5. mount(shell, { assetsVersion, locked })  → { unmount(), onLocale() }
//
// Every /ext/ file asked for with the current hash is served `immutable`, so
// the add-on imports its siblings with the same `?v=` and a cold open on a
// phone costs one round trip each, not a revalidation each. The service
// worker never caches /ext/.
//
// Locked devices mount too (2.3.4: voice on the bots a locked device can see;
// `locked: true` tells the add-on to show only what such a device may use).
// Crossing the lock boundary unmounts every extension (everything it
// registered on the shell is disposed, its strings dropped) and mounts it
// again for the new tier. A failed import, handshake or mount — in any order relative to the
// add-on taking its Settings pane — leaves the shell exactly as it was and
// the Voice tab showing why (js/voice-absent.js).
//
// Pure module: createLoader() takes every effect as a dependency, so
// tests/extensions.test.js drives it with a fake fetch and a fake import.

export const STATES = Object.freeze(['idle', 'loading', 'mounted', 'incompatible', 'failed']);

/**
 * deps:
 *   EXT_API                  number (js/ext-sdk.js)
 *   fetchJson(path)          → parsed JSON; throws on a non-2xx
 *   importModule(url)        → the module namespace
 *   loadStyle(href)          → Promise (resolves on load or error)
 *   mergeBundle(id, b), removeBundle(id)       (js/i18n.js)
 *   lang()                   → the active language code
 *   features()               → /api/auth/status.features (or {})
 *   isLocked()               → Safe Mode?
 *   shellFor(id)             → the frozen per-extension shell
 *   disposeExtension(id)     → tear down what that extension registered
 *   emitLocale(lang)         → after bundles were re-merged
 *   onChange()               → something the Voice tab shows changed
 */
export function createLoader(deps) {
  let list = null;           // the last /api/extensions `extensions` (null = not read)
  let mode = null;
  let readError = null;
  let gen = 0;               // bumps on every unmountAll: stale async mounts give up
  let tier = null;           // what the mounts were made for: null, 'unlocked' or 'locked'
  const tierNow = () => (deps.isLocked() ? 'locked' : 'unlocked');
  let inflight = null;
  const mounted = new Map();   // id -> { handle, ext }
  const client = new Map();    // id -> { state, detail }

  const enc = encodeURIComponent;
  const changed = () => { try { deps.onChange && deps.onChange(); } catch (e) { console.warn('[ext]', e); } };

  function q(ext) { return ext.assets_version ? `?v=${enc(ext.assets_version)}` : ''; }

  async function bundle(ext, lang) {
    const base = `/ext/${enc(ext.id)}/locales/`;
    const get = (l) => deps.fetchJson(`${base}${enc(l)}.json${q(ext)}`).catch(() => null);
    const [en, dict] = await Promise.all([get('en'), lang === 'en' ? null : get(lang)]);
    deps.mergeBundle(ext.id, { lang, dict: lang === 'en' ? en : dict, en });
  }

  function fail(ext, state, detail) {
    client.set(ext.id, { state, detail: String(detail || '').slice(0, 300) });
    try { deps.disposeExtension(ext.id); } catch { /* nothing registered */ }
    deps.removeBundle(ext.id);
    changed();
  }

  async function mountOne(ext, myGen, myTier) {
    if (mounted.has(ext.id)) return;
    client.set(ext.id, { state: 'loading', detail: '' });
    changed();
    try {
      if (ext.locales) await bundle(ext, deps.lang());
      await Promise.all((ext.css || []).map((c) => deps.loadStyle(`/ext/${enc(ext.id)}/${c}${q(ext)}`)));
      const mod = await deps.importModule(`/ext/${enc(ext.id)}/${ext.entry}${q(ext)}`);
      if (myGen !== gen || tierNow() !== myTier) { deps.removeBundle(ext.id); return; }
      const meta = mod && mod.EXT;
      const api = meta && meta.api;
      if (!meta || meta.id !== ext.id || !Array.isArray(api) || api.length !== 2
          || !(api[0] <= deps.EXT_API && deps.EXT_API <= api[1])) {
        // Same wording as the server's handshake (extensions.py), so the
        // Voice tab can say which side to update (C-§A).
        const side = Array.isArray(api) && api[1] < deps.EXT_API ? 'Update the add-on.' : 'Update DisPatch.';
        fail(ext, 'incompatible', meta && Array.isArray(api)
          ? `${ext.name || ext.id} ${ext.version || ''} needs DisPatch extension API ${api[0]}..${api[1]}; `
            + `this DisPatch has ${deps.EXT_API}. ${side}`
          : 'no EXT handshake');
        return;
      }
      if (typeof mod.mount !== 'function') throw new Error('the entry module exports no mount()');
      const handle = await mod.mount(deps.shellFor(ext.id),
        { assetsVersion: ext.assets_version || '', locked: myTier === 'locked' });
      if (myGen !== gen || tierNow() !== myTier) {
        try { handle && handle.unmount && handle.unmount(); } catch { /* best effort */ }
        try { deps.disposeExtension(ext.id); } catch { /* idem */ }
        deps.removeBundle(ext.id);
        return;
      }
      mounted.set(ext.id, { handle: handle || {}, ext });
      client.set(ext.id, { state: 'mounted', detail: '' });
      changed();
    } catch (e) {
      console.warn(`[ext] ${ext.id} could not be mounted:`, e);
      fail(ext, 'failed', e && e.message);
    }
  }

  /** Read /api/extensions and mount every `ok` extension not mounted yet.
   *  `force` reads even when the status count says 0 (the Voice tab and
   *  "Check again" need the failed/incompatible/disabled states). Since 2.3.4
   *  a locked device mounts too (the server sends it the locked rows) and the
   *  add-on is told so (`mount(shell, { locked: true })`); a mount made for
   *  the other tier is unmounted first. */
  async function refresh({ force = false } = {}) {
    const myTier = tierNow();
    if (tier !== null && tier !== myTier) {
      unmountAll();                         // bumps gen: an older read gives up
      const old = inflight;
      if (old) await old.catch(() => {});
      if (inflight === old) inflight = null;
    }
    if (inflight) return inflight;
    tier = myTier;
    const myGen = gen;
    const mine = (async () => {
      const count = Number((deps.features() || {}).extensions || 0);
      // No `ok` extension per the status: no request, ever, unless forced (the
      // Voice tab / "Check again"). A list a forced read already holds stays.
      if (!force && count <= 0) {
        if (list === null) { list = []; mode = null; changed(); }
        return list;
      }
      try {
        const body = await deps.fetchJson('/api/extensions');
        if (myGen !== gen) return list;
        list = Array.isArray(body && body.extensions) ? body.extensions : [];
        mode = (body && body.mode) || null;
        readError = null;
      } catch (e) {
        readError = e;
        if (list === null) list = [];
      }
      changed();
      for (const ext of list) {
        if (ext && ext.state === 'ok' && ext.entry && !mounted.has(ext.id)) {
          const prev = client.get(ext.id);
          if (prev && (prev.state === 'failed' || prev.state === 'incompatible') && !force) continue;
          await mountOne(ext, myGen, myTier);
        }
      }
      return list;
    })();
    inflight = mine;
    try { return await mine; } finally { if (inflight === mine) inflight = null; }
  }

  /** Lock / logout: unmount everything, forget the list. */
  function unmountAll() {
    gen += 1;
    for (const [id, { handle }] of [...mounted]) {
      try { handle.unmount && handle.unmount(); } catch (e) { console.warn('[ext] unmount failed', e); }
      try { deps.disposeExtension(id); } catch { /* idem */ }
      deps.removeBundle(id);
    }
    mounted.clear();
    client.clear();
    list = null;
    mode = null;
    tier = null;
    changed();
  }

  /** Language changed: re-merge each mounted extension's strings, then tell it. */
  async function onLanguage(lang) {
    await Promise.all([...mounted.values()].map(async ({ ext }) => {
      if (ext.locales) await bundle(ext, lang);
    }));
    for (const { handle } of mounted.values()) {
      try { handle.onLocale && handle.onLocale(lang); } catch (e) { console.warn('[ext] onLocale failed', e); }
    }
    deps.emitLocale && deps.emitLocale(lang);
  }

  return {
    refresh,
    unmountAll,
    onLanguage,
    /** The answer the Voice tab renders from. */
    snapshot() {
      return { list, mode, readError, mounted: new Set(mounted.keys()),
               client: new Map(client) };
    },
    isMounted: (id) => mounted.has(id),
    /** The tier the current mounts were made for: 'unlocked', 'locked' or null. */
    tier: () => tier,
  };
}

/**
 * Pane ownership (CONTRACTS §E.1, A16) for one Settings pane an extension may
 * take over — today the Voice tab. Until an extension claims it, the pane
 * shows `renderAbsent(root)` (js/voice-absent.js). `claim()` — what
 * `shell.settings.pane(id, …)` does — empties the pane and hands it to the
 * extension; its dispose() (called by the extension, or by the loader when the
 * extension unmounts, fails or the device locks) gives the pane back to the
 * absent page. A failure before the claim simply never claims, so in either
 * order the page ends on the absent renderer with the reason it shows.
 */
export function createPaneHost(root, renderAbsent) {
  let owner = null;          // { id, render, onShow, onHide }
  let visible = false;

  const fire = (name) => {
    if (owner && typeof owner[name] === 'function') {
      try { owner[name](); } catch (e) { console.warn(`[ext] pane ${name} failed`, e); }
    }
  };

  function showAbsent() {
    if (owner || !root) return;
    try { renderAbsent(root); } catch (e) { console.warn('[ext] absent page failed', e); }
  }

  function claim(id, { render, onShow, onHide } = {}) {
    if (!root || typeof render !== 'function') throw new Error('settings.pane: render(root) is required');
    if (owner && owner.id !== id) throw new Error('settings.pane: this pane is taken');
    if (owner) fire('onHide');
    const me = { id, render, onShow, onHide };
    owner = me;
    root.replaceChildren();
    delete root.dataset.voiceState;
    try { render(root); } catch (e) { console.warn('[ext] pane render failed', e); }
    if (visible) fire('onShow');
    return () => {
      if (owner !== me) return;
      if (visible) fire('onHide');
      owner = null;
      root.replaceChildren();
      showAbsent();
    };
  }

  return {
    claim,
    showAbsent,
    owned: () => !!owner,
    ownerId: () => (owner ? owner.id : null),
    /** The tab came on screen / left it. */
    show() { visible = true; if (owner) fire('onShow'); else showAbsent(); },
    hide() { if (visible) fire('onHide'); visible = false; },
  };
}

/** A tab id an add-in may take: lower-case, digits and hyphens. */
export const ADDIN_TAB_ID = /^[a-z][a-z0-9-]{1,30}$/;

/**
 * Add-in Settings tabs (docs/extensions.md "Settings panes").
 * `shell.settings.pane(id, spec)` with an id the shell did not pre-declare
 * creates a tab button (`stab-<id>`) and a pane (`spane-<id>`) on demand, placed
 * after Voice and before Plugins, so the order reads core -> add-ins -> setup.
 * The pane's contents follow the same ownership rules as the Voice pane (a
 * createPaneHost per pane); disposing the claim removes the tab and the pane
 * again. Tabs are admin-only unless the spec says `public: true`.
 *
 *   doc        the document
 *   tablist    #settings-tabs
 *   beforeTab  the tab button new tabs are inserted before (#stab-plugins)
 *   beforePane the pane element new panes are inserted before (#spane-plugins)
 *   reserved   Set of ids that are not available (the core tabs)
 *   t          i18n lookup (a spec's `titleKey`)
 *   isLocked() is this a Safe-Mode session (an admin tab starts hidden)
 *   onAdd(id, { public }) / onRemove(id)   main.js keeps its tab lists in step
 *   onTab(id)  a tab button was clicked
 */
export function createAddinTabs({ doc, tablist, beforeTab, beforePane, reserved = new Set(),
  t = (k) => k, isLocked = () => false, onAdd, onRemove, onTab }) {
  const tabs = new Map();         // id -> { owner, button, pane, host, isPublic, dispose }

  const mk = (tag, attrs = {}, kids = []) => {
    const n = doc.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'text') n.textContent = v;
      else n.setAttribute(k, v);
    }
    n.append(...kids);
    return n;
  };

  function claim(owner, id, spec = {}) {
    if (typeof id !== 'string' || !ADDIN_TAB_ID.test(id)) {
      throw new Error(`settings.pane: "${id}" is not a valid tab id (a-z, 0-9, hyphen; 2-31 characters)`);
    }
    if (reserved.has(id)) throw new Error(`settings.pane: "${id}" is a built-in tab`);
    if (!spec || typeof spec.render !== 'function') throw new Error('settings.pane: render(root) is required');
    const cur = tabs.get(id);
    if (cur && cur.owner !== owner) throw new Error(`settings.pane: "${id}" is taken`);
    if (cur) return cur.host.claim(owner, spec);

    const isPublic = spec.public === true;
    const title = spec.titleKey ? t(String(spec.titleKey)) : String(spec.title == null ? id : spec.title);
    // No glyph given: the ThemeForge puzzle icon (drawn through the injected doc).
    const puzzleIcon = () => {
      const NS = 'http://www.w3.org/2000/svg';
      const svg = doc.createElementNS(NS, 'svg');
      for (const [k, v] of [['viewBox', '0 0 24 24'], ['fill', 'none'], ['aria-hidden', 'true'], ['width', '17'], ['height', '17'], ['class', 'rail-icon']]) svg.setAttribute(k, v);
      const use = doc.createElementNS(NS, 'use');
      use.setAttribute('href', '/static/ui-theme/icons.svg#puzzle');
      svg.append(use);
      return svg;
    };
    const labelAttrs = { class: 'stab-label', text: title };
    if (spec.titleKey) labelAttrs['data-i18n'] = String(spec.titleKey);
    const glyph = typeof spec.glyph === 'string' && spec.glyph ? spec.glyph.slice(0, 4) : '';   // none given: the puzzle icon
    const button = mk('button', {
      class: 'settings-tab' + (isPublic ? '' : ' admin-only') + (!isPublic && isLocked() ? ' hidden' : ''),
      id: `stab-${id}`, role: 'tab', 'data-tab': id, 'aria-selected': 'false',
      'aria-controls': `spane-${id}`, tabindex: '-1', 'data-addin': owner || '',
    }, [
      mk('span', glyph ? { class: 'stab-glyph', 'aria-hidden': 'true', text: glyph } : { class: 'stab-glyph', 'aria-hidden': 'true' }, glyph ? [] : [puzzleIcon()]),
      mk('span', labelAttrs),
    ]);
    const root = mk('div', { class: 'ext-pane', id: `ext-pane-${id}` });
    const pane = mk('section', {
      class: 'settings-pane hidden', id: `spane-${id}`, role: 'tabpanel',
      'aria-labelledby': `stab-${id}`, tabindex: '0',
    }, [root]);
    const host = createPaneHost(root, () => {});
    tablist.insertBefore(button, beforeTab || null);
    if (beforePane && beforePane.parentNode) beforePane.parentNode.insertBefore(pane, beforePane);
    else tablist.parentNode.append(pane);
    button.addEventListener('click', () => { if (onTab) onTab(id); });
    const entry = { owner, button, pane, host, isPublic, dispose: null };
    tabs.set(id, entry);
    if (onAdd) onAdd(id, { public: isPublic });
    const release = host.claim(owner, spec);
    entry.dispose = () => {
      if (tabs.get(id) !== entry) return;
      try { release(); } catch { /* idem */ }
      tabs.delete(id);
      button.remove();
      pane.remove();
      if (onRemove) onRemove(id);
    };
    return entry.dispose;
  }

  return {
    claim,
    has: (id) => tabs.has(id),
    ids: () => [...tabs.keys()],
    host: (id) => (tabs.has(id) ? tabs.get(id).host : null),
    isPublic: (id) => !!(tabs.get(id) && tabs.get(id).isPublic),
  };
}
