from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from browser_bot import BrowserBot
from runtime_paths import ensure_runtime_layout, runtime_root


ensure_runtime_layout()
ROOT_DIR = runtime_root()
CONFIG_PATH = ROOT_DIR / "config" / "settings.yaml"


def configure_console_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_settings() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def parse_target_list_numbers(value: str) -> list[str]:
    numbers = []
    for part in str(value or "").replace("\n", ",").replace(";", ",").split(","):
        item = part.strip()
        if item and item not in numbers:
            numbers.append(item)
    return numbers


def run_action(action: str) -> None:
    load_dotenv(ROOT_DIR / ".env")
    settings = load_settings()
    bot = BrowserBot(settings=settings, root_dir=ROOT_DIR)
    bot.target_list_number = os.getenv("TARGET_LIST_NUMBER", "").strip()
    bot.target_list_numbers = parse_target_list_numbers(os.getenv("TARGET_LIST_NUMBERS", ""))

    if action == "debug_capture":
        bot.run_debug_capture()
    elif action == "judgement_capture":
        bot.run_reagent_judgement_capture()
    elif action == "todo_export":
        bot.run_todo_tasks_export()
    elif action == "suggestions":
        bot.run_semi_auto_approval_suggestions()
    else:
        raise ValueError(f"Unknown automation action: {action}")


def main() -> None:
    configure_console_output()
    parser = argparse.ArgumentParser(description="Run one ERP approval automation action.")
    parser.add_argument("action", choices=["suggestions", "todo_export", "debug_capture", "judgement_capture"])
    args = parser.parse_args()
    run_action(args.action)


if __name__ == "__main__":
    main()
