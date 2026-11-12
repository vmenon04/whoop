from __future__ import annotations

from itertools import permutations, product
from typing import Any

import numpy as np
import pytest

from whoop_lab.analysis.inference import BlockDesign, RandomizationTest
from whoop_lab.analysis.simulate import simulate_experiment

# --- reference implementation, copied verbatim from the experiment-analysis skill ------


def draw_blocks(n_days: int, block_size: int, rng: np.random.Generator) -> Any:
    """One assignment vector of length n_days from the permuted-block scheme."""
    base = np.array([1] * (block_size // 2) + [0] * (block_size // 2))
    n_blocks = -(-n_days // block_size)
    return np.concatenate([rng.permutation(base) for _ in range(n_blocks)])[:n_days]


def randomization_test(
    y: Any, arm: Any, block_size: int = 4, draws: int = 10_000, seed: int = 0, tau: float = 0.0
) -> tuple[float, float]:
    y, arm = np.asarray(y, float), np.asarray(arm, int)
    ok = ~np.isnan(y)
    y0 = y - tau * arm

    def stat(a: Any) -> float:
        t, c = ok & (a == 1), ok & (a == 0)
        if not t.any() or not c.any():
            return float("nan")
        return float((y0 + tau * a)[t].mean() - (y0 + tau * a)[c].mean())

    observed = stat(arm)
    rng = np.random.default_rng(seed)
    sims = np.array([stat(draw_blocks(len(y), block_size, rng)) for _ in range(draws)])
    sims = sims[~np.isnan(sims)]
    p = (1 + np.sum(np.abs(sims - tau) >= abs(observed - tau) - 1e-12)) / (1 + len(sims))
    return observed, float(p)


# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n_days", "block_size", "effect", "missing", "tau"),
    [
        (28, 4, 0.0, 0.0, 0.0),
        (28, 4, 0.2, 0.15, 0.0),
        (30, 4, 0.1, 0.1, 0.0),  # last block incomplete
        (24, 6, 0.3, 0.0, 0.0),
        (28, 4, 0.2, 0.1, 0.12),  # a non-zero null
        (12, 2, 0.0, 0.2, -0.05),
    ],
)
def test_agrees_with_the_reference_implementation(
    n_days: int, block_size: int, effect: float, missing: float, tau: float
) -> None:
    rng = np.random.default_rng(n_days * 1000 + block_size)
    sim = simulate_experiment(
        rng, n_days=n_days, block_size=block_size, effect=effect, missing_rate=missing
    )
    expected_effect, expected_p = randomization_test(
        sim.y, sim.arm, block_size=block_size, draws=10_000, seed=1, tau=tau
    )
    test = RandomizationTest(sim.y, sim.arm, sim.design, draws=10_000, seed=2)
    assert test.observed == pytest.approx(expected_effect)
    p = test.p_value(tau)
    assert p is not None
    # Different random streams, same distribution: agree within Monte Carlo error.
    assert p == pytest.approx(expected_p, abs=0.02)


def test_redraws_follow_the_block_scheme() -> None:
    design = BlockDesign.contiguous(28, 4)
    draws = design.draw(np.random.default_rng(0), 5000)
    assert draws.shape == (5000, 28)
    per_block = draws.reshape(5000, 7, 4).sum(axis=2)
    assert (per_block == 2).all()  # two treatment days in every block of every draw
    assert draws.mean(axis=0) == pytest.approx(np.full(28, 0.5), abs=0.03)


def test_design_is_rebuilt_from_stored_blocks_not_dates() -> None:
    # Two strata with interleaved days, as weekday/weekend stratification produces.
    strata = [
        "weekday",
        "weekday",
        "weekend",
        "weekend",
        "weekday",
        "weekday",
        "weekend",
        "weekend",
    ]
    index = [0, 0, 0, 0, 0, 0, 0, 0]
    position = [0, 1, 0, 1, 2, 3, 2, 3]
    design = BlockDesign.from_assignments(strata, index, position, 4)
    draws = design.draw(np.random.default_rng(1), 2000)
    weekday = draws[:, [0, 1, 4, 5]].sum(axis=1)
    weekend = draws[:, [2, 3, 6, 7]].sum(axis=1)
    assert (weekday == 2).all()
    assert (weekend == 2).all()


def test_incomplete_final_block_is_drawn_as_a_truncated_full_block() -> None:
    design = BlockDesign.contiguous(6, 4)
    draws = design.draw(np.random.default_rng(2), 6000)
    assert (draws[:, :4].sum(axis=1) == 2).all()
    tail = draws[:, 4:].sum(axis=1)  # two days of a block of four: 0, 1 or 2 treated
    counts = np.bincount(tail, minlength=3) / 6000
    assert counts == pytest.approx([1 / 6, 4 / 6, 1 / 6], abs=0.03)


def test_duplicate_block_slots_are_rejected() -> None:
    with pytest.raises(ValueError, match="same block position"):
        BlockDesign(np.array([0, 0]), np.array([1, 1]), 4)


def test_clear_effect_is_detected_and_the_interval_brackets_it() -> None:
    rng = np.random.default_rng(7)
    sim = simulate_experiment(rng, n_days=40, effect=0.5, sigma=0.1, phi=0.3)
    result = RandomizationTest(sim.y, sim.arm, sim.design, draws=10_000, seed=3).result()
    assert result.effect == pytest.approx(0.5, abs=0.1)
    assert result.p_value is not None
    assert result.p_value < 0.001
    assert result.ci_low is not None
    assert result.ci_high is not None
    assert result.ci_low < 0.5 < result.ci_high
    assert result.ci_low > 0
    assert (result.n_treatment, result.n_control) == (20, 20)


def test_interval_is_exactly_the_set_of_effects_the_test_does_not_reject() -> None:
    rng = np.random.default_rng(11)
    sim = simulate_experiment(rng, n_days=28, effect=0.1)
    test = RandomizationTest(sim.y, sim.arm, sim.design, draws=10_000, seed=5)
    low, high = test.confidence_interval(0.05)
    assert low is not None
    assert high is not None
    assert test.observed is not None
    assert low < test.observed < high
    for inside in (low + 1e-6, test.observed, high - 1e-6):
        assert (test.p_value(inside) or 0) > 0.05
    for outside in (low - 0.01, high + 0.01):
        assert (test.p_value(outside) or 1) <= 0.05
    # p == 0.05 at tau == 0 is equivalent to 0 lying on the edge of the interval.
    p0 = test.p_value(0.0)
    assert p0 is not None
    assert (p0 > 0.05) == (low <= 0.0 <= high)


def test_same_seed_same_answer() -> None:
    rng = np.random.default_rng(3)
    sim = simulate_experiment(rng, n_days=28, effect=0.1, missing_rate=0.1)
    a = RandomizationTest(sim.y, sim.arm, sim.design, draws=4000, seed=9).result()
    b = RandomizationTest(sim.y, sim.arm, sim.design, draws=4000, seed=9).result()
    assert a == b


def test_missing_days_are_masked_not_removed() -> None:
    rng = np.random.default_rng(5)
    sim = simulate_experiment(rng, n_days=28, effect=0.2)
    y = sim.y.copy()
    y[[1, 6, 13]] = np.nan
    test = RandomizationTest(y, sim.arm, sim.design, draws=4000, seed=1)
    keep = ~np.isnan(y)
    expected = y[keep & (sim.arm == 1)].mean() - y[keep & (sim.arm == 0)].mean()
    assert test.observed == pytest.approx(expected)
    assert test.n_treatment + test.n_control == 25


def test_an_empty_arm_gives_no_result_rather_than_a_number() -> None:
    design = BlockDesign.contiguous(4, 4)
    y = np.array([1.0, np.nan, 2.0, np.nan])
    arm = np.array([1, 0, 1, 0])
    result = RandomizationTest(y, arm, design, draws=500, seed=0).result()
    assert (result.effect, result.p_value, result.ci_low, result.ci_high) == (None,) * 4
    assert (result.n_treatment, result.n_control) == (2, 0)


def test_too_few_days_to_reject_gives_an_open_interval() -> None:
    # One block of four has only six possible assignments: p can never reach 0.05.
    design = BlockDesign.contiguous(4, 4)
    result = RandomizationTest(
        np.array([1.0, 2.0, 5.0, 6.0]), np.array([0, 0, 1, 1]), design, draws=2000, seed=0
    ).result()
    assert result.effect == pytest.approx(4.0)
    assert result.p_value is not None
    assert result.p_value > 0.05
    assert result.ci_low is None
    assert result.ci_high is None


# --- small designs are enumerated exactly -------------------------------------------------


def brute_force_p(y: Any, arm: Any, blocks: list[list[int]]) -> float:
    """Exact p-value written the slow, obvious way: every permutation of every block,
    with missing days masked after the assignment is formed."""
    ok = ~np.isnan(y)

    def stat(a: Any) -> float | None:
        t, c = ok & (a == 1), ok & (a == 0)
        return float(y[t].mean() - y[c].mean()) if t.any() and c.any() else None

    observed = stat(arm)
    assert observed is not None
    per_block = [
        sorted(set(permutations([1] * (len(b) // 2) + [0] * (len(b) // 2)))) for b in blocks
    ]
    stats = []
    for choice in product(*per_block):
        candidate = np.zeros(len(y), dtype=int)
        for days, pattern in zip(blocks, choice, strict=True):
            candidate[days] = pattern
        value = stat(candidate)
        if value is not None:
            stats.append(value)
    return float(np.mean(np.abs(stats) >= abs(observed) - 1e-12))


def test_small_designs_are_enumerated_in_full() -> None:
    design = BlockDesign.contiguous(8, 4)
    assert design.n_assignments == 36
    rows = design.enumerate()
    assert rows.shape == (36, 8)
    assert len({tuple(row) for row in rows.tolist()}) == 36
    assert (rows.reshape(36, 2, 4).sum(axis=2) == 2).all()
    # A cut-off block keeps the probabilities of a full one.
    tail = BlockDesign.contiguous(6, 4).enumerate()[:, 4:].sum(axis=1)
    assert (np.bincount(tail, minlength=3) / 36).tolist() == [1 / 6, 4 / 6, 1 / 6]


def test_exact_p_value_masks_missing_days_instead_of_reblocking() -> None:
    y = np.array([0.1, 1.9, np.nan, 0.4, 2.2, np.nan, 0.3, 1.6])
    arm = np.array([0, 1, 1, 0, 1, 0, 0, 1])
    test = RandomizationTest(y, arm, BlockDesign.contiguous(8, 4), draws=10_000, seed=0)
    assert test.exact
    masked = brute_force_p(y, arm, [[0, 1, 2, 3], [4, 5, 6, 7]])
    assert test.p_value() == pytest.approx(masked, abs=1e-12)
    # The wrong analysis drops the missing days first and treats what is left as new blocks.
    keep = ~np.isnan(y)
    reblocked = brute_force_p(y[keep], arm[keep], [[0, 1, 2, 3], [4, 5]])
    assert abs(masked - reblocked) > 0.01
    # A non-zero null goes through the same enumeration.
    shifted = brute_force_p(y - 0.7 * arm, arm, [[0, 1, 2, 3], [4, 5, 6, 7]])
    assert test.p_value(0.7) == pytest.approx(shifted, abs=1e-12)


def test_stratified_exact_p_value_matches_brute_force() -> None:
    strata = [
        "weekday",
        "weekday",
        "weekend",
        "weekend",
        "weekday",
        "weekday",
        "weekend",
        "weekend",
    ]
    position = [0, 1, 0, 1, 2, 3, 2, 3]
    design = BlockDesign.from_assignments(strata, [0] * 8, position, 4)
    y = np.array([1.0, 2.5, 0.2, 1.1, 0.9, 3.0, np.nan, 0.8])
    arm = np.array([0, 1, 0, 1, 0, 1, 1, 0])
    test = RandomizationTest(y, arm, design, draws=10_000, seed=0)
    expected = brute_force_p(y, arm, [[0, 1, 4, 5], [2, 3, 6, 7]])
    assert test.p_value() == pytest.approx(expected, abs=1e-12)


def test_a_tiny_design_never_reports_a_p_value_it_cannot_produce() -> None:
    # Eight days: 36 assignments, and the mirror image of the observed one is as
    # extreme, so no p-value can be below 2/36, whatever the data or the seed.
    y = np.array([0.0, 0.0, 9.0, 9.0, 0.0, 0.0, 9.0, 9.0])
    arm = np.array([0, 0, 1, 1, 0, 0, 1, 1])
    for seed in range(20):
        result = RandomizationTest(y, arm, BlockDesign.contiguous(8, 4), seed=seed).result()
        assert result.exact
        assert result.draws == 36
        assert result.p_value == pytest.approx(2 / 36)
        assert (result.ci_low, result.ci_high) == (None, None)


def test_larger_designs_are_sampled() -> None:
    sim = simulate_experiment(np.random.default_rng(1), n_days=28)
    result = RandomizationTest(sim.y, sim.arm, sim.design, draws=2000, seed=0).result()
    assert not result.exact
    assert result.draws == 2000
