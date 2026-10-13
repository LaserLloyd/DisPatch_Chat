"""Fixtures for the ported ChatForge tool tests (tests/fakes/clock.py there)."""
import asyncio

import pytest


class FakeClock:
    def __init__(self, start_time: float = 0.0):
        self._now = float(start_time)

    @property
    def now(self) -> float:
        return self._now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


@pytest.fixture
def fake_clock():
    return FakeClock()
