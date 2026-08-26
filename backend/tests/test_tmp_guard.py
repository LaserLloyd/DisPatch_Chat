"""The write guard's own rule -- the one that failed silently.

conftest's `_no_writes_outside_tmp` redirects config.DATA_DIR / AVATAR_DIR away
from a real installation before every test. The redirect only happens when the
rule says the path is NOT disposable, so a rule that answers "disposable" too
freely disarms the guard completely -- and disarms it invisibly, because a test
that writes to the wrong directory still passes.

That is not a hypothetical failure mode. The original rule inferred a temp root
by walking a basetemp path to its TOP-LEVEL directory. Under the default
basetemp (/tmp/pytest-of-<user>/...) the root is /tmp and the rule is sound;
run pytest with `--basetemp=/var/tmp/...` -- the ordinary response to /tmp
filling up -- and the root becomes `/var`. Where home is /var/home/<user>
(Fedora Atomic symlinks /home to /var/home), the live data directory then sat
inside the "temp" root, the guard no-opped, and a full suite run wrote fixture
bytes over the family app's avatars and pruned its snapshot store down to a
keep-set of one. Every test reported green.

These tests pin the rule against exactly that.
"""
from __future__ import annotations

from pathlib import Path

from _tmpguard import is_disposable

# A stand-in for the live install. The literal shape matters more than the
# name: a real directory whose top-level component (/var) is also the
# top-level component of a non-default basetemp (/var/tmp/...).
# Assembled from parts rather than written as one literal so it cannot read as
# a real home directory to anything scanning this repository.
LIVE_DATA_DIR = Path("/var") / "home" / "someone" / ".local" / "share" / "local-chat"


def test_a_real_data_dir_is_not_disposable_under_a_var_tmp_basetemp():
    """THE REGRESSION. /var/tmp basetemp must not make /var/home disposable."""
    assert not is_disposable(LIVE_DATA_DIR, basetemp="/var/tmp/pytest-of-someone/pytest-0"), (
        "the live data directory was classified as scratch space, which "
        "disarms the guard that keeps tests out of a real installation")


def test_a_real_data_dir_is_not_disposable_under_the_default_basetemp():
    assert not is_disposable(LIVE_DATA_DIR, basetemp="/tmp/pytest-of-someone/pytest-0")


def test_a_home_relative_data_dir_is_not_disposable(tmp_path):
    """Whatever this box calls home, the guard must not wave it through."""
    assert not is_disposable(Path.home() / ".local" / "share" / "local-chat",
                             basetemp=tmp_path)


def test_paths_inside_the_basetemp_are_disposable(tmp_path, tmp_path_factory):
    basetemp = tmp_path_factory.getbasetemp()
    assert is_disposable(tmp_path, basetemp)
    assert is_disposable(tmp_path / "data" / "avatar-snapshots", basetemp)


def test_a_sibling_that_merely_shares_a_prefix_is_not_disposable():
    """`/tmpfoo` is not inside `/tmp`.

    The old rule compared with str.startswith, which cannot tell the two apart;
    containment is a path question, not a string question.
    """
    assert not is_disposable("/tmpfoo/data", basetemp="/tmp/pytest-of-someone/pytest-0")


def test_the_guard_actually_redirected_this_test(tmp_path, tmp_path_factory):
    """End to end: by the time a test body runs, the real dirs are gone.

    This is the assertion that would have gone red on the run that damaged the
    live install, instead of that run reporting 852 passed.
    """
    from app import config

    basetemp = tmp_path_factory.getbasetemp()
    assert is_disposable(config.DATA_DIR, basetemp), config.DATA_DIR
    assert is_disposable(config.AVATAR_DIR, basetemp), config.AVATAR_DIR
    # And specifically not THIS machine's real store.
    assert config.DATA_DIR != Path.home() / ".local" / "share" / "local-chat"
