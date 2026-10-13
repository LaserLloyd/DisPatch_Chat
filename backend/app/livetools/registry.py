"""Tool registry: OpenAI tool schemas, allowlist check and safe dispatch.

Ported from ChatForge ``src/chatforge/tools/`` (MIT, LaserLloyd) on 2026-10-13: the
eight read-only lookups, without ``create_document``. Keep the modules close to the
originals so fixes travel both ways.

``ToolRegistry.call`` never raises for bad arguments, unknown tools or tool failures; those
come back as ``ToolResult(ok=False)`` so the model can see and correct them. Only
``ToolNotAllowed`` (a known tool that is not enabled) propagates.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("dispatch.livetools")

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


@dataclass
class ToolResult:
    ok: bool
    content: str  # text fed back to the model
    summary: str = ""  # short label for the UI
    #: A file the tool saved for the user: ``{name, path, size, kind}`` (``create_document``).
    document: dict[str, Any] | None = None


class ToolNotAllowed(Exception):
    """The model asked for a tool that is not enabled."""

    code = "tool_not_allowed"


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its OpenAI schema, a UI label, and whether the small local model gets it.

    The local NPU model has a 4096-token prompt window and gets confused by long tool
    lists, so only the ``local`` tools are offered to it; cloud and LAN models get all.
    """

    name: str
    label: str
    schema: dict
    local: bool
    #: Extension tools only (``register_extension``): the callable and the
    #: UI-label callable. Built-ins dispatch through ``ToolRegistry._handlers``.
    fn: Callable[..., Any] | None = field(default=None, compare=False)
    summary: Callable[[dict], str] | None = field(default=None, compare=False)
    ext_id: str = ""


def _spec(
    name: str,
    label: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    local: bool,
) -> ToolSpec:
    return ToolSpec(name, label, _schema(name, description, properties, required), local)


def _str(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


# Canonical order; descriptions are deliberately terse (4096-token NPU prompt window).
_SPECS: dict[str, ToolSpec] = {
    s.name: s
    for s in (
        _spec(
            "web_search",
            "Web search",
            "Search the web for current facts, events and prices.",
            {"query": _str("search query")},
            ["query"],
            local=True,
        ),
        _spec(
            "news_search",
            "News search",
            "Search recent news articles.",
            {"query": _str("news topic")},
            ["query"],
            local=False,
        ),
        _spec(
            "fetch_url",
            "Fetch a web page",
            "Fetch a web page and return its text.",
            {"url": _str("http(s) URL")},
            ["url"],
            local=True,
        ),
        _spec(
            "weather",
            "Weather",
            "Current weather and forecast for a place.",
            {
                "location": _str("city, optionally ', country'; empty = user's home"),
                "days": {"type": "integer", "description": "forecast days 1-7"},
            },
            [],
            local=True,
        ),
        _spec(
            "wikipedia",
            "Wikipedia",
            "Summary of a Wikipedia article.",
            {"topic": _str("article topic")},
            ["topic"],
            local=False,
        ),
        _spec(
            "exchange_rate",
            "Exchange rates",
            "Convert money between currencies (ECB rates).",
            {
                "from": _str("3-letter code, e.g. USD"),
                "to": _str("3-letter code, e.g. JPY"),
                "amount": {"type": "number"},
            },
            ["from", "to"],
            local=False,
        ),
        _spec(
            "current_datetime",
            "Current date and time",
            "Get the current local date, time and timezone.",
            {},
            [],
            local=True,
        ),
        _spec(
            "calculator",
            "Calculator",
            "Evaluate a math expression.",
            {"expression": _str("e.g. sqrt(2)*10")},
            ["expression"],
            local=True,
        ),
    )
}
_SCHEMAS: dict[str, dict] = {name: spec.schema for name, spec in _SPECS.items()}
TOOL_NAMES: tuple[str, ...] = tuple(_SPECS)
LOCAL_TOOL_NAMES: tuple[str, ...] = tuple(n for n, s in _SPECS.items() if s.local)
#: Enabled for a new config (every tool).
DEFAULT_ENABLED: tuple[str, ...] = TOOL_NAMES


# --------------------------------------------------------------------------- #
# Tools from extension packages (ctx.register_live_tools)
# --------------------------------------------------------------------------- #

NAME_RE = re.compile(r"\A[a-z][a-z0-9_]{1,30}\Z")
MAX_DESCRIPTION = 1000

_ext_lock = threading.Lock()
_EXT_SPECS: dict[str, ToolSpec] = {}
_generation = 0


@dataclass
class LiveTool:
    """What an extension hands to ``ctx.register_live_tools`` (a plain dict with
    the same keys works too). ``fn(args: dict)`` is an async callable returning a
    string or a :class:`ToolResult`; ``summary(args)`` labels the notice row."""

    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]
    summary: Callable[[dict], str] | None = None


