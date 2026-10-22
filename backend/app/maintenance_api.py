"""Maintenance, storage and retention routes (4.0 scale and retention, BUILD-PLAN-v2 §1.13).

Foundation ships this router EMPTY and ``main.py`` already includes it. WP-SCALE-OPS adds
(all Admin-only, permission ``admin_system``; each declared in ``route_policy.py``):

* ``GET  /api/maintenance/status``
* ``GET  /api/maintenance/storage``
* ``POST /api/maintenance/run``
* ``GET  /api/maintenance/images/preview?mode=months&months=12``
* ``GET/PUT /api/maintenance/retention``
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
