"""Parallel Studio jobs write the recent-prompts file from worker threads: no failure, no loss."""
from __future__ import annotations

import threading

from app import config

BASE = "/api/apps/imagestudio"


def test_parallel_remember_prompt_never_fails(world):
    world.create_owner()
    import sys
    mod = next(m for m in list(sys.modules.values())
               if getattr(m, "APP_ID", "") == "imagestudio" and hasattr(m, "_remember_prompt"))
    (config.DATA_DIR / "apps-data" / "imagestudio").mkdir(parents=True, exist_ok=True)
    errors: list[BaseException] = []

    def work(i):
        try:
            for j in range(20):
                mod._remember_prompt(f"p{i}-{j}", None)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert len(mod._recent_prompts(None)) == min(mod.RECENT_MAX, 160)
