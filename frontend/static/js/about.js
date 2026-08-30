// The "About" row: what this install is, and where its source lives.
//
// This is not decoration. Every family device on :8765 uses this program over
// the network and never sees the repository, so the app itself is the only
// place that can tell them what they are running and where its source is. MIT
// does not oblige the offer — it is made because a user of a program should be
// able to find out what it is. It lives in the DEVICE pane, the one settings
// tab a Safe-Mode session can open, so it reaches every user and not just the
// operator.
//
// One constant, one place to change it.

/** Where the source for THIS build lives.
 *
 *  <!-- FORK NOTE: if you modify DisPatch, point this at YOUR repository — a
 *  link to ours would send your users to code you are not running. `OWNER` is a
 *  placeholder that the release tooling rewrites repo-wide; a fork must set it
 *  by hand. -->
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
                         : 'Free software under the MIT licence.';

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
