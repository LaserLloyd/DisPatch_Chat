"""The settings schema: every user-facing knob DisPatch has, described once.

Before 3.0 a setting could live in an environment variable read at import time, a
YAML file with its own reader and permissions, a hard-coded constant or a browser's
``localStorage``, and nothing described any of them (audit section B). This module is
the one description. Each :class:`SettingDef` carries what a settings page needs to
draw the control and what the server needs to accept a value:

``key``            dotted, ``<group>.<name>`` (``system.backup_keep``); a plugin's keys
                   live under ``tools.<id>.`` and a voice provider's under ``voice.``
``type``           string | integer | number | boolean | choice | url | path | list | secret
``default``        what applies when nothing is set
``group``          Characters, Connections, Voice, Tools & Modules, People & Profiles,
                   Appearance or System - the top-level order of the Settings dialog
``level``          ``basic`` (shown) or ``advanced`` (behind the toggle)
``restart_required``  a change takes effect at the next start (import-time readers)
``env``            the environment variable that overrides it. ``DISPATCH_X`` also answers
                   to the legacy ``LOCAL_CHAT_X`` (see :func:`env_names`)
``secret``         write-only: the value goes to the secret store and is never returned
``min`` / ``max`` / ``choices`` / ``unit``  the range the value must fall in
``ui_section``     where the Settings dialog draws it: ``section`` or ``section#anchor``
                   (3.2; filled from :data:`UI_SECTIONS`, not by hand on each row). A
                   value without an anchor goes under that section's *Advanced*
``presets``        the values a duration or size dropdown offers (3.2; :data:`PRESETS`),
                   in the row's own ``unit``; the page adds "Custom…" for anything else

The schema is versioned (:data:`SCHEMA_VERSION`, the ``schema: N`` in the generated
documents). A key is never renamed or repurposed inside a major version; one that goes
away is kept in :data:`RETIRED` so an old database row is reported, not silently lost.

The generated reference is ``docs/settings.md`` (``scripts/gen_contracts.py``); the
machine-readable copy is ``docs/contracts/settings.schema.json``. Nothing here imports
the rest of the app, so ``config.py`` can read it while it is still being imported.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

GroupId = Literal["characters", "connections", "voice", "tools", "people", "appearance", "system"]

#: Top-level order of the Settings dialog (the brief's order).
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("characters", "Characters", "Who you talk to: personalities, voices, avatars and who may see them."),
    ("connections", "Connections", "The models and servers DisPatch talks to, and how it finds them."),
    ("voice", "Voice", "Speech recognition and voices (provided by the voice add-on)."),
    ("tools", "Tools & Modules", "Plugins, agent runtimes and the optional panes."),
    ("people", "People & Profiles", "Sign-in, session lengths, and what programs holding the API key may do."),
    ("appearance", "Appearance", "Defaults for a new device: theme, language and avatar style."),
    ("system", "System", "Network, backups, logs and limits."),
)
GROUP_IDS = tuple(g[0] for g in GROUPS)

SettingType = Literal["string", "integer", "number", "boolean", "choice", "url", "path", "list", "secret"]

#: The shipped value of ``connections.discovery_targets``: this computer, plus the hostnames the compose
#: files give the model servers. A name that does not resolve here is skipped, not probed.
DEFAULT_DISCOVERY_TARGETS = ("127.0.0.1", "studioforge", "lmstudio", "ollama")

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_-]*){1,4}$")


class SettingError(ValueError):
    """A value the schema refuses. ``code`` is a stable slug; the message is for people."""

    def __init__(self, key: str, message: str, code: str = "settings_invalid") -> None:
        super().__init__(message)
        self.key = key
        self.code = code


class SettingDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    type: SettingType
    default: Any = None
    label: str
    description: str
    group: GroupId
    section: str = ""                        # a sub-heading inside the group
    level: Literal["basic", "advanced"] = "basic"
    restart_required: bool = False
    env: str | None = None                   # primary variable name (``DISPATCH_X`` or an exact raw name)
    legacy_prefix_alias: bool = True         # DISPATCH_X also answers to LOCAL_CHAT_X
    secret: bool = False
    choices: list[str] = Field(default_factory=list)
    choice_labels: dict[str, str] = Field(default_factory=dict)
    # Filled at run time: from the shipped bundle (themes, locales) or the connections table.
    choices_from: Literal["themes", "locales", "connections", ""] = ""
    min: float | None = None
    max: float | None = None
    unit: str = ""
    binding: str = "store"                   # "store" | "file:<adapter>" (see settings_store.ADAPTERS)
    owner: str = "core"                      # "core" | "voice" | "tool:<id>"
    #: Host control and access posture: only a caller who may administer the system (the Owner,
    #: ``admin_system``) can change it - an Admin holding ``manage_settings`` cannot. Set from
    #: OWNER_ONLY_PREFIXES, not by hand on each row.
    owner_only: bool = False
    since: int = 1
    placeholder: str = ""
    #: 3.2, additive: where Settings draws the row (``section`` or ``section#anchor``) and the
    #: dropdown presets of a duration / size. Set from UI_SECTIONS / PRESETS, not per row.
    ui_section: str = ""
    presets: list[int] = Field(default_factory=list)

    # ------------------------------------------------------------------ #
    def env_names(self) -> tuple[str, ...]:
        """Every variable that overrides this setting, most specific first."""
        if not self.env:
            return ()
        if self.env.startswith("DISPATCH_") and self.legacy_prefix_alias:
            return (self.env, "LOCAL_CHAT_" + self.env[len("DISPATCH_"):])
        return (self.env,)

    def public(self) -> dict:
        d = self.model_dump()
        d["env"] = self.env
        d["env_names"] = list(self.env_names())
        if self.choices_from:
            d["choices"] = dynamic_choices(self.choices_from) or d["choices"]
            if self.choices_from == "connections":
                d["choice_labels"] = {**dynamic_choice_labels("connections"), **d["choice_labels"]}
        if not d["ui_section"]:
            d["ui_section"] = ui_section_for(self.key, self.group)
        d.pop("binding", None)
        return d


def _d(key: str, type: SettingType, default: Any, label: str, description: str, group: GroupId,
       **kw: Any) -> SettingDef:
    return SettingDef(key=key, type=type, default=default, label=label, description=description,
                      group=group, **kw)


# --------------------------------------------------------------------------- #
# The schema
# --------------------------------------------------------------------------- #

_ONOFFAUTO = ["auto", "on", "off"]
_ONOFFAUTO_LABELS = {"auto": "Automatic", "on": "On", "off": "Off"}

_CORE: tuple[SettingDef, ...] = (
    # ============================ SYSTEM ================================= #
    _d("system.public_url", "url", "", "Public address",
       "The address other devices use to reach DisPatch, e.g. https://chat.example.net. An https:// "
       "address tells the Health tab the connection is encrypted.", "system", section="Network",
       env="DISPATCH_PUBLIC_URL", placeholder="https://chat.example.net"),
    _d("system.behind_tls", "boolean", False, "A reverse proxy provides HTTPS",
       "Say so when TLS ends at a proxy in front of DisPatch (Caddy, Tailscale Serve) so Health does "
       "not warn about plain HTTP.", "system", section="Network", env="DISPATCH_BEHIND_TLS"),
    _d("system.network_exposure", "choice", "local", "Who can reach DisPatch",
       "Local only (this computer), or your home network. The listen address itself is set by the "
       "launcher (--host, or BIND_ADDR in the Docker compose file); this records the decision so "
       "Health can tell you when the two disagree.", "system", section="Network",
       choices=["local", "lan"], choice_labels={"local": "This computer only", "lan": "My home network"}),
    _d("system.host", "string", "127.0.0.1", "Listen address (informational)",
       "What DisPatch reports as its own address. The launcher flag (--host) is what binds the port.",
       "system", section="Network", level="advanced", env="DISPATCH_HOST", restart_required=True),
    _d("system.port", "integer", 8765, "Port (informational)",
       "The port DisPatch reports. The launcher flag (--port) is what binds it.",
       "system", section="Network", level="advanced", env="DISPATCH_PORT", restart_required=True,
       min=1, max=65535),
    _d("system.backup_interval", "integer", 21600, "Database snapshot interval",
       "How often the database is copied aside automatically (every 6 hours by default). Off turns it off.",
       "system", section="Backups", env="DISPATCH_BACKUP_INTERVAL", restart_required=True,
       min=0, max=30 * 86400, unit="seconds"),
    _d("system.backup_keep", "integer", 12, "Snapshots to keep",
       "How many rotated database snapshots to keep before the oldest is deleted.",
       "system", section="Backups", env="DISPATCH_BACKUP_KEEP", restart_required=True,
       min=1, max=1000),
    _d("system.encrypted_backup_enabled", "boolean", False, "Scheduled encrypted backups",
       "Write a complete, encrypted backup (chats, characters, settings, avatars) on a schedule. "
       "The first one generates a recovery key and shows it once - keep it somewhere safe.",
       "system", section="Backups"),
    _d("system.encrypted_backup_interval_hours", "integer", 24, "Encrypted backup interval",
       "How often a scheduled encrypted backup is written.", "system", section="Backups", level="advanced",
       min=1, max=24 * 30, unit="hours"),
    _d("system.encrypted_backup_keep", "integer", 7, "Encrypted backups to keep",
       "Older encrypted backups are deleted beyond this many.", "system", section="Backups",
       level="advanced", min=1, max=365),
    _d("system.encrypted_backup_include_media", "boolean", False, "Include pictures and files",
       "Add the media and File Server folders to encrypted backups. They can be large.",
       "system", section="Backups", level="advanced"),
    _d("system.backup_passphrase", "secret", None, "Backup passphrase",
       "The passphrase that encrypts backups. Leave empty to let DisPatch generate a recovery key. "
       "Either way it is kept in this install's secret store (the schedule needs it) and never shown again, "
       "so keep your own copy somewhere else.", "system", section="Backups", level="advanced", secret=True),
    _d("system.log_file", "path", "", "Log file",
       "Where the Health tab reads log lines from. Empty uses <data folder>/logs/dispatch.log.",
       "system", section="Diagnostics", level="advanced", env="DISPATCH_LOG_FILE"),
    _d("system.files_total_max", "integer", 20 * 1024 ** 3, "File Server size limit",
       "How much the File Server may hold before uploads are refused (20 GB by default).",
       "system", section="Limits", level="advanced", env="DISPATCH_FILES_TOTAL_MAX",
       restart_required=True, min=1024 ** 2, unit="bytes"),
    # ---- Storage and retention (WP-SCALE-OPS, BUILD-PLAN-v2 1.13). Every default deletes no
    # chat and no image: images are kept forever unless an Admin chooses "N months".
    _d("system.maintenance.enabled", "boolean", True, "Daily maintenance",
       "Run the daily housekeeping (prune old log rows, tidy files, compact the database).",
       "system", section="Storage", level="advanced"),
    _d("system.maintenance.time", "string", "04:30", "Daily maintenance time",
       "Local time of day (HH:MM) the housekeeping runs. It waits while a backup or an operator "
       "maintenance window is in progress.", "system", section="Storage", level="advanced",
       placeholder="04:30"),
    _d("system.retention.images.mode", "choice", "forever", "Generated images: keep",
       "Forever (no cleanup) or only the last N months. Applies to every generated picture: "
       "reaction pictures already shown, Image Studio results, pictures made in chats and spent "
       "avatar-pool pictures. Pictures still in use are never removed; an expired picture is held "
       "in the trash before it is deleted.", "system", section="Storage",
       choices=["forever", "months"], choice_labels={"forever": "Forever", "months": "N months"}),
    _d("system.retention.images.months", "integer", 12, "Generated images: months to keep",
       "Used when images are kept for N months. A picture counts as old when its last use in a chat "
       "and its own date are both older than this.", "system", section="Storage",
       min=1, max=120, unit="months"),
    _d("system.retention.images.trash_days", "integer", 14, "Expired images: days in the trash",
       "How long an expired picture stays recoverable before it is deleted for good.",
       "system", section="Storage", level="advanced", min=1, max=365, unit="days"),
    _d("system.retention.activity_days", "integer", 90, "Activity log: days to keep",
       "Sign-in and usage rows older than this are deleted.", "system", section="Storage",
       level="advanced", min=7, max=3650, unit="days"),
    _d("system.retention.audit_days", "integer", 400, "Audit log: days to keep",
       "Admin audit rows (who changed what) older than this are deleted.", "system",
       section="Storage", level="advanced", min=30, max=3650, unit="days"),
    _d("system.retention.task_event_days", "integer", 90, "Agent task events: days to keep",
       "Event logs of finished agent tasks older than this are deleted.", "system",
       section="Storage", level="advanced", min=7, max=3650, unit="days"),
    _d("system.retention.job_event_days", "integer", 60, "Job events: days to keep",
       "Event rows of closed Job Board jobs older than this are deleted.", "system",
       section="Storage", level="advanced", min=7, max=3650, unit="days"),
    _d("system.retention.usage_days", "integer", 45, "Daily usage counters: days to keep",
       "Machine upload counters older than this are deleted.", "system", section="Storage",
       level="advanced", min=7, max=3650, unit="days"),
    _d("system.retention.transcript_seen_days", "integer", 180, "Mirror de-duplication rows: days to keep",
       "Records of already-mirrored agent transcript items older than this are deleted.", "system",
       section="Storage", level="advanced", min=35, max=3650, unit="days"),
    _d("system.retention.image_job_days", "integer", 90, "Finished image jobs: days to keep",
       "Finished, failed and cancelled picture requests older than this are deleted (the picture "
       "itself follows the image rule above).", "system", section="Storage", level="advanced",
       min=7, max=3650, unit="days"),
    _d("system.retention.agent_spend_days", "integer", 400, "Agent spend rows: days to keep",
       "Per-day agent usage totals older than this are deleted.", "system", section="Storage",
       level="advanced", min=30, max=3650, unit="days"),
    _d("system.retention.approval_days", "integer", 180, "Decided approvals: days to keep",
       "Agent approval requests that have been decided, older than this, are deleted.", "system",
       section="Storage", level="advanced", min=7, max=3650, unit="days"),
    _d("system.retention.job_feedback_days", "integer", 180, "Closed-job feedback: days to keep",
       "Votes and comments on closed Job Board jobs older than this are deleted.", "system",
       section="Storage", level="advanced", min=7, max=3650, unit="days"),
    _d("system.retention.orphan_grace_days", "integer", 7, "Unreferenced files: grace days",
       "A picture, avatar or upload that nothing refers to is moved to the trash only after this "
       "many days.", "system", section="Storage", level="advanced", min=1, max=365, unit="days"),
    _d("system.retention.orphan_trash_days", "integer", 14, "Unreferenced files: days in the trash",
       "How long a swept file stays in the trash before it is deleted for good.", "system",
       section="Storage", level="advanced", min=1, max=365, unit="days"),
    _d("system.backups.keep_recent", "integer", 8, "Database snapshots: recent to keep",
       "How many of the newest (6-hourly) database snapshots to keep.", "system",
       section="Backups", level="advanced", min=1, max=200),
    _d("system.backups.keep_daily", "integer", 7, "Database snapshots: daily to keep",
       "How many daily snapshots (the last one of each day) to keep beyond the recent ones.",
       "system", section="Backups", level="advanced", min=0, max=400),
    _d("system.backups.keep_weekly", "integer", 4, "Database snapshots: weekly to keep",
       "How many weekly snapshots (the last one of each week) to keep beyond the daily ones.",
       "system", section="Backups", level="advanced", min=0, max=400),
    _d("system.push_subject", "string", "", "Push notification contact",
       "A mailto: address push services can reach if your server misbehaves. Empty uses a placeholder.",
       "system", section="Notifications", level="advanced", env="DISPATCH_PUSH_SUBJECT",
       legacy_prefix_alias=True, placeholder="mailto:you@example.com"),

    # ============================ PEOPLE ================================= #
    _d("people.lock_timeout_seconds", "integer", 600, "Owner and Admin auto-lock",
       "How long an Owner or Admin session may sit idle before it locks.", "people",
       section="Sessions", min=30, max=7 * 86400, unit="seconds", binding="file:security"),
    _d("people.session_timeout_user", "integer", 3600, "User session length",
       "How long a User profile's session may sit idle before it ends.", "people", section="Sessions",
       level="advanced", min=30, max=30 * 86400, unit="seconds", binding="file:security"),
    _d("people.session_timeout_basic", "integer", 1800, "Basic session length",
       "How long a Basic profile's session may sit idle before it ends.", "people",
       section="Sessions", level="advanced", min=30, max=30 * 86400, unit="seconds",
       binding="file:security"),
    _d("people.lock_target", "choice", "profile_list", "When locked, show",
       "Where a lock and a device that has not signed in land: the default profile (shared by "
       "everyone who uses it) or the profile list.", "people", section="Sessions",
       choices=["default_profile", "profile_list"],
       choice_labels={"default_profile": "The default profile", "profile_list": "The profile list"},
       binding="file:security"),
    _d("people.default_profile", "string", "", "Default profile",
       "The profile id a lock lands on when 'When locked, show' is the default profile. It must "
       "not be an Admin; agents and voice cloning are always off for it.", "people",
       section="Sessions", level="advanced", binding="file:security"),
    _d("people.remember_device_days", "integer", 0, "Remember signed-in devices",
       "Off, or how many days a device that ticks 'keep this device signed in' at sign-in stays signed "
       "in (renewed by use). Turning it off forgets every remembered device.", "people", section="Sessions", min=0, max=365,
       unit="days", binding="file:security"),
    _d("people.machine_loopback_trust", "boolean", True, "Trust programs on this computer",
       "Let programs running on the same computer (agents, cron jobs) call the API without a key. "
       "Turn off once every program uses the machine API key.", "people", section="Machines",
       level="advanced", binding="file:security"),
    _d("people.machine_daily_upload_bytes", "integer", 200 * 1024 ** 2, "Machine uploads per day",
       "How much programs holding the API key (agents, cron jobs) may upload through /api/upload "
       "per day, all together, so a looping agent cannot fill the disk. People have no daily limit.",
       "people", section="Machines", level="advanced", env="DISPATCH_MACHINE_UPLOAD_QUOTA",
       restart_required=True, min=0, unit="bytes"),

    # ========================== CHARACTERS =============================== #
    _d("characters.greeting", "boolean", False, "Greet new chats",
       "Show a short canned greeting at the top of a new chat (no model call is made).",
       "characters", env="DISPATCH_GREETING"),

    # ========================== CONNECTIONS ============================== #
    _d("connections.allow_private_targets", "boolean", False, "Allow link-local and metadata targets",
       "Connections to link-local and cloud-metadata addresses (169.254.x.x, fe80::, "
       "metadata.google.internal) are refused to stop DisPatch being used to reach internal services. "
       "Turn on only if you really run a model server there.", "connections", section="Safety",
       level="advanced"),
    _d("connections.discovery_targets", "list", list(DEFAULT_DISCOVERY_TARGETS), "Find servers: where to look",
       "The hosts Find servers probes for StudioForge, LM Studio, Ollama and OpenAI-style servers, on ports "
       "1234, 8080 and 11434 (write host:port to probe one port). The default is this computer and the compose "
       "names studioforge, lmstudio and ollama (a name that does not resolve is skipped). Cloud-metadata and "
       "link-local addresses are never probed. host.docker.internal is added only inside a container; see "
       "the next setting.", "connections", section="Safety", level="advanced",
       env="DISPATCH_DISCOVERY_TARGETS"),
    _d("connections.discovery_probe_docker_host", "boolean", True, "Find servers: look on the Docker host",
       "Inside a container, Find servers also probes host.docker.internal (ports 1234, 8080, 11434), which is "
       "the computer the container runs on. Turn off to stop that. It never happens outside a container, "
       "whatever this says.", "connections", section="Safety", level="advanced",
       env="DISPATCH_DISCOVERY_PROBE_DOCKER_HOST"),
    _d("connections.default", "choice", "", "Default connection",
       "The connection a new character uses when you do not pick one.", "connections",
       choices_from="connections", choices=[""], choice_labels={"": "None"}),
    _d("connections.clawforge_url", "url", "", "Image server (MCP) address",
       "An MCP image server (ClawForge or compatible) for picture jobs, e.g. http://host:8700/mcp.",
       "connections", section="Image server", level="advanced", env="DISPATCH_CLAWFORGE_URL",
       restart_required=True),
    _d("connections.clawforge_files_url", "url", "", "Image server file address",
       "Where finished pictures are fetched from. Empty uses /files/ beside the MCP address.",
       "connections", section="Image server", level="advanced", env="DISPATCH_CLAWFORGE_FILES_URL",
       restart_required=True),
    _d("connections.callback_base", "url", "", "Callback address for the image server",
       "DisPatch's own address as the image server sees it, so it can report a finished picture "
       "straight away instead of waiting to be asked.", "connections", section="Image server",
       level="advanced", env="DISPATCH_CALLBACK_BASE", restart_required=True),
    _d("connections.image_jobs", "choice", "auto", "Picture jobs",
       "Let characters ask for pictures. Automatic turns it on when an image server or image API is "
       "configured.", "connections", section="Image server", env="DISPATCH_IMAGE_JOBS",
       restart_required=True, choices=["auto", "1", "0"],
       choice_labels={"auto": "Automatic", "1": "On", "0": "Off"}),
    _d("connections.image_cli", "string", "", "Image command for picture pools",
       "A command that makes one picture for the avatar and reaction pools. Empty turns the pools' "
       "automatic top-up off.", "connections", section="Image server", level="advanced",
       env="DISPATCH_IMAGE_CLI"),
    _d("connections.image_cli_timeout", "integer", 300, "Image command timeout",
       "How long to wait for one picture from the image command.", "connections",
       section="Image server", level="advanced", env="DISPATCH_IMAGE_CLI_TIMEOUT", min=10, max=7200,
       unit="seconds"),
    _d("connections.gpu_cli", "string", "", "GPU status command",
       "A command that reports free GPU memory, so pools stay off a busy card.", "connections",
       section="Image server", level="advanced", env="DISPATCH_GPU_CLI"),
    _d("connections.gpu_lease_url", "url", "", "GPU lease address",
       "A lease service the picture pools ask before using a shared GPU.", "connections",
       section="Image server", level="advanced", env="DISPATCH_GPU_LEASE_URL"),
    _d("connections.studioforge_panel", "boolean", False, "StudioForge panel tab",
       "Show StudioForge's control panel as a tool in the left rail. Needs the panel address below.",
       "connections", section="StudioForge", env="DISPATCH_STUDIOFORGE", restart_required=True),
    _d("connections.studioforge_panel_url", "url", "", "StudioForge panel address",
       "Where StudioForge's panel is, e.g. http://192.0.2.20:8080.", "connections",
       section="StudioForge", env="DISPATCH_STUDIOFORGE_URL", restart_required=True),
    _d("connections.studioforge_panel_remote_url", "url", "", "StudioForge panel address for other devices",
       "The address a phone or laptop elsewhere can open (e.g. a Tailscale Serve HTTPS address).",
       "connections", section="StudioForge", level="advanced", env="DISPATCH_STUDIOFORGE_REMOTE_URL",
       restart_required=True),

    # ====================== TOOLS & MODULES ============================= #
    _d("tools.livetools_enabled", "boolean", True, "Built-in lookups",
       "Web search, weather, Wikipedia, exchange rates and a calculator for direct-API characters. "
       "Off removes them from every character at once.", "tools", section="Lookups",
       binding="file:tools"),
    _d("tools.openclaw_bin", "string", "openclaw", "OpenClaw command",
       "The openclaw program used for agent characters. Change it when it is not on the PATH.",
       "tools", section="Agents", level="advanced", env="OPENCLAW_BIN", legacy_prefix_alias=False,
       restart_required=True),
    _d("tools.agent_timeout", "integer", 900, "Agent turn timeout",
       "How long an agent may take to answer before the turn is abandoned.", "tools", section="Agents",
       level="advanced", env="DISPATCH_AGENT_TIMEOUT", restart_required=True, min=10, max=86400,
       unit="seconds"),
    _d("tools.agent_max_concurrency", "integer", 3, "Agents running at once",
       "How many agent turns may run at the same time (protects local models).", "tools",
       section="Agents", level="advanced", env="DISPATCH_MAX_CONCURRENCY", restart_required=True,
       min=1, max=64),
    _d("tools.gateway_ws", "choice", "off", "Agent gateway socket",
       "Off: replies are read from transcripts. Shadow: connect and compare without delivering. "
       "On: replies arrive over the gateway socket.", "tools", section="Agents", level="advanced",
       env="DISPATCH_GATEWAY_WS", restart_required=True, choices=["off", "shadow", "1"],
       choice_labels={"off": "Off", "shadow": "Shadow (compare only)", "1": "On"}),
    _d("tools.turn_transport", "choice", "auto", "How turns reach the agent",
       "Automatic uses the gateway socket when connected, else the command line. Gateway only fails "
       "a turn rather than falling back; Command line always spawns the agent.", "tools",
       section="Agents", level="advanced", env="DISPATCH_TURN_TRANSPORT", restart_required=True,
       choices=["auto", "1", "0"],
       choice_labels={"auto": "Automatic", "1": "Gateway only", "0": "Command line"}),
    _d("tools.mirror_enabled", "boolean", True, "Mirror gateway chats",
       "Copy the agent runtime's own conversations into DisPatch threads.", "tools",
       section="Agents", level="advanced", env="DISPATCH_MIRROR", restart_required=True),
    _d("tools.mirror_poll", "integer", 4, "Mirror check interval", "How often the mirror checks while busy.",
       "tools", section="Agents", level="advanced", env="DISPATCH_MIRROR_POLL", restart_required=True,
       min=1, max=3600, unit="seconds"),
    _d("tools.mirror_idle_max", "integer", 60, "Mirror idle interval",
       "The longest the mirror waits between checks when nothing is happening.", "tools",
       section="Agents", level="advanced", env="DISPATCH_MIRROR_IDLE_MAX", restart_required=True,
       min=1, max=86400, unit="seconds"),
    _d("tools.mirror_horizon_h", "integer", 72, "Mirror history window",
       "Conversations idle for longer than this when first seen are followed from now, not imported.",
       "tools", section="Agents", level="advanced", env="DISPATCH_MIRROR_HORIZON_H",
       restart_required=True, min=1, max=24 * 365, unit="hours"),
    _d("tools.mirror_kinds", "string", "webchat,main", "Mirror these session kinds",
       "Comma-separated session kinds to mirror (webchat, main, other).", "tools", section="Agents",
       level="advanced", env="DISPATCH_MIRROR_KINDS", restart_required=True),
    _d("tools.harness", "choice", "auto", "DeepSeek Harness pane",
       "A coding-agent pane (needs the dsh program). Automatic turns it on when dsh is installed.",
       "tools", section="Harness", env="DISPATCH_HARNESS", restart_required=True,
       choices=_ONOFFAUTO, choice_labels=_ONOFFAUTO_LABELS),
    _d("tools.harness_unit", "string", "dsh-web.service", "Harness service name",
       "The systemd user unit that runs dsh web.", "tools", section="Harness", level="advanced",
       env="DISPATCH_HARNESS_UNIT", restart_required=True),
    _d("tools.harness_port", "integer", 3080, "Harness port",
       "The loopback port dsh web listens on.", "tools", section="Harness", level="advanced",
       env="DISPATCH_HARNESS_PORT", restart_required=True, min=1, max=65535),
    _d("tools.harness_remote_url", "url", "", "Harness address for other devices",
       "An address a browser elsewhere can open for the harness page.", "tools", section="Harness",
       level="advanced", env="DISPATCH_HARNESS_REMOTE_URL", restart_required=True),
    _d("tools.mail", "choice", "auto", "Emails pane (MailForge)",
       "Show MailForge's dashboard as a tool. Automatic turns it on when MailForge's runtime files "
       "are found.", "tools", section="MailForge", env="DISPATCH_MAIL", restart_required=True,
       choices=_ONOFFAUTO, choice_labels=_ONOFFAUTO_LABELS),
    _d("tools.mail_data_dir", "path", "", "MailForge data folder",
       "Where MailForge keeps its runtime files. Empty uses ~/.local/share/mailforge.", "tools",
       section="MailForge", level="advanced", env="DISPATCH_MAIL_DATA_DIR", restart_required=True),
    _d("tools.mail_remote_url", "url", "", "MailForge address for other devices",
       "The address a browser elsewhere can open for MailForge.", "tools", section="MailForge",
       level="advanced", env="DISPATCH_MAIL_REMOTE_URL", restart_required=True),
    _d("tools.practice", "choice", "auto", "Clients pane",
       "A client-pipeline dashboard proxied from another service on this computer. Automatic turns "
       "it on when its PIN file exists.", "tools", section="Clients pane", level="advanced",
       env="DISPATCH_PRACTICE", restart_required=True, choices=_ONOFFAUTO,
       choice_labels=_ONOFFAUTO_LABELS),
    _d("tools.practice_url", "url", "http://127.0.0.1:8793", "Clients service address",
       "Where the client-pipeline service listens.", "tools", section="Clients pane", level="advanced",
       env="DISPATCH_PRACTICE_URL", restart_required=True),
    _d("tools.practice_pin_file", "path", "", "Clients service PIN file",
       "A file holding the service's PIN. Empty uses ~/practice/keys/gui-pin.", "tools",
       section="Clients pane", level="advanced", env="DISPATCH_PRACTICE_PIN_FILE",
       restart_required=True),

    # =========================== APPEARANCE ============================== #
    _d("appearance.default_theme", "choice", "glacier", "Default theme",
       "The theme a device uses until its user picks another.", "appearance", choices_from="themes",
       choices=["glacier"]),
    _d("appearance.default_language", "choice", "en", "Default language",
       "The language a device uses until its user picks another.", "appearance",
       choices_from="locales", choices=["en"]),
    _d("appearance.avatar_style", "choice", "rich", "Default avatar style",
       "Rich shows full pictures, Minimal shows small round avatars. A device can override it.",
       "appearance", choices=["rich", "minimal"],
       choice_labels={"rich": "Rich", "minimal": "Minimal"}),
)

#: Settings that run a program, name a file or a server the host will contact, or change who can
#: reach DisPatch and how: host control (``admin_system``), which the Owner and Admins hold and a
#: profile merely granted ``manage_settings`` does not. The flag keeps its 3.0 name ``owner_only``.
#: Everything under ``system.`` is here; the rest are the named ones.
OWNER_ONLY_PREFIXES: tuple[str, ...] = (
    "system.", "tools.openclaw_bin", "tools.agent_", "tools.gateway_ws", "tools.turn_transport",
    "tools.mirror_", "tools.harness", "tools.mail", "tools.practice",
    "connections.allow_private_targets", "connections.clawforge", "connections.callback_base",
    "connections.image", "connections.gpu", "connections.studioforge",
    "people.machine_loopback_trust", "people.lock_target", "people.default_profile",
)

CORE_SETTINGS: tuple[SettingDef, ...] = tuple(
    d.model_copy(update={"owner_only": True}) if d.key.startswith(OWNER_ONLY_PREFIXES) else d
    for d in _CORE)

#: Where the Settings dialog (3.2) draws each setting: ``section`` or ``section#anchor``. Matched
#: by key prefix, longest first; a key without an entry goes to its group's section, under
#: Advanced. The sections and anchors are the frontend's (js/settings-nav.js SECTIONS and the
#: ``data-schema`` slots in index.html); docs/settings.md lists the result.
UI_SECTIONS: tuple[tuple[str, str], ...] = (
    ("system.public_url", "system#network"),
    ("system.behind_tls", "system#network"),
    ("system.network_exposure", "system#network"),
    ("system.backup_", "system#backups"),
    ("system.encrypted_backup_", "system#backups"),
    ("system.", "system"),
    ("people.lock_timeout_seconds", "people#signin-rules"),
    ("people.session_timeout_", "people#signin-rules"),
    ("people.remember_device_days", "people#signin-rules"),
    # Drawn by the People pane's own "When locked, show" picker (people-perms.js lockTargetGroup,
    # anchor lock-target), never as schema rows: the frontend lists both in BESPOKE_KEYS.
    ("people.lock_target", "people#lock-target"),
    ("people.default_profile", "people#lock-target"),
    ("people.machine_", "people#machine"),
    ("characters.", "characters"),
    ("connections.default", "connections#list"),
    ("connections.image_jobs", "connections#images"),
    ("connections.studioforge_", "plugins#tool:studioforge"),
    ("connections.", "connections"),
    ("tools.livetools_enabled", "plugins#lookups"),
    ("tools.openclaw_bin", "connections#agents"),
    ("tools.agent_", "connections#agents"),
    ("tools.gateway_ws", "connections#agents"),
    ("tools.turn_transport", "connections#agents"),
    ("tools.mirror_", "connections#agents"),
    ("tools.harness", "plugins#tool:harness"),
    ("tools.mail", "plugins#tool:mail"),
    ("tools.practice", "plugins#tool:practice"),
    ("appearance.", "appearance"),
    ("voice.", "voice"),
)

_GROUP_SECTION = {"characters": "characters", "connections": "connections", "voice": "voice",
                  "tools": "plugins", "people": "people", "appearance": "appearance", "system": "system"}

#: The dropdown presets of the duration and size settings, in each row's own unit.
PRESETS: dict[str, tuple[int, ...]] = {
    "system.backup_interval": (0, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 7 * 86400),
    "system.encrypted_backup_interval_hours": (6, 12, 24, 48, 168),
    "system.files_total_max": tuple(n * 1024 ** 3 for n in (1, 5, 10, 20, 50, 100)),
    "people.lock_timeout_seconds": (60, 300, 600, 900, 1800, 3600, 2 * 3600, 4 * 3600, 86400),
    "people.session_timeout_user": (900, 1800, 3600, 2 * 3600, 4 * 3600, 8 * 3600, 86400, 7 * 86400),
    "people.session_timeout_basic": (600, 900, 1800, 3600, 2 * 3600, 4 * 3600),
    "people.remember_device_days": (0, 7, 30, 90, 180, 365),
    "people.machine_daily_upload_bytes": (0, *(n * 1024 ** 2 for n in (50, 100, 200, 500, 1024))),
    "connections.image_cli_timeout": (60, 120, 300, 600, 1200),
    "tools.agent_timeout": (120, 300, 600, 900, 1800, 3600, 2 * 3600),
    "tools.mirror_poll": (2, 4, 10, 30, 60),
    "tools.mirror_idle_max": (30, 60, 300, 900, 3600),
    "tools.mirror_horizon_h": (24, 72, 168, 720),
}


def ui_section_for(key: str, group: str) -> str:
    """Where Settings draws ``key``: the longest UI_SECTIONS prefix that matches; a plugin's
    ``tools.<id>.x`` goes under that tool's ⚙ (``plugins#tool:<id>``); else its group."""
    best = ""
    target = ""
    for prefix, where in UI_SECTIONS:
        if key.startswith(prefix) and len(prefix) > len(best):
            best, target = prefix, where
    if target:
        return target
    parts = key.split(".")
    if parts[0] == "tools" and len(parts) >= 3:
        return f"plugins#tool:{parts[1]}"
    return _GROUP_SECTION.get(group, "system")


