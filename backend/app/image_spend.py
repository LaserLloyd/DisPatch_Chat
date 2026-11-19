"""A daily spend cap for PAID picture providers (Image Studio and chat pictures).

WHAT COUNTS
-----------
Only a provider that bills per picture: MiniMax (dollars, list prices below), Civitai (Buzz,
Civitai's own estimate) and OpenAI / xAI Grok (no price known here, so pictures are counted
per provider). The rig's own image server and a custom OpenAI-compatible host are never
counted or blocked (a custom host is usually a free local server).

HOW IT IS ENFORCED
------------------
``reserve()`` runs BEFORE any provider call. It adds the request's estimated cost to both
counters, the profile's and the install's, inside one ``BEGIN IMMEDIATE`` transaction, and
refuses (``SpendRefused``) when either cap would be passed: parallel jobs cannot overshoot,
because the second one sees the first one's reservation. ``Reservation.release()`` gives
the amount back when the job fails or is cancelled; ``adjust()`` trims it to what was
really made (a batch can come back short). A restart keeps whatever was reserved, which
errs on the side of counting (the provider may already have billed).

The day is the server's LOCAL date; a new day starts at local midnight. Caps are the
Owner's / Admins' settings ``connections.image_spend.*`` (0 = no limit).
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import config, settings_store

SCHEMA = """
CREATE TABLE IF NOT EXISTS image_spend (
    day    TEXT NOT NULL,                 -- local YYYY-MM-DD
    scope  TEXT NOT NULL,                 -- 'install' or a profile id ('' = no owner)
    unit   TEXT NOT NULL,                 -- usd | buzz | pictures:<provider id>
    amount REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, scope, unit)
);
"""

INSTALL = "install"
#: Notice at this share of a cap.
NOTICE_AT = 0.8
#: Rows older than this many days are dropped when a day's first reservation is made.
KEEP_DAYS = 45

#: Dollars per picture, from the provider notes in image_providers.PRESETS: image-01 about
#: $0.0035; MiniMax H3 Image about $0.02 (2K); Highspeed about twice that.
MINIMAX_USD = {"image-01": 0.0035, "image-01-live": 0.0035,
               "MiniMax-H3-Image": 0.02, "MiniMax-H3-Image-Highspeed": 0.04}
#: An unlisted MiniMax model counts at the dearest known price.
MINIMAX_USD_DEFAULT = 0.04
#: Civitai's own estimate is asked for first; when it cannot be had, this many Buzz a picture.
BUZZ_FALLBACK = 30
#: Providers counted in pictures (billed per image, no price known here).
COUNTED_PROVIDERS = ("openai", "xai")

#: key -> (default, label) ; every key is ``connections.image_spend.<name>``; 0 = no limit.
DEFAULTS = {
    "profile_usd_per_day": 5.0, "install_usd_per_day": 10.0,
    "profile_buzz_per_day": 5000, "install_buzz_per_day": 10000,
    "profile_pictures_per_day": 150, "install_pictures_per_day": 300,
}


class SpendRefused(Exception):
    """The request would pass a daily cap. ``message`` names the cap and when it resets."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Cost:
    unit: str          # usd | buzz | pictures:<provider>
    amount: float


# ---- clock and storage ----------------------------------------------------- #

_now: Callable[[], datetime] = lambda: datetime.now().astimezone()      # noqa: E731  (test seam)
_lock = threading.Lock()
_ready: set[str] = set()


def today() -> str:
    return _now().date().isoformat()


def resets_at() -> str:
    """The next local midnight, as ``YYYY-MM-DD 00:00`` (shown in refusals)."""
    nxt = _now().date() + timedelta(days=1)
    return f"{nxt.isoformat()} 00:00"


def _db_path() -> Path:
    return Path(config.DATA_DIR) / "chats.db"


@contextlib.contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=5000")
        if str(path) not in _ready:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            _ready.add(str(path))
        yield con
    finally:
        con.close()


def reset_for_tests() -> None:
    _ready.clear()


# ---- settings -------------------------------------------------------------- #

def _key(name: str) -> str:
    return f"connections.image_spend.{name}"


