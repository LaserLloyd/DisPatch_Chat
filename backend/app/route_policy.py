"""Every route and WebSocket action, with the permission it declares.

This table IS the access-control policy. ``tests/test_route_table.py`` walks the real
FastAPI app and fails the build when a route is missing here, when an entry here
names a route that no longer exists, or when an entry is malformed - so a new
endpoint cannot ship without somebody deciding who may call it. The auth gate
(``main.auth_gate``) enforces the entry before the handler runs, and refuses a route
with no entry (fail closed).

Reading an entry::

    ("POST", "/api/bots", R(MANAGE_CHARACTERS))
        needs the manage_characters permission (Owner, Admin; or a profile granted it)
    ("GET",  "/api/bots", R(USE_CHAT, scoped=True, machine=[BOTS_READ]))
        any role that can chat; a Restricted caller is let through but the handler
        narrows the list to audience=restricted|all characters. A machine holding the
        machine:bots_read scope may call it too.
    ("GET",  "/", public("the app shell"))
        the explicit PUBLIC marker - no credential, and the note says why.

The permission names are the vocabulary in ``contracts/permissions.py``; what each
role holds is the matrix there.
"""

from __future__ import annotations

import re

from .contracts.permissions import MachineScope as M
from .contracts.permissions import Permission as P
from .policy import R as _R
from .policy import Requirement, public

#: (methods, path template exactly as FastAPI registers it, requirement)
ROUTES: list[tuple[tuple[str, ...], str, Requirement]] = []


def _r(methods: str | tuple[str, ...], path: str, req: Requirement) -> None:
    ms = (methods,) if isinstance(methods, str) else methods
    ROUTES.append((ms, path, req))


# --------------------------------------------------------------------------- #
# Public on purpose
# --------------------------------------------------------------------------- #
_r("GET", "/", public("the app shell; carries no data"))
_r("GET", "/api/health", Requirement(public=True, machine=(M.HEALTH_DETAIL.value,),
   note="liveness for anyone; the handler adds detail only for Owner/Admin or a machine holding machine:health_detail"))
_r("GET", "/api/quick-actions", public("static list of built-in prompt shortcuts"))
_r("GET", "/api/auth/status", public("the lock screen needs it; detail is added only for a signed-in caller"))
_r("GET", "/api/auth/me", public("who am I, what may I do - answers Restricted too"))
_r("POST", "/api/auth/unlock", public("credential check; throttled per device, profile and address"))
_r("POST", "/api/auth/login", public("credential check; throttled per device, profile and address"))
_r("POST", "/api/auth/switch", public("quick profile switch; stepping up needs the target's credential"))
_r("POST", "/api/auth/lock", public("dropping your own session needs no permission"))
_r("POST", "/api/auth/recover", public("recovery-code check; throttled"))
_r("POST", "/api/auth/setup", public("first-run owner creation (loopback or setup token) / PIN change (session + current PIN) - decided in the handler"))
_r("POST", "/api/image-jobs/{job_id}/callback", Requirement(
    public=True, machine=(M.IMAGE_JOB_CALLBACK.value,),
    note="the image rig's per-job callback token is the credential; the body is discarded"))
_r("GET", "/local/view/{ticket}/{rel:path}", public("capability URL: the random, expiring, client-bound ticket is the credential"))
_r("GET", "/tools/_t/{ticket}/{rel:path}", public("capability URL: the random, expiring ticket is the credential"))

# --------------------------------------------------------------------------- #
# Sign-in state
# --------------------------------------------------------------------------- #
_r("POST", "/api/auth/remember-config", _R(P.MANAGE_SETTINGS))
_r("POST", "/api/auth/forget-devices", _R(P.MANAGE_SETTINGS))
_r("POST", "/api/auth/password", _R(P.MANAGE_SETTINGS, note="YOUR password: only the Owner and Admins have one"))
_r("POST", "/api/auth/pin", _R(P.USE_CHAT, note="YOUR OWN PIN (a Member or an Admin; current credential required, throttled); the Owner's is the install PIN"))
_r("POST", "/api/auth/api-token", _R(P.MANAGE_SETTINGS, note="the Owner and Admins (checked in the handler); shown once"))
_r("GET", "/api/auth/api-token", _R(P.MANAGE_SETTINGS, note="reports whether a token exists; never the token"))

# People & profiles
_r("GET", "/api/people", _R(P.MANAGE_USERS))
_r("POST", "/api/people", _R(P.MANAGE_USERS))
_r("PATCH", "/api/people/{profile_id}", _R(P.MANAGE_USERS))
_r("DELETE", "/api/people/{profile_id}", _R(P.MANAGE_USERS))
_r("POST", "/api/people/{profile_id}/pin", _R(P.MANAGE_USERS))
_r("DELETE", "/api/people/{profile_id}/pin", _R(P.MANAGE_USERS))
_r("POST", "/api/people/{profile_id}/password", _R(P.MANAGE_USERS))
_r("POST", "/api/people/{profile_id}/sign-out", _R(P.MANAGE_USERS))
_r("GET", "/api/people/devices", _R(P.MANAGE_USERS))
_r("DELETE", "/api/people/devices/{device_id}", _R(P.MANAGE_USERS))
_r("GET", "/api/people/activity", _R(P.VIEW_ACTIVITY))
_r("GET", "/api/people/audit", _R(P.VIEW_ACTIVITY))
_r("GET", "/api/me/activity", _R(P.USE_CHAT, note="a profile's own summary - the disclosure the Restricted UI shows"))

# Web Push
_r(("POST", "DELETE"), "/api/push/subscribe", _R(P.USE_NOTIFICATIONS))
_r("GET", "/api/push/vapid", _R(P.USE_NOTIFICATIONS))

