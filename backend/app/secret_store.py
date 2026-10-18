"""Where DisPatch keeps credentials that must not sit in a settings file.

A Connection's API key, the backup passphrase and any ``secret`` setting live here, by a
*reference* (``connection.openai``), never in ``config.yaml`` or the settings table. It is
the store ``app/agents/secrets.py`` built for agent backends (OS keyring when one is usable,
else a 0600 JSON file under the data directory), so there is exactly one secret mechanism.

Reads fall through keyring -> file, writes go to the first writable one. The process
environment is deliberately NOT part of this chain: an ``env:NAME`` credential reference is
resolved explicitly by the caller (``connections.credential``), because "read any variable
a stranger names" is exactly the hole ``_stored_probe_key`` in the old Connect flow closed.

``DISPATCH_SECRET_STORE=file`` forces the file (containers, tests); ``=keyring`` insists on
the keyring and fails loudly if there is none.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path

from . import config
from .agents.secrets import (
    GLOBAL_REDACTOR,
    ChainSecretProvider,
    FileSecretProvider,
    SecretError,
    SecretProvider,
)

log = logging.getLogger("local-chat.secrets")

_lock = threading.Lock()
_providers: dict[str, SecretProvider] = {}


def _mode() -> str:
    return (config.env("SECRET_STORE", "auto") or "auto").strip().lower()


def provider() -> SecretProvider:
    """The store for the CURRENT data directory (cached per directory)."""
    data = Path(config.DATA_DIR)
    key = f"{data}|{_mode()}"
    with _lock:
        hit = _providers.get(key)
        if hit is not None:
            return hit
        file_p = FileSecretProvider(data / "agents" / "secrets.json")
        chain: list[SecretProvider] = []
        mode = _mode()
        if mode != "file":
            try:
                from .agents.secrets import KeyringSecretProvider
                kr = KeyringSecretProvider(service="dispatch-chat")
                if kr.usable():
                    chain.append(kr)
                elif mode == "keyring":
                    raise SecretError("DISPATCH_SECRET_STORE=keyring but no usable OS keyring was found")
            except ImportError:
                if mode == "keyring":
                    raise SecretError("DISPATCH_SECRET_STORE=keyring but the keyring package is not installed") from None
        chain.append(file_p)
        built = ChainSecretProvider(chain)
        _providers[key] = built
        return built


def reset_for_tests() -> None:
    with _lock:
        _providers.clear()


def get(ref: str) -> str | None:
    value = provider().get(ref)
    if value:
        GLOBAL_REDACTOR.register(value)
    return value


def put(ref: str, value: str) -> None:
    """Store ``value`` and read it straight back; raises SecretError if it did not stick."""
    provider().set(ref, value)
    if provider().get(ref) != value:
        raise SecretError("the secret store did not keep the value")
    GLOBAL_REDACTOR.register(value)


def delete(ref: str) -> None:
    with contextlib.suppress(Exception):
        provider().delete(ref)


def exists(ref: str) -> bool:
    return bool(provider().get(ref))
