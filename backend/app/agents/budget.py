"""Budgets: per-task limits and the tracker adapters feed.

The brief: "per agent and per profile - USD/day, max turns, max wall-clock,
token cap. Hard stop plus a notification at the limit. Costs are labeled
'estimate' (SDK/CLI cost figures are client-side)."

What this module owns: the :class:`Budget` model, the per-task
:class:`BudgetTracker` (pure, injectable clock) and the arithmetic for turning a
budget into the cap an SDK is given. What it does NOT own: the per-day ledger -
spend history needs the DB, so W11b passes ``spent_today_usd`` in.

Design points, all from RESEARCH.md:

* ``max_budget_usd`` is enforced client-side and CAN OVERSHOOT (R-COST), so the
  SDK is given ``SDK_BUDGET_HEADROOM`` of the cap and the adapter re-checks the
  reported total against 100% (section 5 item 9).
* A resumed SDK session restores its earlier totals but they do NOT count toward
  the new run's ``max_budget_usd`` (R-COST). The tracker therefore keeps the
  task-lifetime total itself and :func:`sdk_run_cap` hands the SDK only the
  REMAINING budget on resume.
* Token and wall-clock caps exist because not every backend reports money
  (OpenClaw reports tokens only).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Fraction of the USD cap handed to the SDK so its own overshoot still lands
# under the cap we enforce (RESEARCH.md R-COST, section 5 item 9).
SDK_BUDGET_HEADROOM = 0.9

BudgetDimension = Literal["usd", "usd_per_day", "turns", "wall_clock", "tokens"]


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_usd: float | None = Field(default=None, gt=0)            # per task
    max_usd_per_day: float | None = Field(default=None, gt=0)    # per agent/profile; ledger is W11b's
    max_turns: int | None = Field(default=None, gt=0)
    max_wall_clock_s: float | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)

    def is_unbounded(self) -> bool:
        return all(v is None for v in (self.max_usd, self.max_usd_per_day, self.max_turns,
                                       self.max_wall_clock_s, self.max_tokens))


@dataclass(frozen=True)
class BudgetVerdict:
    dimension: BudgetDimension
    limit: float
    used: float

    @property
    def message(self) -> str:
        return f"budget exceeded ({self.dimension}: used {self.used:g} of {self.limit:g})"


def effective_usd_cap(budget: Budget, spent_today_usd: float = 0.0) -> float | None:
    """The tightest USD ceiling for one task: per-task cap vs what is left today."""
    caps: list[float] = []
    if budget.max_usd is not None:
        caps.append(budget.max_usd)
    if budget.max_usd_per_day is not None:
        caps.append(max(0.0, budget.max_usd_per_day - max(0.0, spent_today_usd)))
    return min(caps) if caps else None


def sdk_run_cap(budget: Budget, *, spent_task_usd: float = 0.0,
                spent_today_usd: float = 0.0) -> float | None:
    """The ``max_budget_usd`` to hand the SDK for THIS run.

    Remaining budget (resumed sessions do not count earlier spend, R-COST) times
    the overshoot headroom. Never negative; a zero cap means "refuse to start".
    """
    cap = effective_usd_cap(budget, spent_today_usd)
    if cap is None:
        return None
    remaining = max(0.0, cap - max(0.0, spent_task_usd))
    return round(remaining * SDK_BUDGET_HEADROOM, 6)


class BudgetTracker:
    """Accumulates one task's usage and says when a limit is crossed.

    The wall clock is only started by :meth:`start` and is task-lifetime
    cumulative across runs (``pause`` / ``start`` bracket each run), so an
    interrupted-and-resumed task cannot reset its time budget by resuming.
    """

    def __init__(self, budget: Budget, *, clock: Callable[[], float] = time.monotonic,
                 spent_today_usd: float = 0.0) -> None:
        self.budget = budget
        self._clock = clock
        self.spent_today_usd = spent_today_usd
        self.usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.turns = 0
        self._elapsed = 0.0
        self._started_at: float | None = None
        self.has_cost = False          # did any backend report money?

    # -- clock -------------------------------------------------------------- #

    def start(self) -> None:
        if self._started_at is None:
            self._started_at = self._clock()

    def reset_clock(self, elapsed_s: float = 0.0) -> None:
        """Start the wall clock over (a chat turn has its own timeout) or restore what a restart lost."""
        self._elapsed = max(0.0, float(elapsed_s))
        self._started_at = None

    def pause(self) -> None:
        if self._started_at is not None:
            self._elapsed += self._clock() - self._started_at
            self._started_at = None

    @property
    def elapsed_s(self) -> float:
        live = (self._clock() - self._started_at) if self._started_at is not None else 0.0
        return self._elapsed + live

    def remaining_wall_clock_s(self) -> float | None:
        if self.budget.max_wall_clock_s is None:
            return None
        return max(0.0, self.budget.max_wall_clock_s - self.elapsed_s)

    # -- usage -------------------------------------------------------------- #

    def record_total_cost(self, usd: float | None) -> None:
        """A backend reported the session's running TOTAL (SDK ``total_cost_usd``)."""
        if usd is None:
            return
        self.has_cost = True
        self.usd = max(self.usd, float(usd))

    def record_tokens(self, input_tokens: int | None = None,
                      output_tokens: int | None = None, *, total: bool = False) -> None:
        """Add token usage (``total=True``: values are running totals, not deltas)."""
        if total:
            self.input_tokens = max(self.input_tokens, int(input_tokens or 0))
            self.output_tokens = max(self.output_tokens, int(output_tokens or 0))
        else:
            self.input_tokens += int(input_tokens or 0)
            self.output_tokens += int(output_tokens or 0)

    def record_turns(self, turns: int | None) -> None:
        if turns is not None:
            self.turns = max(self.turns, int(turns))

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    # -- verdict ------------------------------------------------------------ #

    def check(self) -> BudgetVerdict | None:
        """The first limit that is crossed, else None. ``>`` not ``>=``: spending
        exactly the cap is within budget; only going past it is a stop."""
        b = self.budget
        if b.max_usd is not None and self.usd > b.max_usd:
            return BudgetVerdict("usd", b.max_usd, self.usd)
        if b.max_usd_per_day is not None and self.spent_today_usd + self.usd > b.max_usd_per_day:
            return BudgetVerdict("usd_per_day", b.max_usd_per_day, self.spent_today_usd + self.usd)
        if b.max_tokens is not None and self.tokens > b.max_tokens:
            return BudgetVerdict("tokens", b.max_tokens, self.tokens)
        if b.max_turns is not None and self.turns > b.max_turns:
            return BudgetVerdict("turns", b.max_turns, self.turns)
        if b.max_wall_clock_s is not None and self.elapsed_s > b.max_wall_clock_s:
            return BudgetVerdict("wall_clock", b.max_wall_clock_s, self.elapsed_s)
        return None

    def snapshot(self) -> dict[str, float | int | None]:
        return {"usd": round(self.usd, 6) if self.has_cost else None, "tokens": self.tokens,
                "turns": self.turns, "elapsed_s": round(self.elapsed_s, 3), "estimate": True}