# --------------------------------------------------------------------------- #
# Chat (Restricted can do these, narrowed to the characters it may see)
# --------------------------------------------------------------------------- #
_r("GET", "/api/bots", _R(P.USE_CHAT, scoped=True, machine=[M.BOTS_READ]))
_r("GET", "/api/threads", _R(P.USE_CHAT, scoped=True, machine=[M.THREADS]))
_r("POST", "/api/threads", _R(P.USE_CHAT, machine=[M.THREADS], note="daily thread quota for Restricted"))
_r("GET", "/api/threads/{thread_id}", _R(P.USE_CHAT, scoped=True, machine=[M.THREADS]))
_r("GET", "/api/threads/{thread_id}/messages", _R(P.USE_CHAT, scoped=True, machine=[M.THREADS]))
_r("POST", "/api/threads/{thread_id}/messages", _R(P.USE_CHAT, machine=[M.THREADS]))
_r("POST", "/api/threads/{thread_id}/read", _R(P.USE_CHAT, machine=[M.THREADS]))
_r("GET", "/api/unread", _R(P.USE_CHAT, scoped=True, machine=[M.THREADS]))
_r("POST", "/api/messages/{message_id}/feedback", _R(P.USE_CHAT, note="daily feedback quota for Restricted"))
_r("GET", "/api/messages/{message_id}/mood-face", _R(P.USE_CHAT, scoped=True))
_r("POST", "/api/upload", _R(P.USE_CHAT, machine=[M.UPLOAD], note="daily byte quota for Restricted"))
_r("POST", "/api/drop", _R(P.USE_CHAT, note="daily byte quota for Restricted"))
_r("GET", "/api/threads/{thread_id}/avatar", _R(P.USE_CHAT, scoped=True))
_r("GET", "/api/bots/{bot_id}/avatar", _R(P.USE_CHAT, scoped=True, machine=[M.AVATARS]))
_r("GET", "/api/bots/{bot_id}/avatar/thumb", _R(P.USE_CHAT, scoped=True, machine=[M.AVATARS]))
_r("GET", "/api/bots/{bot_id}/avatar/history", _R(P.USE_CHAT, scoped=True, machine=[M.AVATARS]))
_r("GET", "/api/bots/{bot_id}/avatar/history/{snapshot_id}", _R(P.USE_CHAT, scoped=True, machine=[M.AVATARS]))
_r("GET", "/api/reactions", _R(P.USE_CHAT, scoped=True, machine=[M.REACTIONS]))
_r("GET", "/api/reactions/{rid}/image", _R(P.USE_CHAT, scoped=True))
_r("GET", "/api/extensions", _R(P.USE_CHAT, machine=[M.EXTENSIONS_READ], note="add-on mount rows only"))
_r("GET", "/ext/{ext_id}/{rel:path}", _R(P.USE_VOICE, note="an add-on's public frontend files"))

# History housekeeping: rename / pin / archive / delete / edit / abort
_r("PATCH", "/api/threads/{thread_id}", _R(P.MANAGE_HISTORY, machine=[M.THREADS], note="model/thinking fields additionally need a signed-in session"))
_r("DELETE", "/api/threads/{thread_id}", _R(P.MANAGE_HISTORY, machine=[M.THREADS]))
_r("POST", "/api/threads/{thread_id}/abort", _R(P.MANAGE_HISTORY))
_r("DELETE", "/api/messages/{message_id}", _R(P.MANAGE_HISTORY, machine=[M.MESSAGES]))
_r("PATCH", "/api/messages/{message_id}", _R(P.MANAGE_HISTORY))
_r("PATCH", "/api/messages/{message_id}/checklist", _R(P.MANAGE_HISTORY))

# Images and media
_r("GET", "/api/bots/{bot_id}/avatar/full", _R(P.USE_IMAGES, machine=[M.AVATARS]))
_r("GET", "/api/media", _R(P.USE_IMAGES))
_r("POST", "/api/image-jobs", _R(P.USE_IMAGES, machine=[M.IMAGE_JOBS]))
_r("GET", "/api/image-jobs/{job_id}", _R(P.USE_IMAGES, machine=[M.IMAGE_JOBS]))

# File server (upload-only drop box, plus listing for on-box agents)
_r("GET", "/api/files", _R(P.USE_TOOLS, machine=[M.FILES_LIST]))
_r("POST", "/api/files", _R(P.USE_TOOLS))
_r("GET", "/api/files/{file_id}/download", _R(P.USE_TOOLS))
_r("GET", "/api/files/{file_id}/raw", _R(P.USE_TOOLS))
_r("DELETE", "/api/files/{file_id}", _R(P.USE_TOOLS))
_r("POST", "/api/files/wipe", _R(P.ADMIN_SYSTEM, note="deletes the whole File Server"))

# Tools, apps, plugins
_r("GET", "/api/tools", _R(P.USE_TOOLS, scoped=True, note="Restricted sees only tools flagged safe"))
_r("GET", "/api/tools/{tool_id}/status", _R(P.USE_TOOLS, scoped=True))
_r("PUT", "/api/tools", _R(P.MANAGE_PLUGINS))
_r("POST", "/api/tools/{tool_id}/refresh", _R(P.MANAGE_PLUGINS))
_r("GET", "/tools/{tool_id}", _R(P.USE_TOOLS, scoped=True))
_r("GET", "/tools/{tool_id}/{rel:path}", _R(P.USE_TOOLS, scoped=True))
_r("GET", "/apps/{app_id}", _R(P.USE_TOOLS, scoped=True, note="per-app permission and safe flag from its manifest"))
_r("GET", "/apps/{app_id}/{rel:path}", _R(P.USE_TOOLS, scoped=True, note="per-app permission and safe flag from its manifest"))
_r("GET", "/api/plugins", _R(P.MANAGE_PLUGINS))
_r("GET", "/api/plugins/catalog", _R(P.MANAGE_PLUGINS))
_r("PUT", "/api/plugins/extensions/{ext_id}", _R(P.MANAGE_PLUGINS))
_r("PUT", "/api/plugins/livetools", _R(P.MANAGE_PLUGINS))
_r("GET", "/api/voice/providers", _R(P.MANAGE_PLUGINS, note=(
    "the voice providers and the installed provider plugins. With the add-on running this is ITS route "
    "(health, capabilities, settings of each provider); without it the host answers the plugin list "
    "alone. Listing needs manage_plugins; using a provider is use_voice (/ext, /ws/voice)")))

