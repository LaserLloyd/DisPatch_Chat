// Outside sign-in (OIDC, 4.3 issue #24): the Owner's settings card and the per-profile
// "Link sign-in" button for People.
//
//   GET/PUT  /api/oidc/config           the protected Admin only (who may sign in is host-level)
//   GET      /api/oidc/links            the caller's own link; the protected Admin sees all
//   DELETE   /api/oidc/links/{id}       unlink: the profile itself or the protected Admin
//   GET      /api/oidc/start?link=<id>  a page navigation: the provider's sign-in, then back
//
// No work at import time. `deps` = {request, t, el, toast, navigate}; `request` defaults to
// a same-origin JSON fetch so the module needs nothing from api.js (tests hand in stubs).

/** A same-origin JSON call: resolves the body, rejects with an Error carrying status/code. */
export async function defaultRequest(method, url, body) {
  const opts = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (!r.ok) {
    const env = data && data.error;
    const e = new Error((env && env.message) || (data && data.detail) || `HTTP ${r.status}`);
    e.status = r.status;
    if (env && env.code) e.code = env.code;
    if (env && env.fix) e.fix = env.fix;
    throw e;
  }
  return data;
}

/** Only the protected Admin (the Owner) gets the card. `me` = /api/auth/me or status. */
export function showsOidcCard(me) {
  return !!(me && me.profile && me.profile.protected);
}

/** The PUT body from the form values. An empty secret keeps the stored one. */
export function configBody(v) {
  const body = {
    enabled: !!v.enabled,
    issuer: String(v.issuer || '').trim(),
    client_id: String(v.client_id || '').trim(),
    scopes: String(v.scopes || '').trim().replace(/\s+/g, ' ') || 'openid email profile',
    label: String(v.label || '').trim(),
  };
  const secret = String(v.client_secret || '').trim();
  if (secret) body.client_secret = secret;
  if (v.clear_secret) body.clear_secret = true;
  return body;
}

const errText = (e, t) => {
  const msg = (e && e.message) || t('oidc.save_failed');
  return e && e.fix ? `${msg} ${e.fix}` : msg;
};

/** Mount the "Outside sign-in" card into `host` (replaces an earlier one). Returns the card,
 *  or null for anyone but the protected Admin. */
