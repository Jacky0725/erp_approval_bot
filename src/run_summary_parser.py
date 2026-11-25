"""Run-log parsing, workflow progress tracking, and run-summary aggregation."""
from __future__ import annotations

import re
from typing import Any

from approval_suggestion_metrics import aggregate_suggestion_summaries


ACTION_LABELS = {
    "workbuddy_loop": "WorkBuddy 批次循环",
    "workbuddy_prepare": "WorkBuddy 准备下一批（按配置条数）",
    "workbuddy_import": "WorkBuddy 导入结果并按写入模式保存",
    "suggestions": "审批流程",
    "todo_export": "待办清单刷新",
    "erp_smoke": "ERP 只读冒烟测试",
    "api_discovery": "ERP 接口探测",
    "debug_capture": "首页采集",
    "judgement_capture": "试剂判定页采集",
}

WORKFLOW_STEPS = [
    {"id": "login", "label": "登录 ERP"},
    {"id": "judgement", "label": "进入试剂判定"},
    {"id": "auto_match", "label": "一键匹配"},
    {"id": "sort_read", "label": '排序读取 "-"'},
    {"id": "search", "label": "网站查询"},
    {"id": "llm", "label": "大模型整理"},
    {"id": "rule", "label": "规则判定"},
    {"id": "write", "label": "网页写入"},
]

MOJIBAKE_MARKERS = (
    "\ufffd",
    "\u00c2",
    "\u00c3",
    "鐕冩",
    "枡鍙",
    "婃补",
    "鍝",
    "鐗",
    "鍒",
    "绫",
    "\u93b4",
    "\u6fb6",
    "\u9427",
    "\u93c8",
    "\u934f",
    "\u74c7",
    "\u6d93",
)


def mojibake_score(text: str) -> int:
    return sum(text.count(marker) for marker in MOJIBAKE_MARKERS) + text.count("?")


def repair_display_text(value: Any) -> str:
    text = str(value or "")
    if not text or not any(marker in text for marker in MOJIBAKE_MARKERS):
        return text
    candidates = [text]
    for encoding in ("latin1", "cp1252", "gbk", "cp936"):
        try:
            candidates.append(text.encode(encoding).decode("utf-8"))
        except UnicodeError:
            pass
        try:
            candidates.append(text.encode(encoding, errors="replace").decode("utf-8", errors="replace"))
        except UnicodeError:
            pass
    return min(candidates, key=mojibake_score)


def action_label(action: str) -> str:
    return ACTION_LABELS.get(str(action or ""), str(action or ""))


def workbuddy_outcome_counts(lines: list[str]) -> dict[str, int]:
    counts = dict.fromkeys(("saved", "failed", "blocked", "review"), 0)
    for line in lines:
        match = re.match(r"^\[workbuddy\] .+ (saved|failed|blocked|review): ", line)
        if match:
            counts[match.group(1)] += 1
    return counts


def reported_workbuddy_reviews(lines: list[str]) -> int | None:
    for line in reversed(lines):
        match = re.fullmatch(r"\[workbuddy review_pending\] (\d+)", line)
        if match:
            return int(match.group(1))
    return None


def confirmed_api_write_count(lines: list[str]) -> int:
    confirmed_api_records: set[str] = set()
    for line in lines:
        lower = line.lower()
        if "[api_property_confirmed]" in lower:
            match = re.search(r"\brecord_id=([^\s]+)", line)
            if match:
                confirmed_api_records.add(match.group(1))
            else:
                confirmed_api_records.add(line)
    # The WorkBuddy bridge logs saved only after ERP read-back confirmation.
    return len(confirmed_api_records) + workbuddy_outcome_counts(lines)["saved"]


def confirmed_write_count(lines: list[str]) -> int:
    page_confirmations = 0
    for line in lines:
        lower = line.lower()
        if "save verified for sequence" in lower and ": false" not in lower:
            page_confirmations += 1
        elif "save result for sequence" in lower and ": false" not in lower:
            page_confirmations += 1
    return confirmed_api_write_count(lines) + page_confirmations


