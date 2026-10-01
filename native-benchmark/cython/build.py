"""Build any compatible pinned Python revision into a C++ extension candidate."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib

from source import capture, check_commit, digest, sha, stage_snapshot, verify_source, write_json

ROOT = Path(__file__).resolve().parent
REPOSITORY = "https://github.com/curvefi/llamma-simulator_v2"


class Incompatible(ValueError):
    """A reviewed profile or declaration change is required."""


def check_requirements(project, label):
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    metadata = tomllib.loads(project.read_text()).get("project", {})
    if platform.python_version() not in SpecifierSet(metadata.get("requires-python", "")):
        raise Incompatible(f"{label} requires a different Python version")
    for requirement in metadata.get("dependencies", []):
        req = Requirement(requirement)
        if req.marker and not req.marker.evaluate():
            continue
        try:
            version = importlib.metadata.version(req.name)
        except importlib.metadata.PackageNotFoundError:
            raise Incompatible(f"{label} dependency {req.name} is missing; update the environment lock explicitly") from None
        if req.url or version not in req.specifier:
            raise Incompatible(f"{label} dependency {requirement} is incompatible with the locked environment")


def toolchain(lock):
    if sys.flags.optimize:
        raise Incompatible("Build with Python assertions enabled; remove -O or PYTHONOPTIMIZE")
    compiler = shutil.which("clang++")
    if compiler is None:
        raise Incompatible("The pinned clang++ toolchain is unavailable")
    actual = {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version(),
              "compiler": "\n".join(line for line in subprocess.check_output([compiler, "--version"], text=True).splitlines()
                                    if not line.startswith("InstalledDir:"))}
    if actual["system"] == "Darwin":
        actual["sdk_version"] = subprocess.check_output(["xcrun", "--show-sdk-version"], text=True).strip()
        actual["deployment_target"] = lock["target"]["deployment_target"]
    if actual != lock["target"]:
        raise Incompatible(f"Unsupported build target; expected {lock['target']}, found {actual}")
    check_requirements(ROOT / "pyproject.toml", "Build")
    environment = {"target": actual, "abi": sysconfig.get_config_var("SOABI"),
                   "packages": dict(sorted((d.metadata["Name"].lower(), d.version) for d in importlib.metadata.distributions()))}
    return compiler, environment


def clean_environment(lock, compiler):
    excluded = {"PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
                "ARCHFLAGS", "CC", "CXX", "LDSHARED", "LDCXXSHARED", "SDKROOT", "CPATH", "C_INCLUDE_PATH",
                "CPLUS_INCLUDE_PATH", "LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES"}
    env = {k: v for k, v in os.environ.items() if k not in excluded}
    env.update(CC=compiler, CXX=compiler, PYTHONHASHSEED="0", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               LC_ALL="C", TZ="UTC", SOURCE_DATE_EPOCH=lock.get("source_date_epoch", "0"))
    env["MACOSX_DEPLOYMENT_TARGET"] = lock["target"]["deployment_target"]
    env["SDKROOT"] = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    return env


def compatibility(directory, profile):
    """Check inputs; Cython itself checks the declarations against the Python."""
    for module, declaration in profile["modules"].items():
        path = Path(directory) / (module.replace(".", "/") + ".py")
        if not path.is_file():
            raise Incompatible(f"Required module missing: {module}")
        if declaration and path.with_suffix(".pxd").exists():
            raise Incompatible(f"Upstream declarations need review before overlaying: {module}")
    project = Path(directory) / "pyproject.toml"
    if project.exists():
        check_requirements(project, "Upstream")


def build(commit, output, source=None, *, repository=REPOSITORY, profile=None, source_manifest=None):
    check_commit(commit)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("Build outputs are immutable; choose a new directory")
    profile_path = Path(profile) if profile else ROOT / "profiles/default.json"
    recipe = json.loads(profile_path.read_text())
    lock = json.loads((ROOT / "toolchain.json").read_text())
    compiler, environment = toolchain(lock)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cython-build-", dir=output.parent) as temporary:
        work = Path(temporary)
        staged = work / "release"
        if source_manifest:
            manifest = json.loads(Path(source_manifest).read_text())
            if manifest["commit"] != commit:
                raise Incompatible("Source manifest does not identify the requested commit")
            stage_snapshot(source or ROOT / "python-source.tar.gz", staged, manifest)
        else:
            checkout = Path(source).resolve() if source else work / "checkout"
            if source is None:
                subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
                subprocess.run(["git", "-C", str(checkout), "fetch", "--quiet", "--depth=1", repository, commit], check=True)
            manifest = capture(repository, commit, checkout, staged)
        compatibility(staged, recipe)
        for module, name in recipe["modules"].items():
            if name:
                shutil.copy2(ROOT / name, staged / (module.replace(".", "/") + ".pxd"))
        builder_files = [ROOT / name for name in ("build.py", "source.py", "setup.py", "toolchain.json", "pyproject.toml", "uv.lock")]
        builder_files += [ROOT / name for name in recipe["modules"].values() if name]
        identity = {"source": manifest, "profile": recipe,
                    "builder": {str(p.relative_to(ROOT)): sha(p) for p in builder_files}, "environment": environment}
        config = {"modules": list(recipe["modules"]), "flags": lock["flags"], "language": "c++",
                  "directives": recipe["directives"]}
        shutil.copy2(ROOT / "setup.py", staged / "setup.py")
        write_json(staged / "build-config.json", config)
        env = clean_environment(lock | {"source_date_epoch": manifest["source_date_epoch"]}, compiler)
        with (staged / "build.log").open("w") as log:
            process = subprocess.run([sys.executable, "-s", "setup.py", "build_ext", "--inplace"], cwd=staged, env=env,
                                     stdout=log, stderr=subprocess.STDOUT)
        if process.returncode:
            raise Incompatible("Cython compilation failed; review source compatibility:\n" + (staged / "build.log").read_text()[-5000:])
        verify_source(staged, manifest)
        suffix = sysconfig.get_config_var("EXT_SUFFIX")
        artifacts = {module.replace(".", "/") + suffix: sha(staged / (module.replace(".", "/") + suffix))
                     for module in recipe["modules"]}
        generated = {module.replace(".", "/") + ".cpp": sha(staged / (module.replace(".", "/") + ".cpp"))
                     for module in recipe["modules"]}
        record = {"schema": 2, "identity": identity, "build_id": digest(identity), "artifacts": artifacts,
                  "artifact_hash": digest(artifacts), "generated": generated}
        write_json(staged / "source.json", manifest)
        write_json(staged / "build.json", record)
        shutil.rmtree(staged / "build")
        os.rename(staged, output)
    return record


def arguments(parser):
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source", type=Path, help="Git checkout; committed objects only")
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--profile", type=Path, default=ROOT / "profiles/default.json")
    parser.add_argument("--source-manifest", type=Path, help="Manifest for an offline source archive or snapshot")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    args = parser.parse_args()
    print(json.dumps(build(args.commit, args.output, args.source, repository=args.repository,
                           profile=args.profile, source_manifest=args.source_manifest), indent=2))
