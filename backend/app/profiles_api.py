"""Profile lifecycle routes (4.0 chat-first profiles, BUILD-PLAN-v2 §1.3).

Foundation ships this router EMPTY and ``main.py`` already includes it, so WP-PROF adds
routes here without touching ``main.py``. Every route it adds must also be declared in
``route_policy.py`` (``tests/test_route_table.py`` fails otherwise) and, when it takes an
id, be listed in ``route_policy.OWNER_SCOPED``.

Planned (WP-PROF):

* ``GET  /api/me/reset/preview``                 -> ``profiles_service.reset_preview`` (``manage_history``)
* ``POST /api/me/reset`` ``{token}``             -> ``profiles_service.reset_profile`` (``manage_history``)
* ``GET  /api/people/{profile_id}/reset/preview`` and ``POST /api/people/{profile_id}/reset``
  (``manage_users``; never the protected Admin unless it is the caller)
* ``GET/POST /api/me/characters``, ``GET/PATCH/DELETE /api/me/characters/{bot_id}``,
  ``POST /api/me/characters/{bot_id}/avatar`` (``create_characters``)
* ``GET  /api/me/export`` (``manage_history``)
* ``POST /api/characters/{bot_id}/clone`` (``create_characters``) and
  ``POST /api/characters/{bot_id}/clone-to/{profile_id}`` (``manage_users``)
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
