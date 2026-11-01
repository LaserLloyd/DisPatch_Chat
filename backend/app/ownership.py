"""Who owns a conversation or a file (4.0 ownership, data model).

Every thread and every File Server record carries ``owner_profile_id``: the profile it
belongs to. This module answers one question for the creation paths - *whose is the
thing being created right now?* - so every path stamps the same answer:

* a signed-in profile (cookie, PIN, the lock-target default profile on an unsigned
  device): that profile;
* a profile session minted before accounts existed, an open install (no PIN), a
  machine caller (API token, image callback), a mirror of a gateway session, a
  first-run seed: the **protected Admin** (the 3.x Owner).

``None`` comes back only while an install has no protected Admin yet; creating it
adopts every row still without one (``accounts.create_profile``).

Enforcement (4.0, rounds R2a/R2b)
---------------------------------
The second half of this module decides who may READ or WRITE a conversation, and every
thread route, message route, WebSocket action and outgoing thread frame asks it:

* **A chat is its owner's.** Nobody else - Admins included, the protected one too - lists,
  opens, searches, exports, edits, deletes or receives live frames of it. Every role is
  treated the same way: a Basic profile's chats are as private as a User's. A thread whose
  ``owner_profile_id`` is NULL reads as the protected Admin's (the 009 rule).
* **Machines** (the API key) READ only the protected Admin's chats (a cron posting to a
  daily thread, runs-deliver, the advisor digest). Writing (inject) into any existing
  thread is decided by the route, not here (BUILD-PLAN-v2 §1.7).
* **An open install** (nobody owns it yet) is everyone-is-the-Owner: everything.
* **An unsigned device** is either the lock-target default profile (an ordinary ``profile``
  principal: it reads that profile's chats) or nobody (nothing).

Every thread route, message route, WebSocket action and outgoing frame asks these
functions (``main._require_thread`` / ``_ws_thread`` / ``scope_frame_for_principal``).

A thread you may not read is a **404**, the same answer as a thread that does not exist,
so an id tells a stranger nothing (tasks have always worked this way).

The synchronous frame scoper cannot query the database, so the owner of each thread a
frame names is cached here (:func:`remember`, :func:`cached_owner`); the WebSocket manager
fills the cache before it fans a frame out (:func:`prime_for_frame`), and a thread frame
whose owner cannot be established is dropped (fail closed).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from . import accounts

#: What a thread you may not read answers - word for word the answer for one that does not
#: exist, so the two cannot be told apart.
NOT_FOUND = "Thread not found"
MESSAGE_NOT_FOUND = "Message not found"


def protected_admin_id() -> str | None:
    """The protected first Admin's id (the 3.x Owner), or None while the install has none."""
    own = accounts.get_protected_admin()
    return own.id if own is not None else None


def owner_id() -> str | None:
    """3.x name of :func:`protected_admin_id`."""
    return protected_admin_id()


def corpus_profile_id(advisor_cfg: Any = None) -> str | None:
    """Whose knowledge corpus an advisor bot reads (BUILD-PLAN-v2 §1.8): the profile named by
    the bot's ``advisor.corpus_profile`` when that profile exists, else the protected Admin.
    A turn in a thread owned by anyone else runs without the corpus (WP-ADV)."""
    want = ""
    if isinstance(advisor_cfg, dict):
        want = str(advisor_cfg.get("corpus_profile") or "").strip()
    if want and accounts.get_profile(want) is not None:
        return want
    return protected_admin_id()


def for_principal(principal: Any) -> str | None:
    """Whose new thread / file this is, for the principal creating it."""
    if principal is None:
        return owner_id()
    kind = getattr(principal, "kind", "")
    if kind == "profile":
        return getattr(principal, "profile_id", None) or owner_id()
    if kind == "none":
        return None                         # nobody: nothing is created for no one
    # "open" (no PIN: everyone is the Owner) and "machine" (agents, cron, callbacks).
    return owner_id()


def for_request(request: Any) -> str | None:
    """:func:`for_principal` for an HTTP request (via the gate's policy verdict)."""
    from . import policy
    return for_principal(policy.of(request).principal)


# --------------------------------------------------------------------------- #
# Enforcement (R2a/R2b): who may read or write a conversation
# --------------------------------------------------------------------------- #


def effective_owner(owner: str | None) -> str | None:
    """A stored ``owner_profile_id`` as access sees it: NULL = the protected Admin's."""
    return owner or protected_admin_id()


def _principal_profile_id(principal: Any) -> str | None:
    pid = getattr(principal, "profile_id", None)
    if pid:
        return pid
    # A session minted before accounts existed (``policy.for_session`` with no owner row
    # yet) is the protected Admin with no id: it owns what the protected Admin owns.
    if getattr(principal, "kind", "") == "profile" and getattr(principal, "protected", False):
        return protected_admin_id()
    return None


def may_see_thread(principal: Any, owner: str | None, *, write: bool = False) -> bool:
    """May ``principal`` read (or, with ``write``, change / send into / receive the live
    frames of) a thread owned by ``owner`` (its stored ``owner_profile_id``)? Today both
    answers are the same - the owner, and nobody else - for every role; the two words stay
    separate so a later rule can tell reading from writing without touching every caller.

    This is the whole ownership rule; the character rule (``characters.may_use``: household
    list, personal-bot owner, agents) is checked beside it, never instead of it."""
    if principal is None:
        return False
    kind = getattr(principal, "kind", "")
    if kind == "open":
        return True                         # an unclaimed install: everyone is the Owner
    eff = effective_owner(owner)
    if kind == "machine":
        # The protected Admin's chats (and, on an install with none yet, the unassigned ones).
        return eff == protected_admin_id()
    if kind != "profile":
        return False
    me = _principal_profile_id(principal)
    return me is not None and eff == me


