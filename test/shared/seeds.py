"""The tests' random data, reproducible and fresh on request.

seeded(seed) is random.Random(seed): every test draws the same data on every run, as committed.
With the environment variable OPENPIO_SEED set, each test's seed is mixed with it, so every
value of OPENPIO_SEED draws new data everywhere: tests that pass only on the data they were
written with fail. A failure is run again with the same OPENPIO_SEED.

    OPENPIO_SEED=7 python3 programs/report.py
"""

from __future__ import annotations

import os
import random

BASE = os.environ.get("OPENPIO_SEED") or None


def seeded(seed: int | str = 0) -> random.Random:
    return random.Random(seed if BASE is None else f"{BASE}/{seed}")
