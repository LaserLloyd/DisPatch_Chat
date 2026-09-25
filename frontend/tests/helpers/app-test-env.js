// Shared setup for app package tests (apps/<id>/tests/*.test.js), which live
// outside frontend/ and therefore cannot find frontend/node_modules (jsdom)
// or the served /static/ paths on their own.
//
//   import { jsdom, dom, REPO, STATIC, appDir } from '../../../frontend/tests/helpers/app-test-env.js';
//
// Importing it registers static-resolve-hooks.js, so a later dynamic
// import('/apps/jobboard/board.js?v=…') loads the repo file and its
// `/static/js/app-sdk.js?v=N` import resolves to frontend/static/js/.

import { createRequire, register } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import { domSkip } from '../_require-dom.js';

const HERE = dirname(fileURLToPath(import.meta.url));
export const FRONTEND = join(HERE, '..', '..');
export const REPO = join(FRONTEND, '..');
export const STATIC = join(FRONTEND, 'static');
export const appDir = (id) => join(REPO, 'apps', id);

register(pathToFileURL(join(HERE, 'static-resolve-hooks.js')).href);

const require = createRequire(join(FRONTEND, 'package.json'));
let _jsdom = null;
try { _jsdom = require('jsdom'); } catch { /* not installed — tests skip (or fail under CI_REQUIRE_DOM) */ }
export const jsdom = _jsdom;
export const dom = { skip: domSkip(_jsdom ? false : 'jsdom is not installed (npm ci in frontend/)') };

/** A fresh jsdom window at `url`, installed as the globals the modules read,
 *  with fetch answering from `routes` ({'GET /path?query': (opts) => [status, body]}).
 *  Returns {win, calls, routes}. */
export function setupDom(html, url, routes = {}) {
  const { JSDOM } = _jsdom;
  const win = new JSDOM(html, { url, runScripts: 'outside-only' }).window;
  // defineProperty, not assignment: node itself defines some of these
  // (navigator, localStorage) as getter-only globals.
  const put = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  put('window', win);
  put('document', win.document);
  put('location', win.location);
  put('localStorage', win.localStorage);
  put('navigator', win.navigator);
  put('CSS', win.CSS || { escape: (s) => s });
  const calls = [];
  globalThis.fetch = async (u, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${u}`;
    let body = null;
    try { body = opts.body ? JSON.parse(opts.body) : null; } catch { body = opts.body; }
    calls.push({ key, url: String(u), method: (opts.method || 'GET').toUpperCase(), body, opts });
    const h = routes[key] || routes[`${(opts.method || 'GET').toUpperCase()} ${String(u).split('?')[0]}`];
    if (opts.signal && opts.signal.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' });
    const [status, json] = h ? await h(opts) : [404, { detail: 'unhandled route in test stub: ' + key }];
    return {
      ok: status >= 200 && status < 300, status, statusText: String(status),
      headers: new win.Headers(), json: async () => json,
    };
  };
  return { win, calls, routes };
}

export async function flush(n = 10) {
  for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0));
}
