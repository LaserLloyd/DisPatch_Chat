"""``python -m app`` - account management and lock-out recovery from a terminal.

Run it on the machine DisPatch lives on (or inside the container with
``docker compose exec app python -m app ...``). It works on the data directory directly, so it
needs no sign-in, and works whether the server is running or not: a running server notices the
change within about a second (profile cache revision, ``app/accounts.py``) and signs the affected
profile out.

    python -m app users list
    python -m app users add <name> [--profile basic|user|admin|main-admin] [--password-stdin | --password-env VAR]
    python -m app users passwd <name> [--kind pin|password] [--password-stdin | --password-env VAR]
    python -m app users delete <name> [--yes]
    python -m app admin reset [--yes]
    python -m app token show | rotate

A secret is NEVER a command-line argument (it would sit in the shell history and ``ps``). It comes
from ``--password-stdin`` (one line), ``--password-env VAR``, or a hidden prompt on a terminal.
"Password" here means the credential of the profile's role: a PIN for User and Basic
profiles, a password for Admins (``--kind pin`` sets the protected first Admin's PIN instead:
the install PIN). The 3.x words still work as hidden aliases: ``restricted`` / ``member`` for
``basic`` / ``user``, ``owner`` for ``main-admin`` and ``owner reset`` for ``admin reset``.

``--data-dir`` (before or after the verb) picks another data directory; otherwise
``DISPATCH_DATA_DIR`` (legacy ``LOCAL_CHAT_DATA_DIR``) or ``~/.local/share/local-chat``, the same
rule the server uses. The first line printed always says which one was used, and a directory
without a ``chats.db`` is refused rather than silently created.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

from . import accounts, auth, config, credentials, recovery
from .contracts.permissions import Role, normalise_role

#: Lowest first. ``users add`` without ``--profile`` gets ``basic``. ``main-admin`` = the protected
#: first Admin (only when there is none); ``owner`` (its 3.x word) and ``restricted`` / ``member``
#: (the 3.x words for ``basic`` / ``user``) are still accepted, but not offered in the help.
ROLE_CHOICES = ("basic", "user", "admin", "main-admin", "owner", "restricted", "member")
SHOWN_ROLES = "{basic,user,admin,main-admin}"
MAIN_ADMIN_WORDS = ("main-admin", "owner")
DEFAULT_ROLE = "basic"


def _label(p: accounts.Profile) -> str:
    """How the command line names a profile's standing: ``main-admin`` for the protected Admin."""
    return "main-admin" if p.protected else p.role.value


class CliError(Exception):
    """A refused or failed command: the message is the whole output, exit status 1."""


# --------------------------------------------------------------------------- #
# Data directory
# --------------------------------------------------------------------------- #


def _data_dir_source(explicit: str | None) -> str:
    if explicit:
        return "--data-dir"
    for name in ("DISPATCH_DATA_DIR", "LOCAL_CHAT_DATA_DIR"):
        if os.environ.get(name):
            return name
    return "default location"


def _repoint(data_dir: Path) -> None:
    from . import migrate  # the same repointing the other command-line tools use
    migrate._repoint(data_dir)


def open_data_dir(explicit: str | None) -> Path:
    """Aim every module at the data directory, say which one it is, and refuse an empty one."""
    if explicit:
        _repoint(Path(explicit).expanduser().resolve())
    data = Path(config.DATA_DIR)
    print(f"DisPatch data directory: {data}  ({_data_dir_source(explicit)})", file=sys.stderr)
    db = accounts.db_path()
    if not db.is_file():
        raise CliError(
            f"There is no DisPatch database at {db}.\n"
            "This is the wrong data directory, or DisPatch has never been started here. "
            "Pass --data-dir <folder holding chats.db>, or set DISPATCH_DATA_DIR. "
            "In Docker run this inside the container: docker compose exec app python -m app ...")
    auth.ensure_owner_for_pin()          # a PIN-only 2.x install has no owner row until first use
    return data


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


def read_secret(args: argparse.Namespace, *, what: str, required: bool = True) -> str | None:
    """The secret for ``what`` from stdin, an environment variable or a hidden prompt."""
    if args.password_stdin and args.password_env:
        raise CliError("Use --password-stdin or --password-env, not both.")
    if args.password_stdin:
        line = sys.stdin.readline()
        if not line:
            raise CliError("--password-stdin: nothing on standard input.")
        return line.rstrip("\r\n")
    if args.password_env:
        value = os.environ.get(args.password_env)
        if not value:
            raise CliError(f"--password-env: the environment variable {args.password_env} is not set or is empty.")
        return value
    if not required:
        return None
    if sys.stdin.isatty():
        first = getpass.getpass(f"New {what}: ")
        if getpass.getpass(f"New {what} again: ") != first:
            raise CliError("The two entries differ; nothing was changed.")
        return first
    raise CliError(f"A {what} is needed: pass --password-stdin or --password-env VAR "
                   "(it is never accepted as an argument).")


