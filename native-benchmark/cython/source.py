"""Capture committed source; never build from a mutable working tree."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def checked_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or str(path) != name or "\\" in name:
        raise ValueError(f"Invalid source path: {name}")
    return name


def check_commit(commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Select an exact, full 40-character Git commit, not a branch or tag")


def capture(repository, commit, checkout, destination):
    """Generate a source manifest from Git objects, including tests and resources."""
    check_commit(commit)
    checkout, destination = Path(checkout), Path(destination)
    resolved = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", commit + "^{commit}"], text=True).strip()
    if resolved != commit:
        raise ValueError("Git revision did not resolve to the requested commit")
    entries = subprocess.check_output(["git", "-C", str(checkout), "ls-tree", "-rz", "--full-tree", commit])
    files = {}
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, filename = entry.split(b"\t", 1)
        name = checked_name(filename.decode())
        if name.split("/")[0] not in ("simulator", "tests", "benchmarks") and name not in ("pyproject.toml", "uv.lock", "README.md"):
            continue
        mode, kind, oid = metadata.decode().split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError(f"Source symlinks and submodules require review: {name}")
        data = subprocess.check_output(["git", "-C", str(checkout), "cat-file", "blob", oid])
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    if not files or "simulator/__init__.py" not in files:
        raise ValueError("Selected commit does not contain the simulator package")
    epoch = subprocess.check_output(["git", "-C", str(checkout), "show", "-s", "--format=%ct", commit], text=True).strip()
    return {"schema": 1, "repository": repository, "commit": commit, "files": files, "source_date_epoch": epoch}


def stage_snapshot(source, destination, manifest):
    """Offline snapshots require a manifest; every selected file is checked."""
    source, destination = Path(source), Path(destination)
    check_commit(manifest["commit"])
    archive = tarfile.open(source) if source.is_file() else None
    try:
        if archive and sha(source) != manifest.get("archive_sha256"):
            raise ValueError("Source archive does not match its manifest")
        for name, expected in manifest["files"].items():
            checked_name(name)
            if archive:
                member = archive.getmember(name)
                if not member.isfile():
                    raise ValueError(f"Source member is not a regular file: {name}")
                data = archive.extractfile(member).read()
            else:
                path = source / name
                if not path.resolve().is_relative_to(source.resolve()) or path.is_symlink():
                    raise ValueError(f"Source symlink is unsupported: {name}")
                data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f"Source does not match manifest: {name}")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    finally:
        if archive:
            archive.close()


def verify_source(directory, manifest):
    for name, expected in manifest["files"].items():
        if sha(Path(directory) / checked_name(name)) != expected:
            raise ValueError(f"Authoritative source changed: {name}")
