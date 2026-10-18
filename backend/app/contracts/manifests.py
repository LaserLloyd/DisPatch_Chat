"""Manifest schemas for the four things DisPatch loads from outside its own code.

SCHEMA ONLY. W5 (Connections, Characters) and W6 (Voice providers) populate and load
these; W0 freezes the shape so the two workstreams and the Tool plugin API agree. Each
model forbids unknown keys (a typo must never silently mean "default"), carries
``schema: 1``, and is dumped to ``docs/contracts/manifests/<name>.schema.json`` by
``scripts/gen_contracts.py``.

Nothing here is imported by the request path.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,47}$"


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = Field(default=1, alias="schema")


class SettingField(BaseModel):
    """One setting a plugin contributes to its Settings section (rendered from this)."""
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    type: Literal["string", "integer", "number", "boolean", "choice", "secret", "url", "path"]
    label: str = Field(max_length=80)
    description: str = Field(default="", max_length=400)
    default: str | int | float | bool | None = None
    choices: list[str] = Field(default_factory=list)
    advanced: bool = False                      # hidden under "Advanced"
    env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")


class ConnectionManifest(_Manifest):
    """A model/service endpoint DisPatch talks to (an LLM provider, an image rig, a
    speech server). The Settings > Connections section is generated from these."""
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(max_length=80)
    kind: Literal["llm", "image", "speech", "agent", "search", "other"]
    description: str = Field(default="", max_length=400)
    base_url: str | None = None                 # default; the owner can change it
    auth: Literal["none", "api_key", "bearer", "basic"] = "none"
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    protocol: Literal["openai", "anthropic", "lmstudio", "ollama", "mcp", "http", "custom"] = "openai"
    discover: list[str] = Field(default_factory=list)   # URLs probed to auto-detect it on the LAN
    capabilities: list[str] = Field(default_factory=list)
    settings: list[SettingField] = Field(default_factory=list)
    requires_permission: str = "manage_connections"


class ToolPluginManifest(_Manifest):
    """A Tool plugin: a static page, a URL, an app package, or a separately installed
    add-on (``dispatch.extensions`` entry point). The permission a profile needs to
    use it is declared here, never inferred."""
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(max_length=80)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")
    kind: Literal["static", "url", "app", "extension"]
    description: str = Field(default="", max_length=400)
    entry: str | None = None
    permission: str = "use_tools"
    restricted_ok: bool = False                  # `safe: true` - Restricted profiles may use it
    api_version: int = Field(default=1, ge=1)    # DisPatch extension API it targets
    network: list[str] = Field(default_factory=list)   # hosts it may call, declared up front
    license: str = Field(default="MIT", max_length=40)
    settings: list[SettingField] = Field(default_factory=list)


class VoiceModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(max_length=120)
    size_mb: int | None = Field(default=None, ge=0)
    license: str = Field(default="", max_length=60)
    url: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class VoiceProviderManifest(_Manifest):
    """A speech engine: speech-to-text, text-to-speech, voice-activity or turn
    detection. A GPL engine MUST declare ``process_boundary: true`` (it runs as its own
    process, never imported into DisPatch) - the loader refuses it otherwise."""
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(max_length=80)
    role: Literal["stt", "tts", "vad", "turn"]
    engine: str = Field(max_length=60)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    streaming: bool = True
    gpu: Literal["none", "optional", "required"] = "none"
    license: str = Field(max_length=60)
    process_boundary: bool = False
    models: list[VoiceModelRef] = Field(default_factory=list)
    settings: list[SettingField] = Field(default_factory=list)
    requires_permission: str = "use_voice"


class CharacterBackend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["direct", "advisor", "agent"] = "direct"
    connection: str | None = Field(default=None, pattern=ID_PATTERN)   # a Connection id
    model: str | None = None
    agent: str | None = None                      # agent id (OpenClaw seat / future AgentBackend)


class CharacterManifest(_Manifest):
    """A character (the 2.x "bot"): who it is, who it is for, and what answers for it."""
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    name: str = Field(min_length=1, max_length=40)
    emoji: str = Field(default="", max_length=8)
    avatar: str = Field(default="", max_length=120)
    color: str = Field(default="", max_length=32)
    description: str = Field(default="", max_length=400)
    audience: Literal["all", "members", "restricted"] = "members"
    persona: str = Field(default="", max_length=8000)
    backend: CharacterBackend = Field(default_factory=CharacterBackend)
    voice: str = Field(default="", max_length=80)
    guardrails: str = Field(default="", max_length=4000)   # appended for Restricted turns
    pack: str | None = Field(default=None, pattern=ID_PATTERN)    # starter pack it came from


MANIFESTS: dict[str, type[_Manifest]] = {
    "connection": ConnectionManifest,
    "tool-plugin": ToolPluginManifest,
    "voice-provider": VoiceProviderManifest,
    "character": CharacterManifest,
}


def schema_documents() -> dict[str, dict]:
    """``{name: JSON Schema}`` for ``docs/contracts/manifests/<name>.schema.json``."""
    out = {}
    for name, model in MANIFESTS.items():
        doc = model.model_json_schema(by_alias=True)
        doc["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        doc["title"] = f"DisPatch {name} manifest"
        out[name] = doc
    return out