def parse_target_list_numbers(value: str) -> list[str]:
    numbers = []
    for part in str(value or "").replace("\n", ",").replace(";", ",").split(","):
        item = part.strip()
        if item and item not in numbers:
            numbers.append(item)
    return numbers


def extract_after(line: str, marker: str) -> str:
    if marker not in line:
        return ""
    tail = line.split(marker, 1)[1].strip()
    return tail.split()[0].strip(" .,:;")


def extract_between(line: str, start_marker: str, end_marker: str) -> str:
    if start_marker not in line or end_marker not in line:
        return ""
    value = line.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
    return repair_display_text(value)


def dropdown_failure_details(lines: list[str]) -> list[dict[str, str]]:
    failures = []
    for line in lines:
        lower = line.lower()
        if "could not select physicochemical property" not in lower:
            continue
        failures.append(
            {
                "line": repair_display_text(line),
                "sequence": extract_after(line, "sequence:"),
                "category": extract_between(line, "property", "for sequence:"),
            }
        )
    return failures


def extract_number_before(line: str, marker: str) -> float | None:
    if marker not in line:
        return None
    before = line.split(marker, 1)[0].strip()
    token = before.split()[-1] if before.split() else ""
    try:
        return float(token)
    except ValueError:
        return None


def extract_stage_seconds(line: str) -> float | None:
    if "(" not in line or "s)" not in line:
        return None
    value = line.rsplit("(", 1)[-1].split("s)", 1)[0].strip()
    try:
        return float(value)
    except ValueError:
        return None