def _placed(d: SettingDef) -> SettingDef:
    return d.model_copy(update={"ui_section": d.ui_section or ui_section_for(d.key, d.group),
                                "presets": list(PRESETS.get(d.key, d.presets))})


CORE_SETTINGS = tuple(_placed(d) for d in CORE_SETTINGS)

#: key -> reason it was removed, so an old database row is reported rather than dropped.
RETIRED: dict[str, str] = {
    "characters.default_audience": "4.0: who uses a character is each profile's household list "
                                   "(People), not an audience",
}

_registry: dict[str, SettingDef] = {d.key: d for d in CORE_SETTINGS}
_plugin_keys: dict[str, set[str]] = {}          # owner -> keys it contributed


def _check_unique() -> None:
    keys = [d.key for d in CORE_SETTINGS]
    assert len(keys) == len(set(keys)), "duplicate setting key"
    for d in CORE_SETTINGS:
        assert KEY_RE.match(d.key), d.key
        assert d.key.split(".", 1)[0] == d.group, f"{d.key}: key prefix must equal its group"


_check_unique()


# --------------------------------------------------------------------------- #
# Registry access (the core schema plus what plugins contribute)
# --------------------------------------------------------------------------- #


def all_defs() -> list[SettingDef]:
    return list(_registry.values())


