"""Secret handling for agent backends: a provider abstraction and a redactor.

RESEARCH.md section 5 items 1, 2 and 5: product credentials are an Anthropic
Console API key or Bedrock / Google Cloud / Foundry credentials. Never a
claude.ai login, never an OAuth token, and no key ever lands in logs,
transcripts, events, fixtures or support bundles.

Three layers keep that true:

1. WHERE a key lives: :class:`SecretProvider` - the OS keyring when one is
   usable, else a 0600 file under the data dir (never ``config.yaml``).
2. HOW it travels: adapters hand it to the worker over a pipe (stdin), not argv
   or the worker's own environment (see claude_sdk_adapter).
3. WHAT leaks anyway: :class:`SecretRedactor` scrubs every string that is about
   to enter an event, a log record or a support dump.

Nothing here imports the keyring at module import: it is an optional extra and
a headless Linux box often has no usable backend.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

REDACTED = "***redacted***"

# Env vars that must never reach an agent worker (RESEARCH.md section 5 item 2).
# The OAuth token belongs to a subscription login, which a third-party product
# may not offer (F2); the others are the same family of ambient credentials.
SCRUBBED_ENV = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
)

# Anything shaped like an Anthropic key is redacted even if we never registered
# it (a key echoed by a tool result, a pasted traceback).
_KEY_SHAPES = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
_MIN_SECRET_LEN = 8


class SecretError(Exception):
    pass


@runtime_checkable
class SecretProvider(Protocol):
    name: str

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class SecretRedactor:
    """Replaces registered secret values (and key-shaped strings) with a mask."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def register(self, value: str | None) -> None:
        if value and len(value) >= _MIN_SECRET_LEN:
            with self._lock:
                self._values.add(value)

    def register_all(self, values: Iterable[str | None]) -> None:
        for v in values:
            self.register(v)

    def redact(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for v in values:
            if v in text:
                text = text.replace(v, REDACTED)
        return _KEY_SHAPES.sub(REDACTED, text)

    def redact_obj(self, obj):
        """Redact every string inside a JSON-ish structure (support dumps)."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {self.redact(str(k)): self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj

    __call__ = redact


# One process-wide redactor: adapters register what they load, and the loggers
# below consult it. A global is the honest design here - a log record from any
# module must be scrubbed by whatever any adapter registered.
GLOBAL_REDACTOR = SecretRedactor()


class RedactingFilter(logging.Filter):
    """Scrubs a log record's message and args in place, then lets it through."""

    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or GLOBAL_REDACTOR

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = self._redactor.redact(msg)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            # Tracebacks quote values; render once, redact, and drop the raw tuple.
            import traceback
            rendered = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self._redactor.redact(rendered)
            record.exc_info = None
        return True


def get_agent_logger(suffix: str) -> logging.Logger:
    """A logger under ``dispatch.agents`` that redacts before any handler sees it."""
    lg = logging.getLogger(f"dispatch.agents.{suffix}")
    if not any(isinstance(f, RedactingFilter) for f in lg.filters):
        lg.addFilter(RedactingFilter())
    return lg


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

class EnvSecretProvider:
    """Read-only: process environment (``ANTHROPIC_API_KEY`` and friends)."""

    name = "env"

    def get(self, key: str) -> str | None:
        return os.environ.get(key) or None

    def set(self, key: str, value: str) -> None:
        raise SecretError("the environment provider is read-only")

    def delete(self, key: str) -> None:
        raise SecretError("the environment provider is read-only")


class MemorySecretProvider:
    """In-process only. Tests, and callers that inject a key per task."""

    name = "memory"

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._v = dict(values or {})

    def get(self, key: str) -> str | None:
        return self._v.get(key)

    def set(self, key: str, value: str) -> None:
        self._v[key] = value

    def delete(self, key: str) -> None:
        self._v.pop(key, None)


class FileSecretProvider:
    """A JSON file created with mode 0600, atomically replaced on write.

    On POSIX the mode keeps other users out. On Windows ``chmod`` only toggles
    the read-only bit; the file is protected by the ACL it inherits from the
    per-user data dir. docs/agents/security.md lists that as a known limit.
    """

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _store(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, key: str) -> str | None:
        return self._load().get(key) or None

    def set(self, key: str, value: str) -> None:
        data = self._load()
        data[key] = value
        self._store(data)

    def delete(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            self._store(data)


class KeyringSecretProvider:
    """The OS keyring (Windows Credential Manager, macOS Keychain, Secret Service)."""

    name = "keyring"

    def __init__(self, service: str = "dispatch-chat-agents") -> None:
        import keyring  # optional extra; raises ImportError if absent
        self._kr = keyring
        self.service = service

    def usable(self) -> bool:
        """False for the no-op 'fail' backend a headless box falls back to."""
        try:
            backend = self._kr.get_keyring()
            return not type(backend).__module__.startswith("keyring.backends.fail")
        except Exception:
            return False

    def get(self, key: str) -> str | None:
        return self._kr.get_password(self.service, key) or None

    def set(self, key: str, value: str) -> None:
        self._kr.set_password(self.service, key, value)

    def delete(self, key: str) -> None:
        with contextlib.suppress(Exception):
            self._kr.delete_password(self.service, key)


class ChainSecretProvider:
    """Reads the first provider that has the key; writes go to the first writable."""

    name = "chain"

    def __init__(self, providers: list[SecretProvider]) -> None:
        self.providers = providers

    def get(self, key: str) -> str | None:
        for p in self.providers:
            with contextlib.suppress(Exception):
                v = p.get(key)
                if v:
                    return v
        return None

    def set(self, key: str, value: str) -> None:
        last: Exception | None = None
        for p in self.providers:
            try:
                p.set(key, value)
                return
            except Exception as e:      # read-only provider, locked keyring ...
                last = e
        raise SecretError(f"no writable secret provider: {last}")

    def delete(self, key: str) -> None:
        for p in self.providers:
            with contextlib.suppress(Exception):
                p.delete(key)


def default_provider(data_dir: Path) -> SecretProvider:
    """Keyring if importable and backed by something real, else a 0600 file.

    The environment is consulted first for reads so an operator can override
    (``ANTHROPIC_API_KEY`` in a service unit) without touching stored secrets.
    """
    store: SecretProvider
    try:
        kr = KeyringSecretProvider()
        store = kr if kr.usable() else FileSecretProvider(Path(data_dir) / "agents" / "secrets.json")
    except ImportError:
        store = FileSecretProvider(Path(data_dir) / "agents" / "secrets.json")
    return ChainSecretProvider([EnvSecretProvider(), store])


# Names the adapters look up. One place so a typo cannot silently mean "no key".
SECRET_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
SECRET_OPENCLAW_TOKEN = "OPENCLAW_GATEWAY_TOKEN"
