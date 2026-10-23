"""The capability registry: what DisPatch can do, and where each thing can be done.

A *capability* is one thing the product does for someone ("create a thread", "bind this
device to a profile"). Each maps to the surfaces that expose it:

``http``   (method, path template) pairs of the REST API
``ws``     WebSocket actions on ``/ws``
``ui``     the frontend entry point: ``api.<fn>`` in ``frontend/static/js/api.js`` or a
           Settings pane (``settings.<pane>``)
``cli``    ``None`` today. DisPatch has no ``dispatch`` command yet (audit P11); the
           field exists so the parity test can say exactly which capabilities still
           have no CLI. W9/W10 fill it as commands appear.
``mcp``    ``None`` today, for the same reason (no MCP server in this product).

``tests/test_capabilities.py`` is the parity test (gate G6): every declared surface
must exist, and the generated ``docs/contracts/capabilities.json`` must equal the
committed copy. ``unmapped_routes`` lists REST routes no capability claims yet - the
backlog, not a failure.

Filled from what exists in 3.0. Not exhaustive on purpose: grouped routes (all the
harness endpoints, the reaction pool) share one capability.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    id: str
    title: str
    permission: str | None
    http: tuple[tuple[str, str], ...] = ()
    ws: tuple[str, ...] = ()
    ui: str | None = None
    cli: str | None = None
    mcp: str | None = None
    machine: tuple[str, ...] = ()        # machine scopes that reach it as well

    def as_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "permission": self.permission,
            "http": [f"{m} {p}" for m, p in self.http], "ws": list(self.ws),
            "ui": self.ui, "cli": self.cli, "mcp": self.mcp, "machine": list(self.machine),
        }


def _c(id: str, title: str, permission: str | None, *, http=(), ws=(), ui=None, machine=()) -> Capability:
    return Capability(id=id, title=title, permission=permission,
                      http=tuple(tuple(h.split(" ", 1)) for h in http),   # type: ignore[arg-type]
                      ws=tuple(ws), ui=ui, machine=tuple(machine))


CAPABILITIES: tuple[Capability, ...] = (
    # ---- sign-in and accounts ----------------------------------------------
    _c("auth.status", "Read the sign-in state", None, http=["GET /api/auth/status"], ui="api.authStatus"),
    _c("auth.me", "Who am I, and what may I do", None, http=["GET /api/auth/me"], ui="api.me"),
    _c("auth.unlock", "Sign the main Admin in with the install PIN", None, http=["POST /api/auth/unlock"], ui="api.unlock"),
    _c("auth.login", "Sign in as a profile (PIN or password)", None, http=["POST /api/auth/login"], ui="api.login"),
    _c("auth.switch", "Quick-switch profile", None, http=["POST /api/auth/switch"], ui="api.switchProfile"),
    _c("auth.lock", "Sign out", None, http=["POST /api/auth/lock"], ui="api.lock"),
    _c("auth.setup", "Create the owner account / change the PIN", None, http=["POST /api/auth/setup"], ui="api.setupOwner"),
    _c("auth.recover", "Recover with the recovery code", None, http=["POST /api/auth/recover"], ui="api.recover"),
    _c("auth.password", "Change my password", "manage_settings", http=["POST /api/auth/password"], ui="api.changePassword"),
    _c("auth.remember", "Configure remembered devices", "manage_settings",
       http=["POST /api/auth/remember-config", "POST /api/auth/forget-devices"], ui="api.rememberConfig"),
    _c("auth.api_token", "Manage the machine API key", "manage_settings",
       http=["GET /api/auth/api-token", "POST /api/auth/api-token"], ui="api.apiTokenInfo"),
    _c("people.list", "List profiles", "manage_users", http=["GET /api/people"], ui="api.people"),
    _c("people.create", "Create a profile", "manage_users", http=["POST /api/people"], ui="api.createPerson"),
    _c("people.update", "Change a profile (role, limits, name)", "manage_users",
       http=["PATCH /api/people/{profile_id}"], ui="api.patchPerson"),
    _c("people.delete", "Delete a profile", "manage_users", http=["DELETE /api/people/{profile_id}"], ui="api.deletePerson"),
    _c("people.pin", "Set or clear a profile's PIN", "manage_users",
       http=["POST /api/people/{profile_id}/pin", "DELETE /api/people/{profile_id}/pin"], ui="api.setPersonPin"),
    _c("people.password", "Set an Admin's password", "manage_users",
       http=["POST /api/people/{profile_id}/password"], ui="api.setPersonPassword"),
    _c("people.sign_out", "Sign a profile out everywhere", "manage_users",
       http=["POST /api/people/{profile_id}/sign-out"], ui="api.signOutPerson"),
    _c("people.devices", "List devices; forget one", "manage_users",
       http=["GET /api/people/devices", "DELETE /api/people/devices/{device_id}"],
       ui="api.devices"),
    _c("people.activity", "Activity summary and admin audit log", "view_activity",
       http=["GET /api/people/activity", "GET /api/people/audit"], ui="api.peopleActivity"),
    # ---- chat ----------------------------------------------------------------
    _c("threads.list", "List threads", "use_chat", http=["GET /api/threads"], ws=["get_threads"],
       ui="api.threads", machine=["machine:threads"]),
    _c("threads.create", "Start a thread", "use_chat", http=["POST /api/threads"], ws=["create_thread"],
       ui="api.createThread", machine=["machine:threads"]),
    _c("threads.get", "Read one thread", "use_chat", http=["GET /api/threads/{thread_id}"], ui="api.thread",
       machine=["machine:threads"]),
    _c("threads.messages", "Read a thread's messages", "use_chat", http=["GET /api/threads/{thread_id}/messages"],
       ws=["get_messages"], ui="api.messages", machine=["machine:threads"]),
    _c("threads.edit", "Rename, pin or archive a thread", "manage_history",
       http=["PATCH /api/threads/{thread_id}", "DELETE /api/threads/{thread_id}"],
       ws=["archive_thread"], ui="api.rename", machine=["machine:threads"]),
    _c("threads.abort", "Stop a running turn", "manage_history", http=["POST /api/threads/{thread_id}/abort"],
       ws=["abort"]),
    _c("threads.read", "Mark a thread read", "use_chat", http=["POST /api/threads/{thread_id}/read"],
       ui="api.markRead", machine=["machine:threads"]),
    _c("threads.unread", "Unread counts", "use_chat", http=["GET /api/unread"], ui="api.unread",
       machine=["machine:threads"]),
    _c("messages.send", "Send a message (starts a turn)", "use_chat",
       http=["POST /api/threads/{thread_id}/messages"], ws=["send"], machine=["machine:threads"]),
    _c("messages.retry", "Retry the last reply", "use_chat", ws=["retry"]),
    _c("messages.regenerate", "Regenerate or edit-and-rerun", "manage_history", ws=["regenerate", "edit_rerun"]),
    _c("messages.delete", "Delete a message", "manage_history", http=["DELETE /api/messages/{message_id}"],
       ui="api.deleteMessage", machine=["machine:messages"]),
    _c("messages.edit", "Edit a message", "manage_history", http=["PATCH /api/messages/{message_id}"],
       ui="api.editMessage"),
    _c("messages.feedback", "Rate a reply", "use_chat", http=["POST /api/messages/{message_id}/feedback"],
       ui="api.messageFeedback"),
    _c("uploads.attach", "Attach a picture or file to a chat", "attach_files",
       http=["POST /api/upload"], ui="api.upload", machine=["machine:upload"]),
    _c("search", "Search your own conversations", "use_chat", http=["GET /api/search"], ui="api.search"),
    _c("export", "Export your own history", "manage_history", http=["GET /api/export"], ui="api.exportUrl"),
    # ---- characters -----------------------------------------------------------
    _c("characters.list", "List the characters I may talk to", "use_chat", http=["GET /api/bots"], ui="api.bots",
       machine=["machine:bots_read"]),
    _c("characters.manage_list", "List every character with its settings", "manage_characters",
       http=["GET /api/bots/all"], ui="api.allBots"),
    _c("characters.create", "Add a character", "manage_characters", http=["POST /api/bots"], ui="api.addBot"),
    _c("characters.update", "Change a character (including whether it is hidden)", "manage_characters",
       http=["PATCH /api/bots/{bot_id}"], ui="api.patchBot"),
    _c("characters.delete", "Remove a character", "manage_characters", http=["DELETE /api/bots/{bot_id}"],
       ui="api.deleteBot"),
    _c("characters.order", "Reorder characters", "manage_characters", http=["PUT /api/bots/order"], ui="api.saveOrder"),
    _c("characters.avatar", "Upload or restore a character's avatar", "manage_characters",
       http=["POST /api/bots/{bot_id}/avatar", "POST /api/bots/{bot_id}/avatar/restore"],
       ui="api.uploadAvatar", machine=["machine:avatars"]),
    _c("reactions.list", "List reaction images", "use_chat", http=["GET /api/reactions"], ui="api.reactions",
       machine=["machine:reactions"]),
    _c("reactions.manage", "Curate the reaction pack", "manage_characters",
       http=["POST /api/reactions", "PATCH /api/reactions/{rid}", "DELETE /api/reactions/{rid}",
             "POST /api/reactions/reseed", "PUT /api/reactions/settings"], ui="api.addReaction"),
    _c("reactions.fire", "Fire a reaction (agents)", None, http=["POST /api/reactions/fire"], ui="api.fireReaction",
       machine=["machine:reactions"]),
    # ---- connections, agents ---------------------------------------------------
    _c("connections.llm", "Connect and test an AI provider", "manage_connections",
       http=["GET /api/llm/providers", "POST /api/llm/test", "POST /api/llm/connect"], ui="settings.connections"),
    _c("connections.image", "Configure image generation", "manage_connections",
       http=["GET /api/llm/image", "PUT /api/llm/image", "POST /api/llm/image/test"], ui="settings.connections"),
    _c("connections.registry", "Add, change, test and discover connections", "manage_connections",
       http=["GET /api/connections", "POST /api/connections", "GET /api/connections/types",
             "POST /api/connections/test", "POST /api/connections/discover", "POST /api/connections/adopt",
             "GET /api/connections/{connection_id}", "PATCH /api/connections/{connection_id}",
             "DELETE /api/connections/{connection_id}", "POST /api/connections/{connection_id}/test"],
       ui="settings.connections"),
    _c("characters.api", "Add and change characters (3.0 name for bots)", "manage_characters",
       http=["GET /api/characters/all", "GET /api/characters/personas", "PUT /api/characters/order",
             "POST /api/characters", "GET /api/characters/{character_id}",
             "PATCH /api/characters/{character_id}", "DELETE /api/characters/{character_id}"],
       ui="settings.characters"),
    _c("characters.visible", "List the characters I may talk to", "use_chat", http=["GET /api/characters"],
       ui="settings.characters", machine=["machine:bots_read"]),
    _c("characters.packs", "List and install starter packs", "manage_characters",
       http=["GET /api/starter-packs", "POST /api/starter-packs/{pack_id}/install"], ui="settings.characters"),
    _c("settings.schema", "Read the settings schema and every value", "manage_settings",
       http=["GET /api/settings/schema", "GET /api/settings"], ui="settings.system"),
    _c("settings.change", "Change or reset settings", "manage_settings", http=["PATCH /api/settings"],
       ui="settings.system"),
    _c("settings.defaults", "Read the default theme, language and avatar style", None,
       http=["GET /api/settings/defaults"], ui="settings.appearance"),
    _c("wizard.state", "Is first-run setup needed, and what is left", None, http=["GET /api/wizard"],
       ui="settings.wizard"),
    _c("wizard.connection", "Detect and add the first connection", "manage_connections",
       http=["POST /api/wizard/detect", "POST /api/wizard/connection"], ui="settings.wizard"),
    _c("wizard.family", "Create family profiles and install a pack", "manage_users",
       http=["POST /api/wizard/family"], ui="settings.wizard"),
    _c("wizard.setup", "Skip a step, finish setup", "manage_settings",
       http=["POST /api/wizard/step/{step}", "POST /api/wizard/finish"], ui="settings.wizard"),
    _c("wizard.network", "Choose network exposure", "admin_system", http=["POST /api/wizard/network"],
       ui="settings.wizard"),
    _c("backups.manage", "List, create, verify, restore and delete backups", "admin_system",
       http=["GET /api/backups", "POST /api/backups", "GET /api/backups/pending", "DELETE /api/backups/pending",
             "POST /api/backups/{backup_id}/verify", "POST /api/backups/{backup_id}/restore",
             "DELETE /api/backups/{backup_id}"], ui="settings.system"),
    _c("support.bundle", "Download a redacted support bundle", "admin_system",
       http=["POST /api/support-bundle"], ui="settings.system"),
    _c("users.data", "Export or erase one profile's data", "manage_users",
       http=["GET /api/users/{profile_id}/export", "DELETE /api/users/{profile_id}"], ui="settings.people"),
    _c("plugins.manifests", "Read every plugin as a Tool manifest with its requested permissions",
       "manage_plugins", http=["GET /api/plugins/manifests"], ui="settings.plugins"),
    _c("agents.inject", "Post a message as an agent", "manage_agents", http=["POST /api/inject"],
       machine=["machine:inject"]),
    _c("agents.daily", "Ensure today's thread", "manage_agents", http=["POST /api/daily"], machine=["machine:daily"]),
    _c("agents.handoff", "Approve or dismiss an agent hand-off", "approve_agent_actions",
       http=["POST /api/advisor/requests/{rid}/send", "POST /api/advisor/requests/{rid}/dismiss"],
       ui="api.advisorSend"),
    _c("agents.openclaw", "Browse and import OpenClaw sessions", "manage_agents",
       http=["GET /api/openclaw/sessions", "GET /api/openclaw/transcript", "POST /api/openclaw/import"],
       ui="api.ocSessions"),
    _c("agents.available", "List the agents published to me", "dispatch_agents",
       http=["GET /api/agents/available"], ui="api.agentsAvailable"),
    _c("agents.manage", "Create, edit, publish, budget and delete agents", "manage_agents",
       http=["GET /api/agents", "POST /api/agents", "PATCH /api/agents/{agent_id}",
             "DELETE /api/agents/{agent_id}", "GET /api/agents/backends", "GET /api/agents/settings",
             "PUT /api/agents/settings", "GET /api/agents/usage",
             "POST /api/agents/retention/purge"], ui="api.agents"),
    _c("agents.credentials", "Store the Claude API key and MCP secrets (write-only)", "manage_agents",
       http=["GET /api/agents/credentials/claude", "PUT /api/agents/credentials/claude",
             "DELETE /api/agents/credentials/claude", "GET /api/agents/secrets",
             "PUT /api/agents/secrets/{name}", "DELETE /api/agents/secrets/{name}"],
       ui="api.claudeCredentials"),
    _c("tasks.dispatch", "Assign a task to an agent, watch it, steer or stop it", "dispatch_agents",
       http=["POST /api/tasks", "GET /api/tasks", "GET /api/tasks/{task_id}",
             "GET /api/tasks/{task_id}/events", "POST /api/tasks/{task_id}/send",
             "POST /api/tasks/{task_id}/interrupt", "POST /api/tasks/{task_id}/resume",
             "POST /api/tasks/{task_id}/cancel"], ui="api.createTask"),
    _c("tasks.approve", "Answer an agent's approval request; Send or dismiss a held hand-off",
       "approve_agent_actions",
       http=["POST /api/approvals/{request_id}", "POST /api/tasks/{task_id}/release",
             "POST /api/tasks/{task_id}/dismiss"], ui="api.answerApproval"),
    _c("images.jobs", "Create and read an image job", "use_images",
       http=["POST /api/image-jobs", "GET /api/image-jobs/{job_id}"], machine=["machine:image_jobs"]),
    # ---- files, tools, plugins ---------------------------------------------------
    _c("files.list", "List your own File Server files", "files_read", http=["GET /api/files"], ui="api.files",
       machine=["machine:files_list"]),
    _c("files.upload", "Upload to your File Server", "files_upload", http=["POST /api/files"], ui="api.uploadFile"),
    _c("files.delete", "Delete one of your own files (File Server: files_read; a chat attachment: manage_history)",
       "use_chat", http=["DELETE /api/files/{file_id}"], ui="api.deleteFile"),
    _c("files.wipe", "Delete your own File Server files up to a date", "files_read",
       http=["POST /api/files/wipe"], ui="api.wipeFiles"),
    _c("tools.list", "List tools I may open", "use_tools", http=["GET /api/tools"], ui="api.tools"),
    _c("tools.configure", "Edit the tool list; refresh a tool", "manage_plugins",
       http=["PUT /api/tools", "POST /api/tools/{tool_id}/refresh"], ui="api.toolsSave"),
    _c("plugins.manage", "Enable or disable plugins and live tools", "manage_plugins",
       http=["GET /api/plugins", "PUT /api/plugins/extensions/{ext_id}", "PUT /api/plugins/livetools"],
       ui="api.plugins"),
    _c("push.subscribe", "Subscribe this device to notifications", "use_notifications",
       http=["POST /api/push/subscribe", "DELETE /api/push/subscribe", "GET /api/push/vapid"], ui="settings.device"),
    # ---- host ------------------------------------------------------------------------
    _c("system.health", "Health and diagnostics", None, http=["GET /api/health"], ui="api.health",
       machine=["machine:health_detail"]),
    _c("system.dashboard", "Host dashboard", "admin_system",
       http=["GET /api/dashboard", "GET /api/dashboard/deep", "GET /api/dashboard/logs"], ui="settings.health"),
    _c("system.harness", "DeepSeek harness (code execution)", "admin_system",
       http=["GET /api/harness/status", "POST /api/harness/jobs", "POST /api/harness/sessions"],
       ui="api.harnessStatus"),
    _c("system.local_viewer", "Local viewer", "admin_system",
       http=["GET /api/local/roots", "GET /api/local/stat", "GET /api/local/ls", "PUT /api/local/config"],
       ui="api.localRoots"),
    _c("system.openapi", "Read the OpenAPI document", "manage_settings", http=["GET /api/openapi.json"],
       machine=["machine:openapi"]),
)


def by_id() -> dict[str, Capability]:
    return {c.id: c for c in CAPABILITIES}


def document(unmapped_routes: list[str] | None = None) -> dict:
    """The JSON-able contract written to ``docs/contracts/capabilities.json``."""
    doc = {
        "schema": 1,
        "surfaces": ["http", "ws", "ui", "cli", "mcp"],
        "note": "cli and mcp are null everywhere: DisPatch ships no CLI or MCP server yet.",
        "capabilities": [c.as_dict() for c in CAPABILITIES],
    }
    if unmapped_routes is not None:
        doc["unmapped_routes"] = unmapped_routes
    return doc
