"""Fake extensions that register live tools (test_extension_live_tools.py).

Not a pytest module. Each ``*_EXT`` is the metadata dict an extension exports;
``setup`` does what ``MODE["setup"]`` says so one entry point can play several
parts.
"""
from __future__ import annotations

from app import livetools

DICER = {"id": "dicer", "name": "Dicer", "version": "1.0.0", "api": [1, 1],
         "setup": "_extfake_tools:setup", "env": []}
OTHER = {**DICER, "id": "other-dicer", "name": "Other Dicer"}

MODE: dict = {"setup": "dice"}
CALLS: list[dict] = []
CTX: list = []


async def roll(args: dict) -> str:
    CALLS.append(dict(args))
    return f"Rolled {args.get('sides', 6)}: 4"


async def crash(args: dict) -> str:
    raise RuntimeError("secret internals at a private path")


async def big(args: dict) -> str:
    return "x" * 10_000


async def rich(args: dict) -> livetools.ToolResult:
    return livetools.ToolResult(True, "rich body", "Rich label")


def dice_spec(**over) -> dict:
    spec = {"name": "dice", "description": "Roll an n-sided die.",
            "parameters": {"type": "object", "properties": {"sides": {"type": "integer"}}},
            "fn": roll, "summary": lambda a: f"Dice: d{a.get('sides', 6)}"}
    spec.update(over)
    return spec


def setup(ctx) -> None:
    CTX.append(ctx)
    mode = MODE["setup"]
    if mode == "dice":
        ctx.register_live_tools([dice_spec()])
    elif mode == "several":
        ctx.register_live_tools([dice_spec(),
                                 dice_spec(name="boom", fn=crash, summary=None),
                                 dice_spec(name="huge", fn=big, summary=None),
                                 dice_spec(name="rich_tool", fn=rich, summary=None)])
    elif mode == "builtin_clash":
        ctx.register_live_tools([dice_spec(name="weather")])
    elif mode == "bad_name":
        ctx.register_live_tools([dice_spec(name="Dice Roller!")])
    elif mode == "twice":
        ctx.register_live_tools([dice_spec()])
        ctx.register_live_tools([dice_spec()])
    elif mode == "bad_fn":
        ctx.register_live_tools([dice_spec(fn="not callable")])
    elif mode == "bad_params":
        ctx.register_live_tools([dice_spec(parameters={"type": "object", "properties": {"x": object()}})])
    elif mode == "dataclass":
        ctx.register_live_tools([livetools.LiveTool("coin", "Flip a coin.", {}, roll)])
    elif mode == "late":
        ctx.on_startup(lambda: None)
    else:                                             # pragma: no cover
        raise AssertionError(mode)
