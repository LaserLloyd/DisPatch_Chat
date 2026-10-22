"""Privacy matrix, REST surface: every by-id route of every owner-scoped kind x every principal.

One test per (kind, actor): the fixture install is built fresh, every probe of the kind runs
against every owner's resource (foreign first, own last), and ALL failing cells are listed
together. Rules: tests/privacy_fixtures.py ``expected``. Opt in with DISPATCH_PRIVACY_MATRIX=1.
"""
from __future__ import annotations

import pytest
from privacy_fixtures import ACTORS, KINDS, PRIVACY_MARKS, build_install, conclude, run_kind

pytestmark = PRIVACY_MARKS


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("actor", ACTORS)
def test_by_id_routes(world, monkeypatch, actor, kind):
    inst = build_install(world, monkeypatch, actor)
    fails, deferred = run_kind(inst, actor, kind)
    conclude(fails, deferred)


def test_fixture_is_complete(world, monkeypatch):
    """Every owner got every resource it should (reports what the Foundation base cannot build)."""
    inst = build_install(world, monkeypatch, "A0")
    assert not inst.problems, "\n".join(inst.problems)
