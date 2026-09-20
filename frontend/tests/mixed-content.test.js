// isMixedContent() — the predicate behind the StudioForge pane's "insecure"
// state.
//
// WHY THIS EXISTS
// ---------------
// The StudioForge pane embeds the rig's own web UI in an iframe and probes
// first with `fetch(url, {mode: 'no-cors'})` to decide whether the frame is
// worth loading. Over Tailscale that probe LIED. Tailscale Serve fronts
// DisPatch over HTTPS on a tailnet name while the rig is configured as a bare
// http:// address, so the browser blocks both the iframe and the probe as
// mixed content — but a no-cors fetch yields an opaque response that RESOLVES
// even when nothing was retrieved. Measured against the live origin, the
// console logged
//
//     Mixed Content: The page at 'https://<the tailnet host>/' was loaded over
//     HTTPS, but requested an insecure resource 'http://<the rig>:8080/'.
//
// with the request failing as net::ERR_ABORTED, while the probe reported
// success. The pane therefore skipped every explanatory note, set the frame's
// src, and painted an empty rectangle. Reached over plain http on the LAN the
// identical build works, which is exactly why it read as "the frame typically
// does not load over Tailscale".
//
// So the case has to be decided from the two URLs BEFORE any probe runs, which
// is what this function does — and being a pure function of two strings, it is
// testable without a browser or a tailnet.

import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const { isMixedContent } = await import(join(HERE, '..', 'static', 'js', 'util.js'));

test('the Tailscale case: an HTTPS page may not embed the plain-HTTP rig', () => {
  assert.equal(
    isMixedContent('https://host.example.net/', 'http://198.51.100.20:8080'),
    true,
  );
});

test('the LAN case is fine: an HTTP page may embed an HTTP rig', () => {
  assert.equal(
    isMixedContent('http://203.0.113.10:8765/', 'http://198.51.100.20:8080'),
    false,
  );
  assert.equal(
    isMixedContent('http://127.0.0.1:8765/', 'http://198.51.100.20:8080'),
    false,
  );
});

test('HTTPS to HTTPS is fine', () => {
  assert.equal(
    isMixedContent('https://host.example.net/', 'https://rig.example.net:8080'),
    false,
  );
});

test('loopback targets are exempt — browsers treat them as trustworthy', () => {
  // W3C secure-contexts: 127.0.0.1 / ::1 / localhost are potentially
  // trustworthy, so they are NOT blocked from an https page. Flagging them
  // would put the pane into a permanent, wrong "insecure" state for anyone
  // running the rig on the same machine.
  for (const target of [
    'http://localhost:8080',
    'http://127.0.0.1:8080',
    'http://[::1]:8080',
    'http://rig.localhost:8080',
  ]) {
    assert.equal(isMixedContent('https://host.example.net/', target), false, target);
  }
});

test('a relative target resolves against the page and is never mixed', () => {
  assert.equal(isMixedContent('https://host.example.net/', '/api/studioforge'), false);
});

test('junk in either argument is false, never a throw', () => {
  // This runs on every render of the pane. A malformed configured URL must
  // degrade to "not a scheme problem" and let the other branches diagnose it,
  // not take the render path down with it.
  for (const [page, target] of [
    ['', 'http://rig:8080'],
    ['not a url', 'http://rig:8080'],
    ['https://host.example.net/', ''],
    ['https://host.example.net/', 'not a url'],
    ['https://host.example.net/', null],
    ['https://host.example.net/', undefined],
  ]) {
    assert.doesNotThrow(() => isMixedContent(page, target));
    assert.equal(typeof isMixedContent(page, target), 'boolean');
  }
});

test('other insecure schemes are not misreported as mixed content', () => {
  // ws:// and ftp:// are a different problem with a different message; only
  // http: belongs to this branch.
  assert.equal(isMixedContent('https://host.example.net/', 'ws://rig:8080'), false);
  assert.equal(isMixedContent('https://host.example.net/', 'ftp://rig/'), false);
});
