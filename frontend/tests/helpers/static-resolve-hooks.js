// Node module-resolution hook for tests that import SERVED paths.
//
// An app page (apps/<id>/static/*.js) imports the shell's SDK by the URL the
// browser sees — `/static/js/app-sdk.js?v=N` — because that is the only path
// that is right on the server. Node would read that as a file at the root of
// the filesystem. This hook maps the two served roots back onto the repo:
//   /static/…  → frontend/static/…
//   /apps/<id>/… → apps/<id>/static/…   (the URL the server serves them at)
// keeping the ?v= query, so the same module URL is the same instance — which
// is exactly the property assets.test.js pins for the shell.
//
// Registered by app-test-env.js; never loaded by anything the browser runs.

import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const REPO = join(FRONTEND, '..');

export async function resolve(specifier, context, next) {
  if (typeof specifier === 'string' && (specifier.startsWith('/static/') || specifier.startsWith('/apps/'))) {
    const q = specifier.indexOf('?');
    const path = q >= 0 ? specifier.slice(0, q) : specifier;
    const query = q >= 0 ? specifier.slice(q) : '';
    let file;
    if (path.startsWith('/static/')) file = join(FRONTEND, path);
    else {
      const m = /^\/apps\/([a-z0-9-]{1,40})\/(.*)$/.exec(path);
      if (!m) return next(specifier, context);
      file = join(REPO, 'apps', m[1], 'static', m[2]);
    }
    return next(pathToFileURL(file).href + query, context);
  }
  return next(specifier, context);
}
