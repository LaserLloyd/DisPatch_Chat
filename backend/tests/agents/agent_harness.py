"""One harness per adapter, so the contract suite is written once.

A harness builds an adapter whose underlying world (the scripted fake, the fake
gateway, the fake SDK transport) answers ``"scenario:<name>"`` prompts the same
way the others do. The contract tests never learn which adapter they are on.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_fakes import FAKE_API_KEY, FAKE_GATEWAY_TOKEN, FakeGateway, FakeSdkTransport

from app.agents.budget import Budget
from app.agents.claude_sdk_adapter import ClaudeAgentBackend
from app.agents.fake import FakeBackend
from app.agents.interface import AgentSpec, ApprovalPolicy, TaskContext
from app.agents.openclaw_adapter import OpenClawBackend
from app.agents.secrets import MemorySecretProvider, SecretRedactor

ADAPTERS = ("fake", "openclaw", "claude_sdk")


class Harness:
    name = ""
    #: scenarios this adapter can act out (others are skipped with a reason in the suite)
    scenarios: frozenset[str] = frozenset()

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.data_dir = tmp_path / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.redactor = SecretRedactor()
        self.backend: Any = None

    async def setup(self) -> Any:
        raise NotImplementedError

    async def teardown(self) -> None:
        if self.backend is not None:
            await self.backend.aclose()

    def prepare(self, scenario: str) -> None:
        """Hook for adapters whose failure is configured at connection time."""

    def spec(self, **over: Any) -> AgentSpec:
        base: dict[str, Any] = {
            "backend": self.name, "name": "contract", "tool_allowlist": ["Read", "Bash"],
            "approval_policy": ApprovalPolicy(), "budget": Budget(max_usd=1.0, max_wall_clock_s=None)}
        base.update(over)
        return AgentSpec(**base)

    def ctx(self, **over: Any) -> TaskContext:
        return TaskContext(data_dir=self.data_dir, secrets=MemorySecretProvider(
            {"ANTHROPIC_API_KEY": FAKE_API_KEY}), **over)


class FakeHarness(Harness):
    name = "fake"
    scenarios = frozenset({"simple", "tools", "approval", "subagent", "retry", "long", "budget",
                           "error_unavailable", "error_refused", "error_auth", "crash", "artifact"})

    async def setup(self) -> Any:
        self.backend = FakeBackend(redactor=self.redactor)
        return self.backend


class OpenClawHarness(Harness):
    name = "openclaw"
    scenarios = frozenset({"simple", "tools", "long", "budget", "error_unavailable", "error_refused",
                           "error_auth"})

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.gateway = FakeGateway()

    async def setup(self) -> Any:
        url = await self.gateway.start()
        self.backend = OpenClawBackend(gateway_url=url, token=FAKE_GATEWAY_TOKEN, transport="gateway",
                                       redactor=self.redactor, probe_timeout_s=3.0, connect_timeout_s=5.0)
        self.backend.ABORT_GRACE_S = 3.0
        return self.backend

    def prepare(self, scenario: str) -> None:
        if scenario == "error_auth":
            self.gateway.token = "a-different-token"           # the gateway rejects ours

    async def teardown(self) -> None:
        await super().teardown()
        await self.gateway.stop()


class ClaudeHarness(Harness):
    name = "claude_sdk"
    scenarios = frozenset({"simple", "tools", "approval", "subagent", "retry", "long", "budget",
                           "error_unavailable", "error_refused", "error_auth"})

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.transports: list[FakeSdkTransport] = []

    def _factory(self, rt: Any, ws: Any) -> FakeSdkTransport:
        t = FakeSdkTransport(session_id=f"sdk-sess-{rt.task_id}")
        self.transports.append(t)
        return t

    async def setup(self) -> Any:
        self.backend = ClaudeAgentBackend(transport_factory=self._factory, redactor=self.redactor)
        self.backend.ABORT_GRACE_S = 3.0
        return self.backend


HARNESSES = {"fake": FakeHarness, "openclaw": OpenClawHarness, "claude_sdk": ClaudeHarness}
