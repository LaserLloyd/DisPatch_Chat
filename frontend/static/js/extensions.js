// The extension loader: installed add-ons' frontends, mounted into the shell.
//
// Contract: docs/extensions.md and CONTRACTS §E.1. On an UNLOCKED session the
// loader reads GET /api/extensions (only when /api/auth/status says at least
// one extension is `ok` — a fresh install costs no request), and for each `ok`
// one, in this order:
//
//   1. its locale bundle → i18n.mergeBundle (active language + English)
//   2. its stylesheets   → <link>, ?v=<assets hash>
//   3. import('/ext/<id>/<entry>?v=<assets hash>')
//   4. the JS handshake  → EXT.id matches and EXT.api = [lo, hi] spans EXT_API
//   5. mount(shell, { assetsVersion })  → { unmount(), onLocale() }
//
// Every /ext/ file asked for with the current hash is served `immutable`, so
// the add-on imports its siblings with the same `?v=` and a cold open on a
// phone costs one round trip each, not a revalidation each. The service
// worker never caches /ext/.
//
// On lock / logout every extension is unmounted, everything it registered on
// the shell is disposed (js/ext-sdk.js disposeExtension) and its strings are
// dropped. A failed import, handshake or mount — in any order relative to the
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

  async function mountOne(ext, myGen) {
    if (mounted.has(ext.id)) return;
    client.set(ext.id, { state: 'loading', detail: '' });
    changed();
    try {
      if (ext.locales) await bundle(ext, deps.lang());
      await Promise.all((ext.css || []).map((c) => deps.loadStyle(`/ext/${enc(ext.id)}/${c}${q(ext)}`)));
      const mod = await deps.importModule(`/ext/${enc(ext.id)}/${ext.entry}${q(ext)}`);
      if (myGen !== gen || deps.isLocked()) { deps.removeBundle(ext.id); return; }
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
      const handle = await mod.mount(deps.shellFor(ext.id), { assetsVersion: ext.assets_version || '' });
      if (myGen !== gen || deps.isLocked()) {
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

  /** Read /api/extensions (unless locked) and mount every `ok` extension not
   *  mounted yet. `force` reads even when the status count says 0 (the Voice
   *  tab and "Check again" need the failed/incompatible/disabled states). */
  async function refresh({ force = false } = {}) {
    if (deps.isLocked()) return list;
    if (inflight) return inflight;
    const myGen = gen;
    inflight = (async () => {
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
          await mountOne(ext, myGen);
        }
      }
      return list;
    })();
    try { return await inflight; } finally { inflight = null; }
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