def get_def(key: str) -> SettingDef | None:
    return _registry.get(key)


def by_env() -> dict[str, SettingDef]:
    """env variable suffix (``BACKUP_KEEP``, ``OPENCLAW_BIN``) -> its definition."""
    out: dict[str, SettingDef] = {}
    for d in _registry.values():
        for name in d.env_names():
            out[name] = d
    return out


def register(defs: list[SettingDef], owner: str) -> None:
    """Add a plugin's / the voice add-on's settings. Keys must sit in the owner's namespace."""
    for d in defs:
        if d.key in _registry and d.key not in _plugin_keys.get(owner, set()):
            raise ValueError(f"setting {d.key!r} is already defined")
        if owner.startswith("tool:") and not d.key.startswith(f"tools.{owner[5:]}."):
            raise ValueError(f"{d.key!r} must live under tools.{owner[5:]}.")
        if owner == "voice" and not d.key.startswith("voice."):
            raise ValueError(f"{d.key!r} must live under voice.")
    for d in defs:
        _registry[d.key] = d
        _plugin_keys.setdefault(owner, set()).add(d.key)


def unregister(owner: str) -> None:
    for k in _plugin_keys.pop(owner, set()):
        _registry.pop(k, None)


def reset_plugins_for_tests() -> None:
    for owner in list(_plugin_keys):
        unregister(owner)


