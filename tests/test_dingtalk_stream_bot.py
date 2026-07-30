from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dingtalk_stream_bot import (  # noqa: E402
    COMMAND_HELP,
    COMMAND_RUN,
    COMMAND_STATUS,
    DingTalkCommandService,
    dingtalk_stream_config,
    extract_message_text,
    parse_stream_command,
    should_process_message,
    stream_binding_from_message,
)


class FakeJobManager:
    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, str]]] = []

    def start(self, action: str, options: dict[str, str]) -> dict[str, object]:
        self.started.append((action, options))
        return {"started": True, "message": "ok"}


class DingTalkStreamBotTests(unittest.TestCase):
    def test_parse_supported_commands(self) -> None:
        self.assertEqual(parse_stream_command("@审批机器人 帮助").kind, COMMAND_HELP)
        self.assertEqual(parse_stream_command("状态").kind, COMMAND_STATUS)

        command = parse_stream_command("@审批机器人 执行审批 SJ202607300001")

        self.assertEqual(command.kind, COMMAND_RUN)
        self.assertEqual(command.list_number, "SJ202607300001")

    def test_extract_message_text_from_callback_data(self) -> None:
        self.assertEqual(
            extract_message_text({"text": {"content": "@机器人 执行审批"}}),
            "@机器人 执行审批",
        )

    def test_group_messages_require_at_robot(self) -> None:
        config = dingtalk_stream_config({"dingtalk_stream": {"enabled": True}})

        self.assertTrue(
            should_process_message({"conversationType": "2", "isInAtList": True}, config)
        )
        self.assertFalse(
            should_process_message({"conversationType": "2", "isInAtList": False}, config)
        )
        self.assertTrue(
            should_process_message({"conversationType": "1", "isInAtList": False}, config)
        )

    def test_stream_binding_is_extracted_from_group_message(self) -> None:
        binding = stream_binding_from_message(
            {
                "conversationId": "cid123",
                "openConversationId": "open-cid123",
                "sessionWebhook": "https://example.test/session",
                "robotCode": "dingbot",
                "senderStaffId": "user1",
                "conversationType": "2",
            }
        )

        self.assertEqual(binding["openConversationId"], "open-cid123")
        self.assertEqual(binding["sessionWebhook"], "https://example.test/session")
        self.assertEqual(binding["robotCode"], "dingbot")

    def test_any_group_member_can_start_approval_with_list_number(self) -> None:
        manager = FakeJobManager()
        service = DingTalkCommandService(
            job_manager=manager,
            status_provider=lambda: {"running": False, "result_label": "空闲"},
            run_options_builder=lambda list_number: {
                "TARGET_LIST_NUMBERS": list_number,
                "PROCESS_ALL_TODOS": "false" if list_number else "true",
            },
        )

        response = service.handle_text("@审批机器人 执行审批 SJ202607300001")

        self.assertIn("已启动审批任务", response)
        self.assertEqual(manager.started[0][0], "suggestions")
        self.assertEqual(manager.started[0][1]["TARGET_LIST_NUMBERS"], "SJ202607300001")

    def test_running_task_blocks_new_approval(self) -> None:
        manager = FakeJobManager()
        service = DingTalkCommandService(
            job_manager=manager,
            status_provider=lambda: {"running": True, "action": "suggestions"},
            run_options_builder=lambda list_number: {},
        )

        response = service.handle_text("执行审批")

        self.assertIn("已有审批任务", response)
        self.assertEqual(manager.started, [])


if __name__ == "__main__":
    unittest.main()
