from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from browser_bot import BrowserBot
from runtime_paths import ensure_runtime_layout, runtime_root
from task_control import checkpoint, SafeStop, TaskControl, SAFE_STOP_EXIT_CODE


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
    if action == "workbuddy_loop" and os.getenv("WORKBUDDY_BATCH_SIZE"):
        settings.setdefault("workbuddy", {}).update(
            batch_size=int(os.environ["WORKBUDDY_BATCH_SIZE"]),
            selected_result_timeout_seconds=int(os.environ["WORKBUDDY_RESULT_TIMEOUT_SECONDS"]),
        )
    bot = BrowserBot(settings=settings, root_dir=ROOT_DIR)
    bot.target_list_number = os.getenv("TARGET_LIST_NUMBER", "").strip()
    bot.target_list_numbers = parse_target_list_numbers(os.getenv("TARGET_LIST_NUMBERS", ""))
    checkpoint("准备登录")

    if action == "debug_capture":
        bot.run_debug_capture()
    elif action == "judgement_capture":
        bot.run_reagent_judgement_capture()
    elif action == "erp_smoke":
        bot.run_todo_tasks_export()
    elif action == "api_discovery":
        bot.run_erp_api_discovery()
    elif action == "api_canary":
        bot.run_erp_api_canary()
    elif action == "todo_export":
        bot.run_todo_tasks_export()
    elif action == "suggestions":
        bot.run_semi_auto_approval_suggestions()
    elif action in {"workbuddy_prepare", "workbuddy_import"}:
        bot.run_workbuddy_handoff(import_results=action == "workbuddy_import")
    elif action == "workbuddy_loop":
        bot.run_workbuddy_loop()
    else:
        raise ValueError(f"Unknown automation action: {action}")


def main() -> None:
    configure_console_output()
    parser = argparse.ArgumentParser(description="Run one ERP approval automation action.")
    parser.add_argument(
        "action",
        choices=[
            "suggestions", "todo_export", "debug_capture", "judgement_capture",
            "erp_smoke", "api_discovery", "api_canary",
            "workbuddy_prepare", "workbuddy_import",
            "workbuddy_loop",
        ],
    )
    args = parser.parse_args()
    try:
        if args.action == "workbuddy_loop" and (task_id := os.getenv("AUTOMATION_TASK_ID")):
            TaskControl(ROOT_DIR).record_details(task_id, worker_pid=os.getpid(), worker_launching=False)
        run_action(args.action)
        if os.getenv("AUTOMATION_TASK_ID", ""):
            TaskControl(ROOT_DIR).finish(os.environ["AUTOMATION_TASK_ID"], "completed")
    except SafeStop:
        task_id = os.getenv("AUTOMATION_TASK_ID", "")
        if not task_id:
            raise
        TaskControl(ROOT_DIR).finish(task_id, "stopped")
        print("[safe_stop] 已人工停止；已保存结果不会撤销。", flush=True)
        raise SystemExit(SAFE_STOP_EXIT_CODE)


if __name__ == "__main__":
    main()