def from_fragment(fragment: list[dict], *, namespace: str, owner: str,
                  group: GroupId = "tools") -> list[SettingDef]:
    """Turn a plugin manifest's ``settings`` fragment (``SettingField`` dicts: key, type, label,
    description, default, choices, advanced, env) into :class:`SettingDef` rows under ``namespace.``"""
    out = []
    for f in fragment:
        t = f.get("type", "string")
        mapped: SettingType = {"string": "string", "integer": "integer", "number": "number",
                               "boolean": "boolean", "choice": "choice", "secret": "secret",
                               "url": "url", "path": "path"}.get(t, "string")   # type: ignore[assignment]
        out.append(SettingDef(
            key=f"{namespace}.{f['key']}", type=mapped, default=f.get("default"),
            label=f.get("label") or f["key"], description=f.get("description") or "",
            group=group, section=owner.split(":", 1)[-1],
            level="advanced" if f.get("advanced") else "basic",
            choices=list(f.get("choices") or []), env=f.get("env"),
            legacy_prefix_alias=False, secret=(mapped == "secret"), owner=owner))
    return out


# --------------------------------------------------------------------------- #
# Dynamic choices (themes / locales come from the shipped frontend)
# --------------------------------------------------------------------------- #


def _frontend_dir():
    from pathlib import Path
    return Path(__file__).resolve().parents[2] / "frontend" / "static"


