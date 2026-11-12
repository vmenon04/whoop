from __future__ import annotations

from collections import Counter
from datetime import date

import pytest

from whoop_lab.experiments.randomize import arm_for, block_sequence, stratum_for


@pytest.mark.parametrize("block_size", [2, 4, 6, 8])
def test_every_block_is_balanced(block_size: int) -> None:
    for seed in range(500):
        counts = Counter(block_sequence(seed, block_size))
        assert counts == {"treatment": block_size // 2, "control": block_size // 2}


def test_sequence_is_a_function_of_the_seed_alone() -> None:
    for seed in (0, 1, 2**61 + 17, 987654321):
        assert block_sequence(seed, 4) == block_sequence(seed, 4)
        assert [arm_for(seed, 4, i) for i in range(4)] == block_sequence(seed, 4)


def test_different_seeds_give_all_six_orderings_about_equally() -> None:
    orderings = Counter(tuple(block_sequence(seed, 4)) for seed in range(6000))
    assert len(orderings) == 6  # 4 choose 2
    assert all(850 < n < 1150 for n in orderings.values())


@pytest.mark.parametrize("block_size", [0, 1, 3, -2])
def test_odd_or_tiny_block_sizes_are_rejected(block_size: int) -> None:
    with pytest.raises(ValueError, match="even"):
        block_sequence(1, block_size)


def test_strata() -> None:
    friday, saturday, sunday, monday = (date(2026, 11, d) for d in (13, 14, 15, 16))
    assert stratum_for(saturday, stratify_weekend=False) == "all"
    assert stratum_for(friday, stratify_weekend=True) == "weekday"
    assert stratum_for(saturday, stratify_weekend=True) == "weekend"
    assert stratum_for(sunday, stratify_weekend=True) == "weekend"
    assert stratum_for(monday, stratify_weekend=True) == "weekday"
