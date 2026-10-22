"""Permissions, roles and machine scopes - the single access-control vocabulary.

Every HTTP route and WebSocket action declares exactly one :class:`Requirement` in
``app/route_policy.py``; the policy engine answers "may this principal do that?" from the
tables below and nowhere else. The client never decides access.

4.0 (chat-first profiles, BUILD-PLAN-v2 §1.1): three roles.

* **Admin** runs the install and holds every permission; it takes no overrides. The FIRST
  Admin is *protected* (it cannot be demoted, disabled or deleted). Protection is a fact
  about one profile, not a role: ``accounts.Profile.protected``.
* **User**: chat, voice, lookups, own bots, own files, images and voice cloning; agent bots
  only when an Admin switches them on.
* **Basic**: chat, voice, lookups and the household bots an Admin picked; images and voice
  cloning only when an Admin switches them on.

One table, three columns per role (SPEC section 5): :data:`ROLE_DEFAULTS` (on),
:data:`ROLE_GRANTABLE` (off, an Admin may switch it on) and :data:`ROLE_NEVER` (the ceiling:
no grant reaches it). The three are disjoint and together cover every :class:`Permission`,
for every role; ``tests/test_permissions_table.py`` checks them cell by cell.

Stored role strings: the ``profiles.role`` column still holds the 3.x words
(``owner/admin/member/restricted``) - no table rebuild in 4.0. Only ``app/accounts.py`` maps
them (``ROLE_OF_STORED``); :data:`LEGACY_ROLE` here is the INPUT vocabulary that old API
callers, old tests and the migration may still send, normalised by :func:`normalise_role`.

Naming history (old -> new): Owner -> the protected Admin; Admin -> Admin; Member -> User;
Restricted (was Safe Mode / decoy / locked device) -> Basic.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Permission(StrEnum):
    """A named capability. Extensible: add members, never rename or repurpose one."""

    USE_CHAT = "use_chat"
    USE_VOICE = "use_voice"                  # voice chat and the built-in voices
    USE_IMAGES = "use_images"                # Image Studio, pictures in chat
    USE_TOOLS = "use_tools"                  # tool panels; also parametrised: use_tools:<tool_id>
    USE_NOTIFICATIONS = "use_notifications"  # Web Push on a remembered device
    MANAGE_HISTORY = "manage_history"        # rename / pin / archive / delete / export own chats; Reset
    DISPATCH_AGENTS = "dispatch_agents"      # a turn that runs an agent on the host
    APPROVE_AGENT_ACTIONS = "approve_agent_actions"
    MANAGE_AGENTS = "manage_agents"
    MANAGE_CONNECTIONS = "manage_connections"
    MANAGE_CHARACTERS = "manage_characters"  # household bots (Settings -> Characters)
    MANAGE_PLUGINS = "manage_plugins"
    MANAGE_USERS = "manage_users"
    MANAGE_SETTINGS = "manage_settings"
    VIEW_ACTIVITY = "view_activity"
    ADMIN_SYSTEM = "admin_system"            # host control: code execution, wipe, export, logs
    # 4.0
    ATTACH_FILES = "attach_files"            # attach files or photos in a chat
    USE_LOOKUPS = "use_lookups"              # live lookups (web, news, fetch, weather, ...)
    FILES_UPLOAD = "files_upload"            # File Server: upload
    FILES_READ = "files_read"                # File Server: list and download OWN files
    VOICE_LAB = "voice_lab"                  # voice cloning / Voice Lab (own clones)
    CREATE_CHARACTERS = "create_characters"  # own bots, AI build, copy household bots
    CHOOSE_BACKEND = "choose_backend"        # pick a personal bot's backend (allowed connections)


class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"
    BASIC = "basic"


#: Lowest to highest privilege. ``rank`` comparisons decide "may A manage B".
ROLE_ORDER: tuple[Role, ...] = (Role.BASIC, Role.USER, Role.ADMIN)

#: The 3.x role words, as old callers, old tests and the migration may still send them.
#: Normalise with :func:`normalise_role`; never store or emit these (accounts.py owns the
#: stored column).
LEGACY_ROLE: dict[str, Role] = {
    "owner": Role.ADMIN,
    "admin": Role.ADMIN,
    "member": Role.USER,
    "restricted": Role.BASIC,
}

_ALL = frozenset(Permission)
P = Permission

#: SPEC section 5, "on": what a role holds with no overrides.
ROLE_DEFAULTS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: _ALL,
    Role.USER: frozenset({
        P.USE_CHAT, P.USE_VOICE, P.ATTACH_FILES, P.USE_LOOKUPS, P.MANAGE_HISTORY,
        P.USE_NOTIFICATIONS, P.FILES_UPLOAD, P.FILES_READ, P.USE_IMAGES, P.VOICE_LAB,
        P.CREATE_CHARACTERS, P.CHOOSE_BACKEND,
    }),
    Role.BASIC: frozenset({
        P.USE_CHAT, P.USE_VOICE, P.ATTACH_FILES, P.USE_LOOKUPS, P.MANAGE_HISTORY,
        P.USE_NOTIFICATIONS, P.FILES_UPLOAD,
    }),
}

#: SPEC section 5, "off, switchable": what an Admin may grant per profile. Agent bots carry a
#: warning in the UI (they can run commands on the host and read anyone's chats).
ROLE_GRANTABLE: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(),                 # Admin = everything, no overrides
    Role.USER: frozenset({P.DISPATCH_AGENTS, P.APPROVE_AGENT_ACTIONS}),
    Role.BASIC: frozenset({P.USE_IMAGES, P.VOICE_LAB, P.DISPATCH_AGENTS, P.APPROVE_AGENT_ACTIONS}),
}

#: SPEC section 5, "never" / "-": the ceiling. No grant reaches these; ``use_tools`` (and so
#: every ``use_tools:<id>`` panel: Benchmark, StudioForge, Mail, Clients, Job Board) is
#: Admin-only.
ROLE_NEVER: dict[Role, frozenset[Permission]] = {
    role: _ALL - ROLE_DEFAULTS[role] - ROLE_GRANTABLE[role] for role in ROLE_ORDER
}

#: Never effective for the profile that is the current lock target (the shared default
#: profile everyone lands on without signing in), whatever its role or grants say - and
#: never grantable to it (the People API answers 422).
LOCK_TARGET_FENCE: frozenset[Permission] = frozenset({
    P.DISPATCH_AGENTS, P.APPROVE_AGENT_ACTIONS, P.VOICE_LAB,
})

TOOL_PREFIX = "use_tools:"


def tool_permission(tool_id: str) -> str:
    """The parametrised permission naming one tool, e.g. ``use_tools:weather``."""
    return f"{TOOL_PREFIX}{tool_id}"


def is_valid_permission(value: str) -> bool:
    """True for a Permission value or a well-formed ``use_tools:<id>``."""
    if value in {p.value for p in Permission}:
        return True
    return value.startswith(TOOL_PREFIX) and len(value) > len(TOOL_PREFIX)


def normalise_role(value: Role | str | None) -> Role:
    """A :class:`Role` from a Role, a 4.0 word (``admin/user/basic``) or a 3.x word
    (:data:`LEGACY_ROLE`). Case and surrounding space are ignored. Anything else -> ValueError."""
    if isinstance(value, Role):
        return value
    v = str(value or "").strip().lower()
    try:
        return Role(v)
    except ValueError:
        pass
    if v in LEGACY_ROLE:
        return LEGACY_ROLE[v]
    raise ValueError(f"unknown role {value!r}")


def role_rank(role: Role | str) -> int:
    return ROLE_ORDER.index(normalise_role(role))


def _base(p: str) -> str:
    """The table entry a permission value is judged by (``use_tools:<id>`` -> ``use_tools``)."""
    return Permission.USE_TOOLS.value if p.startswith(TOOL_PREFIX) else p


def grantable(role: Role | str, permission: str, *, lock_target: bool = False) -> bool:
    """May an Admin switch ``permission`` on for a profile of ``role``? (For the People API's
    422.) Admins take no grants; the lock-target fence wins over the table."""
    r = normalise_role(role)
    if not is_valid_permission(permission):
        return False
    if lock_target and permission in {p.value for p in LOCK_TARGET_FENCE}:
        return False
    base = _base(permission)
    return base in {p.value for p in ROLE_GRANTABLE[r]} and not permission.startswith(TOOL_PREFIX)


def effective_permissions(role: Role | str, grants: Iterable[str] = (), denies: Iterable[str] = (),
                          *, lock_target: bool = False) -> frozenset[str]:
    """Pure: what a profile of ``role`` with these overrides may do.

    ``ROLE_DEFAULTS[role]`` + the ``grants`` that are in ``ROLE_GRANTABLE[role]`` - ``denies``.
    A grant outside the grantable column is dropped (defence in depth: the People API
    refuses it with 422 before it is ever stored). An Admin holds everything and takes no
    overrides. ``lock_target`` (this profile is the current lock target) removes
    :data:`LOCK_TARGET_FENCE` last, whatever the role (``policy.effective_permissions`` works
    it out from security.yaml)."""
    r = normalise_role(role)
    if r is Role.ADMIN:
        out = {p.value for p in _ALL}
    else:
        out = {p.value for p in ROLE_DEFAULTS[r]}
        allowed = {p.value for p in ROLE_GRANTABLE[r]}
        for g in grants or ():
            g = str(g)
            if g in allowed:
                out.add(g)
        for d in denies or ():
            out.discard(str(d))
    if lock_target:
        out -= {p.value for p in LOCK_TARGET_FENCE}
    return frozenset(out)


class MachineScope(StrEnum):
    """What a *machine* principal (an agent, a cron, the image rig) may do.

    A machine is identified by a credential - the install's ``api_token`` - or, while
    ``machine_loopback_trust`` is on, by being an on-box process. Either way it receives
    these named scopes, never a role: it is not a person and is never an Admin. 4.0: a
    machine WRITES (inject) into any existing thread and READS only the protected Admin's.
    """

    INJECT = "machine:inject"                  # POST /api/inject
    DAILY = "machine:daily"                    # POST /api/daily
    THREADS = "machine:threads"                # list/read/manage threads, post a message
    MESSAGES = "machine:messages"              # delete a message
    BOTS_READ = "machine:bots_read"            # GET /api/bots roster discovery
    FILES_LIST = "machine:files_list"          # GET /api/files (metadata only)
    UPLOAD = "machine:upload"                  # POST /api/upload (a document an agent hands back)
    REACTIONS = "machine:reactions"            # fire / pool / prompts / settings
    AVATARS = "machine:avatars"                # one bot's / one thread's avatar
    AVATAR_POOL = "machine:avatar_pool"
    IMAGE_JOBS = "machine:image_jobs"          # create / read an image job
    IMAGE_JOB_CALLBACK = "machine:image_job_callback"   # the rig's per-job-token poke
    ADVISOR = "machine:advisor"                # status + nightly digest
    APPS = "machine:apps"                      # a mounted app's API
    EXTENSIONS_READ = "machine:extensions_read"
    GUIDE_SEED = "machine:guide_seed"
    OPENAPI = "machine:openapi"
    HEALTH_DETAIL = "machine:health_detail"    # GET /api/health with the full body


#: The scopes an api_token machine holds unless ``machine_scopes`` in security.yaml
#: narrows them. Equal to today's machine-inbound surface (nothing widened).
MACHINE_DEFAULT_SCOPES: frozenset[MachineScope] = frozenset(MachineScope)

#: Kept apart from roles on purpose: ``Role`` is a person's standing, ``MachineScope``
#: is a program's. Nothing maps one to the other. 4.0: no ``anonymous`` kind - an unsigned
#: device is either the lock-target default profile (a ``profile`` principal) or nobody.
PRINCIPAL_KINDS: tuple[str, ...] = ("profile", "machine", "open")


def permissions_document() -> dict:
    """The JSON-able contract written to ``docs/contracts/permissions.json``."""
    def col(table: dict[Role, frozenset[Permission]]) -> dict[str, list[str]]:
        return {r.value: sorted(p.value for p in table[r]) for r in ROLE_ORDER}
    return {
        "schema": 2,
        "permissions": [p.value for p in Permission],
        "parametrised": [f"{TOOL_PREFIX}<tool_id>"],
        "roles": [r.value for r in ROLE_ORDER],
        "role_defaults": col(ROLE_DEFAULTS),
        "role_grantable": col(ROLE_GRANTABLE),
        "role_never": col(ROLE_NEVER),
        "lock_target_fence": sorted(p.value for p in LOCK_TARGET_FENCE),
        "legacy_roles": {k: v.value for k, v in LEGACY_ROLE.items()},
        "protected": "The first Admin is protected: it cannot be demoted, disabled or deleted, "
                     "and only it may change its own credentials.",
        "principal_kinds": list(PRINCIPAL_KINDS),
        "machine_scopes": sorted(s.value for s in MachineScope),
        "machine_default_scopes": sorted(s.value for s in MACHINE_DEFAULT_SCOPES),
        "naming": {
            "Owner": "the protected Admin", "Member": "user", "Restricted": "basic",
            "decoy": "basic", "Safe Mode": "basic", "locked device": "basic",
            "operator": "admin",
        },
    }