def dynamic_choices(kind: str) -> list[str]:
    import json
    try:
        if kind == "themes":
            doc = json.loads((_frontend_dir() / "ui-theme" / "themes.json").read_text(encoding="utf-8"))
            items = doc.get("themes") if isinstance(doc, dict) else doc
            # ForgeUI's themes.json names a theme by `slug` (older bundles: `id`).
            ids = [str(t.get("slug") or t.get("id")) for t in (items or [])
                   if isinstance(t, dict) and (t.get("slug") or t.get("id"))]
            return ids
        if kind == "locales":
            return sorted(p.stem for p in (_frontend_dir() / "locales").glob("*.json"))
        if kind == "connections":
            # "" = no default. Imported here, at call time: this module must not import the
            # app while config.py is still being imported.
            from . import connections
            return ["", *[c.id for c in connections.list_all_ro()]]
    except (OSError, ValueError, ImportError):
        return []
    except Exception:          # a database that cannot be read must not break the schema
        return [""] if kind == "connections" else []
    return []


def dynamic_choice_labels(kind: str) -> dict[str, str]:
    """Display names for the choices ``dynamic_choices`` returns (connections: their names)."""
    if kind != "connections":
        return {}
    try:
        from . import connections
        return {"": "None", **{c.id: c.name for c in connections.list_all_ro()}}
    except Exception:
        return {"": "None"}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