def validate_live_tools(specs: Any, ext_id: str, *, pending: Iterable[str] = ()) -> list[ToolSpec]:
    """Check an extension's specs and return ready ``ToolSpec`` rows. Raises
    ``ValueError`` with a message fit for the extension's ``detail``. Names may
    not collide with a built-in, another extension's tool, or ``pending`` (this
    extension's earlier calls)."""
    if isinstance(specs, LiveTool | Mapping):
        specs = [specs]
    if not isinstance(specs, list | tuple):
        raise ValueError("register_live_tools takes a list of tool specs")
    taken = set(pending)
    out: list[ToolSpec] = []
    for raw in specs:
        def get(key: str, default: Any = None, _r: Any = raw) -> Any:
            return _r.get(key, default) if isinstance(_r, Mapping) else getattr(_r, key, default)

        name = get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError(f"live tool name {name!r} must match [a-z][a-z0-9_]{{1,30}}")
        if name in _SPECS:
            raise ValueError(f"live tool {name!r} collides with a built-in lookup")
        owner = _EXT_SPECS.get(name)
        if name in taken or (owner is not None and owner.ext_id != ext_id):
            who = f"extension {owner.ext_id!r}" if owner is not None else "this extension"
            raise ValueError(f"live tool {name!r} is already registered by {who}")
        desc = get("description")
        if not isinstance(desc, str) or not desc.strip() or len(desc) > MAX_DESCRIPTION:
            raise ValueError(f"live tool {name!r}: description must be 1-{MAX_DESCRIPTION} characters")
        params = get("parameters")
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            raise ValueError(f"live tool {name!r}: parameters must be a JSON-schema dict")
        try:
            params = json.loads(json.dumps(params))       # a private, JSON-clean copy
        except (TypeError, ValueError):
            raise ValueError(f"live tool {name!r}: parameters must be JSON-serialisable") from None
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        if params["type"] != "object" or not isinstance(params["properties"], dict):
            raise ValueError(f"live tool {name!r}: parameters must be an object schema")
        fn = get("fn")
        if not callable(fn):
            raise ValueError(f"live tool {name!r}: fn must be callable")
        summary = get("summary")
        if summary is not None and not callable(summary):
            raise ValueError(f"live tool {name!r}: summary must be callable")
        taken.add(name)
        out.append(ToolSpec(
            name, name.replace("_", " ").capitalize(),
            {"type": "function",
             "function": {"name": name, "description": desc.strip(), "parameters": params}},
            False, fn=fn, summary=summary, ext_id=ext_id))
    return out


def register_extension(ext_id: str, specs: list[ToolSpec]) -> None:
    """Commit validated specs to the live registry (the host does this once the
    extension's setup succeeded). Re-checks collisions under the lock."""
    global _generation
    with _ext_lock:
        for s in specs:
            owner = _EXT_SPECS.get(s.name)
            if s.name in _SPECS or (owner is not None and owner.ext_id != ext_id):
                raise ValueError(f"live tool {s.name!r} is already registered")
        for s in specs:
            _EXT_SPECS[s.name] = s
        if specs:
            _generation += 1


def unregister_extension(ext_id: str) -> None:
    global _generation
    with _ext_lock:
        gone = [n for n, s in _EXT_SPECS.items() if s.ext_id == ext_id]
        for n in gone:
            del _EXT_SPECS[n]
        if gone:
            _generation += 1


def generation() -> int:
    """Bumps whenever the set of extension tools changes (cache invalidation)."""
    return _generation


def all_tool_names() -> tuple[str, ...]:
    """Built-ins in canonical order, then extension tools in registration order."""
    return TOOL_NAMES + tuple(_EXT_SPECS)


def find_spec(name: str) -> ToolSpec | None:
    return _SPECS.get(name) or _EXT_SPECS.get(name)