def chemical_source_summary(lines: list[str]) -> dict[str, Any]:
    summary = {
        "chemsrc_success_count": 0,
        "chemsrc_failure_count": 0,
        "chemsrc_circuit_open_count": 0,
        "chemicalbook_success_count": 0,
        "chemicalbook_failure_count": 0,
        "chemicalbook_circuit_open_count": 0,
        "chemicalbook_503_count": 0,
        "pubchem_result_count": 0,
        "chemical_cache_hit_count": 0,
        "chemical_fresh_count": 0,
        "chemical_stale_count": 0,
        "chemical_unavailable_count": 0,
        "chemical_identity_conflict_count": 0,
        "chemical_mixture_count": 0,
        "chemical_public_request_count": 0,
        "fallback_web_result_count": 0,
        "chemsrc_no_trusted_count": 0,
        "chemicalbook_no_trusted_count": 0,
        "duplicate_search_reuse_count": 0,
        "duplicate_llm_reuse_count": 0,
        "llm_skip_count": 0,
        "chemical_search_seconds": 0.0,
        "manual_review_queued_count": 0,
        "manual_review_updated_count": 0,
        "manual_review_batch_flush_count": 0,
        "api_read_detail_count": 0,
        "api_record_resolve_count": 0,
        "api_record_resolve_failure_count": 0,
        "api_write_attempt_count": 0,
        "api_write_success_count": 0,
        "api_verify_failure_count": 0,
        "api_page_stale_warning_count": 0,
        "api_page_not_located_warning_count": 0,
        "api_page_mismatch_warning_count": 0,
        "api_fallback_web_write_count": 0,
        "identity_enrichment_call_count": 0,
        "identity_enrichment_english_candidate_count": 0,
        "identity_enrichment_cas_candidate_count": 0,
        "identity_enrichment_verified_count": 0,
        "identity_enrichment_failure_count": 0,
    }
    for raw_line in lines:
        line = str(raw_line)
        lower = line.lower()
        if "chemical source success: chemsrc" in lower:
            summary["chemsrc_success_count"] += 1
        if "chemical source failure: chemsrc" in lower:
            summary["chemsrc_failure_count"] += 1
        if "chemical source circuit opened: chemsrc" in lower:
            summary["chemsrc_circuit_open_count"] += 1
        if "chemical source success: chemicalbook" in lower:
            summary["chemicalbook_success_count"] += 1
        if "chemical source failure: chemicalbook" in lower:
            summary["chemicalbook_failure_count"] += 1
        if "chemical source failure: chemicalbook" in lower and "http_error_503" in lower:
            summary["chemicalbook_503_count"] += 1
        if "chemical source circuit opened: chemicalbook" in lower:
            summary["chemicalbook_circuit_open_count"] += 1
        if "chemical source no trusted result: chemsrc" in lower:
            summary["chemsrc_no_trusted_count"] += 1
        if "chemical source no trusted result: chemicalbook" in lower:
            summary["chemicalbook_no_trusted_count"] += 1
        if "-> pubchem" in lower or ("chemical lookup result:" in lower and "source=pubchem" in lower):
            summary["pubchem_result_count"] += 1
        if "chemical lookup result:" in lower and "retrieval=cache" in lower:
            summary["chemical_cache_hit_count"] += 1
        if "chemical lookup result:" in lower and "retrieval=fresh" in lower:
            summary["chemical_fresh_count"] += 1
        if "served stale verified cache" in lower or (
            "chemical lookup result:" in lower and "retrieval=stale" in lower
        ):
            summary["chemical_stale_count"] += 1
        if "chemical lookup result:" in lower and "retrieval=unavailable" in lower:
            summary["chemical_unavailable_count"] += 1
        if "chemical lookup result:" in lower and "identity=conflict" in lower:
            summary["chemical_identity_conflict_count"] += 1
        if "chemical lookup result:" in lower and "mixture=true" in lower:
            summary["chemical_mixture_count"] += 1
        if "chemical batch metrics:" in lower:
            match = re.search(r"public_requests=(\d+)", lower)
            if match:
                summary["chemical_public_request_count"] += int(match.group(1))
        if "fallback_source" in lower and "pubchem" in lower:
            summary["fallback_web_result_count"] += 1
        if "reusing run search result for duplicate reagent" in lower:
            summary["duplicate_search_reuse_count"] += 1
        if "reusing run llm result for duplicate reagent" in lower:
            summary["duplicate_llm_reuse_count"] += 1
        if "[parallel llm] skip" in lower:
            summary["llm_skip_count"] += 1
        if "[flow] end" in lower and "chemical_search" in lower:
            seconds = extract_stage_seconds(line)
            if seconds is not None:
                summary["chemical_search_seconds"] += seconds
        if "queued search-failure item for manual review" in lower:
            summary["manual_review_queued_count"] += 1
        if "queued manual review update" in lower:
            summary["manual_review_updated_count"] += 1
        if "flushed " in lower and "manual review row" in lower:
            summary["manual_review_batch_flush_count"] += 1
        if "[api_read_detail]" in lower:
            summary["api_read_detail_count"] += 1
        if "[api_record_resolve]" in lower and " failed=" not in lower:
            summary["api_record_resolve_count"] += 1
        if "[api_record_resolve]" in lower and " failed=" in lower:
            summary["api_record_resolve_failure_count"] += 1
        if "[api_property_save]" in lower and "skipped=" not in lower:
            summary["api_write_attempt_count"] += 1
        if re.match(r"^\[workbuddy\] .+ saved: ERP (?:JSON-RPC|API) write saved and verified\.$", line):
            summary["api_write_attempt_count"] += 1
        if "[api_property_verify]" in lower and "failed=" in lower:
            summary["api_verify_failure_count"] += 1
        if "[api_property_verify]" in lower and "warning=stable_id_confirmed_page_not_located" in lower:
            summary["api_page_not_located_warning_count"] += 1
        if "[api_property_verify]" in lower and "warning=stable_id_confirmed_page_mismatch" in lower:
            summary["api_page_mismatch_warning_count"] += 1
        if "[api_property_verify]" in lower and any(
            marker in lower for marker in (
                "warning=stable_id_confirmed_page_stale",
                "warning=stable_id_confirmed_page_not_located",
                "warning=stable_id_confirmed_page_mismatch",
            )
        ):
            summary["api_page_stale_warning_count"] += 1
        if "[api_fallback_web_write]" in lower:
            summary["api_fallback_web_write_count"] += 1
        if "chemical identity enrichment:" in lower:
            summary["identity_enrichment_call_count"] += 1
            english_match = re.search(r"english_candidate=(true|false)", lower)
            if english_match and english_match.group(1) == "true":
                summary["identity_enrichment_english_candidate_count"] += 1
            cas_match = re.search(r"cas_candidates=(\d+)", lower)
            if cas_match:
                summary["identity_enrichment_cas_candidate_count"] += int(cas_match.group(1))
            if "status=failure" in lower:
                summary["identity_enrichment_failure_count"] += 1
        if "chemical identity enrichment verified:" in lower:
            summary["identity_enrichment_verified_count"] += 1
    summary["chemical_search_seconds"] = round(float(summary["chemical_search_seconds"]), 1)
    summary["api_write_success_count"] = confirmed_api_write_count(lines)
    return summary