def cap(scope_kind: str, unit: str) -> float:
    """The cap for ``scope_kind`` ('profile' | 'install') and a unit; 0 = unlimited."""
    base = unit.split(":", 1)[0]
    name = f"{scope_kind}_{base}_per_day"
    try:
        v = settings_store.get(_key(name), DEFAULTS[name])
        return max(0.0, float(v))
    except (TypeError, ValueError, KeyError):
        return float(DEFAULTS[name])


# ---- what a picture costs ------------------------------------------------- #

def cost_of(provider: str, model: str, n: int, *, buzz_total: float | None = None) -> Cost | None:
    """The estimated cost of ``n`` pictures, or None for a provider that is free / not counted."""
    n = max(1, int(n))
    if provider == "minimax":
        return Cost("usd", round(MINIMAX_USD.get(model, MINIMAX_USD_DEFAULT) * n, 6))
    if provider == "civitai":
        total = buzz_total if buzz_total and buzz_total > 0 else BUZZ_FALLBACK * n
        return Cost("buzz", float(total))
    if provider in COUNTED_PROVIDERS:
        return Cost(f"pictures:{provider}", float(n))
    return None


def _fmt(unit: str, v: float) -> str:
    base = unit.split(":", 1)[0]
    if base == "usd":
        return f"${v:,.2f}"
    if base == "buzz":
        return f"{v:,.0f} Buzz"
    return f"{v:,.0f} pictures"


def _what(unit: str) -> str:
    base = unit.split(":", 1)[0]
    if base == "pictures":
        return f"{unit.split(':', 1)[1]} pictures"
    return {"usd": "picture spending", "buzz": "Civitai Buzz"}[base]


# ---- reservations ---------------------------------------------------------- #

@dataclass
class Reservation:
    day: str
    scope: str
    unit: str
    amount: float
    notice: str = ""
    _open: bool = True

    def _add(self, delta: float) -> None:
        with _lock, _connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                for sc in (self.scope, INSTALL):
                    con.execute(
                        "UPDATE image_spend SET amount = MAX(0, amount + ?) WHERE day=? AND scope=? AND unit=?",
                        (delta, self.day, sc, self.unit))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def release(self) -> None:
        """Give everything back (the job failed or was cancelled). Safe to call twice."""
        if self._open:
            self._open = False
            with contextlib.suppress(sqlite3.Error):
                self._add(-self.amount)

    def adjust(self, actual: float) -> None:
        """Trim the reservation to ``actual`` (never raises it)."""
        if self._open and 0 <= actual < self.amount:
            diff = actual - self.amount
            self.amount = actual
            with contextlib.suppress(sqlite3.Error):
                self._add(diff)

    def commit(self) -> None:
        self._open = False


def _scope_of(owner: str | None) -> str:
    return owner or ""


def reserve(owner: str | None, cost: Cost | None) -> Reservation | None:
    """Reserve ``cost`` for ``owner`` before a provider call. None for a free provider.

    Raises :class:`SpendRefused` when the profile's or the install's cap for this unit would be
    passed. A cap of 0 is no limit. Atomic across threads and processes."""
    if cost is None or cost.amount <= 0:
        return None
    day = today()
    scope = _scope_of(owner)
    with _lock, _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            used = {}
            for sc in (scope, INSTALL):
                row = con.execute("SELECT amount FROM image_spend WHERE day=? AND scope=? AND unit=?",
                                  (day, sc, cost.unit)).fetchone()
                used[sc] = float(row["amount"]) if row else 0.0
            for kind, sc in (("profile", scope), ("install", INSTALL)):
                limit = cap(kind, cost.unit)
                if limit > 0 and used[sc] + cost.amount > limit + 1e-9:
                    who = "for this profile" if kind == "profile" else "for the whole household"
                    con.execute("ROLLBACK")
                    raise SpendRefused(
                        f"Today's limit on {_what(cost.unit)} {who} is {_fmt(cost.unit, limit)} a day "
                        f"({_fmt(cost.unit, used[sc])} used, this request needs {_fmt(cost.unit, cost.amount)}). "
                        f"It starts again at midnight ({resets_at()}, this computer's time). "
                        f"An Owner or Admin can change it in Settings.")
            for sc in (scope, INSTALL):
                con.execute(
                    "INSERT INTO image_spend (day, scope, unit, amount) VALUES (?,?,?,?) "
                    "ON CONFLICT(day, scope, unit) DO UPDATE SET amount = amount + excluded.amount",
                    (day, sc, cost.unit, cost.amount))
            con.execute("DELETE FROM image_spend WHERE day < ?",
                        ((_now().date() - timedelta(days=KEEP_DAYS)).isoformat(),))
            con.execute("COMMIT")
        except SpendRefused:
            raise
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
    notice = ""
    for kind, sc in (("profile", scope), ("install", INSTALL)):
        limit = cap(kind, cost.unit)
        after = used[sc] + cost.amount
        if limit > 0 and after + 1e-9 >= NOTICE_AT * limit:
            who = "this profile's" if kind == "profile" else "the household's"
            notice = (f"{int(after / limit * 100)}% of {who} daily limit on {_what(cost.unit)} used "
                      f"({_fmt(cost.unit, after)} of {_fmt(cost.unit, limit)}); it starts again at midnight.")
            if kind == "profile":
                break
    return Reservation(day=day, scope=scope, unit=cost.unit, amount=cost.amount, notice=notice)


