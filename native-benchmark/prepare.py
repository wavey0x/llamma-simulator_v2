"""Build a pinned baseline or candidate using the existing frozen replay inputs."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "cython"))
from build import clean_environment, toolchain
from pipeline import run as pipeline
from source import check_commit, sha, stage_snapshot, write_json

CURVE = "0bb370f02970c2c1056da8a4faffd0fa9be35575"
PHIL = "e9487b9fb5602f2a343f40276508966c1edb526d"
HEADERS_SHA256 = "618a65240fa757a204cd31f2e28cad1c94c030e1c4c5c4260b65cf92a8bcd8e1"


def build_phil(output):
    output.mkdir()
    started = time.perf_counter()
    for remote, local in (("src/ref_model_v2.cpp", "ref_model_v2.cpp"), ("Makefile", "Makefile")):
        url = f"https://raw.githubusercontent.com/phil-svg/curve/{PHIL}/cpp-src/{remote}"
        with urlopen(url, timeout=60) as response:
            (output / local).write_bytes(response.read())
    if sha(output / "ref_model_v2.cpp") != "0594794c295e3f59cbf6154efc7d616ba56b2dff76422aa5a5c0585cf4364f68":
        raise ValueError("Phil source differs from the frozen benchmark")
    archive = HERE / "vendor/nlohmann-3.12.0.tar.gz"
    if sha(archive) != HEADERS_SHA256:
        raise ValueError("Frozen JSON headers changed")
    with tarfile.open(archive) as source:
        source.extractall(output / "include", filter="data")
    shutil.copy2(HERE / "driver.cpp", output / "driver.cpp")
    lock = json.loads((HERE / "cython/toolchain.json").read_text())
    compiler, environment = toolchain(lock)
    env = clean_environment(lock, compiler)
    sdk = Path(env["SDKROOT"])
    flags = ["-std=c++17", "-O3", "-mcpu=native", "-DNDEBUG", "-flto", "-fno-math-errno"]
    command = [compiler, *flags, "-dynamiclib", "-isysroot", str(sdk), "-isystem", str(sdk / "usr/include/c++/v1"),
               "-Iinclude", "driver.cpp", "-o", "phil.dylib"]
    with (output / "build.log").open("w") as log:
        subprocess.run(command, cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    write_json(output / "build.json", {"commit": PHIL, "flags": flags, "command": command,
               "environment": environment, "seconds": time.perf_counter() - started,
               "files": {str(p.relative_to(output)): sha(p) for p in sorted(output.rglob("*")) if p.is_file()}})


def prepare(args):
    commit = CURVE if args.action == "baseline" else args.commit
    if commit is None:
        raise ValueError("Candidate preparation requires --commit with a full Git SHA")
    check_commit(commit)
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError("Benchmark outputs are immutable; choose a new directory")
    output.mkdir(parents=True)
    profile = "default" if args.action == "baseline" else "native"
    repository = f"https://github.com/{'curvefi' if args.action == 'baseline' else 'wavey0x'}/llamma-simulator_v2"
    pipeline(argparse.Namespace(output=output / "curve", commit=commit, source=args.source,
                               repository=repository, profile=HERE / f"cython/profiles/{profile}.json",
                               source_manifest=None, windows=48, reproducibility=args.reproducibility))
    candidate = output / "curve/candidate"
    manifest = json.loads((candidate / "source.json").read_text())
    stage_snapshot(candidate, output / "python", manifest)
    shutil.copytree(HERE / "inputs", output / "inputs")
    if args.action == "baseline":
        build_phil(output / "phil")
    print(output, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("baseline", "candidate"))
    parser.add_argument("--source", type=Path, default=HERE.parent, help="Git checkout; only committed objects are built")
    parser.add_argument("--commit", help="Candidate commit; the upstream baseline is pinned in this script")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reproducibility", action="store_true", help="Optional second clean build; unnecessary for routine experiments")
    prepare(parser.parse_args())
