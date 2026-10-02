"""Profile-guided optimization workload: synthetic replays only, never the frozen benchmark inputs.

Run against an instrumented build. The fixed seed keeps the merged profile, and so the
optimized binaries, reproducible.
"""
import argparse
from pathlib import Path
import sys

import numpy as np


def train(source):
    sys.path.insert(0, str(Path(source).resolve()))
    from simulator.amm.intitial_liquidity import ConstantInitialLiquidity
    from simulator.amm.simulator import Simulator, replay_batch

    rng = np.random.default_rng(20261001)
    count = 100_000
    close = 2000 * np.exp(np.cumsum(rng.normal(0, 0.0015, count)))
    timestamps = 1_600_000_000 + 60 * np.arange(count)
    high = close * (1 + np.abs(rng.normal(0, 0.001, count)))
    low = close / (1 + np.abs(rng.normal(0, 0.001, count)))
    oracle = np.roll(close, 1)
    oracle[0] = close[0]
    points = np.column_stack((timestamps, close, high, low, close, np.zeros(count), oracle))

    tasks = []
    for i, start in enumerate(rng.integers(0, count - 60, 4000)):
        A = (10, 100, 300, 600)[i % 4]
        tasks.append((A, min(0.003, 1 / A), start, start + (30, 60)[i % 2], 4, 0.0005, 0.25, 0))

    simulator = Simulator.__new__(Simulator)
    simulator.initial_liquidity_class = ConstantInitialLiquidity
    simulator.log_enabled = simulator.verbose = False
    losses = replay_batch(simulator, points, np.asarray(tasks, dtype=np.float64))
    if not np.isfinite(losses).all():
        raise ValueError("Nonfinite training output")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Directory containing the instrumented simulator package")
    train(parser.parse_args().source)