def readable_owner_ids(principal: Any, *, write: bool = False) -> set[str | None] | None:
    """The owners whose threads ``principal`` may read (``write``: write), as a set for a
    SQL filter. ``None`` = every thread (an open install). ``{None}`` in the set means the
    NULL-owner rows (the protected Admin's). An empty set = nothing."""
    if principal is None:
        return set()
    kind = getattr(principal, "kind", "")
    if kind == "open":
        return None
    own = protected_admin_id()
    out: set[str | None] = set()
    if kind == "machine":
        mine = own
    elif kind == "profile":
        mine = _principal_profile_id(principal)
    else:
        return set()
    if mine is not None:
        out.add(mine)
    if mine is not None and mine == own:
        out.add(None)                       # NULL rows are the protected Admin's
    return out


def owner_filter_sql(principal: Any, alias: str = "t", *, write: bool = False) -> tuple[str, list]:
    """``(sql, params)``: a WHERE fragment that keeps only the threads ``principal`` may read
    - a person's OWN chats (the thread list, search, unread and export are "mine"). Fails
    closed: an unknown principal matches nothing."""
    ids = readable_owner_ids(principal, write=write)
    if ids is None:
        return "1=1", []
    col = f"{alias}.owner_profile_id" if alias else "owner_profile_id"
    named = sorted(i for i in ids if i is not None)
    parts: list[str] = []
    params: list = []
    if named:
        parts.append(f"{col} IN ({','.join('?' * len(named))})")
        params += named
    if None in ids:
        parts.append(f"{col} IS NULL")
    if not parts:
        return "0=1", []
    return "(" + " OR ".join(parts) + ")", params


async def require_thread(db: Any, principal: Any, thread_id: str | None, *,
                         write: bool = False):
    """The thread, or **404** when it does not exist or ``principal`` may not read it
    (``write``: change it). Callers still run the character check on ``thread.bot_id``."""
    thread = await db.get_thread(thread_id) if thread_id else None
    if thread is None:
        raise HTTPException(404, NOT_FOUND)
    remember(thread.id, thread.owner_profile_id)
    if not may_see_thread(principal, thread.owner_profile_id, write=write):
        raise HTTPException(404, NOT_FOUND)
    return thread


async def require_message(db: Any, principal: Any, message_id: str | None, *,
                          write: bool = False):
    """``(message, thread)``, or 404 ("Message not found") when either is missing or the
    message's thread is not one ``principal`` may read (``write``: change)."""
    msg = await db.get_message(message_id) if message_id else None
    if msg is None:
        raise HTTPException(404, MESSAGE_NOT_FOUND)
    try:
        thread = await require_thread(db, principal, msg.thread_id, write=write)
    except HTTPException:
        raise HTTPException(404, MESSAGE_NOT_FOUND) from None
    return msg, thread


# --------------------------------------------------------------------------- #
# The owner cache the (synchronous) frame scoper reads
# --------------------------------------------------------------------------- #

#: thread id -> stored owner_profile_id (None = NULL = the protected Admin's). An id that is not a key
#: is UNKNOWN, which is not the same as None.
_owners: dict[str, str | None] = {}
_OWNERS_CAP = 20000


def remember(thread_id: str | None, owner: str | None) -> None:
    if not thread_id:
        return
    if thread_id not in _owners and len(_owners) >= _OWNERS_CAP:
        # Bounded: drop the oldest half (insertion order). A dropped entry is re-read from
        # the database before the next frame about that thread goes out.
        for k in list(_owners)[: _OWNERS_CAP // 2]:
            _owners.pop(k, None)
    _owners[thread_id] = owner


_UNKNOWN: Any = object()


def cached_owner(thread_id: str | None) -> Any:
    """The cached owner, or :data:`_UNKNOWN`."""
    if not thread_id:
        return _UNKNOWN
    return _owners.get(thread_id, _UNKNOWN)


def is_known(value: Any) -> bool:
    return value is not _UNKNOWN


def forget_all() -> None:
    _owners.clear()


def frame_thread_id(frame: dict) -> str | None:
    """The thread a frame is about, if it names one (``thread_id``, a ``thread`` row, a
    ``message`` row, or a ``thread`` id inside a ``progress`` / reaction item)."""
    if not isinstance(frame, dict):
        return None
    tid = frame.get("thread_id")
    if isinstance(tid, str) and tid:
        return tid
    th = frame.get("thread")
    if isinstance(th, dict) and isinstance(th.get("id"), str) and th.get("id"):
        return th["id"]
    msg = frame.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("thread_id"), str) and msg.get("thread_id"):
        return msg["thread_id"]
    return None


async def prime_for_frame(db: Any, frame: dict) -> None:
    """Make sure the owner of the thread ``frame`` names is in the cache (called by the
    WebSocket manager before every fan-out). A thread that no longer exists stays
    unknown, and the scoper drops a thread frame it cannot attribute."""
    tid = frame_thread_id(frame)
    if tid is None or is_known(cached_owner(tid)) or db is None:
        return
    try:
        lookup = getattr(db, "thread_owner_lookup", None)
        if lookup is not None:
            exists, owner = await lookup(tid)
        else:                               # a Database without WP-CORE's helper yet
            thread = await db.get_thread(tid)
            exists, owner = thread is not None, getattr(thread, "owner_profile_id", None)
    except Exception:
        return
    if exists:
        remember(tid, owner)