# --------------------------------------------------------------------------- #
# Characters
# --------------------------------------------------------------------------- #
_r("GET", "/api/bots/all", _R(P.MANAGE_CHARACTERS))
_r("GET", "/api/bots/personas", _R(P.MANAGE_CHARACTERS))
_r("GET", "/api/bots/{bot_id}/models", _R(P.MANAGE_CHARACTERS))
_r("POST", "/api/bots", _R(P.MANAGE_CHARACTERS))
_r("PATCH", "/api/bots/{bot_id}", _R(P.MANAGE_CHARACTERS))
_r("DELETE", "/api/bots/{bot_id}", _R(P.MANAGE_CHARACTERS))
_r("PUT", "/api/bots/order", _R(P.MANAGE_CHARACTERS))
_r("POST", "/api/bots/{bot_id}/avatar", _R(P.MANAGE_CHARACTERS, machine=[M.AVATARS]))
_r("POST", "/api/bots/{bot_id}/avatar/restore", _R(P.MANAGE_CHARACTERS, machine=[M.AVATARS]))
_r("POST", "/api/threads/{thread_id}/avatar", _R(P.MANAGE_CHARACTERS, machine=[M.AVATARS]))
_r("GET", "/api/avatar-pool", _R(P.MANAGE_CHARACTERS, machine=[M.AVATAR_POOL]))
_r("PUT", "/api/avatar-pool/{bot_id}", _R(P.MANAGE_CHARACTERS, machine=[M.AVATAR_POOL]))
_r("GET", "/api/avatar-pool/{bot_id}/prompts", _R(P.MANAGE_CHARACTERS, machine=[M.AVATAR_POOL]))
_r("PUT", "/api/avatar-pool/{bot_id}/prompts", _R(P.MANAGE_CHARACTERS, machine=[M.AVATAR_POOL]))
_r("POST", "/api/avatar-pool/{bot_id}/refill", _R(P.MANAGE_CHARACTERS, machine=[M.AVATAR_POOL]))
_r("GET", "/api/reactions/pool", _R(P.MANAGE_CHARACTERS, machine=[M.REACTIONS]))
_r("PUT", "/api/reactions/pool", _R(P.MANAGE_CHARACTERS, machine=[M.REACTIONS]))
_r("GET", "/api/reactions/prompts", _R(P.MANAGE_CHARACTERS, machine=[M.REACTIONS]))
_r("PUT", "/api/reactions/prompts", _R(P.MANAGE_CHARACTERS, machine=[M.REACTIONS]))
_r("POST", "/api/reactions/pool/refill", _R(P.MANAGE_CHARACTERS, machine=[M.REACTIONS]))
_r("PUT", "/api/reactions/settings", _R(P.MANAGE_CHARACTERS, machine=[M.REACTIONS]))
_r("POST", "/api/reactions/generate", _R(P.MANAGE_CHARACTERS, machine=[M.REACTIONS]))
_r("POST", "/api/reactions/fire", Requirement(machine=(M.REACTIONS.value,), note="agents only",
                                              denied_detail="Only agents may fire reactions"))
_r("POST", "/api/reactions", _R(P.MANAGE_CHARACTERS))
_r("PATCH", "/api/reactions/{rid}", _R(P.MANAGE_CHARACTERS))
_r("DELETE", "/api/reactions/{rid}", _R(P.MANAGE_CHARACTERS))
_r("POST", "/api/reactions/reseed", _R(P.MANAGE_CHARACTERS))

# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
for _path, _methods in (
    ("/api/llm/providers", "GET"), ("/api/llm/test", "POST"), ("/api/llm/image", ("GET", "PUT")),
    ("/api/llm/image/test", "POST"), ("/api/llm/quicksetup", ("GET", "POST")),
    ("/api/llm/connect", "POST"),
):
    _r(_methods, _path, _R(P.MANAGE_CONNECTIONS))
_r("GET", "/api/studioforge/status", _R(P.MANAGE_CONNECTIONS))

# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
_r("POST", "/api/inject", _R(P.MANAGE_AGENTS, machine=[M.INJECT]))
_r("POST", "/api/daily", _R(P.MANAGE_AGENTS, machine=[M.DAILY]))
_r("GET", "/api/advisor/status", _R(P.MANAGE_AGENTS, machine=[M.ADVISOR]))
_r("POST", "/api/advisor/digest", _R(P.MANAGE_AGENTS, machine=[M.ADVISOR]))
_r("POST", "/api/advisor/requests/{rid}/send", _R(P.APPROVE_AGENT_ACTIONS, note="the human Send button; a machine may never press it"))
_r("POST", "/api/advisor/requests/{rid}/dismiss", _R(P.APPROVE_AGENT_ACTIONS))
_r("GET", "/api/openclaw/sessions", _R(P.MANAGE_AGENTS))
_r("GET", "/api/openclaw/transcript", _R(P.MANAGE_AGENTS))
_r("POST", "/api/openclaw/import", _R(P.MANAGE_AGENTS))

# Agents and tasks (W11b). dispatch_agents is Admin-only unless an Admin grants it to a User
# or Basic profile (4.0 ROLE_GRANTABLE; never the lock-target profile), and none of these is
# `scoped`, so every one answers 403 to anyone without it - listing included. A task or agent you may not see is a 404, decided in the handler.
for _methods, _path in (
    ("GET", "/api/agents"), ("POST", "/api/agents"), ("PATCH", "/api/agents/{agent_id}"),
    ("DELETE", "/api/agents/{agent_id}"), ("GET", "/api/agents/backends"),
    ("GET", "/api/agents/credentials/claude"), ("PUT", "/api/agents/credentials/claude"),
    ("DELETE", "/api/agents/credentials/claude"), ("GET", "/api/agents/secrets"),
    ("PUT", "/api/agents/secrets/{name}"), ("DELETE", "/api/agents/secrets/{name}"),
    ("GET", "/api/agents/settings"), ("PUT", "/api/agents/settings"),
    ("GET", "/api/agents/profile-budgets"), ("PUT", "/api/agents/profile-budgets/{profile_id}"),
    ("GET", "/api/agents/usage"), ("POST", "/api/agents/retention/purge"),
):
    _r(_methods, _path, _R(P.MANAGE_AGENTS, note="define agents, credentials, budgets, retention (write-only secrets)"))
