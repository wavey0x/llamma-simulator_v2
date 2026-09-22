#!/usr/bin/env python3
"""Replay the three-leg USD LP oracle against crvUSD-denominated market spot.

The upstream LendingAMM is unchanged. Results describe this normalized replay;
they do not choose launch parameters, caps, loan discounts, or monetary policy.
"""
import argparse
import hashlib
import json
import math
import multiprocessing as mp
import platform
import sys
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from simulator.amm.lending_amm import LendingAMM
from simulator.pairs.reusd_sfrxusd_lp.collect_history import read_history

WAD = 10**18
A_PRECISION = 10**4


@dataclass(frozen=True)
class Config:
    a_values: tuple = (25, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000)
    fees: tuple = (0.0005, 0.001, 0.002, 0.003, 0.004, 0.005, 0.0075, 0.01)
    base_fee: float = 0.002
    bands: int = 4
    dynamic_fee_multiplier: float = 0.25
    external_fee: float = 0.0005
    loan_seconds: int = 86400
    window_step_seconds: int = 86400
    warmup_seconds: int = 86400
    ema_seconds: int = 866
    oracle_update_seconds: int = 0  # 0 = price_w at each sampled block
    initial_vp_scale: float = 1.0


def _x_from_y(a_raw, y):
    b = WAD - 4 * a_raw * (WAD - y) // A_PRECISION
    radicand = b * b + 4 * a_raw * WAD**3 // (A_PRECISION * y)
    return (math.isqrt(radicand) - b) * A_PRECISION // (8 * a_raw)


