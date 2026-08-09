#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


CODE_SUFFIXES = (".sh", ".py", ".mjs", ".js", ".ts")


def load_manifest(path: Path) -> dict[str, dict]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return {item["path"]: item for item in manifest.get("files", [])}


def is_code_reference(line: str, root: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or root not in stripped:
        return False
    before_redirect = re.split(r"\s(?:>>?|2>>?)", stripped, maxsplit=1)[0]
    if root not in before_redirect:
        return False
    command = re.search(r"(^|[;&|]\s*)(source|\.|exec|bash|sh|python3?|node|npx|tsx|cd)\s+", before_redirect)
    return bool(command) or any(suffix in before_redirect for suffix in CODE_SUFFIXES)


def absolute_code_paths(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return []
    before_redirect = re.split(r"\s(?:>>?|2>>?)", stripped, maxsplit=1)[0]
    if not re.search(r"(^|[;&|]\s*)(source|\.|exec|bash|sh|python3?|node|npx|tsx|cd)\s+", before_redirect):
        return []
    return re.findall(r"/home/ubuntu/[A-Za-z0-9_./${}-]+", before_redirect)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    policy = json.loads((root / "runtime-closure-policy.json").read_text(encoding="utf-8"))
    closure = {name for name in policy["entrypoints"]}
    for dependencies in policy["entrypoints"].values():
        closure.update(dependencies)

    failures: list[str] = []
    for name in sorted(closure):
        path = root / name
        if not path.is_file():
            failures.append(f"missing release dependency: {name}")
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        for forbidden in policy["forbiddenCodeRoots"]:
            for number, line in enumerate(source.splitlines(), 1):
                if is_code_reference(line, forbidden):
                    failures.append(f"forbidden executable code root: {name}:{number}: {forbidden}")
        allowed = tuple(policy["externalCodeRoots"] + policy["mutableDataRoots"])
        for number, line in enumerate(source.splitlines(), 1):
            for absolute in absolute_code_paths(line):
                if not absolute.startswith(allowed):
                    failures.append(f"unapproved absolute runtime path: {name}:{number}: {absolute}")

    for entry, dependencies in policy["entrypoints"].items():
        source = (root / entry).read_text(encoding="utf-8", errors="replace")
        for dependency in dependencies:
            token = Path(dependency).name
            module_token = Path(dependency).stem
            if token not in source and module_token not in source:
                failures.append(f"undeclared/unverifiable edge: {entry} -> {dependency}")

    for name in policy["executableReleaseFiles"]:
        path = root / name
        if path.is_file() and not os.access(path, os.X_OK):
            failures.append(f"release entry is not executable: {name}")

    if args.manifest:
        manifest = load_manifest(args.manifest)
        for name in sorted(closure):
            if name not in manifest:
                failures.append(f"runtime dependency absent from release manifest: {name}")
        for name in policy["executableReleaseFiles"]:
            item = manifest.get(name)
            if item and not (int(item["mode"], 8) & 0o111):
                failures.append(f"manifest executable bit missing: {name}")

    if failures:
        print("runtime closure: FAILED")
        print("\n".join(failures))
        return 1
    print(f"runtime closure: PASSED ({len(policy['entrypoints'])} entries, {len(closure)} release files)")
    print("external code roots: " + ", ".join(policy["externalCodeRoots"]))
    print("mutable data roots: " + ", ".join(policy["mutableDataRoots"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
