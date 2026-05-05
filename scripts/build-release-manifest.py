#!/usr/bin/env python3
"""Build a WaveForward release manifest from distribution artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tomllib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", default="dist", help="Distribution directory.")
    parser.add_argument(
        "--output",
        default="dist/waveforward-release-manifest.json",
        help="Manifest output path.",
    )
    args = parser.parse_args()

    dist = Path(args.dist)
    wheel = _single(dist.glob("waveforward-*-py3-none-any.whl"), "wheel")
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    commit = os.getenv("GITHUB_SHA", "").strip()
    payload = {
        "format": "waveforward.release_manifest",
        "format_version": 1,
        "version": version,
        "commit": commit,
        "wheel": {
            "url": wheel.name,
            "sha256": _sha256(wheel),
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


def _single(paths, label: str) -> Path:
    items = sorted(paths)
    if len(items) != 1:
        raise SystemExit(f"expected exactly one {label}, found {len(items)}")
    return items[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
