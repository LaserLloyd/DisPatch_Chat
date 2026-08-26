"""pool_guard.py — VRAM headroom guard for the reaction/avatar pool refills.

The pools mint images on an image-generation host that may share its GPUs
with an LLM server. When a large model is loaded across those GPUs it leaves
the image backend (the image CLI and the REMOTE rig's ComfyUI behind it, which
needs ~8-20 GB free and selects its GPU at ">= 10.0 GB free") nothing to work
with, and every mint is
refused. A tight refill loop then burns hundreds of rapid silent refusals in
a night, which is the failure this module exists to prevent.

This module gives the refill three things it lacked:

  (a) a VRAM headroom check BEFORE minting, freeing VRAM when short by
      unloading non-pinned, non-active LLM models through the host's
      companion CLI (the same mechanism its idle TTL uses; pinned models and
      models with in-flight requests are never touched);
  (b) a shared refusal tally (deque + consecutive counter) so callers can
      back off exponentially instead of tight-looping refused calls;
  (c) a loud journal alert once consecutive failures pass a threshold,
      plus a 24h counter surfaced on /api/health.

Everything is best-effort and fails OPEN: if the GPU CLI (``DISPATCH_GPU_CLI``)
is missing or the host can't be reached we say "gpu-cli-unavailable" and the
refill proceeds exactly as before — a monitor outage must never stall the
pools. Installs without a shared GPU host never notice this module.

Environment
-----------
``DISPATCH_POOL_FREE_VRAM``
    ``0`` (default) — check headroom but NEVER unload anything. When the mint
    GPU is short the round is simply skipped. This is the right default: the
    host's GPUs are usually shared with somebody's working LLM, and evicting
    their model to make room for a decorative image is not this feature's call.
    ``1`` — allow the guard to unload non-pinned, idle models to make room.
``DISPATCH_MINT_MIN_FREE_GB``
    Free GB required on the mint GPU before a refill round (default
    :data:`MIN_MINT_FREE_GB`).
``DISPATCH_GPU_CLI`` / ``DISPATCH_IMAGE_CLI``
    Explicit paths to the companion CLIs, when they are not on ``PATH``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from collections import deque
from pathlib import Path

from . import config

log = logging.getLogger("pool_guard")

# The remote rig ComfyUI's own GPU-selection threshold
# ("31.4 GB free >= 10.0 needed"), as reported by the image CLI.
MIN_MINT_FREE_GB = 10.0
# Number of consecutive refill failures before we log an ERROR (the alert).
ALERT_AFTER = 3
# Backoff: 300s base (the pool loop's normal poll), doubling, capped at 1h.
_BACKOFF_BASE_S = 300.0
_BACKOFF_CAP_S = 3600.0

_REFILL_FAILURES: deque = deque(maxlen=200)
_consecutive = 0


# --------------------------------------------------------------------------- #
# GPU CLI / image CLI plumbing (subprocess; never raises)
# --------------------------------------------------------------------------- #


def gpu_cli_bin() -> str | None:
    """The optional GPU-host companion CLI. Configured, never guessed: set
    ``DISPATCH_GPU_CLI`` to a binary that answers ``status --json`` (GPU free
    memory + loaded models), ``models settings --json -- <id>`` and
    ``models unload --json -- <id>``. Unset = the VRAM guard is off."""
    return config.env("GPU_CLI") or None


def _image_cli_bin() -> str | None:
    """The image CLI, from ``DISPATCH_IMAGE_CLI`` — a bare name resolved on PATH
    or an absolute path. Never guessed: the guard shells out to whatever this
    names, so a chance PATH match on an unrelated binary would be worse than
    having no guard at all. Unset = the guard simply has no image-host view."""
    name = config.env("IMAGE_CLI")
    if not name:
        return None
    if os.sep in name:
        return name if Path(name).is_file() else None
    return shutil.which(name)


def _run_json(argv: list[str], timeout: float = 15.0) -> dict | None:
    """Run a CLI, parse stdout as JSON. None on any failure (never raises)."""
    if not argv:
        return None
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
        return json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
        log.warning("pool_guard: %s failed: %s", argv[0], e)
        return None


def gpu_status() -> dict | None:
    bin_ = gpu_cli_bin()
    if not bin_:
        log.debug("pool_guard: no GPU CLI configured (DISPATCH_GPU_CLI) — VRAM guard off")
        return None
    return _run_json([bin_, "status", "--json"])


def _image_cli_status() -> dict | None:
    bin_ = _image_cli_bin()
    if not bin_:
        return None
    return _run_json([bin_, "--status"])


# --------------------------------------------------------------------------- #
# GPU leases — somebody else has booked the whole rig
# --------------------------------------------------------------------------- #


def lease_url() -> str | None:
    """The image host's lease endpoint, from ``DISPATCH_GPU_LEASE_URL``.

    Configured, never guessed. Unset = no lease view and the guard behaves
    exactly as it always did.
    """
    return config.env("GPU_LEASE_URL") or None


def rig_lease_holder() -> str | None:
    """Who holds an exclusive lease on the image host's GPUs, or None.

    A lease means another tenant (a benchmark run, a long job) has booked the
    cards and expects nothing to be planned onto or evicted from them for the
    duration. Minting a decorative image into that is exactly the eviction the
    lease exists to prevent -- and this guard is allowed to UNLOAD models when
    DISPATCH_POOL_FREE_VRAM is on, so without this check a nightly top-up
    could throw out the very model the lease was protecting.

    Fails OPEN on every error (unreachable host, bad JSON, timeout): a lease
    view we cannot read must not become an outage for the pools.
    """
    url = lease_url()
    if not url:
        return None
    try:
        import urllib.request
        # `url` is built from our own config (lease_url()), never from a request,
        # and the scheme is fixed there -- so the audit rule about opening a
        # caller-supplied URL does not apply. (That rule's family is not
        # enabled, so the reason lives in prose rather than a suppression.)
        with urllib.request.urlopen(url, timeout=5.0) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as e:
        # Blind `except Exception` on purpose (see the docstring): every failure
        # mode here must fail OPEN, or an unreachable rig becomes a pool outage.
        log.debug("pool_guard: lease check failed (%s) — assuming unleased", e)
        return None
    for lease in (data.get("leases") or []):
        holder = str(lease.get("holder") or "").strip()
        if holder:
            return holder[:80]
    return None


# --------------------------------------------------------------------------- #
# VRAM headroom — the GPU the next mint will land on
# --------------------------------------------------------------------------- #


def _gpu_free_map(status: dict | None) -> dict[int, float]:
    out: dict[int, float] = {}
    for g in (status or {}).get("gpus") or []:
        try:
            idx = int(g["index"])
            free = float(g.get("free_bytes") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        out[idx] = free / 1e9
    return out


def mint_gpu_free_gb() -> tuple[float, str] | None:
    """Free GB on the GPU the next mint will use, and which GPU that is.

    Prefers the image CLI's own ``gpu_selected``: that is the GPU the remote
    rig's ComfyUI will actually draw on, and on a multi-GPU host the one it picks can be the one
    an LLM is occupying while the others sit idle — so "is there free VRAM
    somewhere" is the wrong question. Falls back to the best free GPU when
    that view can't be read. None = can't tell (fail open).
    """
    free = _gpu_free_map(gpu_status())
    if not free:
        return None
    sel: int | None = None
    cf = _image_cli_status()
    if cf:
        try:
            sel = int(cf.get("comfy", {}).get("gpu_selected"))
        except (TypeError, ValueError):
            sel = None
    if sel is not None and sel in free:
        return free[sel], f"selected:gpu{sel}"
    best = max(free, key=free.get)
    return free[best], f"best:gpu{best}"


# --------------------------------------------------------------------------- #
# Free VRAM before a mint
# --------------------------------------------------------------------------- #


def _safe_model_id(model_id: str) -> bool:
    """A model id we are willing to pass to the companion CLI.

    The id arrives in the host's status JSON, so it is only as trustworthy as
    the host. An id starting with a dash would be parsed as an OPTION rather
    than an argument, so it is refused outright; the argv below also ends
    option parsing with ``--`` before the positional.
    """
    mid = str(model_id or "").strip()
    return bool(mid) and not mid.startswith("-")


def unload_enabled() -> bool:
    """Whether this install may unload the image host's LLM models.

    Off by default: see free_vram_before_mint. Set
    ``DISPATCH_POOL_FREE_VRAM=1`` on a host whose GPUs belong to DisPatch.
    """
    return config.env("POOL_FREE_VRAM", "0").strip().lower() \
        not in ("", "0", "false", "no", "off")


def _model_pinned(model_id: str) -> bool:
    bin_ = gpu_cli_bin()
    if not bin_ or not _safe_model_id(model_id):
        return False
    s = _run_json([bin_, "models", "settings", "--json", "--", model_id])
    return bool(s and s.get("pinned"))


def _unload_model(model_id: str) -> bool:
    bin_ = gpu_cli_bin()
    if not bin_ or not _safe_model_id(model_id):
        return False
    s = _run_json([bin_, "models", "unload", "--json", "--", model_id],
                  timeout=60.0)
    return bool(s and s.get("unloaded"))


def free_vram_before_mint(min_free_gb: float | None = None, *,
                          dry_run: bool = False) -> dict:
    """Ensure headroom on the mint GPU before a refill round.

    Returns {"ok", "headroom_before", "headroom_after", "unloaded",
             "skipped_pinned", "skipped_active", "reason"}.

    ok=False → caller must NOT mint this round (rig genuinely short).
    ok=True with reason "gpu-cli-unavailable" → fail open, mint as before.
    dry_run=True → only reports what it WOULD unload; never touches the rig
    (this is the evidence/dry-run path — production calls dry_run=False).

    UNLOADING IS OPT-IN. The image host is usually somebody else's working GPU
    too, and evicting their loaded model to make room for a decorative avatar
    is not a decision this feature gets to make silently. With
    ``DISPATCH_POOL_FREE_VRAM`` unset or "0" (the default) the check still
    runs — it still says "don't mint, the rig is short", which is the part
    that stops the refusal storm — but nothing is ever unloaded.
    """
    if min_free_gb is None:
        min_free_gb = float(config.env("MINT_MIN_FREE_GB",
                                           MIN_MINT_FREE_GB))
    # BEFORE anything else, including the headroom probe: a leased rig is
    # off-limits no matter how much VRAM happens to be free, because the
    # holder booked the cards, not the spare bytes.
    holder = rig_lease_holder()
    if holder:
        return {"ok": False, "headroom_before": None, "headroom_after": None,
                "unloaded": [], "skipped_pinned": [], "skipped_active": [],
                "reason": f"rig leased by {holder}"}
    probe = mint_gpu_free_gb()
    if probe is None:
        return {"ok": True, "headroom_before": None, "headroom_after": None,
                "unloaded": [], "skipped_pinned": [], "skipped_active": [],
                "reason": "gpu-cli-unavailable"}
    before, which = probe
    if before >= min_free_gb:
        return {"ok": True, "headroom_before": before, "headroom_after": before,
                "unloaded": [], "skipped_pinned": [], "skipped_active": [],
                "reason": f"headroom-ok ({before:.1f} GB on {which})"}

    if not (dry_run or unload_enabled()):
        return {"ok": False, "headroom_before": before, "headroom_after": before,
                "unloaded": [], "skipped_pinned": [], "skipped_active": [],
                "reason": (f"short ({before:.1f} GB on {which}); unloading is "
                           "off (DISPATCH_POOL_FREE_VRAM=1 to enable)")}

    status = gpu_status()
    unloaded: list[str] = []
    skipped_pinned: list[str] = []
    skipped_active: list[str] = []
    for m in (status or {}).get("loaded") or []:
        mid = m.get("model_id")
        if not mid or not _safe_model_id(mid):
            continue
        if (m.get("active_requests") or 0) > 0:
            skipped_active.append(mid)
            continue
        if _model_pinned(mid):
            skipped_pinned.append(mid)
            continue
        if not dry_run:
            if _unload_model(mid):
                unloaded.append(mid)
        else:
            unloaded.append(mid)   # report-only

    after_probe = mint_gpu_free_gb()
    after = after_probe[0] if after_probe else None
    ok = after is not None and after >= min_free_gb
    reason = f"unloaded {len(unloaded)} non-pinned model(s)"
    if skipped_pinned:
        reason += f"; {len(skipped_pinned)} pinned kept"
    if skipped_active:
        reason += f"; {len(skipped_active)} in-use kept"
    if after is not None and not ok:
        reason += f"; STILL SHORT ({after:.1f} GB < {min_free_gb:.1f} GB)"
    if dry_run:
        reason = f"DRY-RUN would unload {len(unloaded)}; " + reason
    return {"ok": ok, "headroom_before": before, "headroom_after": after,
            "unloaded": unloaded, "skipped_pinned": skipped_pinned,
            "skipped_active": skipped_active, "reason": reason}


# --------------------------------------------------------------------------- #
# Refusal tally + backoff (mirrors note_fire_failure/fire_failure_stats)
# --------------------------------------------------------------------------- #


def note_refill_failure(kind: str, reason: str, *, actor: str = "") -> None:
    global _consecutive
    _consecutive += 1
    _REFILL_FAILURES.append({"at": time.time(), "kind": str(kind)[:40],
                             "reason": str(reason)[:200], "actor": str(actor)[:60]})


def note_refill_success() -> None:
    global _consecutive
    _consecutive = 0


def consecutive_failures() -> int:
    return _consecutive


def refill_failure_stats(window_s: float = 24 * 3600.0) -> dict:
    cutoff = time.time() - window_s
    recent = [f for f in _REFILL_FAILURES if f["at"] >= cutoff]
    return {"failures_24h": len(recent), "consecutive": _consecutive,
            "recent": recent[-10:]}


def backoff_s(consecutive: int) -> float:
    if consecutive <= 0:
        return _BACKOFF_BASE_S
    return min(_BACKOFF_BASE_S * (2 ** (consecutive - 1)), _BACKOFF_CAP_S)
