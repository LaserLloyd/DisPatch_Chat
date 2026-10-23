# Locale TODO

Status after WP-LOC (4.0 wave 3b): `en` is the source; de, es, fr, pt, ja, zh and ar are translated for every key
the 3.0 to 4.0 builds added or reworded, including all WP-SCALE-FE keys (`archive.*`, `storage.*` with
`storage.other` renamed `storage.rest`, `retention.*`, `bkp.tier_recent|daily|weekly`).
No `[xx] ` placeholder values remain, and no retired wording (Safe Mode, Restricted, supervision, daily limits,
Unlock) is left in the non-English locales. Terms: Admin / User / Basic, "Sign in", DisPatch and Atlas never
translated, one phrase each for "Reset to default" and household connections.

Remaining "identical to English" warnings from `scripts/check-locales.py --strict` are legitimate cognates and
technical tokens (Name, Chat, Python, Gateway, version, ...).

App packages (`apps/jobboard`, `apps/imagestudio`): `locked` and `detail.locked` translated in all seven locales.

Deleted: `settings.change_photo` (unused; `change_photo_short` stays), `dash.locked`.

Deleted after the 4.0 stale review (unused, and `i18n-keys.test.js` now ignores comments and substrings, so it
catches these): the `voices.*` namespace, `retention.title|months_value|preview_first`, `archive.archived`,
`nav.label_reactions`, `settings.hint`, `security.note`, `links.url`, `tools.save`, `schema.save|saved`.

Added in all eight locales: `people.audit_*` labels for every audit kind the server writes (agents, API key,
support bundle, add-on approval, main Admin reset); `people.audit_device_bound` stays as a label for 3.x history
rows. `push.needs_remember` names the "Keep this device signed in" checkbox.
