// Web Push — Settings → Device → Notifications.
//
// A ping on this device's lock screen when a bot replies, a picture is ready
// or a scheduled post lands: "Nova replied", never what Nova said. The server
// (backend/app/push.py) only ever sends {kind, thread_id, title}; tapping the
// notification opens the thread (sw.js `notificationclick`).
//
// Rules the row follows:
//   * Safe Mode: not built at all (the routes 403 anyway).
//   * Push needs a secure context. http://<lan-ip>:8765 is not one, so there
//     the switch is disabled and says to open the https address instead.
//   * The server binds a subscription to a REMEMBERED device; on an ordinary
//     session it answers 403 and the row says how to get one.
//
// Pure DOM + fetch, no imports: the module is precached by sw.js like the
// rest of the shell and can be unit-tested with a fake window.

const ENDPOINT = '/api/push';

/** Can this browser, on this address, do push at all?
 *  reason: '' | 'insecure' | 'unsupported' */
export function pushSupport(win = globalThis) {
  if (!win || !win.isSecureContext) return { ok: false, reason: 'insecure' };
  const nav = win.navigator || {};
  if (typeof win.Notification === 'undefined' || !nav.serviceWorker
      || typeof win.PushManager === 'undefined') {
    return { ok: false, reason: 'unsupported' };
  }
  return { ok: true, reason: '' };
}

/** base64url (the VAPID public key) -> the Uint8Array subscribe() wants. */
export function urlBase64ToUint8Array(b64) {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const bin = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

async function call(win, method, path, body) {
  const r = await win.fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const err = new Error((data && data.detail) || r.statusText || 'request failed');
    err.status = r.status;
    throw err;
  }
  return data;
}

function withTimeout(promise, ms, what) {
  return Promise.race([promise, new Promise((_, reject) => {
    setTimeout(() => reject(new Error(`${what} timed out`)), ms);
  })]);
}

async function registration(win) {
  // `ready` never settles when no worker is registered (privacy mode never
  // registers one), so it gets a deadline instead of a spinner forever.
  return withTimeout(win.navigator.serviceWorker.ready, 5000, 'service worker');
}

/** Subscribe this device and register it with the server. */
export async function enablePush(win = globalThis) {
  const perm = await win.Notification.requestPermission();
  if (perm !== 'granted') {
    const e = new Error('permission'); e.code = 'denied'; throw e;
  }
  const { public_key: key } = await call(win, 'GET', `${ENDPOINT}/vapid`);
  const reg = await registration(win);
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key),
    });
  }
  await call(win, 'POST', `${ENDPOINT}/subscribe`, sub.toJSON());
  return sub;
}

/** Forget this device's subscription on both ends. */
export async function disablePush(win = globalThis) {
  let sub = null;
  try { sub = await (await registration(win)).pushManager.getSubscription(); } catch { /* none */ }
  const q = sub ? `?endpoint=${encodeURIComponent(sub.endpoint)}` : '';
  await call(win, 'DELETE', `${ENDPOINT}/subscribe${q}`);
  if (sub) { try { await sub.unsubscribe(); } catch { /* already gone */ } }
}

/** The Settings row, or null in Safe Mode. `t` is i18n's translate. */
export function pushRow(t, { decoy = false, win = globalThis, onToast } = {}) {
  if (decoy) return null;
  const doc = win.document;
  const label = doc.createElement('label');
  label.className = 'bm-avatar-style dev-row';
  label.id = 'push-row';
  const box = doc.createElement('input');
  box.type = 'checkbox';
  box.id = 'push-toggle';
  const text = doc.createElement('span');
  text.className = 'dev-row-text';
  const strong = doc.createElement('strong');
  strong.textContent = t('push.title');
  const hint = doc.createElement('span');
  hint.className = 'dev-row-hint';
  hint.id = 'push-hint';
  text.append(strong, hint);
  label.append(box, text);

  const say = (key) => { hint.textContent = t(key); };
  const support = pushSupport(win);
  if (!support.ok) {
    box.disabled = true;
    say(support.reason === 'insecure' ? 'push.needs_https' : 'push.unsupported');
    label.dataset.state = support.reason;
    return label;
  }
  say('push.hint');

  // Current state: the server knows whether THIS device is subscribed; the
  // browser's permission can have been revoked since, which wins.
  box.disabled = true;
  (async () => {
    let endpoint = '';
    try {
      const reg = await registration(win);
      const sub = await reg.pushManager.getSubscription();
      endpoint = sub ? sub.endpoint : '';
    } catch { /* no worker yet */ }
    try {
      const q = endpoint ? `?endpoint=${encodeURIComponent(endpoint)}` : '';
      const s = await call(win, 'GET', `${ENDPOINT}/vapid${q}`);
      box.checked = Boolean(s.subscribed && endpoint && win.Notification.permission === 'granted');
      box.disabled = false;
    } catch (e) {
      box.checked = false;
      if (e.status === 403 && /remember/i.test(e.message)) {
        say('push.needs_remember');
        label.dataset.state = 'remember';
      } else {
        box.disabled = false;
      }
    }
  })();

  box.addEventListener('change', async () => {
    const want = box.checked;
    box.disabled = true;
    try {
      if (want) {
        await enablePush(win);
        say('push.hint');
        if (onToast) onToast(t('push.on'));
      } else {
        await disablePush(win);
        if (onToast) onToast(t('push.off'));
      }
    } catch (e) {
      box.checked = !want;
      if (e.code === 'denied') say('push.denied');
      else if (e.status === 403 && /remember/i.test(e.message)) {
        say('push.needs_remember');
        label.dataset.state = 'remember';
      } else if (onToast) onToast(t('push.failed'));
    } finally {
      box.disabled = label.dataset.state === 'remember';
    }
  });
  return label;
}