def _bad(d: SettingDef, why: str) -> SettingError:
    return SettingError(d.key, f"{d.label}: {why}")


def coerce(d: SettingDef, raw: Any, *, strict: bool = True) -> Any:
    """``raw`` (from JSON, an env variable or a YAML file) as ``d``'s type, or SettingError.

    ``strict=False`` is for reading back what is already stored. A choice over the
    connections (``connections.default``) was free text in schema 1, so a stored id
    whose connection has since been deleted still reads back as itself (the page shows
    it as a missing connection) instead of turning into the default."""
    t = d.type
    if raw is None:
        if d.secret or t == "secret":
            return None
        return d.default
    if t == "boolean":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise _bad(d, "must be true or false")
    if t in ("integer", "number"):
        if isinstance(raw, bool):
            raise _bad(d, "must be a number")
        try:
            if t == "integer":
                if isinstance(raw, int):
                    n = raw
                elif isinstance(raw, float) and raw.is_integer():
                    n = int(raw)
                else:
                    n = int(str(raw).strip(), 10)
            else:
                n = float(raw)
        except (TypeError, ValueError):
            raise _bad(d, "must be a whole number" if t == "integer" else "must be a number") from None
        if d.min is not None and n < d.min:
            raise _bad(d, f"must be at least {d.min:g}")
        if d.max is not None and n > d.max:
            raise _bad(d, f"must be at most {d.max:g}")
        return n
    if t == "choice":
        s = str(raw).strip()
        if not strict and d.choices_from == "connections":
            if isinstance(raw, (dict, list)) or len(s) > 256 or "\x00" in s:
                raise _bad(d, "must be a connection id")
            return s
        allowed = (dynamic_choices(d.choices_from) if d.choices_from else []) or d.choices
        if allowed and s not in allowed:
            raise _bad(d, "must be one of " + ", ".join(allowed))
        return s
    if t == "url":
        s = str(raw).strip()
        if not s:
            return ""
        if not _URL_RE.match(s) or len(s) > 2048:
            raise _bad(d, "must be an http:// or https:// address")
        from urllib.parse import urlparse
        u = urlparse(s)
        if u.username or u.password:
            raise _bad(d, "must not contain a user name or password")
        return s.rstrip("/") if u.path in ("", "/") else s
    if t == "list":
        if isinstance(raw, str):
            raw = [p.strip() for p in raw.split(",") if p.strip()]
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise _bad(d, "must be a list of text values")
        return raw
    if t in ("string", "path", "secret"):
        if isinstance(raw, (dict, list)):
            raise _bad(d, "must be text")
        s = str(raw)
        if len(s) > 4096 or "\x00" in s:
            raise _bad(d, "is too long or has an invalid character")
        s = s.strip()
        if d.min is not None and len(s) < d.min:
            raise _bad(d, f"must be at least {d.min:g} characters")
        return s
    raise _bad(d, "has an unsupported type")