_r("GET", "/api/agents/available", _R(P.DISPATCH_AGENTS, note="only agents published to the caller's profile (an Owner/Admin sees all)"))
for _methods, _path in (
    ("POST", "/api/tasks"), ("GET", "/api/tasks"), ("GET", "/api/tasks/{task_id}"),
    ("GET", "/api/tasks/{task_id}/events"), ("POST", "/api/tasks/{task_id}/send"),
    ("POST", "/api/tasks/{task_id}/interrupt"), ("POST", "/api/tasks/{task_id}/resume"),
    ("POST", "/api/tasks/{task_id}/cancel"),
):
    _r(_methods, _path, _R(P.DISPATCH_AGENTS, note="own tasks; an Owner/Admin sees everyone's"))
_r("POST", "/api/tasks/{task_id}/release", _R(P.APPROVE_AGENT_ACTIONS, note="the human Send for a held handoff; a machine may never press it"))
_r("POST", "/api/tasks/{task_id}/dismiss", _R(P.APPROVE_AGENT_ACTIONS))
_r("POST", "/api/approvals/{request_id}", _R(P.APPROVE_AGENT_ACTIONS, note="allow or deny one tool call; destructive tools are asked every time"))

# --------------------------------------------------------------------------- #
# Settings / system / host control (Owner and Admins: admin_system)
# --------------------------------------------------------------------------- #
_r("GET", "/api/openapi.json", _R(P.MANAGE_SETTINGS, machine=[M.OPENAPI]))
_r("POST", "/api/guide/seed", _R(P.MANAGE_SETTINGS, machine=[M.GUIDE_SEED]))
_r("GET", "/api/search", _R(P.USE_CHAT, note="the caller's OWN conversations (4.0), every role"))
_r("GET", "/api/export", _R(P.MANAGE_HISTORY, note="the caller's OWN history (4.0); every profile's chats are only in the encrypted backup"))
_r("POST", "/api/recover/transcript", _R(P.ADMIN_SYSTEM))
for _path in ("/api/dashboard", "/api/dashboard/deep", "/api/dashboard/logs"):
    _r("GET", _path, _R(P.ADMIN_SYSTEM))
_r("GET", "/api/local/roots", _R(P.ADMIN_SYSTEM, note="reads arbitrary bytes off the host"))
_r("GET", "/api/local/stat", _R(P.ADMIN_SYSTEM))
_r("GET", "/api/local/ls", _R(P.ADMIN_SYSTEM))
_r("PUT", "/api/local/config", _R(P.ADMIN_SYSTEM))
_r("GET", "/local/file/{path:path}", _R(P.ADMIN_SYSTEM))
_r("GET", "/api/mail/status", _R(P.ADMIN_SYSTEM, note="its launch URL carries MailForge's one-time key"))
_r(("GET", "POST"), "/api/practice/{path:path}", _R(P.ADMIN_SYSTEM, note="same-origin proxy onto the practice box"))
_r(("GET", "PUT"), "/api/practice-links/{client_id}", _R(P.ADMIN_SYSTEM))
for _methods, _path in (
    ("GET", "/api/harness/status"), ("POST", "/api/harness/start"), ("POST", "/api/harness/stop"),
    ("POST", "/api/harness/restart"), ("GET", "/api/harness/models"), ("POST", "/api/harness/model"),
    ("GET", "/api/harness/jobs"), ("GET", "/api/harness/jobs/{job_id}"), ("POST", "/api/harness/jobs"),
    ("POST", "/api/harness/jobs/cancel"), ("GET", "/api/harness/sessions"),
    ("POST", "/api/harness/sessions"), ("GET", "/api/harness/sessions/{sid}"),
    ("POST", "/api/harness/sessions/{sid}/stop"), ("POST", "/api/harness/sessions/{sid}/dismiss"),
):
    _r(_methods, _path, _R(P.ADMIN_SYSTEM, note="code execution on the host"))

# --------------------------------------------------------------------------- #
# W5: settings store, connections, characters, packs, wizard, backups
# --------------------------------------------------------------------------- #
_r("GET", "/api/settings/schema", _R(P.MANAGE_SETTINGS))
_r("GET", "/api/settings", _R(P.MANAGE_SETTINGS))
_r("PATCH", "/api/settings", _R(P.MANAGE_SETTINGS))
for _path, _m in (
    ("/api/connections", ("GET", "POST")), ("/api/connections/types", "GET"),
    ("/api/connections/test", "POST"), ("/api/connections/discover", "POST"),
    ("/api/connections/adopt", "POST"),
    ("/api/connections/{connection_id}", ("GET", "PATCH", "DELETE")),
    ("/api/connections/{connection_id}/test", "POST"),
):
    _r(_m, _path, _R(P.MANAGE_CONNECTIONS))
_r("GET", "/api/characters", _R(P.USE_CHAT, scoped=True, machine=[M.BOTS_READ],
                                note="the characters this caller may talk to (audience and profile list)"))
for _path, _m in (
    ("/api/characters/all", "GET"), ("/api/characters/personas", "GET"), ("/api/characters/order", "PUT"),
    ("/api/characters", "POST"), ("/api/characters/{character_id}", ("GET", "PATCH", "DELETE")),
    ("/api/characters/{character_id}/delete-preview", "GET"),
    ("/api/starter-packs", "GET"), ("/api/starter-packs/{pack_id}/install", "POST"),
):
    _r(_m, _path, _R(P.MANAGE_CHARACTERS))
_r("GET", "/api/wizard", public("answers only whether a first-run wizard is needed until the caller holds manage_settings"))
_r("POST", "/api/wizard/detect", _R(P.MANAGE_CONNECTIONS))
_r("POST", "/api/wizard/connection", _R(P.MANAGE_CONNECTIONS))
_r("POST", "/api/wizard/family", _R(P.MANAGE_USERS))
_r("POST", "/api/wizard/network", _R(P.ADMIN_SYSTEM, note="network exposure is host control: the Owner's and the Admins'"))
for _path in ("/api/wizard/step/{step}", "/api/wizard/finish"):
    _r("POST", _path, _R(P.MANAGE_SETTINGS))
