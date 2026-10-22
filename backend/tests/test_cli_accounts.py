"""``python -m app users|owner|token`` (app/cli.py): every verb, on a scratch data directory.

The command line is the supported way back into an install: nothing here goes through HTTP, a
secret is never an argument, and the first line of output names the data directory acted on.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import accounts, auth, cli, config, credentials, migrate

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A scratch data directory holding a chats.db, with every module path aimed at it. The
    attributes `migrate._repoint` rewrites are patched first so they are restored afterwards."""
    d = tmp_path / "cli-data"
    d.mkdir()
    for obj, name in ((config, "DATA_DIR"), (config, "CONFIG_PATH"), (config, "BACKUP_DIR"),
                      (auth, "SECURITY_PATH"), (auth, "RECOVERY_PATH"), (auth, "TRUSTED_PATH"),
                      (auth, "SETUP_TOKEN_PATH")):
        monkeypatch.setattr(obj, name, getattr(obj, name))
    migrate._repoint(d)
    with accounts.connect():
        pass                                 # creates chats.db with the account tables
    return d


@pytest.fixture
def run(capsys, monkeypatch):
    """run("users", "list", stdin="...") -> (exit code, stdout, stderr)."""
    def _run(*argv: str, stdin: str | None = None, tty: bool = False):
        stream = io.StringIO(stdin or "")
        stream.isatty = lambda: tty          # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", stream)
        try:
            code = cli.main(list(argv))
        except SystemExit as e:             # argparse usage errors
            code = e.code
        out = capsys.readouterr()
        return code, out.out, out.err
    return _run


def make_owner(password="owner-pass1", name="Olive"):
    return accounts.create_profile(name, "owner", password_hash=credentials.hash_secret(password))


# --------------------------------------------------------------------------- #
# Where it acts
# --------------------------------------------------------------------------- #


def test_every_verb_says_which_data_directory_it_acts_on(data, run):
    for argv in (("users", "list"), ("token", "show")):
        code, _out, err = run(*argv)
        assert code == 0
        assert str(data) in err.splitlines()[0] and "data directory" in err.splitlines()[0]


def test_a_directory_without_a_database_is_refused_and_not_created(tmp_path, monkeypatch, run):
    for obj, name in ((config, "DATA_DIR"), (config, "CONFIG_PATH"), (config, "BACKUP_DIR"),
                      (auth, "SECURITY_PATH"), (auth, "RECOVERY_PATH"), (auth, "TRUSTED_PATH"),
                      (auth, "SETUP_TOKEN_PATH")):
        monkeypatch.setattr(obj, name, getattr(obj, name))
    empty = tmp_path / "wrong-place"
    code, out, err = run("--data-dir", str(empty), "users", "list")
    assert code == 1 and out == ""
    assert str(empty) in err and "no dispatch database" in err.lower()
    assert not empty.exists()                 # a wrong path must not leave a phantom install behind


def test_data_dir_flag_is_accepted_before_or_after_the_verb(data, run):
    for argv in (("--data-dir", str(data), "users", "list"), ("users", "list", "--data-dir", str(data))):
        code, _out, err = run(*argv)
        assert code == 0 and str(data) in err


def test_a_pin_only_2x_install_shows_its_owner(data, run):
    auth.set_pin("1234")
    accounts.delete_owner_row()
    accounts.reset_for_tests()
    code, out, _ = run("users", "list")
    assert code == 0 and "Owner" in out and "PIN" in out


# --------------------------------------------------------------------------- #
# users list
# --------------------------------------------------------------------------- #


def test_list_shows_roles_and_never_a_hash(data, run):
    make_owner()
    accounts.create_profile("Kit", "restricted")
    code, out, _ = run("users", "list")
    assert code == 0
    assert "Olive" in out and "owner" in out and "password" in out
    assert "Kit" in out and "basic" in out
    assert "argon2" not in out and "pbkdf2" not in out
    code, out, _ = run("users", "list", "--json")
    rows = json.loads(out)
    assert {r["name"]: r["role"] for r in rows} == {"Olive": "admin", "Kit": "basic"}
    assert all("hash" not in k for r in rows for k in r)


def test_list_on_an_empty_install_says_how_to_start(data, run):
    code, out, _ = run("users", "list")
    assert code == 0 and "No profiles" in out


# --------------------------------------------------------------------------- #
# users add
# --------------------------------------------------------------------------- #


def test_add_without_a_profile_is_the_lowest_role_with_no_pin(data, run):
    make_owner()
    code, out, _ = run("users", "add", "Kit")
    assert code == 0
    kit = accounts.get_by_name("Kit")
    assert kit.role == "basic" and not kit.has_pin and not kit.has_password
    assert "No PIN was set" in out


