"""The scale benchmark is registered and opt-in (BUILD-PLAN-v2 §1.13.2): a plain run must stay
fast, and `-m scale` must select it."""
from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_the_scale_marker_is_registered_and_deselected_by_default():
    cfg = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("scale:") for m in cfg["markers"])
    assert "-m 'not scale'" in cfg["addopts"]

