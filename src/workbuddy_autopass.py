"""Fail-closed final submission for fully reconciled WorkBuddy lists."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from category_mapper import erp_property_options
from task_control import SafeStop, TaskControl, checkpoint


def todo_numbers(records: list[dict[str, Any]]) -> set[str]:
    return {str(row.get("清单号") or row.get("试剂清单号") or row.get("name") or "").strip()
            for row in records}


class WorkBuddyAutoPass:
    def __init__(self, bot: Any, bridge: Any, client: Any, page: Any):
        self.bot, self.bridge, self.client, self.page = bot, bridge, client, page
        self.control = TaskControl(bot.root_dir)
        # Toggling write/configuration flags must not unlock an unknown send.
        api = bot.settings.get("erp_api") or {}
        rpc = api.get("rpc") or {}
        context = [os.getenv("ERP_URL", ""), api.get("protocol"), rpc.get("endpoint"),
                   rpc.get("database") or rpc.get("db")]
        self.context = hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode()).hexdigest()
        with self.control.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS workbuddy_submissions
                          (context TEXT, list_number TEXT, task_id TEXT, state TEXT,
                           snapshot TEXT, PRIMARY KEY(context,list_number))""")

    def receipt(self, number: str) -> str:
        with self.control.connect() as db:
            row = db.execute("SELECT state FROM workbuddy_submissions WHERE context=? AND list_number=?",
                             (self.context, number)).fetchone()
        return row[0] if row else ""

    def set_state(self, number: str, state: str) -> None:
        with self.control.connect() as db:
            db.execute("UPDATE workbuddy_submissions SET state=? WHERE context=? AND list_number=?",
                       (state, self.context, number))

    def audit(self, number: str, state: str, reason: str = "") -> None:
        self.bridge.audit.info(json.dumps({"event": "workbuddy_auto_pass", "list_number": number,
            "task_id": os.getenv("AUTOMATION_TASK_ID", ""), "state": state, "reason": reason},
            ensure_ascii=False))
        print(f"[workbuddy auto_pass] {number}: {state}" + (f"；{reason}" if reason else ""), flush=True)

    def local_block(self, number: str) -> str:
        matches = self.bridge.directory / "erp_matches.json"
        if matches.exists():
            history = json.loads(matches.read_text(encoding="utf-8"))
            if any(entry.get("list_number") == number and entry.get("status") != "completed"
                   for entry in history.values()):
                return "一键匹配结果尚未核对"
        for batch in self.bridge.batches():
            items = [item for item in batch["items"] if item["input"]["试剂清单号"] == number]
            if not items or batch["status"] in {"superseded_failed", "superseded_before_dispatch", "operator_discarded_for_rejudgment"}:
                continue
            if batch.get("operator_stop_task") or batch["status"] != "completed":
                return "存在未完成、人工复核或人工停止批次"
            for item in items:
                if item["status"] != "saved" or item.get("error_code"):
                    return "存在未保存或失败明细"
                if (self.bot.approval_write_mode() == "generate_library" or batch.get("generate_library")):
                    if (item.get("library") or {}).get("status") not in {"created_verified", "existing_unchanged"}:
                        return "试剂库尚未核对完成"
        review, reason = self.bot.current_list_has_manual_review_item(number)
        return (reason or "存在人工复核项") if review else ""

    def snapshot(self, number: str) -> dict[str, tuple[str, ...]]:
        records = self.client.fetch_reagent_detail(number)
        if not records:
            raise ValueError("ERP 明细为空，无法确认整单")
        allowed = set(erp_property_options(self.bot.settings)) - {
            "拒收类", "不建议接收类", "未知类", "需人工复核", "无法判定", "证据不足", "-", ""}
        result = {}
        parent_ids = set()
        for row in records:
            identity = str(row.get("_erp_record_id") or "").strip()
            parent = str(row.get("_erp_parent_id") or "").strip()
            if (not identity or identity in result or not parent
                    or str(row.get("清单号") or row.get("试剂清单号") or "") != number
                    or row.get("_erp_parent_state") != "wait"
                    or row.get("物化特性") not in allowed):
                raise ValueError("ERP 明细身份、待办状态或类别不满足整单提交条件")
            parent_ids.add(parent)
            result[identity] = (parent, str(row.get("试剂名称") or ""),
                                str(row.get("CAS号") or ""), str(row["物化特性"]))
        if len(parent_ids) != 1:
            raise ValueError("ERP 清单父记录不唯一")
        return result

    def _find_visible_approval_modal(self) -> Any | None:
        locator_fn = getattr(self.page, "locator", None)
        if not callable(locator_fn):
            return None
        modals = locator_fn(".ant-modal")
        count = modals.count()
        if not isinstance(count, int) or count <= 0:
            return None
        for index in range(count):
            modal = modals.nth(index)
            if not modal.is_visible():
                continue
            title_loc = modal.locator(".ant-modal-title").first
            title_count = title_loc.count()
            title_text = "".join(str(title_loc.inner_text() or "").split()) if isinstance(title_count, int) and title_count > 0 else ""
            if "审批结果" in title_text:
                return modal
        return None

    @staticmethod
    def _find_modal_confirm_button(modal: Any) -> Any | None:
        primary = modal.locator(".ant-modal-footer button.ant-btn-primary").first
        primary_count = primary.count()
        if isinstance(primary_count, int) and primary_count > 0 and primary.is_visible():
            return primary
        buttons = modal.locator(".ant-modal-footer button")
        count = buttons.count()
        if not isinstance(count, int) or count <= 0:
            return None
        for index in range(count):
            btn = buttons.nth(index)
            if not btn.is_visible():
                continue
            label = "".join(str(btn.inner_text() or "").split())
            if label in {"确定", "确认", "通过"}:
                return btn
        return None

    def _confirm_approval_modal(self) -> bool:
        """Click the confirm button inside ERP's '审批结果' modal when present."""
        modal = None
        for attempt in range(12):
            modal = self._find_visible_approval_modal()
            if modal is not None:
                break
            locator_fn = getattr(self.page, "locator", None)
            if not callable(locator_fn) or not isinstance(locator_fn(".ant-modal").count(), int):
                return False
            if attempt < 11:
                self.page.wait_for_timeout(250)
        if modal is None:
            return False
        confirm_button = self._find_modal_confirm_button(modal)
        if confirm_button is None:
            raise RuntimeError("审批结果确认弹窗未找到确定按钮")
        confirm_button.click()
        for _ in range(20):
            if not modal.is_visible():
                break
            self.page.wait_for_timeout(250)
        return True

    def run(self, numbers: list[str]) -> None:
        for number in dict.fromkeys(numbers):
            checkpoint("WorkBuddy 整单提交前核对", current_list=number)
            print(f"[workbuddy scope] {number}", flush=True)
            print(f"[workbuddy progress] 整单 {number}：自动通过前校验", flush=True)
            previous = self.receipt(number)
            if previous:
                if previous != "submitted_verified":
                    raise RuntimeError(f"{number} 原整单提交结果待核对，禁止自动重发。")
                if number in todo_numbers(self.client.fetch_todo_tasks(all_pages=True)):
                    raise RuntimeError(f"{number} 原已提交清单重新出现在待办中，请人工核对。")
                continue
            if number not in todo_numbers(self.client.fetch_todo_tasks(all_pages=True)):
                self.audit(number, "skipped", "已不在当前待办中，未提交")
                continue
            reason = self.local_block(number)
            if reason:
                self.audit(number, "blocked", reason)
                continue
            try:
                before = self.snapshot(number)
                self.bot.enter_reagent_judgement_page(self.page)
                if not self.bot.open_task_detail_by_list_number(self.page, number):
                    raise ValueError("无法打开目标清单")
                info = self.bot.read_detail_info(self.page)
                if str(info.get("当前清单号") or "").strip() != number:
                    raise ValueError("页面当前清单与目标不一致")
                unmatched = self.bot.find_unmatched_reagents_across_all_pages(self.page)
                if unmatched or not self.bot.pagination_check_succeeded:
                    raise ValueError("页面存在未判定项或未完成全部分页核对")
                reason = self.local_block(number)
                if reason:
                    raise ValueError(reason)
                button = self.bot.find_top_approve_button(self.page)
                if button is None:
                    raise ValueError("未找到整单通过按钮")
                if before != self.snapshot(number):
                    raise ValueError("提交前 ERP 明细发生变化")
            except Exception as error:
                self.audit(number, "blocked", str(error))
                continue
            checkpoint("WorkBuddy ERP 整单通过请求前", current_list=number)
            snapshot = json.dumps(before, sort_keys=True, ensure_ascii=False)
            with self.control.connect() as db:
                db.execute("INSERT INTO workbuddy_submissions VALUES(?,?,?,'sending',?)",
                           (self.context, number, os.getenv("AUTOMATION_TASK_ID", ""), snapshot))
            try:
                checkpoint("WorkBuddy ERP 整单通过发送前", current_list=number)
            except SafeStop:
                with self.control.connect() as db:
                    db.execute("DELETE FROM workbuddy_submissions WHERE context=? AND list_number=? AND state='sending'",
                               (self.context, number))
                raise
            self.audit(number, "sending")
            try:
                button.click()
                # No stop checkpoint inside an in-flight submission/readback.
                self._confirm_approval_modal()
                verified_state = None
                for _ in range(2):
                    self.page.wait_for_timeout(1000)
                    if number in todo_numbers(self.client.fetch_todo_tasks(all_pages=True)):
                        raise RuntimeError("提交后仍在待办中")
                    after = self.client.fetch_reagent_detail(number)
                    state = {str(row.get("_erp_parent_state") or "") for row in after}
                    identities = {str(row.get("_erp_record_id") or ""):
                        (str(row.get("_erp_parent_id") or ""), str(row.get("试剂名称") or ""),
                         str(row.get("CAS号") or ""), str(row.get("物化特性") or "")) for row in after}
                    if (len(after) != len(before) or identities != before or len(state) != 1
                            or state & {"", "wait", "cancel", "cancelled", "reject", "rejected", "refuse", "refused", "draft"}
                            or verified_state is not None and state != verified_state):
                        raise RuntimeError("提交后的清单身份、类别或状态未能确认")
                    verified_state = state
                self.set_state(number, "submitted_verified")
                self.audit(number, "submitted_verified", "已两次核对离开待办及明细状态，不代表下游流程结束")
            except Exception as error:
                self.set_state(number, "needs_review")
                self.audit(number, "needs_review", str(error))
                raise RuntimeError(f"{number} 整单提交结果待核对，禁止自动重发。") from error
            checkpoint("WorkBuddy 整单提交核对完成", current_list=number)