def build_run_summary(
    lines: list[str],
    *,
    action: str,
    options: dict[str, Any] | None,
    running: bool,
    success: bool | None,
    error: str,
    todo_count: int = 0,
) -> dict[str, Any]:
    text = "\n".join(str(line) for line in lines)
    lower_text = text.lower()
    options = options or {}
    suggestion_metrics = aggregate_suggestion_summaries(lines)
    workbuddy = workbuddy_outcome_counts(lines)
    pending_reviews = reported_workbuddy_reviews(lines)

    write_success = confirmed_write_count(lines)
    write_failed = workbuddy["failed"] + sum(
        1
        for line in lines
        if "could not select" in line.lower()
        or "failed save operation" in line.lower()
        or "save verification failed" in line.lower()
    )
    deferred_write_count = workbuddy["blocked"] + sum(1 for line in lines if "deferred pending write candidate" in line.lower())
    not_found_after_reread_count = sum(
        1
        for line in lines
        if "not_found_after_reread" in line.lower()
        or "not found after re-read" in line.lower()
        or "pending write candidate(s) not found" in line.lower()
    )
    dropdown_failures = dropdown_failure_details(lines)
    source_summary = chemical_source_summary(lines)
    llm_seconds = 0.0
    llm_batches = 0
    for line in lines:
        lower = line.lower()
        if "llm extraction completed" in lower and "s" in lower:
            llm_batches += 1
            seconds = extract_number_before(line, "s")
            if seconds is not None:
                llm_seconds += seconds
        elif "[flow] end" in lower and "llm_extract" in lower:
            llm_batches += 1
            seconds = extract_stage_seconds(line)
            if seconds is not None:
                llm_seconds += seconds
    page_count = len(
        {
            marker
            for marker in (
                extract_after(line, "Current page:")
                or extract_after(line, "Read reagent page")
                or extract_after(line, "Read todo page")
                for line in lines
            )
            if marker
        }
    )
    target_lists = parse_target_list_numbers(str(options.get("TARGET_LIST_NUMBERS") or options.get("target_list_numbers") or ""))
    if not target_lists:
        for line in lines:
            for token in line.replace("'", " ").replace('"', " ").replace(",", " ").split():
                if token.startswith("SJ") and token[2:].isdigit() and token not in target_lists:
                    target_lists.append(token)

    if running:
        outcome = "运行中"
    elif success is True and action == "todo_export":
        outcome = "待办清单刷新成功"
    elif success is True and action == "suggestions":
        outcome = "审批流程完成"
    elif success is True and pending_reviews:
        outcome = f"自动处理完成，待人工复核 {pending_reviews} 条"
    elif success is True:
        outcome = f"{action_label(action)}成功"
    elif success is False:
        outcome = repair_display_text(error) or "执行失败"
    else:
        outcome = "未运行"

    return {
        "action": action,
        "action_label": action_label(action),
        "outcome": outcome,
        "target_list_numbers": target_lists,
        "todo_count": int(todo_count or 0),
        "suggestion_count": int(suggestion_metrics.get("suggestion_total") or 0) + sum(workbuddy.values()),
        "manual_review_count": int(suggestion_metrics.get("manual_review_candidate_count") or 0)
        + (workbuddy["review"] if pending_reviews is None else pending_reviews) + workbuddy["blocked"],
        "processed_pages": page_count,
        "write_success_count": write_success,
        "write_failure_count": write_failed,
        "deferred_write_count": deferred_write_count,
        "not_found_after_reread_count": not_found_after_reread_count,
        "dropdown_failure_count": len(dropdown_failures),
        "dropdown_failures": dropdown_failures[:12],
        "page_suggestion_count": int(suggestion_metrics.get("suggestion_total") or 0),
        "writable_candidate_count": int(suggestion_metrics.get("writable_candidate_count") or 0),
        "manual_review_candidate_count": int(suggestion_metrics.get("manual_review_candidate_count") or 0),
        "low_confidence_count": int(suggestion_metrics.get("low_confidence_count") or 0),
        "search_failure_count": int(suggestion_metrics.get("search_failure_count") or 0),
        "memory_hit_count": int(suggestion_metrics.get("memory_hit_count") or 0),
        "llm_knowledge_fallback_count": int(suggestion_metrics.get("llm_knowledge_fallback_count") or 0),
        "llm_batch_count": llm_batches,
        "llm_seconds": round(llm_seconds, 1),
        **source_summary,
        "skipped_candidate_count": int(suggestion_metrics.get("skipped_candidate_count") or 0),
        "skip_reasons": suggestion_metrics.get("skip_reasons") or {},
        "has_traceback": "traceback" in lower_text,
        "has_write_warning": write_failed > 0,
    }


