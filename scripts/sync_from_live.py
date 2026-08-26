#!/usr/bin/env python3
"""Pull application source out of a running install into this public repo.

The maintainer develops against a live install with real family data in it.
This script is the one-way valve between that install and the public tree:

    python3 scripts/sync_from_live.py --from ~/my-install          # dry run
    python3 scripts/sync_from_live.py --from ~/my-install --go     # apply

Why an allowlist
----------------
A deny-list ("copy everything except the database") fails the day someone adds
a new kind of private file, and it fails silently. This copies ONLY the paths
named in ALLOW below. Anything new in the install is invisible to the sync
until a human adds it here — which is the correct default when the cost of a
mistake is publishing someone's chat history.

Order of operations is deliberate: copy into a staging directory, scrub the
STAGING copy, and only then move it into the repo. A failed scrub therefore
never leaves a dirty file in the tree, not even briefly.
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (source path relative to the install, destination relative to the repo, glob)
# A directory entry copies only files matching `glob`, non-recursively, so a
# stray subdirectory of user data can never ride along.
ALLOW: list[tuple[str, str, str]] = [
    ("backend/app",                 "backend/app",                 "*.py"),
    ("backend/tests",               "backend/tests",               "*.py"),
    ("backend",                     "backend",                     "pyproject.toml"),
    ("backend",                     "backend",                     "uv.lock"),
    ("frontend/static",             "frontend/static",             "index.html"),
    ("frontend/static",             "frontend/static",             "app.css"),
    ("frontend/static",             "frontend/static",             "sw.js"),
    ("frontend/static",             "frontend/static",             "manifest.webmanifest"),
    ("frontend/static/js",          "frontend/static/js",          "*.js"),
    ("frontend/static/vendor",      "frontend/static/vendor",      "*"),
]

# Operator tools that do NOT live in the install tree.
#
# The daily timer's shell script and the avatar rotator run from the operator's
# own PATH directory (~/.local/bin), not from the app directory — systemd calls
# them, the app never imports them. That put them outside every allowlist here
# and outside the deploy set as well, i.e. outside version control entirely,
# which is exactly how the rotator came to hold a hardcoded path to a directory
# that had moved and stayed broken for two days with nobody able to diff it.
#
# They are synced from `--tools-from` (default ~/.local/bin) and installed back
# out by `scripts/install-tools.sh`. Same one-way-valve rules as everything
# above: staged, scrubbed, then moved into the tree.
HOST_TOOLS: list[tuple[str, str]] = [
    ("dispatch-avatar-rotate", "scripts/dispatch-avatar-rotate"),
    ("local-chat-daily.sh",    "scripts/local-chat-daily.sh"),
]

# Paths that live ONLY in the public repo. The allowlist above already excludes
# them by construction; listed for the reader.
#
# `frontend/static/locales/` is here rather than in ALLOW on purpose: the whole
# i18n system was built in this repo and the maintainer's install has no
# locales directory at all. Listing it in ALLOW achieved nothing except eight
# "orphan" lines on every run — and it would become actively dangerous the day
# the install is updated FROM this repo, because the sync would then be able to
# copy a stale round-trip of the translations back over the originals.
REPO_ONLY = ("README.md", "LICENSE", "Dockerfile", "docker-compose.yml",
             "docs/", ".github/", "scripts/", "deploy/", ".env.example",
             "frontend/static/locales/", "frontend/static/dashboard.css")

# Files the PUBLIC REPO owns outright, even though a same-named file exists
# upstream. These have diverged because the open-source build gained features
# the maintainer's private install does not have (translations, multi-user,
# the setup flow). Overwriting them from upstream would silently delete that
# work, so the sync refuses and reports instead.
#
# This list existing at all is the signal that the repo has become the real
# upstream. Once it covers most of the app, stop syncing FROM the install and
# start deploying TO it — see docs/development.md.
# Every entry is a file that ACTUALLY DIFFERS from the install today (verified
# by diffing the two trees, not by guessing), with a note saying which repo-side
# work an overwrite would delete. Keep that discipline: an entry with no reason
# beside it is one somebody will eventually delete to make a sync "work".
DIVERGED = {
    # --- backend ---------------------------------------------------------
    # Loopback default bind, the DISPATCH_*-first env() helper, and lazily
    # derived reaction paths so tests cannot seed a real data directory.
    "backend/app/config.py",
    # The single-instance flock (--workers now fails loudly instead of half
    # working), the "no PIN and listening off-box" startup warning, the
    # /api/dashboard entry in the Safe-Mode gate's full-access path list, and
    # quota constants read through config.env() so DISPATCH_* names work.
    "backend/app/main.py",
    # Per-bot reaction pools: each reactions-enabled bot gets its own mood
    # folders, prompt bank and refill schedule, plus the migration off the
    # single shared pool. The install still has the one-pool layout, so a sync
    # would not just revert code — it would revert code that has already
    # migrated the data underneath it.
    "backend/app/reactions.py",
    # `BotOut.api_provider` — the public bot record now says WHICH direct LLM
    # provider backs a bot (never the URL, never the key). The install has no
    # such backend, so a sync would quietly drop the field and leave
    # config.Bot.to_dict() emitting a key this model does not declare.
    "backend/app/models.py",
    # Declares the test roster (TEST_BOTS) instead of asserting against
    # whatever bots the product happens to ship, which is what lets the public
    # build change its starter roster without turning 33 access-control tests
    # red.
    "backend/tests/conftest.py",
    # Covers the per-bot pool layout above (moods-<bot>/, per-bot prompt banks,
    # the migration off the single pool). It has to move with reactions.py: a
    # sync that reverted only the tests would leave the port passing a suite
    # that no longer describes it.
    "backend/tests/test_reactions.py",

    # --- frontend --------------------------------------------------------
    # Pre-paint language/direction bootstrap, the data-i18n markup the DOM pass
    # translates, the language picker, and the dashboard's markup.
    "frontend/static/index.html",
    # Logical properties throughout (margin-inline, border-inline-start …) so
    # RTL works, plus the deliberate `direction: ltr` islands — the PIN keypad
    # must not reorder its digits — and the dashboard styles.
    "frontend/static/app.css",
    # Precaches the i18n/dashboard/privacy modules and EVERY locale (the
    # language picker switches with no reload, and en.json is the per-key
    # fallback for all of them), and carries the matching CACHE bump.
    "frontend/static/sw.js",
    # i18n wiring for dynamically built nodes (setI18nText/setI18nHtml), the
    # popout window mode, and the dashboard + privacy entry points.
    "frontend/static/js/main.js",
    # The English-only date/time/size formatters were REMOVED from here and
    # rewritten on Intl in js/i18n.js. Restoring this file resurrects
    # 'en-US' hard-coding that no locale file can reach.
    "frontend/static/js/util.js",
    # Bare-URL linkification in plain-text bubbles, and the translated
    # attributes markdown.js has to write inline because it builds HTML as
    # strings the DOM pass never sees.
    "frontend/static/js/markdown.js",
    # Every user-facing string routed through t(): overlay alt text, the
    # screen-reader announcement, the manager's refusals.
    "frontend/static/js/reactions.js",
    # The theme button's label is two translation keys rather than one hard
    # coded English string, re-pointed on each toggle.
    "frontend/static/js/theme.js",
    # Identical to the install TODAY, and listed anyway: both are the natural
    # place for the dashboard/i18n endpoints to land next, and the cost of a
    # premature entry is one advisory line in the report, while the cost of a
    # late one is a silent revert. Remove them only if this file stops being
    # used at all.
    "frontend/static/js/api.js",
    "frontend/static/js/ws.js",

    # --- host tools (see HOST_TOOLS) -------------------------------------
    # The repo copy resolves its rotation roster (DISPATCH_ROTATE_BOTS, else
    # the avatar-pool directories) instead of carrying one fork's hardcoded
    # list of bot names, and finds the pre-migration avatar directory through
    # DISPATCH_APP_DIR instead of a hardcoded install path. Syncing the host
    # copy back over it would restore both.
    "scripts/dispatch-avatar-rotate",
    # Same shape: the repo copy must not carry a private bot roster or the
    # host's cron-job names in its comments.
    "scripts/local-chat-daily.sh",
}


def stage(src_root: Path, staging: Path, tools_root: Path | None = None) -> list[str]:
    """Copy the allowlist into `staging`. Returns the relative paths copied."""
    copied: list[str] = []
    if tools_root is not None:
        for name, dst_rel in HOST_TOOLS:
            src = tools_root / name
            if not src.is_file():
                continue          # not installed here; nothing to compare
            dst = staging / dst_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(dst_rel)
    for src_rel, dst_rel, pattern in ALLOW:
        src_dir = src_root / src_rel
        if not src_dir.is_dir():
            continue
        for src in sorted(src_dir.glob(pattern)):
            if not src.is_file():
                continue          # never recurse into a subdirectory
            dst = staging / dst_rel / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(f"{dst_rel}/{src.name}")
    return copied


def classify(staging: Path, copied: list[str]) -> tuple[list[str], list[str]]:
    """Split the staged files into (new, changed) relative to the repo."""
    new, changed = [], []
    for rel in copied:
        cur = REPO / rel
        if not cur.exists():
            new.append(rel)
        elif not filecmp.cmp(staging / rel, cur, shallow=False):
            changed.append(rel)
    return new, changed


def orphans(copied: list[str]) -> list[str]:
    """Files the repo has that the install no longer does.

    Reported, never deleted — a file can be missing because it was removed
    upstream, or because someone renamed a directory and the allowlist is now
    stale. Guessing between those is how a sync eats work.
    """
    tracked = set(copied)
    found = []
    for src_rel, dst_rel, pattern in ALLOW:
        d = REPO / dst_rel
        if not d.is_dir():
            continue
        for p in sorted(d.glob(pattern)):
            rel = f"{dst_rel}/{p.name}"
            if p.is_file() and rel not in tracked:
                found.append(rel)
    return found


def scrub(path: Path) -> bool:
    # The child writes straight to the real stderr/stdout, so flush ours first
    # or its findings appear above the heading that introduces them.
    sys.stdout.flush()
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "scrub_check.py"),
                        "--path", str(path)], check=False)
    sys.stderr.flush()
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", type=Path, required=True,
                    help="path to the live install (the directory holding backend/ and frontend/)")
    ap.add_argument("--tools-from", dest="tools", type=Path,
                    default=Path("~/.local/bin"),
                    help="directory holding the host tools listed in HOST_TOOLS "
                         "(default ~/.local/bin); pass --no-tools to skip them")
    ap.add_argument("--no-tools", action="store_true",
                    help="do not stage the host tools at all")
    ap.add_argument("--go", action="store_true",
                    help="actually write the changes (default is a dry run)")
    args = ap.parse_args()

    src_root = args.src.expanduser().resolve()
    if not (src_root / "backend" / "app").is_dir():
        print(f"✗ {src_root} does not look like an install (no backend/app)", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="dispatch-sync-") as tmp:
        staging = Path(tmp)
        tools_root = None if args.no_tools else args.tools.expanduser().resolve()
        copied = stage(src_root, staging, tools_root)
        if not copied:
            print("✗ nothing matched the allowlist — is --from pointing at the right tree?",
                  file=sys.stderr)
            return 2

        print(f"Staged {len(copied)} file(s) from {src_root}\n")
        print("── Scrubbing the staged copy ──")
        if not scrub(staging):
            print("\n✗ Refusing to sync: private data found in the staged copy.\n"
                  "   Nothing was written to the repo. Fix the source files and re-run.",
                  file=sys.stderr)
            return 1

        new, changed = classify(staging, copied)
        gone = orphans(copied)

        # Protect repo-owned files from being reverted to the upstream version.
        # `new` is folded in as well: a DIVERGED file the repo happens not to
        # have yet (a rename, a fresh checkout) was being dropped from the copy
        # list without ever being reported, which is the one outcome this
        # script must never produce — a decision nobody saw.
        blocked = sorted((set(changed) | set(new)) & DIVERGED)
        changed = [r for r in changed if r not in DIVERGED]
        new = [r for r in new if r not in DIVERGED]

        print(f"\n── Summary ──\n  new:     {len(new)}\n  changed: {len(changed)}\n"
              f"  skipped: {len(blocked)} (repo-owned — see DIVERGED)\n"
              f"  orphans: {len(gone)} (in repo, not in install — review by hand)\n")
        for rel in new:
            print(f"  + {rel}")
        for rel in changed:
            print(f"  M {rel}")
        for rel in blocked:
            print(f"  ! {rel}  — differs upstream, NOT overwritten")
        for rel in gone:
            print(f"  ? {rel}")

        if blocked:
            print("\n  The repo's copy of the files marked ! has features the install\n"
                  "  does not (translations, setup flow, …). If the install genuinely\n"
                  "  has a fix worth keeping, port it by hand — do not remove it from\n"
                  "  DIVERGED to make this sync 'work'.")

        if not (new or changed):
            print("\n✓ Nothing to apply." if blocked else "\n✓ Already up to date.")
            return 0

        if not args.go:
            print("\nDry run. Re-run with --go to apply.")
            return 0

        for rel in new + changed:
            dst = REPO / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / rel, dst)
        print(f"\n✓ Applied {len(new) + len(changed)} file(s).")

    # Belt and braces: the repo as a whole must still be clean afterwards.
    print("\n── Verifying the repo ──")
    if not scrub(REPO):
        print("\n✗ The repo is dirty after the sync. Do NOT commit. Investigate above.",
              file=sys.stderr)
        return 1
    print("\nNext: review `git diff`, run the tests, then commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