def extension_tool_names(ext_id: str) -> list[str]:
    return [n for n, s in _EXT_SPECS.items() if s.ext_id == ext_id]


def tool_catalog() -> list[dict[str, Any]]:
    """``[{name, label, local}]`` in canonical order, for Settings. Extension
    tools follow, tagged with their ``extension`` id."""
    rows: list[dict[str, Any]] = [
        {"name": s.name, "label": s.label, "local": s.local} for s in _SPECS.values()]
    rows += [{"name": s.name, "label": s.label, "local": s.local, "extension": s.ext_id}
             for s in _EXT_SPECS.values()]
    return rows


def assert_tool_allowed(name: str, enabled: Iterable[str]) -> None:
    """Raise ``ToolNotAllowed`` unless ``name`` is in ``enabled``."""
    if name not in set(enabled):
        raise ToolNotAllowed(f"Tool '{name}' is not enabled.")


def _strip_trailing_commas(text: str) -> str:
    """Drop commas directly before ``}`` or ``]``, ignoring anything inside JSON strings."""
    out: list[str] = []
    in_str = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch == ",":
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] in "}]":
                i += 1
                continue
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def parse_arguments(arguments_json: str | None) -> dict[str, Any]:
    """Parse tool-call arguments with a one-pass repair; raise ``ValueError`` if hopeless."""
    text = (arguments_json or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        repaired = text
        m = _FENCE.match(repaired)
        if m:
            repaired = m.group(1).strip()
        repaired = _strip_trailing_commas(repaired)
        try:
            value = json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise ValueError(f"arguments are not valid JSON ({exc.msg})") from None
    if not isinstance(value, dict):
        raise ValueError("arguments must be a JSON object")
    return value


def _opt(cfg: Any, name: str, default: Any) -> Any:
    if cfg is None:
        return default
    if isinstance(cfg, Mapping):
        return cfg.get(name, default)
    return getattr(cfg, name, default)


def _clip(text: str, max_chars: int) -> str:
    if max_chars > 0 and len(text) > max_chars:
        return text[: max(0, max_chars - 1)].rstrip() + "…"
    return text


class ToolRegistry:
    """Owns the tool implementations (and the search cache/spacing state).

    ``cfg`` is a mapping (or object) exposing ``web_search_max_results``,
    ``web_search_min_interval_s``, ``fetch_max_bytes``, ``fetch_timeout_s``,
    ``block_private_addresses``, ``location`` and ``units``; missing values use
    the defaults.
    """

    def __init__(self, cfg: Any = None, *, transport: Any = None) -> None:
        from .web_search import WebSearch

        self._fetch_max_bytes = int(_opt(cfg, "fetch_max_bytes", 1_000_000))
        self._fetch_timeout_s = float(_opt(cfg, "fetch_timeout_s", 10))
        self._block_private = bool(_opt(cfg, "block_private_addresses", True))
        self._location = str(_opt(cfg, "location", "") or "").strip()
        self._units = str(_opt(cfg, "units", "metric") or "metric")
        self._transport = transport
        self._search = WebSearch(
            max_results=int(_opt(cfg, "web_search_max_results", 5)),
            min_interval_s=float(_opt(cfg, "web_search_min_interval_s", 2.0)),
        )
        self._handlers = {
            "web_search": self._web_search,
            "news_search": self._news_search,
            "fetch_url": self._fetch_url,
            "weather": self._weather,
            "wikipedia": self._wikipedia,
            "exchange_rate": self._exchange_rate,
            "current_datetime": self._current_datetime,
            "calculator": self._calculator,
        }

    @property
    def location(self) -> str:
        """The home location from Settings ("" when unset)."""
        return self._location

    def schemas(self, enabled: Iterable[str], *, local: bool = False) -> list[dict]:
        """OpenAI ``tools`` array for the enabled tools (canonical order, unknown names
        ignored). ``local`` keeps only the tools offered to the small local model."""
        wanted = set(enabled)
        names = LOCAL_TOOL_NAMES if local else all_tool_names()
        out = []
        for name in names:
            spec = find_spec(name)
            if spec is not None and name in wanted:
                out.append(spec.schema)
        return out

    async def call(
        self, name: str, arguments_json: str, *, enabled: Iterable[str], max_chars: int
    ) -> ToolResult:
        spec = find_spec(name)
        if spec is None:
            return ToolResult(
                False,
                f"Unknown tool '{name}'. Available tools: {', '.join(all_tool_names())}.",
                f"unknown tool {name}",
            )
        assert_tool_allowed(name, enabled)  # the only exception that may escape
        try:
            args = parse_arguments(arguments_json)
        except ValueError as exc:
            return ToolResult(
                False,
                f"Invalid arguments for {name}: {exc}. Send a JSON object.",
                f"bad arguments for {name}",
            )
        if spec.fn is not None:
            return await self._call_extension(spec, args, max_chars)
        try:
            result = await self._dispatch(name, args, max_chars)
        except Exception as exc:  # a tool bug must not kill the chat turn
            # The model only sees the exception type; the log keeps the traceback.
            log.warning("livetools: %s crashed (%s)", name, type(exc).__name__, exc_info=True)
            return ToolResult(False, f"Tool {name} failed: {type(exc).__name__}.", f"{name} failed")
        result.content = _clip(result.content, max_chars)
        return result

    async def _call_extension(self, spec: ToolSpec, args: dict[str, Any],
                              max_chars: int) -> ToolResult:
        """Run an extension's tool. Same contract as the built-ins: never raises;
        a crash (or a bad return value) becomes a generic failed result, with
        nothing of the exception's message or type shown to the model."""
        name = spec.name
        try:
            out = spec.fn(args)
            if inspect.isawaitable(out):
                out = await out
            if isinstance(out, str):
                result = ToolResult(True, out, _label(spec, args))
            elif isinstance(out, ToolResult):
                result = out
                if not isinstance(result.content, str):
                    raise TypeError("ToolResult.content must be a string")
                if not result.summary:
                    result.summary = _label(spec, args)
            else:
                raise TypeError(f"tool returned {type(out).__name__}")
        except Exception:
            log.warning("livetools: extension tool %s (%s) crashed", name, spec.ext_id,
                        exc_info=True)
            return ToolResult(False, f"Tool {name} failed.", f"{name} failed")
        result.document = None            # an extension tool cannot hand out files
        result.content = _clip(result.content, max_chars)
        return result

    async def _dispatch(self, name: str, args: dict[str, Any], max_chars: int) -> ToolResult:
        return await self._handlers[name](args, max_chars)

    async def _current_datetime(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from . import clock

        return await clock.run()

    async def _calculator(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from . import calculator

        expr = args.get("expression")
        if not isinstance(expr, str):
            return _missing("calculator", "expression")
        return await calculator.run(expr)

    async def _web_search(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _missing("web_search", "query")
        return await self._search.search(query)

    async def _news_search(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _missing("news_search", "query")
        return await self._search.search(query, kind="news")

    async def _fetch_url(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from . import fetch_url

        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            return _missing("fetch_url", "url")
        return await fetch_url.fetch(
            url,
            max_chars=max_chars if max_chars > 0 else 1500,
            max_bytes=self._fetch_max_bytes,
            timeout_s=self._fetch_timeout_s,
            block_private=self._block_private,
            transport=self._transport,
        )

    async def _weather(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from . import weather

        location = args.get("location")
        if location is not None and not isinstance(location, str):
            return _missing("weather", "location")
        days = args.get("days")
        if isinstance(days, str) and days.strip().isdigit():
            days = int(days)
        return await weather.run(
            location,
            days=days if isinstance(days, int) and not isinstance(days, bool) else None,
            units=self._units,
            default_location=self._location,
            transport=self._transport,
        )

    async def _wikipedia(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from . import wikipedia

        topic = args.get("topic") or args.get("query")
        if not isinstance(topic, str) or not topic.strip():
            return _missing("wikipedia", "topic")
        return await wikipedia.run(topic, transport=self._transport)

    async def _exchange_rate(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from . import currency

        return await currency.run(
            args.get("from"), args.get("to"), args.get("amount", 1), transport=self._transport
        )


def _label(spec: ToolSpec, args: dict[str, Any]) -> str:
    if spec.summary is not None:
        try:
            text = spec.summary(args)
            if isinstance(text, str) and text.strip():
                return text.strip()[:120]
        except Exception:
            log.debug("livetools: summary for %s failed", spec.name, exc_info=True)
    return spec.label


def _missing(tool: str, field: str) -> ToolResult:
    return ToolResult(
        False,
        f"Invalid arguments for {tool}: '{field}' (string) is required.",
        f"bad arguments for {tool}",
    )
