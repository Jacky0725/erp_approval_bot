from __future__ import annotations

import copy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import Error, Locator, Page

import pandas as pd

from approval_batch_state import MultiPageWriteState, normalize_write_result
from approval_suggestion_metrics import format_suggestion_summary, summarize_approval_suggestions, write_skip_reason
from approval_writer import ApprovalWriter
from audit_logger import AuditLogger
from category_mapper import erp_property_options, is_non_writable_rule_category, to_erp_property
from chemical_searcher import ChemicalSearcher
from enrichment_metrics import EnrichmentMetrics
from erp_api_client import ErpApiClient, normalize_erp_write_backend
from erp_api_discovery import ApiDiscoveryAnalyzer, ApiDiscoveryRecorder, ErpApiConfigurator
from llm_extractor import LlmExtractor
from name_normalizer import NameNormalizer
from non_reagent_classifier import NonReagentClassifier
from reagent_name_rules import UNKNOWN_CATEGORY, unknown_reagent_name_reason
from reagent_data_minimizer import minimize_reagent_record
from reagent_memory import ReagentMemory
from rule_engine import NORMAL_CATEGORY, RuleEngine
from rule_maintainer import RuleMaintainer
from stage_logger import StageLogger
from ui_waits import wait_until_row_value, wait_until_spinner_hidden
from write_failure_diagnostics import build_write_failure_debug_payload