def light_run_summary(
    lines: list[str],
    *,
    action: str,
    options: dict[str, Any] | None,
    running: bool,
    success: bool | None,
    error: str,
) -> dict[str, Any]:
    options = options or {}
    target_lists = parse_target_list_numbers(str(options.get("TARGET_LIST_NUMBERS") or options.get("target_list_numbers") or ""))
    suggestion_metrics = aggregate_suggestion_summaries(lines)
    workbuddy = workbuddy_outcome_counts(lines)
    pending_reviews = reported_workbuddy_reviews(lines)
    source_summary = chemical_source_summary(lines)
    write_success = confirmed_write_count(lines)
    write_failed = workbuddy["failed"] + sum(
        1
        for line in lines
        if "could not select" in line.lower()
        or "failed save operation" in line.lower()
        or "save verification failed" in line.lower()
    )
    dropdown_failures = dropdown_failure_details(lines)
    if running:
        outcome = "运行中"
    elif success is True and action == "todo_export":
        outcome = "待办清单刷新成功"
    elif success is True and action == "suggestions":
        outcome = "审批流程完成"
    elif success is True and pending_reviews:
        outcome = f"自动处理完成，待人工复核 {pending_reviews} 条"
    elif success is True:
        outcome = f"{action_label(action)}成功"
    elif success is False:
        outcome = repair_display_text(error) or "执行失败"
    else:
        outcome = "未运行"
    return {
        "action": action,
        "action_label": action_label(action),
        "outcome": outcome,
        "target_list_numbers": target_lists,
        "todo_count": 0,
        "suggestion_count": int(suggestion_metrics.get("suggestion_total") or 0) + sum(workbuddy.values()),
        "manual_review_count": int(suggestion_metrics.get("manual_review_candidate_count") or 0)
        + (workbuddy["review"] if pending_reviews is None else pending_reviews) + workbuddy["blocked"],
        "processed_pages": 0,
        "write_success_count": write_success,
        "write_failure_count": write_failed,
        "deferred_write_count": workbuddy["blocked"] + sum(1 for line in lines if "deferred pending write candidate" in line.lower()),
        "not_found_after_reread_count": sum(1 for line in lines if "not found after re-read" in line.lower()),
        "dropdown_failure_count": len(dropdown_failures),
        "dropdown_failures": dropdown_failures[:12],
        "page_suggestion_count": int(suggestion_metrics.get("suggestion_total") or 0),
        "writable_candidate_count": int(suggestion_metrics.get("writable_candidate_count") or 0),
        "manual_review_candidate_count": int(suggestion_metrics.get("manual_review_candidate_count") or 0),
        "low_confidence_count": int(suggestion_metrics.get("low_confidence_count") or 0),
        "search_failure_count": int(suggestion_metrics.get("search_failure_count") or 0),
        "memory_hit_count": int(suggestion_metrics.get("memory_hit_count") or 0),
        "llm_knowledge_fallback_count": int(suggestion_metrics.get("llm_knowledge_fallback_count") or 0),
        "llm_batch_count": 0,
        "llm_seconds": 0.0,
        **source_summary,
        "skipped_candidate_count": int(suggestion_metrics.get("skipped_candidate_count") or 0),
        "skip_reasons": suggestion_metrics.get("skip_reasons") or {},
        "has_traceback": any("traceback" in line.lower() for line in lines),
        "has_write_warning": write_failed > 0,
    }


