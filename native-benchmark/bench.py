"""Interleaved replays of fixed inputs with one common process-pool driver."""
import argparse
import ctypes
import importlib
import importlib.machinery
import json
import multiprocessing as mp
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "cython"))
from source import sha, write_json
from verify import checked_build
from prepare import CURVE, PHIL

BACKENDS = ("python", "cython", "fork-python", "fork-cython", "phil")
MARKETS = ("ETH", "BTC", "LP")
TOLERANCE = 5e-14
DOUBLE_P = ctypes.POINTER(ctypes.c_double)


def ptr(array):
    return array.ctypes.data_as(DOUBLE_P)


def initialize(backend, root, ready=None):
    global BACKEND, POINTS, ROWS, ORACLES, SIM, LIB, DATA, REPLAY
    BACKEND = backend
    POINTS = np.load(root / "inputs/prices.npy")
    loaded = {}
    if backend == "phil":
        path = root / "phil/phil.dylib"
        LIB = ctypes.CDLL(str(path))
        LIB.load_points.argtypes = [DOUBLE_P, ctypes.c_size_t]
        LIB.load_points.restype = ctypes.c_void_p
        LIB.replay.argtypes = [ctypes.c_void_p, DOUBLE_P, ctypes.c_size_t, DOUBLE_P]
        LIB.replay.restype = None
        LIB.free_points.argtypes = [ctypes.c_void_p]
        LIB.free_points.restype = None
        DATA = LIB.load_points(ptr(POINTS), len(POINTS))
        loaded["library"] = str(path.relative_to(root))
    else:
        native = backend in ("cython", "fork-cython")
        source = root / ("curve/candidate" if native else "python")
        sys.path.insert(0, str(source))
        for name in ("simulator.amm.lending_amm", "simulator.amm.simulator"):
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve()
            compiled = any(str(path).endswith(s) for s in importlib.machinery.EXTENSION_SUFFIXES)
            if not path.is_relative_to(source) or compiled != native:
                raise ValueError(f"Wrong {backend} module: {path}")
            loaded[name] = str(path.relative_to(root))
        from simulator.amm.simulator import Simulator
        from simulator.amm.intitial_liquidity import ConstantInitialLiquidity
        SIM = Simulator.__new__(Simulator)
        SIM.initial_liquidity_class = ConstantInitialLiquidity
        SIM.log_enabled = SIM.verbose = False
        ROWS = POINTS[:, :6].tolist()
        ORACLES = POINTS[:, 6].tolist()
        if backend.startswith("fork-"):
            from simulator.amm.simulator import replay_batch
            REPLAY = replay_batch
    if ready is not None:
        ready.put(loaded)
    return loaded


def batch(records):
    if BACKEND.startswith("fork-"):
        return REPLAY(SIM, POINTS, records)
    if BACKEND == "phil":
        output = np.empty(len(records), dtype="<f8")
        LIB.replay(DATA, ptr(records), len(records), ptr(output))
        return output
    output = []
    for a, fee, lo, hi, bands, external, mult, _ in records:
        lo, hi = int(lo), int(hi)
        SIM.external_fee = float(external)
        output.append(SIM.calculate_loss(int(a), float(fee), ROWS[lo:hi], ORACLES[lo:hi], int(bands), float(mult)))
    return np.asarray(output, dtype="<f8")


