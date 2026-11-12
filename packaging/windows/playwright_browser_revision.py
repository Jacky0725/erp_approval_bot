"""Report the Chromium revisions required by the installed Playwright package."""

from __future__ import annotations

import json
from pathlib import Path

import playwright


def browser_revisions() -> dict[str, str]:
    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    browsers = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
    revisions = {item["name"]: str(item["revision"]) for item in browsers}
    return {"chromium": revisions["chromium"], "headless": revisions["chromium-headless-shell"]}


if __name__ == "__main__":
    print(json.dumps(browser_revisions()))
