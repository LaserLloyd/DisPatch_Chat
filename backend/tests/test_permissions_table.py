"""4.0 role table, cell by cell (SPEC section 5; BUILD-PLAN-v2 §1.1).

Every (role, permission) pair is pinned to exactly one of: on by default, off but
grantable by an Admin, or never. A change to the table is a product decision; it must
change this file too.
"""
from __future__ import annotations

import pytest

from app.contracts.permissions import (
    LOCK_TARGET_FENCE,
    ROLE_DEFAULTS,
    ROLE_GRANTABLE,
    ROLE_NEVER,
    ROLE_ORDER,
    Permission,
    Role,
    effective_permissions,
    grantable,
    permissions_document,
    tool_permission,
)

P = Permission
ON, GRANT, NEVER = "on", "grantable", "never"

#: SPEC section 5, word for word. Admin = everything (no overrides).
TABLE: dict[Permission, tuple[str, str]] = {   # (User, Basic)
    P.USE_CHAT: (ON, ON),
    P.USE_VOICE: (ON, ON),
    P.ATTACH_FILES: (ON, ON),
    P.USE_LOOKUPS: (ON, ON),
    P.MANAGE_HISTORY: (ON, ON),
    P.USE_NOTIFICATIONS: (ON, ON),
    P.FILES_UPLOAD: (ON, ON),
    P.FILES_READ: (ON, NEVER),
    P.USE_IMAGES: (ON, GRANT),
    P.VOICE_LAB: (ON, GRANT),
    P.CREATE_CHARACTERS: (ON, NEVER),
    P.CHOOSE_BACKEND: (ON, NEVER),
    P.DISPATCH_AGENTS: (GRANT, GRANT),
    P.APPROVE_AGENT_ACTIONS: (GRANT, GRANT),
    P.USE_TOOLS: (NEVER, NEVER),
    P.MANAGE_AGENTS: (NEVER, NEVER),
    P.MANAGE_CONNECTIONS: (NEVER, NEVER),
    P.MANAGE_CHARACTERS: (NEVER, NEVER),
    P.MANAGE_PLUGINS: (NEVER, NEVER),
    P.MANAGE_USERS: (NEVER, NEVER),
    P.MANAGE_SETTINGS: (NEVER, NEVER),
    P.VIEW_ACTIVITY: (NEVER, NEVER),
    P.ADMIN_SYSTEM: (NEVER, NEVER),
}


def test_the_table_names_every_permission():
    assert set(TABLE) == set(Permission)


def test_three_roles_lowest_first():
    assert ROLE_ORDER == (Role.BASIC, Role.USER, Role.ADMIN)
    assert {r.value for r in Role} == {"admin", "user", "basic"}


@pytest.mark.parametrize("perm", list(Permission), ids=lambda p: p.value)
@pytest.mark.parametrize("role", [Role.USER, Role.BASIC], ids=lambda r: r.value)
def test_cell(role, perm):
    want = TABLE[perm][0 if role is Role.USER else 1]
    got = (ON if perm in ROLE_DEFAULTS[role] else GRANT if perm in ROLE_GRANTABLE[role]
           else NEVER if perm in ROLE_NEVER[role] else "missing")
    assert got == want, f"{role.value}/{perm.value}: {got}, SPEC says {want}"


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
def test_columns_are_disjoint_and_complete(role):
    d, g, n = ROLE_DEFAULTS[role], ROLE_GRANTABLE[role], ROLE_NEVER[role]
    assert not (d & g) and not (d & n) and not (g & n)
    assert d | g | n == frozenset(Permission)


def test_admin_holds_everything_and_takes_no_overrides():
    assert ROLE_DEFAULTS[Role.ADMIN] == frozenset(Permission)
    assert not ROLE_GRANTABLE[Role.ADMIN] and not ROLE_NEVER[Role.ADMIN]
    every = {p.value for p in Permission}
    assert effective_permissions(Role.ADMIN, denies=["use_chat", "admin_system"]) == every
    assert effective_permissions("admin", grants=["anything"]) == every


@pytest.mark.parametrize("role", [Role.USER, Role.BASIC], ids=lambda r: r.value)
def test_effective_is_defaults_plus_grantable_grants_minus_denies(role):
    base = {p.value for p in ROLE_DEFAULTS[role]}
    assert effective_permissions(role) == base
    for perm in ROLE_GRANTABLE[role]:
        assert perm.value in effective_permissions(role, grants=[perm.value])
        assert grantable(role, perm.value)
    for perm in ROLE_NEVER[role]:
        # defence in depth: a stored grant outside the grantable column is dropped
        assert perm.value not in effective_permissions(role, grants=[perm.value])
        assert not grantable(role, perm.value)
    some = sorted(base)[0]
    assert some not in effective_permissions(role, denies=[some])


def test_tool_panels_are_admin_only():
    for role in (Role.USER, Role.BASIC):
        assert tool_permission("benchmark") not in effective_permissions(
            role, grants=[tool_permission("benchmark"), "use_tools"])
        assert not grantable(role, tool_permission("benchmark"))
    assert "use_tools" in effective_permissions(Role.ADMIN)


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
def test_the_lock_target_fence_wins_over_role_and_grants(role):
    grants = [p.value for p in LOCK_TARGET_FENCE]
    eff = effective_permissions(role, grants=grants, lock_target=True)
    assert not (eff & {p.value for p in LOCK_TARGET_FENCE})
    for p in LOCK_TARGET_FENCE:
        assert not grantable(role, p.value, lock_target=True)
    assert LOCK_TARGET_FENCE == {P.DISPATCH_AGENTS, P.APPROVE_AGENT_ACTIONS, P.VOICE_LAB}


def test_the_contract_document_is_schema_2():
    doc = permissions_document()
    assert doc["schema"] == 2
    assert doc["roles"] == ["basic", "user", "admin"]
    assert "anonymous" not in doc["principal_kinds"]
    assert set(doc["role_defaults"]) == set(doc["role_grantable"]) == set(doc["role_never"]) == \
        {"admin", "user", "basic"}
    assert doc["lock_target_fence"] == sorted(p.value for p in LOCK_TARGET_FENCE)
    assert "restricted_never" not in doc and "role_permissions" not in doc
