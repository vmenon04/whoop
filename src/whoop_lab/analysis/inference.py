"""Randomization inference for a permuted-block design.

The test re-draws assignments with the same scheme that produced the real ones:
the same blocks, block size and strata. A plain shuffle of labels would be the
wrong reference distribution for this design.

This is a vectorised equivalent of the reference implementation in the
experiment-analysis skill; tests/test_inference.py checks the two agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

type FloatArray = NDArray[np.float64]
type IntArray = NDArray[np.int64]

_TIE_TOLERANCE = 1e-12


@dataclass(frozen=True)
class BlockDesign:
    """Which block and position each analysed day occupies.

    Built from the stored `stratum`, `block_index` and `block_position`, never
    inferred from dates: washout days leave gaps in the calendar but not in a block.
    """

    block_of_day: IntArray
    position: IntArray
    block_size: int

    def __post_init__(self) -> None:
        if self.block_size < 2 or self.block_size % 2:
            raise ValueError("block_size must be a positive even number")
        if len(self.block_of_day) != len(self.position):
            raise ValueError("block_of_day and position must have the same length")
        if len(self.position) and (
            self.position.min() < 0 or self.position.max() >= self.block_size
        ):
            raise ValueError("block position out of range")
        slots = set(zip(self.block_of_day.tolist(), self.position.tolist(), strict=True))
        if len(slots) != len(self.position):
            raise ValueError("two days occupy the same block position")

    @property
    def n_days(self) -> int:
        return len(self.block_of_day)

    @classmethod
    def contiguous(cls, n_days: int, block_size: int) -> BlockDesign:
        days = np.arange(n_days, dtype=np.int64)
        return cls(days // block_size, days % block_size, block_size)

    @classmethod
    def from_assignments(
        cls, strata: list[str], block_index: list[int], block_position: list[int], block_size: int
    ) -> BlockDesign:
        keys = sorted(set(zip(strata, block_index, strict=True)))
        dense = {key: i for i, key in enumerate(keys)}
        blocks = np.array(
            [dense[key] for key in zip(strata, block_index, strict=True)], dtype=np.int64
        )
        return cls(blocks, np.array(block_position, dtype=np.int64), block_size)

    def draw(self, rng: np.random.Generator, draws: int) -> NDArray[np.int8]:
        """`draws` assignment vectors (rows) from the permuted-block scheme. 1 = treatment."""
        n_blocks = int(self.block_of_day.max()) + 1 if self.n_days else 0
        half = self.block_size // 2
        base = np.array([1] * half + [0] * half, dtype=np.int8)
        blocks = np.broadcast_to(base, (draws, n_blocks, self.block_size)).copy()
        permuted = rng.permuted(blocks, axis=2)
        result: NDArray[np.int8] = permuted[:, self.block_of_day, self.position]
        return result


def _difference_in_means(
    arms: NDArray[Any], values: FloatArray, observed: NDArray[np.bool_]
) -> FloatArray:
    """mean(values | arm 1) - mean(values | arm 0) per row of `arms`, over observed days.
    NaN for a row in which either arm has no observed day."""
    masked = np.where(observed, values, 0.0)
    treated = arms.astype(np.float64) * observed
    n_treated = treated.sum(axis=1)
    n_control = observed.sum() - n_treated
    sum_treated = treated @ masked
    sum_control = masked.sum() - sum_treated
    with np.errstate(divide="ignore", invalid="ignore"):
        stat = sum_treated / n_treated - sum_control / n_control
    stat[(n_treated == 0) | (n_control == 0)] = np.nan
    result: FloatArray = stat
    return result


@dataclass(frozen=True)
class TestResult:
    effect: float | None
    p_value: float | None
    ci_low: float | None
    ci_high: float | None
    n_treatment: int
    n_control: int
    draws: int

    __test__ = False  # not a pytest class


class RandomizationTest:
    """Difference in means by assigned arm, with a re-randomization p-value.

    y: outcome per analysed day, NaN where missing. arm: 1 treatment, 0 control.
    Missing days stay in the assignment vector and are masked after each draw,
    so the re-draws see the same missingness pattern as the real experiment.
    """

    def __init__(
        self,
        y: FloatArray | list[float],
        arm: NDArray[Any] | list[int],
        design: BlockDesign,
        *,
        draws: int = 10_000,
        seed: int = 0,
    ) -> None:
        self.y = np.asarray(y, dtype=np.float64)
        self.arm = np.asarray(arm, dtype=np.int64)
        if not (len(self.y) == len(self.arm) == design.n_days):
            raise ValueError("y, arm and design must describe the same days")
        if not np.isin(self.arm, (0, 1)).all():
            raise ValueError("arm must be 0 or 1")
        self.draws = draws
        self.ok = ~np.isnan(self.y)
        self.n_treatment = int((self.ok & (self.arm == 1)).sum())
        self.n_control = int((self.ok & (self.arm == 0)).sum())
        self.observed: float | None = None
        self._sim_y: FloatArray = np.array([])
        self._sim_arm: FloatArray = np.array([])
        if self.n_treatment == 0 or self.n_control == 0:
            return

        self.observed = float(_difference_in_means(self.arm[None, :], self.y, self.ok)[0])
        redraws = design.draw(np.random.default_rng(seed), draws)
        # Under H0: effect == tau, a re-drawn assignment `a` would have produced the statistic
        #   stat_a(y - tau * arm) + tau  =  stat_a(y) - tau * stat_a(arm) + tau,
        # so two vectors are enough to evaluate any tau without re-drawing.
        sim_y = _difference_in_means(redraws, self.y, self.ok)
        sim_arm = _difference_in_means(redraws, self.arm.astype(np.float64), self.ok)
        valid = ~np.isnan(sim_y)
        self._sim_y, self._sim_arm = sim_y[valid], sim_arm[valid]

    def p_value(self, tau: float = 0.0) -> float | None:
        """Two-sided p-value for H0: constant additive effect == tau."""
        if self.observed is None:
            return None
        centred = self._sim_y - tau * self._sim_arm
        extreme = np.abs(centred) >= abs(self.observed - tau) - _TIE_TOLERANCE
        return float((1 + extreme.sum()) / (1 + len(centred)))

    def _bound(self, direction: float, alpha: float) -> float | None:
        assert self.observed is not None
        spread = float(np.std(self._sim_y)) if len(self._sim_y) else 0.0
        if spread == 0.0:
            return self.observed
        inside, step = self.observed, spread
        outside: float | None = None
        for _ in range(60):
            candidate = self.observed + direction * step
            p = self.p_value(candidate)
            if p is not None and p <= alpha:
                outside = candidate
                break
            inside, step = candidate, step * 2
        if outside is None:
            return None  # the design cannot reject any effect in this direction
        for _ in range(60):
            middle = (inside + outside) / 2
            p = self.p_value(middle)
            if p is not None and p > alpha:
                inside = middle
            else:
                outside = middle
        return inside

    def confidence_interval(self, alpha: float = 0.05) -> tuple[float | None, float | None]:
        """All tau with p > alpha, by bisection, reusing the same draws for every tau."""
        if self.observed is None:
            return None, None
        return self._bound(-1.0, alpha), self._bound(1.0, alpha)

    def result(self, alpha: float = 0.05) -> TestResult:
        low, high = self.confidence_interval(alpha)
        return TestResult(
            effect=self.observed,
            p_value=self.p_value(0.0),
            ci_low=low,
            ci_high=high,
            n_treatment=self.n_treatment,
            n_control=self.n_control,
            draws=len(self._sim_y),
        )
