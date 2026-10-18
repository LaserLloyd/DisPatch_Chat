"""Agent dispatch core: the AgentBackend interface, normalized events, and adapters.

A self-contained library. Importing this package imports NOTHING heavy: no
``app.main``, no backend SDK, no websocket client - adapters import what they
need when they are used. W11b wires it into the app (routes, DB, UI).

Design notes live in docs/agents/backends.md and docs/agents/security.md; every
decision there cites RESEARCH.md by item id.
"""
from __future__ import annotations

from .budget import Budget, BudgetTracker
from .events import (
    ErrorCode,
    EventLog,
    TaskState,
    parse_event,
)
from .interface import (
    AgentBackend,
    AgentBackendError,
    AgentSpec,
    ApprovalPolicy,
    BaseBackend,
    Capabilities,
    McpServerSpec,
    SessionInfo,
    TaskContext,
    TaskInfo,
    WorkspacePolicy,
)

__all__ = [
    "AgentBackend", "AgentBackendError", "AgentSpec", "ApprovalPolicy", "BaseBackend", "Budget",
    "BudgetTracker", "Capabilities", "ErrorCode", "EventLog", "McpServerSpec", "SessionInfo",
    "TaskContext", "TaskInfo", "TaskState", "WorkspacePolicy", "parse_event",
]
