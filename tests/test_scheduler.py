from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheduler import ApprovalScheduler, next_run_at, scheduled_run_options, scheduler_config


class FakeJobManager:
    def __init__(self) -> None:
        self.running = False
        self.started = []
        self.callbacks = []

    def status(self) -> dict:
        return {"running": self.running}

    def start(self, action: str, options: dict[str, str]) -> dict:
        self.started.append((action, options))
        return {"started": True, "message": "started"}

    def add_completion_callback(self, callback) -> None:
        self.callbacks.append(callback)


class SchedulerTest(unittest.TestCase):
    def test_interval_next_run_uses_interval_hours(self) -> None:
        config = scheduler_config({"scheduler": {"enabled": True, "mode": "interval", "interval_hours": 6}})

        result = next_run_at(config, datetime(2026, 7, 13, 8, 30))

        self.assertEqual(result, datetime(2026, 7, 13, 14, 30))

    def test_daily_next_run_rolls_to_tomorrow_after_time_passes(self) -> None:
        config = scheduler_config({"scheduler": {"enabled": True, "mode": "daily", "daily_time": "16:00"}})

        result = next_run_at(config, datetime(2026, 7, 13, 16, 1))

        self.assertEqual(result, datetime(2026, 7, 14, 16, 0))

    def test_scheduled_run_options_process_latest_all_todos(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            options = scheduled_run_options(
                scheduler_config(
                    {
                        "scheduler": {
                            "use_default_run_policy": False,
                            "process_all_todos_max": 20,
                            "approval_write_mode": "multi_page",
                            "auto_pass": False,
                            "skip_manual_review_lists": True,
                        }
                    }
                )
            )

        self.assertEqual(options["PROCESS_ALL_TODOS"], "true")
        self.assertEqual(options["TARGET_LIST_NUMBERS"], "")
        self.assertEqual(options["PROCESS_ALL_TODOS_MAX"], "20")
        self.assertEqual(options["SCHEDULED_RUN"], "true")
        self.assertEqual(options["SCHEDULED_SKIP_MANUAL_REVIEW_LISTS"], "true")

    def test_default_run_policy_uses_manual_approval_settings(self) -> None:
        with patch.dict("os.environ", {"PROCESS_ALL_TODOS_MAX": "12", "AUTO_PASS": "true"}, clear=True):
            config = scheduler_config(
                {
                    "approval": {"write_mode": "single_page", "write_min_confidence": 0.9},
                    "scheduler": {"use_default_run_policy": True, "approval_write_mode": "multi_page"},
                }
            )

        self.assertEqual(config["process_all_todos_max"], 12)
        self.assertEqual(config["approval_write_mode"], "single_page")
        self.assertEqual(config["approval_write_min_confidence"], "0.9")
        self.assertTrue(config["auto_pass"])

    def test_trigger_due_run_skips_when_job_is_running(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeJobManager()
            manager.running = True
            scheduler = ApprovalScheduler(
                root_dir=Path(tmp),
                settings_loader=lambda: {"scheduler": {"enabled": True}},
                job_manager=manager,
            )

            result = scheduler.trigger_due_run()

            self.assertFalse(result["started"])
            self.assertEqual(manager.started, [])
            self.assertEqual(scheduler.status()["last_result"], "skipped")

    def test_trigger_due_run_starts_suggestions(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeJobManager()
            scheduler = ApprovalScheduler(
                root_dir=Path(tmp),
                settings_loader=lambda: {
                    "scheduler": {
                        "enabled": True,
                        "use_default_run_policy": False,
                        "process_all_todos_max": 7,
                    }
                },
                job_manager=manager,
            )

            result = scheduler.trigger_due_run()

            self.assertTrue(result["started"])
            self.assertEqual(manager.started[0][0], "suggestions")
            self.assertEqual(manager.started[0][1]["PROCESS_ALL_TODOS_MAX"], "7")

    def test_scheduled_job_completion_updates_final_result(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeJobManager()
            scheduler = ApprovalScheduler(
                root_dir=Path(tmp),
                settings_loader=lambda: {
                    "scheduler": {
                        "enabled": True,
                        "use_default_run_policy": False,
                    }
                },
                job_manager=manager,
            )

            scheduler.trigger_due_run()
            manager.callbacks[0](
                action="suggestions",
                success=False,
                error="ERP timeout",
                options={"SCHEDULED_RUN": "true"},
            )

            status = scheduler.status()
            self.assertEqual(status["last_result"], "failed")
            self.assertEqual(status["last_message"], "ERP timeout")

    def test_manual_job_completion_does_not_update_scheduler_result(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeJobManager()
            scheduler = ApprovalScheduler(
                root_dir=Path(tmp),
                settings_loader=lambda: {
                    "scheduler": {
                        "enabled": True,
                        "use_default_run_policy": False,
                    }
                },
                job_manager=manager,
            )

            scheduler.trigger_due_run()
            manager.callbacks[0](
                action="suggestions",
                success=True,
                error="",
                options={},
            )

            self.assertEqual(scheduler.status()["last_result"], "started")


if __name__ == "__main__":
    unittest.main()
