from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def asset(path: Path, kind: str, revision: str = "") -> dict[str, object]:
    result: dict[str, object] = {
        "name": path.name,
        "kind": kind,
        "arch": "amd64",
        "size": path.stat().st_size,
        "sha256": digest(path),
    }
    if revision:
        result["browser_revision"] = revision
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--browser-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = {
        "release_version": args.version,
        "repository": args.repository,
        "channel": "stable",
        "assets": [asset(args.core, "core"), asset(args.browser, "browser", args.browser_revision)],
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