def to_env_text(d: SettingDef, value: Any) -> str:
    """A stored value as the text the old environment readers parse."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list):
        return ",".join(value)
    return "" if value is None else str(value)


def validate_all(values: dict[str, Any], *,
                 current: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """(coerced values, {key: error}) for a PATCH body. ``current`` holds keys' values as
    they stand: writing back exactly that value is read-style (lenient), so the page's
    Undo can restore a stored value that is no longer one of the choices."""
    ok: dict[str, Any] = {}
    errs: dict[str, str] = {}
    current = current or {}
    for k, raw in values.items():
        d = get_def(k)
        if d is None:
            errs[k] = "unknown setting"
            continue
        same = k in current and raw is not None and str(raw).strip() == str(current[k])
        try:
            ok[k] = coerce(d, raw, strict=not same)
        except SettingError as e:
            errs[k] = str(e)
    return ok, errs


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


def schema_document(defs: list[SettingDef] | None = None) -> dict:
    """``GET /api/settings/schema`` and ``docs/contracts/settings.schema.json``."""
    ds = defs if defs is not None else list(CORE_SETTINGS)
    return {
        "schema": SCHEMA_VERSION,
        "groups": [{"id": g, "label": label, "description": desc} for g, label, desc in GROUPS],
        "settings": [d.public() for d in ds],
        "retired": dict(RETIRED),
    }


#: Section and anchor names as the Settings dialog shows them (English), for docs/settings.md.
_SECTION_NAMES = {"appearance": "Appearance", "account": "Account", "voice": "Voice",
                  "characters": "Characters", "connections": "AI & Connections",
                  "people": "People & Access", "plugins": "Plugins & Apps", "system": "System"}
_ANCHOR_NAMES = {"network": "Network", "backups": "Backups", "signin-rules": "Sign-in rules",
                 "lock-target": "When locked",
                 "machine": "Machine access",
                 "list": "Connections", "images": "Image generation", "agents": "Agents",
                 "lookups": "Built-in lookups"}


def shown_in(ui_section: str) -> str:
    """``people#signin-rules`` -> ``People & Access → Sign-in rules`` (docs)."""
    sec, _, anchor = (ui_section or "").partition("#")
    name = _SECTION_NAMES.get(sec, sec)
    if not anchor:
        return f"{name} → Advanced"
    if anchor.startswith("tool:"):
        return f"{name} → Apps & tools → ⚙ {anchor[5:]}"
    return f"{name} → {_ANCHOR_NAMES.get(anchor, anchor)}"


