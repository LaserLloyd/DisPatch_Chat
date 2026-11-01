/* GENERATED component helpers: ThemeForge 2.0.1, drop-in bundle (ui-theme/).
   https://github.com/LaserLloyd/ThemeForge . Do not edit this copy: update the whole
   folder (python ui-theme/update.py) and keep app styles in the app.
   body sha256: 996bd1cf328464c5 */
/* ============================================================================
   ui-components.js — behaviour for the component layer (optional) · ThemeForge
   ----------------------------------------------------------------------------
   Themed dialogs and toasts, a copy-to-clipboard helper, inline field
   validation, settings-row wiring, the icon helpers and a string table for all
   of them. Load it anywhere (defer is fine):

     <script src="ui-theme/ui-components.js" defer></script>

     if (await UIComponents.confirm({ title: 'Delete job?', message: 'This cannot be undone.',
                                      confirmLabel: 'Delete', danger: true })) { ... }
     await UIComponents.alert('Saved.');
     const name = await UIComponents.prompt({ title: 'Rename', value: 'draft-1' });  // null on cancel
     UIComponents.toast('Settings saved', { kind: 'success' });  // kind: info|success|warning|danger

     await UIComponents.copy(button);                       // copies .ui-copy / .ui-codeblock text
     UIComponents.validate(input, 'Enter a port from 1 to 65535.');   // aria-invalid + message
     UIComponents.validate(input, null);                    // clear it
     UIComponents.setting(row, { default: '8080' });        // "reset to default" for a .ui-setting

     button.append(UIComponents.icon('trash'));             // <svg class="ui-icon"> from icons.svg
     UIComponents.iconFor('🗑️');                            // 'trash' (null when no icon means it)
     UIComponents.emojify(message, 'text');                 // emoji as icons | text colour | colour
     <div data-ui-emoji="icon">                             // the same, run by init()

   Icons come from icons.svg next to this file; data-icons="<url>" on the script tag
   points elsewhere. On another origin (a CDN) the sprite is fetched once and put in
   the page, because <use> cannot reach across origins.

   Every string is set as text, never as HTML, so it is safe to show user
   input. A string argument is shorthand for { message: string }.

   Words this file puts on the page (Cancel, OK, Copy, Copied, Reset to default, ...)
   come from a string table, so an app can translate them:

     UIComponents.setStrings({ cancel: 'キャンセル', ok: 'OK' });   // merge overrides
     await UIComponents.loadLocale('ja');          // fetches locales/ja.json next to this file
     <script src=".../ui-components.js" data-locale="auto">   // loads the page's <html lang>
     <html data-ui-str-cancel="Annuler">           // any element: its subtree uses this word

   Order of precedence for a word: a data-ui-str-<key> attribute on the element
   or an ancestor, then setStrings()/loadLocale(), then the English default.
   Placeholders like {name} are filled from the call. ui-theme/locales/ holds
   en.json (the defaults) and one file each for ar, de, es, fr, ja, pt and zh.
   ============================================================================ */