def workflow_step_index(step_id: str) -> int:
    for index, step in enumerate(WORKFLOW_STEPS):
        if step["id"] == step_id:
            return index
    return -1


def parse_flow_stage(line: str, marker: str) -> str:
    needle = f"[FLOW] {marker}"
    if needle not in line:
        return ""
    tail = line.split(needle, 1)[1].strip()
    if not tail:
        return ""
    return tail.split(" - ", 1)[0].split(" (", 1)[0].strip()


def stage_to_workflow_step(stage: str) -> str:
    normalized = stage.strip().lower()
    if normalized in {"perform_auto_match"}:
        return "auto_match"
    if normalized in {"wait_reagent_table_ready", "read_detail_info", "sort_property_column", "read_current_page_unmatched"}:
        return "sort_read"
    if normalized in {"chemical_search"}:
        return "search"
    if normalized in {"llm_extract"}:
        return "llm"
    if normalized in {"rule_classify", "record_rule_candidate", "add_manual_review_item"}:
        return "rule"
    if normalized in {"apply_approval_write_mode"}:
        return "write"
    return ""


def line_to_workflow_step(line: str) -> str:
    for marker in ("START", "END"):
        stage = parse_flow_stage(line, marker)
        if stage:
            mapped = stage_to_workflow_step(stage)
            if mapped:
                return mapped

    text = line.lower()
    if "opening menu" in text or "opening page" in text or "opening target task detail" in text or "todo list refresh" in text:
        return "judgement"
    if "auto-match" in text or "一键匹配" in line:
        return "auto_match"
    if "physicochemical property header" in text or "read_current_page_unmatched" in text or "sorting considered successful" in text:
        return "sort_read"
    if "chemsrc" in text or "chemicalbook" in text or "chemical_search" in text:
        return "search"
    if "llm_extract" in text or "大模型" in line:
        return "llm"
    if "rule_classify" in text or "规则判定" in line:
        return "rule"
    if (
        "approval write candidate" in text
        or "save result for sequence" in text
        or "save verified for sequence" in text
        or "apply_approval_write_mode" in text
        or "[workbuddy library]" in text
    ):
        return "write"
    if "start " in text:
        return "login"
    return ""


def active_workflow_step(lines: list[str]) -> str:
    active_stage = ""
    for line in lines:
        stage = parse_flow_stage(line, "START")
        if stage:
            active_stage = stage
            continue
        stage = parse_flow_stage(line, "END")
        if stage and stage == active_stage:
            active_stage = ""
    if active_stage:
        return stage_to_workflow_step(active_stage)

    for line in reversed(lines):
        step = line_to_workflow_step(line)
        if step:
            return step
    return ""


