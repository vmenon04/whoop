"""Permuted-block randomization.

Each block has its own stored seed, and a block's sequence is a function of
that seed alone. Stratified designs keep a separate stream of blocks per stratum.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from whoop_lab.models.experiments import ARM_CONTROL, ARM_TREATMENT

STRATUM_ALL = "all"
STRATUM_WEEKDAY = "weekday"
STRATUM_WEEKEND = "weekend"


def block_sequence(seed: int, block_size: int) -> list[str]:
    """The arms for one block, half and half, in the order fixed by `seed`."""
    if block_size < 2 or block_size % 2:
        raise ValueError("block_size must be a positive even number")
    half = block_size // 2
    base = np.array([1] * half + [0] * half)
    order = np.random.default_rng(seed).permutation(base)
    return [ARM_TREATMENT if bit else ARM_CONTROL for bit in order]


def arm_for(seed: int, block_size: int, position: int) -> str:
    return block_sequence(seed, block_size)[position]


def stratum_for(day: date, *, stratify_weekend: bool) -> str:
    if not stratify_weekend:
        return STRATUM_ALL
    return STRATUM_WEEKEND if day.weekday() >= 5 else STRATUM_WEEKDAY
