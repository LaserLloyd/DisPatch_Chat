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

## 4.3 (WP-LOC): needs native review

**4.3 strings machine-drafted by an agent: needs native review.** Nothing in this round was checked by a native
speaker. Drafted in ar, de, es, fr, ja, pt, zh:

- Every key the 4.3 builds added (176): `agents.*` (approval timeout, shell allow-list, command-line programs,
  default task limits, Owner-only notes, managed-agent ids), `bkp.reveal*` and `bkp.re_enter_*`, `cpage.*`
  (reorder, park in Tools & more, avatar pool, reactions, voice), `cxn.owner_only_*`, the `oidc.*` namespace
  ("outside sign-in"), the `sched.*` namespace (schedules), `settings.link_unknown`, `settings.owner_only_badge`,
  `signin.oidc_*`, `tasks.waiting_ok*`; also `packs.*` (the Add-a-pack dialog and pack cards).
- `approval.state_expired` now reads "Expired" in English (approvals time out) and in every locale.
- `oidc.status_on` got the plural categories each language needs (ar six forms; ja and zh `other` only).
- `storage.files` in ar, es, fr now uses the same "File Server" wording as `files.title`.
- `apps/jobboard/locales`: every string translated (before, only `locked` and `detail.locked` were). The app name
  stays "Job Board" where the existing `locked` text already used it (ar, es, fr, zh).

Terms used: "Owner" stays untranslated (as in `cpage.who_agent`); "the main Admin" uses each language's word for
Admin (Haupt-Admin, admin principal, administrador principal, المشرف الرئيسي, メインの管理者, 主管理员);
"outside sign-in" = Externe Anmeldung / Connexion externe / Inicio de sesión externo / Início de sessão externo /
تسجيل الدخول الخارجي / 外部サインイン / 外部登录. Day names in `sched.day_*` are lower case in es, fr, pt (they are
inserted mid-sentence by `sched.rule_weekly`).

Reviewer notes: fr and pt mix formal and informal address by namespace (agents/cpage/tasks/sched/cxn use
vous/você, bkp/oidc/settings/signin use tu); the new strings follow their namespace. pt mixes pt-PT and pt-BR
vocabulary in the same way. A native pass should pick one register per language.

Identical-to-English values left on purpose (cognates and tokens): product and service names (DisPatch Chat,
DeepSeek Harness, StudioForge, Image Studio, Amazon Bedrock, Google Cloud, Microsoft Foundry), format-only strings
(`about.version`, `search.hit_meta`, `sessions.meta`, `chip.label`, `files.named_error`,
`files.upload_progress_label`, `cxn.latency`, `unit.*`, `tasks.duration_*`, `harness.subtitle`), technical tokens
(CPU, PID, Python, stderr, PIN, HTML, JSON, Markdown, MB, GB, `main`, `root (uid 0)`, LC-XXXX-XXXX), and words that are
the normal term in the language: de Name, Agent, Admin, Chat, Bots, Details, Emoji, Prompt, Seed, Workflow,
Snapshot, Passphrase, Backups, Container, Gateway, Problem, Version, Start, System, Text, Person, Feedback, Uploads,
Add-ons, Plugins & Apps, Info, Offline, Scopes, Subagent; fr Agents, Agent, Arguments, Description, Instructions,
Secrets, Type, Direct, Copies, Images, Messages, Pages, Notes, Actions, Important, Note, Notifications, Sessions,
Total, Version, Avatars, Incompatible, Attention, minutes, Assistant, Admin, Clients; es/pt Backend(s), Tokens,
Bots, Chat(s), Emoji, Prompt(s), Total, App, Embeddings, Id, web, NO, Error; ja Web UI; Job Board seniority
levels (Junior, Senior, Staff, Principal) where the language uses the English titles.

## 4.3 Fable round (2026-10-31): needs native review

**Machine-drafted by an agent on 2026-10-31: needs native review.** The Fable review rewrote the English UI in
plain language (scrub of `en.json` plus the copy, icons, people, session, host-check, chat and PIN fixes). Every
key whose English changed or was added since `e76f67e` was re-translated from the NEW English in ar, de, es, fr,
ja, pt and zh: **510 keys per locale** in `frontend/static/locales/*.json` and **1 key per locale** in
`apps/imagestudio/locales/*.json` (`avatar.no_bots`). That covers the `keys-*.txt` lists of the round
(copy, icons, pin, host, people, session, chat). Nobody who speaks these languages has checked them.

- Plural objects reshaped to each language's categories (ar six, ja/zh `other` only, fr/es/pt `one many other`,
  de `one other`): `archive.months`, `archive.would`, `chars.pools_status`, `hostcheck.seen`,
  `recovery.recovered_summary`, `viewer.roots_count`.
- `harness.subtitle` no longer carries `{port}` (the English dropped it).
- New namespaces drafted from English placeholders: `hostcheck.*` (the host allow-list card), `host_notice.*`,
  and the 4.3 session texts (`lock.idle_landed`, `lock.landed`, `lock.default_*`, `signin.role_shared`).

Terms (kept consistent with the existing files): "Owner" stays untranslated; DisPatch, Atlas, Image Studio never
translated. Sign out / Sign in = Abmelden / Anmelden, Cerrar sesión / Iniciar sesión, Se déconnecter / Se
connecter, Terminar sessão / Iniciar sessão, サインアウト / サインイン, 退出登录 / 登录, تسجيل الخروج / تسجيل الدخول.
"Settings → People & Access" uses each locale's `settings.sec_people` (ar writes navigation paths with ←, as
the rest of ar.json does). "bot" follows the new English: de "Bot" (the section name stays "Figuren"),
ja ボット (キャラクター only where English says "character"), zh 机器人 (角色 only for "character"), ar بوت.
"the DisPatch computer" = DisPatch-Rechner, el ordenador de DisPatch, computador do DisPatch, DisPatch のコンピューター,
DisPatch 电脑, جهاز DisPatch.

Reviewer notes: fr and pt still mix registers by namespace (see the 4.3 WP-LOC note above); new namespaces chose
tu (fr: hostcheck, signin, threads, reset, host_notice) or vous (fr: sched). ja uses プロファイル (the file's majority
form) except プロフィール画像 for profile pictures. ar.json mixes بوت (majority) and روبوت (older keys); Image Studio's
ar file mostly says روبوت. A native pass should settle one word each.
