"""Generate a release manifest from an archive of an explicit reviewed commit.

Never reads workspace source files: all bytes come from git archive. The output
is a new build directory, not a deployed release or data directory.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile


def build_context(repository, commit, destination):
    resolved = subprocess.check_output(["git", "-C", str(repository), "rev-parse", commit + "^{commit}"], text=True).strip()
    if len(commit) != 40 or resolved != commit:
        raise ValueError("An exact full commit hash is required")
    destination = Path(destination)
    destination.mkdir(exist_ok=False, parents=True)
    archive = subprocess.check_output(["git", "-C", str(repository), "archive", "--format=tar", commit])
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or not (member.isfile() or member.isdir()):
                raise ValueError("Archive contains an unsafe path or link")
        bundle.extractall(destination)
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    package = destination / "src" / "mietinkasso"
    manifest = {"commit": commit,
                "package": {p.relative_to(package).as_posix(): digest(p) for p in sorted(package.rglob("*"))
                            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"},
                "scripts": {name: digest(destination / "scripts" / name) for name in
                            ["indexautomatik_taegliche_pflege.py", "indexautomatik_monatslauf.py"]}}
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    (destination / "runtime-tree-manifest.json").write_bytes(payload)
    return {"commit": commit, "manifest_sha256": hashlib.sha256(payload).hexdigest(),
            "archive_sha256": hashlib.sha256(archive).hexdigest(), "build_context": str(destination)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("commit")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_context(args.repository, args.commit, args.destination)))