def markdown_reference() -> str:
    """``docs/settings.md``: one table per group, generated from :data:`CORE_SETTINGS`."""
    out = [
        "# Settings reference",
        "",
        "<!-- Generated by scripts/gen_contracts.py from backend/app/settings_schema.py."
        " Do not edit by hand. -->",
        "",
        f"Schema version {SCHEMA_VERSION}. Every setting below can be read with `GET /api/settings` and "
        "changed with `PATCH /api/settings` (the `manage_settings` permission), or in "
        "**Settings** in the app (the *Shown in* column says where; a search box at the top of "
        "Settings finds any of them by name). How the store works, and how an environment variable "
        "interacts with it, is in [Settings store](#how-the-store-works) at the end.",
        "",
        "**Env** is the variable that overrides the setting. `DISPATCH_X` also answers to the legacy "
        "`LOCAL_CHAT_X`. A setting set by an environment variable shows *set by environment* in the "
        "app and cannot be changed there. **Restart** means the new value is used from the next start. "
        "*Host control* settings run a program, name a server the host contacts, or change who can reach DisPatch: "
        "they need `admin_system` (the Owner and Admins); a profile granted only `manage_settings` cannot change them.",
        "",
    ]
    for gid, label, desc in GROUPS:
        rows = [d for d in CORE_SETTINGS if d.group == gid]
        out += [f"## {label}", "", desc, ""]
        if not rows:
            out += ["*Provided by the voice add-on once it is installed; see its documentation.*"
                    if gid == "voice" else "*None yet.*", ""]
            continue
        out += ["| Key | Type | Default | Level | Restart | Env | Shown in | What it does |",
                "|---|---|---|---|---|---|---|---|"]
        for d in rows:
            default = "" if d.default in (None, "") else f"`{d.default}`"
            if d.type == "boolean":
                default = f"`{str(d.default).lower()}`"
            rng = ""
            choices = (dynamic_choices(d.choices_from) if d.choices_from else []) or d.choices
            if choices:
                rng = " One of: " + ", ".join(f"`{c}`" for c in choices) + "."
            elif d.min is not None or d.max is not None:
                lo = "" if d.min is None else f"{d.min:g}"
                hi = "" if d.max is None else f"{d.max:g}"
                rng = f" Range {lo}..{hi}{(' ' + d.unit) if d.unit else ''}."
            elif d.unit:
                rng = f" In {d.unit}."
            env = " / ".join(f"`{n}`" for n in d.env_names()) or ""
            desc_txt = d.description.replace("|", "\\|") + rng
            sec = (" *(secret, write-only)*" if d.secret else "") + (" *(host control)*" if d.owner_only else "")
            out.append(f"| `{d.key}` | {d.type} | {default} | {d.level} | "
                       f"{'yes' if d.restart_required else ''} | {env} | {shown_in(d.ui_section)} | "
                       f"{desc_txt}{sec} |")
        out.append("")
    out += [
        "## How the store works",
        "",
        "Settings live in the `settings` table of `chats.db`. On the first start of 3.0 DisPatch "
        "imports the values it finds in `security.yaml` and `tools.yaml` (idempotent: it only fills "
        "keys that have no value yet) and records every key it does not recognise in "
        "`settings_unknown` so nothing is dropped silently; both are listed by `GET /api/settings`. "
        "Run `python -m app.migrate --dry-run` to see exactly what would be imported.",
        "",
        "Precedence, highest first: **environment variable**, then the **stored value**, then the "
        "**default**. A value set by the environment is reported with `source: env` and refused by "
        "`PATCH` (change the environment instead). Settings marked *file* in the app are written "
        "through to their original file (`security.yaml`, `tools.yaml`) so everything that reads "
        "that file keeps working.",
        "",
        "A plugin's own settings are declared in its manifest (`settings` fragment) and appear under "
        "`tools.<plugin id>.`; the voice add-on's under `voice.`.",
        "",
        "### `settings_store.writer(namespace)` - for add-ons",
        "",
        "An add-on moves its own settings into the store with a function it gets from the store:",
        "",
        "```python",
        "from app import settings_store",
        "",
        'write = settings_store.writer("voice")        # -> Callable[[str, Any], None]',
        'write("voice.speed", 1.1)                      # a qualified key (what migrate_voice_yaml passes)',
        'write("speed", 1.1)                            # a bare key gets the namespace: the same key',
        "```",
        "",
        "Signature: `writer(namespace: str) -> Callable[[str, Any], None]`; the namespace is lower-case "
        "dotted words (`voice`, `tools.weather`). `write(key, value)` raises `ValueError` for a key that is "
        "not shaped like a settings key and `settings_schema.SettingError` for a value the schema refuses "
        "or a key an environment variable currently sets. A key the schema defines is validated exactly like "
        "a `PATCH /api/settings`; a key it does not define is stored as given (JSON) and listed under `extra` by "
        "`GET /api/settings`. To have the Settings page draw an add-on's rows, register definitions once with "
        "`settings_schema.register(defs, owner)` (see `from_fragment`). Read back with "
        "`settings_store.get(key, default)`.",
        "",
    ]
    return "\n".join(out)



# --------------------------------------------------------------------------- #
# Boot-time bridge for config.py
# --------------------------------------------------------------------------- #


def env_values_from_db(db_path) -> dict[str, str]:
    """``{env name: text}`` for every stored value whose setting has an environment alias.

    ``config.env()`` consults this AFTER the process environment and BEFORE its own default,
    so a value changed in Settings reaches the old import-time readers (the frozen
    ``Settings`` dataclass, the quota constants in main.py) at the next start, and the
    call-time readers at once. Keys are the variable names with the ``DISPATCH_`` prefix
    removed (what ``config.env`` is asked for); a raw name (``OPENCLAW_BIN``) keeps its name.
    Reads ``chats.db`` read-only and never raises: no file, no table, no values.
    """
    import json
    import sqlite3
    from pathlib import Path
    p = Path(db_path)
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return {}
    try:
        try:
            rows = con.execute("SELECT key, value FROM settings").fetchall()
        except sqlite3.Error:
            return {}
        for key, text in rows:
            d = _registry.get(key)
            if d is None or not d.env or d.secret:
                continue
            try:
                value = coerce(d, json.loads(text))
            except (ValueError, SettingError):
                continue
            t = to_env_text(d, value)
            if t == "":
                continue
            name = d.env[len("DISPATCH_"):] if d.env.startswith("DISPATCH_") else d.env
            out[name] = t
    finally:
        con.close()
    return out

# type used by settings_store adapters
Adapter = tuple[Callable[[], Any], Callable[[Any], None]]
