from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dingtalk_stream_bot import (  # noqa: E402
    COMMAND_HELP,
    COMMAND_RUN,
    COMMAND_STATUS,
    COMMAND_UNKNOWN,
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
        self.assertEqual(command.pipeline, "workbuddy")

    def test_pipeline_commands_and_strict_arguments(self) -> None:
        for text, pipeline, number in (
            ("执行审批", "workbuddy", ""),
            ("执行审批V1", "v1", ""),
            ("执行审批v1 SJ123", "v1", "SJ123"),
            ("执行审批 WorkBuddy SJ456", "workbuddy", "SJ456"),
        ):
            command = parse_stream_command(text)
            self.assertEqual((command.kind, command.pipeline, command.list_number), (COMMAND_RUN, pipeline, number))
        for text in ("执行审批 不存在", "执行审批 SJ123 多余参数", "执行审批V2", "执行审批 SJ123 SJ456"):
            self.assertEqual(parse_stream_command(text).kind, COMMAND_UNKNOWN)

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
            run_options_builder=lambda list_number, pipeline: {
                "TARGET_LIST_NUMBERS": list_number,
                "PROCESS_ALL_TODOS": "false" if list_number else "true",
            },
        )

        response = service.handle_text("@审批机器人 执行审批 SJ202607300001")

        self.assertIn("已启动审批任务", response)
        self.assertEqual(manager.started[0][0], "workbuddy_loop")
        self.assertEqual(manager.started[0][1]["TARGET_LIST_NUMBERS"], "SJ202607300001")

    def test_running_task_blocks_new_approval(self) -> None:
        manager = FakeJobManager()
        service = DingTalkCommandService(
            job_manager=manager,
            status_provider=lambda: {"running": True, "action": "suggestions"},
            run_options_builder=lambda list_number, pipeline: {},
        )

        response = service.handle_text("执行审批")

        self.assertIn("已有审批任务", response)
        self.assertEqual(manager.started, [])

    def test_status_includes_current_approval_list_number_when_running(self) -> None:
        service = DingTalkCommandService(
            job_manager=FakeJobManager(),
            run_options_builder=lambda list_number, pipeline: {},
            status_provider=lambda: {
                "running": True,
                "action": "suggestions",
                "started_at": "2026-09-09T10:00:00",
                "finished_at": "",
                "current_list_number": "SJ202609090001",
            },
        )

        response = service.handle_text("状态")

        self.assertIn("正在审批清单号：SJ202609090001", response)

    def test_v1_command_routes_to_existing_pipeline(self) -> None:
        manager = FakeJobManager()
        service = DingTalkCommandService(job_manager=manager, status_provider=lambda: {},
            run_options_builder=lambda number, pipeline: {"PIPELINE": pipeline})
        service.handle_text("执行审批V1 SJ123")
        self.assertEqual(manager.started, [("suggestions", {"PIPELINE": "v1"})])

    def test_preflight_error_does_not_start_task(self) -> None:
        manager = FakeJobManager()
        def invalid(number, pipeline):
            raise ValueError("客户端未启用")
        service = DingTalkCommandService(job_manager=manager, status_provider=lambda: {}, run_options_builder=invalid)
        self.assertIn("客户端未启用", service.handle_text("执行审批"))
        self.assertEqual(manager.started, [])

    def test_workbuddy_status_includes_batch_scope_and_stage(self) -> None:
        service = DingTalkCommandService(job_manager=FakeJobManager(), run_options_builder=lambda number, pipeline: {},
            status_provider=lambda: {"running": True, "action": "workbuddy_loop", "current_list_number": "SJ123, SJ456",
                "log_tail": ["[workbuddy progress] 批次 abc：等待 WorkBuddy 结果"]})
        response = service.handle_text("状态")
        self.assertIn("审批链路：WorkBuddy", response)
        self.assertIn("当前批次涉及清单：SJ123, SJ456", response)
        self.assertIn("等待 WorkBuddy 结果", response)


if __name__ == "__main__":
    unittest.main()
