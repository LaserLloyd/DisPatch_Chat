"""The optional ``window`` block of an app manifest / tools.yaml row.

One validator for both callers (apps_loader, tools); each wraps the ValueError
in its own error type. Design: docs/design/app-windows.md section 6.
"""
from __future__ import annotations

from typing import Any

LAYOUTS = ("float", "left", "right", "full")
_INT_RANGES = {
    "width": (320, 3840),
    "height": (240, 2160),
    "min_width": (240, 1600),
    "min_height": (200, 1200),
}
_BOOLS = ("dockable", "keep_alive")
WINDOW_KEYS = frozenset({"layout", *_INT_RANGES, *_BOOLS})


def default_window() -> dict:
    return {"layout": "float", "min_width": 360, "min_height": 320,
            "dockable": True, "keep_alive": True}


def parse_window(value: Any) -> dict:
    """A raw ``window`` value -> the normalized dict (all defaults filled;
    width/height only when given). None -> the defaults. Raises ValueError."""
    out = default_window()
    if value is None:
        return out
    if not isinstance(value, dict):
        raise ValueError("must be a mapping")
    for k in value:
        if k not in WINDOW_KEYS:
            raise ValueError(f"unknown key {str(k)!r}")
    if "layout" in value:
        v = value["layout"]
        if not isinstance(v, str) or v not in LAYOUTS:
            raise ValueError("layout must be float, left, right or full")
        out["layout"] = v
    for k, (lo, hi) in _INT_RANGES.items():
        if k in value:
            v = value[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{k} must be an integer {lo}..{hi}")
            out[k] = v
    for k in _BOOLS:
        if k in value:
            if not isinstance(value[k], bool):
                raise ValueError(f"{k} must be true or false")
            out[k] = value[k]
    # A minimum the author set must fit the size they set; a default minimum
    # simply follows a smaller size (width 320 is in range, so it is honoured).
    for size, low in (("width", "min_width"), ("height", "min_height")):
        if size in out and out[low] > out[size]:
            if low in value:
                raise ValueError(f"{low} must not exceed {size}")
            out[low] = out[size]
    return out


def non_default(window: dict) -> dict:
    """Only what differs from the defaults (what to_manifest writes)."""
    d = default_window()
    return {k: v for k, v in window.items() if k not in d or d[k] != v}