export function renderOidcCard(host, deps) {
  if (!host) return null;
  const { t, el, toast = () => {}, me } = deps;
  const request = deps.request || defaultRequest;
  for (const old of host.querySelectorAll('[data-anchor="oidc"]')) old.remove();
  if (!showsOidcCard(me)) return null;

  const field = (id, name, text, attrs = {}) => {
    const input = el('input', { id, name, type: 'text', autocomplete: 'off', spellcheck: 'false', ...attrs });
    return { input, row: el('div', { class: 'sec-field oidc-field' }, [el('label', { for: id, text }), input]) };
  };
  const label = field('oidc-label', 'label', t('oidc.label'), { maxlength: '60', placeholder: t('oidc.label_placeholder') });
  const issuer = field('oidc-issuer', 'issuer', t('oidc.issuer'), { type: 'url', inputmode: 'url', placeholder: 'https://' });
  const clientId = field('oidc-client-id', 'client_id', t('oidc.client_id'));
  const secret = field('oidc-client-secret', 'client_secret', t('oidc.client_secret'), { type: 'password', autocomplete: 'new-password' });
  const scopes = field('oidc-scopes', 'scopes', t('oidc.scopes'));
  const enabled = el('input', { id: 'oidc-enabled', type: 'checkbox' });
  const clearSecret = el('button', { type: 'button', class: 'ui-btn oidc-clear-secret', text: t('oidc.clear_secret') });
  const redirect = el('code', { class: 'oidc-redirect' });
  const status = el('p', { class: 'dev-group-hint oidc-status', role: 'status', 'aria-live': 'polite' });
  const warn = el('p', { class: 'oidc-warn', role: 'alert', hidden: '' });
  const save = el('button', { type: 'submit', class: 'btn-primary oidc-save', text: t('oidc.save') });

  const form = el('form', { class: 'oidc-form', novalidate: '' }, [
    label.row, issuer.row, clientId.row, secret.row, scopes.row,
    el('div', { class: 'oidc-toggle-row' }, [enabled, el('label', { for: 'oidc-enabled', text: t('oidc.enabled') })]),
    el('p', { class: 'dev-group-hint' }, [el('span', { text: `${t('oidc.redirect_uri')} ` }), redirect]),
    warn, status,
    el('div', { class: 'bm-actions oidc-actions' }, [save, clearSecret]),
  ]);
  const card = el('section', { class: 'dev-group oidc-card', 'data-anchor': 'oidc', 'aria-labelledby': 'oidc-h' }, [
    el('h3', { class: 'dev-group-title', id: 'oidc-h', text: t('oidc.title') }),
    el('p', { class: 'dev-group-hint', text: t('oidc.sum') }),
    el('p', { class: 'dev-group-hint', text: t('oidc.owner_keeps_pin') }),
    form,
  ]);
  host.append(card);

  let clearing = false;
  const paint = (cfg) => {
    label.input.value = cfg.label || '';
    issuer.input.value = cfg.issuer || '';
    clientId.input.value = cfg.client_id || '';
    scopes.input.value = cfg.scopes || 'openid email profile';
    secret.input.value = '';
    secret.input.placeholder = cfg.has_secret ? t('oidc.secret_saved') : '';
    clearSecret.hidden = !cfg.has_secret;
    enabled.checked = !!cfg.enabled;
    redirect.textContent = cfg.redirect_uri || t('oidc.no_public_url');
    warn.hidden = !!cfg.public_url_ok;
    warn.textContent = cfg.public_url_ok ? '' : t('oidc.public_url_needed');
    status.textContent = cfg.active ? t('oidc.status_on', { count: cfg.links || 0 }) : t('oidc.status_off');
    clearing = false;
  };
  card._paint = paint;

  const submit = async () => {
    save.disabled = true;
    try {
      const out = await request('PUT', '/api/oidc/config', configBody({
        enabled: enabled.checked, issuer: issuer.input.value, client_id: clientId.input.value,
        client_secret: secret.input.value, clear_secret: clearing, scopes: scopes.input.value, label: label.input.value,
      }));
      paint(out);
      toast(t('oidc.saved'));
    } catch (e) {
      toast(errText(e, t), true);
    } finally {
      save.disabled = false;
    }
  };
  form.addEventListener('submit', (ev) => { ev.preventDefault(); submit(); });
  clearSecret.addEventListener('click', () => { clearing = true; secret.input.value = ''; submit(); });

  card.ready = request('GET', '/api/oidc/config').then(paint).catch((e) => {
    status.textContent = errText(e, t);
  });
  return card;
}

/**
 * The per-profile "Link sign-in" / "Unlink sign-in" button for People (WP-INT wires it).
 *   profile   the row's profile ({id, name, ...})
 *   ctx       {me, enabled, links, t, el, request?, navigate?, toast?, onChange?}
 *             me = /api/auth/me; enabled + links = GET /api/oidc/links
 * Null when outside sign-in is off, or the viewer is neither that profile nor the Owner.
 */
export function linkButtonFor(profile, ctx) {
  const { me, enabled, links = [], t, el, toast = () => {}, onChange = () => {} } = ctx || {};
  if (!profile || !enabled || !me || !me.profile) return null;
  const self = me.profile.id === profile.id;
  if (!self && !me.profile.protected) return null;
  const request = ctx.request || defaultRequest;
  const navigate = ctx.navigate || ((url) => window.location.assign(url));
  const linked = links.find((l) => l.profile_id === profile.id);
  // The server refuses a link for the shared default profile and a disabled one (403
  // oidc_link_forbidden): never offer a button that lands on an error page (4.3 review).
  if (!linked && (profile.lock_target || profile.shared || profile.disabled)) return null;
  if (!linked) {
    const b = el('button', { type: 'button', class: 'ui-btn oidc-link', 'data-profile': profile.id, text: t('oidc.link') });
    b.addEventListener('click', () => {
      b.disabled = true;
      navigate(`/api/oidc/start?link=${encodeURIComponent(profile.id)}`);
    });
    return b;
  }
  const b = el('button', {
    type: 'button', class: 'ui-btn oidc-unlink', 'data-profile': profile.id, text: t('oidc.unlink'),
    title: linked.email_at_link ? t('oidc.linked_as', { email: linked.email_at_link }) : t('oidc.linked'),
  });
  b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      await request('DELETE', `/api/oidc/links/${encodeURIComponent(profile.id)}`);
      toast(t('oidc.unlinked'));
      onChange();
    } catch (e) {
      toast(errText(e, t), true);
      b.disabled = false;
    }
  });
  return b;
}
