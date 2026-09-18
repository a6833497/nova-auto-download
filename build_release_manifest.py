#!/usr/bin/env python3
"""Build the checksum manifest for one immutable auto-download release."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path


MANIFEST_NAME = "release-manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: Path, commit: str, built_at: str, previous_target: str) -> dict:
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("commit must be a full lowercase Git SHA")
    root = root.resolve(strict=True)
    files = []
    for path in sorted(root.rglob("*")):
        if path.name == MANIFEST_NAME or path.is_symlink() or not path.is_file():
            continue
        relative = str(path.relative_to(root))
        files.append({
            "path": relative,
            "sha256": sha256(path),
            "mode": format(path.stat().st_mode & 0o777, "o"),
        })
    return {
        "schemaVersion": 1,
        "commit": commit,
        "builtAt": built_at,
        "previousTarget": previous_target,
        "files": files,
    }


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--built-at", default=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--previous-target", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    output = args.output or root / MANIFEST_NAME
    atomic_write(output, build_manifest(root, args.commit, args.built_at, args.previous_target))
    print(json.dumps({"manifest": str(output), "status": "PASSED"}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