for _path, _m in (
    ("/api/backups", ("GET", "POST")), ("/api/backups/pending", ("GET", "DELETE")),
    ("/api/backups/{backup_id}/verify", "POST"), ("/api/backups/{backup_id}/restore", "POST"),
    ("/api/backups/{backup_id}", "DELETE"),
    ("/api/support-bundle", "POST"),
):
    _r(_m, _path, _R(P.ADMIN_SYSTEM))
_r("GET", "/api/users/{profile_id}/export", _R(P.MANAGE_USERS, note="the Owner and Admins may export anyone; the Owner's own export is the Owner's"))
_r("DELETE", "/api/users/{profile_id}", _R(P.MANAGE_USERS, note="same rank rules as /api/people"))
_r("GET", "/api/plugins/manifests", _R(P.MANAGE_PLUGINS))
_r("GET", "/api/settings/defaults", public("default theme / language / avatar style for a device that has not signed in; no secrets"))

# --------------------------------------------------------------------------- #
# 4.0 chat-first profiles (BUILD-PLAN-v2). One section per package, so wave packages
# add their routes here with ``_r(...)`` without touching another package's lines.
# A route taking a thread / message / bot / profile id must ALSO be listed in
# OWNER_SCOPED once WP-CORE has ported it (tests/test_route_table.py).
# --------------------------------------------------------------------------- #

# ---- WP-PROF: profile lifecycle (app/profiles_api.py): reset, my characters, export, copies
_r(("GET",), "/api/me/reset/preview", _R(P.MANAGE_HISTORY, note="counts only, for the caller's own profile"))
_r(("POST",), "/api/me/reset", _R(P.MANAGE_HISTORY, note="Reset to default for the caller's own profile; every role, the default profile included"))
_r(("GET", "POST"), "/api/me/characters", _R(P.CREATE_CHARACTERS, note="the caller's own bots"))
_r(("GET", "PATCH", "DELETE"), "/api/me/characters/{bot_id}", _R(P.CREATE_CHARACTERS, note="the caller's own bot; anyone else's is 404"))
_r(("POST",), "/api/me/characters/{bot_id}/avatar", _R(P.CREATE_CHARACTERS, note="the caller's own bot"))
_r(("GET",), "/api/me/export", _R(P.MANAGE_HISTORY, note="the caller's own chats and bot setup"))
_r(("POST",), "/api/characters/{bot_id}/clone", _R(P.CREATE_CHARACTERS, note="a household bot into the caller's own profile; agent bots refused"))
_r(("POST",), "/api/characters/{bot_id}/clone-to/{profile_id}", _R(P.MANAGE_USERS, note="an Admin copies setup into a profile; chats are never copied"))
_r(("POST",), "/api/people/{profile_id}/reset", _R(P.MANAGE_USERS, note="counts only; the protected Admin only by itself"))
_r(("GET",), "/api/people/{profile_id}/reset/preview", _R(P.MANAGE_USERS, note="counts only"))
_r(("GET",), "/api/people/{profile_id}/role/preview", _R(P.MANAGE_USERS, note="counts only"))
_r(("POST",), "/api/people/{profile_id}/role", _R(P.MANAGE_USERS, note="to Basic deletes the profile's bots and chats after a confirm token"))
_r(("PUT",), "/api/people/{profile_id}/household-bots", _R(P.MANAGE_USERS, note="which household bots a profile may use"))
_r(("PUT",), "/api/people/{profile_id}/backends", _R(P.MANAGE_USERS, note="which connections a profile's own bots may run on"))
_r(("GET",), "/api/people/{profile_id}/delete/preview", _R(P.MANAGE_USERS, note="counts only"))
_r(("POST",), "/api/people/{profile_id}/delete", _R(P.MANAGE_USERS, note="deletes the profile and its content after a confirm token"))

# ---- WP-AIB: AI build (app/bot_builder_api.py): POST /api/me/characters/draft
_r("POST", "/api/me/characters/draft", _R(P.CREATE_CHARACTERS,
   note="AI build: a draft bot from a description; nothing is saved or logged"))

# ---- WP-SCALE-OPS: maintenance, storage, retention (app/maintenance_api.py), all P.ADMIN_SYSTEM

# --------------------------------------------------------------------------- #
# Mounts (static trees) and dynamic prefixes
# --------------------------------------------------------------------------- #
#: (prefix, requirement) for ``Mount`` routes - first match wins.
MOUNTS: list[tuple[str, Requirement]] = [
    ("/static/avatars", public("avatar files; the gate itself narrows to audience=restricted|all characters for a Restricted caller")),
    ("/static", public("the app's own JS/CSS/icons; carries no user data")),
    ("/media", _R(P.USE_IMAGES, note="chat media")),
]

#: WebSocket routes: template -> Requirement for the handshake. Individual
#: actions on ``/ws`` are in WS_ACTION_PERMISSIONS.
WS_ROUTES: dict[str, Requirement] = {
    "/ws": public("the handshake only identifies the caller; every action is checked in WS_ACTION_PERMISSIONS"),
    "/ws/{name}/{thread_id}": _R(P.USE_VOICE, scoped=True, note="an add-on's socket; the host also checks the thread's character"),
}

#: Every action on ``/ws`` and the permission it needs. ``ping`` is liveness.
WS_ACTION_PERMISSIONS: dict[str, Requirement] = {
    "ping": public("liveness"),
    "send": _R(P.USE_CHAT, note="quota-charged for Restricted"),
    "retry": _R(P.USE_CHAT, note="quota-charged for Restricted"),
    "create_thread": _R(P.USE_CHAT, note="quota-charged for Restricted"),
    "get_threads": _R(P.USE_CHAT, scoped=True),
    "get_messages": _R(P.USE_CHAT, scoped=True),
    "abort": _R(P.MANAGE_HISTORY),
    "archive_thread": _R(P.MANAGE_HISTORY),
    "regenerate": _R(P.MANAGE_HISTORY),
    "edit_rerun": _R(P.MANAGE_HISTORY),
}