def highest_seen_workflow_index(lines: list[str]) -> int:
    highest = -1
    for line in lines:
        step = line_to_workflow_step(line)
        index = workflow_step_index(step)
        if index > highest:
            highest = index
    return highest


def workflow_summary(
    lines: list[str],
    *,
    running: bool,
    success: bool | None,
    error: str,
) -> dict[str, Any]:
    active_step = active_workflow_step(lines)
    seen_order = highest_seen_workflow_index(lines)
    active_index = workflow_step_index(active_step)

    if running and active_index < 0:
        active_step = "login"
        active_index = 0

    if running and active_index >= 0:
        completed_index = active_index - 1
    elif success:
        completed_index = seen_order
    else:
        completed_index = seen_order

    steps = []
    for index, step in enumerate(WORKFLOW_STEPS):
        state = "waiting"
        if index <= completed_index:
            state = "done"
        if running and index == active_index:
            state = "active"
        elif error and not running and index == active_index:
            state = "failed"
        steps.append({**step, "state": state})

    current = WORKFLOW_STEPS[active_index] if active_index >= 0 else None
    return {
        "current_step": current["id"] if current else "",
        "current_label": current["label"] if current else ("已完成" if success else "未运行"),
        "steps": steps,
    }


def result_label(running: bool, success: bool | None, error: str, health: str = "", action: str = "") -> str:
    if running:
        return "运行中"
    if success is True:
        if health == "warning":
            return "需检查"
        if action == "todo_export":
            return "待办清单刷新成功"
        if action == "suggestions":
            return "审批流程完成"
        return "成功"
    if success is False or error:
        return "失败"
    return "未运行"


def run_health(lines: list[str], success: bool | None, error: str) -> str:
    if success is False or error:
        return "failed"
    if reported_workbuddy_reviews(lines):
        return "warning"
    text = "\n".join(str(line) for line in lines).lower()
    warning_tokens = (
        "[workbuddy attention]",
        "failed save operation",
        "could not select",
        "could not open technical judgement",
        "multi-page mode stopped because",
        "pagination check stopped",
        "traceback",
    )
    if any(token in text for token in warning_tokens):
        return "warning"
    if success is True:
        return "ok"
    return "unknown"


def automation_failure_reason(lines: list[str]) -> str:
    text = "\n".join(str(line) for line in lines).lower()
    failure_patterns = (
        ("could not select physicochemical property", "存在物化特性下拉选择失败"),
        ("could not open technical judgement", "存在技术判定入口打开失败"),
        ("save verification failed", "存在网页保存后校验失败"),
        ("failed save operation", "存在保存操作失败"),
        ("multi-page mode stopped because the reagent detail page could not be stabilized", "写入失败后详情页未能恢复稳定"),
        ("multi-page mode stopped because next-page navigation could not be verified", "分页切换未能确认"),
        ("multi-page mode stopped because sorting after", "排序或翻页后的页面状态异常"),
        ("traceback", "运行过程中出现异常堆栈"),
    )
    for pattern, reason in failure_patterns:
        if pattern in text:
            return reason
    return ""


def current_approval_list_number(lines: list[str], options: dict[str, Any] | None = None) -> str:
    patterns = (
        r"Processing todo detail\s+\d+\s*:\s*(SJ\d+)",
        r"Processing selected todo detail\s+\d+\s*:\s*(SJ\d+)",
        r"Opening target task detail(?: from todo page \S+)?\s*:\s*(SJ\d+)",
        r"Auto-pass precheck for list:\s*(SJ\d+)",
    )
    for line in reversed(lines):
        if "[workbuddy scope]" in str(line):
            return str(line).split("[workbuddy scope]", 1)[1].strip()
        for pattern in patterns:
            match = re.search(pattern, str(line))
            if match:
                return match.group(1)

    target_lists = parse_target_list_numbers(
        str((options or {}).get("TARGET_LIST_NUMBERS") or (options or {}).get("target_list_numbers") or "")
    )
    if len(target_lists) == 1:
        return target_lists[0]
    return ""
