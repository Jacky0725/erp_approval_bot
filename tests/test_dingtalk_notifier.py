from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dingtalk_notifier import (  # noqa: E402
    build_task_result_message,
    dingtalk_notification_config,
    send_task_result_notification,
)


class DingTalkNotifierTests(unittest.TestCase):
    def test_config_defaults_to_disabled_with_at_all(self) -> None:
        config = dingtalk_notification_config({})

        self.assertFalse(config["enabled"])
        self.assertEqual(config["webhook_env"], "DINGTALK_ROBOT_WEBHOOK")
        self.assertEqual(config["secret_env"], "DINGTALK_ROBOT_SECRET")
        self.assertTrue(config["at_all"])

    def test_build_task_result_message_is_concise_summary(self) -> None:
        message = build_task_result_message(
            action="suggestions",
            success=False,
            started_at="2026-07-13T10:00:00",
            finished_at="2026-07-13T10:05:00",
            error="ERP timeout",
            approval={"exists": True, "rows": 12, "manual_review": 3},
            review_queue={"exists": True, "pending": 2},
        )

        self.assertIn("试剂审批任务失败", message.title)
        self.assertIn("任务：生成审批建议", message.markdown)
        self.assertIn("审批建议：12 条", message.markdown)
        self.assertIn("复核队列待处理：2 条", message.markdown)
        self.assertIn("ERP timeout", message.markdown)

    def test_send_notification_disabled_is_skipped(self) -> None:
        result = send_task_result_notification(
            {"notification": {"dingtalk": {"enabled": False}}},
            build_task_result_message(action="suggestions", success=True),
        )

        self.assertFalse(result["sent"])
        self.assertTrue(result["skipped"])

    def test_send_notification_without_webhook_returns_error(self) -> None:
        previous = os.environ.pop("DINGTALK_ROBOT_WEBHOOK", None)
        try:
            result = send_task_result_notification(
                {"notification": {"dingtalk": {"enabled": True}}},
                build_task_result_message(action="suggestions", success=False),
            )
        finally:
            if previous is not None:
                os.environ["DINGTALK_ROBOT_WEBHOOK"] = previous

        self.assertFalse(result["sent"])
        self.assertFalse(result["skipped"])
        self.assertIn("webhook", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
