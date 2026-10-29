"""Generate a source manifest on the GitHub runner before building the image."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def build_manifest(root: Path, revision: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be a full Git SHA")
    files = {
        str(path.relative_to(root)).replace(chr(92), "/"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "app").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        and path.name != "deployment_manifest.json"
    }
    for name in ("requirements.txt",):
        files[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    return {"revision": revision, "files": files}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / "app/deployment_manifest.json"
    output.write_text(json.dumps(build_manifest(root, args.revision), sort_keys=True, indent=2), encoding="utf-8")
