"""Build and check a pinned commit; optionally qualify clean-build reproducibility."""
import argparse
import json
from pathlib import Path

from build import Incompatible, ROOT, arguments, build
from source import digest, sha, write_json
from verify import verify


def run(args):
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError("Pipeline outputs are immutable; choose a new directory")
    output.mkdir(parents=True)
    receipt = {"status": "failed", "commit": args.commit,
               "reproducibility": "not completed" if args.reproducibility else "not requested",
               "pipeline_sha256": sha(ROOT / "pipeline.py")}
    try:
        options = dict(source=args.source, repository=args.repository, profile=args.profile, source_manifest=args.source_manifest)
        first = build(args.commit, output / "candidate", **options)
        receipt.update(build_id=first["build_id"], artifact_hash=first["artifact_hash"])
        verify(output / "candidate", output / "validation", windows=args.windows)
        if args.reproducibility:
            # Rebuild the exact captured source offline in a separate clean directory.
            second = build(args.commit, output / "rebuild", source=output / "candidate", profile=args.profile,
                           source_manifest=output / "candidate/source.json")
            if any(first[key] != second[key] for key in ("build_id", "artifacts", "generated")):
                raise ValueError("Clean builds differ; inspect build receipts before accepting this configuration")
            receipt["reproducibility"] = "identical generated C++ and extension binaries"
        receipt["status"] = "verified"
    except Exception as exc:
        receipt.update(status="incompatible" if isinstance(exc, Incompatible) else "failed", error=str(exc))
        raise
    finally:
        verification = output / "validation/verification.json"
        if verification.is_file():
            receipt["verification"] = {"path": "validation/verification.json", "sha256": sha(verification)}
        receipt["evidence_id"] = digest(receipt)
        write_json(output / "release.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    parser.add_argument("--windows", type=int, default=48)
    parser.add_argument("--reproducibility", action="store_true", help="Also build again and compare bytes; no sweeps")
    args = parser.parse_args()
    print(json.dumps(run(args), indent=2))
