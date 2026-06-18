from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from browser_bot import BrowserBot


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config" / "settings.yaml"


def load_settings() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def main() -> None:
    configure_console_output()
    load_dotenv(ROOT_DIR / ".env")
    settings = load_settings()

    browser_bot = BrowserBot(settings=settings, root_dir=ROOT_DIR)
    browser_bot.target_list_number = os.getenv("TARGET_LIST_NUMBER", "").strip()
    browser_bot.run_semi_auto_approval_suggestions()


def configure_console_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    main()
