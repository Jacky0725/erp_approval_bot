"""Small, resumable file hand-off to the WorkBuddy reagent project.

No model calls or chemical decisions live here. ERP identity comes exclusively
from the saved input, never from an ID invented in the returned spreadsheet.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Iterator
from uuid import uuid4

import pandas as pd
from openpyxl import load_workbook

from audit_logger import AuditLogger
from category_mapper import erp_candidates_for_rule_category, erp_property_options
from excel_exports import write_excel_atomic
from task_control import checkpoint, TaskControl
from workbuddy_rules import copy_compact, publish_compact, verify_compact


RESULT_COLUMNS = ["批次ID", "记录ID", "试剂名称", "物化特性类别", "判断依据"]
REVIEW_CATEGORIES = {"", "需人工复核", "无法判定", "证据不足"}
EMPTY_PROPERTIES = {"", "-", "—"}
MAX_BATCH_SIZE = 100
LIBRARY_DONE = {"created_verified", "existing_unchanged"}


def batch_in_scope(state: dict[str, Any], scope: set[str] | None, *, reject_partial: bool = False) -> bool:
    if scope is None:
        return True
    numbers = {item["input"]["试剂清单号"] for item in state["items"]}
    if reject_partial and numbers & scope and not numbers <= scope:
        raise RuntimeError(f"批次 {state['batch_id']} 与所选清单部分重合，请选择原批次的全部清单：{', '.join(sorted(numbers))}。")
    return numbers <= scope


def pending_model_review(item: dict[str, Any]) -> bool:
    return item.get("status") == "review" and item.get("error_code") == "MODEL_UNRESOLVED"


LIBRARY_DONE = {"created_verified", "existing_unchanged"}


def batch_in_scope(state: dict[str, Any], scope: set[str] | None, *, reject_partial: bool = False) -> bool:
    if scope is None:
        return True
    numbers = {item["input"]["试剂清单号"] for item in state["items"]}
    if reject_partial and numbers & scope and not numbers <= scope:
        raise RuntimeError(f"批次 {state['batch_id']} 与所选清单部分重合，请选择原批次的全部清单：{', '.join(sorted(numbers))}。")
    return numbers <= scope


def pending_model_review(item: dict[str, Any]) -> bool:
    return item.get("status") == "review" and item.get("error_code") == "MODEL_UNRESOLVED"


def configured_batch_size(config: dict[str, Any]) -> int:
    value = config.get("batch_size", 20)
    if isinstance(value, bool) or not str(value).strip().isdigit():
        raise ValueError("workbuddy.batch_size 必须是 1–100 的整数。")
    size = int(value)
    if not 1 <= size <= MAX_BATCH_SIZE:
        raise ValueError("workbuddy.batch_size 必须是 1–100 的整数。")
    return size


def text(value: Any) -> str:
    return "" if value is None or value is False else str(value).strip()


def record_identity(record: dict[str, Any]) -> dict[str, str]:
    return {
        "试剂清单号": text(record.get("试剂清单号") or record.get("清单号")),
        "_erp_record_id": text(record.get("_erp_record_id") or record.get("id")),
        "试剂名称": text(record.get("试剂名称") or record.get("name")),
        "CAS号": text(record.get("CAS号") or record.get("cas_code")),
        "序号": text(record.get("序号") or record.get("sequence")),
        "type": text(record.get("type")),
        "desc": text(record.get("desc")),
    }


def record_key(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record_identity(record), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class WorkBuddyBridge:
    def __init__(self, root_dir: Path, settings: dict[str, Any]):
        self.root_dir = Path(root_dir)
        self.settings = settings
        self.config = settings.get("workbuddy", {}) or {}
        self.directory = self.root_dir / self.config.get("batch_dir", "data/workbuddy")
        self.audit = AuditLogger.from_settings(settings, self.root_dir)

    def rules_path(self) -> Path:
        path = Path(self.settings.get("paths", {}).get("structured_rules_excel", "config/rules_structured.xlsx"))
        return path if path.is_absolute() else self.root_dir / path

    def publish_compact_rules(self, workbook: bytes | None = None) -> Path:
        return publish_compact(self.directory / ".rules-cache", self.rules_path().read_bytes() if workbook is None else workbook)

    def _compact_fallback(self, state: dict[str, Any], error: Exception) -> None:
        self.audit.info(json.dumps({"event": "workbuddy_compact_fallback", "batch_id": state["batch_id"],
                                    "rules_sha256": state["rules_sha256"], "detail": str(error)}, ensure_ascii=False))
        print(f"[workbuddy] 批次 {state['batch_id']} 改用本批 XLSX 分段读取：{error}", flush=True)

    def dispatch_prompts(self, state: dict[str, Any]) -> tuple[str, str, str]:
        """Return full prompt, follow-up prompt, and the verified rule representation."""
        path = self.batch_path(state)
        prompt = (path / "prompt.txt").read_text(encoding="utf-8")
        version = state["rules_sha256"]
        if hashlib.sha256((path / "rules.xlsx").read_bytes()).hexdigest() != version:
            raise ValueError("本批规则快照被修改，禁止投递。")
        compact_sha = state.get("compact_rules_sha256")
        if compact_sha:
            try:
                if self.config.get("compact_rules") is not True:
                    raise ValueError("紧凑规则开关已关闭。")
                verify_compact(path, version, compact_sha)
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._compact_fallback(state, error)
                state.pop("compact_rules_sha256")
                self.save(state)
                compact_sha = None
        if compact_sha:
            prompt = (f"按 WB-EXCEL-v1 执行批次目录：{path}。仅处理本批，不访问 ERP 或其他批次。"
                      "本批 rules.xlsx 的完整等价读取版为 rules_compact。"
                      f"源版本 SHA-256 为 {version}。先读取 rules_compact/manifest.json 核对版本，"
                      "再按编号每次工具调用只读一个 part 文本，完整读取所有段。保留各表的原列序和规则优先级。"
                      "禁止合并输出各段、重读 XLSX 或查阅旧结果。使用 Python/openpyxl 读输入和模板，"
                      "按五列协议校验并关闭 pending 后重命名为 result.xlsx，再回复对应批次完成标记。")
        elif self.config.get("compact_rules") is True:
            prompt += "\n只读取本批原始 XLSX。分段完整读取规则和示例，禁止一次输出整表或引用旧批次结果。"
        followup = (f"按 WB-EXCEL-v1 续做同一会话的新批次目录：{path}。仅处理本批，不访问 ERP 或旧结果。"
                    f"本批 rules.xlsx 源 SHA-256 为 {version}。核对版本相同后沿用本会话已完整读取的规则，"
                    "包括优先级、阈值、排除条件和示例。只在需要时定向核对，不全量重读。"
                    "若规则或版本无法确认，停止并说明。只用 Python/openpyxl 读本批输入和模板，"
                    "按五列协议校验并关闭 pending 后重命名为 result.xlsx，再回复对应批次完成标记。")
        return prompt, followup, "compact" if compact_sha else "xlsx"

    def pause_operator_batch(self, state: dict[str, Any] | None) -> None:
        if not state or state.get("status") in {"completed", "review_pending"}:
            return
        task_id = os.getenv("AUTOMATION_TASK_ID", "")
        state["resume_status"] = state.get("resume_status") or state["status"]
        state["operator_stop_task"] = task_id
        state["status"] = "operator_stopped"
        self.save(state)
        if task_id:
            items = state.get("items", [])
            control = TaskControl(self.root_dir)
            if not control.get(task_id)["details"].get("scope_frozen"):
                control.record_details(task_id, scope=sorted({item["input"]["试剂清单号"] for item in items}), scope_frozen=True)
            TaskControl(self.root_dir).record_details(task_id,
                batch_id=state["batch_id"], count_scope="batch", saved=max(control.get(task_id)["details"].get("saved", 0), sum(item.get("status") == "saved" for item in items)),
                pending_library=sum(item.get("status") == "saved" and state.get("generate_library", False) and (item.get("library") or {}).get("status") not in {"created_verified", "existing_unchanged"} for item in items),
                remaining=sum(item.get("status") == "pending" for item in items),
                uncertain=sum(item.get("error_code") in {"ERP_WRITE", "ERP_ERROR"} or (item.get("library") or {}).get("status") in {"sending", "failed"} for item in items))

    def restore_authorized_pause(self, scope: set[str] | None = None) -> None:
        resume = os.getenv("RESUME_STOPPED_TASK_ID", "")
        allowed = self.authorized_pause_ids()
        for state in self.batches():
            if state.get("status") == "completed" or not (state.get("operator_stop_task") or state.get("status") == "operator_stopped"):
                continue
            if not batch_in_scope(state, scope, reject_partial=True):
                continue
            if not resume or state.get("operator_stop_task") not in allowed:
                raise RuntimeError("存在人工停止批次，请从 Web UI 核对恢复。")
            if state.get("status") == "operator_stopped":
                state["status"] = state.pop("resume_status", "waiting_result")
                self.save(state)

    def authorized_pause_ids(self) -> set[str]:
        resume = os.getenv("RESUME_STOPPED_TASK_ID", "")
        if not resume:
            return set()
        ids = {resume}
        task_id = os.getenv("AUTOMATION_TASK_ID", "")
        if task_id:
            ids.update(TaskControl(self.root_dir).get(task_id).get("details", {}).get("resume_ancestors") or [])
        return ids

    def release_prepared_pause(self, state: dict[str, Any]) -> None:
        if (state.get("operator_stop_task") in self.authorized_pause_ids()
                and state.get("status") == "waiting_result" and not state.get("dispatch_state")
                and not (self.batch_path(state) / "result.xlsx").exists()
                and not any(i.get("attempts") or i.get("write_started") for i in state["items"])):
            state.pop("operator_stop_task", None)
            self.save(state)

    def matched_records(self, client: Any, number: str, *, dry_run: bool) -> list[dict[str, Any]]:
        checkpoint("读取 WorkBuddy 清单", current_list=number)
        records = client.fetch_reagent_detail(number)
        if dry_run or not any(text(r.get("物化特性")) in EMPTY_PROPERTIES for r in records):
            return records
        # One list-level operation per set of ERP line ids. Persist before send;
        # an interrupted request must be reconciled, never blindly repeated.
        key = hashlib.sha256(json.dumps([number, sorted(text(r.get("_erp_record_id")) for r in records)], ensure_ascii=False).encode()).hexdigest()
        path = self.directory / "erp_matches.json"
        history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        previous = history.get(key)
        if previous:
            if previous["status"] != "completed":
                raise RuntimeError(f"清单 {number} 上次一键匹配结果待核对，未自动重试。")
            return records
        pending_ids = {item["input"]["_erp_record_id"] for state in self.batches() for item in state["items"]
                       if pending_model_review(item) and item["input"]["试剂清单号"] == number}
        if any(text(row.get("_erp_record_id") or row.get("id")) in pending_ids
               and text(row.get("物化特性")) in EMPTY_PROPERTIES for row in records):
            print(f"[workbuddy] 清单 {number} 含仍为空的人工复核项，跳过整表一键匹配。", flush=True)
            return records
        def persist():
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
            self._replace_checkpoint(temporary, path)
        history[key] = {"list_number": number, "status": "sending"}
        checkpoint("ERP 一键匹配前", current_list=number)
        persist()
        try:
            outcome = client.match_reagent_list(number)
        except Exception as error:
            history[key].update(status="needs_reconciliation", detail=str(error))
            persist()
            self.audit.info(json.dumps({"event": "workbuddy_erp_match", **history[key]}, ensure_ascii=False))
            raise
        history[key] = {"list_number": number, "status": "completed", "changes": outcome["changes"]}
        persist()
        self.audit.info(json.dumps({"event": "workbuddy_erp_match", **history[key]}, ensure_ascii=False))
        print(f"[workbuddy] 一键匹配 {number}：完成，{len(outcome['changes'])} 条类别变化。", flush=True)
        checkpoint("ERP 一键匹配已核对", current_list=number)
        return outcome["records"]

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """OS lock is released even when a worker is killed; no stale lock flag."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".lock").open("a+b") as stream:
            try:
                # Windows byte-range locks also prohibit reads from another
                # handle. Inspect length, not the byte another worker locked.
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError("另一个 WorkBuddy 批次正在处理，请勿重复启动。") from error
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def batches(self) -> list[dict[str, Any]]:
        batches = []
        for path in sorted(self.directory.glob("*/state.json")):
            state = json.loads(path.read_text(encoding="utf-8"))
            if path.parent.name != state["batch_id"]:
                raise ValueError(f"批次目录与状态不一致：{path}")
            batches.append(state)
        return batches

    def batch_path(self, state: dict[str, Any]) -> Path:
        batch_id = state["batch_id"]
        if not batch_id or Path(batch_id).name != batch_id or any(c not in "0123456789_abcdef" for c in batch_id):
            raise ValueError("批次 ID 无效")
        path = (self.directory / batch_id).resolve()
        if path.parent != self.directory.resolve():
            raise ValueError("批次路径越界")
        return path

    def save(self, state: dict[str, Any]) -> None:
        path = self.batch_path(state) / "state.json"
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        self._replace_checkpoint(temporary, path)

    @staticmethod
    def _replace_checkpoint(temporary: Path, path: Path) -> None:
        # Windows readers/virus scanners can briefly deny rename. Retry only
        # this local commit, never the ERP operation guarded by the checkpoint.
        for attempt in range(6):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (2 ** attempt))

    def check_before_dispatch(self, state: dict[str, Any], client: Any) -> list[str]:
        """Recheck unsent batch against ERP without changing it or its state."""
        cache: dict[str, list[dict[str, Any]]] = {}
        problems = []
        for item in state["items"]:
            identity = item["input"]
            number = identity["试剂清单号"]
            if number not in cache:
                cache[number] = client.fetch_reagent_detail(number)
            matches = [r for r in cache[number] if text(r.get("_erp_record_id") or r.get("id")) == identity["_erp_record_id"]]
            reason = ""
            if len(matches) != 1:
                reason = "ERP 明细不存在或不唯一"
            elif record_identity({**matches[0], "试剂清单号": number}) != identity:
                reason = "ERP 明细身份或判定信息已变化"
            elif matches[0].get("_erp_parent_state") != "wait":
                reason = "ERP 清单不是待审批状态或状态不可读取"
            elif "物化特性" not in matches[0]:
                reason = "ERP 未返回物化特性"
            elif text(matches[0]["物化特性"]) not in EMPTY_PROPERTIES:
                reason = f"ERP 物化特性已为 {text(matches[0]['物化特性'])}"
            if reason:
                problems.append(f"{number}/{identity['_erp_record_id']}：{reason}")
        return problems

    def prepare(self, records: Iterable[dict[str, Any]], *, compact: bool = False,
                scope: set[str] | None = None) -> dict[str, Any] | None:
        checkpoint("准备 WorkBuddy 批次")
        existing = self.batches()
        allowed_pauses = self.authorized_pause_ids()
        if any((state.get("status") == "operator_stopped" or state.get("operator_stop_task"))
               and state.get("status") != "completed" and batch_in_scope(state, scope, reject_partial=True)
               and state.get("operator_stop_task") not in allowed_pauses for state in existing):
            raise RuntimeError("存在人工停止的 WorkBuddy 批次，必须通过 Web UI 核对后恢复。")
        mode = self.config.get("prompt_mode", "full")
        if mode not in {"full", "project"}:
            raise ValueError("workbuddy.prompt_mode 必须为 full 或 project。")
        rule_bytes = self.rules_path().read_bytes()
        rule_digest = hashlib.sha256(rule_bytes).hexdigest()
        waiting = [s for s in existing if s["status"] == "waiting_result" and s["rules_sha256"] == rule_digest
                   and batch_in_scope(s, scope, reject_partial=True)]
        if waiting:
            return waiting[0]
        # A failed row is retried from its original batch, not silently reissued.
        known = {item["key"] for s in existing if s["rules_sha256"] == rule_digest and s["status"] != "superseded_before_dispatch" for item in s["items"]}
        pending_reviews = {(item["input"]["试剂清单号"], item["input"]["_erp_record_id"])
                           for state in existing for item in state["items"] if pending_model_review(item)}
        stable_ids: set[tuple[str, str]] = set()
        selected = []
        size = configured_batch_size(self.config)
        for record in records:
            identity = record_identity(record)
            if not identity["_erp_record_id"] or not identity["试剂清单号"] or not identity["试剂名称"]:
                raise ValueError("WorkBuddy 输入缺少稳定明细 ID、清单号或试剂名称。")
            if record_key(record) in known or text(record.get("物化特性")) not in EMPTY_PROPERTIES:
                continue
            stable_id = (identity["试剂清单号"], identity["_erp_record_id"])
            if stable_id in pending_reviews:
                continue
            if stable_id in stable_ids:
                raise ValueError("输入中同一 ERP 明细 ID 对应多条不同记录。")
            stable_ids.add(stable_id)
            known.add(record_key(record))
            selected.append({"key": record_key(record), "input": identity, "status": "pending", "attempts": 0})
            if len(selected) == size:
                break
        if not selected:
            return None
        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8]
        state = {"batch_id": batch_id, "status": "waiting_result", "rules_sha256": hashlib.sha256(rule_bytes).hexdigest(), "batch_size_limit": size, "items": selected}
        path = self.batch_path(state)
        path.mkdir(parents=True)
        (path / "rules.xlsx").write_bytes(rule_bytes)
        if compact:
            try:
                state["compact_rules_sha256"] = copy_compact(self.directory / ".rules-cache", path, rule_digest)
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._compact_fallback(state, error)
        rows = [{**item["input"], "批次ID": batch_id, "记录ID": item["key"]} for item in selected]
        write_excel_atomic(pd.DataFrame(rows), path / "input.xlsx")
        result_rows = [{"批次ID": batch_id, "记录ID": i["key"], "试剂名称": i["input"]["试剂名称"], "物化特性类别": "", "判断依据": ""} for i in selected]
        write_excel_atomic(pd.DataFrame(result_rows, columns=RESULT_COLUMNS), path / "result_template.xlsx")
        prompt = (
            f"试剂审批批次 {batch_id}，共 {len(selected)} 条。独立判定，不访问或写入 ERP，不修改规则。\n"
            f"输入：{path / 'input.xlsx'}\n规则：{path / 'rules.xlsx'}\n"
            f"请完整读取本批规则（含优先级及排除条件），以此快照为准。\n"
            "优先复用已有 Excel 处理工具或可用 Python 环境，避免每批新建环境或重复安装依赖。\n"
            f"填写模板：{path / 'result_template.xlsx'}，仅一个工作表 Sheet1，严格五列：{'、'.join(RESULT_COLUMNS)}。\n"
            "批次ID、记录ID、试剂名称原样返回，每条一行，不漏项、不重复，不使用公式。"
            "无法判定时类别填“需人工复核”，判断依据说明缺失信息；不得猜测。"
            "判断依据写明适用规则和事实来源，模型知识如实标注。\n"
            f"先完整保存并关闭 {path / 'result.pending.xlsx'}，最后重命名为 {path / 'result.xlsx'}。"
            "只有正式文件保存成功后才回复完成。不要只返回表格或代码。"
            "请在 pending 文件上完成校验，重命名必须是最后一次文件操作；发布后只回复完成，不再读取、修改或重建本批文件。"
        )
        if mode == "project":
            prompt = (f"按 WB-EXCEL-v1 执行批次目录：{path}。仅处理本批，结果保存到该目录的 result.xlsx。"
                      "本批使用本机 Python/openpyxl 读写 Excel，不使用表格子代理或编辑器 SDK，避免文件占用；"
                      "写完关闭文件后再重命名。分段完整读取规则及示例，避免整表输出截断。无需创建工作日志或额外产物。")
        state["prompt_mode"] = mode
        (path / "prompt.txt").write_text(prompt, encoding="utf-8")
        self.save(state)
        self.audit.info(json.dumps({"event": "workbuddy_batch_prepared", "batch_id": batch_id, "rows": len(selected)}, ensure_ascii=False))
        return state

    def reconcile_unsent_batch(self, state: dict[str, Any], client: Any) -> bool:
        """Retire an unsent snapshot only when ERP categories have moved on."""
        if (state["status"] != "waiting_result" or state.get("dispatch_state")
                or (self.batch_path(state) / "result.xlsx").exists()
                or any(i.get("attempts", 0) or i.get("write_started") for i in state["items"])):
            return False
        cache = {}
        categories = {}
        for item in state["items"]:
            identity = item["input"]
            number = identity["试剂清单号"]
            if number not in cache:
                cache[number] = client.fetch_reagent_detail(number)
            matches = [r for r in cache[number] if text(r.get("_erp_record_id")) == identity["_erp_record_id"]]
            if (len(matches) != 1 or record_identity(matches[0]) != identity
                    or matches[0].get("_erp_parent_state") != "wait" or "物化特性" not in matches[0]):
                raise RuntimeError("旧批次 ERP 身份或状态变化，需核对，未自动结束。")
            categories[item["key"]] = text(matches[0]["物化特性"])
        changed = sum(c not in EMPTY_PROPERTIES for c in categories.values())
        if not changed:
            return False
        state.update(status="superseded_before_dispatch", reconciliation={"reason": "未发送批次中已有记录由ERP完成分类，按当前状态重新组批。", "categories": categories})
        self.save(state)
        self.audit.info(json.dumps({"event": "workbuddy_unsent_superseded", "batch_id": state["batch_id"], "classified": changed, "categories": categories}, ensure_ascii=False))
        print(f"[workbuddy] 旧批次 {state['batch_id']} 未曾投递，{changed} 条已分类；原文件保留，按 ERP 当前状态重新组批。", flush=True)
        return True

    def load_results(self, state: dict[str, Any]) -> None:
        rules = self.batch_path(state) / "rules.xlsx"
        if hashlib.sha256(rules.read_bytes()).hexdigest() != state["rules_sha256"]:
            raise ValueError("本批规则快照被修改，禁止导入。")
        if state.get("compact_rules_sha256"):
            verify_compact(self.batch_path(state), state["rules_sha256"], state["compact_rules_sha256"])
        path = self.batch_path(state) / "result.xlsx"
        if path.resolve().parent != self.batch_path(state):
            raise ValueError("结果文件路径越界。")
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if state.get("result_sha256"):
            if digest != state["result_sha256"]:
                raise ValueError("已导入的结果文件发生变化；为避免同批混用，拒绝继续写入。")
            return
        # Read formulas as formulas: data_only would conceal them as cached values.
        import io
        workbook = load_workbook(io.BytesIO(payload), read_only=False, data_only=False, keep_links=False)
        try:
            if len(workbook.worksheets) != 1:
                raise ValueError("结果 Excel 必须只有一个工作表。")
            sheet = workbook.worksheets[0]
            if sheet.merged_cells.ranges:
                raise ValueError("结果 Excel 不允许合并单元格。")
            if sheet.max_row > len(state["items"]) + 1 or sheet.max_column != len(RESULT_COLUMNS):
                raise ValueError("结果 Excel 行列数不符合本批约定。")
            if any(c.data_type == "f" for row in sheet for c in row):
                raise ValueError("结果 Excel 不允许公式。")
            rows = list(sheet.iter_rows(values_only=True))
            if not rows or list(rows[0]) != RESULT_COLUMNS:
                raise ValueError(f"结果表头必须为：{'、'.join(RESULT_COLUMNS)}")
            expected = {i["key"]: i for i in state["items"]}
            parsed = {}
            for raw in rows[1:]:
                if not any(v is not None for v in raw):
                    continue
                values = dict(zip(RESULT_COLUMNS, map(text, raw)))
                key = values["记录ID"]
                if key not in expected or key in parsed or values["批次ID"] != state["batch_id"]:
                    raise ValueError("结果存在重复记录、未知记录或批次不一致。")
                if values["试剂名称"] != expected[key]["input"]["试剂名称"] or not values["判断依据"]:
                    raise ValueError("试剂名称被修改或缺少判断依据。")
                parsed[key] = values
            if set(parsed) != set(expected):
                raise ValueError("结果记录不完整，禁止部分导入。")
            for key, values in parsed.items():
                expected[key]["result"] = values
            state["result_sha256"] = digest
            state["status"] = "imported"
            self.save(state)
        finally:
            workbook.close()

    def process(self, state: dict[str, Any], client: Any, memory: Any, *, dry_run: bool = True,
                generate_library: bool = False, confirm_reviews: bool = False) -> dict[str, Any]:
        """Reconcile the original record before each write; no chemical reclassification."""
        if state.get("status") == "operator_stopped":
            raise RuntimeError("人工停止批次禁止直接导入，请从受控恢复入口启动。")
        checkpoint("校验 WorkBuddy 结果", batch_id=state["batch_id"])
        if state["status"] == "completed":
            return state
        self.load_results(state)
        if (not dry_run and hashlib.sha256(self.rules_path().read_bytes()).hexdigest() != state["rules_sha256"]
                and not (confirm_reviews and self.can_confirm_stale_reviews(state))):
            raise ValueError("正式规则已更新，本批旧版本结果禁止写入；请按新规则重新判定。")
        if not dry_run:
            # Freeze the operation requested on first import; resume must not
            # silently drop a pending library operation when UI mode changes.
            state.setdefault("generate_library", generate_library)
            if confirm_reviews:
                self._confirm_reviewed_rows(state, client)
        for item in state["items"]:
            checkpoint("WorkBuddy 明细安全点", batch_id=state["batch_id"])
            if item["status"] in {"saved", "review"}:
                continue
            result = item["result"]
            category = result["物化特性类别"]
            candidates = [c for c in erp_candidates_for_rule_category(category, self.settings) if c in erp_property_options(self.settings)]
            if dry_run:
                print(f"[workbuddy preview] {item['input']['试剂名称']} -> {category}; ERP={candidates}")
                continue
            # Store model provenance separately from manually verified memory.
            memory.record_workbuddy_result(state["batch_id"], item["key"], item["input"], result)
            if category in REVIEW_CATEGORIES:
                self._record(state, item, "review", "判定", "MODEL_UNRESOLVED", result["判断依据"])
                continue
            if len(candidates) != 1:
                self._record(state, item, "blocked", "类别映射", "CATEGORY_MAPPING", f"判定类别“{category}”没有唯一 ERP 映射；候选：{candidates}")
                continue
            target = candidates[0]
            item["erp_category"] = target
            identity = item["input"]
            try:
                records = client.fetch_reagent_detail(identity["试剂清单号"])
                matches = [r for r in records if text(r.get("_erp_record_id") or r.get("id")) == identity["_erp_record_id"]]
                if len(matches) != 1 or record_identity(matches[0]) != identity:
                    self._record(state, item, "blocked", "写前核对", "IDENTITY_CHANGED", "ERP 明细身份或判定相关信息已变化。")
                    continue
                if matches[0].get("_erp_parent_state") != "wait":
                    self._record(state, item, "blocked", "写前核对", "TASK_NOT_PENDING", "ERP 清单不是待审批状态，或未能读取其状态。")
                    continue
                if "物化特性" not in matches[0]:
                    self._record(state, item, "blocked", "写前核对", "PROPERTY_UNAVAILABLE", "ERP 返回中缺少物化特性字段，未写入。")
                    continue
                current = text(matches[0].get("物化特性"))
                if current == target:
                    if item.get("write_started"):
                        memory.record_workbuddy_result(state["batch_id"], item["key"], identity, result, synced=True)
                        self._record(state, item, "saved", "回读", "", "上次写入结果已在 ERP 中确认。")
                    else:
                        self._record(state, item, "blocked", "写前核对", "ALREADY_CLASSIFIED", "ERP 已有类别，未覆盖；不能确认由本批写入。")
                    continue
                if current not in EMPTY_PROPERTIES:
                    self._record(state, item, "blocked", "写前核对", "PROPERTY_CHANGED", f"ERP 物化特性已为“{current}”，未覆盖。")
                    continue
                if item["attempts"] >= max(1, int(self.config.get("max_write_attempts", 2))):
                    continue
                checkpoint("ERP 类别保存前", batch_id=state["batch_id"], current_list=identity["试剂清单号"])
                item["attempts"] += 1
                item["write_started"] = True
                self.save(state)  # Durable before sending a non-idempotent request.
                outcome = client.save_physicochemical_property_by_id(identity["_erp_record_id"], target, {**identity, "最终建议类别": target}, matches[0])
                if outcome.saved and outcome.verified:
                    memory.record_workbuddy_result(state["batch_id"], item["key"], identity, result, synced=True)
                    self._record(state, item, "saved", "回读", "", outcome.detail)
                else:
                    code = "ERP_CATEGORY_UNAVAILABLE" if "category is not available" in outcome.detail else "ERP_WRITE"
                    self._record(state, item, "failed", "保存/回读", code, outcome.detail)
            except Exception as error:
                self._record(state, item, "failed", "保存/回读", "ERP_ERROR", str(error))
        if not dry_run:
            if state.get("generate_library"):
                for item in state["items"]:
                    checkpoint("试剂库生成前安全点", batch_id=state["batch_id"])
                    if item["status"] != "saved":
                        continue
                    library = item.get("library") or {}
                    if library.get("status") in LIBRARY_DONE:
                        continue
                    if library:
                        print(f"[workbuddy attention] {item['input']['试剂名称']} 试剂库待核对：{library.get('detail', library['status'])}")
                        continue
                    item["library"] = {"status": "sending", "detail": "请求结果未确认时禁止自动重发。"}
                    self.save(state)
                    try:
                        item["library"] = client.generate_reagent_library_by_id(item["input"]["_erp_record_id"], item["input"], item["erp_category"])
                    except Exception as error:
                        item["library"] = {"status": "failed", "detail": str(error)}
                    self.audit.info(json.dumps({"event": "workbuddy_library", "batch_id": state["batch_id"], "record_id": item["key"], **item["library"]}, ensure_ascii=False))
                    self.save(state)
                    print(f"[workbuddy library] {item['input']['试剂名称']} {item['library']['status']}: {item['library']['detail']}", flush=True)
            checkpoint("WorkBuddy 批次结束安全点", batch_id=state["batch_id"])
            reviews = sum(pending_model_review(item) for item in state["items"])
            blocked = sum(not pending_model_review(item) and (
                item["status"] != "saved" or (state.get("generate_library")
                                             and (item.get("library") or {}).get("status") not in LIBRARY_DONE)
            ) for item in state["items"])
            state["status"] = "needs_attention" if blocked else "review_pending" if reviews else "completed"
            state["pending_verification_count"] = blocked
            if state["status"] == "review_pending":
                state.pop("operator_stop_task", None)
            self.save(state)
            if state["status"] == "completed":
                self.cleanup(state)
            elif blocked:
                print(f"[workbuddy attention] 批次 {state['batch_id']} 有 {blocked} 条写入或入库待核对，结果文件已保留。")
            if reviews:
                print(f"[workbuddy review] 批次 {state['batch_id']} 有 {reviews} 条待人工复核，原始结果已保留。")
        return state

    def require_safe_continuation(self, state: dict[str, Any], scope: set[str] | None) -> None:
        count = state.get("pending_verification_count", 0)
        if any(item.get("error_code") in {"ERP_ERROR", "ERP_WRITE"} for item in state["items"]):
            self.report_pending_reviews(scope)
            raise RuntimeError(f"批次 {state['batch_id']} 有 {count} 条写入或入库待核对。ERP 保存或回读异常，结果已保留。")
        if state.get("generate_library") and state["status"] == "needs_attention":
            self.report_pending_reviews(scope)
            raise RuntimeError(f"批次 {state['batch_id']} 有 {count} 条写入或入库待核对，类别保存状态和结果已保留。")

    def report_pending_reviews(self, scope: set[str] | None) -> None:
        records = {(item["input"]["试剂清单号"], item["input"]["_erp_record_id"])
                   for state in self.batches() for item in state["items"]
                   if pending_model_review(item) and (scope is None or item["input"]["试剂清单号"] in scope)}
        print(f"[workbuddy review_pending] {len(records)}", flush=True)

    def review_confirmation_path(self, state: dict[str, Any]) -> Path:
        return self.root_dir / "data" / "workbuddy_review_confirmations" / f"{state['batch_id']}.json"

    def can_confirm_stale_reviews(self, state: dict[str, Any]) -> bool:
        items = state["items"]
        return bool(state.get("result_sha256") and self.review_confirmation_path(state).is_file()
                    and any(pending_model_review(item) or item.get("operator_confirmed_category") for item in items)
                    and all(pending_model_review(item) or (
                        item["status"] == "saved" and (item.get("operator_confirmed_category")
                        or not state.get("generate_library") or (item.get("library") or {}).get("status") in LIBRARY_DONE)
                    ) for item in items))

    def _confirm_reviewed_rows(self, state: dict[str, Any], client: Any) -> None:
        path = self.review_confirmation_path(state)
        if not path.is_file():
            return
        confirmations = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(confirmations, dict):
            raise ValueError("人工复核确认文件必须为记录 ID 到已在 ERP 保存类别的 JSON 对象。")
        eligible = {item["key"]: item for item in state["items"]
                    if pending_model_review(item) or item.get("operator_confirmed_category")}
        verified = []
        for key, category in confirmations.items():
            if key not in eligible or category not in erp_property_options(self.settings):
                raise ValueError(f"记录 {key} 不属于待复核范围或类别无效。")
            item = eligible[key]
            if item.get("operator_confirmed_category"):
                if item["operator_confirmed_category"] != category:
                    raise ValueError(f"记录 {key} 已核对的类别发生变化。")
                continue
            identity = item["input"]
            matches = [row for row in client.fetch_reagent_detail(identity["试剂清单号"])
                       if text(row.get("_erp_record_id") or row.get("id")) == identity["_erp_record_id"]]
            if (len(matches) != 1 or record_identity(matches[0]) != identity
                    or matches[0].get("_erp_parent_state") != "wait" or text(matches[0].get("物化特性")) != category):
                raise ValueError(f"记录 {key} 的 ERP 身份、状态或人工保存类别未核对通过。")
            verified.append((item, category))
        for item, category in verified:
            item.update(status="saved", stage="人工复核回读", error_code="", detail="已确认 ERP 中人工保存的类别。",
                        erp_category=category, operator_confirmed_category=category)
            self.save(state)
            self.audit.info(json.dumps({"event": "workbuddy_manual_review_confirmed", "batch_id": state["batch_id"],
                                        "record_id": item["key"], "erp_category": category}, ensure_ascii=False))
            print(f"[workbuddy review] 记录 {item['key']} 的人工保存类别已回读。", flush=True)

    def cleanup(self, state: dict[str, Any]) -> None:
        if state["status"] != "completed" or not all(i["status"] == "saved" for i in state["items"]):
            return
        try:
            (self.batch_path(state) / "result.xlsx").unlink(missing_ok=True)
            state["cleaned"] = True
            state.pop("cleanup_error", None)
        except OSError as error:
            state["cleanup_error"] = str(error)
        self.save(state)

    def _record(self, state: dict[str, Any], item: dict[str, Any], status: str, stage: str, code: str, reason: str) -> None:
        item.update(status=status, stage=stage, error_code=code, detail=reason)
        event = {"event": "workbuddy_result", "batch_id": state["batch_id"], "record_id": item["key"], "status": status, "stage": stage, "code": code, "detail": reason, "category": item["result"]["物化特性类别"], "erp_category": item.get("erp_category", "")}
        self.audit.info(json.dumps(event, ensure_ascii=False))
        self.save(state)
        task_id = os.getenv("AUTOMATION_TASK_ID", "")
        if status == "saved" and task_id:
            control = TaskControl(self.root_dir)
            keys = set(control.get(task_id)["details"].get("verified_saved_keys", []))
            keys.add(f"{state['batch_id']}:{item['key']}")
            control.record_details(task_id, verified_saved_keys=sorted(keys), saved=len(keys))
        print(f"[workbuddy] {item['input']['试剂名称']} {status}: {reason}")
