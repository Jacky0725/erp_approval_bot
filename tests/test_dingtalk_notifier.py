from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dingtalk_notifier import (  # noqa: E402
    build_task_result_message,
    dingtalk_notification_config,
    save_stream_binding,
    send_task_result_notification,
)


class DingTalkNotifierTests(unittest.TestCase):
    def test_config_defaults_to_disabled_with_at_all(self) -> None:
        config = dingtalk_notification_config({})

        self.assertFalse(config["enabled"])
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

    def test_stream_notification_without_bound_group_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = send_task_result_notification(
                {
                    "notification": {"dingtalk": {"enabled": True}},
                    "dingtalk_stream": {"enabled": True, "corp_id": "ding123"},
                },
                build_task_result_message(action="suggestions", success=True),
                root_dir=Path(tmp),
            )

        self.assertFalse(result["sent"])
        self.assertIn("openconversationid", result["error"].lower())

    def test_save_stream_binding_persists_latest_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            saved = save_stream_binding(
                {
                    "openConversationId": "cid123",
                    "sessionWebhook": "https://example.test/session",
                    "robotCode": "dingbot",
                },
                Path(tmp),
            )

            self.assertEqual(saved["openConversationId"], "cid123")
            self.assertTrue((Path(tmp) / "data" / "dingtalk_stream_binding.json").exists())

    def test_stream_notification_uses_original_markdown_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_stream_binding(
                {
                    "sessionWebhook": "https://example.test/session",
                    "openConversationId": "cid123",
                    "robotCode": "dingbot",
                },
                Path(tmp),
            )
            message = build_task_result_message(action="suggestions", success=False, error="ERP timeout")
            with patch("dingtalk_notifier.post_json") as post_json:
                post_json.return_value = {"sent": True, "status": 200, "body": "{}"}

                result = send_task_result_notification(
                    {
                        "notification": {"dingtalk": {"enabled": True, "at_all": True}},
                        "dingtalk_stream": {"enabled": True, "corp_id": "ding123"},
                    },
                    message,
                    root_dir=Path(tmp),
                )

            self.assertTrue(result["sent"])
            payload = post_json.call_args.args[1]
            self.assertEqual(payload["markdown"]["title"], message.title)
            self.assertEqual(payload["markdown"]["text"], message.markdown)
            self.assertTrue(payload["at"]["isAtAll"])


if __name__ == "__main__":
    unittest.main()
