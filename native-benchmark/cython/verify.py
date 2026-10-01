"""Validate a candidate against its own Python source using short fixed fixtures."""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile

from build import ROOT, clean_environment, toolchain
from source import digest, sha, stage_snapshot, verify_source, write_json


def compare(expected, actual, tolerance, path="result"):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or expected.keys() != actual.keys():
            raise ValueError(f"Different result fields at {path}")
        return max((compare(value, actual[key], tolerance, f"{path}.{key}") for key, value in expected.items()), default=0.)
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise ValueError(f"Different result lengths at {path}")
        return max((compare(a, b, tolerance, f"{path}[{i}]") for i, (a, b) in enumerate(zip(expected, actual))), default=0.)
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if not isinstance(actual, (int, float)) or not math.isfinite(expected) or not math.isfinite(actual):
            raise ValueError(f"Invalid numeric result at {path}")
        error = abs(expected - actual)
        if error > tolerance:
            raise ValueError(f"Parity failed at {path}: {expected} != {actual}; absolute error {error}")
        return error
    if type(expected) is not type(actual) or expected != actual:
        raise ValueError(f"Behavior differs at {path}: {expected!r} != {actual!r}")
    return 0.


def checked_build(candidate):
    candidate = Path(candidate)
    record = json.loads((candidate / "build.json").read_text())
    if record["build_id"] != digest(record["identity"]) or record["artifact_hash"] != digest(record["artifacts"]):
        raise ValueError("Build receipt identity changed")
    verify_source(candidate, record["identity"]["source"])
    for name, expected in {**record["artifacts"], **record["generated"]}.items():
        if sha(candidate / name) != expected:
            raise ValueError(f"Build artifact changed: {name}")
    return record


def verify(candidate, output, *, windows=48, tolerance=5e-14):
    candidate, output = Path(candidate).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Verification evidence is immutable; choose a new output directory")
    if windows < 1 or tolerance < 0 or not math.isfinite(tolerance):
        raise ValueError("Invalid verification settings")
    record = checked_build(candidate)
    lock = json.loads((ROOT / "toolchain.json").read_text())
    compiler, environment = toolchain(lock)
    if environment != record["identity"]["environment"]:
        raise ValueError("Validation environment differs from the build")
    env = clean_environment(lock, compiler)
    output.mkdir(parents=True)
    receipt = {"status": "failed", "build_id": record["build_id"], "artifact_hash": record["artifact_hash"],
               "scope": "fixed short replay and AMM fixtures; no parameter sweeps", "windows": windows,
               "absolute_tolerance": tolerance, "checks": {name: sha(ROOT / name) for name in ("verify.py", "check.py")}}
    try:
        with tempfile.TemporaryDirectory(prefix="python-reference-") as temporary:
            reference = Path(temporary)
            stage_snapshot(candidate, reference, record["identity"]["source"])
            results = []
            for mode, directory in (("python", reference), ("compiled", candidate)):
                path = output / (mode + ".json")
                command = [sys.executable, "-s", str(ROOT / "check.py"), "--source", str(directory),
                           "--mode", mode, "--output", str(path), "--windows", str(windows),
                           "--modules", *record["identity"]["profile"]["modules"]]
                with (output / (mode + ".log")).open("w+") as log:
                    process = subprocess.run(command, cwd=reference, env=env, stdout=log, stderr=subprocess.STDOUT)
                    if process.returncode:
                        log.seek(0)
                        raise ValueError(f"{mode} fixture failed:\n" + log.read()[-4000:])
                result = json.loads(path.read_text())
                result.pop("modules")
                results.append(result)
            loss_error = compare(results[0]["ordered_losses"], results[1]["ordered_losses"], tolerance, "losses")
            # State values may be larger than loss fractions; preserve a separately stated gate.
            state_error = compare(results[0]["probes"], results[1]["probes"], 1e-10, "state")
            compare(results[0]["errors"], results[1]["errors"], 0., "errors")
            checked_build(candidate)
            receipt.update(status="passed", maximum_loss_error=loss_error, maximum_state_error=state_error,
                           state_absolute_tolerance=1e-10, repeatability="bitwise within each backend")
    except Exception as exc:
        receipt["error"] = str(exc)
        write_json(output / "verification.json", receipt)
        raise
    receipt["evidence"] = {name: sha(output / name) for name in ("python.json", "compiled.json")}
    write_json(output / "verification.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--windows", type=int, default=48)
    args = parser.parse_args()
    print(json.dumps(verify(args.candidate, args.output, windows=args.windows), indent=2))