def _confirm(prompt: str, args: argparse.Namespace) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise CliError("This changes accounts for good; add --yes to confirm (no terminal to ask on).")
    if input(f"{prompt} Type 'yes' to continue: ").strip().lower() != "yes":
        raise CliError("Cancelled; nothing was changed.")


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #


def _find(name: str) -> accounts.Profile:
    prof = accounts.get_by_name(name)
    if prof is None:
        known = ", ".join(repr(p.name) for p in accounts.list_profiles()) or "(none)"
        raise CliError(f"No profile named {name!r}. Profiles here: {known}")
    return prof


def _credentials_of(p: accounts.Profile) -> str:
    have = [label for label, ok in (("password", p.has_password), ("PIN", p.has_pin)) if ok]
    return " + ".join(have) or "none"


def _account_error(e: accounts.AccountError) -> CliError:
    return CliError(str(e))


def _audit(kind: str, prof: accounts.Profile | None, detail: str = "") -> None:
    accounts.log_activity(kind, profile_id=prof.id if prof else None, subject="command line",
                          detail=detail, category="audit")


def cmd_users_list(args: argparse.Namespace) -> int:
    profiles = accounts.list_profiles()
    if args.json:
        print(json.dumps([p.public() for p in profiles], indent=2))
        return 0
    if not profiles:
        print("No profiles yet. Create the main Admin in the setup wizard, or: "
              "python -m app users add <name> --profile main-admin")
        return 0
    rows = [("NAME", "ROLE", "SIGN-IN", "STATE", "CREATED")]
    for p in profiles:
        rows.append((p.name, _label(p), _credentials_of(p), "disabled" if p.disabled else "active", p.created[:10]))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    return 0


def cmd_users_add(args: argparse.Namespace) -> int:
    word = args.profile
    protected = word in MAIN_ADMIN_WORDS
    role = normalise_role("owner" if protected else word)
    name = args.name
    if protected and accounts.has_owner():
        owner = accounts.get_owner()
        raise CliError(f"This install already has a main Admin ({owner.name!r}). To get back into it: "
                       f"python -m app users passwd {owner.name!r}   or   python -m app admin reset")
    if role is Role.ADMIN and not protected:
        owner = accounts.get_owner()
        if not (owner and owner.has_password):
            raise CliError("Give the main Admin a password first (python -m app users passwd <name>): "
                           "an Admin's password is only as safe as the main Admin's.")
    needs_secret = role is not Role.BASIC
    what = "password" if role is Role.ADMIN else "PIN"
    secret = read_secret(args, what=what, required=needs_secret)
    pin_hash = password_hash = None
    if secret is not None:
        err = credentials.check_password(secret) if what == "password" else credentials.check_pin(secret)
        if err:
            raise CliError(err)
        if what == "password":
            password_hash = credentials.hash_secret(secret)
        else:
            pin_hash = credentials.hash_secret(secret)
    try:
        prof = accounts.create_profile(name, role, protected=protected, pin_hash=pin_hash,
                                       password_hash=password_hash)
    except accounts.AccountError as e:
        raise _account_error(e) from e
    _audit("profile_created", prof, f"{prof.name} ({_label(prof)})")
    # 4.0: the same hook the People API and the wizard run (an own Atlas for an Admin or a
    # User; none for Basic, none for the first Admin of a fresh install). Never fails the add.
    from . import profiles_service
    try:
        profiles_service.on_profile_created(prof, first_admin=protected)
    except Exception:  # the profile exists; Reset can make the Atlas later
        pass
    print(f"Added {_label(prof)} {prof.name!r} (sign-in: {_credentials_of(prof)}).")
    if role is Role.BASIC and secret is None:
        print("No PIN was set: anyone at the lock screen can pick this profile. "
              "Set one with: python -m app users passwd <name>")
    if protected:
        auth.burn_setup_token()
    return 0