# --------------------------------------------------------------------------- #
# 4.0 ownership: what each path parameter names, and so which owner check runs
# --------------------------------------------------------------------------- #
#: path template -> ``thread`` | ``message`` | ``character`` | ``file`` | ``task`` |
#: ``none: <why>``. EVERY parameterised route is listed (``tests/test_route_table.py``), so a
#: new ``/{thing_id}`` route cannot ship without somebody saying whose thing it is.
#:
#: thread / message / task: enforced (``app/ownership.py``; tasks: ``agents_ws.may_see_task``)
#: - a resource of somebody else's is a 404. ``character``: ``characters.may_use`` - another
#: profile's PERSONAL character is a 404 for everyone, Admins included (the gate's
#: ``_foreign_character_item``); a household one off the caller's list is the route's 403.
#: ``none``: the parameter is not a per-profile resource - or its ownership check lands in a
#: later package, which the reason says (the File Server and Image Studio ledger: WP-DEL).
_NONE_ADMIN = "none: profile administration (manage_users, people_api rank rules)"
OWNER_SCOPED: dict[str, str] = {
    "/api/threads/{thread_id}": "thread",
    "/api/threads/{thread_id}/messages": "thread",
    "/api/threads/{thread_id}/read": "thread",
    "/api/threads/{thread_id}/abort": "thread",
    "/api/threads/{thread_id}/avatar": "thread",
    "/ws/{name}/{thread_id}": "thread",
    "/api/advisor/requests/{rid}/send": "thread",
    "/api/advisor/requests/{rid}/dismiss": "thread",
    "/api/image-jobs/{job_id}": "thread",
    "/api/image-jobs/{job_id}/callback": "none: the rig's per-job token is the credential and nothing is returned",
    "/api/messages/{message_id}": "message",
    "/api/messages/{message_id}/checklist": "message",
    "/api/messages/{message_id}/feedback": "message",
    "/api/messages/{message_id}/mood-face": "message",
    "/api/tasks/{task_id}": "task",
    "/api/tasks/{task_id}/events": "task",
    "/api/tasks/{task_id}/send": "task",
    "/api/tasks/{task_id}/interrupt": "task",
    "/api/tasks/{task_id}/resume": "task",
    "/api/tasks/{task_id}/cancel": "task",
    "/api/tasks/{task_id}/release": "task",
    "/api/tasks/{task_id}/dismiss": "task",
    "/api/approvals/{request_id}": "task",
    "/api/bots/{bot_id}": "character",
    "/api/bots/{bot_id}/avatar": "character",
    "/api/bots/{bot_id}/avatar/full": "character",
    "/api/bots/{bot_id}/avatar/thumb": "character",
    "/api/bots/{bot_id}/avatar/history": "character",
    "/api/bots/{bot_id}/avatar/history/{snapshot_id}": "character",
    "/api/bots/{bot_id}/avatar/restore": "character",
    "/api/bots/{bot_id}/models": "character",
    "/api/characters/{character_id}": "character",
    "/api/avatar-pool/{bot_id}": "character",
    "/api/avatar-pool/{bot_id}/prompts": "character",
    "/api/avatar-pool/{bot_id}/refill": "character",
    "/api/files/{file_id}": "none: WP-DEL - files.owner_profile_id is stamped; the per-owner File Server checks land in wave 2",
    "/api/files/{file_id}/download": "none: WP-DEL - per-owner File Server (see /api/files/{file_id})",
    "/api/files/{file_id}/raw": "none: WP-DEL - per-owner File Server (see /api/files/{file_id})",
    "/api/reactions/{rid}": "none: the household reaction library (manage_characters)",
    "/api/reactions/{rid}/image": "none: a household reaction card (safe flag decides Restricted)",
    "/api/agents/{agent_id}": "none: an agent definition is household configuration (manage_agents)",
    "/api/agents/profile-budgets/{profile_id}": _NONE_ADMIN,
    "/api/agents/secrets/{name}": "none: write-only household secret (manage_agents)",
    "/api/people/{profile_id}": _NONE_ADMIN,
    "/api/people/{profile_id}/pin": _NONE_ADMIN,
    "/api/people/{profile_id}/password": _NONE_ADMIN,
    "/api/people/{profile_id}/sign-out": _NONE_ADMIN,
    "/api/people/devices/{device_id}": _NONE_ADMIN,
    "/api/users/{profile_id}": _NONE_ADMIN,
    "/api/users/{profile_id}/export": "none: account metadata only (no chats), manage_users; the own-data export is /api/me/export (WP-PROF)",
    "/api/connections/{connection_id}": "none: household configuration (manage_connections)",
    "/api/connections/{connection_id}/test": "none: household configuration (manage_connections)",
    "/api/backups/{backup_id}": "none: host control (admin_system); a backup holds every profile's chats, stated in docs/security.md",
    "/api/backups/{backup_id}/verify": "none: host control (admin_system)",
    "/api/backups/{backup_id}/restore": "none: host control (admin_system)",
    "/api/harness/jobs/{job_id}": "none: host control (admin_system: the coding harness)",
    "/api/harness/sessions/{sid}": "none: host control (admin_system: the coding harness)",
    "/api/harness/sessions/{sid}/stop": "none: host control (admin_system: the coding harness)",
    "/api/harness/sessions/{sid}/dismiss": "none: host control (admin_system: the coding harness)",
    "/api/plugins/extensions/{ext_id}": "none: household plugin switch (manage_plugins)",
    "/api/practice/{path:path}": "none: host control (admin_system proxy)",
    "/api/practice-links/{client_id}": "none: host control (admin_system)",
    "/api/starter-packs/{pack_id}/install": "none: household characters (manage_characters)",
    "/api/tools/{tool_id}/refresh": "none: household tool (manage_plugins)",
    "/api/tools/{tool_id}/status": "none: household tool status (use_tools)",
    "/api/wizard/step/{step}": "none: first-run settings (manage_settings)",
    "/apps/{app_id}": "none: an app's static page (its manifest permission)",
    "/apps/{app_id}/{rel:path}": "none: an app's static files (its manifest permission)",
    "/ext/{ext_id}/{rel:path}": "none: an add-on's public frontend files",
    "/tools/{tool_id}": "none: a tool panel page (use_tools)",
    "/tools/{tool_id}/{rel:path}": "none: a tool panel's files (use_tools)",
    "/tools/_t/{ticket}/{rel:path}": "none: a capability URL (the ticket is the credential)",
    "/local/view/{ticket}/{rel:path}": "none: a capability URL (the ticket is the credential)",
    "/local/file/{path:path}": "none: host control (admin_system: files off the host)",
    # WP-PROF (profiles_api.py / characters_api.py), declared at INT-1.
    "/api/me/characters/{bot_id}": "character",
    "/api/me/characters/{bot_id}/avatar": "character",
    "/api/characters/{bot_id}/clone": "character",
    "/api/characters/{bot_id}/clone-to/{profile_id}": "character",
    "/api/characters/{character_id}/delete-preview": "none: household characters only, numbers only (manage_characters)",
    "/api/people/{profile_id}/reset": _NONE_ADMIN,
    "/api/people/{profile_id}/reset/preview": _NONE_ADMIN,
    "/api/people/{profile_id}/role": _NONE_ADMIN,
    "/api/people/{profile_id}/role/preview": _NONE_ADMIN,
    "/api/people/{profile_id}/household-bots": _NONE_ADMIN,
    "/api/people/{profile_id}/backends": _NONE_ADMIN,
    "/api/people/{profile_id}/delete": _NONE_ADMIN,
    "/api/people/{profile_id}/delete/preview": _NONE_ADMIN,
    # Unparameterised routes that read or write conversations: listed so routes.json says so.
    "/api/threads": "thread",
    "/api/unread": "thread",
    "/api/search": "thread",
    "/api/export": "thread",
    "/api/inject": "thread",
    "/api/daily": "none: the Owner's daily thread (a machine's sphere)",
    "/api/drop": "thread",
    "/api/reactions/fire": "thread",
    "/api/image-jobs": "thread",
    "/api/recover/transcript": "thread",
    "/api/openclaw/sessions": "thread",
    "/api/openclaw/transcript": "thread",
    "/api/openclaw/import": "thread",
}
#: App routes (mounted at runtime from ``apps/<id>``): prefix -> owner_scoped.
OWNER_SCOPED_PREFIXES: dict[str, str] = {
    "/api/apps/jobboard/": "none: the Job Board is the Admins' tracker (its manifest permission; personal data, never for Users)",
    "/api/jobs/": "none: the Job Board's legacy prefix (see /api/apps/jobboard/)",
    "/api/apps/imagestudio/": "none: WP-DEL - Image Studio's ledger is shared until wave 2; /targets and /chat-images read the caller's own threads",
}


