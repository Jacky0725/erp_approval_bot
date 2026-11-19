from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheduler import ApprovalScheduler, atomic_write_text, next_run_at, normalize_web_write_mode, scheduled_run_options, scheduler_config, workbuddy_preflight


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

    def test_existing_schedule_without_pipeline_defaults_to_workbuddy(self) -> None:
        config = scheduler_config({"scheduler": {"enabled": True}})
        self.assertEqual(config["pipeline"], "workbuddy")
        self.assertFalse(config["auto_pass"])

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
        self.assertEqual(config["approval_write_mode"], "disabled")
        self.assertEqual(config["approval_write_min_confidence"], "0.9")
        self.assertFalse(config["auto_pass"])

    def test_scheduler_write_mode_fails_closed_for_retired_and_unknown_values(self) -> None:
        self.assertEqual(normalize_web_write_mode("save_one"), "disabled")
        self.assertEqual(normalize_web_write_mode("single_page"), "disabled")
        self.assertEqual(normalize_web_write_mode("unknown"), "disabled")
        self.assertEqual(normalize_web_write_mode("multi_page"), "multi_page")

    def test_workbuddy_dry_run_does_not_require_api_write(self) -> None:
        settings = {"app": {"dry_run": True}, "workbuddy": {"desktop_enabled": True, "executable": "client.exe"}}
        options = {"APPROVAL_WRITE_MODE": "multi_page", "ERP_WRITE_BACKEND": "web_ui"}
        with patch("workbuddy_discovery.client_path", return_value=Path("client.exe")), patch("scheduler.interactive_desktop_available", return_value=True):
            self.assertEqual(workbuddy_preflight(settings, options), "")

    def test_workbuddy_formal_write_requires_verified_api(self) -> None:
        settings = {"app": {"dry_run": False}, "workbuddy": {"desktop_enabled": True,
            "executable": "client.exe", "write_enabled": True}, "erp_api": {"enabled_for_write": False}}
        options = {"APPROVAL_WRITE_MODE": "multi_page", "ERP_WRITE_BACKEND": "api_write_with_web_verify"}
        with patch("workbuddy_discovery.client_path", return_value=Path("client.exe")), patch("scheduler.interactive_desktop_available", return_value=True):
            self.assertIn("未验证", workbuddy_preflight(settings, options))

    def test_scheduler_atomic_write_text_replaces_existing_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "scheduler_state.yaml"
            path.write_text("last_result: old\n", encoding="utf-8")

            atomic_write_text(path, "last_result: new\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "last_result: new\n")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

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

    def test_trigger_due_run_starts_workbuddy_when_ready(self) -> None:
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

            with patch("scheduler.workbuddy_preflight", return_value=""):
                result = scheduler.trigger_due_run()

            self.assertTrue(result["started"])
            self.assertEqual(manager.started[0][0], "workbuddy_loop")
            self.assertEqual(manager.started[0][1]["PROCESS_ALL_TODOS_MAX"], "7")
            self.assertEqual(manager.started[0][1]["AUTO_PASS"], "false")

    def test_workbuddy_preflight_failure_falls_back_only_before_start(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeJobManager()
            scheduler = ApprovalScheduler(Path(tmp), lambda: {"scheduler": {"enabled": True,
                "use_default_run_policy": False, "approval_write_mode": "multi_page"}}, manager)
            with patch("scheduler.workbuddy_preflight", return_value="no desktop"), patch("scheduler.v1_preflight", return_value=""):
                result = scheduler.trigger_due_run()
            self.assertTrue(result["started"])
            self.assertEqual([action for action, _ in manager.started], ["suggestions"])
            self.assertEqual(manager.started[0][1]["SCHEDULED_FALLBACK_REASON"], "no desktop")
            self.assertEqual(manager.started[0][1]["APPROVAL_WRITE_MODE"], "multi_page")
            self.assertEqual(manager.started[0][1]["AUTO_PASS"], "false")

    def test_fallback_skips_when_v1_cannot_start(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeJobManager()
            scheduler = ApprovalScheduler(Path(tmp), lambda: {"scheduler": {"enabled": True}}, manager)
            with patch("scheduler.workbuddy_preflight", return_value="no desktop"), patch("scheduler.v1_preflight", return_value="no ERP config"):
                result = scheduler.trigger_due_run()
            self.assertFalse(result["started"])
            self.assertEqual(manager.started, [])

    def test_explicit_v1_does_not_probe_workbuddy(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeJobManager()
            scheduler = ApprovalScheduler(Path(tmp), lambda: {"scheduler": {"enabled": True, "pipeline": "v1"}}, manager)
            with patch("scheduler.workbuddy_preflight") as preflight:
                scheduler.trigger_due_run()
            preflight.assert_not_called()
            self.assertEqual(manager.started[0][0], "suggestions")

    def test_workbuddy_start_failure_never_retries_as_v1(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeJobManager()
            manager.start = lambda action, options: (manager.started.append((action, options)) or
                {"started": False, "message": "result uncertain"})
            scheduler = ApprovalScheduler(Path(tmp), lambda: {"scheduler": {"enabled": True}}, manager)
            with patch("scheduler.workbuddy_preflight", return_value=""):
                result = scheduler.trigger_due_run()
            self.assertFalse(result["started"])
            self.assertEqual([action for action, _ in manager.started], ["workbuddy_loop"])

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

            with patch("scheduler.workbuddy_preflight", return_value=""):
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

            with patch("scheduler.workbuddy_preflight", return_value=""):
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