def test_add_member_takes_the_pin_from_stdin(data, run):
    make_owner()
    code, out, err = run("users", "add", "Noor", "--profile", "member", "--password-stdin", stdin="4321\n")
    assert code == 0, err
    noor = accounts.get_by_name("Noor")
    assert noor.role == "user" and noor.has_pin and not noor.has_password
    assert auth.verify_profile_secret(noor, "pin", "4321")
    assert "4321" not in out + err


def test_add_reads_the_secret_from_an_environment_variable(data, run, monkeypatch):
    make_owner()
    monkeypatch.setenv("NEW_PIN", "9876")
    code, _, err = run("users", "add", "Noor", "--profile", "member", "--password-env", "NEW_PIN")
    assert code == 0, err
    assert auth.verify_profile_secret(accounts.get_by_name("Noor"), "pin", "9876")


def test_add_member_without_a_secret_is_refused(data, run):
    make_owner()
    code, _, err = run("users", "add", "Noor", "--profile", "member")
    assert code == 1 and "--password-stdin" in err
    assert accounts.get_by_name("Noor") is None


def test_add_refuses_a_missing_or_double_secret_source(data, run, monkeypatch):
    make_owner()
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    code, _, err = run("users", "add", "A", "--profile", "member", "--password-env", "NOT_SET_ANYWHERE")
    assert code == 1 and "NOT_SET_ANYWHERE" in err
    code, _, err = run("users", "add", "B", "--profile", "member", "--password-stdin", "--password-env", "X")
    assert code == 1 and "not both" in err
    code, _, err = run("users", "add", "C", "--profile", "member", "--password-stdin", stdin="")
    assert code == 1 and "nothing on standard input" in err


def test_a_secret_can_never_be_an_argument(data, run, capsys):
    make_owner()
    for flag in ("--password", "--pin"):
        code, out, err = run("users", "add", "Noor", "--profile", "member", flag, "4321")
        assert code == 2
        assert "unrecognized argument" in err and "4321" not in err      # the stray word is not echoed
    assert accounts.get_by_name("Noor") is None


def test_add_enforces_the_pin_and_password_rules(data, run):
    make_owner()
    code, _, err = run("users", "add", "Noor", "--profile", "member", "--password-stdin", stdin="12\n")
    assert code == 1 and "PIN" in err
    code, _, err = run("users", "add", "Ada", "--profile", "admin", "--password-stdin", stdin="short\n")
    assert code == 1 and "assword" in err


def test_add_admin_needs_an_owner_password_first(data, run):
    code, _, err = run("users", "add", "Ada", "--profile", "admin", "--password-stdin", stdin="admin-pass1\n")
    assert code == 1 and "owner a password first" in err
    make_owner()
    code, _, err = run("users", "add", "Ada", "--profile", "admin", "--password-stdin", stdin="admin-pass1\n")
    assert code == 0, err
    ada = accounts.get_by_name("Ada")
    assert ada.role == "admin" and ada.has_password and not ada.has_pin
    assert auth.verify_profile_secret(ada, "password", "admin-pass1")


def test_add_owner_only_when_there_is_none(data, run):
    code, out, err = run("users", "add", "Olive", "--profile", "owner", "--password-stdin", stdin="owner-pass1\n")
    assert code == 0, err
    assert accounts.get_owner().name == "Olive" and accounts.get_owner().has_password
    code, _, err = run("users", "add", "Other", "--profile", "owner", "--password-stdin", stdin="owner-pass2\n")
    assert code == 1 and "already has an owner" in err and "owner reset" in err
    assert len([p for p in accounts.list_profiles() if p.protected]) == 1


def test_add_rejects_a_duplicate_name_and_an_unknown_role(data, run):
    make_owner()
    run("users", "add", "Kit")
    code, _, err = run("users", "add", "kit")
    assert code == 1 and "already exists" in err
    code, _, err = run("users", "add", "X", "--profile", "root")
    assert code == 2 and "invalid choice" in err


