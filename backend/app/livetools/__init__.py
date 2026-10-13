"""Live lookups an advisor bot can call itself (OpenAI tool calling).

Ported from ChatForge ``src/chatforge/tools/`` (MIT, LaserLloyd), 2026-10-13: web and
news search (DuckDuckGo via ``ddgs``), ``fetch_url`` (SSRF-hardened), ``weather``
(Open-Meteo, no key), ``wikipedia`` (online summary), ``exchange_rate`` (ECB via
Frankfurter), ``current_datetime`` and ``calculator``. Every tool is read-only and
talks to a fixed public service; none can run a program or touch the box. The
``create_document`` tool was left behind on purpose.
"""
from .registry import (  # noqa: F401
    DEFAULT_ENABLED, TOOL_NAMES, ToolNotAllowed, ToolRegistry, ToolResult,
    parse_arguments, tool_catalog,
)
