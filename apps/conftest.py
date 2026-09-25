"""pytest glue for app packages' own tests (apps/<id>/tests/test_*.py).

`backend/pyproject.toml` collects `../apps` alongside `tests/`, but a conftest
only reaches the tests below its own directory — so without this file an
app's tests would run WITHOUT the backend suite's hermetic guards (the
throwaway DATA_DIR, the synthetic roster, the cheap KDF), which is exactly how
a test ends up writing into a live install. This loads backend/tests/conftest.py
under a private module name and re-exports its fixtures here, so every app test
gets the same autouse fixtures the backend tests do. One source of truth.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
for _p in (_BACKEND / "tests", _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_spec = importlib.util.spec_from_file_location(
    "_dispatch_backend_conftest", _BACKEND / "tests" / "conftest.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})

# Importing the shell mounts every repo app, which registers each package as
# `dispatch_app_<id>` — so an app's tests may simply `import dispatch_app_<id>`.
from app import main as _dispatch_main  # noqa: E402,F401