def test_a_prompt_on_a_terminal_asks_twice(data, run, monkeypatch):
    make_owner()
    answers = iter(["4321", "4321"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    code, _, err = run("users", "add", "Noor", "--profile", "member", tty=True)
    assert code == 0, err
    assert auth.verify_profile_secret(accounts.get_by_name("Noor"), "pin", "4321")
    answers = iter(["4321", "9999"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    code, _, err = run("users", "add", "Mo", "--profile", "member", tty=True)
    assert code == 1 and "differ" in err and accounts.get_by_name("Mo") is None


# --------------------------------------------------------------------------- #
# users passwd
# --------------------------------------------------------------------------- #


def test_passwd_sets_the_role_credential_and_ends_sessions(data, run):
    make_owner()
    accounts.create_profile("Noor", "member", pin_hash=credentials.hash_secret("1111"))
    noor = accounts.get_by_name("Noor")
    tok = auth.issue_session(profile_id=noor.id)
    assert auth.get_session(tok) is not None
    code, out, err = run("users", "passwd", "Noor", "--password-stdin", stdin="2222\n")
    assert code == 0, err
    noor = accounts.get_by_name("Noor")
    assert auth.verify_profile_secret(noor, "pin", "2222")
    assert not auth.verify_profile_secret(noor, "pin", "1111")
    assert auth.get_session(tok) is None
    assert "2222" not in out + err


def test_passwd_for_the_owner_sets_the_password_and_keeps_the_pin(data, run):
    make_owner()
    auth.set_pin("2468")
    code, _, err = run("users", "passwd", "olive", "--password-stdin", stdin="a-new-owner-pass\n")
    assert code == 0, err
    owner = accounts.get_owner()
    assert auth.verify_profile_secret(owner, "password", "a-new-owner-pass")
    assert not auth.verify_profile_secret(owner, "password", "owner-pass1")
    assert auth.verify_pin("2468")


def test_passwd_kind_pin_for_the_owner_changes_the_install_pin(data, run):
    make_owner()
    code, out, err = run("users", "passwd", "Olive", "--kind", "pin", "--password-stdin", stdin="8642\n")
    assert code == 0, err
    assert auth.verify_pin("8642") and auth.RECOVERY_PATH.exists()
    assert "recovery code was regenerated" in out


def test_passwd_refuses_a_credential_the_role_does_not_use(data, run):
    make_owner()
    accounts.create_profile("Noor", "member", pin_hash=credentials.hash_secret("1111"))
    accounts.update_profile(accounts.get_owner().id, password_hash=credentials.hash_secret("owner-pass1"))
    accounts.create_profile("Ada", "admin", password_hash=credentials.hash_secret("admin-pass1"))
    code, _, err = run("users", "passwd", "Noor", "--kind", "password", "--password-stdin", stdin="long-enough-1\n")
    assert code == 1 and "sign in with a PIN" in err
    code, _, err = run("users", "passwd", "Ada", "--kind", "pin", "--password-stdin", stdin="1234\n")
    assert code == 1 and "password" in err


def test_passwd_unknown_profile_lists_the_known_ones(data, run):
    make_owner()
    code, _, err = run("users", "passwd", "Nobody", "--password-stdin", stdin="x\n")
    assert code == 1 and "Nobody" in err and "Olive" in err


def test_passwd_applies_the_length_rules(data, run):
    make_owner()
    code, _, err = run("users", "passwd", "Olive", "--password-stdin", stdin="short\n")
    assert code == 1 and "assword" in err
    assert auth.verify_profile_secret(accounts.get_owner(), "password", "owner-pass1")


# --------------------------------------------------------------------------- #
# users delete
# --------------------------------------------------------------------------- #


def test_delete_needs_yes_when_there_is_no_terminal(data, run):
    make_owner()
    accounts.create_profile("Kit", "restricted")
    code, _, err = run("users", "delete", "Kit")
    assert code == 1 and "--yes" in err
    assert accounts.get_by_name("Kit") is not None


def test_delete_removes_the_profile_and_its_sessions(data, run):
    make_owner()
    kit = accounts.create_profile("Kit", "restricted")
    tok = auth.issue_session(profile_id=kit.id)
    code, out, err = run("users", "delete", "Kit", "--yes")
    assert code == 0, err
    assert accounts.get_by_name("Kit") is None and auth.get_session(tok) is None
    assert "Kit" in out and accounts.get_owner() is not None


def test_delete_asks_on_a_terminal(data, run, monkeypatch):
    make_owner()
    accounts.create_profile("Kit", "restricted")
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    code, _, err = run("users", "delete", "Kit", tty=True)
    assert code == 1 and "Cancelled" in err and accounts.get_by_name("Kit") is not None
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    code, _, err = run("users", "delete", "Kit", tty=True)
    assert code == 0 and accounts.get_by_name("Kit") is None


def test_the_owner_cannot_be_deleted(data, run):
    make_owner()
    code, _, err = run("users", "delete", "Olive", "--yes")
    assert code == 1 and "cannot be deleted" in err and "owner reset" in err
    assert accounts.get_owner() is not None


def test_delete_unknown_name(data, run):
    code, _, err = run("users", "delete", "Ghost", "--yes")
    assert code == 1 and "Ghost" in err


# --------------------------------------------------------------------------- #
# owner reset
# --------------------------------------------------------------------------- #


def test_owner_reset_clears_the_signin_keeps_the_rest_and_prints_a_token(data, run):
    owner = make_owner()
    auth.set_pin("2468")
    kit = accounts.create_profile("Kit", "member", pin_hash=credentials.hash_secret("1111"))
    tok = auth.issue_session(profile_id=owner.id)
    from app import policy
    assert policy.claimed()
    code, out, err = run("owner", "reset", "--yes")
    assert code == 0, err
    after = accounts.get_owner()
    assert after.id == owner.id and after.name == "Olive"          # the row (and its history) stays
    assert not after.has_password and not after.has_pin
    assert not policy.claimed() and not auth.is_pin_set()
    assert not auth.RECOVERY_PATH.exists()
    assert accounts.get_by_name("Kit").pin_hash == kit.pin_hash    # other profiles untouched
    assert auth.get_session(tok) is None
    line = next(ln for ln in out.splitlines() if "SETUP TOKEN:" in ln)
    token = line.split("SETUP TOKEN:")[1].strip()
    assert len(token) >= 16 and auth.verify_setup_token(token)
    assert auth.SETUP_TOKEN_PATH.read_text().strip() == token
    assert not auth.verify_setup_token("wrong-token-wrong-token")


def test_owner_reset_needs_yes_without_a_terminal_and_changes_nothing(data, run):
    make_owner()
    code, _, err = run("owner", "reset")
    assert code == 1 and "--yes" in err
    assert accounts.get_owner().has_password and not auth.SETUP_TOKEN_PATH.exists()


def test_owner_reset_on_an_install_with_no_owner_just_opens_setup(data, run):
    code, out, err = run("owner", "reset", "--yes")
    assert code == 0, err
    assert "no owner existed" in out.lower() and "SETUP TOKEN:" in out


def test_owner_reset_is_repeatable_and_replaces_the_token(data, run):
    make_owner()
    _, out1, _ = run("owner", "reset", "--yes")
    _, out2, _ = run("owner", "reset", "--yes")
    t1 = out1.split("SETUP TOKEN:")[1].split()[0]
    t2 = out2.split("SETUP TOKEN:")[1].split()[0]
    assert t1 != t2 and auth.verify_setup_token(t2) and not auth.verify_setup_token(t1)


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #


def test_token_rotate_then_show(data, run):
    code, out, _ = run("token", "show")
    assert code == 0 and "No machine API key" in out
    code, out, err = run("token", "rotate")
    assert code == 0
    new = out.strip()
    assert len(new) >= 32 and auth.verify_api_token(new)
    code, out, _ = run("token", "show")
    assert out.strip() == new
    code, out2, _ = run("token", "rotate")
    assert out2.strip() != new and not auth.verify_api_token(new) and auth.verify_api_token(out2.strip())


def test_token_refuses_to_overwrite_an_unreadable_security_file(data, run):
    auth.SECURITY_PATH.write_text("pin: [unclosed", encoding="utf-8")
    auth._cache = None
    code, _, err = run("token", "rotate")
    assert code == 1 and "cannot be read" in err
    assert auth.SECURITY_PATH.read_text(encoding="utf-8") == "pin: [unclosed"


# --------------------------------------------------------------------------- #
# As a real process
# --------------------------------------------------------------------------- #


def test_python_dash_m_app_runs_as_a_process(tmp_path):
    d = tmp_path / "proc-data"
    d.mkdir()
    env = {**__import__("os").environ, "DISPATCH_DATA_DIR": str(d), "PYTHONPATH": str(BACKEND),
           "DISPATCH_ARGON2_MEMORY_KIB": "8", "DISPATCH_ARGON2_PASSES": "1"}
    env.pop("LOCAL_CHAT_DATA_DIR", None)
    cp = subprocess.run([sys.executable, "-m", "app", "users", "list"], cwd=BACKEND, env=env,
                        capture_output=True, text=True, timeout=120)
    assert cp.returncode == 1 and "DISPATCH_DATA_DIR" in cp.stderr and str(d) in cp.stderr
    sqlite_db = d / "chats.db"
    assert not sqlite_db.exists()
    import sqlite3
    con = sqlite3.connect(sqlite_db)
    con.execute("CREATE TABLE seen (x)")
    con.commit()
    con.close()
    cp = subprocess.run([sys.executable, "-m", "app", "users", "add", "Olive", "--profile", "owner",
                         "--password-stdin"], cwd=BACKEND, env=env, input="owner-pass1\n",
                        capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0, cp.stderr
    cp = subprocess.run([sys.executable, "-m", "app", "users", "list"], cwd=BACKEND, env=env,
                        capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0 and "Olive" in cp.stdout and "owner-pass1" not in cp.stdout + cp.stderr


def test_a_locked_or_unusable_database_is_a_message_not_a_traceback(data, run, monkeypatch):
    import sqlite3
    make_owner()

    def boom(*a, **kw):
        raise sqlite3.OperationalError("attempt to write a readonly database")
    monkeypatch.setattr(accounts, "create_profile", boom)
    code, _, err = run("users", "add", "Kit")
    assert code == 1 and "database could not be used" in err and "readonly" in err