def cmd_users_passwd(args: argparse.Namespace) -> int:
    prof = _find(args.name)
    kind = args.kind or ("password" if prof.role is Role.ADMIN else "pin")
    if kind == "pin" and prof.role is Role.ADMIN and not prof.protected:
        raise CliError("Admins sign in with a password; use --kind password.")
    if kind == "password" and prof.role is not Role.ADMIN:
        raise CliError(f"{prof.role.value.capitalize()} profiles sign in with a PIN; use --kind pin.")
    secret = read_secret(args, what=kind)
    assert secret is not None
    try:
        if kind == "pin":
            auth.set_profile_pin(prof.id, secret)
        else:
            auth.set_profile_password(prof.id, secret)
    except accounts.AccountError as e:
        raise _account_error(e) from e
    _audit("pin_set" if kind == "pin" else "password_set", prof, "command line")
    label = "PIN" if kind == "pin" else "password"
    print(f"Set a new {label} for {_label(prof)} {prof.name!r}. Its other sessions and remembered devices were signed out.")
    if prof.protected and kind == "pin":
        print(f"The PIN recovery code was regenerated: {auth.RECOVERY_PATH}")
    return 0


def cmd_users_delete(args: argparse.Namespace) -> int:
    prof = _find(args.name)
    if prof.protected:
        raise CliError("The main Admin cannot be deleted (every install keeps exactly one). To start "
                       "the main Admin's sign-in again: python -m app admin reset")
    # 4.0: deleting a profile deletes its content first (chats, own bots, pictures, cloned
    # voices) through the same service the People API uses, so nothing is left without an owner.
    from . import profiles_service
    counts = profiles_service.content_counts(prof.id)
    what = ", ".join(f"{v} {k}" for k, v in counts.items())
    _confirm(f"Delete the {_label(prof)} profile {prof.name!r} and everything it owns ({what}), "
             "its sessions and remembered devices?", args)
    try:
        token = profiles_service.delete_preview(prof.id)["token"]
        profiles_service.delete_profile(prof.id, token)
    except profiles_service.ProfileServiceError as e:
        raise CliError(str(e)) from e
    except accounts.AccountError as e:
        raise _account_error(e) from e
    _audit("profile_deleted", prof, prof.name)
    print(f"Deleted {_label(prof)} {prof.name!r} and its content ({what}); it is signed out everywhere.")
    return 0


# --------------------------------------------------------------------------- #
# admin (the main Admin's lock-out recovery) / token
# --------------------------------------------------------------------------- #


def cmd_owner_reset(args: argparse.Namespace) -> int:
    owner = accounts.get_owner()
    who = f"the main Admin {owner.name!r}" if owner else "the install (there is no main Admin yet)"
    _confirm(f"Clear the password and PIN of {who} and open setup again? Chats and other profiles are kept.", args)
    res = recovery.reset_owner(via="command line")
    print(f"Cleared the sign-in of {res.owner_name!r}." if res.owner_name else "No main Admin existed; setup is open.")
    print("The install is unclaimed again: it is closed to the network until the main Admin sets a new password.")
    print("  - On THIS computer open the app (http://localhost:8765 by default) and finish the setup wizard.")
    print("  - From another device, or from a browser talking to a container, the wizard asks for:")
    print(f"        SETUP TOKEN: {res.setup_token}")
    print(f"    (also kept in {auth.SETUP_TOKEN_PATH}; it is deleted once the main Admin exists.)")
    print("Other profiles keep their PINs but cannot sign in until the main Admin is back.")
    return 0


def _writable_security() -> auth.SecurityConfig:
    cfg = auth.load()
    if cfg.pin_hash == auth._LOCKDOWN_HASH:
        raise CliError(f"{auth.SECURITY_PATH} exists but cannot be read; fix it, or run: python -m app admin reset")
    return cfg


def cmd_token_show(args: argparse.Namespace) -> int:
    cfg = _writable_security()
    if not cfg.api_token:
        print("No machine API key is set (remote callers of /api/inject and friends are refused). "
              "Create one with: python -m app token rotate")
        return 0
    print(cfg.api_token)
    print("Keep this private: it lets a machine post messages and read agent state. "
          "Send it as the X-API-Key header.", file=sys.stderr)
    return 0


def cmd_token_rotate(args: argparse.Namespace) -> int:
    cfg = _writable_security()
    with auth._lock:
        cfg.api_token = secrets.token_urlsafe(32)
        auth._write(cfg)
        auth._bust_cache()
    _audit("api_token_changed", accounts.get_owner(), "rotated (command line)")
    print(cfg.api_token)
    print("New machine API key printed above; the old one stopped working. "
          "Update OpenClaw and any cron that sends X-API-Key.", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def _secret_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--password-stdin", action="store_true",
                   help="read the PIN/password from the first line of standard input")
    p.add_argument("--password-env", metavar="VAR",
                   help="read the PIN/password from this environment variable")


