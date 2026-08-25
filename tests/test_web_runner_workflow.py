from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web_runner import (
    AutomationJobManager,
    automation_failure_reason,
    atomic_write_text,
    artifact_summary,
    current_run_lines,
    normalize_web_write_mode,
    parse_target_list_numbers,
    repair_display_text,
    light_run_summary,
    run_summary,
    run_health,
    todo_tasks_summary,
    workflow_summary,
)
import web_app
from memory_sync import MemorySyncError
from web_app import artifact_path_for_download, run_options, web_ui_restart_command


class WorkflowSummaryTest(unittest.TestCase):
    def test_repeated_reagent_pipeline_marks_current_stage_active(self) -> None:
        lines = [
            "2026-06-22 12:39:48 [FLOW] START chemical_search - page 1 1/20 A",
            "2026-06-22 12:40:19 [FLOW] END   chemical_search (30.6s)",
            "2026-06-22 12:40:19 [FLOW] START llm_extract - page 1 1/20 A",
            "2026-06-22 12:40:31 [FLOW] END   llm_extract (12.2s)",
            "2026-06-22 12:40:31 [FLOW] START rule_classify - A",
            "2026-06-22 12:40:31 [FLOW] END   rule_classify (0.0s)",
            "2026-06-22 12:40:59 [FLOW] START chemical_search - page 1 2/20 B",
        ]

        result = workflow_summary(lines, running=True, success=None, error="")
        states = {step["id"]: step["state"] for step in result["steps"]}

        self.assertEqual(result["current_step"], "search")
        self.assertEqual(states["search"], "active")
        self.assertEqual(states["llm"], "waiting")
        self.assertEqual(states["rule"], "waiting")
        self.assertEqual(states["write"], "waiting")

    def test_manager_status_reports_finished_result(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = AutomationJobManager(root_dir=Path(tmp))
            manager.action = "suggestions"
            manager.started_at = "2026-06-22T12:00:00"
            manager.finished_at = "2026-06-22T12:02:00"
            manager.success = True
            manager.error = ""

            status = manager.status()

            self.assertEqual(status["result_label"], "审批流程完成")
            self.assertEqual(status["action"], "suggestions")

    def test_todo_export_status_uses_business_label(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = AutomationJobManager(root_dir=Path(tmp))
            manager.action = "todo_export"
            manager.started_at = "2026-08-05T08:38:49"
            manager.finished_at = "2026-08-05T08:39:09"
            manager.success = True
            manager.error = ""

            status = manager.status()

            self.assertEqual(status["action_label"], "待办清单刷新")
            self.assertEqual(status["result_label"], "待办清单刷新成功")
            self.assertEqual(status["summary"]["outcome"], "待办清单刷新成功")

    def test_erp_smoke_action_has_read_only_label(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = AutomationJobManager(root_dir=Path(tmp))
            manager.action = "erp_smoke"
            manager.started_at = "2026-08-25T09:00:00"
            manager.finished_at = "2026-08-25T09:00:20"
            manager.success = True
            manager.error = ""

            status = manager.status()

            self.assertEqual(status["action_label"], "ERP 只读冒烟测试")
            self.assertEqual(status["summary"]["outcome"], "ERP 只读冒烟测试成功")

    def test_run_summary_counts_write_outcome_and_targets(self) -> None:
        with TemporaryDirectory() as tmp:
            lines = [
                "Read reagent page 1: 20 row(s).",
                "Opening target task detail: SJ202608040001",
                'Page suggestion summary: {"total": 3, "writable": 2, "manual_review": 1, "low_confidence": 0, "search_failure": 1, "memory_hit": 1, "llm_knowledge_fallback": 0, "skipped": 1, "skip_reasons": {"manual_review": 1}}',
                "Save verified for sequence 1",
                "Could not select physicochemical property 强反应 for sequence 2",
            ]

            summary = run_summary(
                lines,
                action="suggestions",
                options={"TARGET_LIST_NUMBERS": "SJ202608040001"},
                running=False,
                success=True,
                error="",
                root_dir=Path(tmp),
            )

            self.assertEqual(summary["target_list_numbers"], ["SJ202608040001"])
            self.assertEqual(summary["write_success_count"], 1)
            self.assertEqual(summary["write_failure_count"], 1)
            self.assertEqual(summary["page_suggestion_count"], 3)
            self.assertEqual(summary["writable_candidate_count"], 2)
            self.assertEqual(summary["manual_review_candidate_count"], 1)
            self.assertEqual(summary["search_failure_count"], 1)
            self.assertEqual(summary["memory_hit_count"], 1)
            self.assertTrue(summary["has_write_warning"])

    def test_run_summary_can_count_full_log_beyond_tail(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = ["Save verified for sequence 1: True (clicked_save=True)"]
            lines.extend(f"noise line {index}" for index in range(200))
            lines.append("Could not select physicochemical property 易燃类 for sequence: 14")

            summary = run_summary(
                lines,
                action="suggestions",
                options={"TARGET_LIST_NUMBERS": "SJ202608170010"},
                running=False,
                success=True,
                error="",
                root_dir=root,
            )

            self.assertEqual(summary["write_success_count"], 1)
            self.assertEqual(summary["write_failure_count"], 1)
            self.assertEqual(summary["dropdown_failure_count"], 1)
            self.assertEqual(summary["dropdown_failures"][0]["sequence"], "14")
            self.assertIn("易燃类", summary["dropdown_failures"][0]["category"])

    def test_run_summary_counts_deferred_and_not_found_pending_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            lines = [
                "Page suggestion summary: {\"total\": 20, \"writable\": 20, \"manual_review\": 0}",
                "Deferred pending write candidate until a later page/read: 6|-|reagent-6|",
                "Deferred pending write candidate until a later page/read: 7|-|reagent-7|",
                "Multi-page mode reached the last reagent page with 9 pending write candidate(s) not found after re-read.",
            ]

            summary = run_summary(
                lines,
                action="suggestions",
                options={"TARGET_LIST_NUMBERS": "SJ202608170010"},
                running=False,
                success=True,
                error="",
                root_dir=Path(tmp),
            )

            self.assertEqual(summary["page_suggestion_count"], 20)
            self.assertEqual(summary["deferred_write_count"], 2)
            self.assertEqual(summary["not_found_after_reread_count"], 1)

    def test_light_run_summary_includes_dropdown_failure_details(self) -> None:
        lines = [
            "Save verified for sequence 35: True (clicked_save=True)",
            "Could not select physicochemical property 未知类 for sequence: 36",
        ]

        summary = light_run_summary(
            lines,
            action="suggestions",
            options={"TARGET_LIST_NUMBERS": "SJ202608180001"},
            running=True,
            success=None,
            error="",
        )

        self.assertEqual(summary["write_success_count"], 1)
        self.assertEqual(summary["write_failure_count"], 1)
        self.assertEqual(summary["dropdown_failure_count"], 1)
        self.assertEqual(summary["dropdown_failures"][0]["sequence"], "36")
        self.assertIn("未知类", summary["dropdown_failures"][0]["category"])

    def test_current_run_lines_reads_complete_log_file(self) -> None:
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "run.log"
            log_path.write_text("\n".join(f"line {index}" for index in range(220)), encoding="utf-8")

            lines = current_run_lines(log_path, fallback=["tail"])

            self.assertEqual(len(lines), 220)
            self.assertEqual(lines[0], "line 0")
            self.assertEqual(lines[-1], "line 219")

    def test_api_log_tail_reads_persisted_run_log_when_idle(self) -> None:
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "run.log"
            log_path.write_text("\n".join(f"run line {index}" for index in range(170)), encoding="utf-8")

            with patch(
                "web_app.manager.status",
                return_value={"run_log_path": str(log_path), "log_tail": ["stale memory line"]},
            ):
                response = web_app.api_log_tail()

            payload = response.body.decode("utf-8")

        self.assertIn("run line 10", payload)
        self.assertIn("run line 169", payload)
        self.assertNotIn("stale memory line", payload)

    def test_todo_tasks_summary_prefers_utf8_json(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "data" / "logs"
            log_dir.mkdir(parents=True)
            (log_dir / "todo_tasks.json").write_text(
                '[{"试剂清单号":"SJ202608040001","客户名称":"上海交通大学","申请人":"戚明燕"}]',
                encoding="utf-8",
            )

            summary = todo_tasks_summary(root)

            self.assertEqual(summary["rows"], 1)
            self.assertEqual(summary["tasks"][0]["list_number"], "SJ202608040001")
            self.assertEqual(summary["tasks"][0]["customer_name"], "上海交通大学")

    def test_parse_target_list_numbers_deduplicates_values(self) -> None:
        result = parse_target_list_numbers("SJ1, SJ2;SJ1\nSJ3")

        self.assertEqual(result, ["SJ1", "SJ2", "SJ3"])

    def test_repair_display_text_keeps_valid_chinese(self) -> None:
        self.assertEqual(repair_display_text("成功"), "成功")

    def test_repair_display_text_restores_gbk_decoded_utf8(self) -> None:
        mojibake = "成功".encode("utf-8").decode("gbk")

        self.assertEqual(repair_display_text(mojibake), "成功")

    def test_repair_display_text_handles_common_gbk_replacement_mojibake(self) -> None:
        mojibake = "燃料及油品".encode("utf-8").decode("gbk", errors="replace")

        self.assertIn("燃料及油", repair_display_text(mojibake))

    def test_run_health_warns_on_business_failures(self) -> None:
        health = run_health(["Failed save operation(s): reagent_save_1"], True, "")

        self.assertEqual(health, "warning")

    def test_automation_failure_reason_detects_web_write_failure(self) -> None:
        reason = automation_failure_reason(
            [
                "Could not select physicochemical property 强反应 for sequence: 9",
                "2026-07-14T13:11:53 END suggestions",
            ]
        )

        self.assertIn("物化特性", reason)

    def test_stop_reports_not_stopped_when_idle(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = AutomationJobManager(root_dir=Path(tmp))

            result = manager.stop()

            self.assertFalse(result["stopped"])

    def test_worker_process_not_started_after_stop_requested(self) -> None:
        with TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            src_dir.mkdir(parents=True)
            manager = AutomationJobManager(root_dir=Path(tmp), running=True)
            manager._stop_requested = True

            result = manager._run_worker_process("suggestions", {}, writer=None)  # type: ignore[arg-type]

            self.assertEqual(result, 130)
            self.assertIsNone(manager._process)

    def test_source_restart_command_uses_configured_port(self) -> None:
        with patch.dict("os.environ", {"WEB_UI_HOST": "127.0.0.1", "WEB_UI_PORT": "8123"}):
            command, cwd = web_ui_restart_command(frozen=False)

        self.assertIn("uvicorn", command)
        self.assertEqual(command[-1], "8123")
        self.assertTrue(cwd.endswith("src"))

    def test_frozen_restart_command_restarts_current_executable(self) -> None:
        command, cwd = web_ui_restart_command(frozen=True)

        self.assertEqual(command, [sys.executable])
        self.assertFalse(cwd.endswith("src"))

    def test_artifact_download_path_stays_inside_log_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "data" / "logs"
            log_dir.mkdir(parents=True)
            artifact = log_dir / "ok.txt"
            artifact.write_text("ok", encoding="utf-8")
            outside = root / "data" / "logs_evil.txt"
            outside.write_text("no", encoding="utf-8")

            original = web_app.LOG_DIR
            web_app.LOG_DIR = log_dir
            try:
                self.assertEqual(artifact_path_for_download("ok.txt"), artifact.resolve())
                self.assertIsNone(artifact_path_for_download("../logs_evil.txt"))
            finally:
                web_app.LOG_DIR = original

    def test_artifact_summary_excludes_noisy_large_runtime_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "data" / "logs"
            log_dir.mkdir(parents=True)
            (log_dir / "approval_suggestions.xlsx").write_text("ok", encoding="utf-8")
            (log_dir / "web_run_stdout.txt").write_text("large aggregate log", encoding="utf-8")
            (log_dir / "reagent_memory_backup_20260817.sqlite").write_text("backup", encoding="utf-8")

            names = {item["name"] for item in artifact_summary(root)}

        self.assertIn("approval_suggestions.xlsx", names)
        self.assertNotIn("web_run_stdout.txt", names)
        self.assertNotIn("reagent_memory_backup_20260817.sqlite", names)

    def test_write_mode_options_expose_only_production_modes(self) -> None:
        template_text = (Path(__file__).resolve().parents[1] / "src" / "templates" / "partials" / "run.html").read_text(
            encoding="utf-8"
        )
        settings_text = (
            Path(__file__).resolve().parents[1] / "src" / "templates" / "partials" / "settings.html"
        ).read_text(encoding="utf-8")
        combined = template_text + settings_text

        self.assertIn('value="disabled"', combined)
        self.assertIn('value="multi_page"', combined)
        self.assertIn('value="generate_library"', combined)
        for retired in ["test_one", "save_one", "single_page"]:
            self.assertNotIn(f'<option value="{retired}"', combined)

    def test_logs_page_forces_log_refresh_while_running(self) -> None:
        script_text = (Path(__file__).resolve().parents[1] / "src" / "static" / "dashboard.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("refreshLogTail({force: true})", script_text)
        self.assertIn("refreshLogTail({force: status.running || runJustFinished})", script_text)

    def test_dry_run_safety_gate_is_visible_in_web_ui(self) -> None:
        root = Path(__file__).resolve().parents[1]
        layout_text = (root / "src" / "templates" / "layout.html").read_text(encoding="utf-8")
        run_text = (root / "src" / "templates" / "partials" / "run.html").read_text(encoding="utf-8")
        settings_text = (root / "src" / "templates" / "partials" / "settings.html").read_text(encoding="utf-8")
        dashboard_js = (root / "src" / "static" / "dashboard.js").read_text(encoding="utf-8")

        self.assertIn("dryRunText", layout_text)
        self.assertIn("dryRunWarning", run_text)
        self.assertIn('name="app_dry_run"', settings_text)
        self.assertIn("updateDryRunUi", dashboard_js)
        self.assertIn('setCheckbox(settingsForm, "app_dry_run"', dashboard_js)

    def test_memory_sync_controls_are_visible_in_settings_ui(self) -> None:
        root = Path(__file__).resolve().parents[1]
        settings_text = (root / "src" / "templates" / "partials" / "settings.html").read_text(encoding="utf-8")
        dashboard_js = (root / "src" / "static" / "dashboard.js").read_text(encoding="utf-8")

        self.assertIn("试剂库同步", settings_text)
        self.assertIn('class="settings-section memory-sync-section"', settings_text)
        self.assertIn('name="memory_sync_enabled"', settings_text)
        self.assertIn('name="memory_sync_base_url"', settings_text)
        self.assertIn('id="uploadMemorySyncButton"', settings_text)
        memory_sync_section = settings_text.split("试剂库同步", 1)[0].rsplit("<section", 1)[-1]
        self.assertNotIn("update-section", memory_sync_section)
        self.assertIn("/api/memory/sync/upload", dashboard_js)
        self.assertIn("/api/memory/sync/download", dashboard_js)
        self.assertIn('setCheckbox(settingsForm, "memory_sync_enabled"', dashboard_js)

    def test_memory_sync_api_surfaces_clear_configuration_errors(self) -> None:
        with patch("web_app.test_memory_sync_connection", side_effect=MemorySyncError("WebDAV 用户名或应用密码未配置。")):
            with self.assertRaises(web_app.HTTPException) as context:
                web_app.api_memory_sync_test()

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("应用密码", str(context.exception.detail))

    def test_memory_sync_download_conflict_uses_409_response(self) -> None:
        with patch(
            "web_app.download_memory_sync",
            side_effect=MemorySyncError(
                "本地和云端试剂库可能存在冲突，未自动覆盖本地库。",
                status_code=409,
                payload={"conflict": True},
            ),
        ):
            with self.assertRaises(web_app.HTTPException) as context:
                web_app.api_memory_sync_download()

        self.assertEqual(context.exception.status_code, 409)
        self.assertTrue(context.exception.detail["conflict"])

    def test_web_write_mode_normalizes_retired_values(self) -> None:
        self.assertEqual(normalize_web_write_mode("save_one"), "disabled")
        self.assertEqual(normalize_web_write_mode("unknown"), "disabled")
        self.assertEqual(normalize_web_write_mode("disabled"), "disabled")
        self.assertEqual(normalize_web_write_mode("generate_library"), "generate_library")

        options = run_options(
            target_list_numbers="SJ1",
            process_all_todos="",
            process_all_todos_max="50",
            approval_write_mode="disabled",
            approval_write_min_confidence="0.8",
            approval_write_batch_size="3",
            auto_pass="",
        )

        self.assertEqual(options["APPROVAL_WRITE_MODE"], "disabled")
        self.assertEqual(options["APPROVAL_WRITE_BATCH_SIZE"], "3")

    def test_atomic_write_text_replaces_existing_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.yaml"
            path.write_text("old: true\n", encoding="utf-8")

            atomic_write_text(path, "new: true\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "new: true\n")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_api_status_does_not_call_heavy_dashboard_summaries(self) -> None:
        with (
            patch("web_app.approval_summary", side_effect=AssertionError("approval should be lazy")),
            patch("web_app.review_queue_summary", side_effect=AssertionError("review should be lazy")),
            patch("web_app.artifact_summary", side_effect=AssertionError("artifacts should be lazy")),
        ):
            response = web_app.api_status()

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