# ---- reading usage ---------------------------------------------------------- #

def _rows(day: str, scopes: tuple[str, ...]) -> list[sqlite3.Row]:
    with _connect() as con:
        q = ",".join("?" for _ in scopes)
        return con.execute(f"SELECT scope, unit, amount FROM image_spend WHERE day=? AND scope IN ({q})",
                           (day, *scopes)).fetchall()


UNITS = ("usd", "buzz", "pictures:openai", "pictures:xai")


def _line(unit: str, used: float, limit: float) -> dict:
    return {"unit": unit, "label": _what(unit), "used": round(used, 4), "cap": limit,
            "used_text": _fmt(unit, used), "cap_text": _fmt(unit, limit) if limit > 0 else "no limit",
            "share": round(used / limit, 3) if limit > 0 else 0.0,
            "near": bool(limit > 0 and used + 1e-9 >= NOTICE_AT * limit)}


def usage_for(owner: str | None) -> dict:
    """The caller's own usage today plus the notice line (for Image Studio's state)."""
    scope = _scope_of(owner)
    rows = {(r["scope"], r["unit"]): float(r["amount"]) for r in _rows(today(), (scope, INSTALL))}
    lines, install, notices = [], [], []
    for unit in UNITS:
        for kind, sc in (("profile", scope), ("install", INSTALL)):
            limit = cap(kind, unit)
            used = rows.get((sc, unit), 0.0)
            if limit > 0 and used + 1e-9 >= NOTICE_AT * limit:
                who = "your" if kind == "profile" else "the household's"
                notices.append(f"{int(used / limit * 100)}% of {who} daily limit on {_what(unit)} used "
                               f"({_fmt(unit, used)} of {_fmt(unit, limit)}); it starts again at midnight.")
            (lines if kind == "profile" else install).append(_line(unit, used, limit))
    return {"day": today(), "resets_at": resets_at(), "lines": lines, "install": install,
            "notice": notices[0] if notices else ""}


def usage_all() -> dict:
    """Install-wide and per-profile usage today (Owner / Admin, Settings)."""
    day = today()
    with _connect() as con:
        rows = con.execute("SELECT scope, unit, amount FROM image_spend WHERE day=?", (day,)).fetchall()
    install = {r["unit"]: float(r["amount"]) for r in rows if r["scope"] == INSTALL}
    profiles: dict[str, dict[str, float]] = {}
    for r in rows:
        if r["scope"] != INSTALL:
            profiles.setdefault(r["scope"], {})[r["unit"]] = float(r["amount"])
    return {"day": day, "resets_at": resets_at(),
            "install": [_line(u, install.get(u, 0.0), cap("install", u)) for u in UNITS],
            "profiles": [{"profile_id": pid, "lines": [_line(u, v.get(u, 0.0), cap("profile", u)) for u in UNITS]}
                         for pid, v in sorted(profiles.items())]}