def build_parser() -> argparse.ArgumentParser:
    help_dir = "the data directory (default: DISPATCH_DATA_DIR, else ~/.local/share/local-chat)"
    common = argparse.ArgumentParser(add_help=False)       # lets --data-dir follow the verb too
    common.add_argument("--data-dir", metavar="DIR", dest="verb_data_dir", default=None, help=help_dir)
    ap = argparse.ArgumentParser(
        prog="python -m app",
        description="DisPatch accounts and lock-out recovery. Run on the machine DisPatch lives on "
                    "(in Docker: docker compose exec app python -m app ...).")
    ap.add_argument("--data-dir", metavar="DIR", dest="data_dir", default=None, help=help_dir)
    top = ap.add_subparsers(dest="noun", required=True, metavar="{users,admin,token}")

    users = top.add_parser("users", help="list, add, change or delete profiles")
    uv = users.add_subparsers(dest="verb", required=True)
    p = uv.add_parser("list", parents=[common], help="list profiles (never shows a secret)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_users_list)

    p = uv.add_parser("add", parents=[common], allow_abbrev=False, help="add a profile (the lowest role unless --profile says otherwise)")
    p.add_argument("name")
    p.add_argument("--profile", choices=ROLE_CHOICES, default=DEFAULT_ROLE, metavar=SHOWN_ROLES,
                   help="role of the new profile: basic (default), user, admin, or main-admin (the protected "
                        "first Admin, only when there is none)")
    _secret_flags(p)
    p.set_defaults(fn=cmd_users_add)

    p = uv.add_parser("passwd", parents=[common], allow_abbrev=False, help="set a new password/PIN for a profile (signs it out)")
    p.add_argument("name")
    p.add_argument("--kind", choices=("password", "pin"),
                   help="which credential (default: password for an Admin, PIN for a User/Basic profile)")
    _secret_flags(p)
    p.set_defaults(fn=cmd_users_passwd)

    p = uv.add_parser("delete", parents=[common], help="delete a profile (never the main Admin)")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="do not ask")
    p.set_defaults(fn=cmd_users_delete)

    # `owner` is the 3.x noun, kept as a hidden alias (old docs and scripts say `owner reset`):
    # main() rewrites it to `admin` before parsing (an argparse SUPPRESS'd sub-command still
    # shows in the help).
    ov = top.add_parser("admin", help="the main Admin's lock-out recovery").add_subparsers(
        dest="verb", required=True)
    p = ov.add_parser("reset", parents=[common],
                      help="clear the main Admin's password and PIN, open setup again, print a new setup token")
    p.add_argument("--yes", action="store_true", help="do not ask")
    p.set_defaults(fn=cmd_owner_reset)

    token = top.add_parser("token", help="the machine API key (X-API-Key) for agents and crons")
    tv = token.add_subparsers(dest="verb", required=True)
    p = tv.add_parser("show", parents=[common], help="print the current key")
    p.set_defaults(fn=cmd_token_show)
    p = tv.add_parser("rotate", parents=[common], help="make a new key; the old one stops working")
    p.set_defaults(fn=cmd_token_rotate)
    return ap


def _legacy_nouns(argv: list[str]) -> list[str]:
    """``owner`` (the 3.x noun) -> ``admin``: the first word that is not ``--data-dir DIR``."""
    out, i = list(argv), 0
    while i < len(out):
        if out[i] == "--data-dir":
            i += 2
            continue
        if out[i].startswith("--data-dir="):
            i += 1
            continue
        if out[i] == "owner":
            out[i] = "admin"
        break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, extras = parser.parse_known_args(_legacy_nouns(sys.argv[1:] if argv is None else argv))
    if extras:
        # argparse would echo the stray words back, and a stray word may be somebody's PIN
        # (`--password 1234`). Say how many, not what.
        parser.error(f"{len(extras)} unrecognized argument(s) (not echoed). A PIN or password is never "
                     "an argument: use --password-stdin, --password-env VAR, or the prompt.")
    try:
        open_data_dir(args.verb_data_dir or args.data_dir)
        return args.fn(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except accounts.AccountError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f"error: the database could not be used ({e}). Is the folder read-only, on a network share, "
              "or owned by another user? Run this as the user DisPatch runs as.", file=sys.stderr)
        return 1


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
