"""AI build: draft a personal bot from a description (4.0 chat-first profiles).

Foundation ships this router EMPTY and ``main.py`` already includes it. WP-AIB adds
``POST /api/me/characters/draft`` (permission ``create_characters``; declare it in
``route_policy.py``): one meta-prompt template in ``backend/app/bot_templates/*.md``
(already on the deploy and sync allowlists), JSON validation plus one repair retry, the
draft never logged.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