def _p_from_y(a_raw, y):
    x = _x_from_y(a_raw, y)
    a = 4 * a_raw * x // A_PRECISION
    return (a + WAD**3 // (4 * y * y)) * WAD // (a + WAD**3 // (4 * x * y))


@lru_cache(maxsize=65536)
def portfolio_value(a_raw, price):
    """Integer port of pinned curve-std lp_oracle_2 (coin zero).

    The pool's A_precise must be rescaled before calling this function.
    Keep rounding and the 1e-6 bisection price tolerance identical to Vyper.
    """
    if not (0 < a_raw <= 100000 * A_PRECISION and 10**16 <= price <= 10**20):
        raise ValueError("outside the contract's LP solver domain")
    target = (WAD * WAD + price // 2) // price if price < WAD else price
    lo, hi = 1, WAD // 2 + 1
    for _ in range(60):
        y = (lo + hi) // 2
        candidate = _p_from_y(a_raw, y)
        if abs(candidate - target) <= target // 10**6:
            break
        if candidate > target:
            lo = y
        else:
            hi = y
        if hi - lo <= 1:
            y = hi
            break
    else:
        raise ArithmeticError("LP solver did not converge")
    x = _x_from_y(a_raw, y)
    if price < WAD:
        x, y = y, x
    return x + price * y // WAD


@lru_cache(maxsize=8192)
def decay(dt, horizon):
    # Float exp is the only approximation to the contract's integer EMA math.
    return int(math.exp(-dt / horizon) * WAD)


class VirtualPriceEMA:
    def __init__(self, value, timestamp, horizon):
        self.previous = self.queued = value
        self.timestamp, self.horizon = timestamp, horizon

    def price(self, spot, timestamp, write=True):
        if timestamp < self.timestamp or self.horizon <= 0:
            raise ValueError("invalid EMA time")
        mul = decay(timestamp - self.timestamp, self.horizon)
        smoothed = (self.previous * mul + self.queued * (WAD - mul)) // WAD
        value = min(spot, smoothed)
        if write:
            self.previous = value
            self.queued = spot
            self.timestamp = timestamp
        return value


def feed_value(row):
    fee = row["base_redemption_fee"]
    if not 0 <= fee <= WAD or row["bridge_price_oracle"] <= 0:
        raise ValueError("invalid redemption inputs")
    modeled = max(WAD * WAD // row["bridge_price_oracle"], WAD - fee)
    observed = row["reusd_feed"]
    if observed is not None and observed != modeled:
        raise ValueError("observed feed disagrees with proposed reconstruction")
    return min(WAD, modeled if observed is None else observed)


def reconstruct(rows, config):
    if not rows:
        raise ValueError("empty history")
    ema = VirtualPriceEMA(
        int(Decimal(rows[0]["lp_virtual_price"]) * Decimal(str(config.initial_vp_scale))),
        rows[0]["timestamp"],
        config.ema_seconds,
    )
    points = []
    previous_block, previous_time = -1, -1
    for row in rows:
        timestamp, block = row["timestamp"], row["block"]
        if block <= previous_block or timestamp <= previous_time:
            raise ValueError("history must be strictly chronological")
        previous_block, previous_time = block, timestamp
        a_raw = row["lp_A_precise"] * A_PRECISION // 200
        vp = row["lp_virtual_price"]
        write = timestamp - ema.timestamp >= config.oracle_update_seconds
        smoothed = ema.price(vp, timestamp, write)
        lp_spot = portfolio_value(a_raw, row["lp_last_price"]) * vp // WAD
        lp_oracle = portfolio_value(a_raw, row["lp_price_oracle"]) * smoothed // WAD
        spot = lp_spot * (WAD * WAD // row["bridge_last_price"]) // WAD
        oracle = lp_oracle * feed_value(row) // WAD
        # Match ChainOracle's third leg and stepwise integer rounding. LLAMMA
        # consumes this USD-valued number directly; market trades remain crvUSD.
        aggregator = row["crvusd_agg_price"]
        if aggregator <= 0:
            raise ValueError("nonpositive USD aggregator price")
        oracle = oracle * aggregator // WAD
        if spot <= 0 or oracle <= 0:
            raise ValueError("nonpositive price")
        points.append((timestamp, block, spot / WAD, oracle / WAD))
    return points


def window_starts(points, config):
    times = [p[0] for p in points]
    first, last = times[0] + config.warmup_seconds, times[-1] - config.loan_seconds
    if first > last:
        raise ValueError("history too short for warm-up and loan window")
    starts = set(range(first, last + 1, config.window_step_seconds))
    # Include hourly starts around five separated oracle/market dislocations.
    candidates = sorted(range(len(points)), key=lambda i: abs(points[i][3] / points[i][2] - 1), reverse=True)
    stress = []
    for i in candidates:
        time = times[i]
        if time < first or any(abs(time - other) < config.loan_seconds for other in stress):
            continue
        stress.append(time)
        starts.update(t for t in range(time - config.loan_seconds, time + 1, 3600) if first <= t <= last)
        if len(stress) == 5:
            break
    return sorted({bisect_left(times, time) for time in starts if times[bisect_left(times, time)] <= last})


def simulate(points, A, fee, config):
    timestamp, _, _, oracle = points[0]
    amm = LendingAMM(oracle * (A / (A - 1) + 1e-4), A, fee, config.dynamic_fee_multiplier)
    amm.deposit_nrange(1.0, oracle, config.bands)
    amm.p_oracle = amm.prev_p_oracle = amm.raw_p_oracle = amm.old_p_oracle = oracle
    amm.old_dfee = 0.0
    amm.prev_p_oracle_time = amm.current_timestamp = timestamp
    # Normalize at the starting oracle, after seeding its memory.
    initial = amm.get_all_x()

    for timestamp, _, market, oracle in points:
        amm.set_p_oracle(oracle, timestamp=timestamp)
        up = market > amm.get_p()
        external = market * (1 - config.external_fee if up else 1 + config.external_fee)
        bands = range(amm.max_band, amm.min_band - 1, -1) if up else range(amm.min_band, amm.max_band + 1)
        for band in bands:
            dynamic = amm.dynamic_fee(band, timestamp=timestamp)
            boundary = amm.p_down(band) * (1 + dynamic) if up else amm.p_up(band) * (1 - dynamic)
            if (up and external > boundary) or (not up and external < boundary):
                break
        target = external * (1 - dynamic if up else 1 + dynamic)
        current = amm.get_p()
        if (up and target > current) or (not up and target < current):
            amm.trade_to_price(target)
    loss = 1 - amm.get_all_x() / initial
    if not math.isfinite(loss):
        raise ArithmeticError("nonfinite simulation result")
    return loss


POINTS, TIMES, CONFIG = [], [], None


def init_worker(points, config):
    global POINTS, TIMES, CONFIG
    POINTS, TIMES, CONFIG = points, [p[0] for p in points], config


def simulate_task(task):
    A, fee, start = task
    end = bisect_right(TIMES, TIMES[start] + CONFIG.loan_seconds)
    return simulate(POINTS[start:end], A, fee, CONFIG)


def evaluate(mapper, points, starts, A, fee, config):
    losses = list(mapper(simulate_task, [(A, fee, start) for start in starts]))
    if len(losses) != len(starts) or not losses:
        raise RuntimeError("incomplete evaluations")
    maximum = max(losses)
    worst = starts[losses.index(maximum)]
    coefficient = sum(((A - 1) / A) ** (band + 0.5) for band in range(config.bands)) / config.bands
    return {
        "A": A,
        "fee": fee,
        "windows": len(starts),
        "maximum_raw_loss": maximum,
        "band_adjusted_loss": 1 - (1 - maximum) * coefficient,
        "worst_start_block": points[worst][1],
        "worst_start_timestamp": points[worst][0],
    }


def contract_valid(A, fee):
    # LendFactory A bounds and AMM MIN_FEE / MAX_FEE (MIN_TICKS = 4).
    fee_wad = int(Decimal(str(fee)) * WAD)
    return 2 <= A <= 10000 and 10**6 <= fee_wad <= min(4 * WAD // A, WAD // 10)


def run(points, config, workers, starts=None, exact=False):
    starts = window_starts(points, config) if starts is None else starts
    init_worker(points, config)
    pool = mp.get_context("fork").Pool(workers) if workers > 1 else None
    mapper = pool.map if pool else map
    results = {}
    try:

        def assess(A, fee):
            if not contract_valid(A, fee):
                raise ValueError(f"A={A}, fee={fee} violates the deployment bounds")
            if (A, fee) not in results:
                results[A, fee] = evaluate(mapper, points, starts, A, fee, config)
                print(json.dumps(results[A, fee]), flush=True)
            return results[A, fee]

        if exact:
            for A in config.a_values:
                for fee in config.fees:
                    assess(A, fee)
            selection = {}
        else:
            # Search A independently at every fee; a winner at one fee need not
            # be competitive at another. Every case uses the same loan starts.
            best_by_fee, bounds, excluded = [], [], []
            for fee in sorted(set((*config.fees, config.base_fee))):
                eligible = [A for A in config.a_values if contract_valid(A, fee)]
                excluded.extend({"A": A, "fee": fee} for A in config.a_values if A not in eligible)
                if not eligible:
                    raise ValueError(f"no deployable A in the supplied range at fee={fee}")
                maximum_A = min(config.a_values[-1], 10000, 4 * WAD // int(Decimal(str(fee)) * WAD))
                eligible = sorted(set([*eligible, maximum_A]))
                bounds.append({"fee": fee, "minimum_A": eligible[0], "maximum_A": eligible[-1]})
                coarse = [assess(A, fee) for A in eligible]
                selected = min(coarse, key=lambda r: r["band_adjusted_loss"])["A"]
                index = eligible.index(selected)
                lower = eligible[max(0, index - 1)]
                upper = eligible[min(len(eligible) - 1, index + 1)]
                step = max(5, (upper - lower) // 100 * 5)
                nearby = [assess(A, fee) for A in range(lower, upper + 1, step)]
                fine = min(coarse + nearby, key=lambda r: r["band_adjusted_loss"])["A"]
                nearby += [assess(A, fee) for A in range(max(lower, fine - step), min(upper, fine + step) + 1, 5)]
                best = min(coarse + nearby, key=lambda r: r["band_adjusted_loss"])
                best_by_fee.append(best)
            best = min(best_by_fee, key=lambda r: r["band_adjusted_loss"])
            selection = {
                "lowest_loss_A_at_base_fee": next(r["A"] for r in best_by_fee if r["fee"] == config.base_fee),
                "best_by_fee": best_by_fee,
                "best_tested": best,
                "A_boundary_fees": [
                    r["fee"] for r, b in zip(best_by_fee, bounds) if r["A"] in (b["minimum_A"], b["maximum_A"])
                ],
                "deployment_bounds_by_fee": bounds,
                "excluded_undeployable_combinations": excluded,
                "fee_minimum_at_boundary": best["fee"] in (best_by_fee[0]["fee"], best_by_fee[-1]["fee"]),
            }
        return {
            **selection,
            "evaluations": sorted(results.values(), key=lambda r: (r["fee"], r["A"])),
            "window_start_blocks": [points[i][1] for i in starts],
            "window_start_timestamps": [points[i][0] for i in starts],
        }
    finally:
        if pool:
            pool.close()
            pool.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--a-values", default=",".join(map(str, Config.a_values)))
    parser.add_argument("--fees", default=",".join(map(str, Config.fees)))
    parser.add_argument("--base-fee", type=float, default=Config.base_fee)
    parser.add_argument("--external-fee", type=float, default=Config.external_fee)
    parser.add_argument("--loan-seconds", type=int, default=Config.loan_seconds)
    parser.add_argument("--bands", type=int, default=Config.bands)
    parser.add_argument("--oracle-update-seconds", type=int, default=Config.oracle_update_seconds)
    parser.add_argument("--warmup-seconds", type=int, default=Config.warmup_seconds)
    parser.add_argument("--initial-vp-scale", type=float, default=Config.initial_vp_scale)
    parser.add_argument("--exact", action="store_true", help="only evaluate the supplied A and fee combinations")
    parser.add_argument("--windows-from", type=Path, help="reuse exact start timestamps from another result")
    parser.add_argument("--window-step-seconds", type=int, default=Config.window_step_seconds)
    args = parser.parse_args()
    config = Config(
        a_values=tuple(sorted(set(map(int, args.a_values.split(","))))),
        fees=tuple(sorted(set(map(float, args.fees.split(","))))),
        base_fee=args.base_fee,
        external_fee=args.external_fee,
        loan_seconds=args.loan_seconds,
        bands=args.bands,
        oracle_update_seconds=args.oracle_update_seconds,
        warmup_seconds=args.warmup_seconds,
        initial_vp_scale=args.initial_vp_scale,
        window_step_seconds=args.window_step_seconds,
    )
    if (
        args.workers < 1
        or min(config.a_values) < 2
        or max(config.a_values) > 10000
        or not all(0 <= f < 1 for f in (*config.fees, config.base_fee, config.external_fee))
    ):
        parser.error("invalid workers, amplification or fees")
    if (
        config.warmup_seconds < 0
        or config.oracle_update_seconds < 0
        or not math.isfinite(config.initial_vp_scale)
        or config.initial_vp_scale <= 0
        or config.window_step_seconds <= 0
        or config.loan_seconds <= 0
        or not 1 <= config.bands <= 50
    ):
        parser.error("invalid EMA configuration")
    metadata, rows = read_history(args.history)
    if metadata.get("schema") != 4 or metadata["chain_id"] != 1:
        raise ValueError("recollect schema-4 Ethereum inputs with the USD aggregator before replay")
    points = reconstruct(rows, config)
    starts = None
    if args.windows_from:
        times = [p[0] for p in points]
        requested = json.loads(args.windows_from.read_text())["window_start_timestamps"]
        starts = [bisect_left(times, t) for t in requested]
        if not starts or any(
            i >= len(times) or times[i] != t or t + config.loan_seconds > times[-1] for i, t in zip(starts, requested)
        ):
            raise ValueError("requested windows are absent or incomplete")
    gaps = [(p[3] / p[2] - 1, p[1], p[0]) for p in points]
    output = {
        "schema": 3,
        "python_version": platform.python_version(),
        "mode": "exact" if args.exact else "joint coarse A/fee grid, then independent A refinement at every fee",
        "units": {
            "spot": "crvUSD per LP",
            "oracle": "USD per LP, supplied directly as the LLAMMA oracle number",
            "loss": "fraction of initial crvUSD value",
        },
        "config": asdict(config),
        "history": metadata,
        "history_sha256": hashlib.sha256(args.history.read_bytes()).hexdigest(),
        "code_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                Path(__file__),
                Path(__file__).with_name("collect_history.py"),
                ROOT / "simulator/amm/lending_amm.py",
                ROOT / "uv.lock",
            ]
        },
        "pricing": {
            "observations": len(points),
            "maximum_sample_gap_seconds": max(b[0] - a[0] for a, b in zip(points, points[1:])),
            "observed_feed_rows": sum(r["reusd_feed"] is not None for r in rows),
            "counterfactual_feed_rows": sum(r["reusd_feed"] is None for r in rows),
            "max_positive_oracle_gap": max(gaps),
            "min_oracle_gap": min(gaps),
            "oracle_updates": "price_w at sampled blocks subject to configured minimum interval",
        },
        "scope": "Sampled chronological, one-day, normalized soft-liquidation loss; not a parameter recommendation or a model of size-dependent exits, hard liquidation, final bad debt, caps or monetary policy.",
    }
    del rows
    output.update(run(points, config, args.workers, starts, args.exact))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
