// Image Studio's form lives on the server; any copy an old page left in the browser (one key per
// profile) must not survive a sign-in, lock, profile change or sign-out.
import test from 'node:test';
import assert from 'node:assert/strict';

function fakeStorage(init = {}) {
  const m = new Map(Object.entries(init));
  return {
    get length() { return m.size; },
    key: (i) => [...m.keys()][i] ?? null,
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { m.set(k, String(v)); },
    removeItem: (k) => { m.delete(k); },
    keys: () => [...m.keys()],
  };
}

test('wipeStudioKeys removes every Studio draft and "Not now" key, and nothing else', async () => {
  globalThis.localStorage = fakeStorage({
    'dispatch-imagestudio-draft:a1b2c3d4e5f60718': '{"prompt":"private"}',
    'dispatch-imagestudio-draft:install': '{}',
    'dispatch-imagestudio-last-dismissed:a1b2c3d4e5f60718': '5',
    'dispatch-imagestudio-sort': 'oldest',
    'dispatch-theme': 'dark',
  });
  globalThis.sessionStorage = fakeStorage({ 'dispatch-imagestudio-anything': 'x', other: 'y' });
  const { wipeStudioKeys } = await import('../static/js/privacy.js');
  wipeStudioKeys();
  assert.deepEqual(globalThis.localStorage.keys().sort(), ['dispatch-imagestudio-sort', 'dispatch-theme']);
  assert.deepEqual(globalThis.sessionStorage.keys(), ['other']);
});

test('the shell wipes them wherever it wipes the local cache (sign-in, lock, profile change, sign-out)', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../static/js/main.js', import.meta.url), 'utf8');
  const wipes = src.split('wipeTier(TIER_FULL)').length - 1;
  const studio = src.split('wipeStudioKeys();').length - 1;
  assert.ok(wipes >= 5 && studio >= wipes, `every wipeTier(TIER_FULL) is followed by wipeStudioKeys() (${studio}/${wipes})`);
});