def owner_scoped_for(template: str) -> str:
    """What :data:`OWNER_SCOPED` says about ``template`` ("" = undeclared)."""
    hit = OWNER_SCOPED.get(template)
    if hit:
        return hit
    for pre, val in OWNER_SCOPED_PREFIXES.items():
        if template.startswith(pre):
            return val
    return ""


#: Every /ws action that names a thread checks its owner (main._ws_thread); the list is the
#: caller's own (get_threads); a new thread is stamped with its creator (create_thread).
WS_ACTION_OWNER_SCOPED: dict[str, str] = {
    "send": "thread", "retry": "thread", "abort": "thread", "archive_thread": "thread",
    "regenerate": "thread", "edit_rerun": "thread", "get_messages": "thread",
    "get_threads": "thread", "create_thread": "none: the new thread is stamped with its creator",
    "ping": "none: liveness",
}

from dataclasses import replace as _replace  # noqa: E402

ROUTES[:] = [(ms, p, _replace(req, owner_scoped=owner_scoped_for(p)) if owner_scoped_for(p) else req)
             for ms, p, req in ROUTES]
for _t, _req in list(WS_ROUTES.items()):
    if owner_scoped_for(_t):
        WS_ROUTES[_t] = _replace(_req, owner_scoped=owner_scoped_for(_t))
for _a, _req in list(WS_ACTION_PERMISSIONS.items()):
    if _a in WS_ACTION_OWNER_SCOPED:
        WS_ACTION_PERMISSIONS[_a] = _replace(_req, owner_scoped=WS_ACTION_OWNER_SCOPED[_a])

_BY_KEY: dict[tuple[str, str], Requirement] = {}
for _ms, _p, _req in ROUTES:
    for _m in _ms:
        _key = (_m, _p)
        if _key in _BY_KEY:
            raise RuntimeError(f"route declared twice: {_m} {_p}")
        _BY_KEY[_key] = _req
        if _m == "GET":
            _BY_KEY[("HEAD", _p)] = _req

#: Prefixes owned by something that is not a FastAPI route of its own (extension
#: routers registered at runtime). They carry the plugin permission.
_EXTENSION_REQ = _R(P.MANAGE_PLUGINS, note="an installed add-on's operator API (its own Origin check applies)")

#: The add-on routes a USER of voice chat reads (2026-10-19, the owner: "Members should have
#: voice"): what the call screen and Settings -> Voice draw from - the status (on/off, ready, the
#: voice list), a voice's sample, the cloned-voice list. ``use_voice`` = Member, Admin, Owner. A
#: Restricted caller holds use_voice for its locked voice socket but not these (``not_restricted``): a
#: locked device reads the add-on's ``locked_status`` in /api/extensions instead. Everything else under an add-on's prefix - the switch
#: and server settings (PATCH /settings), cloning, the lab, the rig, model downloads - is voice
#: ADMINISTRATION and stays ``manage_plugins``.
_EXTENSION_USE_REQ = _R(P.USE_VOICE, not_restricted=True, note=(
    "a voice user's read (Member, Admin, Owner; not a Restricted caller, whose locked device reads "
    "locked_status in /api/extensions). The add-on's other routes are manage_plugins"))
