"""Randomized checks against three pinned Python revisions; standard library only."""
import argparse
import hashlib
import importlib.util
import json
import logging
import math
import random
import struct
from pathlib import Path
from types import SimpleNamespace

if not __debug__:
    raise RuntimeError("Run these assertion-based research checks without -O")
parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, default=Path.cwd())
parser.add_argument("--candidate", required=True)
parser.add_argument("--output", type=Path, default=Path("correctness.json"))
args = parser.parse_args()
spec = importlib.util.spec_from_file_location("benchmark", args.repo / "benchmarks/benchmark_amm.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
refs = {"base": "f18e1231bdc47798314463d1595c69b9dcaacd6f", "pr": "e94f9d196750aba623d2b8bd3a3954e8b5abb65c", "candidate": args.candidate}
HEAD = refs
SOURCES = {label: (b.source_at(ref, "simulator/amm/lending_amm.py"), b.source_at(ref, "simulator/amm/simulator.py")) for label, ref in refs.items()}
MODULES = {label: b.load_source(amm, label) for label, (amm, _) in SOURCES.items()}
liquidities = [b.source_at(ref, "simulator/amm/intitial_liquidity.py") for ref in refs.values()]
assert len(set(liquidities)) == 1
liq = b.load_source(liquidities[0], "liquidity")
CONTEXT = SimpleNamespace(initial_liquidity_class=liq.ConstantInitialLiquidity, external_fee=.0005, log_enabled=False, verbose=False)
def digest(values):
    return hashlib.sha256(struct.pack(f"<{len(values)}d", *values)).hexdigest()
def write(name, result):
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
def state(a):
    result = dict(vars(a))
    for name in ('bands_x', 'bands_y'):
        result[name] = {k: v for k, v in result[name].items() if v != 0}
    return result

def verify():
    rng = random.Random(2026093011)
    comparisons = 0
    for case in range(1500):
        A = rng.choice([10, 50, 100, 300, 600])
        p = 10 ** rng.uniform(-4, 5)
        holdings = {i: (rng.choice([0., 10 ** rng.uniform(-8, 5)]), rng.choice([0., 10 ** rng.uniform(-8, 5)])) for i in rng.sample(range(-500, 500), rng.choice([1, 4, 20, 100]))}
        holdings[-501] = holdings[500] = (1., 1.)
        reference = None
        for label in SOURCES:
            a = MODULES[label].LendingAMM(p, A, .003)
            a.p_oracle = p * rng.uniform(.8, 1.2) if label == 'base' else oracle
            oracle = a.p_oracle
            for n, (x, y) in holdings.items():
                a.bands_x[n], a.bands_y[n] = x, y
            values = [a.get_all_x(), a.get_all_y()]
            if reference is None:
                reference = values
            assert digest(values) == digest(reference), (case, label)
            comparisons += 2
    # Each replay captures final financial state and complete logging output.
    saved = []
    class LogCapture(logging.Handler):
        def emit(self, rec):
            saved.append(rec.getMessage())
    logger = logging.getLogger('benchmark')
    logger.addHandler(LogCapture())
    logger.setLevel(logging.INFO)
    replay_count = 0
    failures = []
    for case in range(1000):
        A = rng.choice([10, 50, 100, 300, 600])
        fee = rng.choice([0., .0001, .001, .005, .02])
        bands = rng.choice([1, 4, 20, 50])
        multiplier = rng.choice([None, 0., .25, .5, 1.])
        shift = rng.choice([0., .1, .5])
        p = 10 ** rng.uniform(-3, 4)
        t = 1700000040
        rows, oracles = [], []
        for i in range(rng.choice([1, 2, 30, 60])):
            t += rng.choice([0, 12, 60, 119, 120, 121, 180])
            opening = p * (1 + .04 * math.sin(i / 3))
            rows.append([t, opening, opening * 1.015, opening * .985, opening, 1.])
            oracles.append(p * rng.choice([.8, 1., 1.25]) if i % 17 == 0 else p)
        context = SimpleNamespace(**vars(CONTEXT))
        context.external_fee = rng.choice([0., .0005, .005])
        context.log_enabled = case < 80 and bool(case % 2)
        context.verbose = case < 80 and bool((case // 2) % 2)
        ref = None
        for label in SOURCES:
            captured = []
            cls = MODULES[label].LendingAMM
            def factory(*args):
                obj = cls(*args)
                captured.append(obj)
                return obj
            fn = b.loss_function(factory, SOURCES[label][1])
            saved.clear()
            try:
                value = fn(context, A, fee, rows, oracles, bands, multiplier, shift)
                outcome = ('ok', value.hex())
            except Exception as exc:
                outcome = (type(exc).__name__, str(exc))
            result = (outcome, state(captured[0]), saved[:])
            if ref is None:
                ref = result
                if outcome[0] != 'ok':
                    failures.append({'case': case, 'error': outcome})
            assert result == ref, (case, label, outcome, ref[0])
        replay_count += 1
    write('correctness.json', {'head': HEAD, 'valuation_cases': 1500, 'valuation_comparisons': comparisons, 'replay_cases': replay_count, 'variants': list(SOURCES), 'bitwise_losses_equal': True, 'nonzero_balances_oracle_state_and_logs_equal': True, 'matching_preexisting_failures': failures})


verify()
