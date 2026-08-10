#!/usr/bin/env python3
"""Persist the exact immutable releases used by a completed Linky projection run."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

from linky_runtime import atomic_json
from linky_sync_runner import valid_batch_id


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_files(path: Path) -> tuple[dict, dict[str, dict]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value, {str(item["path"]): item for item in value.get("files", [])}


def build_receipt(batch_id: str, auto_root: Path, backend_root: Path) -> dict:
    if not valid_batch_id(batch_id):
        raise ValueError("unsafe batch id")
    auto_root = auto_root.resolve(strict=True)
    backend_root = backend_root.resolve(strict=True)
    auto_manifest, auto_files = manifest_files(auto_root / "release-manifest.json")
    runner = auto_root / "linky_sync_runner.py"
    runner_item = auto_files.get("linky_sync_runner.py")
    if not runner_item or sha256(runner) != runner_item.get("sha256"):
        raise ValueError("auto release runner checksum mismatch")

    metadata = json.loads((backend_root / "release-metadata.json").read_text(encoding="utf-8"))
    task_manifest = backend_root / "api/task-chain-manifest.sha256"
    if metadata.get("taskManifestHash") != sha256(task_manifest):
        raise ValueError("backend task manifest checksum mismatch")
    recorded = {}
    for line in task_manifest.read_text(encoding="utf-8").splitlines():
        checksum, relative = line.split(maxsplit=1)
        recorded[relative.lstrip("* ")] = checksum
    projection_relative = "api/src/scripts/refresh-operations-projections.ts"
    projection = backend_root / projection_relative
    if recorded.get(projection_relative) != sha256(projection):
        raise ValueError("backend projection checksum mismatch")

    return {
        "schemaVersion": 1,
        "status": "PASSED",
        "batchId": batch_id,
        "completedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "autoReleaseRoot": str(auto_root),
        "autoCommit": auto_manifest.get("commit"),
        "autoRunnerSha256": runner_item["sha256"],
        "backendReleaseRoot": str(backend_root),
        "backendCommit": metadata.get("commit"),
        "backendTaskManifestHash": metadata.get("taskManifestHash"),
        "projectionPath": projection_relative,
        "projectionSha256": recorded[projection_relative],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--auto-release-root", type=Path, required=True)
    parser.add_argument("--backend-release-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = build_receipt(args.batch_id, args.auto_release_root, args.backend_release_root)
    target = args.state_root / "runtime-receipts" / f"linky-{args.batch_id}.json"
    atomic_json(target, receipt)
    atomic_json(args.state_root / "runtime-receipts" / "linky-latest.json", receipt)
    print(json.dumps({"runtimeReceipt": str(target), "status": "PASSED"}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
