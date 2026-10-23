# Locale TODO

Status after WP-LOC (4.0 wave 3b): `en` is the source; de, es, fr, pt, ja, zh and ar are translated for every key
the 3.0 to 4.0 builds added or reworded. No `[xx] ` placeholder values remain, and no retired wording (Safe Mode,
Restricted, supervision, daily limits, Unlock) is left in the non-English locales. Terms: Admin / User / Basic,
"Sign in", DisPatch and Atlas never translated, one phrase each for "Reset to default" and household connections.

Remaining "identical to English" warnings from `scripts/check-locales.py --strict` are legitimate cognates and
technical tokens (Name, Chat, Python, Gateway, version, ...).

App packages (`apps/jobboard`, `apps/imagestudio`): `locked` and `detail.locked` translated in all seven locales.

Deleted: `settings.change_photo` (unused; `change_photo_short` stays), `dash.locked`.

## WP-SCALE-FE keys

Pending until the `cfp/scale-fe` branch adds them (English copies, to translate when it lands). Nothing listed yet.
