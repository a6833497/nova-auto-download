#!/usr/bin/env python3
"""Fail-closed static/runtime verifier for the canonical auto-download release."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

CODE_SUFFIXES = {".sh", ".py", ".mjs", ".js", ".ts"}
LOCAL_TOKEN = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.(?:sh|py|mjs|js))(?![A-Za-z0-9_.-])")
DYNAMIC_EXEC = re.compile(r"(?:^|[;&|]\s*)(?:exec|source|\.|bash|sh|python3?|node)\s+[\"']?\$\{?([A-Za-z_][A-Za-z0-9_]*)")
SHELL_STRING_EXEC = re.compile(r"\b(?:eval|bash\s+-c|sh\s+-c)\b")
DIRECT_TARGET = re.compile(r"(?:^|[;&|]\s*)(?:exec|source|\.|bash|sh|python3?|node|npx|tsx)\s+([\"']?[^\s\"']+[\"']?)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (FileNotFoundError, RuntimeError, ValueError):
        return False


def load_manifest(path: Path) -> tuple[dict, dict[str, dict]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return manifest, {item["path"]: item for item in manifest.get("files", [])}


def source_lines(path: Path) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    pending = ""
    start = 1
    for number, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if not pending:
            start = number
        pending += raw.rstrip("\\") + (" " if raw.endswith("\\") else "")
        if raw.endswith("\\"):
            continue
        try:
            tokens = shlex.split(pending, comments=True, posix=True)
            clean = " ".join(tokens)
        except ValueError:
            clean = pending.strip()
        if clean:
            result.append((start, clean))
        pending = ""
    if pending:
        result.append((start, pending))
    return result


def discover(root: Path, entrypoints: list[str], overrides: set[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Discover local code edges from source; policy is only the scan root list."""
    graph: dict[str, list[str]] = {}
    failures: list[str] = []
    queue = list(entrypoints)
    seen: set[str] = set()
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        path = root / name
        if not path.is_file():
            failures.append(f"MISSING_ENTRY:{name}")
            continue
        edges: set[str] = set()
        if path.suffix == ".py":
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                failures.append(f"UNPARSEABLE_SOURCE:{name}:{exc.lineno}")
                continue
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module.split(".")[0]]
                for module in modules:
                    candidate = f"{module}.py"
                    if (root / candidate).is_file():
                        edges.add(candidate)
        for number, line in source_lines(path):
            if path.suffix == ".sh" and SHELL_STRING_EXEC.search(line):
                failures.append(f"UNRESOLVED_DYNAMIC_EXEC:{name}:{number}:shell-string")
            for variable in DYNAMIC_EXEC.findall(line) if path.suffix == ".sh" else ():
                if variable not in overrides and variable not in {"SCRIPT_DIR", "API_DIR", "NOTIFY", "NOTIFY_SCRIPT", "DAILY_SYNC", "DISPLAY_TIME_REBUILDER", "ACCOUNT_SYNC_ENV"}:
                    failures.append(f"UNRESOLVED_DYNAMIC_EXEC:{name}:{number}:{variable}")
            if path.suffix == ".sh":
                for raw_target in DIRECT_TARGET.findall(line):
                    target = raw_target.strip("\"'")
                    if re.fullmatch(r"\d*[<>].*", target):
                        continue
                    target = target.replace("${SCRIPT_DIR}", str(root)).replace("$SCRIPT_DIR", str(root))
                    if "$" in target or "/" not in target:
                        continue
                    candidate = Path(target)
                    if not candidate.is_absolute():
                        candidate = path.parent / candidate
                    try:
                        physical = candidate.resolve(strict=True)
                    except (FileNotFoundError, RuntimeError):
                        failures.append(f"MISSING_EXECUTABLE:{name}:{number}:{candidate}")
                        continue
                    if physical.suffix in CODE_SUFFIXES:
                        if within(physical, root):
                            edges.add(str(physical.relative_to(root.resolve())))
                        else:
                            failures.append(f"SYMLINK_OR_PATH_ESCAPE:{name}:{number}:{physical}")
            for token in LOCAL_TOKEN.findall(line):
                if (root / token).is_file() and token != name:
                    edges.add(token)
            for absolute in re.findall(r"/(?:home|tmp|var)/[^\s;|&\"']+", line):
                candidate = Path(absolute.replace("${SCRIPT_DIR}", str(root)).replace("$SCRIPT_DIR", str(root)))
                if candidate.suffix in CODE_SUFFIXES and candidate.exists() and within(candidate, root):
                    edges.add(str(candidate.resolve().relative_to(root.resolve())))
        graph[name] = sorted(edges)
        queue.extend(edge for edge in sorted(edges) if edge not in seen)
    return graph, failures


