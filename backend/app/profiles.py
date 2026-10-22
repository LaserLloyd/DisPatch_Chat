"""``python -m app.profiles`` - profile lifecycle from a terminal (4.0 chat-first profiles).

Run it on the machine DisPatch lives on (or in the container). It works on the data directory
directly, like ``python -m app``, and refuses a directory without a ``chats.db``.

    python -m app.profiles counts --profile <name>
    python -m app.profiles spawn-atlas --profile <name> [--force]
    python -m app.profiles create <name> [--role user|basic] [--household ID,ID,...]
    python -m app.profiles ensure-default

* ``counts`` prints how many bots, chats, pictures and cloned voices a profile has (numbers only).
* ``spawn-atlas`` gives an existing profile its own Atlas, the way a new profile gets one.
  Refused when it already has one, unless ``--force``; refused for a Basic profile.
* ``create`` makes a User (own Atlas, no PIN) or a Basic profile (``--household`` = the bot ids
  it may use; default every bot a Basic profile can use). Set a PIN afterwards with
  ``python -m app users passwd``.
* ``ensure-default`` makes the "DisPatch" default profile and the lock target when they are not
  there yet (what the first-run wizard does).

``--data-dir`` (before the verb) picks another data directory, as for ``python -m app``.
"""

from __future__ import annotations

import argparse
import sys

from . import accounts, cli, profiles_service
from .contracts.permissions import Role
from .profiles_service import ProfileServiceError


def _find(name: str) -> accounts.Profile:
    prof = accounts.get_by_name(name)
    if prof is None:
        known = ", ".join(repr(p.name) for p in accounts.list_profiles()) or "(none)"
        raise cli.CliError(f"No profile named {name!r}. Known profiles: {known}")
    return prof


def cmd_counts(args: argparse.Namespace) -> int:
    prof = _find(args.profile)
    counts = profiles_service.content_counts(prof.id)
    print(f"{prof.name} ({prof.role.value}): " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    return 0


def cmd_spawn_atlas(args: argparse.Namespace) -> int:
    prof = _find(args.profile)
    if profiles_service.has_atlas(prof.id) and not args.force:
        raise cli.CliError(f"{prof.name!r} already has an Atlas. Use --force to make another.")
    try:
        bot_id = profiles_service.spawn_atlas(prof.id)
    except ProfileServiceError as e:
        raise cli.CliError(str(e)) from e
    print(f"Made an Atlas for {prof.name!r} ({bot_id}).")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    role = Role.BASIC if args.role == "basic" else Role.USER
    household = None
    if role is Role.BASIC:
        household = ([x.strip() for x in args.household.split(",") if x.strip()]
                     if args.household else profiles_service.basic_default_household())
    elif args.household:
        raise cli.CliError("--household is for Basic profiles; a User sees every household bot "
                           "unless an Admin narrows it in People.")
    try:
        prof = accounts.create_profile(args.name, role, household_bots=household)
    except accounts.AccountError as e:
        raise cli.CliError(str(e)) from e
    profiles_service.on_profile_created(prof)
    print(f"Added {prof.role.value} {prof.name!r} ({prof.id}); no PIN set.")
    return 0


def cmd_ensure_default(args: argparse.Namespace) -> int:
    prof = profiles_service.ensure_default_profile()
    here = profiles_service.lock_target_profile_id() == prof.id
    print(f"Default profile {prof.name!r} ({prof.id}); the lock {'lands on it' if here else 'is set elsewhere'}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.profiles", description=__doc__.split("\n\n")[0])
    p.add_argument("--data-dir", help="DisPatch data directory (default: the server's rule)")
    sub = p.add_subparsers(dest="verb", required=True)
    c = sub.add_parser("counts", help="what a profile has (numbers only)")
    c.add_argument("--profile", required=True)
    c.set_defaults(fn=cmd_counts)
    a = sub.add_parser("spawn-atlas", help="give a profile its own Atlas")
    a.add_argument("--profile", required=True)
    a.add_argument("--force", action="store_true")
    a.set_defaults(fn=cmd_spawn_atlas)
    n = sub.add_parser("create", help="add a User or Basic profile")
    n.add_argument("name")
    n.add_argument("--role", choices=("user", "basic"), default="user")
    n.add_argument("--household", help="Basic only: comma-separated household bot ids")
    n.set_defaults(fn=cmd_create)
    d = sub.add_parser("ensure-default", help="make the DisPatch default profile and lock target")
    d.set_defaults(fn=cmd_ensure_default)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cli.open_data_dir(args.data_dir)
        return args.fn(args)
    except cli.CliError as e:
        print(str(e), file=sys.stderr)
        return 1
    except ProfileServiceError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