def worker(args):
    root = (args.root if args.backend.startswith("fork-") else args.baseline).resolve()
    output = args.output.resolve()
    tasks = np.load(root / "inputs/tasks.npy")
    workload = json.loads((root / "inputs/workload.json").read_text())
    previous, pool = {}, None
    try:
        if args.workers == 1:
            loaded = [initialize(args.backend, root)]
            execute = batch
        else:
            ctx = mp.get_context("spawn")
            ready = ctx.Queue()
            pool = ctx.Pool(args.workers, initializer=initialize, initargs=(args.backend, root, ready))
            loaded = [ready.get(timeout=120) for _ in range(args.workers)]
            def execute(records):
                chunks = np.array_split(records, min(len(records), args.workers * 8))
                return np.concatenate(pool.map(batch, chunks, chunksize=1))
        print(json.dumps({"ready": True, "loaded": loaded}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request["action"] == "close":
                break
            market = request["market"]
            lo, hi = workload["markets"][market]["tasks"]
            records = tasks[lo:hi]
            warmup = request["action"] == "warmup"
            if warmup:
                records = records[np.linspace(0, len(records) - 1, 128, dtype=int)]
            loops = 32 if args.backend == "phil" and not warmup else 1
            start = time.perf_counter()
            results = [execute(records) for _ in range(loops)]
            total_seconds = time.perf_counter() - start
            seconds = total_seconds / loops
            losses = results[-1]
            if losses.shape != (len(records),) or not np.isfinite(losses).all():
                raise ValueError("Missing or nonfinite losses")
            if any(result.tobytes() != losses.tobytes() for result in results):
                raise ValueError("Nonrepeatable repeated batch")
            filename = f"{args.backend}-{args.workers}-{market}.npy"
            if not warmup:
                if market in previous and losses.tobytes() != previous[market].tobytes():
                    raise ValueError("Nonrepeatable ordered losses")
                if market not in previous:
                    np.save(output / filename, losses)
                    previous[market] = losses
            print(json.dumps({"seconds": seconds, "total_seconds": total_seconds, "batches": loops,
                              "windows": len(records), "file": filename}), flush=True)
    finally:
        if pool:
            pool.terminate()
            pool.join()
        elif args.backend == "phil" and "DATA" in globals():
            LIB.free_points(DATA)


def comparisons(output):
    rows = []
    for market in MARKETS:
        reference = np.load(output / f"python-1-{market}.npy")
        for backend in BACKENDS:
            one = np.load(output / f"{backend}-1-{market}.npy")
            four = np.load(output / f"{backend}-4-{market}.npy")
            if one.tobytes() != four.tobytes():
                raise ValueError(f"{backend}/{market}: worker count changed results")
            difference = np.abs(reference - one)
            index = int(difference.argmax())
            rows.append({"backend": backend, "market": market, "windows": len(one),
                         "maximum_absolute_error": float(difference[index]), "worst_index": index,
                         "python_loss": float(reference[index]), "backend_loss": float(one[index]),
                         "above_tolerance": int(np.count_nonzero(difference > TOLERANCE)),
                         "workers_bitwise_equal": True})
    write_json(output / "comparison.json", {"absolute_tolerance": TOLERANCE, "results": rows})
    if any(r["above_tolerance"] for r in rows if r["backend"] != "phil"):
        raise ValueError("Cython differs from Python beyond the preset tolerance; inspect comparison.json")
    return rows


def run(args):
    root, output = args.root.resolve(), args.output.resolve()
    if output.exists():
        raise FileExistsError("Benchmark outputs are immutable; choose a new directory")
    if args.repeats < 1:
        raise ValueError("At least one trial is required")
    from source import verify_source
    builds = []
    for directory in (root, args.baseline.resolve()):
        release = json.loads((directory / "curve/release.json").read_text())
        if release["status"] != "verified" or sha(directory / "curve" / release["verification"]["path"]) != release["verification"]["sha256"]:
            raise ValueError("Pipeline verification is missing or changed")
        record = checked_build(directory / "curve/candidate")
        verify_source(directory / "python", record["identity"]["source"])
        builds.append(record)
        for name in ("prices.npy", "tasks.npy", "workload.json", "original-protocol.json"):
            if sha(directory / "inputs" / name) != sha(ROOT / "inputs" / name):
                raise ValueError(f"Workload differs from the frozen handoff: {name}")
    built, baseline = builds
    workload = json.loads((root / "inputs/workload.json").read_text())
    phil_root = args.baseline.resolve() / "phil"
    phil = json.loads((phil_root / "build.json").read_text())
    if baseline["identity"]["source"]["commit"] != CURVE or phil["commit"] != PHIL:
        raise ValueError("Baseline commits differ from the handoff pins")
    for directory, files in ((root / "inputs", workload["files"]), (phil_root, phil["files"])):
        for name, expected in files.items():
            if sha(directory / name) != expected:
                raise ValueError(f"Changed benchmark input: {directory / name}")
    output.mkdir(parents=True)
    clients, logs, startup, trials = {}, [], {}, []

    def request(client, action, market=None):
        client.stdin.write(json.dumps({"action": action, "market": market}) + "\n")
        client.stdin.flush()
        line = client.stdout.readline()
        if not line:
            raise RuntimeError("Benchmark worker exited; inspect its log")
        return json.loads(line)

    try:
        for workers in (1, 4):
            for backend in BACKENDS:
                key = f"{backend}-{workers}"
                log = (output / (key + ".log")).open("w")
                logs.append(log)
                start = time.perf_counter()
                client = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", "--root", str(root),
                                           "--baseline", str(args.baseline.resolve()),
                                           "--output", str(output), "--backend", backend, "--workers", str(workers)],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
                clients[key] = client
                ready = client.stdout.readline()
                if not ready:
                    raise RuntimeError(f"{key} startup failed; inspect its log")
                startup[key] = {"seconds": time.perf_counter() - start, **json.loads(ready)}
                for _ in range(2):
                    for market in MARKETS:
                        request(client, "warmup", market)
        write_json(output / "startup.json", startup)
        for repeat in range(args.repeats):
            order = list(clients)
            order = order[repeat % len(order):] + order[:repeat % len(order)]
            if repeat % 2:
                order.reverse()
            for key in order:
                for market in MARKETS[repeat % 3:] + MARKETS[:repeat % 3]:
                    result = request(clients[key], "run", market)
                    row = {"trial": repeat, "configuration": key, "market": market, **result}
                    trials.append(row)
                    print(json.dumps(row), flush=True)
                    write_json(output / "timings.json", trials)
            comparisons(output)
        summary = []
        for key in clients:
            for market in MARKETS:
                times = [r["seconds"] for r in trials if r["configuration"] == key and r["market"] == market]
                lo, hi = workload["markets"][market]["tasks"]
                summary.append({"configuration": key, "market": market, "windows": hi - lo,
                                "median_seconds": statistics.median(times), "min_seconds": min(times), "max_seconds": max(times),
                                "windows_per_second": (hi - lo) / statistics.median(times)})
        write_json(output / "summary.json", summary)
        print("\nSum of per-market medians (seconds):", flush=True)
        print(f"{'Backend':<14} {'1 worker':>12} {'4 workers':>12}", flush=True)
        for backend in BACKENDS:
            totals = [sum(r["median_seconds"] for r in summary if r["configuration"] == f"{backend}-{workers}")
                      for workers in (1, 4)]
            print(f"{backend:<14} {totals[0]:12.6f} {totals[1]:12.6f}", flush=True)
        write_json(output / "receipt.json", {"status": "passed", "repeats": args.repeats,
                   "python": sys.version, "platform": platform.platform(), "machine": subprocess.check_output(["sysctl", "-n", "hw.model"], text=True).strip(),
                   "cpu_cores": mp.cpu_count(), "build_id": built["build_id"], "phil_commit": phil["commit"],
                   "baseline_build_id": baseline["build_id"],
                   "driver_sha256": sha(__file__), "workload_sha256": sha(root / "inputs/workload.json"),
                   "pipeline_release_sha256": sha(root / "curve/release.json"), "phil_build_sha256": sha(phil_root / "build.json"),
                   "files": {p.name: sha(p) for p in sorted(output.iterdir()) if p.suffix in (".npy", ".json")},
                   "scope": "Resident-input replay including dispatch and ordered output return; preparation, process startup and warmup excluded"})
    finally:
        for client in clients.values():
            if client.poll() is None:
                try:
                    client.stdin.write('{"action":"close"}\n')
                    client.stdin.flush()
                    client.wait(timeout=15)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    client.terminate()
                    client.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--backend", choices=BACKENDS)
    parser.add_argument("--workers", type=int, choices=(1, 4), default=1)
    args = parser.parse_args()
    (worker if args.action == "worker" else run)(args)
