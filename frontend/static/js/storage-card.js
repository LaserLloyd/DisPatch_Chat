// Settings -> System: the Storage card and the "Generated images: keep" card (4.0 scale round,
// BUILD-PLAN 1.13.4 and 1.13.6). Admin only: the routes answer 403 to anyone else, and main.js
// does not mount the cards for them.
//
//   GET  /api/maintenance/status            last run per step, next run, blocked, running
//   GET  /api/maintenance/storage           size per area, free disk, stray .bak files
//   POST /api/maintenance/storage/refresh   walk the data folder again (202)
//   POST /api/maintenance/run               "Run now" (202)
//   GET  /api/maintenance/images/preview    the dry run: what N months would expire
//   GET/PUT /api/maintenance/retention      the saved setting
//
// No work at import time. `deps` = {api, t, el, toast} so tests can hand in stubs.

/** 1536 -> "1.5 KB". Binary units; whole numbers from MB up read cleaner without a decimal at 100+. */
export function formatBytes(n) {
  let v = Number(n);
  if (!Number.isFinite(v) || v < 0) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  if (i === 0) return `${Math.round(v)} B`;
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** The rows of the size table, in display order, with the API's finer areas folded into the
 *  Foundation's labels: reactions = spent + shelf, backups = every tier, database = db + wal. */
export function areaRows(report) {
  const a = (report && report.areas) || {};
  const sum = (...keys) => keys.reduce((s, k) => s + ((a[k] && a[k].bytes) || 0), 0);
  const has = (...keys) => keys.some((k) => a[k] && a[k].bytes != null);
  const rows = [
    ['database', sum('database', 'wal'), has('database', 'wal')],
    ['media', sum('media'), has('media')],
    ['reactions', sum('reactions_spent', 'reactions_shelf'), has('reactions_spent', 'reactions_shelf')],
    ['imagestudio', sum('imagestudio'), has('imagestudio')],
    ['avatars', sum('avatars'), has('avatars')],
    ['avatar_pool', sum('avatar_pool'), has('avatar_pool')],
    ['files', sum('files'), has('files')],
    ['voices', sum('voices'), has('voices')],
    ['backups', sum('backups_6h', 'backups_daily', 'backups_weekly', 'backups_other'), has('backups_6h', 'backups_daily', 'backups_weekly', 'backups_other')],
    ['trash', sum('trash'), has('trash')],
    ['stray', sum('stray_bak'), has('stray_bak')],
    ['rest', sum('other'), has('other')],
  ];
  return rows.filter((r) => r[2]).map(([key, bytes]) => ({ key, bytes }));
}

/** "activity_prune" -> "activity prune". The step ids are the server's; they are shown, not translated. */
export function stepLabel(id) { return String(id || '').replace(/[_.]+/g, ' '); }

/** The last run's steps, failures first. */
export function stepRows(lastRun) {
  const steps = (lastRun && Array.isArray(lastRun.steps)) ? lastRun.steps.slice() : [];
  return steps.sort((x, y) => (y.error ? 1 : 0) - (x.error ? 1 : 0));
}

/** "2 min 5 s" / "840 ms" for a run's duration. */
export function formatDuration(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1000) return `${Math.round(n)} ms`;
  const s = Math.round(n / 1000);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${s % 60} s`;
}

const when = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso || '') : d.toLocaleString();
};

// ------------------------------------------------------------------ Storage card

/** Mount the Storage card into `host` (replaces what an earlier mount left). Returns
 *  {card, stop}: stop() ends the polling (the card also stops itself when it leaves the page). */
export function renderStorageCard(host, deps) {
  if (!host) return null;
  const { api, t, el, toast } = deps;
  for (const old of host.querySelectorAll('[data-anchor="storage"]')) { if (old._stop) old._stop(); old.remove(); }

  const summary = el('p', { class: 'dev-group-hint storage-summary', 'aria-live': 'polite' });
  const list = el('ul', { class: 'storage-areas', 'aria-label': t('storage.by_area') });
  const free = el('p', { class: 'storage-free' });
  const runBox = el('div', { class: 'storage-run' });
  const stray = el('div', { class: 'storage-stray' });
  const runBtn = el('button', { type: 'button', class: 'ui-btn storage-run-btn', text: t('storage.run_now') });
  const refreshBtn = el('button', { type: 'button', class: 'ui-btn storage-refresh-btn', text: t('storage.refresh') });
  const card = el('section', { class: 'dev-group storage-card', 'data-anchor': 'storage', 'aria-labelledby': 'storage-h' }, [
    el('h3', { class: 'dev-group-title', id: 'storage-h', text: t('storage.title') }),
    el('p', { class: 'dev-group-hint', text: t('storage.sum') }),
    summary, list, free, runBox,
    el('div', { class: 'bm-actions storage-actions' }, [runBtn, refreshBtn]),
    stray,
  ]);
  host.append(card);

  let timer = 0;
  let stopped = false;
  let asked = false;          // the first-time folder walk was requested
  const stop = () => { stopped = true; if (timer) { clearTimeout(timer); timer = 0; } };
  card._stop = stop;

  const paint = (status, report) => {
    // sizes
    const rows = areaRows(report);
    const biggest = Math.max(1, ...rows.map((r) => r.bytes));
    list.replaceChildren(...rows.map((r) => el('li', { class: 'storage-area', 'data-area': r.key }, [
      el('span', { class: 'storage-area-name', text: t(`storage.${r.key}`) }),
      el('span', { class: 'storage-area-bar', 'aria-hidden': 'true' }, [
        (() => { const b = el('span', { class: 'storage-area-fill' }); b.style.width = `${Math.max(1, Math.round((r.bytes / biggest) * 100))}%`; return b; })(),
      ]),
      el('span', { class: 'storage-area-size', text: formatBytes(r.bytes) }),
    ])));
    if (report && report.measured_at) {
      summary.textContent = `${t('storage.measured', { when: when(report.measured_at) })} · ${t('storage.total')}: ${formatBytes(report.total_bytes)}`;
    } else {
      summary.textContent = (report && report.scanning) ? t('storage.scanning') : t('storage.never_run');
    }
    free.textContent = (report && report.free_disk_bytes != null)
      ? `${t('storage.free')}: ${formatBytes(report.free_disk_bytes)}` : '';
    // last run, per step
    const last = status && status.last_run;
    const parts = [];
    if (status && status.running) parts.push(el('p', { class: 'storage-running', role: 'status', text: t('storage.running') }));
    else if (status && status.blocked) parts.push(el('p', { class: 'storage-blocked', role: 'status', text: t('storage.blocked') }));
    if (last) {
      parts.push(el('p', { class: 'storage-lastrun' + (last.ok === false ? ' failed' : ''), text: t('storage.last_run', { when: when(last.started), duration: formatDuration(last.ms) }) }));
      const steps = stepRows(last);
      if (steps.length) {
        parts.push(el('ul', { class: 'storage-steps' }, steps.map((s) => el('li', { class: 'storage-step' + (s.error ? ' failed' : ''), 'data-step': s.step }, [
          el('span', { class: 'storage-step-name', text: stepLabel(s.step) }),
          el('span', { class: 'storage-step-note', text: s.error ? t('storage.step_failed', { step: stepLabel(s.step), error: s.error }) : t('storage.step_rows', { rows: s.rows || 0 }) }),
        ]))));
      }
    } else if (status && !status.running) {
      parts.push(el('p', { class: 'storage-lastrun', text: t('storage.never_run') }));
    }
    if (status && status.next_run) parts.push(el('p', { class: 'storage-nextrun dev-group-hint', text: t('storage.next_run', { when: when(status.next_run) }) }));
    runBox.replaceChildren(...parts);
    // stray .bak files: listed, never deleted
    const files = (report && report.stray) || [];
    if (files.length) {
      stray.replaceChildren(
        el('h4', { class: 'storage-stray-h', text: t('storage.stray') }),
        el('p', { class: 'dev-group-hint', text: t('storage.stray_hint') }),
        el('ul', { class: 'storage-stray-list' }, files.map((f) => el('li', {}, [
          el('code', { text: f.name }), el('span', { class: 'storage-area-size', text: ` ${formatBytes(f.bytes)}` }),
        ]))),
      );
    } else stray.replaceChildren();
    const busy = !!(status && status.running) || !!(report && report.scanning);
    runBtn.disabled = !!(status && (status.running || status.blocked));
    runBtn.textContent = (status && status.running) ? t('storage.running') : t('storage.run_now');
    refreshBtn.disabled = !!(report && report.scanning);
    return busy;
  };

  const load = async () => {
    if (stopped) return;
    if (!card.isConnected) { stop(); return; }
    let status = null; let report = null;
    try {
      [status, report] = await Promise.all([api.maintenanceStatus(), api.maintenanceStorage()]);
    } catch (e) {
      if (e && e.status === 403) { stop(); card.remove(); return; }
      summary.textContent = (e && e.message) || String(e);
      return;
    }
    if (stopped) return;
    // Nothing measured yet (a fresh install, or the nightly run has not happened): walk the folder once.
    if (report && !report.measured_at && !report.scanning && !asked) {
      asked = true;
      try { await api.maintenanceStorageRefresh(); report = { ...report, scanning: true }; } catch { /* the button is still there */ }
    }
    const busy = paint(status, report);
    if (busy) timer = setTimeout(load, 3000);
  };

  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    try { await api.maintenanceRun(); toast(t('storage.started')); } catch (e) { toast((e && e.message) || String(e), true); }
    stop(); stopped = false; load();
  });
  refreshBtn.addEventListener('click', async () => {
    refreshBtn.disabled = true;
    try { await api.maintenanceStorageRefresh(); } catch (e) { toast((e && e.message) || String(e), true); }
    stop(); stopped = false; load();
  });

  const ready = load();
  return { card, stop, ready };
}

// ------------------------------------------------------------------ Generated images card

export const MONTHS_DEFAULT = 12;
export const MONTHS_MIN = 1;
export const MONTHS_MAX = 120;

/** The months field as the API wants it, or null when it is not a whole number in range. */
export function parseMonths(raw) {
  const s = String(raw == null ? '' : raw).trim();
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return n >= MONTHS_MIN && n <= MONTHS_MAX ? n : null;
}

/** Total of the preview's "protected" counts (they are keyed by reason). */
export function protectedTotal(preview) {
  const p = (preview && preview.protected) || {};
  return Object.values(p).reduce((s, v) => s + (Number(v) || 0), 0);
}

/** The value the card currently shows: 'forever' or the months. */
export function cardValue(mode, monthsRaw) {
  if (mode !== 'months') return { mode: 'forever', months: null };
  return { mode: 'months', months: parseMonths(monthsRaw) };
}

/** Save is allowed for 'forever' (the safe direction) or for months that were previewed as is. */
export function canSave(value, previewed) {
  if (value.mode === 'forever') return true;
  return value.months !== null && !!previewed && previewed.mode === 'months' && previewed.months === value.months;
}

const K_MODE = 'system.retention.images.mode';
const K_MONTHS = 'system.retention.images.months';

export function renderRetentionCard(host, deps) {
  if (!host) return null;
  const { api, t, el, toast } = deps;
  for (const old of host.querySelectorAll('[data-anchor="retention"]')) old.remove();

  const mode = el('select', { id: 'retention-images-mode', class: 'ui-select retention-mode', disabled: 'disabled', 'aria-label': t('retention.images_title') }, [
    el('option', { value: 'forever', text: t('retention.forever') }),
    el('option', { value: 'months', text: t('retention.months') }),
  ]);
  const months = el('input', {
    id: 'retention-images-months', class: 'ui-input retention-months', type: 'number', inputmode: 'numeric',
    min: String(MONTHS_MIN), max: String(MONTHS_MAX), step: '1', value: String(MONTHS_DEFAULT),
    'aria-label': t('retention.months_label'),
  });
  const monthsWrap = el('label', { class: 'retention-months-wrap' }, [months, el('span', { class: 'retention-months-unit', text: t('retention.months_label') })]);
  monthsWrap.hidden = true;
  const check = el('button', { type: 'button', class: 'ui-btn retention-check', text: t('retention.check') });
  const save = el('button', { type: 'button', class: 'ui-btn ui-btn--primary retention-save', text: t('retention.save'), disabled: 'disabled' });
  const result = el('div', { class: 'retention-result', 'aria-live': 'polite' });
  const card = el('section', { class: 'dev-group retention-card', 'data-anchor': 'retention', 'aria-labelledby': 'retention-h' }, [
    el('h3', { class: 'dev-group-title', id: 'retention-h', text: t('retention.images_title') }),
    el('p', { class: 'dev-group-hint', text: t('retention.hint') }),
    el('div', { class: 'retention-row' }, [mode, monthsWrap, check]),
    result,
    el('p', { class: 'dev-group-hint', text: t('retention.trash_note') }),
    el('div', { class: 'bm-actions' }, [save]),
  ]);
  host.append(card);

  let saved = { mode: 'forever', months: null };
  let previewed = null;           // {mode, months} of the last good preview
  let seq = 0;

  const value = () => cardValue(mode.value, months.value);
  const sync = () => {
    const v = value();
    monthsWrap.hidden = mode.value !== 'months';
    check.disabled = v.mode === 'forever' || v.months === null;
    save.disabled = !canSave(v, previewed)
      || (v.mode === saved.mode && v.months === saved.months);
    months.setAttribute('aria-invalid', mode.value === 'months' && v.months === null ? 'true' : 'false');
  };
  const invalidate = () => { previewed = null; seq += 1; result.replaceChildren(); sync(); };

  mode.addEventListener('change', () => {
    // N months starts at 12 (the default) when it was just picked
    if (mode.value === 'months' && parseMonths(months.value) === null) months.value = String(MONTHS_DEFAULT);
    invalidate();
  });
  months.addEventListener('input', invalidate);

  check.addEventListener('click', async () => {
    const v = value();
    if (v.mode !== 'months' || v.months === null) return;
    const mine = ++seq;
    check.disabled = true;
    result.textContent = t('common.loading');
    try {
      const p = await api.imagePreview(v.months);
      if (mine !== seq) return;               // the field moved on while this was in flight
      previewed = { mode: 'months', months: v.months };
      const exp = p.would_expire || {};
      const kept = protectedTotal(p);
      result.replaceChildren(
        el('p', { class: 'retention-dry', role: 'status', text: t('retention.dry_run', { count: exp.count || 0, size: formatBytes(exp.bytes || 0) }) }),
        el('p', { class: 'retention-protected dev-group-hint', text: t('retention.protected', { count: kept }) }),
      );
    } catch (e) {
      if (mine !== seq) return;
      previewed = null;
      result.replaceChildren(el('p', { class: 'retention-error', role: 'alert', text: (e && e.message) || String(e) }));
    } finally {
      if (mine === seq) sync();
    }
  });

  save.addEventListener('click', async () => {
    const v = value();
    if (!canSave(v, previewed)) return;
    save.disabled = true;
    try {
      const body = v.mode === 'months'
        ? { [K_MODE]: 'months', [K_MONTHS]: v.months }
        : { [K_MODE]: 'forever' };
      await api.setRetention(body);
      saved = v.mode === 'months' ? { mode: 'months', months: v.months } : { mode: 'forever', months: saved.months };
      toast(t('retention.saved'));
    } catch (e) {
      toast((e && e.message) || String(e), true);
    } finally {
      sync();
    }
  });

  const ready = Promise.resolve(api.retention()).then((r) => {
    const vals = (r && r.values) || {};
    const m = vals[K_MODE] === 'months' ? 'months' : 'forever';
    const n = parseMonths(vals[K_MONTHS]);
    saved = { mode: m, months: m === 'months' ? n : null };
    mode.value = m;
    months.value = String(n || MONTHS_DEFAULT);
    mode.disabled = false;
    sync();
  }).catch((e) => {
    if (e && e.status === 403) card.remove();
    else result.textContent = (e && e.message) || String(e);
  });
  sync();
  return { card, ready };
}
