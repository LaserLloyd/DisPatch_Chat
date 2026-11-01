"""An in-memory ring of the app's recent log lines (4.3, issue #18).

DisPatch logs to standard output (systemd's journal, ``docker logs``), and nothing writes
``<data>/logs/dispatch.log`` unless the operator sets ``DISPATCH_LOG_FILE``. So the support
bundle's ``log-tail.txt`` and Settings > System > Logs had nothing to show on most installs.
This module keeps the last :data:`MAX_LINES` formatted records in memory for them.

The ring holds formatted lines as logged. **Redaction is the reader's job**: the dashboard
and the support bundle pass every line through the redactor before it leaves the process.
"""

from __future__ import annotations

import collections
import logging
import threading

MAX_LINES = 2000
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RingHandler(logging.Handler):
    """A logging handler that keeps the last ``maxlen`` formatted records."""

    def __init__(self, maxlen: int = MAX_LINES) -> None:
        super().__init__(level=logging.NOTSET)
        self.lines: collections.deque[str] = collections.deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter(_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:          # a logging handler must never raise into the caller
            self.handleError(record)


_handler: RingHandler | None = None
_lock = threading.Lock()


def install() -> RingHandler:
    """Attach the ring to the root logger. Idempotent: a second call (another lifespan in
    the same process, a reload) keeps the one handler and its lines."""
    global _handler
    with _lock:
        root = logging.getLogger()
        if _handler is None:
            _handler = RingHandler()
        if _handler not in root.handlers:
            root.addHandler(_handler)
        # The root logger's level is left alone: main.py's basicConfig sets INFO, and the
        # ring sees exactly what the journal sees.
        return _handler


def installed() -> bool:
    return _handler is not None and _handler in logging.getLogger().handlers


def lines(n: int | None = None) -> list[str]:
    """The last ``n`` lines (all of them when ``n`` is None), oldest first. Unredacted."""
    h = _handler
    if h is None:
        return []
    snap = list(h.lines)
    if n is None:
        return snap
    n = max(0, int(n))
    return snap[-n:] if n else []


def tail(n: int = 200) -> str:
    """The last ``n`` lines as one text block (empty when nothing was logged). Unredacted:
    callers redact before the text leaves the process."""
    return "\n".join(lines(n))