EXTENSION_USE_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/voice/status"),
    ("GET", "/api/voice/preview"),
    ("GET", "/api/voice/profiles"),
})


#: 4.0 per-profile voice clones (BUILD-PLAN-v2 §1.12): the add-on enforces ownership inside
#: its routes; the host gate is coarse. Only while an add-on has declared owned clones
#: (``ext_profile_hooks.voice_owned_clones_active``) - with an add-on that has not (1.5.0)
#: the table above applies and every clone route stays Admin-only.
_EXTENSION_OWNED_USE_REQ = _R(P.USE_VOICE, note=(
    "voice reads: the add-on lists household voices plus the caller's own clones and serves a "
    "clone's recordings to its owner only"))
_EXTENSION_VOICE_LAB_REQ = _R(P.VOICE_LAB, note=(
    "create / rename / delete / try a line of the caller's own cloned voice (the add-on "
    "answers 404 to a non-owner)"))
_VOICE_PROFILE_ONE_RE = re.compile(r"^/api/voice/profiles/[^/]+$")
_VOICE_PROFILE_FILE_RE = re.compile(r"^/api/voice/profiles/[^/]+/files/.+$")
_VOICE_PROFILE_SAY_RE = re.compile(r"^/api/voice/profiles/[^/]+/say$")


def _owned_voice_requirement(m: str, p: str) -> Requirement | None:
    if m == "GET" and (p in ("/api/voice/status", "/api/voice/preview", "/api/voice/profiles")
                       or (_VOICE_PROFILE_ONE_RE.match(p) and p != "/api/voice/profiles/analyse")
                       or _VOICE_PROFILE_FILE_RE.match(p)):
        return _EXTENSION_OWNED_USE_REQ
    if m == "POST" and (p in ("/api/voice/profiles", "/api/voice/profiles/analyse")
                        or _VOICE_PROFILE_SAY_RE.match(p)):
        return _EXTENSION_VOICE_LAB_REQ
    if m in ("PATCH", "DELETE") and _VOICE_PROFILE_ONE_RE.match(p) \
            and p != "/api/voice/profiles/analyse":
        return _EXTENSION_VOICE_LAB_REQ
    return None


def extension_requirement(method: str, path: str) -> Requirement:
    """What one request on an add-on's own routes needs. While an add-on has declared
    per-profile clones: ``use_voice`` for the voice reads (status, preview, the voice list,
    one voice and its recordings) and ``voice_lab`` for creating and changing one's own
    clones; ``manage_plugins`` (Admin) for everything else. Otherwise the 3.x table:
    ``use_voice`` for the reads in EXTENSION_USE_ROUTES, ``manage_plugins`` for the rest."""
    from . import ext_profile_hooks
    m = "GET" if method.upper() == "HEAD" else method.upper()
    p = path.rstrip("/") or "/"
    if ext_profile_hooks.voice_owned_clones_active():
        hit = _owned_voice_requirement(m, p)
        if hit is not None:
            return hit
        return _EXTENSION_REQ
    if (m, p) in EXTENSION_USE_ROUTES:
        return _EXTENSION_USE_REQ
    return _EXTENSION_REQ


def extension_document() -> dict:
    """routes.json's view of the add-on prefixes (registered at runtime, so not in ``routes``)."""
    return {"default": _EXTENSION_REQ.describe(),
            "use": [{"method": m, "path": p, "requirement": _EXTENSION_USE_REQ.describe()}
                    for m, p in sorted(EXTENSION_USE_ROUTES)],
            # 4.0: the table while an add-on declares per-profile clones
            "owned_clones": {
                "use_voice": ["GET /api/voice/status", "GET /api/voice/preview",
                              "GET /api/voice/profiles", "GET /api/voice/profiles/{pid}",
                              "GET /api/voice/profiles/{pid}/files/{name}"],
                "voice_lab": ["POST /api/voice/profiles", "POST /api/voice/profiles/analyse",
                              "PATCH /api/voice/profiles/{pid}", "DELETE /api/voice/profiles/{pid}",
                              "POST /api/voice/profiles/{pid}/say"],
                "use_requirement": _EXTENSION_OWNED_USE_REQ.describe(),
                "voice_lab_requirement": _EXTENSION_VOICE_LAB_REQ.describe()}}


def declared() -> dict[tuple[str, str], Requirement]:
    """The static (method, template) -> Requirement table, HEAD included."""
    return dict(_BY_KEY)


def requirement_for(method: str, template: str | None, path: str) -> Requirement | None:
    """The declared requirement for one concrete request, or None if undeclared."""
    from . import apps_loader, extensions
    if template is None:
        return None
    hit = _BY_KEY.get((method.upper(), template))
    if hit is not None:
        # Per-app static pages carry the manifest's permission, not the generic one.
        if template.startswith("/apps/"):
            app_req = apps_loader.requirement_for(method, path)
            if app_req is None:
                return hit
            # ...and keep the declared owner_scoped decision (routes.json publishes it).
            return _replace(app_req, owner_scoped=hit.owner_scoped) if hit.owner_scoped and \
                not app_req.owner_scoped else app_req
        return hit
    if template.startswith(("/ws",)):
        return WS_ROUTES.get(template)
    if extensions.owns_path(path):
        return extension_requirement(method, path)
    req = apps_loader.requirement_for(method, path)
    if req is not None and not req.owner_scoped and owner_scoped_for(template):
        req = _replace(req, owner_scoped=owner_scoped_for(template))
    return req


def requirement_for_unmatched(method: str, path: str) -> Requirement | None:
    """For a path no route matches: app-shaped paths carry the app requirement, so a
    Restricted caller is refused (403) instead of told which apps exist (404)."""
    from . import apps_loader, extensions
    if extensions.owns_path(path):
        return _EXTENSION_REQ
    return apps_loader.requirement_for(method, path)


def requirement_for_mount(prefix: str, path: str) -> Requirement | None:
    for pre, req in MOUNTS:
        if path == pre or path.startswith(pre + "/"):
            return req
    return None


def ws_action_requirement(action: str | None) -> Requirement | None:
    return WS_ACTION_PERMISSIONS.get(str(action))