def validate_release_file(root: Path, path: Path, manifest: dict[str, dict], label: str) -> list[str]:
    failures: list[str] = []
    try:
        physical = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        return [f"MISSING_EXECUTABLE:{label}:{path}"]
    if not within(physical, root):
        return [f"SYMLINK_OR_PATH_ESCAPE:{label}:{physical}"]
    relative = str(physical.relative_to(root.resolve()))
    item = manifest.get(relative)
    if not item:
        return [f"NOT_IN_RELEASE_MANIFEST:{label}:{relative}"]
    if sha256(physical) != item.get("sha256"):
        failures.append(f"CHECKSUM_MISMATCH:{label}:{relative}")
    return failures


def validate_backend(policy: dict) -> list[str]:
    failures: list[str] = []
    for dependency in policy["externalCodeDependencies"]:
        entry = Path(dependency["entryRoot"])
        try:
            api_root = entry.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            failures.append(f"EXTERNAL_UNRESOLVED:{dependency['name']}:{entry}")
            continue
        release_root = api_root.parent
        allowed_parent = Path(dependency["releaseRootPattern"]).resolve(strict=True)
        if not within(release_root, allowed_parent) or release_root.parent != allowed_parent:
            failures.append(f"EXTERNAL_PATH_BOUNDARY:{dependency['name']}:{release_root}")
            continue
        metadata_path = release_root / dependency["metadata"]
        manifest_path = release_root / dependency["manifest"]
        if not metadata_path.is_file() or not manifest_path.is_file():
            failures.append(f"EXTERNAL_METADATA_MISSING:{dependency['name']}")
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected_hash = metadata.get("taskManifestHash")
        if expected_hash != sha256(manifest_path):
            failures.append(f"EXTERNAL_MANIFEST_HASH_MISMATCH:{dependency['name']}")
            continue
        recorded: dict[str, str] = {}
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            checksum, relative = line.split(maxsplit=1)
            recorded[relative.lstrip("* ")] = checksum
        for relative in dependency["files"]:
            target = release_root / relative
            if relative not in recorded:
                failures.append(f"EXTERNAL_NOT_IN_MANIFEST:{dependency['name']}:{relative}")
            elif not target.is_file() or sha256(target) != recorded[relative]:
                failures.append(f"EXTERNAL_CHECKSUM_MISMATCH:{dependency['name']}:{relative}")
    return failures


def static_check(root: Path, policy: dict, manifest_path: Path | None) -> tuple[dict[str, list[str]], list[str]]:
    graph, failures = discover(root, policy["entrypoints"], set(policy["environmentExecutableOverrides"]))
    closure = set(graph)
    for edges in graph.values():
        closure.update(edges)
    manifest: dict[str, dict] = {}
    if manifest_path:
        metadata, manifest = load_manifest(manifest_path)
        if metadata.get("commit") and len(metadata["commit"]) != 40:
            failures.append("INVALID_MANIFEST_COMMIT")
        for name in sorted(closure):
            failures.extend(validate_release_file(root, root / name, manifest, name))
    for name in policy["executableReleaseFiles"]:
        path = root / name
        if not path.is_file():
            failures.append(f"MISSING_EXECUTABLE:{name}")
        elif not os.access(path, os.X_OK):
            failures.append(f"EXECUTABLE_MODE_MISSING:{name}")
        elif manifest_path:
            item = manifest.get(name)
            if item and not (int(item["mode"], 8) & 0o111):
                failures.append(f"MANIFEST_EXECUTABLE_MODE_MISSING:{name}")
    for name in sorted(closure):
        for number, line in source_lines(root / name):
            for forbidden in policy["forbiddenCodeRoots"]:
                if forbidden in line and re.search(r"\b(?:source|exec|bash|sh|python3?|node|npx|tsx|cd)\b", line):
                    failures.append(f"FORBIDDEN_CODE_ROOT:{name}:{number}:{forbidden}")
            for mutable in policy["mutableDataRoots"]:
                executable_pattern = rf"(?:^|[;&|]\s*)(?:source|exec|bash|sh|python3?|node|npx|tsx)\s+[\"']?{re.escape(mutable)}(?:/|$)"
                if re.search(executable_pattern, line):
                    failures.append(f"MUTABLE_EXECUTABLE:{name}:{number}:{mutable}")
    return graph, failures


