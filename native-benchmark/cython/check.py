"""Small deterministic parity probes; no parameter search or financial implementation."""
import argparse
import importlib
import importlib.machinery
import json
from pathlib import Path
import random
import sys
import warnings

import numpy as np


def exercise(source, mode, modules, windows=48):
    source = Path(source).resolve()
    sys.path.insert(0, str(source))
    loaded = {}
    for name in modules:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        compiled = any(str(path).endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)
        if not path.is_relative_to(source) or compiled != (mode == "compiled"):
            raise ValueError(f"Wrong module loaded for {mode}: {name}: {path}")
        loaded[name] = str(path.relative_to(source))
    from simulator.amm.intitial_liquidity import ConstantInitialLiquidity
    from simulator.amm.lending_amm import LendingAMM, OracleState
    from simulator.amm.simulator import Simulator

    rng = np.random.default_rng(19937)
    prices = np.exp(np.cumsum(rng.normal(0, .004, 180)))
    timestamps = 1700000000 + np.arange(len(prices)) * 60
    candles = np.column_stack((timestamps, prices, prices * 1.01, prices / 1.01, prices, np.zeros(len(prices))))
    oracle = np.roll(prices, 1)
    oracle[0] = prices[0]
    sim = Simulator.__new__(Simulator)
    sim.initial_liquidity_class = ConstantInitialLiquidity
    sim.external_fee = .0005
    sim.log_enabled = sim.verbose = False
    draws = random.Random(20261001)
    tasks = [(draws.randrange(len(prices) - 60), (10, 100, 300, 600)[i % 4], (30, 60)[i % 2]) for i in range(windows)]

    def replay():
        return [sim.calculate_loss(a, min(.003, 1 / a), candles[lo:lo + size], oracle[lo:lo + size], 4, .25,
                                  initial_state=OracleState.initial(oracle[lo], timestamps[lo])) for lo, a, size in tasks]

    losses = np.asarray(replay(), dtype="<f8")
    if not np.isfinite(losses).all():
        raise ValueError("Nonfinite fixture output")
    if np.asarray(replay(), dtype="<f8").tobytes() != losses.tobytes():
        raise ValueError("Replay is not bitwise repeatable")
    probes, errors = [], []
    for a in (10, 100, 300, 600):
        amm = LendingAMM(1.01, a, .001, oracle_state=OracleState.initial(1., 0))
        for band, x, y in ((-501, 2., 0.), (-17, .01, .02), (0, 0., 0.), (3, 0., 1.), (500, 1., 1.)):
            amm.bands_x[band], amm.bands_y[band] = x, y
        probes.append([amm.get_all_x(), amm.get_all_y()])
        for price, timestamp in ((1.2, 10), (1.1, 10), (.95, 180)):
            amm.set_p_oracle(price, timestamp)
            probes.append([list(amm.oracle_state()), amm.dynamic_fee(3, timestamp)])
        try:
            amm.restore_oracle_state(OracleState.initial(-1., 0))
        except Exception as exc:
            errors.append(type(exc).__name__)
        else:
            raise ValueError("Invalid oracle state was accepted")
        amm = LendingAMM(1., a, .001, oracle_state=OracleState.initial(1., 0))
        amm.deposit_nrange(1., 1., 4)
        for price in (1.04, .94, 1., 1.):
            trade = amm.trade_to_price(price)
            probes.append([list(trade), list(amm.oracle_state()), amm.active_band,
                           sorted(amm.bands_x.items()), sorted(amm.bands_y.items())])
        # Compare optional-constructor behavior too, including legacy TypeError.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                unclocked = LendingAMM(1., a, .001)
                unclocked.set_p_oracle(1.01)
                probes.append([unclocked.current_timestamp, unclocked.dynamic_fee(0)])
            except TypeError as exc:
                probes.append(["constructor", type(exc).__name__])
        errors.extend(type(w.message).__name__ for w in caught)
    return {"ordered_losses": losses.tolist(), "probes": probes, "errors": errors,
            "repeatability": "bitwise", "windows": windows, "modules": loaded}


if __name__ == "__main__":
    if sys.flags.optimize:
        raise ValueError("Validation requires Python assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("python", "compiled"), required=True)
    parser.add_argument("--modules", nargs="+", required=True)
    parser.add_argument("--windows", type=int, default=48)
    args = parser.parse_args()
    if args.windows < 1:
        parser.error("--windows must be positive")
    result = exercise(args.source, args.mode, args.modules, args.windows)
    Path(args.output).write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
