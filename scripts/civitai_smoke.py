#!/usr/bin/env python3
"""Live Civitai smoke test (not deployed). Run in-process from the install backend:

  cd <install>/backend
  PYTHONPATH=. DISPATCH_DATA_DIR=<data dir> \
    uv run --offline python <repo>/scripts/civitai_smoke.py whatif|field-probe|one --yes

Reads the key exactly as Image Studio does (``resolve(..., studio=True)``) and never
prints it. ``one`` spends Buzz and refuses without --yes. Exit 0 only when the
effect happened (a cost number / a validated image)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from app import image_jobs, image_providers as ip


def _res(model: str | None):
    cfg = {"provider": "civitai"}
    if model:
        cfg["model"] = model
    r = ip.resolve(cfg, studio=True)
    print(f"key_source={r.key_source} model={r.model}")
    return r


async def _whatif_raw(res, field: str) -> tuple[int, int | None]:
    spec = image_jobs.ImageSpec(prompt="a red apple on a table", ratio="1:1")
    body = ip.civitai_body(res, spec, 1, None, external_id=None)
    inp = body["steps"][0]["input"]
    inp.pop(ip.CIVITAI_MODEL_FIELD, None)
    inp[field] = res.model
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{res.base_url}/v2/consumer/workflows?whatif=true", json=body,
                         headers={"Authorization": f"Bearer {res.api_key}",
                                  "Content-Type": "application/json"})
    total = None
    try:
        total = int(round(r.json()["cost"]["total"]))
    except Exception:
        pass
    return r.status_code, total


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["whatif", "one", "field-probe"])
    ap.add_argument("--model")
    ap.add_argument("--ratio", default="1:1")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    try:
        res = _res(a.model)
    except image_jobs.ImageJobError as e:
        print(f"no key: {e.message}")
        return 1
    spec = image_jobs.ImageSpec(prompt="a red apple on a table, studio light", ratio=a.ratio)
    try:
        if a.cmd == "whatif":
            cost = await ip.estimate_civitai(res, spec)
            print(f"HTTP 200 cost.total={cost}")
            return 0
        if a.cmd == "field-probe":
            ok = []
            for field in ("model", "checkpointModel"):
                status, total = await _whatif_raw(res, field)
                print(f"{field}: HTTP {status} cost.total={total}")
                if status == 200 and total is not None:
                    ok.append(field)
            print("accepted:", ", ".join(ok) or "neither")
            return 0 if ok else 1
        if not a.yes:
            print("refusing: 'one' spends Buzz; pass --yes")
            return 2
        data = (await ip.generate_batch(res, spec, n=1))[0]
        mime = image_jobs.sniff_mime(data) if hasattr(image_jobs, "sniff_mime") else ""
        ext = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".png")
        out = Path(os.environ.get("CIVITAI_SMOKE_OUT") or "./civitai-smoke")
        out = out.with_suffix(ext) if out.suffix not in (".png", ".jpg", ".webp") else out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        print(f"wrote {out} bytes={len(data)} mime={mime or 'validated'}")
        return 0
    except image_jobs.ImageJobError as e:
        print(f"failed: {e.message}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