(function (global) {
  'use strict';

  var doc = global.document;
  var script = doc.currentScript;
  var base = script && script.src ? script.src.replace(/[^\/]*$/, '') : '';

  /* ---- Strings ---------------------------------------------------------- */

  // Keep in step with locales/en.json (the browser tests compare them).
  var DEFAULTS = {
    cancel: 'Cancel',
    ok: 'OK',
    confirm: 'Confirm',
    input: 'Input',
    message: 'Message',
    value: 'Value',
    copy: 'Copy',
    copied: 'Copied',
    copyFailed: 'Copy failed',
    reset: 'Reset to default',
    resetNamed: 'Reset {name} to default',
    basic: 'Basic',
    advanced: 'Advanced',
    setByEnv: 'Set by environment'
  };
  var table = {};

  function resetStrings() {
    table = {};
    for (var k in DEFAULTS) if (Object.prototype.hasOwnProperty.call(DEFAULTS, k)) table[k] = DEFAULTS[k];
    return strings();
  }

  function strings() {
    var copy = {};
    for (var k in table) if (Object.prototype.hasOwnProperty.call(table, k)) copy[k] = table[k];
    return copy;
  }

  function setStrings(map) {
    if (map && typeof map === 'object') {
      for (var k in map) {
        if (Object.prototype.hasOwnProperty.call(map, k) && k.charAt(0) !== '$' && typeof map[k] === 'string') {
          table[k] = map[k];
        }
      }
    }
    return strings();
  }

  function kebab(key) {
    return key.replace(/([A-Z])/g, function (c) { return '-' + c.toLowerCase(); });
  }

  // A word by key. `el` (optional) lets a data-ui-str-<key> attribute on it or an
  // ancestor win; `vars` fills {placeholders}.
  function t(key, el, vars) {
    var text = null;
    if (el && el.closest) {
      var holder = el.closest('[data-ui-str-' + kebab(key) + ']');
      if (holder) text = holder.getAttribute('data-ui-str-' + kebab(key));
    }
    if (text === null) text = doc.documentElement.getAttribute('data-ui-str-' + kebab(key));
    if (text === null) text = Object.prototype.hasOwnProperty.call(table, key) ? table[key] : (DEFAULTS[key] || key);
    if (vars) {
      text = text.replace(/\{(\w+)\}/g, function (m, name) {
        return Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : m;
      });
    }
    return text;
  }

  function locale() {
    return doc.documentElement.lang || (global.navigator && global.navigator.language) || 'en';
  }

  // Fetch <this file's folder>/locales/<lang>.json and merge it over the current words.
  // Tries the full tag (pt-BR) then the language (pt). English is built in:
  // loadLocale('en') restores it. Resolves true when a file was applied.
  // Only the newest loadLocale() call applies (an app's loadLocale('ja') and
  // data-locale="auto" may both be in flight), and a language file's words replace the
  // previous file's: a word the new file lacks goes back to English, while words set with
  // setStrings() that no earlier file set survive. A call that finds no file leaves the
  // words as they are.
  var localeCall = 0;
  var localeWords = [];

  function loadLocale(lang, url) {
    var call = ++localeCall;
    lang = String(lang || locale()).toLowerCase().replace(/_/g, '-');
    if (!url && /^en(-|$)/.test(lang)) {  // English is built in
      resetStrings();
      localeWords = [];
      init(doc);
      return Promise.resolve(true);
    }
    if (typeof global.fetch !== 'function') return Promise.resolve(false);
    var tries = url ? [url] : [base + 'locales/' + lang + '.json'];
    if (!url && lang.indexOf('-') > 0) tries.push(base + 'locales/' + lang.split('-')[0] + '.json');
    function attempt(i) {
      if (i >= tries.length) return Promise.resolve(false);
      return global.fetch(tries[i], { credentials: 'same-origin' }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (map) {
        if (call !== localeCall) return false;  // a later call won
        for (var w = 0; w < localeWords.length; w++) {
          var key = localeWords[w];
          if (!Object.prototype.hasOwnProperty.call(map, key)) {
            if (Object.prototype.hasOwnProperty.call(DEFAULTS, key)) table[key] = DEFAULTS[key];
            else delete table[key];
          }
        }
        setStrings(map);
        localeWords = Object.keys(map).filter(function (k) { return k.charAt(0) !== '$' && typeof map[k] === 'string'; });
        init(doc);
        return true;
      }).catch(function () { return call === localeCall ? attempt(i + 1) : false; });
    }
    return attempt(0);
  }

  // Locale-aware numbers and dates (Intl), in the page's language.
  function formatNumber(value, options) {
    try { return new Intl.NumberFormat(locale(), options).format(value); } catch (e) { return String(value); }
  }

  function formatDate(value, options) {
    var d = value instanceof Date ? value : new Date(value);
    try {
      return new Intl.DateTimeFormat(locale(), options || { dateStyle: 'medium', timeStyle: 'short' }).format(d);
    } catch (e) { return String(value); }
  }

  /* ---- DOM helpers ------------------------------------------------------ */

  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text != null && text !== '') node.textContent = String(text);
    return node;
  }

  function options(arg) {
    return typeof arg === 'object' && arg !== null ? arg : { message: arg == null ? '' : String(arg) };
  }

  var uid = 0;

  // A polite live region for messages that have no element of their own
  // (copied, a validation error). Reusing one node lets screen readers announce
  // each change.
  var live = null;
  var liveTimer = null;

  // While a modal <dialog> is open the rest of the page is inert: nothing there can
  // take focus or a selection, and screen readers skip it. A node these helpers add
  // goes into the dialog that holds `node`, else the topmost open modal, else <body>.
  function layer(node) {
    var d = node && node.closest ? node.closest('dialog[open]') : null;
    if (d) return d;
    var open = doc.querySelectorAll('dialog[open]');
    for (var i = open.length - 1; i >= 0; i--) {
      var modal = false;
      try { modal = open[i].matches(':modal'); } catch (e) { modal = false; }
      if (modal) return open[i];
    }
    return doc.body;
  }

  function liveNode() {
    var node = el('div', 'ui-visually-hidden');
    node.setAttribute('role', 'status');
    node.setAttribute('aria-live', 'polite');
    node.setAttribute('data-ui-live', '');
    return node;
  }

  // announce(message[, node]): `node` (optional) is where the message comes from, so a
  // message from inside a modal dialog is spoken from inside it.
  function announce(message, node) {
    if (!message) return;
    var host = layer(node);
    var region;
    if (host === doc.body) {
      if (!live || !live.isConnected) {
        live = liveNode();
        doc.body.appendChild(live);
      }
      region = live;
    } else {
      region = host.querySelector('[data-ui-live]');
      if (!region) {
        region = liveNode();
        host.appendChild(region);
      }
    }
    region.textContent = '';
    global.clearTimeout(liveTimer);
    liveTimer = global.setTimeout(function () { region.textContent = String(message); }, 30);
  }

  /* ---- Dialogs ---------------------------------------------------------- */

  // One modal at a time; each call resolves with the result of its own dialog.
  function modal(kind, arg) {
    var o = options(arg);
    var id = 'ui-dialog-' + (++uid);
    return new Promise(function (resolve) {
      var dialog = el('dialog', 'ui-dialog');
      var form = el('form');
      form.method = 'dialog';
      if (o.title) {
        var h = el('h2', 'ui-dialog__title', o.title);
        h.id = id + '-title';
        dialog.setAttribute('aria-labelledby', h.id);
        form.appendChild(h);
      } else {
        dialog.setAttribute('aria-label', o.label || t(kind === 'confirm' ? 'confirm' : kind === 'prompt' ? 'input' : 'message'));
      }
      var body = el('div', 'ui-dialog__body');
      body.id = id + '-body';
      if (o.message) body.appendChild(el('p', null, o.message));
      dialog.setAttribute('aria-describedby', body.id);
      var input = null;
      if (kind === 'prompt') {
        input = el('input', 'ui-input');
        input.type = o.type || 'text';
        input.value = o.value == null ? '' : String(o.value);
        if (o.placeholder) input.placeholder = o.placeholder;
        input.setAttribute('aria-label', o.label || o.title || o.message || t('value'));
        body.appendChild(input);
      }
      form.appendChild(body);

      var actions = el('div', 'ui-dialog__actions');
      if (kind !== 'alert') {
        var cancel = el('button', 'ui-btn', o.cancelLabel || t('cancel', doc.activeElement));
        cancel.value = 'cancel';
        actions.appendChild(cancel);
      }
      var ok = el('button', 'ui-btn ' + (o.danger ? 'ui-btn--danger' : 'ui-btn--primary'),
                  o.confirmLabel || t('ok', doc.activeElement));
      ok.value = 'ok';
      actions.appendChild(ok);
      form.appendChild(actions);
      dialog.appendChild(form);

      var previous = doc.activeElement;
      dialog.addEventListener('close', function () {
        var accepted = dialog.returnValue === 'ok';
        dialog.remove();
        if (previous && typeof previous.focus === 'function') {
          try { previous.focus(); } catch (e) { /* element gone */ }
        }
        if (kind === 'confirm') resolve(accepted);
        else if (kind === 'prompt') resolve(accepted ? input.value : null);
        else resolve();
      });
      // Enter in the prompt's field submits as OK, except the Enter that commits an IME
      // composition (Japanese, Chinese, Korean input), which must not close the dialog.
      if (input) {
        input.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); dialog.close('ok'); }
        });
      }
      doc.body.appendChild(dialog);
      dialog.returnValue = '';
      dialog.showModal();
      (input || (kind === 'alert' ? ok : (o.danger ? actions.firstChild : ok))).focus();
      if (input) input.select();
    });
  }

  /* ---- Toasts ----------------------------------------------------------- */

  var region = null;

  function toast(message, opts) {
    var o = opts || {};
    if (!region || !region.isConnected) {
      region = el('div', 'ui-toasts');
      region.setAttribute('role', 'status');
      region.setAttribute('aria-live', 'polite');
      doc.body.appendChild(region);
    }
    var kinds = { info: 1, success: 1, warning: 1, danger: 1 };
    var node = el('div', 'ui-toast' + (kinds[o.kind] ? ' ui-toast--' + o.kind : ''), message);
    region.appendChild(node);
    var timeout = o.timeout == null ? 4000 : o.timeout;
    if (timeout > 0) {
      global.setTimeout(function () { node.remove(); }, timeout);
    }
    return node;
  }

  /* ---- Copy to clipboard ------------------------------------------------ */

  var COPY_BUTTON = '[data-ui-copy], .ui-copy__button, .ui-codeblock__button';

  // The element whose text a copy button copies.
  function copySource(node) {
    var spec = node.getAttribute && node.getAttribute('data-ui-copy');
    if (spec && spec !== 'true' && spec !== '') {
      var named = null;
      try { named = doc.querySelector(spec); } catch (e) { named = null; }  // not a valid selector
      if (named) return named;
    }
    var field = node.closest && node.closest('.ui-copy');
    if (field && node.matches && node.matches(COPY_BUTTON)) return field.querySelector('.ui-copy__value') || field;
    var block = node.closest && node.closest('.ui-codeblock');
    if (block && node.matches && node.matches(COPY_BUTTON)) return block.querySelector('pre') || block;
    return node;
  }

  function textOf(source) {
    if (typeof source === 'string') return source;
    if (source && source.matches && source.matches('input, textarea, select')) return source.value;
    return source ? source.textContent : '';
  }

  // execCommand fallback for pages that are not a secure context (plain http on a
  // LAN address), where navigator.clipboard does not exist.
  function legacyCopy(text, node) {
    var area = el('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.setAttribute('aria-hidden', 'true');
    area.style.cssText = 'position:fixed;top:0;inset-inline-start:0;width:1px;height:1px;padding:0;border:0;opacity:0';
    var previous = doc.activeElement;
    layer(node).appendChild(area);  // inside an open modal, or it cannot be selected
    var ok = false;
    try {
      area.select();
      area.setSelectionRange(0, text.length);
      ok = !!doc.execCommand('copy');
    } catch (e) { ok = false; }
    area.remove();
    if (previous && typeof previous.focus === 'function') {
      try { previous.focus(); } catch (e) { /* element gone */ }
    }
    return ok;
  }

  function writeClipboard(text, node) {
    var nav = global.navigator;
    if (nav && nav.clipboard && typeof nav.clipboard.writeText === 'function' && global.isSecureContext !== false) {
      return nav.clipboard.writeText(text).then(function () { return true; }, function () { return legacyCopy(text, node); });
    }
    return Promise.resolve(legacyCopy(text, node));
  }

  // The text node that holds a button's word, so feedback swaps the word and leaves an
  // icon beside it alone; null for an icon-only button.
  function labelText(button) {
    var walker = doc.createTreeWalker(button, 4 /* NodeFilter.SHOW_TEXT */);
    var found = null;
    var node;
    while ((node = walker.nextNode())) {
      if (node.nodeValue.trim() && !(node.parentElement && node.parentElement.closest('svg'))) found = node;
    }
    return found;
  }

  function copyFeedback(button, ok) {
    if (!button || !button.matches || !button.matches('button, [role="button"]')) return;
    var word = ok ? t('copied', button) : t('copyFailed', button);
    var label = button.__uiCopyNode || labelText(button);
    if (label && button.__uiCopyLabel === undefined) {
      button.__uiCopyNode = label;
      button.__uiCopyLabel = label.nodeValue;
    }
    button.setAttribute('data-copied', ok ? 'true' : 'failed');
    if (label) label.nodeValue = word;
    announce(word, button);
    global.clearTimeout(button.__uiCopyTimer);
    button.__uiCopyTimer = global.setTimeout(function () {
      button.removeAttribute('data-copied');
      if (button.__uiCopyNode) button.__uiCopyNode.nodeValue = button.__uiCopyLabel;
      button.__uiCopyNode = undefined;
      button.__uiCopyLabel = undefined;
    }, 1800);
  }

  // copy(element | string): copies text, resolves true on success. For a copy
  // button, a .ui-copy or a .ui-codeblock it finds the text itself; an input or
  // textarea copies its value, any other element its text.
  function copy(target) {
    var button = target && target.matches && target.matches(COPY_BUTTON) ? target : null;
    if (!button && target && target.querySelector) button = target.querySelector(COPY_BUTTON);
    var source = typeof target === 'string' ? target : copySource(target);
    var text = textOf(source);
    return writeClipboard(text, button || (typeof target === 'string' ? null : target)).then(function (ok) {
      copyFeedback(button, ok);
      return ok;
    });
  }

  /* ---- Inline field validation ------------------------------------------ */

  function controlOf(field) {
    if (!field) return null;
    if (field.matches && field.matches('input, select, textarea')) return field;
    return field.querySelector ? field.querySelector('input, select, textarea') : null;
  }

  function tokens(value) {
    return (value || '').split(/\s+/).filter(Boolean);
  }

  // validate(field, message[, { focus, announce }])
  //   message set:   aria-invalid="true", a .ui-error element after the control that
  //                  aria-describedby points at, and the message announced politely.
  //   message empty: all of that removed.
  // Returns the .ui-error element, or null when cleared.
  function validate(field, message, opts) {
    var o = opts || {};
    var control = controlOf(field);
    if (!control) return null;
    if (!control.id) control.id = 'ui-field-' + (++uid);
    var errId = control.id + '-error';
    var existing = doc.getElementById(errId);
    var describedby = tokens(control.getAttribute('aria-describedby'));
    if (!message) {
      control.removeAttribute('aria-invalid');
      if (existing) existing.remove();
      describedby = describedby.filter(function (id) { return id !== errId; });
      if (describedby.length) control.setAttribute('aria-describedby', describedby.join(' '));
      else control.removeAttribute('aria-describedby');
      return null;
    }
    var host = control.closest('.ui-field, .ui-setting__control') || control.parentNode;
    var err = existing;
    if (!err) {
      err = el('span', 'ui-error');
      err.id = errId;
      host.appendChild(err);
    }
    var changed = err.textContent !== String(message);
    err.textContent = String(message);
    control.setAttribute('aria-invalid', 'true');
    if (describedby.indexOf(errId) < 0) describedby.push(errId);
    control.setAttribute('aria-describedby', describedby.join(' '));
    if (changed && o.announce !== false) announce(message, control);
    if (o.focus && typeof control.focus === 'function') control.focus();
    return err;
  }

  /* ---- Settings rows ---------------------------------------------------- */

  function readValue(control) {
    if (control.type === 'checkbox' || control.type === 'radio') return control.checked ? 'true' : 'false';
    return String(control.value);
  }

  function writeValue(control, value) {
    if (control.type === 'checkbox' || control.type === 'radio') control.checked = String(value) === 'true';
    else control.value = value == null ? '' : String(value);
  }

  function fire(control, type) {
    var e;
    try { e = new global.Event(type, { bubbles: true }); } catch (err) { e = doc.createEvent('Event'); e.initEvent(type, true, false); }
    control.dispatchEvent(e);
  }

  // Put the string table's words into the empty parts of a row: the level marker,
  // the "set by environment" badge and the reset button (with a name of its own, so
  // twenty of them are not twenty identical buttons). Parts the app filled stay.
  // Put a word from the table in an empty element. An element that holds only an icon
  // keeps it: the word becomes its accessible name instead. A name only counts on an
  // element with a role, so a bare span or div that has none gets role="img".
  function fill(node, key, vars) {
    if (!node) return;
    var word = t(key, node, vars);
    if (node.firstElementChild && !node.textContent.trim()) {
      if (!node.hasAttribute('aria-label') || node.__uiWord) {
        node.setAttribute('aria-label', word);
        node.__uiWord = true;
        if (!node.hasAttribute('role') && !/^(BUTTON|A)$/.test(node.tagName)) node.setAttribute('role', 'img');
      }
      return;
    }
    if (!node.textContent.trim() || node.__uiWord) {
      node.textContent = word;
      node.__uiWord = true;
    }
  }

  function words(row) {
    var label = row.querySelector('.ui-setting__label');
    var name = label ? (label.getAttribute('data-ui-name') ||
      (label.querySelector('label') || label).textContent.trim()) : '';
    fill(row.querySelector('.ui-setting__level'), row.getAttribute('data-level') === 'advanced' ? 'advanced' : 'basic');
    fill(row.querySelector('.ui-setting__env'), 'setByEnv');
    var reset = row.querySelector('.ui-setting__reset');
    if (reset) {
      fill(reset, 'reset');
      var iconOnly = reset.__uiWord && !reset.textContent.trim();  // fill() named it; name it fully
      if (!reset.hasAttribute('aria-label') || reset.__uiAutoLabel || iconOnly) {
        reset.setAttribute('aria-label', t('resetNamed', reset, { name: name }));
        reset.__uiAutoLabel = true;
      }
    }
  }

  // setting(row[, { default, onReset }])
  //   Wires a .ui-setting row: the reset button shows only while the control differs
  //   from its default (opts.default, else the row's data-default), clicking it
  //   restores the default and fires input and change; empty badges and the reset
  //   button get their words from the string table; a row with data-source="env"
  //   disables its control, because the environment owns that value.
  // Returns { update(), destroy() }. Calling it again for a row (init() wires every row
  // on load) replaces the earlier wiring instead of adding a second set of handlers.
  function setting(row, opts) {
    var o = opts || {};
    if (row.__uiSetting && typeof row.__uiSetting.destroy === 'function') row.__uiSetting.destroy();
    var control = row.querySelector('.ui-setting__control input, .ui-setting__control select, .ui-setting__control textarea');
    var reset = row.querySelector('.ui-setting__reset');

    words(row);
    var env = row.getAttribute('data-source') === 'env';
    if (env && control) control.disabled = true;

    function defaultValue() {
      if (Object.prototype.hasOwnProperty.call(o, 'default')) return o.default;
      return row.getAttribute('data-default');
    }

    function update() {
      var d = defaultValue();
      var modified = !!control && d !== null && d !== undefined && readValue(control) !== String(d);
      if (modified) row.setAttribute('data-modified', ''); else row.removeAttribute('data-modified');
      if (reset) reset.hidden = !modified || env || (control && control.disabled);
    }

    function onReset(e) {
      e.preventDefault();
      var d = defaultValue();
      if (!control || d === null || d === undefined) return;
      writeValue(control, d);
      fire(control, 'input');
      fire(control, 'change');
      if (typeof o.onReset === 'function') o.onReset(control, row);
      update();
      if (typeof control.focus === 'function') control.focus();
    }

    if (control) {
      control.addEventListener('input', update);
      control.addEventListener('change', update);
    }
    if (reset) reset.addEventListener('click', onReset);
    update();
    var controller = {
      update: update,
      destroy: function () {
        if (control) { control.removeEventListener('input', update); control.removeEventListener('change', update); }
        if (reset) reset.removeEventListener('click', onReset);
        if (row.__uiSetting === controller) row.__uiSetting = null;
      }
    };
    row.__uiSetting = controller;
    return controller;
  }

  /* ---- Icons and emoji ---------------------------------------------------- */

  // icon name -> the emoji and symbols that stand for it (src/icons/icons.json; the
  // build fills this in).
  var ICONS = /*@icons*/{"plus":["\u2795","\uff0b","\u271a"],"x":["\u2715","\u00d7","\u2716","\u2717","\u2718","\u274e"],"check":["\u2713","\u2714","\u2611"],"pencil":["\u270f","\ud83d\udcdd","\u2712","\ud83d\udd8a","\ud83d\udd8b"],"trash":["\ud83d\uddd1"],"copy":["\u29c9"],"save":["\ud83d\udcbe","\ud83d\udcbf","\ud83d\udcc0"],"download":["\u2913"],"upload":["\ud83d\udce4","\u2912"],"refresh-cw":["\ud83d\udd04","\ud83d\udd03","\ud83d\udd01","\u21bb","\u27f3","\u21ba","\u27f2"],"undo-2":["\u238c","\u21b6"],"reply":["\u21a9"],"search":["\ud83d\udd0d","\ud83d\udd0e"],"link":["\ud83d\udd17"],"unlink":[],"external-link":["\u2197","\u2b08"],"pin":["\ud83d\udccc","\ud83d\udccd"],"archive":["\ud83d\uddc3","\ud83d\udce6"],"tag":["\ud83c\udff7","\ud83d\udd16"],"flag":["\ud83d\udea9","\ud83c\udfc1","\ud83c\udff3","\ud83c\udff4"],"eraser":["\ud83e\uddf9","\ud83e\uddfd"],"eye":["\ud83d\udc41","\ud83d\udc40"],"eye-off":["\ud83d\ude48"],"sliders-horizontal":["\ud83c\udf9b","\ud83c\udf9a"],"user-plus":[],"list-checks":[],"menu":["\u2630","\u2261"],"ellipsis":["\u2026","\u22ef"],"ellipsis-vertical":["\u22ee"],"chevron-down":["\u25be","\u25bc","\u2304","\ud83d\udd3d"],"chevron-up":["\u25b4","\u25b2","\u2303","\ud83d\udd3c"],"chevron-right":["\u25b8","\u25b9","\u203a","\u276f"],"chevron-left":["\u25c2","\u25c0","\u25c3","\u2039","\u276e"],"arrow-left":["\u2190","\u2b05","\ud83d\udd19"],"arrow-right":["\u2192","\u27a1","\u27f6"],"arrow-up":["\u2191","\u2b06"],"arrow-down":["\u2193","\u2b07"],"arrow-up-down":["\u21c5","\u2195"],"grip-vertical":["\u283f","\u22ee\u22ee","\u28ff"],"list":[],"maximize-2":["\u26f6","\u2922"],"info":["\u2139","\u24d8","\ud83d\udca1"],"triangle-alert":["\u26a0","\u2757","\u2755","\u203c","\ud83d\udea8"],"circle-check":["\u2705","\ud83d\udfe2"],"circle-x":["\u274c","\ud83d\uded1","\ud83d\udd34"],"ban":["\ud83d\udeab","\u26d4"],"circle-question-mark":["\u2753","\u2754","\u2049","\ud83e\udd14"],"bell":["\ud83d\udd14","\ud83d\udce3","\ud83d\udce2"],"clock":["\u23f0","\u23f1","\u23f2","\u231a","\u231b","\u23f3","\ud83d\udd50","\ud83d\udd51","\ud83d\udd52","\ud83d\udd53","\ud83d\udd54","\ud83d\udd55","\ud83d\udd56","\ud83d\udd57","\ud83d\udd58","\ud83d\udd59","\ud83d\udd5a","\ud83d\udd5b"],"history":["\ud83d\udd70"],"activity":["\ud83e\ude7a","\ud83d\udc93"],"cloud-off":[],"settings":["\u2699","\ud83d\udd29"],"wrench":["\ud83d\udd27","\ud83d\udee0","\ud83d\udd28","\ud83e\uddf0"],"palette":["\ud83c\udfa8","\ud83d\udd8c"],"sun":["\u2600","\ud83c\udf1e","\ud83d\udd06","\ud83d\udd05"],"moon":["\ud83c\udf19","\ud83c\udf1b","\ud83c\udf1c","\u263e","\u263d"],"folder":["\ud83d\udcc1","\ud83d\udcc2","\ud83d\uddc2"],"file-text":["\ud83d\udcc4","\ud83d\udcc3","\ud83d\udcd1","\ud83d\udcdc","\ud83d\udcf0","\ud83d\udcd5"],"book-open":["\ud83d\udcd6","\ud83d\udcda","\ud83d\udcd8","\ud83d\udcd7","\ud83d\udcd9","\ud83d\udcd3","\ud83d\udcd4","\ud83d\udcd2"],"clipboard-list":["\ud83d\udccb","\ud83d\udcbc","\ud83d\uddd2"],"paperclip":["\ud83d\udcce","\ud83d\udd87"],"image":["\ud83d\uddbc","\ud83c\udf9e"],"image-off":[],"camera":["\ud83d\udcf7","\ud83d\udcf8"],"message-circle":["\ud83d\udcac","\ud83d\udde8","\ud83d\udcad","\ud83d\uddef"],"messages-square":[],"mail":["\u2709","\ud83d\udce7","\ud83d\udce8","\ud83d\udce9","\ud83d\udcee"],"mail-open":[],"inbox":["\ud83d\udce5","\ud83d\udcec","\ud83d\udced","\ud83d\udceb","\ud83d\udcea"],"send":[],"chart-column":["\ud83d\udcca","\ud83d\udcc8","\ud83d\udcc9"],"puzzle":["\ud83e\udde9"],"code":[],"music":["\ud83c\udfb5","\ud83c\udfb6","\ud83c\udfbc"],"target":["\ud83c\udfaf"],"user":["\ud83d\udc64","\ud83e\uddd1","\ud83d\udc68","\ud83d\udc69","\ud83d\ude4b"],"users":["\ud83d\udc65","\ud83d\udc6a"],"bot":["\ud83e\udd16","\ud83d\udc7e"],"brain":["\ud83e\udde0"],"sparkles":["\u2728","\ud83c\udf1f","\ud83d\udcab","\u2b50"],"life-buoy":["\ud83d\udedf","\ud83c\udd98"],"lock":["\ud83d\udd12","\ud83d\udd10","\ud83d\udd0f"],"lock-open":["\ud83d\udd13"],"key":["\ud83d\udd11","\ud83d\udddd"],"shield":["\ud83d\udee1"],"shield-check":[],"shield-alert":[],"monitor":["\ud83d\udda5","\ud83d\udcbb","\ud83d\udcfa"],"smartphone":["\ud83d\udcf1","\ud83d\udcf2"],"terminal":["\u2328"],"cpu":[],"database":["\ud83d\uddc4","\ud83d\udee2"],"gauge":[],"power":["\u23fb","\u23fc","\u2b58"],"plug":["\ud83d\udd0c","\ud83d\udd0b"],"globe":["\ud83c\udf10","\ud83c\udf0d","\ud83c\udf0e","\ud83c\udf0f","\ud83d\uddfa"],"flask-conical":["\ud83e\uddea","\u2697","\ud83d\udd2c"],"zap":["\u26a1","\ud83d\udd25","\ud83d\ude80"],"play":["\u25b6","\u23ef","\u25ba"],"pause":["\u23f8"],"square":["\u23f9","\u25a0","\u25fc","\u2b1b"],"mic":["\ud83c\udfa4","\ud83c\udf99"],"thumbs-up":["\ud83d\udc4d","\ud83d\udc4c"],"thumbs-down":["\ud83d\udc4e"]}/*@end-icons*/;
  var SVG = 'http://www.w3.org/2000/svg';
  var sprite = (script && script.getAttribute('data-icons')) || base + 'icons.svg';
  var byEmoji = {};
  Object.keys(ICONS).forEach(function (name) {
    ICONS[name].forEach(function (emoji) { byEmoji[emojiKey(emoji)] = name; });
  });

  // A pictograph or flag, with its variation selector, skin tone and ZWJ parts.
  var EMOJI = null;
  var SHOWN = null;  // ...and drawn as an emoji by default, not as a text symbol like ↗
  try {
    EMOJI = new RegExp('(?:\\p{Regional_Indicator}{2}|[#*0-9]\\uFE0F?\\u20E3|\\p{Extended_Pictographic}[\\uFE0E\\uFE0F]?\\p{Emoji_Modifier}?' +
      '(?:\\u200D\\p{Extended_Pictographic}[\\uFE0E\\uFE0F]?\\p{Emoji_Modifier}?)*)', 'gu');
    SHOWN = new RegExp('^\\p{Emoji_Presentation}|[\\uFE0F\\u200D\\u20E3]', 'u');
  } catch (e) { EMOJI = null; }

  function emojiKey(text) {
    return String(text).replace(/[︎️]|\uD83C[\uDFFB-\uDFFF]/g, '');
  }

  // iconFor('⚙️') -> 'settings'; null when no icon means it. A ZWJ sequence (🧑‍💻)
  // falls back to its first emoji.
  function iconFor(text) {
    var key = emojiKey(String(text == null ? '' : text).trim());
    var has = Object.prototype.hasOwnProperty;
    if (has.call(byEmoji, key)) return byEmoji[key];
    var first = key.split('‍')[0];
    return has.call(byEmoji, first) ? byEmoji[first] : null;
  }

  // <use> cannot reach a sprite on another origin (the CDN install), so that sprite
  // is fetched once and put in the page, its ids prefixed with ui-icon-.
  var crossOrigin = null;
  var inlined = null;
  function href(name) {
    if (crossOrigin === null) {
      // self.origin, not location.origin: an about:srcdoc frame shares its parent's origin.
      try { crossOrigin = new URL(sprite, doc.baseURI).origin !== (global.origin || global.location.origin); }
      catch (e) { crossOrigin = false; }
    }
    if (!crossOrigin) return sprite + '#' + name;
    if (!inlined && global.fetch) {
      inlined = global.fetch(sprite).then(function (r) { return r.ok ? r.text() : ''; }).then(function (text) {
        var parsed = new global.DOMParser().parseFromString(text, 'image/svg+xml').documentElement;
        if (!parsed || parsed.nodeName !== 'svg') return;
        var symbols = parsed.querySelectorAll('symbol[id]');
        for (var i = 0; i < symbols.length; i++) symbols[i].id = 'ui-icon-' + symbols[i].id;
        var host = doc.importNode(parsed, true);
        host.setAttribute('aria-hidden', 'true');
        host.setAttribute('style', 'position:absolute;width:0;height:0;overflow:hidden');
        (doc.body || doc.documentElement).appendChild(host);
      }).catch(function () {});
    }
    return '#ui-icon-' + name;
  }

  // icon('trash', { label: 'Delete', size: 'sm' }) -> <svg class="ui-icon"> that uses
  // the sprite. With no label it is decoration (aria-hidden); with one, an image.
  function icon(name, opts) {
    opts = opts || {};
    if (!Object.prototype.hasOwnProperty.call(ICONS, name) && global.console) {
      global.console.warn('UIComponents.icon: no icon named "' + name + '" (see ui-theme/icons.json)');
    }
    var svg = doc.createElementNS(SVG, 'svg');
    svg.setAttribute('class', 'ui-icon' + (opts.size ? ' ui-icon--' + opts.size : '') +
      (opts.className ? ' ' + opts.className : ''));
    if (opts.label) {
      svg.setAttribute('role', 'img');
      svg.setAttribute('aria-label', opts.label);
    } else {
      svg.setAttribute('aria-hidden', 'true');
    }
    var use = doc.createElementNS(SVG, 'use');
    use.setAttribute('href', href(name));
    svg.appendChild(use);
    return svg;
  }

  function icons() { return Object.keys(ICONS); }

  // One emoji drawn in a style: 'icon' swaps it for its icon (the emoji stays in the
  // page for copy, find and screen readers) or, with no icon, falls back to 'text';
  // 'text' is the text colour; 'color' keeps its colours.
  function emojiNode(seq, style) {
    var name = style === 'icon' ? iconFor(seq) : null;
    if (name) {
      var frag = doc.createDocumentFragment();
      frag.appendChild(icon(name));
      frag.appendChild(el('span', 'ui-visually-hidden', seq));
      return frag;
    }
    if (style === 'color') return el('span', 'ui-emoji--color', seq);
    var bare = seq.replace(/[︎️]/g, '');
    // U+FE0E asks for the text glyph; it is only valid after a single code point.
    return el('span', 'ui-emoji', Array.from(bare).length === 1 ? bare + '︎' : seq);
  }

  var NO_EMOJIFY = 'code, pre, kbd, samp, script, style, textarea, input, select, svg, [contenteditable], ' +
    '.ui-emoji, .ui-emoji--color, .ui-visually-hidden';

  // emojify(element, style): draws the emoji in the element's text in one style
  // ('icon' by default, 'text' or 'color'). A text node that is nothing but one mapped
  // symbol (a button reading ⚙ or ✕) also becomes its icon. Code, inputs and editable
  // text are left alone. Returns how many it changed; running it again changes none.
  function emojify(root, style) {
    style = style === 'text' || style === 'color' ? style : 'icon';
    if (!EMOJI || !root) return 0;
    var walker = doc.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */);
    var nodes = [];
    var node;
    while ((node = walker.nextNode())) {
      var parent = node.parentElement;
      var area = parent && parent.closest('[data-ui-emoji]');
      // A nested [data-ui-emoji] area keeps its own style (init() does it).
      if (parent && !parent.closest(NO_EMOJIFY) && !(area && area !== root && root.contains(area))) nodes.push(node);
    }
    var count = 0;
    for (var i = 0; i < nodes.length; i++) {
      var text = nodes[i].nodeValue;
      var lone = style === 'icon' && text.trim() && iconFor(text) ? text.trim() : null;
      var out = doc.createDocumentFragment();
      var last = 0;
      var changed = 0;
      var m;
      EMOJI.lastIndex = 0;
      while ((m = EMOJI.exec(text))) {
        if (!lone && !SHOWN.test(m[0])) continue;
        if (lone && m[0] !== lone) continue;
        if (m.index > last) out.appendChild(doc.createTextNode(text.slice(last, m.index)));
        out.appendChild(emojiNode(m[0], style));
        last = m.index + m[0].length;
        changed++;
      }
      if (lone && !changed) {  // a mapped text symbol (✕ ▸ ☰) that is not a pictograph
        last = text.indexOf(lone);
        if (last) out.appendChild(doc.createTextNode(text.slice(0, last)));
        out.appendChild(emojiNode(lone, style));
        last += lone.length;
        changed = 1;
      }
      if (!changed) continue;
      if (last < text.length) out.appendChild(doc.createTextNode(text.slice(last)));
      nodes[i].parentNode.replaceChild(out, nodes[i]);
      count += changed;
    }
    return count;
  }

  /* ---- Page wiring ------------------------------------------------------ */

  // init([root]): fills empty copy buttons with the word for "Copy", wires every
  // .ui-setting row (words that came from the table are re-translated when it
  // changes) and runs emojify() on every [data-ui-emoji="icon|text|color"] element.
  // Runs on DOMContentLoaded and after a locale loads; call it after adding such
  // markup later.
  function init(root) {
    root = root || doc;
    var areas = root.querySelectorAll ? [].slice.call(root.querySelectorAll('[data-ui-emoji]')) : [];
    if (root.matches && root.matches('[data-ui-emoji]')) areas.unshift(root);
    for (var a = 0; a < areas.length; a++) emojify(areas[a], areas[a].getAttribute('data-ui-emoji'));
    var buttons = root.querySelectorAll ? root.querySelectorAll(COPY_BUTTON) : [];
    for (var i = 0; i < buttons.length; i++) {
      if (!buttons[i].hasAttribute('aria-label') || buttons[i].__uiWord) fill(buttons[i], 'copy');
    }
    var rows = root.querySelectorAll ? root.querySelectorAll('.ui-setting') : [];
    for (var j = 0; j < rows.length; j++) {
      if (rows[j].__uiSetting) words(rows[j]); else setting(rows[j]);
    }
  }

  // One delegated handler, so copy buttons added later work too.
  doc.addEventListener('click', function (e) {
    var button = e.target && e.target.closest ? e.target.closest(COPY_BUTTON) : null;
    if (!button || button.disabled || button.getAttribute('data-ui-copy') === 'off') return;
    copy(button);
  });

  resetStrings();
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', function () { init(doc); });
  else init(doc);

  var wanted = script && script.getAttribute('data-locale');
  if (wanted) loadLocale(wanted === 'auto' ? locale() : wanted);

  global.UIComponents = {
    confirm: function (arg) { return modal('confirm', arg); },
    alert: function (arg) { return modal('alert', arg); },
    prompt: function (arg) { return modal('prompt', arg); },
    toast: toast,
    icon: icon,
    iconFor: iconFor,
    icons: icons,
    emojify: emojify,
    copy: copy,
    validate: validate,
    setting: setting,
    announce: announce,
    init: init,
    t: t,
    strings: strings,
    setStrings: setStrings,
    resetStrings: resetStrings,
    loadLocale: loadLocale,
    locale: locale,
    formatNumber: formatNumber,
    formatDate: formatDate
  };
})(window);