def runtime_preflight(root: Path, policy: dict, entry: str, manifest_path: Path) -> list[str]:
    graph, failures = static_check(root, policy, manifest_path)
    if entry not in graph:
        failures.append(f"UNKNOWN_PREFLIGHT_ENTRY:{entry}")
    _, manifest = load_manifest(manifest_path)
    for variable, rule in policy["environmentExecutableOverrides"].items():
        value = os.environ.get(variable)
        if not value:
            continue
        if rule.get("singleExecutable"):
            try:
                tokens = shlex.split(value)
            except ValueError:
                tokens = []
            if len(tokens) != 1:
                failures.append(f"ENV_COMMAND_STRING_FORBIDDEN:{variable}")
                continue
        failures.extend(validate_release_file(root, Path(value), manifest, variable))
    failures.extend(validate_backend(policy))
    requirements = policy.get("runtimeRequirements", {}).get(entry)
    if requirements is None:
        failures.append(f"RUNTIME_REQUIREMENTS_UNDECLARED:{entry}")
        requirements = {}
    for package in requirements.get("nodePackages", []):
        result = subprocess.run(
            ["node", "-e", "console.log(require.resolve(process.argv[1]))", package],
            cwd=root, text=True, capture_output=True, check=False,
        )
        if result.returncode != 0:
            failures.append(f"NODE_PACKAGE_UNRESOLVED:{package}")
        else:
            resolved = Path(result.stdout.strip())
            if not within(resolved, root):
                failures.append(f"NODE_PACKAGE_OUTSIDE_RELEASE:{package}:{resolved}")
                continue
            package_root = resolved
            while package_root != root and not (package_root / "package.json").is_file():
                package_root = package_root.parent
            if package_root == root:
                failures.append(f"NODE_PACKAGE_ROOT_UNRESOLVED:{package}:{resolved}")
                continue
            for package_file in package_root.rglob("*"):
                if package_file.is_file():
                    failures.extend(validate_release_file(root, package_file, manifest, f"node:{package}"))
    if requirements.get("playwrightBrowser"):
        browser = subprocess.run(
            ["node", "-e", "console.log(require('playwright').chromium.executablePath())"],
            cwd=root, text=True, capture_output=True, check=False,
        )
        if browser.returncode != 0 or not browser.stdout.strip():
            failures.append("PLAYWRIGHT_BROWSER_UNRESOLVED")
        else:
            browser_path = Path(browser.stdout.strip())
            if not within(browser_path, root):
                failures.append(f"PLAYWRIGHT_BROWSER_OUTSIDE_RELEASE:{browser_path}")
            else:
                browser_root = browser_path
                while browser_root.parent != root and browser_root.parent.name != ".local-browsers":
                    browser_root = browser_root.parent
                for browser_file in browser_root.rglob("*"):
                    if browser_file.is_file():
                        failures.extend(validate_release_file(root, browser_file, manifest, "playwright-browser"))
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--preflight-entry")
    parser.add_argument("--graph-output", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    policy = json.loads((root / "runtime-closure-policy.json").read_text(encoding="utf-8"))
    manifest_path = args.manifest
    if args.preflight_entry and not manifest_path:
        manifest_path = root / "release-manifest.json"
    graph, failures = static_check(root, policy, manifest_path)
    if args.preflight_entry and manifest_path:
        failures = runtime_preflight(root, policy, args.preflight_entry, manifest_path)
    if args.graph_output:
        args.graph_output.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if failures:
        print("runtime closure: FAILED", file=sys.stderr)
        for failure in sorted(set(failures)):
            print(failure, file=sys.stderr)
        return 1
    edge_count = sum(map(len, graph.values()))
    print(f"runtime closure: PASSED entries={len(policy['entrypoints'])} files={len(graph)} edges={edge_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
