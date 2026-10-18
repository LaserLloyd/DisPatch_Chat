"""Permissions, roles and machine scopes - the single access-control vocabulary.

Replaces the binary Locked/Unlocked ("Safe Mode"/"operator") model. Every HTTP
route and WebSocket action declares exactly one :class:`Requirement` in
``app/policy.py``; the policy engine answers "may this principal do that?" from
the tables below and nowhere else. The client never decides access.

Naming history (old -> new) is recorded in CHANGELOG.md and docs/security.md:
decoy / Safe Mode / locked device / limited tier -> **Restricted**;
Unlocked / operator / full session -> **Owner** (or **Admin**).
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    """A named capability. Extensible: add members, never rename or repurpose one."""

    USE_CHAT = "use_chat"
    USE_VOICE = "use_voice"
    USE_IMAGES = "use_images"
    USE_TOOLS = "use_tools"                  # also parametrised: use_tools:<tool_id>
    USE_NOTIFICATIONS = "use_notifications"  # Web Push on a remembered device
    MANAGE_HISTORY = "manage_history"        # rename / pin / archive / delete threads and messages
    DISPATCH_AGENTS = "dispatch_agents"      # a turn that runs an agent on the host
    APPROVE_AGENT_ACTIONS = "approve_agent_actions"
    MANAGE_AGENTS = "manage_agents"
    MANAGE_CONNECTIONS = "manage_connections"
    MANAGE_CHARACTERS = "manage_characters"
    MANAGE_PLUGINS = "manage_plugins"
    MANAGE_USERS = "manage_users"
    MANAGE_SETTINGS = "manage_settings"
    VIEW_ACTIVITY = "view_activity"
    ADMIN_SYSTEM = "admin_system"            # host control: code execution, wipe, export, logs


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    RESTRICTED = "restricted"


#: Lowest to highest privilege. ``rank`` comparisons decide "may A manage B".
ROLE_ORDER: tuple[Role, ...] = (Role.RESTRICTED, Role.MEMBER, Role.ADMIN, Role.OWNER)

_ALL = frozenset(Permission)

#: The role -> permission matrix. A frozen contract: ``docs/contracts/permissions.json``
#: is generated from this and a drift test fails when they disagree.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    # Everything. Ownership transfer / owner deletion are owner-only checks in the
    # People API (they are not permissions: there is exactly one owner role).
    Role.OWNER: _ALL,
    # Everything except host control. multi-user.md section 0: a host-control surface
    # (the coding harness, wipe, full export, log tail) is Owner, not Admin.
    Role.ADMIN: _ALL - {Permission.ADMIN_SYSTEM},
    # Chat, voice, images, tools the owner has enabled, push. No agent dispatch and no
    # configuration; an Owner/Admin can grant dispatch_agents per profile.
    Role.MEMBER: frozenset({
        Permission.USE_CHAT, Permission.USE_VOICE, Permission.USE_IMAGES,
        Permission.USE_TOOLS, Permission.USE_NOTIFICATIONS, Permission.MANAGE_HISTORY,
    }),
    # Child / guest. Chat with audience=restricted|all characters, nothing else.
    # Tools only through the explicit per-tool grant use_tools:<id> (a tool the owner
    # flagged `safe: true`), never the blanket permission.
    Role.RESTRICTED: frozenset({Permission.USE_CHAT, Permission.USE_VOICE}),
}

#: What a per-profile grant may NEVER give a Restricted profile, whatever an admin
#: types into the People UI or the API. (Defence in depth for "no tools, no agents,
#: no settings, no admin, no file access".)
RESTRICTED_NEVER: frozenset[Permission] = frozenset({
    Permission.DISPATCH_AGENTS, Permission.APPROVE_AGENT_ACTIONS, Permission.MANAGE_AGENTS,
    Permission.MANAGE_CONNECTIONS, Permission.MANAGE_CHARACTERS, Permission.MANAGE_PLUGINS,
    Permission.MANAGE_USERS, Permission.MANAGE_SETTINGS, Permission.VIEW_ACTIVITY,
    Permission.ADMIN_SYSTEM, Permission.USE_TOOLS, Permission.MANAGE_HISTORY,
    Permission.USE_NOTIFICATIONS,
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


def role_rank(role: Role | str) -> int:
    return ROLE_ORDER.index(Role(role))


class MachineScope(StrEnum):
    """What a *machine* principal (an agent, a cron, the image rig) may do.

    A machine is identified by a credential - the install's ``api_token`` - or, while
    ``machine_loopback_trust`` is on (the default for upgraded installs), by being an
    on-box process. Either way it receives these named scopes, never a role: it is not
    a person and is never Owner/Admin.
    """

    INJECT = "machine:inject"                  # POST /api/inject
    DAILY = "machine:daily"                    # POST /api/daily
    THREADS = "machine:threads"                # list/read/manage threads, post a message
    MESSAGES = "machine:messages"              # delete a message
    BOTS_READ = "machine:bots_read"            # GET /api/bots roster discovery
    FILES_LIST = "machine:files_list"          # GET /api/files (metadata only)
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
#: is a program's. Nothing maps one to the other.
PRINCIPAL_KINDS: tuple[str, ...] = ("profile", "anonymous", "machine", "open")


def permissions_document() -> dict:
    """The JSON-able contract written to ``docs/contracts/permissions.json``."""
    return {
        "schema": 1,
        "permissions": [p.value for p in Permission],
        "parametrised": [f"{TOOL_PREFIX}<tool_id>"],
        "roles": [r.value for r in ROLE_ORDER],
        "role_permissions": {
            r.value: sorted(p.value for p in ROLE_PERMISSIONS[r]) for r in ROLE_ORDER},
        "restricted_never": sorted(p.value for p in RESTRICTED_NEVER),
        "machine_scopes": sorted(s.value for s in MachineScope),
        "machine_default_scopes": sorted(s.value for s in MACHINE_DEFAULT_SCOPES),
        "naming": {
            "decoy": "restricted", "Safe Mode": "restricted", "locked device": "restricted",
            "limited tier": "restricted", "Unlocked": "owner", "operator": "owner",
            "full session": "owner",
        },
    }