class ApprovalFlowMixin:

    def run_workbuddy_handoff(self, *, import_results: bool = False) -> None:
        """File bridge pilot; never routes through model-fallback or AUTO_PASS."""
        from workbuddy_bridge import WorkBuddyBridge

        bridge = WorkBuddyBridge(self.root_dir, self.settings)

        def after_login(page: Page) -> None:
            client = ErpApiClient(page, self.settings)
            numbers = list(getattr(self, "target_list_numbers", []) or [])
            single = str(getattr(self, "target_list_number", "") or "").strip()
            if single and single not in numbers:
                numbers.append(single)
            if import_results:
                dry_run = self.dry_run_enabled() or self.approval_write_mode() == "disabled"
                if not numbers and not self._truthy(os.getenv("PROCESS_ALL_TODOS", "false")):
                    raise RuntimeError("请先选择待办清单，或明确勾选处理全部待办，再导入结果。")
                if not dry_run and not bridge.config.get("write_enabled", False):
                    raise RuntimeError("WorkBuddy 正式写入尚未启用；请先完成单批验证。")
                if not dry_run and self.erp_write_backend() != "api_write_with_web_verify":
                    raise RuntimeError("WorkBuddy 回写目前只支持已验证 ERP API，请选择接口写入后端。")
                if not dry_run and not client.write_enabled():
                    raise RuntimeError("WorkBuddy 回写需要已验证且启用的 ERP API；当前禁止写入。")
                memory = ReagentMemory.from_settings(self.settings, self.root_dir)
                processed = 0
                for state in bridge.batches():
                    batch_numbers = {i["input"]["试剂清单号"] for i in state["items"]}
                    if numbers and not batch_numbers.issubset(set(numbers)):
                        print(f"[workbuddy] 跳过 {state['batch_id']}：该批次包含本次未选择的清单。")
                        continue
                    if state["status"] == "completed":
                        if not dry_run and not state.get("cleaned"):
                            bridge.cleanup(state)
                        continue
                    if state["rules_sha256"] != hashlib.sha256(bridge.rules_path().read_bytes()).hexdigest():
                        print(f"[workbuddy attention] 跳过旧规则批次 {state['batch_id']}，请准备新批次重新判定；旧文件保留。")
                        continue
                    if not (bridge.batch_path(state) / "result.xlsx").exists():
                        continue
                    bridge.process(state, client, memory, dry_run=dry_run)
                    processed += 1
                print(f"[workbuddy] 已处理结果批次：{processed}；dry_run={dry_run}")
                return
            if not numbers:
                if not self._truthy(os.getenv("PROCESS_ALL_TODOS", "false")):
                    raise RuntimeError("请先选择待办清单，或明确勾选处理全部待办。")
                numbers = [str(r.get("清单号") or r.get("试剂清单号") or r.get("name") or "").strip() for r in client.fetch_todo_tasks(all_pages=True)]
            def records():
                for number in numbers:
                    for record in client.fetch_reagent_detail(number):
                        yield {**record, "试剂清单号": number}
            state = bridge.prepare(records())
            if state is None:
                print("[workbuddy] 没有新的未判定试剂；已有失败项请从原批次恢复。")
                return
            path = bridge.batch_path(state)
            if not {i["input"]["试剂清单号"] for i in state["items"]}.issubset(set(numbers)):
                raise RuntimeError("存在其他清单的未完成批次；请先处理原批次，未投递新清单。")
            print(f"[workbuddy] 批次：{state['batch_id']}，{len(state['items'])} 条。")
            print(f"[workbuddy] 任务文本：{path / 'prompt.txt'}")
            print(f"[workbuddy] 请在 WorkBuddy 试剂审批项目执行该任务；结果保存：{path / 'result.xlsx'}")

        with bridge.exclusive():
            self.run_after_login_capture("workbuddy.png", "workbuddy.html", after_login)

    def run_workbuddy_loop(self) -> None:
        """Serial batches; the existing process manager provides Stop/kill control."""
        import time
        from workbuddy_bridge import WorkBuddyBridge
        from workbuddy_desktop import WorkBuddyDesktop

        bridge = WorkBuddyBridge(self.root_dir, self.settings)
        if not bridge.config.get("desktop_enabled", False):
            raise RuntimeError("WorkBuddy 自动投递尚未启用；请先完成客户端适配验证，或使用准备/导入入口。")
        dry_run = self.dry_run_enabled() or self.approval_write_mode() == "disabled"
        if not dry_run and not bridge.config.get("write_enabled", False):
            raise RuntimeError("WorkBuddy 正式写入尚未启用；请先完成单批验证。")
        if not dry_run and self.erp_write_backend() != "api_write_with_web_verify":
            raise RuntimeError("WorkBuddy 回写目前只支持已验证 ERP API，请选择接口写入后端。")

        def after_login(page: Page) -> None:
            client = ErpApiClient(page, self.settings)
            if not dry_run and not client.write_enabled():
                raise RuntimeError("ERP API 未验证或未启用，禁止 WorkBuddy 回写。")
            memory = ReagentMemory.from_settings(self.settings, self.root_dir)
            desktop = WorkBuddyDesktop(bridge.config)
            while True:
                numbers = list(getattr(self, "target_list_numbers", []) or [])
                single = str(getattr(self, "target_list_number", "") or "").strip()
                if single and single not in numbers:
                    numbers.append(single)
                if not numbers:
                    if not self._truthy(os.getenv("PROCESS_ALL_TODOS", "false")):
                        raise RuntimeError("请先选择清单或勾选处理全部待办。")
                    numbers = [str(r.get("清单号") or r.get("name") or "").strip() for r in client.fetch_todo_tasks(all_pages=True)]
                def records():
                    for number in numbers:
                        for record in client.fetch_reagent_detail(number):
                            yield {**record, "试剂清单号": number}
                # Resume a prepared/imported batch before fetching a new one.
                digest = hashlib.sha256(bridge.rules_path().read_bytes()).hexdigest()
                outstanding = [s for s in bridge.batches() if s["status"] in {"waiting_result", "imported"} and s["rules_sha256"] == digest]
                state = outstanding[0] if outstanding else bridge.prepare(records())
                if state is None:
                    print("[workbuddy] 当前范围无新记录，循环结束；失败项保留原批次。")
                    return
                if not {i["input"]["试剂清单号"] for i in state["items"]}.issubset(set(numbers)):
                    raise RuntimeError("未完成批次不属于本次清单范围，已停止，未投递或写入。")
                path = bridge.batch_path(state)
                if not (path / "result.xlsx").exists():
                    if state.get("dispatch_state") == "sending":
                        raise RuntimeError(f"批次 {state['batch_id']} 上次投递结果不明；请核对 WorkBuddy，禁止自动重发。")
                    if not state.get("dispatch_state"):
                        state["dispatch_state"] = "sending"
                        bridge.save(state)
                        desktop.send((path / "prompt.txt").read_text(encoding="utf-8"))
                        state["dispatch_state"] = "submitted"
                        bridge.save(state)
                    deadline = time.monotonic() + int(bridge.config.get("result_timeout_seconds", 900))
                    while not (path / "result.xlsx").exists():
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f"WorkBuddy 结果等待超时：{path}；保留批次，不重发。")
                        time.sleep(2)
                desktop.wait_until_idle(int(bridge.config.get("result_timeout_seconds", 900)))
                bridge.process(state, client, memory, dry_run=dry_run)
                if dry_run:
                    print("[workbuddy] 单批测试完成：未写入 ERP、未入记忆、未删除 Excel。")
                    return
                if any(i.get("error_code") in {"ERP_ERROR", "ERP_WRITE"} for i in state["items"]):
                    raise RuntimeError("ERP 保存或回读异常，已保留文件并暂停循环。")

        with bridge.exclusive():
            self.run_after_login_capture("workbuddy_loop.png", "workbuddy_loop.html", after_login)

    REJECT_DECISION_CATEGORIES = {"不建议接收类", "拒收类"}

    @staticmethod
    def should_verify_api_page(row_index: int, is_canary: bool, api_settings: dict[str, Any]) -> bool:
        if is_canary:
            return True
        if not bool(api_settings.get("web_verify_after_api_write", True)):
            return False
        try:
            sample_every = max(1, int(api_settings.get("web_verify_sample_every", 1)))
        except (TypeError, ValueError):
            sample_every = 1
        return row_index % sample_every == 0

    @classmethod
    def is_terminal_reject_decision(cls, category: Any) -> bool:
        """Return whether a rule reached the terminal no-procurement outcome."""
        return str(category or "").strip() in cls.REJECT_DECISION_CATEGORIES

    @classmethod
    def apply_terminal_reject_policy(cls, suggestion: dict[str, Any]) -> dict[str, Any]:
        """Send a formal reject directly to the ERP reject-property path."""
        category = str(suggestion.get("最终建议类别") or "").strip()
        if not cls.is_terminal_reject_decision(category):
            return suggestion
        # Keep an unverified reject in review; high_confidence_write_candidates
        # applies the same gate immediately before an ERP write.
        if not cls.terminal_reject_auto_write_allowed(suggestion):
            suggestion["需人工复核"] = True
            suggestion["ERP写入状态"] = "blocked_unverified_reject"
            suggestion["ERP写入说明"] = "拒收规则缺少可审计规则 ID、可靠身份或明确业务处置依据。"
            return suggestion
        suggestion["需人工复核"] = False
        suggestion["ERP写入状态"] = "eligible_for_auto_write"
        suggestion["ERP写入说明"] = "已命中不建议接收规则；跳过人工复核并写入 ERP 的拒收类。"
        return suggestion

    @classmethod
    def terminal_reject_auto_write_allowed(cls, suggestion: dict[str, Any]) -> bool:
        """Allow a deterministic reject rule to write directly to ERP.

        A terminal procurement decision must not be held for manual identity
        confirmation once a formal rule has matched.  An advisory-only or
        unverified V3 model response is still not a formal reject decision.
        """
        if not cls.is_terminal_reject_decision(suggestion.get("最终建议类别")):
            return False
        if cls._truthy(suggestion.get("LLM辅助意见仅供复核")) or cls._truthy(suggestion.get("V3模型未核实")):
            return False
        return bool(str(suggestion.get("命中规则ID") or "").strip()) or cls._truthy(
            suggestion.get("V3可核验直写")
        )

    @staticmethod
    def apply_unknown_auto_write_policy(suggestion: dict[str, Any]) -> dict[str, Any]:
        """Make every final unknown-category decision eligible for ERP writing."""
        if str(suggestion.get("最终建议类别") or "").strip() != UNKNOWN_CATEGORY:
            return suggestion
        suggestion["需人工复核"] = False
        suggestion["置信度"] = 1.0
        suggestion["未知类判定状态"] = "统一自动写入"
        return suggestion

    @classmethod
    def apply_unresolved_product_unknown_policy(
        cls,
        suggestion: dict[str, Any],
        search_result: dict[str, Any],
        name_result: dict[str, Any],
        classification: dict[str, Any],
    ) -> dict[str, Any]:
        """Write an explicitly unresolvable product/formulation as Unknown.

        This is a business label for an absent chemical identity, never a
        safety conclusion.  It is intentionally narrower than a generic web
        lookup failure: valid CAS records, confirmed mixture components, and
        deterministic risk-rule hits retain their existing handling.
        """
        if str(suggestion.get("最终建议类别") or "").strip() == UNKNOWN_CATEGORY:
            return cls.apply_unknown_auto_write_policy(suggestion)
        if not cls._unresolved_product_is_unknown_eligible(search_result, name_result, classification, suggestion):
            return suggestion
        reason = (
            "ERP 未提供有效 CAS；名称属于商品名、未知配方或组分不完整的混合物，"
            "且未获得可核验的化学身份或有效物化信息，按业务口径写入未知类。"
        )
        suggestion.update({
            "最终建议类别": UNKNOWN_CATEGORY,
            "命中类别": UNKNOWN_CATEGORY,
            "命中规则ID": "BUS-UNKNOWN-UNRESOLVED-PRODUCT",
            "规则原因": reason,
            "证据": reason,
            "置信度": 1.0,
            "需人工复核": False,
            "未知类判定依据": "unresolved_product_or_incomplete_formula_without_valid_cas",
        })
        return cls.apply_unknown_auto_write_policy(suggestion)

    @classmethod
    def _unresolved_product_is_unknown_eligible(
        cls,
        search_result: dict[str, Any],
        name_result: dict[str, Any],
        classification: dict[str, Any],
        suggestion: dict[str, Any],
    ) -> bool:
        raw_cas = cls.normalize_cas(str(suggestion.get("CAS号") or ""))
        if cls._checksum_valid_cas(raw_cas):
            return False
        if classification.get("final_category") and not classification.get("need_manual_review", True):
            return False
        if classification.get("matched_rule_ids"):
            return False
        if any(search_result.get(key) for key in ("component_categories", "mixture_risk_categories", "sds_categories")):
            return False
        status = str(search_result.get("identity_status") or "unresolved").strip().lower()
        if status in {"verified", "cas_verified", "name_verified", "name_verified_pubchem", "verified_by_enrichment"}:
            return False
        is_incomplete_mixture = bool(search_result.get("is_mixture")) and not bool(search_result.get("composition_complete"))
        raw_name = str(suggestion.get("试剂名称") or name_result.get("raw_name") or "").strip()
        product_like = bool(re.search(
            r"(?:混合|配方|浆料|乳液|硅油|商品|型号|\b(?:lot|code)\b|"
            r"\b[A-Z]{2,}[A-Z0-9]*[-_][A-Z0-9-]*\d[A-Z0-9-]*\b|"
            r"\b[A-Z]{3,}(?:\s+[A-Z]{3,})+\b)",
            raw_name,
        ))
        # "unresolved" is the lookup outcome, not evidence that a named
        # chemical is a product with an unknown formula.  Treating it as a
        # product type turned ordinary network failures into ERP Unknown writes.
        return bool(is_incomplete_mixture or product_like)

    @staticmethod
    def _v1_clear_chemical_name(name: str, name_result: dict[str, Any]) -> bool:
        """Recognize an explicit chemical name for the approved no-evidence policy.

        A model-suggested replacement alone is not an explicit ERP name. Product
        codes, incomplete formulations and names marked invalid remain outside
        this policy.
        """
        compact = re.sub(r"\s+", "", str(name or "").strip())
        if not compact or len(compact) > 100 or unknown_reagent_name_reason(compact):
            return False
        if name_result.get("suspected_invalid_name"):
            return False
        if re.search(r"(?:配方|混合|商品|型号|偶联剂|试剂盒|[A-Z]{2,}[\-_]?\d{2,})", compact, re.I):
            return False
        # Require a chemical/material naming feature in the ERP name itself.
        # In particular, a free-form product title must not become Ordinary
        # merely because an LLM proposed a plausible standard name.
        return bool(re.search(
            r"(?:酸|醇|酚|酮|醚|酯|胺|铵|烷|烯|炔|苯|糖|盐|氧化|硫化|氯化|氟化|"
            r"溴化|碘化|水合物|多糖|蛋白|聚合物|橡胶|石墨烯|乙烯|丙烯|甲烷|乙烷)",
            compact,
        ))

    def v1_classify_without_web_evidence(
        self,
        reagent: dict[str, Any],
        name_result: dict[str, Any],
        search_result: dict[str, Any],
        classification: dict[str, Any],
        rule_engine: RuleEngine,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Use local rules, then the explicit-name Ordinary business fallback.

        No website failure text or LLM proposal is treated as hazard evidence.
        The result is V1-only and remains marked as a no-evidence business
        decision in the suggestion and eventual ERP-write audit.
        """
        if self._current_pipeline_version() != "v1" or self._search_has_trusted_web_evidence(search_result):
            return classification, search_result
        raw_name = str(reagent.get("试剂名称") or "").strip()
        if not self._v1_clear_chemical_name(raw_name, name_result):
            return classification, search_result
        if search_result.get("is_mixture") and not search_result.get("composition_complete"):
            return classification, search_result
        local = rule_engine.classify({
            "reagent_name": raw_name,
            "name": raw_name,
            "standard_name": str(name_result.get("standard_name") or raw_name),
            "cleaned_name": str(name_result.get("cleaned_name") or raw_name),
            "ordinary_evidence_complete": False,
            "allow_default_normal": False,
        })
        category = str(local.get("final_category") or "").strip()
        if category == UNKNOWN_CATEGORY:
            return classification, search_result
        if category and not local.get("need_manual_review"):
            selected = dict(local)
            selected["reason"] = (
                f"{local.get('reason') or ''} 网站资料不可用；依据已启用的本地名称规则判定。"
            ).strip()
            policy = "local_name_rule"
        elif category:
            return classification, search_result
        else:
            selected = {
                "final_category": NORMAL_CATEGORY,
                "matched_categories": [NORMAL_CATEGORY],
                "matched_rule_ids": ["BUS-NOR-CLEAR-NAME-NO-EVIDENCE-001"],
                "rule_version": rule_engine.rule_version,
                "reason": (
                    "ERP 原名具有明确化学命名特征，但未取得可用的本地危险性或网页证据；"
                    "按用户确认的无证据业务兜底归普通类。此结论不表示已证明无危险性。"
                ),
                "confidence": 0.8,
                "need_manual_review": False,
            }
            policy = "clear_name_without_evidence"
        selected["v1_no_web_evidence_policy"] = policy
        result = dict(search_result)
        result["identity_status"] = "v1_local_clear_name"
        result["identity_decision_basis"] = policy
        result["v1_no_web_evidence_policy"] = policy
        result["need_manual_review"] = False
        return selected, result

    @staticmethod
    def _checksum_valid_cas(value: str) -> bool:
        candidate = re.sub(r"[‐‑‒–—−]", "-", str(value or "").strip())
        if not re.fullmatch(r"\d{2,7}-\d{2}-\d", candidate):
            return False
        digits = candidate.replace("-", "")
        checksum = sum(int(digit) * index for index, digit in enumerate(reversed(digits[:-1]), start=1)) % 10
        return checksum == int(digits[-1])

    def enrichment_metrics(self) -> EnrichmentMetrics:
        metrics = getattr(self, "_enrichment_metrics", None)
        if metrics is None:
            root_dir = Path(getattr(self, "root_dir", Path(__file__).resolve().parents[1]))
            metrics = EnrichmentMetrics.from_settings(getattr(self, "settings", {}), root_dir)
            self._enrichment_metrics = metrics
        return metrics






    @staticmethod

    def run_debug_capture(self) -> None:
        self.run_after_login_capture(
            screenshot_name="home.png",
            html_name="home.html",
            after_login=None,
        )

    def run_reagent_judgement_capture(self) -> None:
        self.run_after_login_capture(
            screenshot_name="reagent_judgement.png",
            html_name="reagent_judgement.html",
            after_login=self.enter_reagent_judgement_page,
        )

    def run_todo_tasks_export(self) -> None:
        self.run_after_login_capture(
            screenshot_name="reagent_judgement.png",
            html_name="reagent_judgement.html",
            after_login=self.export_todo_tasks,
        )

    def run_first_task_detail_capture(self) -> None:
        self.run_after_login_capture(
            screenshot_name="task_detail.png",
            html_name="task_detail.html",
            after_login=self.open_first_task_detail,
        )

    def run_auto_match_capture(self) -> None:
        self.run_after_login_capture(
            screenshot_name="after_auto_match.png",
            html_name="after_auto_match.html",
            after_login=self.perform_auto_match,
        )

    def run_current_page_reagents_export(self) -> None:
        self.run_after_login_capture(
            screenshot_name="task_detail.png",
            html_name="task_detail.html",
            after_login=self.export_current_page_reagents,
        )

    def run_unmatched_reagents_export(self) -> None:
        self.run_after_login_capture(
            screenshot_name="task_detail.png",
            html_name="task_detail.html",
            after_login=self.sort_and_export_unmatched_reagents,
        )

    def run_single_fill_test(self) -> None:
        self.run_after_login_capture(
            screenshot_name="dropdown_options.png",
            html_name="dropdown_options.html",
            after_login=self.inspect_first_unmatched_property_options,
        )

    def run_semi_auto_approval_suggestions(self) -> None:
        if self.selected_todo_list_numbers():
            after_login = self.generate_selected_todo_approval_suggestions
        elif self.process_all_todos_enabled():
            after_login = self.generate_all_todo_approval_suggestions
        else:
            after_login = self.generate_approval_suggestions
        def after_login_with_discovery(page: Page) -> None:
            self.ensure_erp_api_discovery_recorder(page)
            try:
                after_login(page)
            finally:
                self.finalize_erp_api_discovery()

        self.run_after_login_capture(
            screenshot_name="after_auto_match.png",
            html_name="after_auto_match.html",
            after_login=after_login_with_discovery,
        )

    def run_erp_api_canary(self) -> None:
        """Perform one real placeholder-to-category API write with dual read-back."""
        list_number = str(os.getenv("ERP_API_CANARY_LIST_NUMBER") or getattr(self, "target_list_number", "") or "").strip()
        sequence = str(os.getenv("ERP_API_CANARY_SEQUENCE") or "").strip()
        expected_category = str(os.getenv("ERP_API_CANARY_CATEGORY") or "").strip()
        if not list_number or not sequence or not expected_category:
            raise RuntimeError("API canary requires list number, sequence, and existing ERP category.")
        erp_api = (getattr(self, "settings", {}) or {}).get("erp_api") or {}
        discovery = erp_api.get("discovery") or {}
        if str(discovery.get("status") or "") != "pending_canary":
            raise RuntimeError("ERP API canary is only allowed while discovery.status=pending_canary.")
        if erp_api.get("enabled_for_write") is True:
            raise RuntimeError("ERP API writes are already enabled; canary is not applicable.")

        def after_login(page: Page) -> None:
            client = ErpApiClient(page, self.settings)
            status = client.configuration_status()
            if not status.get("configured"):
                raise RuntimeError(f"ERP API canary configuration is incomplete: {status.get('missing')}")
            records = client.fetch_reagent_detail(list_number)
            matches = [record for record in records if str(record.get("序号") or "").strip() == sequence]
            if len(matches) != 1:
                raise RuntimeError(f"ERP API canary expected one sequence {sequence}, found {len(matches)}.")
            record = matches[0]
            current_category = str(record.get("物化特性") or "").strip()
            if current_category not in {"", "-"}:
                raise RuntimeError(
                    f"ERP API canary requires an unclassified row; sequence {sequence} currently shows "
                    f"{current_category}. No write was attempted."
                )
            identity = {
                "清单号": list_number,
                "试剂清单号": list_number,
                "序号": sequence,
                "试剂名称": record.get("试剂名称") or record.get("name"),
                "CAS号": record.get("CAS号") or record.get("cas_code"),
                "最终建议类别": expected_category,
            }
            record_id = client.record_id_from_record(record)
            result = client.save_physicochemical_property_by_id(
                record_id,
                expected_category,
                identity,
                record,
                allow_canary=True,
            )
            print(
                f"[api_canary] list={list_number} sequence={sequence} record_id={record_id} "
                f"saved={result.saved} api_verified={result.verified} detail={result.detail}"
            )
            if not result.saved or not result.verified:
                raise RuntimeError(f"ERP API canary failed API read-back: {result.detail}")
            if not self.open_task_detail_by_list_number(page, list_number):
                raise RuntimeError("ERP API canary could not open the target list for webpage verification.")
            page_value = self.read_reagent_property_across_pages(page, sequence)
            writer = ApprovalWriter(settings=self.settings)
            if not self.property_value_matches(page_value, expected_category, writer):
                raise RuntimeError(
                    f"ERP API canary webpage read-back mismatch: expected {expected_category}, "
                    f"got {page_value or '<empty>'}."
                )
            ErpApiConfigurator(self.root_dir).activate()
            print(
                f"[api_canary] PASSED list={list_number} sequence={sequence} transition=-to-{expected_category}; "
                "API and webpage read-back agree, ERP API writes are now enabled."
            )

        self.run_after_login_capture(
            screenshot_name="erp_api_canary.png",
            html_name="erp_api_canary.html",
            after_login=after_login,
        )

    def read_reagent_property_across_pages(self, page: Page, sequence: str) -> str:
        if not self.goto_first_reagent_page(page):
            return ""
        visited: set[str] = set()
        for _ in range(250):
            current_page = self.current_reagent_page_number(page) or str(len(visited) + 1)
            if current_page in visited:
                return ""
            visited.add(current_page)
            value = self.read_reagent_property_by_sequence(page, sequence)
            if value:
                print(f"[api_canary_web_verify] sequence={sequence} page={current_page} value={value}")
                return value
            moved, terminal = self.click_next_reagent_page(page)
            if not moved:
                if not terminal:
                    print(f"[api_canary_web_verify] pagination failed after page {current_page}")
                return ""
        return ""

    def erp_api_discovery_settings(self) -> dict[str, Any]:
        return ((getattr(self, "settings", {}).get("erp_api") or {}).get("discovery") or {})

    def ensure_erp_api_discovery_recorder(self, page: Page) -> ApiDiscoveryRecorder | None:
        discovery = self.erp_api_discovery_settings()
        status = str(discovery.get("status") or "pending_capture")
        if discovery.get("enabled") is not True or status in {"pending_canary", "verified", "active", "rejected"}:
            return None
        recorder = getattr(self, "_erp_api_discovery_recorder", None)
        if recorder is not None:
            return recorder
        configurator = ErpApiConfigurator(self.root_dir)
        recorder = ApiDiscoveryRecorder(self._log_dir(), max_events=1000)
        recorder.events.extend(configurator.load_events()[-500:])
        recorder.attach(page)
        self._erp_api_discovery_recorder = recorder
        self._erp_api_configurator = configurator
        print(
            "ERP API auto-discovery is listening to normal approval traffic; "
            "credentials and sensitive headers will be redacted."
        )
        return recorder

    def finalize_erp_api_discovery(self) -> None:
        recorder = getattr(self, "_erp_api_discovery_recorder", None)
        configurator = getattr(self, "_erp_api_configurator", None)
        if recorder is None or configurator is None:
            return
        discovery = self.erp_api_discovery_settings()
        try:
            required = max(2, int(discovery.get("required_write_samples") or 2))
        except (TypeError, ValueError):
            required = 2
        candidate = ApiDiscoveryAnalyzer().analyze(recorder.events, required_write_samples=required)
        path = configurator.save_candidate(candidate, recorder.events[-1000:])
        print(
            f"ERP API discovery candidate: status={candidate.status} confidence={candidate.confidence:.2f} "
            f"missing={candidate.missing or '-'} artifact={path}"
        )
        try:
            min_confidence = float(discovery.get("min_confidence") or 0.9)
        except (TypeError, ValueError):
            min_confidence = 0.9
        if candidate.status == "candidate" and candidate.confidence >= min_confidence:
            if configurator.promote_pending(candidate):
                print("ERP API candidate was written atomically; API writes remain disabled until next-run canary.")

    def selected_todo_list_numbers(self) -> list[str]:
        configured = getattr(self, "target_list_numbers", None)
        if configured:
            return [str(item).strip() for item in configured if str(item).strip()]
        value = os.getenv("TARGET_LIST_NUMBERS", "")
        result = []
        for part in str(value or "").replace("\n", ",").replace(";", ",").split(","):
            item = part.strip()
            if item and item not in result:
                result.append(item)
        return result

    def process_all_todos_enabled(self) -> bool:
        value = os.getenv("PROCESS_ALL_TODOS", "")
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    def generate_approval_suggestions(self, page: Page) -> None:
        stage_logger = getattr(self, "stage_logger", None) or StageLogger()
        self.stage_logger = stage_logger
        self.save_results = []
        self.web_write_failures = []
        self.auto_match_succeeded = False
        self.pagination_check_succeeded = False
        with stage_logger.stage("perform_auto_match"):
            if not self.perform_auto_match(page):
                print("Semi-auto approval suggestions stopped because no detail page or auto-match result is available.")
                return
        with stage_logger.stage("wait_reagent_table_ready"):
            self.wait_for_reagent_table_ready(page)
        with stage_logger.stage("read_detail_info"):
            self._current_detail_info = self.read_detail_info(page)
        self.clear_manual_review_items_for_list(self._current_detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7", ""))

        with stage_logger.stage("sort_property_column"):
            sort_succeeded = self.sort_property_column_until_unmatched_visible(page)
        if not sort_succeeded:
            print("Sorting did not bring '-' into the first rows within 4 clicks; reading current page '-' rows anyway.")

        rule_engine = RuleEngine.from_settings(self.settings, self.root_dir)
        rule_maintainer = RuleMaintainer.from_settings(self.settings, self.root_dir)
        seen_search_urls: dict[str, str] = {}

        if self.approval_write_mode() == "multi_page":
            suggestions = self.process_unmatched_reagent_pages(
                page,
                rule_engine,
                rule_maintainer,
                seen_search_urls,
            )
        else:
            suggestions = self.process_current_unmatched_reagent_page(
                page,
                rule_engine,
                rule_maintainer,
                seen_search_urls,
            )
            with stage_logger.stage("apply_approval_write_mode"):
                self.apply_approval_write_mode(page, suggestions)

        with stage_logger.stage("write_approval_suggestions"):
            saved_paths = self.save_approval_suggestions_outputs(suggestions)
            failure_path = self.write_web_write_failures()
            if failure_path is not None:
                saved_paths.append(failure_path)
        print(f"Saved approval suggestions: {saved_paths[0] if saved_paths else '-'}")
        self.record_save_result(
            "local_approval_suggestions",
            True,
            ", ".join(str(path) for path in saved_paths) if saved_paths else "-",
        )

        with stage_logger.stage("try_auto_pass_current_task"):
            self.try_auto_pass_current_task(page)

    def generate_all_todo_approval_suggestions(self, page: Page) -> None:
        original_target = getattr(self, "target_list_number", "")
        processed_list_numbers: set[str] = set()
        max_todos = self.max_process_all_todos_count()

        try:
            while True:
                if len(processed_list_numbers) >= max_todos:
                    break

                self.enter_reagent_judgement_page(page)
                tasks = self.read_all_todo_tasks(page)
                all_list_numbers = self.todo_list_numbers(tasks)
                list_numbers = self.filter_scheduled_todo_list_numbers(all_list_numbers)
                print(f"Todo list refresh: {len(all_list_numbers)} total task(s) across all visible todo pages.")
                if len(list_numbers) != len(all_list_numbers):
                    print(f"Scheduled review filter kept {len(list_numbers)} task(s) for automatic approval.")

                list_number = self.next_unprocessed_list_number(
                    [{"试剂清单号": item} for item in list_numbers],
                    processed_list_numbers,
                )
                if not list_number:
                    break

                print(f"Processing todo detail {len(processed_list_numbers) + 1}: {list_number}")
                self.target_list_number = list_number
                try:
                    self.generate_approval_suggestions(page)
                finally:
                    processed_list_numbers.add(list_number)

            if len(processed_list_numbers) >= max_todos:
                print(f"Stopped all-todo processing after PROCESS_ALL_TODOS_MAX={max_todos}.")
            elif not processed_list_numbers:
                print("No unprocessed todo task remains across todo pages.")
        finally:
            self.target_list_number = original_target

    def generate_selected_todo_approval_suggestions(self, page: Page) -> None:
        original_target = getattr(self, "target_list_number", "")
        selected = self.selected_todo_list_numbers()
        max_todos = self.max_process_all_todos_count()
        processed_count = 0

        try:
            print(f"Selected todo list number(s): {', '.join(selected)}")

            for list_number in selected:
                if processed_count >= max_todos:
                    print(f"Stopped selected-todo processing after PROCESS_ALL_TODOS_MAX={max_todos}.")
                    break

                normalized_list_number = self.extract_list_number(list_number)
                self.enter_reagent_judgement_page(page)
                current_todos = self.todo_list_numbers(self.read_all_todo_tasks(page))
                if normalized_list_number not in current_todos:
                    print(
                        f"Selected todo skipped because it is no longer in the current ERP todo list: {list_number}"
                    )
                    if current_todos:
                        print("Current ERP todo list numbers:")
                        for current in current_todos:
                            print(f"- {current}")
                    continue

                processed_count += 1
                print(f"Processing selected todo detail {processed_count}: {normalized_list_number}")
                self.target_list_number = normalized_list_number
                self.generate_approval_suggestions(page)
                if processed_count < min(len(selected), max_todos):
                    try:
                        self.enter_reagent_judgement_page(page)
                    except Exception as error:
                        print(f"Could not return to reagent judgement list after {list_number}: {error}")
                        break
        finally:
            self.target_list_number = original_target

    def todo_list_numbers(self, tasks: list[dict[str, str]]) -> list[str]:
        list_key = "\u8bd5\u5242\u6e05\u5355\u53f7"
        return [
            list_number
            for list_number in (self.extract_list_number(task.get(list_key, "")) for task in tasks)
            if list_number
        ]

    def next_unprocessed_list_number(self, tasks: list[dict[str, str]], processed: set[str]) -> str:
        for list_number in self.todo_list_numbers(tasks):
            if list_number not in processed:
                return list_number
        return ""

    def max_process_all_todos_count(self) -> int:
        value = os.getenv("PROCESS_ALL_TODOS_MAX", "50")
        try:
            max_count = int(value)
        except ValueError:
            print(f"Invalid PROCESS_ALL_TODOS_MAX={value}; using 50.")
            return 50
        return max(1, max_count)

    def filter_scheduled_todo_list_numbers(self, list_numbers: list[str]) -> list[str]:
        if not self.scheduled_manual_review_skip_enabled():
            return list_numbers

        filtered = []
        skipped = []
        for list_number in list_numbers:
            has_manual_review, reason = self.current_list_has_manual_review_item(list_number)
            if has_manual_review:
                skipped.append(list_number)
                print(
                    "Scheduled approval skipped list with pending manual review: "
                    f"{list_number}. {reason}"
                )
                continue
            filtered.append(list_number)

        if skipped:
            print(f"Scheduled approval skipped {len(skipped)} manual-review blocked list(s): {', '.join(skipped)}")
        return filtered

    def scheduled_manual_review_skip_enabled(self) -> bool:
        scheduled = os.getenv("SCHEDULED_RUN", "").strip().lower() in {"1", "true", "yes", "y", "on"}
        skip = os.getenv("SCHEDULED_SKIP_MANUAL_REVIEW_LISTS", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        return scheduled and skip

    def process_unmatched_reagent_pages(
        self,
        page: Page,
        rule_engine: RuleEngine,
        rule_maintainer: RuleMaintainer,
        seen_search_urls: dict[str, str],
    ) -> list[dict[str, Any]]:
        if not self.goto_first_reagent_page(page):
            print("Could not move to first reagent page; multi-page mode will continue from current page.")

        all_suggestions: list[dict[str, Any]] = []
        all_suggestion_keys: set[str] = set()
        write_state = MultiPageWriteState(max_attempts=self.max_reagent_write_attempts())
        visited_steps = 0
        self.sort_property_column_until_unmatched_visible(page)

        while True:
            visited_steps += 1

            current_page = self.current_reagent_page_number(page) or str(visited_steps)
            current_unmatched = self.current_page_unmatched_reagents(page)
            unhandled_unmatched = [
                reagent for reagent in write_state.unhandled_unmatched(current_unmatched, self.reagent_work_key)
            ]
            if current_unmatched and not unhandled_unmatched:
                print(
                    f"Reagent page {current_page} still has {len(current_unmatched)} '-' row(s), "
                    "but all were already processed, queued, or reached the write retry limit; moving to the next page."
                )
                moved_next, terminal_or_error = self.click_next_reagent_page(page)
                if not moved_next:
                    if terminal_or_error:
                        if write_state.pending_write_suggestions:
                            self.record_not_found_pending_write_failures(
                                write_state.pending_write_suggestions,
                                write_state.not_found_after_reread_keys,
                            )
                            print(
                                f"Multi-page mode reached the last reagent page with "
                                f"{len(write_state.pending_write_suggestions)} pending write candidate(s) not found after re-read."
                            )
                        else:
                            print("Multi-page mode reached the last reagent page.")
                    else:
                        print("Multi-page mode stopped because next-page navigation could not be verified.")
                    break
                if not self.prepare_next_reagent_page_for_light_scan(page, "processed '-' rows"):
                    break
                continue

            if not current_unmatched:
                if write_state.pending_write_suggestions:
                    print(
                        f"Current sorted reagent page has no '-' rows, but "
                        f"{len(write_state.pending_write_suggestions)} pending write candidate(s) still lack a terminal status; "
                        "scanning the next reagent page."
                    )
                else:
                    print("Current sorted reagent page has no '-' rows; scanning the next reagent page before completion.")
                moved_next, terminal_or_error = self.click_next_reagent_page(page)
                if not moved_next:
                    if terminal_or_error:
                        if write_state.pending_write_suggestions:
                            self.record_not_found_pending_write_failures(
                                write_state.pending_write_suggestions,
                                write_state.not_found_after_reread_keys,
                            )
                            print(
                                f"Multi-page mode reached the last reagent page with "
                                f"{len(write_state.pending_write_suggestions)} pending write candidate(s) not found after re-read."
                            )
                        else:
                            print("Multi-page mode reached the last reagent page; no '-' rows remain.")
                    else:
                        print("Multi-page mode stopped because next-page navigation could not be verified.")
                    break
                if not self.prepare_next_reagent_page_for_light_scan(page, "no '-' rows on current page"):
                    break
                continue

            page_suggestions = self.process_current_unmatched_reagent_page(
                page,
                rule_engine,
                rule_maintainer,
                seen_search_urls,
                page_label=current_page,
                skip_reagent_keys=write_state.handled_keys,
            )
            for suggestion in page_suggestions:
                key = self.suggestion_work_key(suggestion)
                if key not in all_suggestion_keys:
                    all_suggestions.append(suggestion)
                    all_suggestion_keys.add(key)
            if page_suggestions:
                self.write_partial_approval_suggestions(all_suggestions)
                write_state.register_writable_suggestions(
                    self.high_confidence_write_candidates(page_suggestions),
                    self.suggestion_work_key,
                )

            if page_suggestions:
                with self.stage_logger.stage("apply_approval_write_mode", f"page {current_page}"):
                    raw_write_result = self.apply_approval_write_mode(page, page_suggestions)
                    write_result = normalize_write_result(raw_write_result, page_suggestions, self.suggestion_work_key)
                state_delta = write_state.apply_write_result(write_result)
                failed_keys = state_delta["failed"]
                for key in state_delta["retry_limited"]:
                    print(f"Write retry limit reached for reagent key: {key}")
                for key in state_delta["deferred"]:
                    print(f"Deferred pending write candidate until a later page/read: {key}")

                if failed_keys:
                    print(
                        "Multi-page mode will re-read the current reagent page after write failure "
                        "before moving to the next page."
                    )
                    if not self.stabilize_reagent_detail_after_write_failure(page):
                        print(
                            "Multi-page mode stopped because the reagent detail page could not be "
                            "stabilized after a write failure."
                        )
                        break
                    try:
                        self.sort_property_column_until_unmatched_visible(page)
                    except Exception as error:
                        print(
                            "Multi-page mode stopped because the physicochemical property header "
                            f"was not available after write recovery: {error}"
                        )
                        break
                    continue
                if self.approval_write_mode() == "multi_page" and write_result.get("attempted"):
                    print(
                        "Multi-page mode completed a serial write batch; "
                        "re-sorting and re-reading the current page."
                    )
                    try:
                        self.sort_property_column_until_unmatched_visible(page)
                    except Exception as error:
                        print(
                            "Multi-page mode stopped because sorting after a successful save failed: "
                            f"{error}"
                        )
                        break
                    continue
            else:
                write_result = {"attempted": set(), "handled": set(), "failed": set()}

            if self.approval_write_mode() in {"disabled", "test_one"}:
                write_state.mark_reagents_handled(current_unmatched, self.reagent_work_key)

            moved_next, terminal_or_error = self.click_next_reagent_page(page)
            if not moved_next:
                if terminal_or_error:
                    print("Multi-page mode reached the last reagent page.")
                else:
                    print("Multi-page mode stopped because next-page navigation could not be verified.")
                break
            if not self.prepare_next_reagent_page_for_light_scan(page, "normal next-page navigation"):
                break

            if visited_steps >= 200:
                raise RuntimeError("Stopped multi-page approval after 200 pages; page navigation may be stuck.")

        return all_suggestions

    def prepare_next_reagent_page_for_light_scan(self, page: Page, reason: str) -> bool:
        try:
            self.wait_for_reagent_table_ready(page)
            wait_until_spinner_hidden(page, timeout_ms=5000)
            page.wait_for_timeout(200)
        except Exception as error:
            print(f"Multi-page mode stopped because the next reagent page was not ready after {reason}: {error}")
            return False
        print(
            "Moved to the next reagent page for completion scanning; "
            "skipping property-header re-sort and reading visible rows directly."
        )
        return True

    def record_not_found_pending_write_failures(
        self,
        pending_write_suggestions: dict[str, dict[str, Any]],
        recorded_keys: set[str],
    ) -> None:
        for key, suggestion in list(pending_write_suggestions.items()):
            if key in recorded_keys:
                continue
            self.record_web_write_failure(
                suggestion,
                str(suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b") or ""),
                "not_found_after_reread",
            )
            recorded_keys.add(key)

    def write_partial_approval_suggestions(self, suggestions: list[dict[str, Any]]) -> None:
        if not suggestions:
            return
        export_rows = self.suggestions_with_current_list_number(suggestions)
        columns = self.approval_suggestion_export_columns()
        output_path = self._log_dir() / "approval_suggestions_partial.xlsx"
        output_path = self.write_excel_with_fallback(
            pd.DataFrame(export_rows, columns=columns),
            output_path,
        )
        print(f"Saved partial approval suggestions: {output_path}")

    def save_approval_suggestions_outputs(self, suggestions: list[dict[str, Any]]) -> list[Any]:
        export_rows = self.suggestions_with_current_list_number(suggestions)
        columns = self.approval_suggestion_export_columns()
        dataframe = pd.DataFrame(export_rows, columns=columns)
        log_dir = self._log_dir()
        saved_paths: list[Any] = []

        latest_path = self.write_excel_with_fallback(dataframe, log_dir / "approval_suggestions.xlsx")
        saved_paths.append(latest_path)

        list_number = self.current_detail_list_number()
        if list_number:
            list_path = self.write_excel_with_fallback(
                dataframe,
                log_dir / f"approval_suggestions_{self.safe_filename_part(list_number)}.xlsx",
            )
            saved_paths.append(list_path)

        aggregate_path = self.write_aggregate_approval_suggestions(dataframe, list_number)
        if aggregate_path is not None:
            saved_paths.append(aggregate_path)
        return saved_paths

    def suggestions_with_current_list_number(self, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        list_number = self.current_detail_list_number()
        return [{"试剂清单号": list_number, **suggestion} for suggestion in suggestions]

    def approval_suggestion_export_columns(self) -> list[str]:
        return ["试剂清单号", *self.approval_suggestion_columns()]

    def current_detail_list_number(self) -> str:
        detail_info = getattr(self, "_current_detail_info", {}) or {}
        return str(detail_info.get("当前清单号") or detail_info.get("试剂清单号") or "").strip()

    def write_aggregate_approval_suggestions(self, dataframe: pd.DataFrame, list_number: str) -> Any:
        output_path = self._log_dir() / "approval_suggestions_all.xlsx"
        try:
            existing = pd.read_excel(output_path) if output_path.exists() else pd.DataFrame(columns=dataframe.columns)
            if list_number and "试剂清单号" in existing.columns:
                existing = existing[existing["试剂清单号"].astype(str) != str(list_number)]
            combined = pd.concat([existing, dataframe], ignore_index=True)
            return self.write_excel_with_fallback(
                combined.reindex(columns=self.approval_suggestion_export_columns()),
                output_path,
            )
        except Exception as error:
            print(f"Could not update aggregate approval suggestions: {error}")
            return None

    @staticmethod
    def safe_filename_part(value: str) -> str:
        safe = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in {"-", "_"})
        return safe or "unknown"

    def max_reagent_write_attempts(self) -> int:
        approval_settings = getattr(self, "settings", {}).get("approval", {}) or {}
        value = os.getenv("APPROVAL_WRITE_MAX_ATTEMPTS") or approval_settings.get("write_max_attempts", 2)
        try:
            attempts = int(value)
        except (TypeError, ValueError):
            return 2
        return max(1, min(5, attempts))

    def process_current_unmatched_reagent_page(
        self,
        page: Page,
        rule_engine: RuleEngine,
        rule_maintainer: RuleMaintainer,
        seen_search_urls: dict[str, str],
        page_label: str = "",
        skip_reagent_keys: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        stage_logger = getattr(self, "stage_logger", None) or StageLogger()
        self.enrichment_metrics().record(
            "batch_start",
            stage="process_current_unmatched_reagent_page",
            page=str(page_label or ""),
        )
        property_key = "\u7269\u5316\u7279\u6027"
        name_key = "\u8bd5\u5242\u540d\u79f0"
        cas_key = "CAS\u53f7"
        skip_reagent_keys = skip_reagent_keys or set()

        with stage_logger.stage("read_current_page_unmatched", f"page {page_label}".strip()):
            unmatched_reagents = [
                minimize_reagent_record(record)
                for record in self.read_current_page_reagents(page)
                if record.get(property_key, "").strip() == "-"
            ]
            skipped_count = sum(1 for record in unmatched_reagents if self.reagent_work_key(record) in skip_reagent_keys)
            unmatched_reagents = [
                record
                for record in unmatched_reagents
                if self.reagent_work_key(record) not in skip_reagent_keys
            ]

        page_text = f" page {page_label}" if page_label else ""
        if skipped_count:
            print(f"Skipped {skipped_count} already processed current-page '-' reagent row(s).")
        print(f"Found {len(unmatched_reagents)} current-page{page_text} reagent row(s) with physicochemical property '-'.")

        suggestions_by_index: dict[int, dict[str, Any]] = {}
        pending_reagents: list[dict[str, Any]] = []
        memory = ReagentMemory.from_settings(self.settings, self.root_dir)
        normalizer = NameNormalizer(settings=self.settings, root_dir=self.root_dir)
        non_reagent_classifier = NonReagentClassifier(settings=self.settings, root_dir=self.root_dir)
        for index, reagent in enumerate(unmatched_reagents, start=1):
            reagent_name = reagent.get(name_key, "").strip()
            cas = reagent.get(cas_key, "").strip()
            progress = f"{index}/{len(unmatched_reagents)}"
            if page_label:
                progress = f"page {page_label} {progress}"
            stage_logger.event(f"Processing reagent {progress}: {reagent_name} / {cas}")

            unknown_reason = unknown_reagent_name_reason(
                reagent_name,
                reagent.get("\u89c4\u683c", ""),
                reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
            )
            if unknown_reason:
                suggestion = self.unknown_reagent_suggestion(reagent, unknown_reason)
                suggestions_by_index[index] = suggestion
                self.remember_erp_suggestion(memory, suggestion)
                print(
                    "Direct unknown reagent rule suggestion: "
                    f"{reagent.get('\u5e8f\u53f7', '')} {reagent_name} -> {UNKNOWN_CATEGORY}"
                )
                continue

            non_reagent_classification = non_reagent_classifier.classify(
                reagent_name,
                reagent.get("\u89c4\u683c", ""),
                reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
            )
            if non_reagent_classification:
                reason = str(non_reagent_classification.get("reason") or "").strip()
                suggestion = self._direct_business_suggestion(
                    reagent,
                    reagent_name,
                    non_reagent_classification,
                    reason,
                )
                suggestions_by_index[index] = suggestion
                self.remember_erp_suggestion(memory, suggestion)
                print(
                    "Direct non-reagent item suggestion: "
                    f"{reagent.get('\u5e8f\u53f7', '')} {reagent_name} -> {non_reagent_classification.get('final_category', '')}"
                )
                continue

            memory_match = self.lookup_reusable_memory(
                memory,
                cas=cas,
                raw_name=reagent_name,
            )
            if memory_match:
                if not self.memory_match_is_safe(reagent, memory_match, rule_engine=rule_engine):
                    self.disable_unsafe_memory_match(memory, memory_match, reagent)
                else:
                    suggestion = self.reagent_memory_suggestion(reagent, memory_match)
                    suggestion = self.enrich_flat_manual_suggestion_with_llm_advice(
                        reagent, suggestion, rule_engine, name_result={}
                    )
                    suggestions_by_index[index] = suggestion
                    self.queue_manual_review_if_suggestion_requires_it(
                        reagent,
                        suggestion,
                        name_result={},
                    )
                    print(
                        "Reagent memory suggestion: "
                        f"{reagent.get('\u5e8f\u53f7', '')} {reagent_name} -> {memory_match.get('final_category', '')}"
                    )
                    continue

            direct_suggestion = self.direct_business_rule_suggestion(reagent, rule_engine)
            if direct_suggestion:
                direct_suggestion = self.enrich_flat_manual_suggestion_with_llm_advice(
                    reagent, direct_suggestion, rule_engine, name_result={}
                )
                suggestions_by_index[index] = direct_suggestion
                if direct_suggestion.get("需人工复核"):
                    self.queue_manual_review_if_suggestion_requires_it(reagent, direct_suggestion, name_result={})
                else:
                    self.remember_erp_suggestion(memory, direct_suggestion)
                sequence = reagent.get("\u5e8f\u53f7", "")
                category = direct_suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b", "")
                print(
                    "Direct business rule suggestion: "
                    f"{sequence} {reagent_name} -> {category}"
                )
                continue

            try:
                name_result = normalizer.normalize(
                    reagent_name,
                    cas=cas,
                )
            except Exception as error:
                name_result = {
                    "raw_name": reagent_name,
                    "cleaned_name": reagent_name,
                    "standard_name": reagent_name,
                    "cas": cas,
                    "confidence": 0.0,
                    "need_manual_review": True,
                    "reason": f"name normalization failed before memory lookup: {error}",
                }

            memory_match = self.lookup_reusable_memory(
                memory,
                cas=name_result.get("cas") or cas,
                standard_name=name_result.get("standard_name", ""),
                cleaned_name=name_result.get("cleaned_name", ""),
                raw_name=reagent_name,
            )
            if memory_match:
                if not self.memory_match_is_safe(reagent, memory_match, name_result, rule_engine=rule_engine):
                    self.disable_unsafe_memory_match(memory, memory_match, reagent)
                else:
                    suggestion = self.reagent_memory_suggestion(reagent, memory_match, name_result)
                    suggestion = self.enrich_flat_manual_suggestion_with_llm_advice(
                        reagent, suggestion, rule_engine, name_result=name_result
                    )
                    suggestions_by_index[index] = suggestion
                    self.queue_manual_review_if_suggestion_requires_it(
                        reagent,
                        suggestion,
                        name_result=name_result,
                    )
                    print(
                        "Reagent memory suggestion after normalization: "
                        f"{reagent.get('\u5e8f\u53f7', '')} {reagent_name} -> {memory_match.get('final_category', '')}"
                    )
                    continue

            pending_reagents.append({"index": index, "progress": progress, "reagent": reagent, "name_result": name_result})

        if not pending_reagents:
            ordered_suggestions = [suggestions_by_index[index] for index in sorted(suggestions_by_index)]
            ordered_suggestions = self.enforce_duplicate_suggestion_consistency(ordered_suggestions)
            for suggestion in ordered_suggestions:
                self.remember_erp_suggestion(memory, suggestion)
            self.audit_approval_suggestions(ordered_suggestions, rule_engine)
            return ordered_suggestions

        with stage_logger.stage("chemical_search", f"parallel {len(pending_reagents)} reagent(s)"):
            search_results = self.search_reagents_parallel(pending_reagents)
        prepared_items: list[dict[str, Any]] = []
        for item in pending_reagents:
            reagent = item["reagent"]
            reagent_name = reagent.get(name_key, "").strip()
            cas = reagent.get(cas_key, "").strip()
            search_result = search_results.get(item["index"]) or self.search_failure_result(reagent, "parallel search did not return a result")
            name_result = search_result.get("name_normalization", {})
            self.mark_duplicate_search_url_if_needed(reagent, search_result, seen_search_urls)
            search_name = search_result.get("name") or name_result.get("standard_name") or name_result.get("cleaned_name") or reagent_name
            search_cas = search_result.get("cas") or name_result.get("cas") or cas
            prepared_items.append(
                {
                    **item,
                    "search_result": search_result,
                    "name_result": name_result,
                    "search_name": search_name,
                    "search_cas": search_cas,
                }
            )

        with stage_logger.stage("llm_extract", f"parallel {len(prepared_items)} reagent(s)"):
            extracted_results = self.extract_and_classify_parallel(prepared_items, rule_engine)
        with stage_logger.stage("rule_classify", f"parallel {len(prepared_items)} reagent(s)"):
            print(f"Rule classification completed for {len(prepared_items)} reagent(s).")
        begin_review_batch = getattr(self, "begin_manual_review_batch", None)
        flush_review_batch = getattr(self, "flush_manual_review_batch", None)
        if callable(begin_review_batch):
            begin_review_batch()
        try:
            for item in prepared_items:
                reagent = item["reagent"]
                reagent_name = reagent.get(name_key, "").strip()
                name_result = item["name_result"]
                search_result = item["search_result"]
                extracted, classification = extracted_results.get(item["index"]) or self.empty_extraction_and_classification(
                    reagent,
                    search_result,
                    rule_engine,
                    "parallel LLM/classification did not return a result",
                )
                try:
                    with stage_logger.stage("record_rule_candidate", reagent_name):
                        if rule_maintainer.record_candidate(reagent, name_result, search_result, extracted, classification):
                            print(f"Recorded pending rule candidate: {reagent_name}")
                except Exception as error:
                    print(f"Could not record rule candidate for {reagent_name}: {error}")
                suggestions_by_index[item["index"]] = self._approval_suggestion_row(
                    reagent,
                    name_result,
                    search_result,
                    extracted,
                    classification,
                )
                if suggestions_by_index[item["index"]].get("需人工复核"):
                    with stage_logger.stage("add_manual_review_item", reagent_name):
                        self.add_manual_review_item_from_suggestion(
                            reagent,
                            name_result,
                            search_result,
                            extracted,
                            classification,
                            suggestions_by_index[item["index"]],
                        )
        finally:
            if callable(flush_review_batch):
                flush_review_batch()

        ordered_suggestions = [suggestions_by_index[index] for index in sorted(suggestions_by_index)]
        ordered_suggestions = self.enforce_duplicate_suggestion_consistency(ordered_suggestions)
        for suggestion in ordered_suggestions:
            self.remember_erp_suggestion(memory, suggestion)
        self.audit_approval_suggestions(ordered_suggestions, rule_engine)
        return ordered_suggestions




    @staticmethod



    @staticmethod

    @staticmethod
    def apply_selected_property_evidence(extracted: dict[str, Any], search_result: dict[str, Any]) -> dict[str, Any]:
        """Override model extraction only with selected source-backed V1 evidence."""
        merged = dict(extracted or {})
        selected = search_result.get("selected_property_evidence") or {}
        if not isinstance(selected, dict):
            return merged
        evidence = list(merged.get("evidence") or [])
        for field_name, item in selected.items():
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "unknown").strip()
            value = str(item.get("value") or "").strip()
            if field_name in {"flash_point", "boiling_point", "toxicity"}:
                merged[field_name] = value if status == "measured" else ""
                merged[f"{field_name}_status"] = status
            elif field_name in {"corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal"}:
                merged[field_name] = True if status == "present" else False if status == "absent" else None
            if value:
                evidence.append(f"{field_name}: {value} ({item.get('source') or '-'})")
        selected_toxicity = search_result.get("selected_toxicity_evidence") or []
        if isinstance(selected_toxicity, list):
            toxicity_values = [
                str(item.get("value") or item.get("raw_value") or "").strip()
                for item in selected_toxicity if isinstance(item, dict)
                and str(item.get("status") or "measured") == "measured"
            ]
            toxicity_values = list(dict.fromkeys(value for value in toxicity_values if value))
            if toxicity_values:
                merged["toxicity"] = " | ".join(toxicity_values)
                merged["toxicity_status"] = "measured"
                for value in toxicity_values:
                    evidence.append(f"toxicity: {value} (分组最低值)")
        merged["evidence"] = list(dict.fromkeys(str(item) for item in evidence if str(item).strip()))
        return merged

    def audit_approval_suggestions(
        self,
        suggestions: list[dict[str, Any]],
        rule_engine: RuleEngine,
    ) -> None:
        audit = AuditLogger.from_settings(self.settings, self.root_dir)
        dry_run = self.dry_run_enabled()
        for suggestion in suggestions:
            item_text = json.dumps(
                {
                    "list_number": suggestion.get("\u8bd5\u5242\u6e05\u5355\u53f7") or self.current_detail_list_number(),
                    "sequence": suggestion.get("\u5e8f\u53f7", ""),
                    "reagent_name": suggestion.get("\u8bd5\u5242\u540d\u79f0", ""),
                    "cas": suggestion.get("CAS\u53f7", ""),
                },
                ensure_ascii=False,
            )
            audit.record_decision(
                item_text,
                {
                    "rule_version": rule_engine.rule_version or "unversioned",
                    "final_category": suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b", ""),
                    "matched_categories": suggestion.get("\u547d\u4e2d\u7c7b\u522b", ""),
                    "matched_rule_ids": suggestion.get("命中规则ID", ""),
                    "reason": suggestion.get("\u89c4\u5219\u539f\u56e0", ""),
                    "confidence": suggestion.get("\u7f6e\u4fe1\u5ea6", 0.0),
                    "need_manual_review": suggestion.get("\u9700\u4eba\u5de5\u590d\u6838", True),
                    "evidence": suggestion.get("\u8bc1\u636e", ""),
                    "source": suggestion.get("\u67e5\u8be2\u6765\u6e90", ""),
                    "source_url": suggestion.get("\u67e5\u8be2URL", ""),
                    "identity_status": suggestion.get("身份验证状态", ""),
                    "identity_decision_basis": suggestion.get("身份判定依据", ""),
                    "original_erp_cas": suggestion.get("原ERP CAS号", ""),
                    "corrected_cas": suggestion.get("修正CAS号", ""),
                },
                dry_run,
            )

    def enforce_duplicate_suggestion_consistency(self, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for suggestion in suggestions:
            key = self.duplicate_reagent_identity_key(suggestion)
            if key:
                groups.setdefault(key, []).append(suggestion)

        for key, group in groups.items():
            categories = {
                str(item.get("最终建议类别") or "").strip()
                for item in group
                if str(item.get("最终建议类别") or "").strip()
            }
            if len(group) < 2 or len(categories) <= 1:
                continue
            best = max(group, key=self._duplicate_consistency_score)
            best_category = str(best.get("最终建议类别") or "").strip()
            best_rule_category = str(best.get("规则判定类别") or best_category).strip()
            best_reason = str(best.get("规则原因") or "").strip()
            print(
                "Duplicate reagent suggestions had conflicting categories; "
                f"using the highest-priority category for identity {key}: {best_category}."
            )
            for item in group:
                if item is best:
                    continue
                original_category = str(item.get("最终建议类别") or "").strip()
                item["最终建议类别"] = best_category
                item["规则判定类别"] = best_rule_category
                item["命中类别"] = best.get("命中类别", best_category)
                item["规则原因"] = (
                    f"同一清单当前页中相同试剂出现不同判定，已统一采用最高风险优先级结果："
                    f"{original_category or '<空>'} -> {best_category}。"
                    f"{best_reason}"
                ).strip()
                item["置信度"] = best.get("置信度", item.get("置信度", 0.0))
                item["需人工复核"] = best.get("需人工复核", item.get("需人工复核", False))
                item["查询来源"] = best.get("查询来源", item.get("查询来源", ""))
                item["查询URL"] = best.get("查询URL", item.get("查询URL", ""))
                item["证据"] = best.get("证据", item.get("证据", ""))
        for suggestion in suggestions:
            self.apply_unknown_auto_write_policy(suggestion)
        return suggestions

    def duplicate_reagent_identity_key(self, suggestion: dict[str, Any]) -> str:
        cas = self.normalize_cas(str(suggestion.get("CAS号") or suggestion.get("原ERP CAS号") or ""))
        if cas.lower() in {"", "-", "无", "n/a", "na", "none"}:
            cas = ""
        names = [
            self._work_key_text(suggestion.get("标准化名称", "")),
            self._work_key_text(suggestion.get("清洗后名称", "")),
            self._work_key_text(suggestion.get("试剂名称", "")),
        ]
        name = next((item for item in names if item), "")
        if cas and name:
            return f"cas_name:{cas}|{name}"
        if name:
            return f"name:{name}"
        if cas:
            return f"cas:{cas}"
        return ""

    @classmethod
    def _duplicate_consistency_score(cls, suggestion: dict[str, Any]) -> tuple[int, float, int]:
        category = str(
            suggestion.get("规则判定类别")
            or suggestion.get("最终建议类别")
            or ""
        ).strip()
        priority = cls._category_risk_priority(category)
        confidence = cls._float_confidence(suggestion.get("置信度"))
        non_manual = 0 if cls._truthy(suggestion.get("需人工复核")) else 1
        return priority, confidence, non_manual

    @staticmethod
    def _category_risk_priority(category: str) -> int:
        normalized = str(category or "").strip()
        priorities = {
            "拒收类": 100,
            "不建议接收类": 100,
            "不建议接受类": 100,
            "剧毒品": 95,
            "易爆类": 90,
            "高毒类": 85,
            "氧化剂": 80,
            "强反应": 75,
            "强反应性": 75,
            "特殊酸": 70,
            "重金属类": 65,
            "易燃类": 60,
            "易燃液体": 60,
            "发烟类": 55,
            "溴碘类": 50,
            "刺激性": 40,
            "异味": 30,
            "常规酸": 20,
            "常规碱": 20,
            "未知类": 15,
            "普通类": 0,
        }
        return priorities.get(normalized, 10)

    @classmethod
    def lookup_reusable_memory(
        cls,
        memory: ReagentMemory,
        *,
        cas: str = "",
        standard_name: str = "",
        cleaned_name: str = "",
        raw_name: str = "",
    ) -> dict[str, Any] | None:
        # Name identity is authoritative.  CAS is only a fallback for an
        # unusable ERP name, never a conflict resolver for a usable one.
        name_unusable = bool(unknown_reagent_name_reason(raw_name, cleaned_name, standard_name))
        if not name_unusable:
            name_match = memory.lookup(
                standard_name=standard_name,
                cleaned_name=cleaned_name,
                raw_name=raw_name,
            )
            if name_match:
                return name_match

            # A plainly named aqueous solution may reuse a verified pure
            # component decision under the approved V1 business policy.  This
            # is intentionally narrow: only ``<single component>[strength]水溶液``
            # forms qualify, never formulations that name another solvent or
            # a second component.  The flag is retained on the returned row so
            # the approval detail can disclose that the category came from the
            # pure-component record.
            primary_component = cls.memory_solution_primary_component(
                raw_name, standard_name, cleaned_name
            )
            if primary_component:
                pure_match = memory.lookup(
                    raw_name=primary_component,
                    standard_name=primary_component,
                    cleaned_name=primary_component,
                )
                if pure_match and str(pure_match.get("source") or "").strip().lower() == "verified_erp_write":
                    matched = dict(pure_match)
                    matched["_memory_solution_reuse"] = True
                    matched["_memory_solution_primary_component"] = primary_component
                    return matched
            return None
        normalized_cas = cls.normalize_cas(cas)
        if normalized_cas and normalized_cas not in {"-", "无", "n/a", "na", "none"}:
            cas_match = memory.lookup(cas=normalized_cas)
            if cas_match:
                return cas_match
        return None

    @staticmethod
    def memory_solution_primary_component(*names: str) -> str:
        """Return the sole component of a clearly named aqueous solution.

        It deliberately does not parse formulations such as ``X乙醇分散液``
        or multi-component products.  Aqueous-solution reuse is a V1 business
        policy and must remain visible to the caller rather than silently
        broadening ordinary name matching.
        """
        for value in names:
            compact = re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", str(value or ""))
            match = re.fullmatch(
                r"(?P<component>.+?)(?:[（(]?(?:\d+(?:\.\d+)?%|\d+(?:\.\d+)?(?:mol/l|mol\s*/\s*l|m))[）)]?)?水溶(?:液)?",
                compact,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            component = str(match.group("component") or "").strip("-_（）() ")
            if not component or any(token in component for token in ("、", "+", "和", "与", "/", "乙醇", "甲醇", "丙酮")):
                continue
            return component
        return ""

    def queue_manual_review_if_suggestion_requires_it(
        self,
        reagent: dict[str, str],
        suggestion: dict[str, Any],
        name_result: dict[str, Any] | None = None,
    ) -> None:
        self.apply_unknown_auto_write_policy(suggestion)
        self.apply_terminal_reject_policy(suggestion)
        if not suggestion.get("\u9700\u4eba\u5de5\u590d\u6838"):
            return
        self.add_manual_review_item_from_suggestion(
            reagent,
            name_result or {
                "raw_name": reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
                "cleaned_name": suggestion.get("\u6e05\u6d17\u540e\u540d\u79f0", ""),
                "standard_name": suggestion.get("\u6807\u51c6\u5316\u540d\u79f0", ""),
                "reason": suggestion.get("\u540d\u79f0\u6807\u51c6\u5316\u539f\u56e0", ""),
            },
            {
                "source": suggestion.get("\u67e5\u8be2\u6765\u6e90", ""),
                "url": suggestion.get("\u67e5\u8be2URL", ""),
                "failure_reason": suggestion.get("\u67e5\u8be2\u5931\u8d25\u539f\u56e0", ""),
                "raw_text": suggestion.get("\u8bc1\u636e", ""),
                "llm_advisory_category": suggestion.get("LLM辅助建议类别", ""),
                "llm_advisory_summary_cn": suggestion.get("LLM辅助物性意见", ""),
                "llm_advisory_reason_cn": suggestion.get("LLM辅助判定理由", ""),
                "llm_advisory_rule_cn": suggestion.get("LLM辅助规则依据", ""),
                "llm_advisory_uncertainties_cn": suggestion.get("LLM辅助不确定项", ""),
                "llm_advisory_confidence": suggestion.get("LLM辅助置信度", ""),
                "llm_advisory_evidence_basis": suggestion.get("LLM辅助依据类型", ""),
                "llm_advisory_only": suggestion.get("LLM辅助意见仅供复核", False),
                "used_llm_manual_review_advice": suggestion.get("是否生成LLM人工复核意见", False),
                "llm_model": suggestion.get("LLM辅助模型", ""),
                "llm_rules_fingerprint": suggestion.get("LLM规则版本", ""),
            },
            {
                "evidence": [suggestion.get("\u8bc1\u636e", "")]
                if suggestion.get("\u8bc1\u636e")
                else [],
                "llm_advisory_only": suggestion.get("LLM辅助意见仅供复核", False),
            },
            {
                "final_category": suggestion.get("最终建议类别", ""),
                "reason": suggestion.get("\u89c4\u5219\u539f\u56e0", ""),
                "need_manual_review": True,
            },
            suggestion,
        )

    def enrich_flat_manual_suggestion_with_llm_advice(
        self,
        reagent: dict[str, Any],
        suggestion: dict[str, Any],
        rule_engine: RuleEngine,
        *,
        name_result: dict[str, Any],
    ) -> dict[str, Any]:
        if not suggestion.get("需人工复核") or not self.llm_manual_review_advice_enabled():
            return suggestion
        rule_summary = self._rule_summary_for_llm(rule_engine)
        rules_fingerprint = hashlib.sha256(
            json.dumps(rule_summary, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        extractor = LlmExtractor(settings=self.settings)
        if not hasattr(extractor, "generate_manual_review_advice"):
            return suggestion
        advice = extractor.generate_manual_review_advice(
            {
                "raw_name": reagent.get("试剂名称", ""),
                "standard_name": name_result.get("standard_name") or suggestion.get("标准化名称", ""),
                "cleaned_name": name_result.get("cleaned_name") or suggestion.get("清洗后名称", ""),
                "cas": reagent.get("CAS号", ""),
                "concentration": name_result.get("concentration", ""),
                "web_source": suggestion.get("查询来源", ""),
                "web_evidence": suggestion.get("证据", ""),
                "has_trusted_web_evidence": False,
                "current_rule_category": suggestion.get("最终建议类别", ""),
                "current_rule_reason": suggestion.get("规则原因", ""),
                "manual_review_reason": suggestion.get("规则原因") or suggestion.get("查询失败原因", ""),
                "rule_summary": rule_summary,
                "allowed_categories": list(erp_property_options(self.settings)),
                "rules_fingerprint": rules_fingerprint,
            }
        )
        enriched = dict(suggestion)
        enriched.update(
            {
                "LLM辅助建议类别": advice.get("candidate_category", ""),
                "LLM辅助物性意见": advice.get("physicochemical_summary_cn", ""),
                "LLM辅助判定理由": advice.get("reason_cn", ""),
                "LLM辅助规则依据": advice.get("matched_rule_summary_cn", ""),
                "LLM辅助不确定项": "；".join(advice.get("uncertainties_cn", []) or []),
                "LLM辅助置信度": advice.get("advisory_confidence", ""),
                "LLM辅助依据类型": advice.get("evidence_basis", ""),
                "LLM辅助意见仅供复核": True,
                "是否生成LLM人工复核意见": advice.get("used_llm", False),
                "LLM辅助模型": advice.get("model", ""),
                "LLM规则版本": advice.get("rules_fingerprint", ""),
            }
        )
        return enriched

    def unknown_reagent_suggestion(self, reagent: dict[str, Any], reason: str) -> dict[str, Any]:
        name = str(reagent.get("\u8bd5\u5242\u540d\u79f0", "") or "").strip()
        cas = str(reagent.get("CAS\u53f7", "") or "").strip()
        return {
            "\u8bd5\u5242\u6e05\u5355\u53f7": self.current_detail_list_number(),
            "\u5e8f\u53f7": reagent.get("\u5e8f\u53f7", ""),
            "\u8bd5\u5242\u540d\u79f0": name,
            "CAS\u53f7": cas,
            "\u89c4\u683c": reagent.get("\u89c4\u683c", ""),
            "\u89c4\u683c\u5355\u4f4d": reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
            "\u8bd5\u5242\u6570\u91cf": reagent.get("\u8bd5\u5242\u6570\u91cf", ""),
            "\u6807\u51c6\u5316\u540d\u79f0": name,
            "\u82f1\u6587\u540d\u79f0": "",
            "\u6e05\u6d17\u540e\u540d\u79f0": name,
            "\u6d53\u5ea6": "",
            "\u540d\u79f0\u6807\u51c6\u5316\u7f6e\u4fe1\u5ea6": 1.0,
            "\u540d\u79f0\u9700\u4eba\u5de5\u590d\u6838": False,
            "\u540d\u79f0\u6807\u51c6\u5316\u539f\u56e0": reason,
            "\u67e5\u8be2\u6765\u6e90": "business_rule_unknown_name",
            "\u67e5\u8be2URL": "",
            "\u67e5\u8be2\u9700\u4eba\u5de5": False,
            "\u7f51\u7ad9\u5339\u914d\u540d\u79f0": "",
            "\u540d\u79f0\u76f8\u4f3c\u5ea6": 1.0,
            "\u67e5\u8be2\u76f8\u5173\u6027\u901a\u8fc7": True,
            "\u8d44\u6599\u53ef\u4fe1\u5ea6": 1.0,
            "\u8bc1\u636e\u8d28\u91cf": "unknown_name",
            "\u660e\u786e\u672a\u77e5\u540d\u79f0\u89c4\u5219\u547d\u4e2d": True,
            "unknown_name_rule": True,
            "\u67e5\u8be2\u5931\u8d25\u539f\u56e0": "",
            "\u5927\u6a21\u578b\u5019\u9009\u7c7b\u522b": [UNKNOWN_CATEGORY],
            "\u8bc1\u636e": reason,
            "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": UNKNOWN_CATEGORY,
            "\u547d\u4e2d\u7c7b\u522b": [UNKNOWN_CATEGORY],
            "\u89c4\u5219\u539f\u56e0": reason,
            "\u7f6e\u4fe1\u5ea6": 1.0,
            "\u9700\u4eba\u5de5\u590d\u6838": False,
        }

    def remember_erp_suggestion(self, memory: ReagentMemory, suggestion: dict[str, Any]) -> bool:
        if str(suggestion.get("查询来源") or "").strip() == "reagent_memory":
            return False
        if self._truthy(suggestion.get("LLM辅助意见仅供复核")):
            return False
        if str(suggestion.get("审批链路") or "").strip() == "v3" and not self._truthy(suggestion.get("ERP写入已校验")):
            return False
        final_category = str(suggestion.get("最终建议类别") or "").strip()
        erp_category = to_erp_property(final_category, self.settings)
        if not erp_category:
            return False
        normalized = dict(suggestion)
        normalized["最终建议类别"] = erp_category
        return memory.remember_suggestion(normalized)

    def remember_verified_approval_suggestion(self, suggestion: dict[str, Any], erp_category: str) -> bool:
        memory = ReagentMemory.from_settings(self.settings, self.root_dir)
        verified = dict(suggestion)
        verified["最终建议类别"] = erp_category
        verified["查询来源"] = str(verified.get("查询来源") or "").strip() or "verified_erp_write"
        original_cas = str(verified.get("CAS号") or "").strip()
        candidate_cas = str(verified.get("网页候选CAS") or "").strip()
        cas_promoted = bool(
            (
                self._truthy(verified.get("V3候选CAS可靠"))
                or self._truthy(verified.get("identity_enrichment_candidate_cas_verified"))
            )
            and original_cas in {"", "-", "--"}
            and self._checksum_valid_cas(candidate_cas)
        )
        if cas_promoted:
            verified["CAS号"] = candidate_cas
        reason = str(verified.get("规则原因") or "").strip()
        formal_rule_ids = list(dict.fromkeys(
            re.findall(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,}\b", str(verified.get("命中规则ID") or ""))
        ))
        if formal_rule_ids:
            reason = f"{reason}\n正式规则ID: {', '.join(formal_rule_ids)}".strip()
        backend_evidence = (
            f"ERP 接口稳定记录 ID {verified.get('_erp_record_id')} 两次回读确认 {erp_category}。"
            if verified.get("_erp_record_id") else f"ERP 网页保存后校验物化特性为 {erp_category}。"
        )
        verified["规则原因"] = f"{reason}\n{backend_evidence}".strip()
        try:
            # A successful ERP write with a stable-ID read-back is the final
            # business confirmation.  Persist every such result for exact
            # future reagent-name reuse, including product names and entries
            # whose earlier model advice had remained advisory-only.
            stored = memory.add_record(
                raw_name=str(verified.get("试剂名称") or "").strip(),
                cleaned_name=str(verified.get("清洗后名称") or "").strip(),
                standard_name=str(verified.get("标准化名称") or "").strip(),
                cas=str(verified.get("CAS号") or "").strip(),
                final_category=erp_category,
                confidence=1.0,
                reason=verified["规则原因"],
                source="verified_erp_write",
                url=str(verified.get("查询URL") or "").strip(),
                need_manual_review=False,
                manual_verified=True,
                track_conflicts=False,
                force_reusable=True,
            )
            if stored and cas_promoted:
                audit_name = (
                    "v3_candidate_cas_audit.jsonl"
                    if self._truthy(verified.get("V3候选CAS可靠"))
                    else "verified_candidate_cas_audit.jsonl"
                )
                path = Path(self.root_dir) / "data" / "logs" / audit_name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "at": pd.Timestamp.now().isoformat(), "sequence": verified.get("序号"),
                        "raw_name": verified.get("试剂名称"), "erp_cas": original_cas,
                        "memory_cas": candidate_cas, "category": erp_category,
                        "record_id": verified.get("_erp_record_id"), "source": verified.get("查询URL"),
                    }, ensure_ascii=False) + "\n")
            return stored
        except Exception as error:  # noqa: BLE001 - memory failures must not break a verified ERP save.
            print(f"Could not store verified approval result in reagent memory: {error}")
            return False

    def memory_match_is_safe(
        self,
        reagent: dict[str, str],
        memory_row: dict[str, Any],
        name_result: dict[str, Any] | None = None,
        rule_engine: RuleEngine | None = None,
    ) -> bool:
        if ReagentMemory.is_unsafe_reusable_evidence(memory_row):
            return False
        final_category = str(memory_row.get("final_category") or "").strip()
        # An ERP readback proves that Unknown was saved, not that the
        # underlying identity/formula was correctly deemed unknowable.  Recheck
        # these entries when they recur; explicit unknown names are handled by
        # the earlier deterministic name rule.
        if final_category == UNKNOWN_CATEGORY:
            return False
        if final_category in {"易燃类", "易燃液体"}:
            return self._flammable_memory_match_is_safe(reagent, memory_row, name_result, rule_engine)
        if final_category != "普通类":
            return True

        raw_name = str(reagent.get("试剂名称", "") or "").strip()
        memory_names = " ".join(
            str(memory_row.get(key) or "")
            for key in ("raw_name", "cleaned_name", "standard_name")
        )
        normalized_names = " ".join(
            str((name_result or {}).get(key) or "")
            for key in ("raw_name", "cleaned_name", "standard_name", "english_name")
        )
        actual_names = " ".join([raw_name, normalized_names])

        if self._has_bromine_or_iodine(actual_names):
            return False
        if self._looks_like_pharmacopoeia_color(memory_names) and not self._looks_like_pharmacopoeia_color(actual_names):
            return False
        if rule_engine and self._ordinary_memory_conflicts_with_rules(
            reagent,
            memory_row,
            name_result,
            rule_engine,
        ):
            return False
        return True

    @staticmethod
    def _ordinary_memory_conflicts_with_rules(
        reagent: dict[str, str],
        memory_row: dict[str, Any],
        name_result: dict[str, Any] | None,
        rule_engine: RuleEngine,
    ) -> bool:
        raw_name = str(reagent.get("试剂名称", "") or memory_row.get("raw_name") or "").strip()
        cleaned_name = str(
            (name_result or {}).get("cleaned_name")
            or memory_row.get("cleaned_name")
            or raw_name
        ).strip()
        standard_name = str(
            (name_result or {}).get("standard_name")
            or memory_row.get("standard_name")
            or raw_name
        ).strip()
        classification = rule_engine.classify(
            {
                "reagent_name": raw_name,
                "name": standard_name or raw_name,
                "standard_name": standard_name,
                "cleaned_name": cleaned_name,
                "cas": reagent.get("CAS号", "") or memory_row.get("cas") or "",
                "text": " ".join(value for value in (raw_name, cleaned_name, standard_name) if value),
                "allow_default_normal": False,
            }
        )
        category = str(classification.get("final_category") or "").strip()
        return bool(category and category != "普通类")

    @staticmethod
    def _flammable_memory_match_is_safe(
        reagent: dict[str, str],
        memory_row: dict[str, Any],
        name_result: dict[str, Any] | None,
        rule_engine: RuleEngine | None,
    ) -> bool:
        reason = str(memory_row.get("reason") or "").strip()
        lowered_reason = reason.lower()
        if any(
            token in lowered_reason or token in reason
            for token in (
                "网页写入失败",
                "could not select",
                "row still shows",
                "selected 易燃类, but",
            )
        ):
            return False
        if bool(memory_row.get("manual_verified")):
            return True
        if rule_engine is None:
            return False

        raw_name = str(reagent.get("试剂名称", "") or memory_row.get("raw_name") or "").strip()
        cleaned_name = str(
            (name_result or {}).get("cleaned_name")
            or memory_row.get("cleaned_name")
            or raw_name
        ).strip()
        standard_name = str(
            (name_result or {}).get("standard_name")
            or memory_row.get("standard_name")
            or raw_name
        ).strip()
        classification = rule_engine.classify(
            {
                "reagent_name": raw_name,
                "name": standard_name or raw_name,
                "standard_name": standard_name,
                "cleaned_name": cleaned_name,
                "cas": reagent.get("CAS号", "") or memory_row.get("cas") or "",
                "text": " ".join(
                    value
                    for value in (
                        raw_name,
                        cleaned_name,
                        standard_name,
                        reason,
                        str(memory_row.get("source") or ""),
                    )
                    if value
                ),
                "allow_default_normal": False,
            }
        )
        return bool(
            classification.get("final_category") == "易燃液体"
            and not classification.get("need_manual_review", True)
            and RuleEngine.is_reusable_flammable_evidence(
                {
                    "reagent_name": raw_name,
                    "name": standard_name or raw_name,
                    "standard_name": standard_name,
                    "cleaned_name": cleaned_name,
                    "text": reason,
                    "evidence": [reason],
                }
            )
        )

    def disable_unsafe_memory_match(
        self,
        memory: ReagentMemory,
        memory_row: dict[str, Any],
        reagent: dict[str, str],
    ) -> None:
        record_id = memory_row.get("id")
        reagent_name = str(reagent.get("试剂名称", "") or "").strip()
        message = (
            "Unsafe reagent memory ignored: "
            f"{reagent_name} matched memory id {record_id} -> {memory_row.get('final_category', '')}."
        )
        print(message)
        if not record_id:
            return
        previous_reason = str(memory_row.get("reason") or "").strip()
        reason = (
            f"{previous_reason}\n"
            f"自动停用：{reagent_name} 的本地记忆命中未通过安全校验，需人工确认后再复用。"
        ).strip()
        try:
            memory.update_record(
                int(record_id),
                {
                    "reusable": False,
                    "conflict": True,
                    "reason": reason,
                },
            )
        except Exception as error:
            print(f"Could not disable unsafe reagent memory id {record_id}: {error}")

    @staticmethod
    def _has_bromine_or_iodine(text: str) -> bool:
        return bool(
            re.search(
                r"溴|碘|bromo|bromide|bromine|iodo|iodide|iodine",
                str(text or ""),
                flags=re.I,
            )
        )

    @staticmethod
    def _looks_like_pharmacopoeia_color(text: str) -> bool:
        normalized = re.sub(r"\s+", "", str(text or "").lower())
        tokens = (
            "药典色度标准品",
            "药典色度标准溶液",
            "欧洲药典色度标准溶液",
            "色度标准品",
            "色度标准溶液",
            "pharmacopoeiacolor",
            "pharmacopoeialcolor",
            "colourstandard",
            "colorstandard",
        )
        return any(token in normalized for token in tokens)

    def add_manual_review_item_from_suggestion(
        self,
        reagent: dict[str, str],
        name_result: dict[str, Any],
        search_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
        suggestion: dict[str, Any],
    ) -> None:
        category = classification.get("final_category") or suggestion.get("最终建议类别")
        if self.is_terminal_reject_decision(category) and not self._truthy(suggestion.get("V3模型未核实")):
            self.apply_terminal_reject_policy(suggestion)
            return
        search_result = dict(search_result or {})
        search_result.update({
            "identity_resolution": suggestion.get("身份解析详情", search_result.get("identity_resolution", "")),
            "name_identity": suggestion.get("名称身份", search_result.get("name_identity", "")),
            "cas_identity": suggestion.get("CAS身份", search_result.get("cas_identity", "")),
            "identity_status": suggestion.get("身份状态", search_result.get("identity_status", "")),
            "identity_candidates": suggestion.get("身份候选", search_result.get("identity_candidates", "")),
            "llm_identity_opinion": suggestion.get("LLM身份意见", search_result.get("llm_identity_opinion", "")),
            "llm_name_identity_opinion": suggestion.get("LLM名称身份意见", search_result.get("llm_name_identity_opinion", "")),
            "llm_cas_identity_opinion": suggestion.get("LLMCAS身份意见", search_result.get("llm_cas_identity_opinion", "")),
            "llm_identity_second_opinion": suggestion.get("LLM身份第二意见", search_result.get("llm_identity_second_opinion", "")),
            "llm_identity_trigger_status": (suggestion.get("身份状态") or ""),
            "llm_advisory_category": suggestion.get("LLM辅助建议类别", search_result.get("llm_advisory_category", "")),
            "llm_advisory_summary_cn": suggestion.get("LLM辅助物性意见", search_result.get("llm_advisory_summary_cn", "")),
            "llm_advisory_reason_cn": suggestion.get("LLM辅助判定理由", search_result.get("llm_advisory_reason_cn", "")),
            "llm_advisory_rule_cn": suggestion.get("LLM辅助规则依据", search_result.get("llm_advisory_rule_cn", "")),
            "llm_advisory_uncertainties_cn": suggestion.get("LLM辅助不确定项", search_result.get("llm_advisory_uncertainties_cn", "")),
            "llm_advisory_confidence": suggestion.get("LLM辅助置信度", search_result.get("llm_advisory_confidence", "")),
            "llm_advisory_evidence_basis": suggestion.get("LLM辅助依据类型", search_result.get("llm_advisory_evidence_basis", "")),
            "llm_advisory_only": bool(
                suggestion.get("LLM辅助意见仅供复核")
                or search_result.get("llm_advisory_only")
                or suggestion.get("LLM身份第二意见")
            ),
            "original_erp_cas": suggestion.get("原ERP CAS号", search_result.get("original_erp_cas", "")),
            "candidate_cas": suggestion.get("网页候选CAS", search_result.get("candidate_cas", "")),
            "candidate_cas_validation_status": suggestion.get("候选CAS校验状态", search_result.get("candidate_cas_validation_status", "")),
            "candidate_cas_validation_reason": suggestion.get("候选CAS拒绝原因", search_result.get("candidate_cas_validation_reason", "")),
            "candidate_cas_page_name": suggestion.get("候选CAS页面名称", search_result.get("candidate_cas_page_name", "")),
            "candidate_cas_secondary_confirmation_source": suggestion.get("候选CAS二次确认来源", search_result.get("candidate_cas_secondary_confirmation_source", "")),
            "candidate_cas_secondary_confirmation_url": suggestion.get("候选CAS二次确认URL", search_result.get("candidate_cas_secondary_confirmation_url", "")),
            "candidate_cas_memory_eligible": bool(suggestion.get("候选CAS可写入记忆库")),
            "corrected_cas": suggestion.get("修正CAS号", search_result.get("corrected_cas", "")),
            "cas_name_conflict": suggestion.get("CAS名称冲突", search_result.get("cas_name_conflict", False)),
            "cas_correction_candidate": suggestion.get("CAS修正候选", search_result.get("cas_correction_candidate", False)),
            "cas_correction_applied": suggestion.get("CAS修正已应用", search_result.get("cas_correction_applied", False)),
            "cas_correction_reason": suggestion.get("CAS修正原因", search_result.get("cas_correction_reason", "")),
            "cas_correction_source": suggestion.get("CAS修正来源", search_result.get("cas_correction_source", "")),
            "cas_correction_url": suggestion.get("CAS修正URL", search_result.get("cas_correction_url", "")),
            "identity_decision_basis": suggestion.get("身份判定依据", search_result.get("identity_decision_basis", "")),
            "used_llm_manual_review_advice": suggestion.get("是否生成LLM身份第二意见", search_result.get("used_llm_manual_review_advice", False)),
        })
        final_category = str(
            suggestion.get("最终建议类别")
            or classification.get("final_category")
            or ""
        ).strip()
        if final_category == UNKNOWN_CATEGORY:
            self.apply_unknown_auto_write_policy(suggestion)
            return
        reason_parts = [
            str(suggestion.get("规则原因") or "").strip(),
            str(search_result.get("failure_reason") or "").strip(),
            str(name_result.get("reason") or "").strip(),
            " | ".join(str(item) for item in extracted.get("evidence", []) or [] if str(item).strip()),
            str(search_result.get("raw_text") or "").strip()[:800],
        ]
        reason = next((part for part in reason_parts if part), "")
        if not reason:
            reason = "Rule classification, name normalization, or source evidence requires manual review."
        self.add_manual_review_item(
            reagent,
            name_result,
            reason=reason,
            search_result=search_result,
            extracted=extracted,
            classification=classification,
        )

    def search_reagents_parallel(self, items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        unique_items, index_groups = self.unique_work_items(
            items,
            key_func=self.reagent_work_cache_key,
            reuse_label="search",
        )

        def expand_results(unique_results: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
            expanded: dict[int, dict[str, Any]] = {}
            for unique_item in unique_items:
                result = unique_results.get(unique_item["index"])
                if result is None:
                    continue
                key = self.reagent_work_cache_key(unique_item)
                for index in index_groups.get(key, [unique_item["index"]]):
                    expanded[index] = copy.deepcopy(result)
            return expanded

        print(
            f"Searching official chemical sources as one deduplicated batch for {len(unique_items)} unique reagent(s) "
            f"from {len(items)} candidate(s)."
        )
        if type(self).search_reagent_worker is not ApprovalFlowMixin.search_reagent_worker:
            return expand_results({item["index"]: self.search_reagent_worker(item) for item in unique_items})
        searcher = getattr(self, "_run_chemical_searcher", None)
        if searcher is None:
            searcher = ChemicalSearcher(
                settings=self.settings,
                root_dir=getattr(self, "root_dir", None),
                metrics=self.enrichment_metrics(),
            )
            self._run_chemical_searcher = searcher
        try:
            batch_results = searcher.search_many([item["reagent"] for item in unique_items])
        except Exception as error:  # noqa: BLE001 - keep lookup failures from stopping the page
            batch_results = [self.search_failure_result(item["reagent"], str(error)) for item in unique_items]
        results = {
            item["index"]: batch_results[position]
            for position, item in enumerate(unique_items)
            if position < len(batch_results)
        }
        return expand_results(results)

    def search_reagent_worker(self, item: dict[str, Any]) -> dict[str, Any]:
        reagent = item["reagent"]
        reagent_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "").strip()
        print(f"[parallel search] START {item['progress']} {reagent_name}")
        searcher = ChemicalSearcher(settings=self.settings, root_dir=self.root_dir)
        result = searcher.search(
            reagent_name,
            cas=reagent.get("CAS\u53f7", "").strip(),
        )
        print(f"[parallel search] END {item['progress']} {reagent_name} -> {result.get('source') or 'manual_review'}")
        return result

    def extract_and_classify_parallel(
        self,
        items: list[dict[str, Any]],
        rule_engine: RuleEngine,
    ) -> dict[int, tuple[dict[str, Any], dict[str, Any]]]:
        worker_count = self.parallel_worker_count()
        unique_items, index_groups = self.unique_work_items(
            items,
            key_func=self.reagent_llm_cache_key,
            reuse_label="llm",
        )

        def expand_results(
            unique_results: dict[int, tuple[dict[str, Any], dict[str, Any]]],
        ) -> dict[int, tuple[dict[str, Any], dict[str, Any]]]:
            expanded: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
            for unique_item in unique_items:
                result = unique_results.get(unique_item["index"])
                if result is None:
                    continue
                key = self.reagent_llm_cache_key(unique_item)
                for index in index_groups.get(key, [unique_item["index"]]):
                    expanded[index] = copy.deepcopy(result)
            return expanded

        if worker_count <= 1 or len(items) <= 1:
            return expand_results({item["index"]: self.extract_and_classify_worker(item, rule_engine) for item in unique_items})

        print(
            f"Extracting LLM properties with {worker_count} worker(s) for {len(unique_items)} unique reagent(s) "
            f"from {len(items)} candidate(s)."
        )
        results: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="llm-extract") as executor:
            futures = {executor.submit(self.extract_and_classify_worker, item, rule_engine): item for item in unique_items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    results[item["index"]] = future.result()
                except Exception as error:  # noqa: BLE001
                    results[item["index"]] = self.empty_extraction_and_classification(
                        item["reagent"],
                        item["search_result"],
                        rule_engine,
                        str(error),
                    )
        return expand_results(results)

    def extract_and_classify_worker(
        self,
        item: dict[str, Any],
        rule_engine: RuleEngine,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        reagent = item["reagent"]
        reagent_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "").strip()
        search_result = item["search_result"]
        print(f"[parallel llm] START {item['progress']} {reagent_name}")
        skip_llm = self.search_result_should_skip_llm(search_result, item.get("name_result") or {}, reagent_name)
        # In batch-advice mode, a missing web result is exactly when an
        # advisory-only model opinion is useful.  It never supplies formal web
        # evidence and the resulting classification remains manual review.
        if skip_llm and self.llm_manual_review_advice_enabled() and not self.manual_review_name_should_skip_llm(reagent_name):
            skip_llm = False
        if skip_llm:
            print(f"[parallel llm] SKIP {item['progress']} {reagent_name} -> manual_review_no_trusted_evidence")
            extracted, classification = self.empty_extraction_and_classification(
                reagent,
                search_result,
                rule_engine,
                search_result.get("failure_reason") or "No trusted web evidence; LLM skipped for obvious product/mixture/manual-review item.",
            )
            classification, search_result = self.v1_classify_without_web_evidence(
                reagent,
                item.get("name_result") or {},
                search_result,
                classification,
                rule_engine,
            )
            item["search_result"] = search_result
            if classification.get("v1_no_web_evidence_policy"):
                return extracted, classification
            classification = dict(classification)
            classification["need_manual_review"] = True
            classification["reason"] = (
                f"{classification.get('reason') or ''} No trusted web evidence; batch LLM was skipped."
            ).strip()
            return extracted, classification
        extractor = LlmExtractor(settings=self.settings)
        extractor.metrics = self.enrichment_metrics()
        if not hasattr(extractor, "generate_manual_review_advice") and self._search_result_needs_llm_knowledge_fallback(search_result):
            rule_fallback: dict[str, Any] = {}
            if hasattr(extractor, "classify_by_rules_fallback"):
                rule_fallback = extractor.classify_by_rules_fallback(
                    {
                        "raw_name": reagent_name,
                        "name": reagent_name,
                        "cas": search_result.get("cas") or str(item.get("search_cas") or reagent.get("CAS\u53f7", "")),
                        "standard_name": (search_result.get("name_normalization") or {}).get("standard_name", ""),
                        "cleaned_name": (search_result.get("name_normalization") or {}).get("cleaned_name", ""),
                        "failed_queries": [search_result.get("query", "")],
                        "no_web_evidence_reason": search_result.get("failure_reason") or search_result.get("raw_text") or "",
                        "web_evidence_quality": search_result.get("evidence_quality", ""),
                        "rule_summary": self._rule_summary_for_llm(rule_engine),
                    }
                )
                if rule_fallback.get("used_llm"):
                    search_result = dict(search_result)
                    search_result["evidence_quality"] = "llm_rule_low"
                    search_result["used_llm_rule_fallback"] = True
                    search_result["llm_rule_confidence"] = min(
                        self._float_confidence(rule_fallback.get("confidence")),
                        1.0,
                    )
                    search_result["llm_rule_candidate_category"] = str(
                        rule_fallback.get("candidate_category") or ""
                    ).strip()
                    search_result["llm_rule_reason"] = str(rule_fallback.get("reason") or "").strip()
                    search_result["llm_rule_matched_rule"] = str(
                        rule_fallback.get("matched_rule_summary") or ""
                    ).strip()
                    search_result["llm_rule_evidence_type"] = str(rule_fallback.get("evidence_type") or "").strip()
                    search_result["llm_rule_must_manual_review"] = bool(
                        rule_fallback.get("must_manual_review")
                    )
                    search_result["need_manual_review"] = True
                    item["search_result"] = search_result
            fallback = extractor.generate_knowledge_fallback(
                {
                    "raw_name": reagent_name,
                    "name": reagent_name,
                    "cas": search_result.get("cas") or str(item.get("search_cas") or reagent.get("CAS\u53f7", "")),
                    "standard_name": (search_result.get("name_normalization") or {}).get("standard_name", ""),
                    "cleaned_name": (search_result.get("name_normalization") or {}).get("cleaned_name", ""),
                    "failed_queries": [search_result.get("query", "")],
                    "no_web_evidence_reason": search_result.get("failure_reason") or search_result.get("raw_text") or "",
                    "web_evidence_quality": search_result.get("evidence_quality", ""),
                }
            )
            raw_text = str(fallback.get("raw_text") or "").strip()
            if raw_text:
                search_result = dict(search_result)
                search_result["raw_text"] = raw_text
                search_result["source"] = "LLM knowledge fallback"
                search_result["fallback_source"] = "LLM knowledge fallback"
                search_result["fallback_url"] = ""
                search_result["source_confidence"] = 0.0
                search_result["llm_confidence"] = min(self._float_confidence(fallback.get("confidence")), 0.65)
                search_result["evidence_quality"] = "llm_low"
                search_result["used_llm_knowledge_fallback"] = True
                search_result["need_manual_review"] = True
                search_result["failure_reason"] = (
                    f"{search_result.get('failure_reason') or 'Website evidence is missing or insufficient.'} "
                    f"LLM fallback was used for manual-review advice only. {fallback.get('reason') or ''}"
                ).strip()
                item["search_result"] = search_result
        extraction_text = search_result.get("raw_text", "") if self._search_has_extractable_evidence(search_result) else ""
        extracted = extractor.extract_properties(
            raw_text=extraction_text,
            name=f"{reagent_name} / {search_result.get('name') or str(item.get('search_name') or reagent_name)}",
            cas=search_result.get("cas") or str(item.get("search_cas") or reagent.get("CAS\u53f7", "")),
        )
        extracted = self.apply_selected_property_evidence(extracted, search_result)
        if search_result.get("used_llm_knowledge_fallback"):
            extracted["used_llm_knowledge_fallback"] = True
            extracted["llm_confidence"] = search_result.get("llm_confidence", "")
        if search_result.get("used_llm_rule_fallback"):
            rule_category = str(search_result.get("llm_rule_candidate_category") or "").strip()
            if rule_category:
                categories = list(extracted.get("suggested_categories") or [])
                if rule_category not in categories:
                    categories.insert(0, rule_category)
                extracted["suggested_categories"] = categories
                evidence = list(extracted.get("evidence") or [])
                rule_reason = str(search_result.get("llm_rule_reason") or "").strip()
                matched_rule = str(search_result.get("llm_rule_matched_rule") or "").strip()
                if rule_reason:
                    evidence.append(f"LLM按规则辅助判断：{rule_reason}")
                if matched_rule:
                    evidence.append(f"LLM命中规则摘要：{matched_rule}")
                extracted["evidence"] = evidence
            extracted["used_llm_rule_fallback"] = True
            extracted["llm_rule_confidence"] = search_result.get("llm_rule_confidence", "")
            extracted["llm_rule_reason"] = search_result.get("llm_rule_reason", "")
            extracted["llm_rule_matched_rule"] = search_result.get("llm_rule_matched_rule", "")
            extracted["llm_rule_must_manual_review"] = search_result.get("llm_rule_must_manual_review", True)
        component_categories: list[str] = []
        for component_result in search_result.get("component_results", []) or []:
            component_info = {
                "reagent_name": component_result.get("name", ""),
                "name": component_result.get("name", ""),
                "cas": component_result.get("cas", ""),
                "text": component_result.get("raw_text", ""),
                "allow_default_normal": False,
            }
            component_classification = rule_engine.classify(component_info)
            component_result["classification"] = component_classification
            component_candidates = list(component_classification.get("matched_categories", []) or [])
            if component_classification.get("final_category"):
                component_candidates.insert(0, component_classification["final_category"])
            for category in component_candidates:
                category = str(category or "").strip()
                if category and category != "未知类" and category not in component_categories:
                    component_categories.append(category)
        if component_categories:
            search_result["component_categories"] = component_categories
            search_result["mixture_risk_categories"] = list(component_categories)
            search_result["mixture_selected_category"] = rule_engine.highest_priority_category(component_categories)
            search_result["mixture_classification_basis"] = "highest_rule_priority_of_confirmed_components"
        classification = rule_engine.classify(self._classification_input(reagent, search_result, extracted))
        if search_result.get("is_mixture") and not search_result.get("composition_complete", False):
            classification = dict(classification)
            classification["need_manual_review"] = True
            classification["reason"] = (
                f"{classification.get('reason') or ''} 混合物组成不完整，已采用已知最高风险类别并转人工复核。"
            ).strip()
        elif search_result.get("is_mixture") and search_result.get("composition_complete", False):
            classification = dict(classification)
            classification["reason"] = (
                f"{classification.get('reason') or ''} 混合物按全部已确认组分中的最高规则优先级保守分类，"
                "不因低浓度而降级。"
            ).strip()
        if search_result.get("used_llm_knowledge_fallback") or search_result.get("used_llm_rule_fallback"):
            classification = dict(classification)
            classification["need_manual_review"] = True
            if search_result.get("used_llm_rule_fallback"):
                rule_category = str(search_result.get("llm_rule_candidate_category") or "").strip()
                if rule_category and not classification.get("final_category"):
                    classification["final_category"] = rule_category
                    classification["matched_categories"] = [rule_category]
                classification["llm_rule_confidence"] = search_result.get("llm_rule_confidence", "")
                classification["llm_rule_must_manual_review"] = search_result.get(
                    "llm_rule_must_manual_review",
                    True,
                )
            classification["reason"] = (
                f"{classification.get('reason') or ''} "
                "LLM fallback evidence is advisory only and requires manual review."
            ).strip()
        classification, search_result = self.v1_primary_component_classification(
            reagent,
            search_result,
            extracted,
            classification,
            rule_engine,
        )
        classification, search_result = self.v1_classify_without_web_evidence(
            reagent,
            item.get("name_result") or {},
            search_result,
            classification,
            rule_engine,
        )
        item["search_result"] = search_result
        if (
            hasattr(extractor, "generate_manual_review_advice")
            and self.llm_manual_review_advice_enabled()
            and self._classification_requires_manual_advice(search_result, item.get("name_result") or {}, extracted, classification)
        ):
            rule_summary = self._rule_summary_for_llm(rule_engine)
            rules_fingerprint = hashlib.sha256(
                json.dumps(rule_summary, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            advice = extractor.generate_manual_review_advice(
                {
                    "raw_name": reagent_name,
                    "standard_name": (search_result.get("name_normalization") or {}).get("standard_name", ""),
                    "cleaned_name": (search_result.get("name_normalization") or {}).get("cleaned_name", ""),
                    "cas": search_result.get("cas") or str(item.get("search_cas") or reagent.get("CAS号", "")),
                    "concentration": (search_result.get("name_normalization") or {}).get("concentration", ""),
                    "web_source": search_result.get("source", ""),
                    "web_evidence": str(search_result.get("raw_text") or "")[:3000],
                    "web_evidence_quality": search_result.get("evidence_quality", ""),
                    "source_confidence": search_result.get("source_confidence", 0.0),
                    "has_trusted_web_evidence": self._search_has_trusted_web_evidence(search_result),
                    "current_rule_category": classification.get("final_category", ""),
                    "current_rule_reason": classification.get("reason", ""),
                    "manual_review_reason": self._manual_advice_reason(search_result, item.get("name_result") or {}, extracted, classification),
                    "rule_summary": rule_summary,
                    "allowed_categories": list(erp_property_options(self.settings)),
                    "rules_fingerprint": rules_fingerprint,
                }
            )
            search_result = dict(search_result)
            search_result.update(self._manual_advice_search_fields(advice))
            item["search_result"] = search_result
            extracted = dict(extracted)
            extracted.update(self._manual_advice_extracted_fields(advice))
            if self.llm_rule_gap_auto_adopt_enabled():
                classification, search_result = self._adopt_llm_rule_gap_opinion(
                    classification=classification,
                    search_result=search_result,
                    advice=advice,
                )
                item["search_result"] = search_result
        print(
            f"[parallel llm] END {item['progress']} {reagent_name} "
            f"-> {classification.get('final_category') or '<manual_review>'}"
        )
        return extracted, classification

    @staticmethod
    def _search_has_extractable_evidence(search_result: dict[str, Any]) -> bool:
        source = str(search_result.get("source") or "").strip()
        raw_text = str(search_result.get("raw_text") or "").strip()
        quality = str(search_result.get("evidence_quality") or "").strip().lower()
        return bool(source and raw_text and quality not in {"", "none", "llm_knowledge_low"})

    @staticmethod
    def _search_has_trusted_web_evidence(search_result: dict[str, Any]) -> bool:
        return bool(
            ChemicalSearcher.is_trusted_source(str(search_result.get("source") or ""))
            and search_result.get("relevance_passed", False)
            and ApprovalFlowMixin._float_confidence(search_result.get("source_confidence")) >= 0.7
            and str(search_result.get("retrieval_status") or "fresh").lower() != "stale"
        )

    def llm_manual_review_advice_enabled(self) -> bool:
        approval_settings = self.settings.get("approval", {}) or {}
        mode = str(approval_settings.get("llm_manual_review_advice_mode") or "batch").strip().lower()
        if mode == "on_demand":
            return False
        value = os.getenv("ENABLE_LLM_MANUAL_REVIEW_ADVICE")
        if value is None:
            value = approval_settings.get("enable_llm_manual_review_advice", True)
        return self._truthy(value)

    def llm_rule_gap_auto_adopt_enabled(self) -> bool:
        """Whether a non-empty LLM second opinion fills an otherwise empty rule result.

        This is intentionally narrower than replacing a rule-engine decision: it
        applies only when the engine produced no ERP-mappable category at all.
        The resulting suggestion remains explicitly marked for audit and is only
        written through the normal ERP save-and-readback path.
        """
        approval_settings = self.settings.get("approval", {}) or {}
        value = os.getenv("ADOPT_LLM_SECOND_OPINION_WHEN_RULE_UNRESOLVED")
        if value is None:
            value = approval_settings.get("adopt_llm_second_opinion_when_rule_unresolved", False)
        return self._truthy(value)

    def _adopt_llm_rule_gap_opinion(
        self,
        *,
        classification: dict[str, Any],
        search_result: dict[str, Any],
        advice: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Promote a valid LLM second opinion only when rules yielded no category."""
        current_category = str(classification.get("final_category") or "").strip()
        if current_category and to_erp_property(current_category, self.settings):
            return classification, search_result
        candidate = str(advice.get("candidate_category") or "").strip()
        if not candidate or not to_erp_property(candidate, self.settings):
            return classification, search_result
        if not self._truthy(advice.get("used_llm")):
            return classification, search_result

        adopted = dict(classification)
        reason = str(advice.get("reason_cn") or "").strip()
        rule_summary = str(advice.get("matched_rule_summary_cn") or "").strip()
        adopted.update({
            "final_category": candidate,
            "matched_categories": [candidate],
            "matched_rule_ids": [],
            "confidence": self._float_confidence(advice.get("advisory_confidence")),
            "need_manual_review": False,
            "llm_second_opinion_adopted": True,
            "reason": (
                "规则引擎未形成可写入 ERP 的类别；按启用的 LLM 第二意见兜底采用"
                f"“{candidate}”。"
                + (f" 理由：{reason}" if reason else "")
                + (f" 规则摘要：{rule_summary}" if rule_summary else "")
            ),
        })
        result = dict(search_result)
        result.update({
            "llm_advisory_only": False,
            "llm_second_opinion_adopted": True,
            "need_manual_review": False,
        })
        return adopted, result

    def _classification_requires_manual_advice(
        self,
        search_result: dict[str, Any],
        name_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
    ) -> bool:
        return self._suggestion_needs_manual_review(search_result, name_result, extracted, classification)

    @staticmethod
    def _manual_advice_reason(
        search_result: dict[str, Any],
        name_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
    ) -> str:
        parts = [
            str(classification.get("reason") or "").strip(),
            str(search_result.get("failure_reason") or "").strip(),
            str(name_result.get("reason") or "").strip(),
            "；".join(str(item) for item in extracted.get("evidence", []) or [] if str(item).strip()),
        ]
        return "；".join(part for part in parts if part)[:3000]

    @staticmethod
    def _manual_advice_search_fields(advice: dict[str, Any]) -> dict[str, Any]:
        return {
            "llm_advisory_category": advice.get("candidate_category", ""),
            "llm_advisory_summary_cn": advice.get("physicochemical_summary_cn", ""),
            "llm_advisory_reason_cn": advice.get("reason_cn", ""),
            "llm_advisory_rule_cn": advice.get("matched_rule_summary_cn", ""),
            "llm_advisory_uncertainties_cn": "；".join(advice.get("uncertainties_cn", []) or []),
            "llm_advisory_confidence": advice.get("advisory_confidence", 0.0),
            "llm_advisory_evidence_basis": advice.get("evidence_basis", "证据不足"),
            "llm_advisory_only": True,
            "llm_advisory_high_risk": advice.get("high_risk", False),
            "llm_model": advice.get("model", ""),
            "llm_provider": advice.get("provider", ""),
            "llm_generated_at": advice.get("generated_at", ""),
            "llm_rules_fingerprint": advice.get("rules_fingerprint", ""),
            "llm_raw_diagnostic": advice.get("raw_diagnostic", ""),
            "used_llm_manual_review_advice": advice.get("used_llm", False),
        }

    @staticmethod
    def _manual_advice_extracted_fields(advice: dict[str, Any]) -> dict[str, Any]:
        return {
            "llm_advisory_category": advice.get("candidate_category", ""),
            "llm_advisory_summary_cn": advice.get("physicochemical_summary_cn", ""),
            "llm_advisory_reason_cn": advice.get("reason_cn", ""),
            "llm_advisory_rule_cn": advice.get("matched_rule_summary_cn", ""),
            "llm_advisory_uncertainties_cn": advice.get("uncertainties_cn", []) or [],
            "llm_advisory_confidence": advice.get("advisory_confidence", 0.0),
            "llm_advisory_only": True,
        }

    @staticmethod
    def _search_result_needs_llm_knowledge_fallback(search_result: dict[str, Any]) -> bool:
        if search_result.get("used_llm_knowledge_fallback"):
            return False
        evidence_quality = str(search_result.get("evidence_quality") or "").strip().lower()
        source_confidence = ApprovalFlowMixin._float_confidence(search_result.get("source_confidence"))
        return bool(
            search_result.get("need_manual_review", False)
            and (not search_result.get("raw_text") or evidence_quality in {"", "none", "low"} or source_confidence < 0.7)
        )

    @staticmethod
    def _rule_summary_for_llm(rule_engine: RuleEngine) -> list[dict[str, str]]:
        summary: list[dict[str, str]] = []
        rules_by_category = {rule.category: rule for rule in getattr(rule_engine, "rules", [])}
        for category in getattr(rule_engine, "priority", []) or []:
            rule = rules_by_category.get(category)
            if not rule:
                continue
            explanation = "；".join(rule.explanation_keywords[:12]) or rule.explanation[:300]
            examples = "；".join(rule.example_keywords[:12]) or rule.examples[:300]
            summary.append(
                {
                    "category": category,
                    "rule_keywords": explanation[:800],
                    "example_names": examples[:800],
                }
            )
        return summary

    def parallel_worker_count(self) -> int:
        approval_settings = self.settings.get("approval", {}) or {}
        value = os.getenv("APPROVAL_PARALLEL_WORKERS") or approval_settings.get("parallel_workers", 3)
        try:
            workers = int(value)
        except (TypeError, ValueError):
            workers = 3
        return max(1, min(8, workers))

    @classmethod
    def unique_work_items(
        cls,
        items: list[dict[str, Any]],
        *,
        key_func: Any,
        reuse_label: str,
    ) -> tuple[list[dict[str, Any]], dict[str, list[int]]]:
        unique_items: list[dict[str, Any]] = []
        index_groups: dict[str, list[int]] = {}
        for item in items:
            key = key_func(item)
            if key in index_groups:
                index_groups[key].append(item["index"])
                print(f"Reusing run {reuse_label} result for duplicate reagent: {item.get('progress', '')}")
                continue
            index_groups[key] = [item["index"]]
            unique_items.append(item)
        return unique_items, index_groups

    @classmethod
    def reagent_work_cache_key(cls, item: dict[str, Any]) -> str:
        reagent = item.get("reagent", {}) or {}
        parts = [
            cls._work_key_text(reagent.get("\u8bd5\u5242\u540d\u79f0", "")),
            cls.normalize_cas(str(reagent.get("CAS\u53f7", "") or "")),
            cls._work_key_text(reagent.get("\u89c4\u683c", "")),
            cls._work_key_text(reagent.get("\u89c4\u683c\u5355\u4f4d", "")),
        ]
        return "|".join(part.lower() for part in parts)

    @classmethod
    def reagent_llm_cache_key(cls, item: dict[str, Any]) -> str:
        reagent = item.get("reagent", {}) or {}
        search_result = item.get("search_result", {}) or {}
        name_result = item.get("name_result", {}) or {}
        parts = [
            cls.reagent_work_cache_key({"reagent": reagent}),
            cls._work_key_text(search_result.get("source", "")),
            cls._work_key_text(search_result.get("url", "")),
            cls._work_key_text(search_result.get("failure_reason", ""))[:200],
            cls._work_key_text(name_result.get("standard_name", "")),
            cls._work_key_text(name_result.get("cleaned_name", "")),
        ]
        return "|".join(part.lower() for part in parts)

    @classmethod
    def search_result_should_skip_llm(
        cls,
        search_result: dict[str, Any],
        name_result: dict[str, Any],
        reagent_name: str,
    ) -> bool:
        if search_result.get("product_model_identity_blocked", False):
            return True
        if not search_result.get("need_manual_review", False):
            return False
        if cls._search_has_trusted_web_evidence(search_result):
            return False
        return True

    @staticmethod
    def manual_review_name_should_skip_llm(text: str) -> bool:
        normalized = re.sub(r"[\s\-_()/\\\[\]{}（）]+", "", str(text or "").lower())
        if not normalized:
            return False
        if re.fullmatch(r"[a-z]{1,8}(?:[-_][a-z0-9]{1,16})+(?:\s+\d+(?:\.\d+){1,3})?", str(text or "").strip().lower()):
            return True
        return any(
            token in normalized
            for token in (
                "分散剂",
                "润湿剂",
                "润滑液",
                "机油",
                "油品",
                "树脂",
                "聚合物",
                "共聚物",
                "色浆",
                "涂料",
                "胶水",
                "清洗剂",
                "添加剂",
                "助剂",
                "耗材",
                "roll",
                "pvdf",
                "偶联剂tm",
                "偶联剂kh",
            )
        )

    @staticmethod
    def search_failure_result(reagent: dict[str, Any], reason: str) -> dict[str, Any]:
        name = str(reagent.get("\u8bd5\u5242\u540d\u79f0", "") or "").strip()
        cas = str(reagent.get("CAS\u53f7", "") or "").strip()
        return {
            "name": name,
            "cas": cas,
            "source": "",
            "url": "",
            "raw_text": reason,
            "hazard_keywords": [],
            "need_manual_review": True,
            "name_normalization": {
                "raw_name": name,
                "cleaned_name": name,
                "standard_name": name,
                "cas": cas,
                "confidence": 0.0,
                "need_manual_review": True,
                "reason": reason,
            },
            "query": "",
            "matched_site_name": "",
            "name_similarity": 0.0,
            "relevance_passed": False,
            "source_confidence": 0.0,
            "evidence_quality": "none",
            "failure_reason": reason,
            "fallback_source": "",
            "fallback_url": "",
            "used_llm_search_candidates": False,
            "llm_search_candidates": [],
        }

    def empty_extraction_and_classification(
        self,
        reagent: dict[str, Any],
        search_result: dict[str, Any],
        rule_engine: RuleEngine,
        reason: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extracted = {
            "name": reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
            "cas": reagent.get("CAS\u53f7", ""),
            "flash_point": "",
            "boiling_point": "",
            "toxicity": "",
            "corrosive": None,
            "oxidizing": None,
            "flammable": None,
            "water_reactive": None,
            "explosive_risk": None,
            "heavy_metal": None,
            "suggested_categories": [],
            "evidence": [f"LLM/classification failed: {reason}"],
            "confidence": 0.0,
        }
        classification = rule_engine.classify(self._classification_input(reagent, search_result, extracted))
        return extracted, classification

    @staticmethod
    def reagent_work_key(reagent: dict[str, Any]) -> str:
        parts = [
            ApprovalFlowMixin._work_key_text(reagent.get("\u5e8f\u53f7", "")),
            ApprovalFlowMixin._work_key_text(reagent.get("\u8bd5\u5242\u540d\u79f0", "")),
            ApprovalFlowMixin.normalize_cas(ApprovalFlowMixin._work_key_text(reagent.get("CAS\u53f7", ""))),
            ApprovalFlowMixin._work_key_text(reagent.get("\u89c4\u683c", "")),
            ApprovalFlowMixin._work_key_text(reagent.get("\u89c4\u683c\u5355\u4f4d", "")),
        ]
        return "|".join(parts)

    @staticmethod
    def _work_key_text(value: Any) -> str:
        return re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", str(value or "").strip())

    def direct_business_rule_suggestion(
        self,
        reagent: dict[str, str],
        rule_engine: RuleEngine,
    ) -> dict[str, Any] | None:
        reagent_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "")
        basic_suggestion = self.basic_reagent_rule_suggestion(reagent)
        if basic_suggestion:
            return basic_suggestion

        # V1 can safely classify a clearly named *elemental* heavy metal before
        # web lookup.  This does not apply to salts, oxides, alloys or products.
        if self._current_pipeline_version() == "v1":
            elemental_heavy_metal = rule_engine.explicit_heavy_metal_element_rule({
                "reagent_name": reagent_name,
                "name": reagent_name,
                "standard_name": reagent_name,
                "cleaned_name": reagent_name,
            })
            if elemental_heavy_metal:
                suggestion = self._direct_business_suggestion(
                    reagent,
                    reagent_name,
                    elemental_heavy_metal,
                    str(elemental_heavy_metal["reason"]),
                )
                suggestion.update({
                    "审批链路": "v1",
                    "查询来源": "v1_local_rule",
                    "证据质量": "v1_local_rule",
                    "V1本地规则来源": "elemental_heavy_metal",
                    "身份验证状态": "v1_local_elemental_heavy_metal",
                    "身份判定依据": "v1_local_rule",
                })
                return suggestion

        # Evaluate explicit hazards before any broad product/kit/ordinary shortcut.
        # A hazardous name continues through the evidence lookup path instead of
        # being silently downgraded to ordinary.
        name_classification = rule_engine.classify(
            {
                "reagent_name": reagent_name,
                "name": reagent_name,
                "standard_name": reagent_name,
                "cleaned_name": reagent_name,
                "text": reagent_name,
            }
        )
        if name_classification.get("final_category") not in {"", "普通类", UNKNOWN_CATEGORY}:
            return None

        ambiguous_acid_reason = self.ambiguous_acid_reason(reagent_name)
        if ambiguous_acid_reason:
            classification = {
                "final_category": "\u5e38\u89c4\u9178",
                "matched_categories": ["\u5e38\u89c4\u9178"],
                "reason": ambiguous_acid_reason,
                "confidence": 1.0,
                "need_manual_review": False,
            }
            return self._direct_business_suggestion(
                reagent,
                reagent_name,
                classification,
                ambiguous_acid_reason,
            )

        kit_reason = self.product_kit_normal_reason(reagent_name)
        if kit_reason:
            classification = {
                "final_category": "\u666e\u901a\u7c7b",
                "matched_categories": ["\u666e\u901a\u7c7b"],
                "reason": kit_reason,
                "confidence": 0.9,
                "need_manual_review": False,
            }
            return self._direct_business_suggestion(reagent, reagent_name, classification, kit_reason)

        classification = name_classification
        if classification.get("final_category") != "\u666e\u901a\u7c7b":
            return None
        if float(classification.get("confidence") or 0.0) < 0.9:
            return None
        return self._direct_business_suggestion(
            reagent,
            reagent_name,
            classification,
            str(classification.get("reason") or "\u547d\u4e2d\u666e\u901a\u7c7b\u4e1a\u52a1\u89c4\u5219"),
        )

    def _current_pipeline_version(self) -> str:
        return "v1"

    @staticmethod
    def v1_primary_component_profile(reagent_name: str) -> dict[str, str] | None:
        """Extract a named principal material from simple dilution/dispersion labels."""
        raw = str(reagent_name or "").strip()
        if not raw:
            return None
        working = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", raw).strip()
        modifiers: list[str] = []
        prefix = re.match(r"^(?:稀释|稀释了?)\s*(\d+(?:\.\d+)?)\s*倍\s*", working, flags=re.I)
        if prefix:
            modifiers.append(f"稀释{prefix.group(1)}倍")
            working = working[prefix.end():].strip()
        suffix = re.search(r"(?:水性)?(分散液|悬浊液|乳液|溶液)$", working, flags=re.I)
        if suffix:
            modifiers.append(suffix.group(0))
            working = working[:suffix.start()].strip(" -_，,；;")
        # A named carrier solvent is a material hazard clue, not a removable
        # formulation suffix.  Let the normal lookup/rule path inspect it.
        if re.search(r"(?:乙醇|甲醇|异丙醇|丙酮|乙腈|dmf|二甲基甲酰胺|四氢呋喃|thf)$", working, flags=re.I):
            return None
        if not modifiers or len(working) < 2:
            return None
        return {"raw_name": raw, "primary_component": working, "modifiers": "、".join(modifiers)}

    def v1_primary_component_classification(
        self,
        reagent: dict[str, str],
        search_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
        rule_engine: RuleEngine,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Apply a deterministic principal-component rule after known hazards win."""
        if self._current_pipeline_version() != "v1":
            return classification, search_result
        profile = self.v1_primary_component_profile(reagent.get("试剂名称", ""))
        if not profile:
            return classification, search_result
        # Existing extracted evidence and any non-ordinary rule result are
        # authoritative.  A name shortcut must never downgrade a known hazard.
        current_category = str(classification.get("final_category") or "").strip()
        confirmed_hazard = current_category not in {"", UNKNOWN_CATEGORY, NORMAL_CATEGORY}
        confirmed_hazard = confirmed_hazard or any(
            extracted.get(field) is True
            for field in ("corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal")
        )
        if confirmed_hazard:
            updated = dict(search_result)
            updated["primary_component"] = profile["primary_component"]
            updated["primary_component_modifiers"] = profile["modifiers"]
            updated["primary_component_hazard_overridden"] = True
            return classification, updated
        component_result = rule_engine.classify({
            "reagent_name": profile["primary_component"],
            "name": profile["primary_component"],
            "standard_name": profile["primary_component"],
            "cleaned_name": profile["primary_component"],
            "text": profile["primary_component"],
            "allow_default_normal": False,
        })
        if component_result.get("final_category") not in rule_engine.priority or component_result.get("need_manual_review", True):
            return classification, search_result
        updated = dict(search_result)
        updated.update({
            "primary_component": profile["primary_component"],
            "primary_component_modifiers": profile["modifiers"],
            "primary_component_rule_ids": list(component_result.get("matched_rule_ids") or []),
            "primary_component_hazard_overridden": False,
            "source": "v1_local_primary_component",
            "source_confidence": 1.0,
            "evidence_quality": "v1_local_rule",
            "relevance_passed": True,
            "need_manual_review": False,
            "identity_status": "v1_local_primary_component",
            "identity_decision_basis": "v1_local_rule",
            "llm_advisory_only": False,
        })
        component_result = dict(component_result)
        component_result["reason"] = (
            f"按名称主成分“{profile['primary_component']}”判定；已剥离修饰：{profile['modifiers']}。"
            f" {component_result.get('reason') or ''}"
        ).strip()
        component_result["confidence"] = max(float(component_result.get("confidence") or 0.0), 0.95)
        component_result["need_manual_review"] = False
        return component_result, updated

    def basic_reagent_rule_suggestion(self, reagent: dict[str, str]) -> dict[str, Any] | None:
        reagent_name = str(reagent.get("\u8bd5\u5242\u540d\u79f0", "") or "").strip()
        cas = self.normalize_cas(str(reagent.get("CAS\u53f7", "") or ""))
        profile = self.basic_reagent_profile(cas=cas, reagent_name=reagent_name)
        if not profile:
            return None
        category = str(profile["category"])
        standard_name = str(profile["standard_name"])
        profile_cas = str(profile.get("cas") or "").strip()
        cas_conflict = bool(profile.get("cas_name_conflict"))
        if cas_conflict:
            reason = str(
                profile.get("cas_correction_reason")
                or f"ERP CAS {cas} 与明确试剂名称 {reagent_name} 不匹配；按名称修正为 {standard_name}。"
            )
        else:
            display_cas = profile_cas or cas
            reason = (
                f"命中高频基础试剂本地规则：{standard_name}"
                f"{f' / CAS {display_cas}' if display_cas and display_cas != '-' else ''}"
                f"，按稳定审批规则判定为 {category}。"
            )
        classification = {
            "final_category": category,
            "matched_categories": [category],
            "reason": reason,
            "confidence": 1.0,
            "need_manual_review": False,
        }
        suggestion = self._direct_business_suggestion(reagent, standard_name, classification, reason)
        suggestion["\u67e5\u8be2\u6765\u6e90"] = "local_basic_reagent_rule"
        suggestion["\u8d44\u6599\u53ef\u4fe1\u5ea6"] = 1.0
        suggestion["\u8bc1\u636e\u8d28\u91cf"] = "local_basic_reagent_rule"
        suggestion["\u6807\u51c6\u5316\u540d\u79f0"] = standard_name
        suggestion["\u6e05\u6d17\u540e\u540d\u79f0"] = standard_name
        suggestion["\u89c4\u5219\u539f\u56e0"] = reason
        suggestion["\u7f6e\u4fe1\u5ea6"] = 1.0
        suggestion["\u9700\u4eba\u5de5\u590d\u6838"] = False
        if profile_cas:
            suggestion["CAS\u53f7"] = profile_cas
        if cas_conflict:
            suggestion["\u539fERP CAS\u53f7"] = cas
            suggestion["\u4fee\u6b63CAS\u53f7"] = profile_cas
            suggestion["CAS\u540d\u79f0\u51b2\u7a81"] = True
            suggestion["CAS\u4fee\u6b63\u5019\u9009"] = True
            suggestion["\u8eab\u4efd\u5224\u5b9a\u4f9d\u636e"] = "\u540d\u79f0\u8eab\u4efd"
            suggestion["CAS\u4fee\u6b63\u5df2\u5e94\u7528"] = True
            suggestion["CAS\u4fee\u6b63\u539f\u56e0"] = reason
            suggestion["CAS\u4fee\u6b63\u6765\u6e90"] = "local_basic_reagent_rule"
        return suggestion

    @classmethod
    def basic_reagent_profile(cls, *, cas: str = "", reagent_name: str = "") -> dict[str, Any] | None:
        by_cas: dict[str, dict[str, str]] = {
            "67-64-1": {"standard_name": "丙酮", "category": "易燃类", "cas": "67-64-1"},
            "64-17-5": {"standard_name": "乙醇", "category": "易燃类", "cas": "64-17-5"},
            "67-56-1": {"standard_name": "甲醇", "category": "易燃类", "cas": "67-56-1"},
            "60-29-7": {"standard_name": "乙醚", "category": "易燃类", "cas": "60-29-7"},
            "8032-32-4": {"standard_name": "石油醚", "category": "易燃类", "cas": "8032-32-4"},
            "7647-01-0": {"standard_name": "盐酸", "category": "常规酸", "cas": "7647-01-0"},
            "7664-93-9": {"standard_name": "硫酸", "category": "常规酸", "cas": "7664-93-9"},
            "7697-37-2": {"standard_name": "硝酸", "category": "常规酸", "cas": "7697-37-2"},
            "1310-73-2": {"standard_name": "氢氧化钠", "category": "常规碱", "cas": "1310-73-2"},
            "1336-21-6": {"standard_name": "氨水", "category": "常规碱", "cas": "1336-21-6"},
        }
        normalized_name = re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", str(reagent_name or "").lower())
        by_name: dict[str, dict[str, str]] = {
            "丙酮": {"standard_name": "丙酮", "category": "易燃类", "cas": "67-64-1"},
            "acetone": {"standard_name": "丙酮", "category": "易燃类", "cas": "67-64-1"},
            "乙醇": {"standard_name": "乙醇", "category": "易燃类", "cas": "64-17-5"},
            "无水乙醇": {"standard_name": "乙醇", "category": "易燃类", "cas": "64-17-5"},
            "ethanol": {"standard_name": "乙醇", "category": "易燃类", "cas": "64-17-5"},
            "甲醇": {"standard_name": "甲醇", "category": "易燃类", "cas": "67-56-1"},
            "methanol": {"standard_name": "甲醇", "category": "易燃类", "cas": "67-56-1"},
            "乙醚": {"standard_name": "乙醚", "category": "易燃类", "cas": "60-29-7"},
            "ether": {"standard_name": "乙醚", "category": "易燃类", "cas": "60-29-7"},
            "石油醚": {"standard_name": "石油醚", "category": "易燃类", "cas": "8032-32-4"},
            "盐酸": {"standard_name": "盐酸", "category": "常规酸", "cas": "7647-01-0"},
            "hydrochloricacid": {"standard_name": "盐酸", "category": "常规酸", "cas": "7647-01-0"},
            "hcl": {"standard_name": "盐酸", "category": "常规酸", "cas": "7647-01-0"},
            "硫酸": {"standard_name": "硫酸", "category": "常规酸", "cas": "7664-93-9"},
            "sulfuricacid": {"standard_name": "硫酸", "category": "常规酸", "cas": "7664-93-9"},
            "硝酸": {"standard_name": "硝酸", "category": "常规酸", "cas": "7697-37-2"},
            "nitricacid": {"standard_name": "硝酸", "category": "常规酸", "cas": "7697-37-2"},
            "氢氧化钠": {"standard_name": "氢氧化钠", "category": "常规碱", "cas": "1310-73-2"},
            "naoh": {"standard_name": "氢氧化钠", "category": "常规碱", "cas": "1310-73-2"},
            "氨水": {"standard_name": "氨水", "category": "常规碱", "cas": "1336-21-6"},
            "ammoniawater": {"standard_name": "氨水", "category": "常规碱", "cas": "1336-21-6"},
            "ammoniumhydroxide": {"standard_name": "氨水", "category": "常规碱", "cas": "1336-21-6"},
        }
        name_profile = dict(by_name[normalized_name]) if normalized_name in by_name else None
        normalized_cas = cls.normalize_cas(cas)
        cas_profile = dict(by_cas[normalized_cas]) if normalized_cas in by_cas else None
        if name_profile and cas_profile and cls.normalize_cas(str(name_profile.get("cas") or "")) != normalized_cas:
            name_profile["cas_name_conflict"] = True
            name_profile["original_erp_cas"] = normalized_cas
            name_profile["corrected_cas"] = name_profile.get("cas", "")
            name_profile["cas_correction_reason"] = (
                f"ERP CAS {normalized_cas} 对应 {cas_profile.get('standard_name')}，"
                f"但试剂名称明确为 {name_profile.get('standard_name')}；"
                f"按高频基础试剂名称修正 CAS 为 {name_profile.get('cas')}，"
                f"并判定为 {name_profile.get('category')}。"
            )
            return name_profile
        return cas_profile or name_profile

    @staticmethod
    def ambiguous_acid_reason(reagent_name: str) -> str:
        normalized = str(reagent_name or "").replace(" ", "").lower()
        if not normalized:
            return ""
        if RuleEngine._is_mineral_acid_salt_like(
            {
                "reagent_name": reagent_name,
                "standard_name": reagent_name,
                "cleaned_name": reagent_name,
            }
        ):
            return ""
        has_uncertain_prefix = any(token in normalized for token in ("\u7591\u4f3c", "\u53ef\u80fd", "\u6216", "/"))
        has_common_acid = "\u786b\u9178" in normalized or "\u78f7\u9178" in normalized
        if has_uncertain_prefix and has_common_acid:
            return "\u8bd5\u5242\u540d\u79f0\u5305\u542b\u201c\u7591\u4f3c/\u53ef\u80fd/\u6216\u201d\u4e14\u6307\u5411\u786b\u9178\u6216\u78f7\u9178\uff0c\u6309\u4e1a\u52a1\u89c4\u5219\u76f4\u63a5\u5224\u5b9a\u4e3a\u5e38\u89c4\u9178\u3002"
        return ""

    @staticmethod
    def product_kit_normal_reason(reagent_name: str) -> str:
        normalized = str(reagent_name or "").replace(" ", "").lower()
        tokens = (
            "\u8bd5\u5242\u76d2",
            "\u7eaf\u5316\u8bd5\u5242",
            "\u7f13\u51b2\u6761",
            "\u6bd4\u8272\u6db2",
            "\u5e95\u7269",
            "geneclean",
            "spin",
            "dna\u7eaf\u5316",
            "rna\u7eaf\u5316",
            "hydranal",
            "water-std",
            "waterstd",
            "tmb",
            "substrate",
            "minitrap",
            "pdmnitrap",
            "pdminitrap",
            "bufferstrips",
            "bufferstrip",
            "excelgel",
            "gelbuffer",
            "kit",
        )
        if any(token in normalized for token in tokens) and not any(
            risk_token in normalized for risk_token in ("\u53e0\u6c2e", "\u53e0\u5316", "\u9ad8\u6c2f\u9178", "azide")
        ):
            return "\u8bd5\u5242\u540d\u79f0\u547d\u4e2d\u8bd5\u5242\u76d2/\u6807\u51c6\u6db2/\u5546\u54c1\u8bd5\u5242\u4e1a\u52a1\u89c4\u5219\uff0c\u6309\u666e\u901a\u7c7b\u5904\u7406\u3002"
        return ""

    def _direct_business_suggestion(
        self,
        reagent: dict[str, str],
        reagent_name: str,
        classification: dict[str, Any],
        name_reason: str,
    ) -> dict[str, Any]:

        name_result = {
            "standard_name": reagent_name,
            "cleaned_name": reagent_name,
            "confidence": classification.get("confidence", 0.9),
            "need_manual_review": False,
            "reason": name_reason,
        }
        search_result = {
            "name": reagent_name,
            "cas": reagent.get("CAS\u53f7", ""),
            "source": "business_rule",
            "url": "",
            "raw_text": "",
            "need_manual_review": False,
            "relevance_passed": True,
            "source_confidence": classification.get("confidence", 0.9),
            "evidence_quality": "business_rule",
            "name_normalization": name_result,
        }
        extracted = {
            "suggested_categories": [classification.get("final_category", "")],
            "evidence": [classification.get("reason", "")],
            "confidence": 0.95,
        }
        return self._approval_suggestion_row(reagent, name_result, search_result, extracted, classification)

    def reagent_memory_suggestion(
        self,
        reagent: dict[str, str],
        memory_row: dict[str, Any],
        name_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        confidence = float(memory_row.get("confidence") or 0.0)
        final_category = str(memory_row.get("final_category") or "").strip()
        memory_url = str(memory_row.get("url") or "").strip()
        erp_cas = str(reagent.get("CAS\u53f7", "") or "").strip()
        authoritative_cas = str(memory_row.get("cas") or "").strip() or erp_cas
        suggestion_reagent = dict(reagent)
        name_result = dict(name_result or {})
        name_result.setdefault("raw_name", reagent.get("\u8bd5\u5242\u540d\u79f0", ""))
        name_result.setdefault("cleaned_name", memory_row.get("cleaned_name") or reagent.get("\u8bd5\u5242\u540d\u79f0", ""))
        name_result.setdefault("standard_name", memory_row.get("standard_name") or reagent.get("\u8bd5\u5242\u540d\u79f0", ""))
        name_result["cas"] = authoritative_cas
        name_result.setdefault("confidence", confidence)
        name_result["need_manual_review"] = False
        name_result.setdefault(
            "reason",
            "Matched a reusable local reagent memory record before chemical website lookup.",
        )

        reason = str(memory_row.get("reason") or "Matched reusable local reagent memory.").strip()
        search_result = {
            "name": memory_row.get("standard_name") or reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
            "cas": authoritative_cas,
            "source": "reagent_memory",
            "url": memory_url,
            "raw_text": reason,
            "hazard_keywords": [],
            "need_manual_review": False,
            "relevance_passed": True,
            "source_confidence": confidence,
            "evidence_quality": "local_memory",
            "name_normalization": name_result,
            "matched_site_name": memory_row.get("standard_name") or memory_row.get("raw_name") or "",
            "name_similarity": 1.0,
            "identity_status": "name_verified",
            "identity_decision_basis": "name_identity",
            "original_erp_cas": "",
            "corrected_cas": "",
            "cas_name_conflict": False,
            "cas_correction_applied": False,
        }
        extracted = {
            "suggested_categories": [final_category] if final_category else [],
            "evidence": [reason],
            "confidence": confidence,
        }
        classification = {
            "final_category": final_category,
            "matched_categories": [final_category] if final_category else [],
            "reason": f"本地高可信试剂记忆库命中：{reason}",
            "confidence": confidence,
            "need_manual_review": False,
            "unknown_name_rule": bool(
                unknown_reagent_name_reason(
                    reagent.get("试剂名称", ""),
                    memory_row.get("cleaned_name", ""),
                    memory_row.get("standard_name", ""),
                )
            ),
        }
        suggestion = self._approval_suggestion_row(suggestion_reagent, name_result, search_result, extracted, classification)
        # ERP-verified records retain their original formal rule provenance in
        # the free-text audit reason.  Restore those identifiers on a memory
        # reuse so terminal reject decisions can pass the same audited-write
        # gate as the source record; model-only or ordinary web-memory rows
        # never gain that privilege.
        if str(memory_row.get("source") or "").strip().lower() == "verified_erp_write":
            rule_ids = self.memory_formal_rule_ids(memory_row)
            if rule_ids:
                suggestion["命中规则ID"] = ", ".join(rule_ids)
                suggestion["试剂记忆库正式规则复用"] = True
        if self._truthy(memory_row.get("_memory_solution_reuse")):
            primary_component = str(memory_row.get("_memory_solution_primary_component") or "").strip()
            suggestion["主成分判定"] = primary_component
            suggestion["试剂记忆库水溶液复用"] = True
            suggestion["规则原因"] = (
                f"{suggestion.get('规则原因') or ''}\n"
                f"按 V1 水溶液复用口径，使用已验证纯品“{primary_component}”的判定结论。"
            ).strip()
        return suggestion

    @staticmethod
    def memory_formal_rule_ids(memory_row: dict[str, Any]) -> list[str]:
        if str(memory_row.get("source") or "").strip().lower() != "verified_erp_write":
            return []
        reason = str(memory_row.get("reason") or "")
        # Prefer the explicitly persisted list.  This permits a later rule
        # migration to supersede legacy evidence identifiers without granting
        # an obsolete threshold rule new write authority.
        explicit = re.search(r"正式规则ID\s*[:：]\s*([^\n]+)", reason)
        source_text = explicit.group(1) if explicit else reason
        # Workbook and threshold rule IDs use uppercase segments separated by
        # hyphens, e.g. BUS-TOX-ACUTE-CAT3-001 or T-ACU-DERM-001.
        return list(dict.fromkeys(re.findall(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,}\b", source_text)))

    @staticmethod
    def extract_cas_from_url(url: str) -> str:
        match = re.search(r"(?<!\d)(\d{2,7}-\d{2}-\d)(?!\d)", str(url or ""))
        return match.group(1) if match else ""

    @staticmethod
    def normalize_cas(cas: str) -> str:
        return re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", str(cas or "").strip())

    def apply_approval_write_mode(self, page: Page, suggestions: list[dict[str, Any]]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {
            "attempted": set(),
            "handled": set(),
            "failed": set(),
            "eligible": set(),
            "deferred": set(),
        }
        min_confidence = self.approval_write_min_confidence()
        page_summary = summarize_approval_suggestions(
            suggestions,
            min_confidence=min_confidence,
            category_resolver=lambda category: to_erp_property(category, self.settings),
        )
        print(f"Page suggestion summary: {format_suggestion_summary(page_summary)}")
        if self.dry_run_enabled():
            print("Dry-run mode is enabled; no webpage fields, saves, approvals, or library generation will be changed.")
            result["handled"].update(self.suggestion_work_key(suggestion) for suggestion in suggestions)
            return result

        mode = self.approval_write_mode()
        if mode == "disabled":
            print("Approval write mode is disabled; no webpage fields will be changed.")
            result["handled"].update(self.suggestion_work_key(suggestion) for suggestion in suggestions)
            return result

        # A source-verified candidate CAS is retained only in the local identity
        # memory.  It never modifies the ERP CAS field.
        self.remember_verified_v1_candidate_identities(suggestions)

        candidates = self.high_confidence_write_candidates(suggestions)
        candidate_keys = {self.suggestion_work_key(suggestion) for suggestion in candidates}
        result["eligible"].update(candidate_keys)
        self.queue_low_confidence_write_skips(suggestions, min_confidence=min_confidence)
        result["handled"].update(
            self.suggestion_work_key(suggestion)
            for suggestion in suggestions
            if self.suggestion_work_key(suggestion) not in candidate_keys
        )
        if not candidates:
            print("No high-confidence approval suggestion is eligible for webpage writing.")
            return result

        if mode in {"test_one", "save_one"}:
            candidates = candidates[:1]
        elif mode in {"single_page", "generate_library"}:
            pass
        elif mode == "multi_page":
            batch_size = self.approval_write_batch_size()
            print(
                "Multi-page write mode uses serial per-row transactions; "
                f"{min(batch_size, len(candidates))} of {len(candidates)} writable candidate(s) "
                "will be saved before re-reading the page."
            )
            deferred_candidates = candidates[batch_size:]
            result["deferred"].update(self.suggestion_work_key(suggestion) for suggestion in deferred_candidates)
            candidates = candidates[:batch_size]
        else:
            print(f"Unknown APPROVAL_WRITE_MODE={mode}; no webpage fields will be changed.")
            return result

        writer = ApprovalWriter(settings=self.settings)
        write_backend = self.erp_write_backend()
        discovery = self.erp_api_discovery_settings()
        discovery_status = str(discovery.get("status") or "pending_capture")
        canary_pending = (
            discovery.get("enabled") is True
            and discovery_status == "pending_canary"
            and discovery.get("canary_on_next_run", True) is True
        )
        canary_attempted = False
        api_configurator = ErpApiConfigurator(self.root_dir)
        if discovery.get("enabled") is True and discovery_status == "pending_capture":
            write_backend = "web_ui"
            print("ERP API discovery is collecting verified normal saves; this task continues with webpage writes.")
        api_client: ErpApiClient | None = None
        api_detail_records: list[dict[str, Any]] | None = None
        api_status_logged = False
        if write_backend != "web_ui":
            api_client = ErpApiClient(page, self.settings)
            status = api_client.configuration_status()
            missing = ", ".join(status.get("missing") or [])
            if write_backend == "api_write_with_web_verify" and not status.get("write_enabled"):
                write_backend = "api_read_web_write"
                print(
                    "ERP API save protocol is not verified; using API reads with webpage writes. "
                    f"discovery_status={status.get('discovery_status') or 'disabled'}"
                )
            elif status.get("configured"):
                print(
                    f"ERP write backend is {write_backend}; API endpoints are configured; "
                    "webpage writer remains available as fallback."
                )
            else:
                print(
                    f"ERP write backend is {write_backend}, but API is not fully configured "
                    f"({missing or 'unknown missing fields'}); webpage writer remains available as fallback."
                )
            api_status_logged = True
        consecutive_failures = 0
        for row_index, suggestion in enumerate(candidates, start=1):
            sequence = str(suggestion.get("\u5e8f\u53f7") or "").strip()
            rule_category = str(
                suggestion.get("\u89c4\u5219\u5224\u5b9a\u7c7b\u522b")
                or suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b")
                or ""
            ).strip()
            category = to_erp_property(rule_category, self.settings) or str(
                suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b") or ""
            ).strip()
            reagent_name = str(suggestion.get("\u8bd5\u5242\u540d\u79f0") or "").strip()
            cas = str(suggestion.get("CAS\u53f7") or "").strip()
            work_key = self.suggestion_work_key(suggestion)
            result["attempted"].add(work_key)
            if category != rule_category:
                print(
                    f"Approval write candidate {row_index}/{len(candidates)}: "
                    f"{sequence} {reagent_name} -> {rule_category} / ERP {category}"
                )
            else:
                print(f"Approval write candidate {row_index}/{len(candidates)}: {sequence} {reagent_name} -> {category}")

            # In multi-page mode, ERP saves can re-render or re-sort the table.
            # Keep retries at the page-loop level so each retry starts from a
            # freshly read current page instead of stale row locators.
            write_attempt_limit = 1
            last_failure_detail = ""
            verified = False
            saved = False
            save_outcome_unknown = False
            row = None

            if write_backend in {"api_read_web_write", "api_write_with_web_verify"} and api_client is not None:
                if not api_status_logged:
                    status = api_client.configuration_status()
                    print(f"ERP API configuration status: {status}")
                    api_status_logged = True
                try:
                    if api_detail_records is None:
                        list_number = self.current_detail_list_number()
                        if not list_number:
                            list_number = str(suggestion.get("试剂清单号") or suggestion.get("清单号") or "").strip()
                        api_detail_records = api_client.fetch_reagent_detail(list_number)
                        print(
                            f"[api_read_detail] list={list_number or '<unknown>'} "
                            f"records={len(api_detail_records)}"
                        )
                    record_id, _api_record = api_client.resolve_reagent_record_id(suggestion, api_detail_records)
                    suggestion["_erp_record_id"] = record_id
                    suggestion["_erp_record"] = _api_record
                    print(f"[api_record_resolve] sequence={sequence} record_id={record_id}")
                except Exception as error:
                    print(f"[api_record_resolve] sequence={sequence} failed={error}")


            if write_backend == "api_write_with_web_verify":
                is_canary = canary_pending and not canary_attempted
                api_result = (api_client or ErpApiClient(page, self.settings)).save_physicochemical_property(
                    suggestion,
                    category,
                    allow_canary=is_canary,
                )
                canary_attempted = canary_attempted or is_canary
                if api_result.attempted:
                    print(
                        f"[api_property_save] sequence={sequence} record_id={api_result.record_id or '-'} "
                        f"saved={api_result.saved} verified={api_result.verified} detail={api_result.detail}"
                    )
                else:
                    print(f"[api_property_save] sequence={sequence} skipped={api_result.detail}")
                if api_result.saved:
                    try:
                        verified = (api_client or ErpApiClient(page, self.settings)).confirm_property_twice_by_id(
                            str(api_result.record_id or suggestion.get("_erp_record_id") or ""), category, suggestion
                        )
                    except Exception as error:
                        print(f"[api_property_verify] sequence={sequence} double_read_failed={error}")
                    if not verified:
                        last_failure_detail = "API saved, but two stable-record-ID readbacks did not confirm identity and property"
                        print(f"[api_property_verify] sequence={sequence} failed={last_failure_detail}")
                        result["failed"].add(work_key)
                        self.record_save_result(f"reagent_save_{sequence}", False, last_failure_detail)
                        self.add_manual_review_item_from_write_failure(suggestion, last_failure_detail)
                        continue
                    saved = True
                    print(f"[api_property_confirmed] sequence={sequence} record_id={api_result.record_id or suggestion.get('_erp_record_id')} category={category}")
                elif not api_result.fallback_to_web:
                    last_failure_detail = api_result.detail or "ERP API write failed without webpage fallback"
                    result["failed"].add(work_key)
                    self.record_save_result(f"reagent_save_{sequence}", False, last_failure_detail)
                    self.record_web_write_failure(suggestion, category, last_failure_detail)
                    self.add_manual_review_item_from_write_failure(suggestion, last_failure_detail)
                    continue
                else:
                    print(f"[api_fallback_web_write] sequence={sequence} reason={api_result.detail}")
                    if is_canary:
                        reason = f"API canary failed: {api_result.detail}"
                        api_configurator.reject(reason)
                        write_backend = "web_ui"
                        api_client = None
                        canary_pending = False
                        print("ERP API candidate was rejected and rolled back; continuing with webpage writes.")

            if verified:
                api_settings = self.settings.get("erp_api") or {}
                page_check_ok = True
                if (
                    write_backend == "api_write_with_web_verify"
                    and api_client is not None
                    and self.should_verify_api_page(row_index, is_canary, api_settings)
                ):
                    try:
                        page.reload(wait_until="domcontentloaded")
                        wait_until_spinner_hidden(page, timeout_ms=5000)
                    except Error as error:
                        print(f"[api_property_verify] sequence={sequence} reload_failed={error}")
                    page_value = self.read_reagent_property_by_sequence(page, sequence)
                    if not self.property_value_matches(page_value, category, writer):
                        list_number = str(suggestion.get("试剂清单号") or self.current_detail_list_number() or "").strip()
                        if list_number:
                            try:
                                if self.reopen_target_detail_for_recovery(page, list_number):
                                    page_value = self.read_reagent_property_by_sequence(page, sequence)
                            except Exception as error:
                                print(f"[api_property_verify] sequence={sequence} detail_reopen_failed={error}")
                    page_check_ok = self.property_value_matches(page_value, category, writer)
                    if not page_check_ok:
                        warning = "stable_id_confirmed_page_mismatch" if page_value else "stable_id_confirmed_page_not_located"
                        print(
                            f"[api_property_verify] sequence={sequence} warning={warning} "
                            f"page_value={page_value or '<not located>'}; no duplicate save"
                        )
                        result.setdefault("warnings", set()).add(work_key)
                    if page_value:
                        print(f"[api_property_verify] sequence={sequence} webpage_value={page_value}")
                if verified and is_canary and not page_check_ok:
                    api_configurator.reject("API canary readback passed, but webpage readback could not be confirmed")
                    write_backend = "web_ui"
                    api_client = None
                    canary_pending = False
                    print("ERP API canary was not activated; subsequent writes use webpage fallback.")
                if verified and is_canary and page_check_ok:
                    api_configurator.activate()
                    erp_api_settings = self.settings.setdefault("erp_api", {})
                    erp_api_settings["enabled_for_write"] = True
                    erp_api_settings.setdefault("discovery", {})["status"] = "verified"
                    if api_client is not None:
                        api_client.api_settings["enabled_for_write"] = True
                    canary_pending = False
                    print("ERP API canary passed API and webpage read-back checks; API writes are now active.")
            if verified:
                self.record_save_result(f"reagent_save_{sequence}", True, category)
                result["handled"].add(work_key)
                self.clear_web_write_failure(suggestion)
                if self.remember_verified_approval_suggestion(suggestion, category):
                    print(f"Stored verified ERP API approval result in reagent memory: {sequence} {reagent_name} -> {category}")
                continue

            for write_attempt in range(1, write_attempt_limit + 1):
                if write_attempt > 1:
                    print(f"Retrying approval write for sequence {sequence}, attempt {write_attempt}/{write_attempt_limit}.")
                if not self.clear_existing_edit_state(page, writer, sequence):
                    last_failure_detail = "could not clear existing edit row"
                    print(f"Could not clear an existing edit row before writing sequence: {sequence}")
                    self.capture_write_failure(
                        page, sequence, write_attempt, last_failure_detail, row=row, category=category, writer=writer
                    )
                    break
                writer.dismiss_open_dropdown(page)
                row = self.find_reagent_row_by_sequence(page, sequence, reagent_name, cas)
                if row is None:
                    last_failure_detail = "row not found"
                    print(f"Could not find current-page row for sequence: {sequence}")
                    break

                self.prepare_reagent_row_for_write(page, row)
                already_editing = writer.row_is_editing(page, row)
                if already_editing:
                    print(f"Sequence {sequence} is already in edit mode; continuing property selection.")
                    opened = True
                else:
                    opened = writer.open_technical_judgement(row, page)
                if not opened:
                    last_failure_detail = "technical judgement button not found"
                    print(f"Could not open technical judgement for sequence: {sequence}")
                    self.capture_write_failure(
                        page, sequence, write_attempt, last_failure_detail, row=row, category=category, writer=writer
                    )
                    self.cleanup_failed_write(page, writer, row, sequence, last_failure_detail)
                    continue

                wait_until_spinner_hidden(page, timeout_ms=3000)
                selected_value = ""
                selected = False
                for selection_attempt in range(1, 3):
                    if selection_attempt > 1:
                        print(
                            f"Retrying property dropdown selection for sequence {sequence}, "
                            f"attempt {selection_attempt}/2."
                        )
                    selected = writer.choose_property(page, category, row)
                    if not selected:
                        continue
                    selected_value = wait_until_row_value(
                        page,
                        lambda: self.read_reagent_property_by_sequence(page, sequence),
                        lambda value: bool(value.strip()) and value.strip() not in {"-", "选择搜索"},
                        timeout_ms=3000,
                    )
                    if self.property_value_matches(selected_value, category, writer):
                        break
                    if selected_value in {"", "-", "\u9009\u62e9\u641c\u7d22"}:
                        writer.dismiss_open_dropdown(page)
                        page.wait_for_timeout(250)
                        continue
                    break
                if not selected:
                    last_failure_detail = f"could not select {category}"
                    print(f"Could not select physicochemical property {category} for sequence: {sequence}")
                    self.capture_write_failure(
                        page, sequence, write_attempt, last_failure_detail, row=row, category=category, writer=writer
                    )
                    self.cleanup_failed_write(page, writer, row, sequence, last_failure_detail)
                    continue

                if not self.property_value_matches(selected_value, category, writer):
                    last_failure_detail = f"selected {category}, but row still shows {selected_value or '<empty>'}"
                    print(
                        f"Property selection verification failed for sequence {sequence}: "
                        f"expected {category}, got {selected_value or '<empty>'}."
                    )
                    self.capture_write_failure(
                        page, sequence, write_attempt, last_failure_detail, row=row, category=category, writer=writer
                    )
                    self.cleanup_failed_write(page, writer, row, sequence, last_failure_detail)
                    continue

                screenshot_path = self._log_dir() / f"write_mode_{mode}_{sequence}.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"Saved approval write screenshot before save: {screenshot_path}")

                if mode == "test_one":
                    print("Test write mode: selected value for inspection only; not saving.")
                    result["handled"].add(work_key)
                    return result

                recorder = getattr(self, "_erp_api_discovery_recorder", None)
                if recorder is not None:
                    recorder.begin_save_window(suggestion, category)
                try:
                    saved = writer.save(page, row)
                    wait_until_spinner_hidden(page, timeout_ms=5000)
                    saved_value = wait_until_row_value(
                        page,
                        lambda: self.read_reagent_property_by_sequence(page, sequence),
                        lambda value: self.property_value_matches(value, category, writer),
                        timeout_ms=5000,
                    )
                    verified = saved and self.property_value_matches(saved_value, category, writer)
                except Exception as error:
                    last_failure_detail = (
                        f"save outcome unknown after {type(error).__name__}: {error}"
                        if saved
                        else f"save operation failed before confirmation: {type(error).__name__}: {error}"
                    )
                    save_outcome_unknown = saved
                    self.capture_write_failure(
                        page, sequence, write_attempt, last_failure_detail, row=row, category=category, writer=writer
                    )
                    if not save_outcome_unknown:
                        self.cleanup_failed_write(page, writer, row, sequence, last_failure_detail)
                finally:
                    if recorder is not None:
                        recorder.end_save_window(verified)
                if save_outcome_unknown:
                    break
                print(f"Save verified for sequence {sequence}: {verified} (clicked_save={saved})")
                if verified:
                    break
                last_failure_detail = f"saved={saved}, row shows {saved_value or '<empty>'}"
                print(
                    f"Save verification failed for sequence {sequence}: "
                    f"expected {category}, got {saved_value or '<empty>'}."
                )
                self.capture_write_failure(
                    page, sequence, write_attempt, last_failure_detail, row=row, category=category, writer=writer
                )
                self.cleanup_failed_write(page, writer, row, sequence, last_failure_detail)

            if save_outcome_unknown:
                result["handled"].add(work_key)
                self.record_save_result(f"reagent_save_{sequence}", False, last_failure_detail)
                self.record_web_write_failure(suggestion, category, last_failure_detail)
                self.add_manual_review_item_from_write_failure(suggestion, last_failure_detail)
                print(
                    f"Save outcome is unknown for sequence {sequence}; automatic retry is disabled "
                    "and the current write round will stop for manual verification."
                )
                break

            self.record_save_result(
                f"reagent_save_{sequence}",
                verified,
                category if verified else last_failure_detail or f"could not select {category}",
            )
            if verified:
                result["handled"].add(work_key)
                self.clear_web_write_failure(suggestion)
                if self.remember_verified_approval_suggestion(suggestion, category):
                    print(f"Stored verified approval result in reagent memory: {sequence} {reagent_name} -> {category}")
                consecutive_failures = 0
                if not self.settle_after_successful_write(page, writer, sequence):
                    self.record_web_write_failure(
                        suggestion,
                        category,
                        "保存后页面仍处于编辑状态，程序无法确认该行已稳定完成。",
                    )
                    self.record_save_result(
                        f"page_settle_{sequence}",
                        False,
                        "ERP save was verified, but the page remained in an unstable edit state; "
                        "the saved reagent will not be retried automatically.",
                    )
                    print(
                        f"Page edit state did not settle after saving sequence {sequence}; "
                        "the verified save is terminal and the current write round will stop."
                    )
                    if mode == "multi_page":
                        break
            else:
                result["failed"].add(work_key)
                self.record_web_write_failure(
                    suggestion,
                    category,
                    last_failure_detail or f"could not write {category}",
                )
                self.add_manual_review_item_from_write_failure(
                    suggestion,
                    last_failure_detail or f"could not write {category}",
                )
                consecutive_failures += 1
                if mode == "multi_page":
                    print(
                        "Stopping this multi-page write batch after a failed webpage write; "
                        "the failed reagent was recorded and the page will be re-sorted/re-read."
                    )
                    if not self.stabilize_reagent_detail_after_write_failure(page):
                        print("Stopping this multi-page write round because the page could not be stabilized.")
                        break
                    break
                if consecutive_failures >= self.approval_write_failure_break_limit():
                    print("Stopping this write round after repeated save failures; the page will be re-read.")
                    break

            if mode == "generate_library" and verified and row is not None:
                generated = writer.generate_reagent_library(page, row)
                self.record_save_result(f"reagent_library_{sequence}", generated, category)
                print(f"Generate reagent library result for sequence {sequence}: {generated}")

        if mode == "save_one":
            return result

        return result

    def queue_low_confidence_write_skips(self, suggestions: list[dict[str, Any]], *, min_confidence: float) -> None:
        for suggestion in suggestions:
            self.apply_unknown_auto_write_policy(suggestion)
            rule_category = str(suggestion.get("最终建议类别") or "").strip()
            if self.is_terminal_reject_decision(rule_category):
                continue
            if is_non_writable_rule_category(rule_category, self.settings, getattr(self, "root_dir", None)):
                continue
            if write_skip_reason(
                suggestion,
                min_confidence=min_confidence,
                category_resolver=lambda category: to_erp_property(category, self.settings),
            ) != "low_confidence":
                continue
            self.add_manual_review_item_from_low_confidence_suggestion(suggestion, min_confidence)

    def approval_write_batch_size(self) -> int:
        approval_settings = getattr(self, "settings", {}).get("approval", {}) or {}
        value = os.getenv("APPROVAL_WRITE_BATCH_SIZE") or approval_settings.get("write_batch_size", 3)
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return 3

    def approval_write_min_confidence(self) -> float:
        approval_settings = getattr(self, "settings", {}).get("approval", {}) or {}
        value = os.getenv("APPROVAL_WRITE_MIN_CONFIDENCE") or approval_settings.get("write_min_confidence", 0.8)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.8

    def erp_write_backend(self) -> str:
        approval_settings = getattr(self, "settings", {}).get("approval", {}) or {}
        return normalize_erp_write_backend(
            os.getenv("ERP_WRITE_BACKEND") or approval_settings.get("erp_write_backend", "web_ui")
        )

    def record_web_write_failure(self, suggestion: dict[str, Any], category: str, reason: str) -> None:
        if getattr(self, "web_write_failures", None) is None:
            self.web_write_failures = []
        detail_info = getattr(self, "_current_detail_info", {}) or {}
        failure_key = self.web_write_failure_key(suggestion)
        self.web_write_failures = [
            item
            for item in self.web_write_failures
            if str(item.get("_failure_key") or "") != failure_key
        ]
        self.web_write_failures.append(
            {
                "_failure_key": failure_key,
                "试剂清单号": self.current_detail_list_number(),
                "序号": suggestion.get("序号", ""),
                "试剂名称": suggestion.get("试剂名称", ""),
                "CAS号": suggestion.get("CAS号", ""),
                "清洗后名称": suggestion.get("清洗后名称", ""),
                "标准化名称": suggestion.get("标准化名称", ""),
                "拟写入物化特性": category,
                "规则判定类别": suggestion.get("规则判定类别", ""),
                "最终建议类别": suggestion.get("最终建议类别", ""),
                "置信度": suggestion.get("置信度", ""),
                "需人工复核": suggestion.get("需人工复核", ""),
                "写入失败原因": self.natural_web_write_failure_reason(reason),
                "客户名称": detail_info.get("客户名称", ""),
                "申请人": detail_info.get("申请人", ""),
            }
        )

    def clear_web_write_failure(self, suggestion: dict[str, Any]) -> None:
        failures = getattr(self, "web_write_failures", None)
        if not failures:
            return
        failure_key = self.web_write_failure_key(suggestion)
        self.web_write_failures = [
            item
            for item in failures
            if str(item.get("_failure_key") or "") != failure_key
        ]

    def web_write_failure_key(self, suggestion: dict[str, Any]) -> str:
        return f"{self.current_detail_list_number()}::{self.suggestion_work_key(suggestion)}"

    def write_web_write_failures(self) -> Any:
        failures = getattr(self, "web_write_failures", None) or []
        output_path = self._log_dir() / "web_write_failures.xlsx"
        if not failures:
            try:
                if output_path.exists():
                    output_path.unlink()
                    print(f"Removed stale web write failures: {output_path}")
            except Exception as error:
                print(f"Could not remove stale web write failures: {error}")
            return None
        dataframe = pd.DataFrame(failures)
        if "_failure_key" in dataframe.columns:
            dataframe = dataframe.drop(columns=["_failure_key"])
        return self.write_excel_with_fallback(dataframe, output_path)

    @staticmethod
    def natural_web_write_failure_reason(reason: str) -> str:
        text = str(reason or "").strip()
        if not text:
            return "网页写入失败，程序没有拿到明确的失败原因。"
        if "could not select" in text:
            category = text.replace("could not select", "").strip()
            return f"程序打开了技术判定，但没有在物化特性下拉框中成功选中 {category}。"
        if "not_found_after_reread" in text:
            return (
                "程序生成了可写入建议，但多页重读后没有再次定位到该试剂行；"
                "需要核对是否已经由 ERP 自动更新或分页排序移动。"
            )
        if "row not found" in text:
            return "当前页没有重新定位到这条试剂，可能是保存后页面刷新、分页或排序变化导致。"
        if "technical judgement" in text:
            return "程序没有找到该行的“技术判定”入口，无法进入编辑状态。"
        if "row shows" in text:
            return f"程序尝试保存后，页面显示值与预期不一致：{text}。"
        if "edit" in text:
            return f"页面编辑状态没有正常恢复：{text}。"
        return text

    def add_manual_review_item_from_write_failure(self, suggestion: dict[str, Any], reason: str) -> None:
        reagent = {
            "\u5e8f\u53f7": suggestion.get("\u5e8f\u53f7", ""),
            "\u8bd5\u5242\u540d\u79f0": suggestion.get("\u8bd5\u5242\u540d\u79f0", ""),
            "CAS\u53f7": suggestion.get("CAS\u53f7", ""),
            "\u89c4\u683c": suggestion.get("\u89c4\u683c", ""),
            "\u89c4\u683c\u5355\u4f4d": suggestion.get("\u89c4\u683c\u5355\u4f4d", ""),
            "\u8bd5\u5242\u6570\u91cf": suggestion.get("\u8bd5\u5242\u6570\u91cf", ""),
        }
        name_result = {
            "standard_name": suggestion.get("\u6807\u51c6\u5316\u540d\u79f0", ""),
            "cleaned_name": suggestion.get("\u6e05\u6d17\u540e\u540d\u79f0", ""),
            "review_kind": "erp_write_verification",
            "expected_category": str(
                suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b")
                or suggestion.get("\u89c4\u5219\u5224\u5b9a\u7c7b\u522b")
                or ""
            ).strip(),
            "write_failure_reason": str(reason or "").strip(),
        }
        is_cas_fill_failure = "CAS 自动补齐" in str(reason or "") or "CAS update" in str(reason or "")
        manual_reason = (
            f"ERP CAS 自动补齐失败：{reason}。程序未继续写入物化特性，需人工核验 CAS 后重试。"
            if is_cas_fill_failure
            else f"网页写入失败：{reason}。程序没有确认物化特性已成功写入 ERP 行内，需要人工在网页端核对并处理该试剂。"
        )
        search_result = {
            "source": suggestion.get("候选CAS确认来源") or suggestion.get("查询来源") or "",
            "url": suggestion.get("候选CAS确认URL") or suggestion.get("查询URL") or "",
            "candidate_cas": suggestion.get("网页候选CAS") or "",
            "original_erp_cas": suggestion.get("原ERP CAS号") or suggestion.get("CAS号") or "",
            "candidate_cas_confirmation_source": suggestion.get("候选CAS确认来源") or "",
            "candidate_cas_confirmation_url": suggestion.get("候选CAS确认URL") or "",
            "erp_cas_fill_status": "failed" if is_cas_fill_failure else "",
            "erp_cas_fill_note": str(reason or "") if is_cas_fill_failure else "",
            "need_manual_review": True,
        }
        try:
            self.add_manual_review_item(reagent, name_result, reason=manual_reason, search_result=search_result)
        except TypeError as error:
            # Lightweight test and integration subclasses created before the
            # audit payload extension may expose the legacy three-argument hook.
            if "search_result" not in str(error):
                raise
            self.add_manual_review_item(reagent, name_result, reason=manual_reason)

    def add_manual_review_item_from_low_confidence_suggestion(
        self,
        suggestion: dict[str, Any],
        min_confidence: float,
    ) -> None:
        reagent = {
            "\u5e8f\u53f7": suggestion.get("\u5e8f\u53f7", ""),
            "\u8bd5\u5242\u540d\u79f0": suggestion.get("\u8bd5\u5242\u540d\u79f0", ""),
            "CAS\u53f7": suggestion.get("CAS\u53f7", ""),
            "\u89c4\u683c": suggestion.get("\u89c4\u683c", ""),
            "\u89c4\u683c\u5355\u4f4d": suggestion.get("\u89c4\u683c\u5355\u4f4d", ""),
            "\u8bd5\u5242\u6570\u91cf": suggestion.get("\u8bd5\u5242\u6570\u91cf", ""),
        }
        name_result = {
            "standard_name": suggestion.get("\u6807\u51c6\u5316\u540d\u79f0", ""),
            "cleaned_name": suggestion.get("\u6e05\u6d17\u540e\u540d\u79f0", ""),
            "english_name": suggestion.get("\u82f1\u6587\u540d\u79f0", ""),
            "cas": suggestion.get("CAS\u53f7", ""),
            "confidence": suggestion.get("\u540d\u79f0\u6807\u51c6\u5316\u7f6e\u4fe1\u5ea6", ""),
            "need_manual_review": False,
        }
        search_result = {
            "source": suggestion.get("\u67e5\u8be2\u6765\u6e90", ""),
            "url": suggestion.get("\u67e5\u8be2URL", ""),
            "cas": suggestion.get("CAS\u53f7", ""),
            "matched_site_name": suggestion.get("\u7f51\u7ad9\u5339\u914d\u540d\u79f0", ""),
            "name_similarity": suggestion.get("\u540d\u79f0\u76f8\u4f3c\u5ea6", ""),
            "relevance_passed": suggestion.get("\u67e5\u8be2\u76f8\u5173\u6027\u901a\u8fc7", False),
            "source_confidence": suggestion.get("\u8d44\u6599\u53ef\u4fe1\u5ea6", ""),
            "evidence_quality": suggestion.get("\u8bc1\u636e\u8d28\u91cf", ""),
            "failure_reason": suggestion.get("\u67e5\u8be2\u5931\u8d25\u539f\u56e0", ""),
            "need_manual_review": False,
        }
        extracted = {
            "flash_point": suggestion.get("\u95ea\u70b9", ""),
            "boiling_point": suggestion.get("\u6cb8\u70b9", ""),
            "toxicity": suggestion.get("\u6bd2\u6027", ""),
            "corrosive": suggestion.get("\u8150\u8680\u6027", ""),
            "oxidizing": suggestion.get("\u6c27\u5316\u6027", ""),
            "flammable": suggestion.get("\u6613\u71c3", ""),
            "water_reactive": suggestion.get("\u9047\u6c34\u53cd\u5e94", ""),
            "explosive_risk": suggestion.get("\u7206\u70b8\u98ce\u9669", ""),
            "heavy_metal": suggestion.get("\u91cd\u91d1\u5c5e", ""),
            "suggested_categories": [suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b", "")],
            "evidence": [suggestion.get("\u8bc1\u636e", "")] if suggestion.get("\u8bc1\u636e") else [],
        }
        classification = {
            "final_category": suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b", ""),
            "matched_categories": [
                item.strip()
                for item in str(suggestion.get("\u547d\u4e2d\u7c7b\u522b", "") or "").split(",")
                if item.strip()
            ],
            "reason": suggestion.get("\u89c4\u5219\u539f\u56e0", ""),
            "confidence": suggestion.get("\u7f6e\u4fe1\u5ea6", 0.0),
            "need_manual_review": True,
        }
        confidence = self._float_confidence(suggestion.get("\u7f6e\u4fe1\u5ea6"))
        manual_reason = (
            f"规则已给出 {classification['final_category'] or '候选'} 建议，但置信度 {confidence:.2f} "
            f"低于自动写入阈值 {min_confidence:.2f}；需人工确认后再写入 ERP 或加入试剂记忆库。"
        )
        self.add_manual_review_item(
            reagent,
            name_result,
            reason=manual_reason,
            search_result=search_result,
            extracted=extracted,
            classification=classification,
        )

    def write_failure_can_skip_manual_review(self, suggestion: dict[str, Any]) -> bool:
        # A reusable classification is only safe after ERP confirms the webpage
        # field was written and saved. Failed writes stay out of reusable memory
        # to avoid contaminating future automated approvals.
        return False

    def stabilize_reagent_detail_after_write_failure(self, page: Page) -> bool:
        try:
            self.wait_for_reagent_table_ready(page)
            wait_until_spinner_hidden(page, timeout_ms=5000)
        except Exception as error:
            print(f"Reagent table was not ready while stabilizing after write failure: {error}")
            return self.recover_reagent_detail_page_after_write_failure(page, "stabilize after write failure")

        if self.reagent_property_header_available(page):
            return True

        print("Physicochemical property header is not visible after write failure; recovering detail page.")
        return self.recover_reagent_detail_page_after_write_failure(page, "missing property header after write failure")

    def reagent_property_header_available(self, page: Page) -> bool:
        try:
            header = page.locator("thead th").filter(has_text="\u7269\u5316\u7279\u6027").first
            if header.count() and header.is_visible():
                return True
        except Exception:
            pass
        try:
            return bool(
                page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('thead th')).some((th) => {
                      const rect = th.getBoundingClientRect();
                      const style = window.getComputedStyle(th);
                      const text = (th.innerText || th.textContent || '').replace(/\\s+/g, '');
                      return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && text.includes('\u7269\u5316\u7279\u6027');
                    })
                    """
                )
            )
        except Exception:
            return False

    def clear_existing_edit_state(self, page: Page, writer: ApprovalWriter, sequence: str) -> bool:
        if not writer.any_row_is_editing(page):
            return True
        print(f"An existing edit row is open before sequence {sequence}; cancelling it first.")
        if writer.cancel_any_edit(page):
            return True
        return self.recover_reagent_detail_page_after_write_failure(page, "existing edit row before write")

    def settle_after_successful_write(self, page: Page, writer: ApprovalWriter, sequence: str) -> bool:
        writer.dismiss_open_dropdown(page)
        try:
            self.wait_for_reagent_table_ready(page)
        except Exception as error:
            print(f"Reagent table was not ready immediately after saving sequence {sequence}: {error}")
        page.wait_for_timeout(500)
        for _ in range(10):
            if not writer.any_row_is_editing(page):
                return True
            page.wait_for_timeout(250)
        print(f"Edit controls are still visible after saving sequence {sequence}; cancelling before next row.")
        writer.cancel_any_edit(page)
        page.wait_for_timeout(300)
        return not writer.any_row_is_editing(page)

    def cleanup_failed_write(
        self,
        page: Page,
        writer: ApprovalWriter,
        row: Any,
        sequence: str,
        reason: str,
    ) -> bool:
        cleaned = writer.cancel_edit(page, row)
        if cleaned:
            print(f"Cancelled edit state after failed write for sequence {sequence}: {reason}")
        else:
            print(f"Could not fully cancel edit state after failed write for sequence {sequence}: {reason}")
            cleaned = self.recover_reagent_detail_page_after_write_failure(page, f"failed write for sequence {sequence}")
        return cleaned

    def recover_reagent_detail_page_after_write_failure(self, page: Page, reason: str) -> bool:
        target_list_number = self.current_detail_list_number() or str(getattr(self, "target_list_number", "") or "").strip()
        print(
            "Recovering reagent detail page after write-state failure"
            f" ({reason}); target list: {target_list_number or '<current>'}."
        )
        self.force_close_editing_overlays(page)
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as error:
            print(f"Normal detail page reload failed; continuing with current page: {error}")
        self.force_close_editing_overlays(page)

        if self.current_page_is_target_detail(page, target_list_number):
            try:
                self.wait_for_reagent_table_ready(page)
                page.wait_for_timeout(1000)
                return True
            except Exception as error:
                print(f"Target detail page is visible, but reagent table was not ready after reload: {error}")

        if target_list_number:
            print(
                "Detail page recovery will reopen the target list from the todo page "
                "and let the next loop re-sort/re-read current '-' rows."
            )
            opened = self.reopen_target_detail_for_recovery(page, target_list_number)
            if not opened:
                print(f"Could not reopen target detail after write failure: {target_list_number}")
                return False
            try:
                self.wait_for_reagent_table_ready(page)
                page.wait_for_timeout(1000)
                self._current_detail_info = self.read_detail_info(page)
                return True
            except Exception as error:
                print(f"Reopened target detail, but reagent table was not ready: {error}")
                return False

        try:
            self.wait_for_reagent_table_ready(page)
            page.wait_for_timeout(1000)
            return True
        except Exception as error:
            print(f"Reagent table was not ready after reload and no target list was known: {error}")
            return False

    def force_close_editing_overlays(self, page: Page) -> None:
        for _ in range(3):
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(150)
            except Exception:
                pass
            try:
                page.evaluate(
                    """
                    () => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      for (const selector of [
                        '.ant-select-dropdown:not(.ant-select-dropdown-hidden)',
                        '.ant-popover:not(.ant-popover-hidden)',
                        '.ant-tooltip',
                        '.ant-modal-wrap',
                        '.ant-drawer'
                      ]) {
                        for (const node of document.querySelectorAll(selector)) {
                          if (!visible(node)) continue;
                          const closer = node.querySelector('.ant-drawer-close, .ant-modal-close, .ant-popconfirm-buttons button');
                          if (closer && visible(closer)) closer.click();
                        }
                      }
                    }
                    """
                )
                page.wait_for_timeout(150)
            except Exception:
                pass

    def reopen_target_detail_for_recovery(self, page: Page, target_list_number: str) -> bool:
        attempts = [
            ("todo list page", lambda: self.open_task_detail_by_list_number(page, target_list_number)),
            ("browser back then todo list page", lambda: self.reopen_after_browser_back(page, target_list_number)),
            ("menu reset then todo list page", lambda: self.reopen_after_menu_reset(page, target_list_number)),
        ]
        for label, action in attempts:
            try:
                self.force_close_editing_overlays(page)
                if action():
                    print(f"Recovered target detail via {label}: {target_list_number}")
                    return True
            except Exception as error:
                print(f"Could not reopen target detail {target_list_number} via {label}: {error}")
        return False

    def reopen_after_browser_back(self, page: Page, target_list_number: str) -> bool:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(800)
        except Exception as error:
            print(f"Browser back during detail recovery failed: {error}")
        return self.open_task_detail_by_list_number(page, target_list_number)

    def reopen_after_menu_reset(self, page: Page, target_list_number: str) -> bool:
        try:
            self.enter_reagent_judgement_page(page)
        except Exception as error:
            print(f"Menu reset during detail recovery failed: {error}")
            self.click_reagent_judgement_from_dom(page)
        return self.open_task_detail_by_list_number(page, target_list_number)

    @staticmethod
    def click_reagent_judgement_from_dom(page: Page) -> None:
        page.evaluate(
            """
            () => {
              const visible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                  && style.visibility !== 'hidden'
                  && style.display !== 'none';
              };
              const textOf = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, '').trim();
              const nodes = Array.from(document.querySelectorAll('li, a, span, div, button'));
              const judgement = nodes.find((node) => visible(node) && textOf(node).includes('试剂判定'));
              if (judgement) {
                judgement.scrollIntoView({block: 'center', inline: 'center'});
                judgement.click();
              }
            }
            """
        )
        page.wait_for_timeout(1000)

    def current_page_is_target_detail(self, page: Page, target_list_number: str = "") -> bool:
        target_list_number = str(target_list_number or "").strip()
        try:
            detail_info = self.read_detail_info(page)
        except Exception:
            detail_info = {}
        current_list_number = str(detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7") or "").strip()
        if target_list_number and current_list_number and current_list_number != target_list_number:
            return False
        if target_list_number and not current_list_number:
            try:
                body_text = str(page.locator("body").inner_text(timeout=3000) or "")
            except Exception:
                body_text = ""
            if target_list_number not in body_text:
                return False
        if not target_list_number and not current_list_number:
            try:
                if not self.reagent_property_header_available(page):
                    return False
            except Exception:
                return False
        try:
            records = self.read_current_page_reagents(page)
        except Exception:
            records = []
        if any(str(record.get("\u5e8f\u53f7") or "").strip() for record in records):
            return True
        try:
            row_count = page.locator("tbody tr.ant-table-row").count()
            return bool(row_count and self.reagent_property_header_available(page))
        except Exception:
            return False

    def capture_write_failure(
        self,
        page: Page,
        sequence: str,
        attempt: int,
        reason: str,
        row: Any | None = None,
        category: str = "",
        writer: ApprovalWriter | None = None,
    ) -> None:
        safe_sequence = "".join(ch for ch in str(sequence or "unknown") if ch.isalnum() or ch in {"-", "_"})
        safe_reason = "".join(ch if ch.isalnum() else "_" for ch in str(reason or "failure"))[:60].strip("_")
        prefix = f"write_fail_{safe_sequence}_attempt{attempt}_{safe_reason or 'failure'}"
        try:
            screenshot_path = self._log_dir() / f"{prefix}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"Saved write failure screenshot: {screenshot_path}")
        except Exception as error:
            print(f"Could not save write failure screenshot for sequence {sequence}: {error}")
        try:
            html_path = self._log_dir() / f"{prefix}.html"
            html_path.write_text(page.content(), encoding="utf-8")
            print(f"Saved write failure HTML: {html_path}")
        except Exception as error:
            print(f"Could not save write failure HTML for sequence {sequence}: {error}")
        try:
            debug_path = self._log_dir() / f"{prefix}.json"
            debug_payload = build_write_failure_debug_payload(
                sequence=sequence,
                attempt=attempt,
                reason=reason,
                category=category,
                failure_stage=getattr(writer, "_last_property_failure_stage", "") if writer is not None else "",
                dropdown_state_loader=lambda: ApprovalWriter.dropdown_debug_state(
                    page,
                    row=row,
                    candidate_name=category,
                    option_names=erp_property_options(self.settings),
                ),
            )
            debug_path.write_text(json.dumps(debug_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Saved write failure debug JSON: {debug_path}")
        except Exception as error:
            print(f"Could not save write failure debug JSON for sequence {sequence}: {error}")

    def approval_write_failure_break_limit(self) -> int:
        approval_settings = getattr(self, "settings", {}).get("approval", {}) or {}
        value = os.getenv("APPROVAL_WRITE_FAILURE_BREAK_LIMIT") or approval_settings.get("write_failure_break_limit", 2)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def prepare_reagent_row_for_write(page: Page, row: Any) -> None:
        try:
            row.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            row.evaluate(
                """
                (node) => {
                  const rect = node.getBoundingClientRect();
                  const targetY = window.scrollY + rect.top - Math.max(80, window.innerHeight * 0.35);
                  window.scrollTo({ top: Math.max(0, targetY), behavior: 'instant' });
                  node.scrollIntoView({ block: 'center', inline: 'nearest' });
                }
                """
            )
        except Exception:
            pass
        try:
            page.wait_for_timeout(250)
        except Exception:
            pass

    def suggestion_work_key(self, suggestion: dict[str, Any]) -> str:
        return self.reagent_work_key(
            {
                "\u5e8f\u53f7": suggestion.get("\u5e8f\u53f7", ""),
                "\u8bd5\u5242\u540d\u79f0": suggestion.get("\u8bd5\u5242\u540d\u79f0", ""),
                "CAS\u53f7": suggestion.get("CAS\u53f7", ""),
                "\u89c4\u683c": suggestion.get("\u89c4\u683c", ""),
                "\u89c4\u683c\u5355\u4f4d": suggestion.get("\u89c4\u683c\u5355\u4f4d", ""),
            }
        )

    @staticmethod
    def property_value_matches(value: str, expected: str, writer: ApprovalWriter) -> bool:
        normalized = " ".join(str(value or "").split())
        if not normalized:
            return False
        candidates = set(writer.property_name_candidates(expected))
        if normalized in candidates:
            return True

        # Ant Design fixed columns can duplicate the same cell text when the
        # table is read from both the main body and the fixed action/body panes.
        # Treat repeated equivalent labels such as "普通类 普通类" as a match.
        parts = [part for part in normalized.split(" ") if part]
        return bool(parts) and all(part in candidates for part in parts)

    def approval_write_mode(self) -> str:
        configured = (getattr(self, "settings", {}).get("approval", {}) or {}).get("write_mode", "disabled")
        normalized = str(os.getenv("APPROVAL_WRITE_MODE") or configured or "disabled").strip().lower()
        if normalized in {"disabled", "multi_page", "generate_library"}:
            return normalized
        print(f"Unknown APPROVAL_WRITE_MODE={normalized or '<empty>'}; write mode fails closed as disabled.")
        return "disabled"

    def dry_run_enabled(self) -> bool:
        app_settings = (getattr(self, "settings", {}).get("app", {}) or {})
        # A project-level dry-run is a safety floor.  A stale shell value such
        # as DRY_RUN=false must not turn a deliberately configured dry-run into
        # live ERP activity.
        if self._truthy(app_settings.get("dry_run", False)):
            return True
        env_value = os.getenv("APP_DRY_RUN") or os.getenv("DRY_RUN")
        if env_value is not None and env_value.strip():
            return self._truthy(env_value)
        return False

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}

    def remember_verified_v1_candidate_identities(self, suggestions: list[dict[str, Any]]) -> None:
        """Persist verified candidate CAS values locally; never modify ERP CAS."""
        if self._current_pipeline_version() != "v1":
            return
        from reagent_memory import ReagentMemory

        memory = ReagentMemory.from_settings(self.settings, Path(self.root_dir))
        for suggestion in suggestions:
            if not self._truthy(suggestion.get("候选CAS可写入记忆库")):
                continue
            candidate = str(suggestion.get("网页候选CAS") or "").strip()
            if not candidate:
                continue
            stored = memory.remember_verified_identity(
                raw_name=str(suggestion.get("试剂名称") or ""),
                cleaned_name=str(suggestion.get("清洗后名称") or ""),
                standard_name=str(suggestion.get("标准化名称") or ""),
                cas=candidate,
                source=str(suggestion.get("候选CAS确认来源") or suggestion.get("查询来源") or ""),
                url=str(suggestion.get("候选CAS确认URL") or suggestion.get("查询URL") or ""),
                reason="V1 名称检索的来源唯一确认 CAS；只写入身份记忆库，不修改 ERP CAS。",
            )
            suggestion["候选CAS记忆库状态"] = "已写入" if stored else "未写入"
            self.record_v1_candidate_cas_memory_audit(suggestion, candidate_cas=candidate, stored=stored)
            print(f"[v1_identity_memory] sequence={suggestion.get('序号') or '-'} cas={candidate} stored={stored}")

    def v1_candidate_cas_memory_eligible(
        self,
        *,
        erp_cas: str,
        candidate_cas: str,
        search_result: dict[str, Any],
    ) -> bool:
        """Whether a unique, source-verified CAS may enter local identity memory."""
        settings = getattr(self, "settings", {}) or {}
        approval = settings.get("approval", {}) or {}
        if self._current_pipeline_version() != "v1":
            return False
        policy = (approval.get("v1", {}) or {}).get("candidate_cas_memory", {}) or {}
        if not self._truthy(policy.get("enabled", False)):
            return False
        candidate = self.normalize_cas(candidate_cas)
        if not self._checksum_valid_cas(candidate):
            return False
        if not self._truthy(search_result.get("candidate_cas_source_verified")):
            return False
        if not self._truthy(search_result.get("candidate_cas_eligible_for_memory")):
            return False
        source = str(search_result.get("source") or "").strip()
        is_web_source = source.lower() in {"chemsrc", "chemicalbook"}
        if self._truthy(policy.get("require_page_bound_web_cas", False)) and is_web_source:
            if not self._truthy(search_result.get("candidate_cas_page_bound")):
                return False
        if self._truthy(policy.get("require_secondary_confirmation_for_web_sources", False)) and is_web_source:
            if not str(search_result.get("candidate_cas_secondary_confirmation_source") or "").strip():
                return False
        if str(search_result.get("identity_status") or "").strip() not in {"name_verified", "verified_by_enrichment"}:
            return False
        if not ChemicalSearcher.is_trusted_source(str(search_result.get("source") or "")):
            return False
        if not search_result.get("relevance_passed", False):
            return False
        return True

    def record_v1_candidate_cas_memory_audit(
        self, suggestion: dict[str, Any], *, candidate_cas: str, stored: bool
    ) -> None:
        path = Path(self.root_dir) / "data" / "logs" / "v1_candidate_cas_memory_audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "at": pd.Timestamp.now().isoformat(), "pipeline": "v1",
            "list_number": suggestion.get("试剂清单号") or suggestion.get("清单号") or "",
            "sequence": suggestion.get("序号") or "", "raw_name": suggestion.get("试剂名称") or "",
            "candidate_cas": candidate_cas, "stored": stored,
            "source": suggestion.get("候选CAS确认来源") or suggestion.get("查询来源") or "",
            "source_url": suggestion.get("候选CAS确认URL") or suggestion.get("查询URL") or "",
            "candidate_validation_status": suggestion.get("候选CAS校验状态") or "",
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def high_confidence_write_candidates(self, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        threshold = self.approval_write_min_confidence()
        output: list[dict[str, Any]] = []
        for suggestion in suggestions:
            self.apply_unknown_auto_write_policy(suggestion)
            llm_rule_gap_adopted = self._truthy(suggestion.get("llm_second_opinion_adopted"))
            rule_category = str(suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b") or "").strip()
            if not rule_category:
                continue
            erp_category = to_erp_property(rule_category, self.settings)
            if not erp_category:
                print(f"Skipping write candidate with no ERP property mapping: {rule_category}")
                continue
            if rule_category == UNKNOWN_CATEGORY:
                normalized = dict(suggestion)
                normalized["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"] = erp_category
                if erp_category != rule_category:
                    normalized["\u89c4\u5219\u5224\u5b9a\u7c7b\u522b"] = rule_category
                output.append(normalized)
                continue
            # This must precede the reject shortcut below: V3 web-search output
            # is advisory, including a model-proposed reject category.
            if self._truthy(suggestion.get("V3模型未核实")):
                continue
            if str(suggestion.get("审批链路") or "").strip().lower() == "v3":
                if not self._truthy(suggestion.get("V3可核验直写")):
                    continue
            if is_non_writable_rule_category(rule_category, self.settings, getattr(self, "root_dir", None)):
                continue
            if self.is_terminal_reject_decision(rule_category):
                if not self.terminal_reject_auto_write_allowed(suggestion):
                    continue
                normalized = dict(suggestion)
                normalized["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"] = erp_category
                if erp_category != rule_category:
                    normalized["\u89c4\u5219\u5224\u5b9a\u7c7b\u522b"] = rule_category
                output.append(normalized)
                continue
            formal_rule_direct_precheck = (
                self._truthy(((self.settings.get("approval", {}) or {}).get("v1", {}) or {}).get("formal_rule_direct_write", True))
                and bool(str(suggestion.get("\u547d\u4e2d\u89c4\u5219ID") or "").strip())
                and not self._truthy(suggestion.get("\u5546\u54c1\u578b\u53f7\u8eab\u4efd\u4fdd\u62a4"))
            )
            if (
                str(suggestion.get("\u9700\u4eba\u5de5\u590d\u6838")).strip().lower() in {"true", "1", "yes"}
                and not (llm_rule_gap_adopted or formal_rule_direct_precheck)
            ):
                continue
            identity_status = str(suggestion.get("身份状态") or suggestion.get("身份验证状态") or "").strip()
            legacy_name_conflict = identity_status == "conflict" and str(suggestion.get("身份判定依据") or "").strip() in {"名称身份", "name_identity"}
            v3_identity_verified = self._truthy(suggestion.get("V3可核验直写")) and identity_status in {"name_verified", "cas_verified"}
            v1_local_identity = identity_status in {
                "v1_local_elemental_heavy_metal", "v1_local_primary_component", "v1_local_clear_name",
            }
            has_formal_rule_match = bool(str(suggestion.get("命中规则ID") or "").strip())
            v1_formal_rule_direct = (
                self._truthy(((self.settings.get("approval", {}) or {}).get("v1", {}) or {}).get("formal_rule_direct_write", True))
                and has_formal_rule_match
                and not self._truthy(suggestion.get("商品型号身份保护"))
            )
            if ((identity_status in {"cas_missing", "ambiguous", "unresolved"} and not (v3_identity_verified or v1_local_identity or v1_formal_rule_direct)) or (identity_status == "conflict" and not (v3_identity_verified or v1_formal_rule_direct))) and not llm_rule_gap_adopted:
                if not legacy_name_conflict:
                    continue
            # A second opinion is supplementary.  Once the rule engine has
            # formally matched a rule and the preceding identity/source gates
            # passed, it must not suppress the rule category's ERP write.
            if (
                rule_category != UNKNOWN_CATEGORY
                and self._truthy(suggestion.get("LLM辅助意见仅供复核"))
                and not (v3_identity_verified or legacy_name_conflict or v1_local_identity or has_formal_rule_match)
            ):
                continue
            try:
                confidence = float(suggestion.get("\u7f6e\u4fe1\u5ea6") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < threshold and not llm_rule_gap_adopted:
                continue
            normalized = dict(suggestion)
            normalized["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"] = erp_category
            if erp_category != rule_category:
                normalized["\u89c4\u5219\u5224\u5b9a\u7c7b\u522b"] = rule_category
            output.append(normalized)
        return output

    def _classification_input(
        self,
        reagent: dict[str, str],
        search_result: dict[str, Any],
        extracted: dict[str, Any],
    ) -> dict[str, Any]:
        text_parts = [
            reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
            reagent.get("CAS\u53f7", ""),
            search_result.get("raw_text", "")[:2000],
            extracted.get("toxicity", ""),
            " ".join(extracted.get("suggested_categories", []) or []),
            " ".join(extracted.get("evidence", []) or []),
        ]
        identity_status = str(search_result.get("identity_status") or "").strip().lower()
        identity_basis = str(search_result.get("identity_decision_basis") or "").strip().lower()
        identity_reliable = identity_status in {"verified", "cas_verified", "name_verified", "name_verified_pubchem"} or (
            identity_status == "conflict" and identity_basis == "name_identity"
        )
        hazard_fields = ("corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal")
        hazard_fields_complete = all(extracted.get(field) is not None for field in hazard_fields)
        ordinary_evidence_complete = bool(search_result.get("ordinary_evidence_complete")) if "ordinary_evidence_complete" in search_result else bool(
            identity_reliable and hazard_fields_complete
            and not search_result.get("need_manual_review", True)
            and search_result.get("relevance_passed", False)
        )
        return {
            "reagent_name": reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
            "name": search_result.get("name", ""),
            "standard_name": (search_result.get("name_normalization", {}) or {}).get("standard_name", ""),
            "cleaned_name": (search_result.get("name_normalization", {}) or {}).get("cleaned_name", ""),
            "english_name": (search_result.get("name_normalization", {}) or {}).get("english_name", ""),
            "cas": reagent.get("CAS\u53f7", ""),
            "text": " ".join(str(part) for part in text_parts if part),
            "flash_point": extracted.get("flash_point", ""),
            "boiling_point": extracted.get("boiling_point", ""),
            "toxicity": extracted.get("toxicity", ""),
            "corrosive": extracted.get("corrosive"),
            "oxidizing": extracted.get("oxidizing"),
            "flammable": extracted.get("flammable"),
            "water_reactive": extracted.get("water_reactive"),
            "explosive_risk": extracted.get("explosive_risk"),
            "heavy_metal": extracted.get("heavy_metal"),
            "suggested_categories": extracted.get("suggested_categories", []),
            "mixture_risk_categories": search_result.get("mixture_risk_categories", []),
            "sds_categories": search_result.get("sds_categories", []),
            "component_categories": search_result.get("component_categories", []),
            "evidence": extracted.get("evidence", []),
            "allow_default_normal": bool(
                not search_result.get("need_manual_review", True)
                and search_result.get("relevance_passed", False)
            ),
            "ordinary_evidence_complete": ordinary_evidence_complete,
        }

    def mark_duplicate_search_url_if_needed(
        self,
        reagent: dict[str, str],
        search_result: dict[str, Any],
        seen_search_urls: dict[str, str],
    ) -> None:
        url = str(search_result.get("url") or "").strip()
        if not url:
            return

        reagent_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "")
        previous_name = seen_search_urls.get(url)
        if previous_name and previous_name != reagent_name:
            search_result["need_manual_review"] = True
            search_result["relevance_passed"] = False
            search_result["raw_text"] = (
                f"Duplicate search URL was returned for different reagents. "
                f"Current reagent: {reagent_name}; previous reagent: {previous_name}; url: {url}"
            )
            print(f"Duplicate search URL detected; forcing manual review: {reagent_name} -> {url}")
            return

        seen_search_urls[url] = reagent_name

    def _approval_suggestion_row(
        self,
        reagent: dict[str, str],
        name_result: dict[str, Any],
        search_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
    ) -> dict[str, Any]:
        erp_cas = str(reagent.get("CAS\u53f7", "") or "").strip()
        corrected_cas = str(search_result.get("corrected_cas") or name_result.get("corrected_cas") or "").strip()
        # Existing checksum-valid ERP CAS values are authoritative.  A source-
        # verified candidate can only fill a missing/invalid value later in the
        # stable-ID ERP transaction; it never replaces an existing valid CAS.
        effective_cas = erp_cas
        erp_cas_is_valid = self._checksum_valid_cas(self.normalize_cas(erp_cas))
        if erp_cas_is_valid:
            corrected_cas = ""
        candidate_cas = str(
            search_result.get("candidate_cas")
            or name_result.get("candidate_cas")
            or (search_result.get("cas") or name_result.get("cas") or "")
        ).strip()
        candidate_cas_memory = self.v1_candidate_cas_memory_eligible(
            erp_cas=erp_cas,
            candidate_cas=candidate_cas,
            search_result=search_result,
        )
        # A rejected or insufficiently verified CAS is diagnostic noise, not a
        # candidate identity.  Keep it only in non-user-facing source audit
        # records; never expose it in suggestions or the review queue.
        display_candidate_cas = candidate_cas if candidate_cas_memory else ""
        enrichment_candidate_verified = bool(
            search_result.get("identity_status") == "verified_by_enrichment" and candidate_cas
        )
        enrichment_english_name = str(
            (search_result.get("identity_enrichment") or {}).get("english_name") or ""
        ).strip()
        classification_confidence = self._effective_classification_confidence(
            search_result,
            classification,
        )
        suggestion = {
            "identity_enrichment_candidate_cas_verified": enrichment_candidate_verified,
            "identity_enrichment_english_name": enrichment_english_name,
            "候选CAS可写入记忆库": candidate_cas_memory,
            "候选CAS记忆库状态": "待写入" if candidate_cas_memory else "",
            "候选CAS确认来源": search_result.get("candidate_cas_confirmation_source") or search_result.get("source") or "",
            "候选CAS确认URL": search_result.get("candidate_cas_confirmation_url") or search_result.get("url") or "",
            "候选CAS校验状态": search_result.get("candidate_cas_validation_status") or "",
            "候选CAS拒绝原因": search_result.get("candidate_cas_validation_reason") or "",
            "候选CAS页面名称": search_result.get("candidate_cas_page_name") or search_result.get("matched_site_name") or "",
            "候选CAS二次确认来源": search_result.get("candidate_cas_secondary_confirmation_source") or "",
            "候选CAS二次确认URL": search_result.get("candidate_cas_secondary_confirmation_url") or "",
            "\u5e8f\u53f7": reagent.get("\u5e8f\u53f7", ""),
            "\u8bd5\u5242\u540d\u79f0": reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
            "CAS\u53f7": effective_cas,
            "\u7f51\u9875\u5019\u9009CAS": display_candidate_cas,
            "\u539fERP CAS\u53f7": search_result.get("original_erp_cas") or name_result.get("original_erp_cas") or "",
            "\u4fee\u6b63CAS\u53f7": corrected_cas,
            "CAS\u540d\u79f0\u51b2\u7a81": search_result.get("cas_name_conflict") or name_result.get("cas_name_conflict") or False,
            "CAS\u540d\u79f0\u51b2\u7a81\u8bb0\u5f55": search_result.get("identity_conflict_recorded") or False,
            "CAS\u540d\u79f0\u51b2\u7a81\u8bf4\u660e": search_result.get("identity_conflict_reason") or "",
            "CAS\u67e5\u8be2\u72b6\u6001": search_result.get("cas_lookup_status") or "",
            "CAS校验状态": search_result.get("cas_validation_status") or "",
            "查询状态": search_result.get("lookup_status") or search_result.get("retrieval_status") or "",
            "实际查询键": search_result.get("actual_query_key") or search_result.get("query") or "",
            "CAS\u4fee\u6b63\u5019\u9009": search_result.get("cas_correction_candidate") or name_result.get("cas_correction_candidate") or False,
            "CAS\u4fee\u6b63\u5df2\u5e94\u7528": False if erp_cas_is_valid else (search_result.get("cas_correction_applied") or name_result.get("cas_correction_applied") or False),
            "CAS\u4fee\u6b63\u539f\u56e0": search_result.get("cas_correction_reason") or name_result.get("cas_correction_reason") or "",
            "CAS\u4fee\u6b63\u6765\u6e90": search_result.get("cas_correction_source") or name_result.get("cas_correction_source") or "",
            "CAS\u4fee\u6b63URL": search_result.get("cas_correction_url") or name_result.get("cas_correction_url") or "",
            "\u8eab\u4efd\u5224\u5b9a\u4f9d\u636e": search_result.get("identity_decision_basis") or name_result.get("identity_decision_basis") or "",
            "\u89c4\u683c": reagent.get("\u89c4\u683c", ""),
            "\u89c4\u683c\u5355\u4f4d": reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
            "\u8bd5\u5242\u6570\u91cf": reagent.get("\u8bd5\u5242\u6570\u91cf", ""),
            "\u6807\u51c6\u5316\u540d\u79f0": name_result.get("standard_name", ""),
            "\u82f1\u6587\u540d\u79f0": name_result.get("english_name", ""),
            "\u6e05\u6d17\u540e\u540d\u79f0": name_result.get("cleaned_name", ""),
            "\u6d53\u5ea6": name_result.get("concentration", ""),
            "\u540d\u79f0\u6807\u51c6\u5316\u7f6e\u4fe1\u5ea6": name_result.get("confidence", 0.0),
            "\u540d\u79f0\u9700\u4eba\u5de5\u590d\u6838": name_result.get("need_manual_review", True),
            "\u540d\u79f0\u6807\u51c6\u5316\u539f\u56e0": name_result.get("reason", ""),
            "LLM名称标准化是否调用": name_result.get("llm_normalization_attempted", False),
            "LLM名称标准化状态": name_result.get("llm_normalization_status", "not_needed"),
            "LLM名称标准化模型": name_result.get("llm_normalization_model", ""),
            "LLM名称标准化错误": name_result.get("llm_normalization_error", ""),
            "\u7591\u4f3c\u9519\u8bef\u540d\u79f0": name_result.get("suspected_invalid_name", False),
            "\u5019\u9009\u4fee\u6b63\u540d\u79f0": ", ".join(name_result.get("candidate_names", []) or []),
            "\u7591\u4f3c\u9519\u8bef\u539f\u56e0": name_result.get("suspected_invalid_reason", ""),
            "\u67e5\u8be2\u6765\u6e90": search_result.get("source", ""),
            "\u67e5\u8be2URL": search_result.get("url", ""),
            "\u67e5\u8be2\u9700\u4eba\u5de5": search_result.get("need_manual_review", False),
            "\u7f51\u7ad9\u5339\u914d\u540d\u79f0": search_result.get("matched_site_name", ""),
            "网站返回物质名称": search_result.get("website_returned_name") or search_result.get("matched_site_name", ""),
            "网站返回CAS": search_result.get("candidate_cas") or search_result.get("cas") or "",
            "\u540d\u79f0\u76f8\u4f3c\u5ea6": search_result.get("name_similarity", 0.0),
            "\u67e5\u8be2\u76f8\u5173\u6027\u901a\u8fc7": search_result.get("relevance_passed", False),
            "\u515c\u5e95\u67e5\u8be2\u6765\u6e90": search_result.get("fallback_source", ""),
            "\u515c\u5e95\u67e5\u8be2URL": search_result.get("fallback_url", ""),
            "\u8d44\u6599\u53ef\u4fe1\u5ea6": search_result.get("source_confidence", 0.0),
            "\u8bc1\u636e\u8d28\u91cf": search_result.get("evidence_quality", ""),
            "\u660e\u786e\u672a\u77e5\u540d\u79f0\u89c4\u5219\u547d\u4e2d": bool(
                classification.get("unknown_name_rule")
                or search_result.get("unknown_name_rule")
                or name_result.get("unknown_name_rule")
            ),
            "unknown_name_rule": bool(
                classification.get("unknown_name_rule")
                or search_result.get("unknown_name_rule")
                or name_result.get("unknown_name_rule")
            ),
            "\u67e5\u8be2\u5931\u8d25\u539f\u56e0": search_result.get("failure_reason", ""),
            "身份验证状态": search_result.get("identity_status", ""),
            "商品型号身份保护": bool(search_result.get("product_model_identity_blocked", False)),
            "名称身份": json.dumps(search_result.get("name_identity") or {}, ensure_ascii=False),
            "CAS身份": json.dumps(search_result.get("cas_identity") or {}, ensure_ascii=False),
            "数据获取状态": search_result.get("retrieval_status", ""),
            "是否混合物": search_result.get("is_mixture", False),
            "字段证据": json.dumps(search_result.get("evidence_items", []) or [], ensure_ascii=False),
            "化源网完整区块": json.dumps(search_result.get("chemsrc_sections") or {}, ensure_ascii=False),
            "选用物化证据": json.dumps(search_result.get("selected_property_evidence") or {}, ensure_ascii=False),
            "选用毒理证据": json.dumps(search_result.get("selected_toxicity_evidence") or [], ensure_ascii=False),
            "混合物判定依据": search_result.get("mixture_classification_basis") or "",
            "混合物最终类别": search_result.get("mixture_selected_category") or "",
            "主成分": search_result.get("primary_component") or "",
            "主成分修饰": search_result.get("primary_component_modifiers") or "",
            "主成分规则ID": ", ".join(search_result.get("primary_component_rule_ids") or []),
            "主成分是否被危险证据覆盖": bool(search_result.get("primary_component_hazard_overridden")),
            "V1无网页证据业务口径": search_result.get("v1_no_web_evidence_policy") or "",
            "数据源诊断": json.dumps(search_result.get("provider_results", []) or [], ensure_ascii=False),
            "\u662f\u5426\u4f7f\u7528\u5927\u6a21\u578b\u751f\u6210\u5019\u9009\u540d": search_result.get("used_llm_search_candidates", False),
            "\u5927\u6a21\u578b\u5019\u9009\u641c\u7d22\u8bcd": ", ".join(search_result.get("llm_search_candidates", []) or []),
            "是否使用LLM知识托底": search_result.get("used_llm_knowledge_fallback", False),
            "LLM辅助建议类别": search_result.get("llm_advisory_category", ""),
            "LLM辅助物性意见": search_result.get("llm_advisory_summary_cn", ""),
            "LLM辅助判定理由": search_result.get("llm_advisory_reason_cn", ""),
            "LLM辅助规则依据": search_result.get("llm_advisory_rule_cn", ""),
            "LLM辅助不确定项": search_result.get("llm_advisory_uncertainties_cn", ""),
            "LLM辅助置信度": search_result.get("llm_advisory_confidence", ""),
            "LLM辅助依据类型": search_result.get("llm_advisory_evidence_basis", ""),
            "LLM辅助意见仅供复核": search_result.get("llm_advisory_only", False),
            "llm_second_opinion_adopted": bool(classification.get("llm_second_opinion_adopted")),
            "\u95ea\u70b9": extracted.get("flash_point", ""),
            "\u6cb8\u70b9": extracted.get("boiling_point", ""),
            "\u6bd2\u6027": extracted.get("toxicity", ""),
            "\u8150\u8680\u6027": extracted.get("corrosive"),
            "\u6c27\u5316\u6027": extracted.get("oxidizing"),
            "\u6613\u71c3": extracted.get("flammable"),
            "\u9047\u6c34\u53cd\u5e94": extracted.get("water_reactive"),
            "\u7206\u70b8\u98ce\u9669": extracted.get("explosive_risk"),
            "\u91cd\u91d1\u5c5e": extracted.get("heavy_metal"),
            "\u5927\u6a21\u578b\u5019\u9009\u7c7b\u522b": ", ".join(extracted.get("suggested_categories", []) or []),
            "\u8bc1\u636e": " | ".join(extracted.get("evidence", []) or []),
            "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": classification.get("final_category", ""),
            "\u547d\u4e2d\u7c7b\u522b": ", ".join(classification.get("matched_categories", []) or []),
            "命中规则ID": ", ".join(classification.get("matched_rule_ids", []) or []),
            "\u89c4\u5219\u539f\u56e0": classification.get("reason", ""),
            "\u7f6e\u4fe1\u5ea6": classification_confidence,
            "\u9700\u4eba\u5de5\u590d\u6838": self._suggestion_needs_manual_review(
                search_result,
                name_result,
                extracted,
                classification,
            ),
        }
        return self.apply_unresolved_product_unknown_policy(
            self.apply_unknown_auto_write_policy(suggestion),
            search_result,
            name_result,
            classification,
        )

    def _effective_classification_confidence(
        self,
        search_result: dict[str, Any],
        classification: dict[str, Any],
    ) -> float:
        confidence = self._float_confidence(classification.get("confidence", 0.0))
        if confidence >= 0.7:
            return confidence
        if self._trusted_high_quality_exact_example_match(search_result, classification):
            return max(confidence, 0.7)
        return confidence

    def _trusted_high_quality_exact_example_match(
        self,
        search_result: dict[str, Any],
        classification: dict[str, Any],
    ) -> bool:
        source = str(search_result.get("source") or "").strip()
        if not ChemicalSearcher.is_trusted_source(source):
            return False
        if str(search_result.get("evidence_quality") or "").strip().lower() != "high":
            return False
        if not search_result.get("relevance_passed", False):
            return False
        if self._float_confidence(search_result.get("source_confidence")) < 0.86:
            return False
        if classification.get("need_manual_review", True):
            return False
        reason = str(classification.get("reason") or "")
        return "举例列辅助命中" in reason or "example" in reason.lower()

    def _suggestion_needs_manual_review(
        self,
        search_result: dict[str, Any],
        name_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
    ) -> bool:
        if str(search_result.get("identity_status") or "").strip() in {
            "v1_local_elemental_heavy_metal", "v1_local_primary_component", "v1_local_clear_name",
        }:
            return False
        if self.is_terminal_reject_decision(classification.get("final_category")):
            return False
        if classification.get("llm_second_opinion_adopted"):
            return False
        evidence = extracted.get("evidence", []) or []
        llm_failed = any("LLM extraction failed" in str(item) for item in evidence)
        if search_result.get("need_manual_review", True):
            return True
        if llm_failed:
            return True
        if classification.get("need_manual_review", True):
            return True
        if name_result.get("need_manual_review", True):
            return not self._search_resolved_name_review(search_result, name_result)
        return False

    @staticmethod
    def _search_resolved_name_review(search_result: dict[str, Any], name_result: dict[str, Any]) -> bool:
        source = str(search_result.get("source") or "").strip()
        trusted_source = ChemicalSearcher.is_trusted_source(source)
        confidence = ApprovalFlowMixin._float_confidence(name_result.get("confidence"))
        source_confidence = ApprovalFlowMixin._float_confidence(search_result.get("source_confidence"))
        has_identifier = bool(str(name_result.get("cas") or search_result.get("cas") or "").strip())
        return bool(
            trusted_source
            and search_result.get("relevance_passed", False)
            and source_confidence >= 0.86
            and str(search_result.get("retrieval_status") or "fresh").lower() != "stale"
            and confidence >= 0.8
            and (
                name_result.get("web_verified_alias")
                or not name_result.get("suspected_invalid_name", False)
                or has_identifier
            )
        )

    @staticmethod
    def _float_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, confidence))

    def try_auto_pass_current_task(self, page: Page) -> None:
        if self.dry_run_enabled():
            print("Auto-pass skipped; dry-run mode is enabled.")
            return

        if not self.auto_pass_enabled():
            print("Auto-pass skipped; AUTO_PASS is not true.")
            return

        detail_info = self.read_detail_info(page)
        list_number = detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7", "").strip()
        print(f"Auto-pass precheck for list: {list_number or '<unknown>'}")

        blocked_reasons: list[str] = []

        if not self.auto_match_succeeded:
            blocked_reasons.append("Auto-match did not complete cleanly.")

        if not list_number:
            blocked_reasons.append("Current reagent list number could not be read.")

        unmatched_records: list[dict[str, str]] = []
        try:
            unmatched_records = self.find_unmatched_reagents_across_all_pages(page)
            if not self.pagination_check_succeeded:
                blocked_reasons.append("Sorted unmatched reagent pages could not be verified.")
        except Exception as error:  # noqa: BLE001 - auto-pass must fail closed, not crash the run
            blocked_reasons.append(f"Could not verify unmatched reagent pages: {error}")

        if unmatched_records:
            blocked_reasons.append(
                f"Found {len(unmatched_records)} reagent row(s) with physicochemical property '-'."
            )

        has_manual_review, manual_reason = self.current_list_has_manual_review_item(list_number)
        if has_manual_review:
            blocked_reasons.append(manual_reason)

        if not self.all_save_operations_successful():
            blocked_reasons.append("One or more save operations failed.")

        if blocked_reasons:
            print("Auto-pass blocked; the top approve button was not clicked.")
            for reason in blocked_reasons:
                print(f"- {reason}")
            return

        self.click_top_approve_button(page)

    def auto_pass_enabled(self) -> bool:
        value = os.getenv("AUTO_PASS", "")
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    def record_save_result(self, name: str, success: bool, detail: str = "") -> None:
        if self.save_results is None:
            self.save_results = []
        self.save_results.append({"name": name, "success": success, "detail": detail})
        if hasattr(self, "settings") and hasattr(self, "root_dir"):
            AuditLogger.from_settings(self.settings, self.root_dir).record_execution(
                name,
                success,
                detail,
                self.dry_run_enabled(),
            )

    def all_save_operations_successful(self) -> bool:
        if not self.save_results:
            print("No save operations were recorded; save precheck is treated as failed.")
            return False

        approval_save_prefixes = ("erp_", "page_", "reagent_save")
        approval_saves = [
            result
            for result in self.save_results
            if str(result.get("name", "")).startswith(approval_save_prefixes)
        ]

        if not approval_saves:
            local_saves = [
                result
                for result in self.save_results
                if str(result.get("name", "")).startswith("local_")
            ]
            failed_local = [result for result in local_saves if not result.get("success")]
            if local_saves and not failed_local:
                print("No ERP/page save operations were needed; local save operation(s) succeeded.")
                return True
            print("No ERP/page save operations were recorded; auto-pass save precheck failed.")
            return False

        failed = [result for result in approval_saves if not result.get("success")]
        if failed:
            print(f"Failed save operation(s): {failed}")
            return False

        print(f"All recorded ERP/page save operation(s) succeeded: {len(approval_saves)}")
        return True

    def click_top_approve_button(self, page: Page) -> None:
        approve_button = self.find_top_approve_button(page)
        if not approve_button:
            raise RuntimeError("AUTO_PASS is true and checks passed, but the top approve button was not found.")

        print("AUTO_PASS checks passed; clicking the top approve button.")
        approve_button.click()
        page.wait_for_timeout(1000)

        prompt_text = self.capture_prompt_if_present(page, "auto_pass_prompt.png")
        if prompt_text:
            print(f"Prompt after clicking approve: {prompt_text}")
        else:
            print("Top approve button clicked; no prompt was detected.")

    def find_top_approve_button(self, page: Page) -> Locator | None:
        selectors = self.settings.get("selectors", {})
        configured_selector = selectors.get("approve_button", "").strip()

        candidates: list[Locator] = []
        if configured_selector:
            candidates.append(page.locator(configured_selector).first)

        candidates.extend(
            [
                page.locator(".ant-page-header button").filter(has_text="\u901a\u8fc7").first,
                page.get_by_role("button", name="\u901a\u8fc7").first,
                page.locator("button").filter(has_text="\u901a\u8fc7").first,
            ]
        )

        for candidate in candidates:
            try:
                if candidate.count() and candidate.is_visible():
                    return candidate
            except Error:
                continue

        return None

    def approval_suggestion_columns(self) -> list[str]:
        return [
            "\u5e8f\u53f7",
            "\u8bd5\u5242\u540d\u79f0",
            "CAS\u53f7",
            "\u89c4\u683c",
            "\u89c4\u683c\u5355\u4f4d",
            "\u8bd5\u5242\u6570\u91cf",
            "\u6807\u51c6\u5316\u540d\u79f0",
            "\u82f1\u6587\u540d\u79f0",
            "\u6e05\u6d17\u540e\u540d\u79f0",
            "\u6d53\u5ea6",
            "\u540d\u79f0\u6807\u51c6\u5316\u7f6e\u4fe1\u5ea6",
            "\u540d\u79f0\u9700\u4eba\u5de5\u590d\u6838",
            "\u540d\u79f0\u6807\u51c6\u5316\u539f\u56e0",
            "\u7591\u4f3c\u9519\u8bef\u540d\u79f0",
            "\u5019\u9009\u4fee\u6b63\u540d\u79f0",
            "\u7591\u4f3c\u9519\u8bef\u539f\u56e0",
            "\u67e5\u8be2\u6765\u6e90",
            "\u67e5\u8be2URL",
            "\u67e5\u8be2\u9700\u4eba\u5de5",
            "\u7f51\u7ad9\u5339\u914d\u540d\u79f0",
            "\u540d\u79f0\u76f8\u4f3c\u5ea6",
            "\u67e5\u8be2\u76f8\u5173\u6027\u901a\u8fc7",
            "\u515c\u5e95\u67e5\u8be2\u6765\u6e90",
            "\u515c\u5e95\u67e5\u8be2URL",
            "\u8d44\u6599\u53ef\u4fe1\u5ea6",
            "\u8bc1\u636e\u8d28\u91cf",
            "\u67e5\u8be2\u5931\u8d25\u539f\u56e0",
            "V1无网页证据业务口径",
            "\u662f\u5426\u4f7f\u7528\u5927\u6a21\u578b\u751f\u6210\u5019\u9009\u540d",
            "\u5927\u6a21\u578b\u5019\u9009\u641c\u7d22\u8bcd",
            "\u95ea\u70b9",
            "\u6cb8\u70b9",
            "\u6bd2\u6027",
            "\u8150\u8680\u6027",
            "\u6c27\u5316\u6027",
            "\u6613\u71c3",
            "\u9047\u6c34\u53cd\u5e94",
            "\u7206\u70b8\u98ce\u9669",
            "\u91cd\u91d1\u5c5e",
            "\u5927\u6a21\u578b\u5019\u9009\u7c7b\u522b",
            "\u8bc1\u636e",
            "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b",
            "\u547d\u4e2d\u7c7b\u522b",
            "\u89c4\u5219\u539f\u56e0",
            "\u7f6e\u4fe1\u5ea6",
            "\u9700\u4eba\u5de5\u590d\u6838",
        ]
