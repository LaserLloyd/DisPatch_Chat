Redacted copy of the live 4.1 data shape (taken 2026-10-25) for the 4.2 template and migration tests.

- `roster.yaml` (a copy of config.yaml; that name is git-ignored): the whole roster (agent bots, the direct household bots, the kids' bots, three personal copies).
  Personas are replaced by a one-line placeholder, provider `api_key` / `api_key_env` / `base_url` are removed
  (connection ids and every other provider key are kept), paths, location and the owner's name are generic.
- `profiles.json`: profile ids (re-derived), names, roles and the 4.1 `household_bots` lists.
- `thread-counts.json`: thread counts per profile id, then bot id.
- `bot-kits-state.json`: the kit state file with profile ids re-derived.
- ids `kid_*` stand for the live kids' science bots (renamed: the repo bans the live prefix in tracked files).
- ids `trainer` (name Sam) and `reviewer` (name Reviewer) stand for two live agent bots (renamed for the same reason).
