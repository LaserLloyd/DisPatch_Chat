// The "About" row: what this install is, and where its source lives.
//
// This is not decoration. DisPatch is AGPL-3.0, and §13 of that licence says
// that if users interact with the program remotely over a network — which is
// exactly what every family device on :8765 does — they must be offered the
// Corresponding Source. A link in the README does not reach them; a row in the
// app does. It therefore lives in the DEVICE pane, the one settings tab a
// Safe-Mode session can open, so the offer reaches every user and not just the
// operator.
//
// One constant, one place to change it.

/** Where the Corresponding Source for THIS build lives.
 *
 *  <!-- FORK NOTE: if you modify DisPatch and let anyone else use it over a
 *  network, AGPL §13 obliges you to offer YOUR modified source — not ours. Point
 *  this at your own repository. `OWNER` is a placeholder that the release
 *  tooling rewrites repo-wide; a fork must set it by hand. -->
 */
export const SOURCE_URL = 'https://github.com/LaserLloyd/dispatch-chat';

/** Kept in step with backend/app/__init__.py `__version__`.
 *
 *  Deliberately a constant rather than a fetch: the version has to be readable
 *  from a Safe-Mode session, and the endpoint that reports it (/api/dashboard)
 *  is admin-only by design. frontend/tests/about.test.js fails if the two
 *  numbers drift.
 */
export const APP_VERSION = '1.0.0';

/** Build the About row for the Device pane.
 *
 *  `t` is passed in (same contract as nimRow/privacyRow) so this module needs
 *  no import of its own and stays trivially testable.
 */
export function aboutRow(t) {
  const wrap = document.createElement('div');
  wrap.className = 'about-row';

  const title = document.createElement('div');
  title.className = 'about-title';
  const strong = document.createElement('strong');
  strong.setAttribute('data-i18n', 'about.title');
  strong.textContent = t ? t('about.title') : 'About';
  title.append(strong);

  const version = document.createElement('div');
  version.className = 'about-line muted';
  version.setAttribute('data-i18n', 'about.version');
  version.setAttribute('data-i18n-vars', JSON.stringify({ version: APP_VERSION }));
  version.textContent = t ? t('about.version', { version: APP_VERSION })
                          : `DisPatch Chat ${APP_VERSION}`;

  const licence = document.createElement('div');
  licence.className = 'about-line muted';
  licence.setAttribute('data-i18n', 'about.license');
  licence.textContent = t ? t('about.license')
                         : 'Free software under the GNU AGPL v3.';

  const link = document.createElement('a');
  link.className = 'about-source';
  link.href = SOURCE_URL;
  link.target = '_blank';
  link.rel = 'noreferrer noopener';
  link.setAttribute('data-i18n', 'about.source');
  link.textContent = t ? t('about.source') : 'Source code';

  wrap.append(title, version, licence, link);
  return wrap;
}
