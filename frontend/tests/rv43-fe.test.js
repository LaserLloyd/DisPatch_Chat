// 4.3 Opus review: OIDC link button only where the server allows a link, the schedule editor
// defaults to the open chat, and the setup wizard only opens for someone who can finish it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', 'static');
const { linkButtonFor } = await import('../static/js/oidc-settings.js');
const t = (k) => k;
const el = (tag, attrs = {}) => ({ tag, attrs, addEventListener() {} });
const owner = { profile: { id: 'own', protected: true } };

test('OIDC: no Link sign-in for the shared default profile (People row)', () => {
  assert.equal(linkButtonFor({ id: 'dflt', name: 'DisPatch', lock_target: true }, { me: owner, enabled: true, links: [], t, el }), null);
});
test("OIDC: no Link sign-in on the default profile's own Account page", () => {
  const me = { profile: { id: 'dflt', shared: true, protected: false } };
  assert.equal(linkButtonFor(me.profile, { me, enabled: true, links: [], t, el }), null);
});
test('OIDC: no Link sign-in for a disabled profile, still one for a normal profile', () => {
  assert.equal(linkButtonFor({ id: 'p2', name: 'X', disabled: true }, { me: owner, enabled: true, links: [], t, el }), null);
  assert.ok(linkButtonFor({ id: 'p3', name: 'Y' }, { me: owner, enabled: true, links: [], t, el }));
});
test('schedules: main.js passes currentThreadId to createTasksUi', () => {
  const src = readFileSync(join(STATIC, 'js', 'main.js'), 'utf8');
  const i = src.indexOf('createTasksUi({');
  assert.match(src.slice(i, src.indexOf('\n});', i)), /currentThreadId\s*:/);
});
test('wizard: not opened for a caller the server sent no steps (cannot manage settings)', async () => {
  const { maybeStartWizard } = await import('../static/js/wizard.js');
  globalThis.localStorage ??= { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.sessionStorage ??= globalThis.localStorage;
  const api = { wizard: async () => ({ needed: true, claimed: true }) };
  assert.equal(await maybeStartWizard({ api }), null);
});
