"""Grep gate (4.0 ownership, BUILD-PLAN-v2 §1.5): every caller of the unscoped readers is
listed here with the reason it may read across owners - or the reason its answer is scoped.

``db.all_threads(`` / ``db.list_threads(`` read every profile's chats unless they are given a
``principal`` / ``owner_ids``; ``config.load_bots(`` returns every character, other profiles'
personal ones included. A new caller fails this test until somebody decides (and writes
down here) why it is safe. Keyed by (file, enclosing function, reader) so line moves do not
matter.

The second gate: no thread-path function in main.py short-circuits on an Admin.

Run: cd backend && uv run pytest tests/test_ownership_callers.py
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
READERS = re.compile(r"\.(all_threads|list_threads_page|list_threads|load_bots)\(")

_HOUSE = "household housekeeping: no person sees the result"
_ADMIN_CFG = "Admin-only configuration route (household characters / connections)"
_FILTER = "the result is filtered through characters.may_use / visible_to before it leaves"
_SCOPER = "a roster broadcast: manager.scoper filters it per socket through visible_to"
_OWNED = "passes principal= / owner_ids= (the caller's own chats only)"

ALLOWED: dict[tuple[str, str, str], str] = {
    ("apps/imagestudio/backend.py", "chat_images", "all_threads"): _OWNED,
    ("apps/imagestudio/backend.py", "targets", "all_threads"): _OWNED,
    ("apps/imagestudio/backend.py", "targets", "load_bots"): _FILTER,
    ("backend/app/bot_builder_api.py", "provider_chain", "load_bots"):
        "household bots' provider entries only (never personal); nothing about any bot is returned",
    ("backend/app/characters.py", "_drop_voice_ack_entries", "load_bots"): _HOUSE,
    ("backend/app/characters.py", "new_personal_id", "load_bots"): "id collision check while creating a personal bot",
    ("backend/app/characters.py", "personal_bots", "load_bots"): "filters to scope personal + owner_profile == the given profile",
    ("backend/app/profiles_service.py", "_household_choice", "load_bots"):
        "household bot ids only, for an Admin's role/household change (manage_users)",
    ("backend/app/profiles_service.py", "basic_default_household", "load_bots"):
        "household direct bot ids only, the default list for a new Basic profile",
    ("backend/app/profiles_service.py", "connect_unconnected_atlases", "load_bots"):
        "housekeeping: connects personal Atlases per their owner's backends; returns a count only",
    ("backend/app/advisor.py", "advisor_bots", "load_bots"): "advisor status / digest per character; requests are owner-filtered in main.advisor_status",
    ("backend/app/apps_loader.py", "_bot_ids_lower", "load_bots"): _HOUSE,
    ("backend/app/apps_loader.py", "provision_bots", "load_bots"): _HOUSE,
    ("backend/app/avatar_pool.py", "enabled_bots", "load_bots"): "household pools only (personal characters never have a pool)",
    ("backend/app/backup_api.py", "export_user", "load_bots"): "Admin export of one account's metadata (manage_users); lists characters limited to it",
    ("backend/app/media_inventory.py", "find_orphans", "load_bots"):
        "maintenance orphan sweep: avatar file names of every bot, so no live avatar counts as an orphan; never returned to a caller",
    ("backend/app/backups.py", "build_support_bundle", "load_bots"): "support bundle (admin_system)",
    ("backend/app/characters.py", "visible_bots", "load_bots"): _FILTER,
    ("backend/app/characters.py", "usable_bots", "load_bots"): _FILTER,
    ("backend/app/characters.py", "create", "load_bots"): "id collision check while creating a character",
    ("backend/app/characters_api.py", "broadcast_roster", "load_bots"): _SCOPER,
    ("backend/app/connections.py", "_users", "load_bots"): _ADMIN_CFG,
    ("backend/app/connections.py", "delete", "load_bots"): _ADMIN_CFG,     # household names for the 409
    ("backend/app/ext_profile_hooks.py", "bots_visible", "load_bots"): _FILTER,
    ("backend/app/extensions.py", "all", "load_bots"): "ctx.bots.all(): the trusted add-on's host view; per-caller views use ctx.bots_visible",
    ("backend/app/firstrun.py", "seed_one", "load_bots"): _HOUSE,
    ("backend/app/firstrun.py", "seed_if_first_run", "load_bots"): _HOUSE,
    ("backend/app/firstrun.py", "_atlas_of", "load_bots"):
        "server-side: which Atlas posts one profile's release notes (its own copy); returns an id only",
    ("backend/app/firstrun.py", "backfill_guides_once", "load_bots"):
        "server-side: queues each personal Atlas's guides for its own owner; returns a count only",
    ("backend/app/image_providers.py", "shared_chat_key", "load_bots"): _HOUSE,
    ("backend/app/main.py", "_lifespan_startup", "load_bots"): _HOUSE,
    ("backend/app/main.py", "_bot_for_avatar", "load_bots"): "the gate's avatar-file lookup (it refuses, never serves)",
    ("backend/app/main.py", "fire_reaction", "load_bots"): _HOUSE,
    ("backend/app/main.py", "_gap_sweep_loop", "all_threads"): _HOUSE,
    ("backend/app/main.py", "_migrate_transcript_seen_backfill", "all_threads"): _HOUSE,
    ("backend/app/main.py", "_migrate_sanitize_stored_messages", "all_threads"): _HOUSE,
    ("backend/app/main.py", "_advisor_threads_since", "list_threads"): "owner_ids = the advisor's corpus profile only",
    ("backend/app/main.py", "llm_providers", "load_bots"): _ADMIN_CFG,
    ("backend/app/main.py", "llm_quicksetup", "load_bots"): _ADMIN_CFG,
    ("backend/app/main.py", "llm_connect", "load_bots"): _ADMIN_CFG,
    ("backend/app/main.py", "get_all_bots", "load_bots"): "drops other profiles' personal characters",
    ("backend/app/main.py", "_broadcast_roster", "load_bots"): _SCOPER,
    ("backend/app/main.py", "_complete_bot_order", "load_bots"): "order rows; the route 404s a foreign personal id and filters its reply",
    ("backend/app/main.py", "upload_bot_avatar", "load_bots"): _SCOPER,
    ("backend/app/main.py", "bot_avatar_history", "all_threads"): _OWNED,
    ("backend/app/main.py", "bot_avatar_history_image", "all_threads"): _OWNED,
    ("backend/app/main.py", "restore_bot_avatar", "load_bots"): _SCOPER,
    ("backend/app/main.py", "reactions_list", "load_bots"): "which characters may fire reactions (household flags)",
    ("backend/app/main.py", "_audit_avatar_snapshots", "all_threads"): _HOUSE,
    ("backend/app/main.py", "_prune_avatar_snapshots", "all_threads"): _HOUSE,
    ("backend/app/main.py", "_prune_avatar_snapshots", "load_bots"): _HOUSE,
    ("backend/app/main.py", "_export_markdown", "load_bots"): "names for the caller's own exported threads",
    ("backend/app/main.py", "_export_html", "load_bots"): "names for the caller's own exported threads",
    ("backend/app/main.py", "export_all", "all_threads"): _OWNED,
    ("backend/app/main.py", "recover_transcript", "all_threads"): _OWNED,
    ("backend/app/main.py", "_mirror_cycle", "load_bots"): _HOUSE,
    # 4.0 scale: GET /api/threads, the WS get_threads frame and the hello first page page
    # through here, always with the caller's principal.
    ("backend/app/main.py", "_thread_page", "list_threads_page"): _OWNED,
    ("backend/app/main.py", "websocket_endpoint", "load_bots"): "hello roster: filtered through visible_to",
    ("backend/app/main.py", "_probe_chat", "load_bots"): "plugin catalog probe (manage_plugins): household bots only, personal ones filtered out",
    ("backend/app/main.py", "_probe_infoforge", "load_bots"): "plugin catalog probe (manage_plugins): household bots only, personal ones filtered out",
    ("backend/app/reactions.py", "_bot_exists", "load_bots"): _HOUSE,
    ("backend/app/reactions.py", "default_bot_id", "load_bots"): _HOUSE,
    ("backend/app/reactions.py", "reaction_bots", "load_bots"): "household reaction flags",
    ("backend/app/starter_packs.py", "list_packs", "load_bots"): _ADMIN_CFG,
    ("backend/app/starter_packs.py", "install", "load_bots"): _ADMIN_CFG,
    ("backend/app/tools.py", "_bot_ids", "load_bots"): _HOUSE,
    ("backend/app/wizard_api.py", "_checklist", "load_bots"): "first-run checklist (manage_settings)",
}


def _callers() -> set[tuple[str, str, str]]:
    out = set()
    files = list(ROOT.glob("backend/app/**/*.py")) + list(ROOT.glob("apps/*/*.py"))
    for f in sorted(files):
        rel = f.relative_to(ROOT).as_posix()
        if "__pycache__" in rel or "/tests/" in rel:
            continue
        src = f.read_text(encoding="utf-8")
        if not READERS.search(src):
            continue
        funcs = [(n.lineno, n.end_lineno, n.name) for n in ast.walk(ast.parse(src))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for i, line in enumerate(src.splitlines(), 1):
            if line.lstrip().startswith(("def ", "async def ")):
                continue
            for m in READERS.finditer(line):
                enc = [fn for fn in funcs if fn[0] <= i <= fn[1]]
                name = min(enc, key=lambda x: x[1] - x[0])[2] if enc else "<module>"
                out.add((rel, name, m.group(1)))
    return out


def test_every_unscoped_reader_caller_is_listed_with_a_reason():
    found = _callers()
    new = sorted(found - set(ALLOWED))
    assert not new, ("Unlisted callers of all_threads / list_threads / load_bots - decide why "
                     "each may read across owners and add it to ALLOWED:\n  "
                     + "\n  ".join(" ".join(c) for c in new))


def test_the_allowlist_has_no_stale_rows():
    stale = sorted(set(ALLOWED) - _callers())
    assert not stale, "ALLOWED rows with no such caller any more:\n  " + "\n  ".join(" ".join(c) for c in stale)


def test_every_reason_is_written_down():
    assert all(len(v) > 12 for v in ALLOWED.values())


# --------------------------------------------------------------------------- #
# No Admin short-circuit on a thread path
# --------------------------------------------------------------------------- #

#: main.py functions on a thread / message / frame path. None of them may decide anything
#: from the caller being an Admin: ownership and the character rule hold for every role.
THREAD_PATH_FUNCS = {
    "scope_frame_for_principal", "_frame_owner_allows", "_roster_row_visible",
    "_thread_access", "_require_thread", "_require_message", "_require_bot_use",
    "_file_access", "file_list", "file_download", "file_raw", "file_delete", "file_wipe",
    "_ws_thread", "_ws_bot_allowed", "get_thread", "get_messages", "list_threads",
    "patch_thread", "delete_thread", "mark_read", "abort_turn", "unread_summary",
    "delete_message_endpoint", "edit_message_endpoint", "update_message_checklist",
    "message_feedback", "get_message_mood_face", "get_thread_avatar", "pin_thread_avatar",
    "search_messages", "export_all", "recover_transcript", "inject_message", "_hand_over_attachments",
    "reactions_fire", "image_job_create", "image_job_status",
    "openclaw_sessions", "openclaw_transcript", "openclaw_import_session",
    "_require_session_key_owned", "_owns_thread", "_require_advisor_request_owned",
    "_notify_thread_owner", "_handle_send", "_handle_get_threads", "_handle_get_messages",
    "_handle_archive", "_handle_regenerate", "_handle_edit_rerun", "_handle_retry",
    "_handle_abort", "bot_avatar_history", "bot_avatar_history_image",
}
SHORT_CIRCUIT = re.compile(r"\bis_operator\b|\.operator\b|\bis_admin\b|\bis_protected\b")


def test_no_admin_short_circuit_on_a_thread_path():
    src = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    hits = []
    seen = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in THREAD_PATH_FUNCS:
            seen.add(n.name)
            for i in range(n.lineno, n.end_lineno + 1):
                code = lines[i - 1].split("#", 1)[0]
                if SHORT_CIRCUIT.search(code):
                    hits.append(f"main.py:{i} {n.name}: {lines[i - 1].strip()}")
    missing = THREAD_PATH_FUNCS - seen
    assert not missing, f"thread-path functions not found (renamed?): {sorted(missing)}"
    assert not hits, "Admin short-circuit on a thread path:\n  " + "\n  ".join(hits)


def test_is_operator_is_gone_from_the_app():
    hits = []
    for f in list(ROOT.glob("backend/app/**/*.py")) + list(ROOT.glob("apps/*/*.py")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            # ctx.is_operator(request) is the add-on API (manage_plugins), not a principal flag
            if re.search(r"(?<!ctx)\.is_operator\b", code) and "def is_operator" not in code \
                    and "tools._is_operator" not in code:
                hits.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not hits, "\n".join(hits)
