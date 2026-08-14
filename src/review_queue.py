from __future__ import annotations

from typing import Any

import pandas as pd


REVIEW_EVIDENCE_COLUMNS = [
    "suggested_category",
    "classification_confidence",
    "property_summary",
    "evidence_source_type",
    "source_confidence",
    "llm_confidence",
    "evidence_quality",
    "source_url",
    "flash_point",
    "boiling_point",
    "toxicity",
    "corrosive",
    "oxidizing",
    "flammable",
    "water_reactive",
    "explosive_risk",
    "used_llm_knowledge_fallback",
    "review_advice",
    "display_suggestion",
    "display_reason",
    "evidence_status",
    "detail_summary",
    "allow_suggestion_preselect",
]


BLOCKING_REVIEW_STATUSES = {
    "",
    "pending",
    "manual_review",
    "open",
    "todo",
    "待处理",
    "待复核",
    "人工复核",
    "需人工复核",
}


class ReviewQueueMixin:
    def clear_manual_review_items_for_list(self, list_number: str) -> None:
        list_number = str(list_number or "").strip()
        if not list_number:
            return

        paths = self.settings.get("paths", {})
        review_queue_path = self.root_dir / paths.get("review_queue_excel", "data/review_queue.xlsx")
        if not review_queue_path.exists():
            return

        try:
            queue = pd.read_excel(review_queue_path, dtype=str).fillna("")
        except Exception as error:
            print(f"Could not clear old manual review items for {list_number}: {error}")
            return

        if queue.empty:
            return

        list_columns = [
            "\u8bd5\u5242\u6e05\u5355\u53f7",
            "\u5f53\u524d\u6e05\u5355\u53f7",
            "\u6e05\u5355\u53f7",
            "list_number",
            "reagent_list_no",
            "reagent_list_number",
            "order_no",
        ]
        present_list_columns = [column for column in list_columns if column in queue.columns]
        if not present_list_columns:
            return

        matched = pd.Series(False, index=queue.index)
        for column in present_list_columns:
            matched = matched | (queue[column].astype(str).str.strip() == list_number)

        removed_count = int(matched.sum())
        if not removed_count:
            return

        queue = queue[~matched].copy()
        self.write_excel_with_fallback(queue, review_queue_path)
        print(f"Cleared {removed_count} old manual review item(s) for list {list_number}.")

    def current_list_has_manual_review_item(self, list_number: str) -> tuple[bool, str]:
        if not list_number:
            return True, "Cannot check review queue because current list number is empty."

        paths = self.settings.get("paths", {})
        review_queue_path = self.root_dir / paths.get("review_queue_excel", "data/review_queue.xlsx")

        if not review_queue_path.exists():
            print(f"Review queue file does not exist; treating as no pending manual review item: {review_queue_path}")
            return False, ""

        try:
            queue = pd.read_excel(review_queue_path, dtype=str).fillna("")
        except Exception as error:
            return True, f"Could not read review queue: {error}"

        if queue.empty:
            print(f"Review queue is empty: {review_queue_path}")
            return False, ""

        list_columns = [
            "\u5f53\u524d\u6e05\u5355\u53f7",
            "\u8bd5\u5242\u6e05\u5355\u53f7",
            "\u6e05\u5355\u53f7",
            "list_number",
            "reagent_list_no",
            "reagent_list_number",
            "order_no",
        ]
        present_list_columns = [column for column in list_columns if column in queue.columns]

        if not present_list_columns:
            return (
                True,
                f"Review queue has {len(queue)} row(s) but no list-number column; auto-pass is blocked.",
            )

        matched = pd.DataFrame()
        for column in present_list_columns:
            column_matched = queue[queue[column].astype(str).str.strip() == list_number]
            if not column_matched.empty:
                matched = pd.concat([matched, column_matched], ignore_index=True)

        if matched.empty:
            print(f"No manual review item for current list in review queue: {list_number}")
            return False, ""

        blocking = self._blocking_review_rows(matched)
        if blocking.empty:
            print(f"Review queue has no pending manual review item for current list: {list_number}")
            return False, ""

        output_path = self._log_dir() / "auto_pass_blocked_review_queue.xlsx"
        output_path = self.write_excel_with_fallback(blocking, output_path)
        return (
            True,
            f"Review queue contains {len(blocking)} pending manual review row(s) for current list; saved {output_path}.",
        )

    @staticmethod
    def _blocking_review_rows(rows: pd.DataFrame) -> pd.DataFrame:
        if rows.empty:
            return rows
        status_column = next((column for column in ("status", "状态", "处理状态") if column in rows.columns), "")
        if not status_column:
            return rows
        normalized = rows[status_column].astype(str).str.strip().str.lower()
        return rows[normalized.isin(BLOCKING_REVIEW_STATUSES)]

    def add_manual_review_item_from_search_failure(
        self,
        reagent: dict[str, str],
        name_result: dict[str, Any],
        search_result: dict[str, Any],
    ) -> None:
        self.add_manual_review_item(
            reagent,
            name_result,
            reason=str(search_result.get("raw_text") or search_result.get("failure_reason") or "Chemical website lookup failed after name normalization."),
            search_result=search_result,
        )

    def add_manual_review_item(
        self,
        reagent: dict[str, str],
        name_result: dict[str, Any],
        reason: str = "",
        search_result: dict[str, Any] | None = None,
        extracted: dict[str, Any] | None = None,
        classification: dict[str, Any] | None = None,
    ) -> None:
        detail_info = getattr(self, "_current_detail_info", None)
        if not detail_info:
            detail_info = {}

        paths = self.settings.get("paths", {})
        review_queue_path = self.root_dir / paths.get("review_queue_excel", "data/review_queue.xlsx")
        review_queue_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            queue = pd.read_excel(review_queue_path, dtype=str).fillna("") if review_queue_path.exists() else pd.DataFrame()
        except Exception:
            queue = pd.DataFrame()

        list_number = detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7", "")
        chemical_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "")
        cas = reagent.get("CAS\u53f7", "")
        sequence = reagent.get("\u5e8f\u53f7", "")
        specification = reagent.get("\u89c4\u683c", "")
        unit = reagent.get("\u89c4\u683c\u5355\u4f4d", "")
        reason = str(reason or "Manual review is required before writing this reagent to ERP.")
        evidence_fields = self._manual_review_evidence_fields(
            reagent=reagent,
            name_result=name_result,
            search_result=search_result or {},
            extracted=extracted or {},
            classification=classification or {},
            reason=reason,
        )

        existing_match = pd.Series(dtype=bool)
        if not queue.empty:
            list_columns = [
                "\u8bd5\u5242\u6e05\u5355\u53f7",
                "\u5f53\u524d\u6e05\u5355\u53f7",
                "\u6e05\u5355\u53f7",
                "list_number",
            ]
            name_columns = ["chemical_name", "\u8bd5\u5242\u540d\u79f0"]
            sequence_columns = ["\u5e8f\u53f7", "sequence", "index"]
            cas_columns = ["cas", "CAS\u53f7"]
            specification_columns = ["specification", "\u89c4\u683c"]
            unit_columns = ["unit", "\u89c4\u683c\u5355\u4f4d"]

            def match_optional(columns: list[str], expected: str) -> pd.Series:
                column = next((item for item in columns if item in queue.columns), "")
                if not column:
                    return pd.Series(True, index=queue.index)
                return queue[column].astype(str).str.strip() == str(expected or "").strip()

            list_column = next((column for column in list_columns if column in queue.columns), "")
            name_column = next((column for column in name_columns if column in queue.columns), "")
            if list_column and name_column:
                existing_match = (
                    (queue[list_column].astype(str).str.strip() == list_number)
                    & (queue[name_column].astype(str).str.strip() == chemical_name)
                    & match_optional(sequence_columns, sequence)
                    & match_optional(cas_columns, cas)
                    & match_optional(specification_columns, specification)
                    & match_optional(unit_columns, unit)
                )

        if not existing_match.empty and bool(existing_match.any()):
            updated = self._update_existing_manual_review_reason(
                queue,
                existing_match,
                reason,
                evidence_fields,
            )
            if updated:
                review_queue_path = self.write_excel_with_fallback(queue, review_queue_path)
                print(f"Updated manual review queue reason: {review_queue_path}")
            else:
                print(f"Manual review queue already contains search-failure item: {list_number} / {chemical_name}")
            return

        row = {
            "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
            "\u8bd5\u5242\u6e05\u5355\u53f7": list_number,
            "applicant": detail_info.get("\u7533\u8bf7\u4eba", ""),
            "\u5e8f\u53f7": sequence,
            "chemical_name": chemical_name,
            "\u8bd5\u5242\u540d\u79f0": chemical_name,
            "cas": cas,
            "quantity": reagent.get("\u8bd5\u5242\u6570\u91cf", ""),
            "specification": reagent.get("\u89c4\u683c", ""),
            "unit": reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
            "standard_name": name_result.get("standard_name", ""),
            "cleaned_name": name_result.get("cleaned_name", ""),
            "decision": "manual_review",
            "reason": reason,
            "status": "pending",
            **evidence_fields,
        }
        queue = pd.concat([queue, pd.DataFrame([row])], ignore_index=True)
        review_queue_path = self.write_excel_with_fallback(queue, review_queue_path)
        print(f"Added search-failure item to manual review queue: {review_queue_path}")

    @staticmethod
    def _update_existing_manual_review_reason(
        queue: pd.DataFrame,
        existing_match: pd.Series,
        new_reason: str,
        evidence_fields: dict[str, Any] | None = None,
    ) -> bool:
        new_reason = str(new_reason or "").strip()
        if not new_reason:
            return False
        reason_column = "reason"
        if reason_column not in queue.columns:
            queue[reason_column] = ""

        updated = False
        timestamp = pd.Timestamp.now().isoformat(timespec="seconds")
        for index in queue[existing_match].index:
            old_reason = str(queue.at[index, reason_column] or "").strip()
            if not old_reason:
                merged_reason = new_reason
            elif new_reason in old_reason:
                merged_reason = old_reason
            elif old_reason in new_reason:
                merged_reason = new_reason
            else:
                merged_reason = f"{old_reason} | {new_reason}"

            if merged_reason != old_reason:
                queue.at[index, reason_column] = merged_reason
                if "timestamp" in queue.columns:
                    queue.at[index, "timestamp"] = timestamp
                updated = True
            for column, value in (evidence_fields or {}).items():
                if column not in queue.columns:
                    queue[column] = ""
                value_text = str(value or "").strip()
                if value_text and str(queue.at[index, column] or "").strip() != value_text:
                    queue.at[index, column] = value_text
                    if "timestamp" in queue.columns:
                        queue.at[index, "timestamp"] = timestamp
                    updated = True
        return updated

    @staticmethod
    def _manual_review_evidence_fields(
        reagent: dict[str, Any],
        name_result: dict[str, Any],
        search_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
        reason: str = "",
    ) -> dict[str, Any]:
        suggested_category = str(classification.get("final_category") or "").strip()
        if not suggested_category:
            suggested_category = ", ".join(str(item) for item in classification.get("matched_categories", []) or [])
        used_llm = bool(search_result.get("used_llm_knowledge_fallback") or extracted.get("used_llm_knowledge_fallback"))
        source = str(search_result.get("source") or search_result.get("fallback_source") or "").strip()
        if used_llm:
            evidence_source_type = "llm_fallback"
        elif source in {"Chemsrc", "ChemicalBook"}:
            evidence_source_type = "trusted_web"
        elif source:
            evidence_source_type = "web_fallback"
        else:
            evidence_source_type = "none"

        evidence = extracted.get("evidence", []) or []
        if isinstance(evidence, list):
            evidence_text = " | ".join(str(item) for item in evidence if str(item).strip())
        else:
            evidence_text = str(evidence)
        property_parts = []
        for label, key in (
            ("flash_point", "flash_point"),
            ("boiling_point", "boiling_point"),
            ("toxicity", "toxicity"),
            ("corrosive", "corrosive"),
            ("oxidizing", "oxidizing"),
            ("flammable", "flammable"),
            ("water_reactive", "water_reactive"),
            ("explosive_risk", "explosive_risk"),
        ):
            value = extracted.get(key)
            if value not in ("", None, []):
                property_parts.append(f"{label}={value}")
        if evidence_text:
            property_parts.append(f"evidence={evidence_text[:300]}")

        advice = "Manual review required; confirm the physicochemical category before writing to ERP."
        if used_llm:
            advice = "Website evidence is missing or insufficient; LLM fallback is advisory only and must be manually verified."
        elif evidence_source_type == "trusted_web":
            advice = "Trusted website evidence is available; verify the suggested category and source evidence."
        elif evidence_source_type == "web_fallback":
            advice = "Fallback web evidence is available but may be incomplete; manually verify before confirming."

        llm_confidence = search_result.get("llm_confidence", "")
        if used_llm and llm_confidence == "":
            llm_confidence = search_result.get("source_confidence", extracted.get("confidence", ""))

        fields = {
            "suggested_category": suggested_category,
            "classification_confidence": classification.get("confidence", ""),
            "property_summary": " | ".join(property_parts),
            "evidence_source_type": evidence_source_type,
            "source_confidence": "" if used_llm else search_result.get("source_confidence", ""),
            "llm_confidence": llm_confidence,
            "evidence_quality": search_result.get("evidence_quality", ""),
            "source_url": search_result.get("url") or search_result.get("fallback_url") or "",
            "flash_point": extracted.get("flash_point", ""),
            "boiling_point": extracted.get("boiling_point", ""),
            "toxicity": extracted.get("toxicity", ""),
            "corrosive": extracted.get("corrosive", ""),
            "oxidizing": extracted.get("oxidizing", ""),
            "flammable": extracted.get("flammable", ""),
            "water_reactive": extracted.get("water_reactive", ""),
            "explosive_risk": extracted.get("explosive_risk", ""),
            "used_llm_knowledge_fallback": used_llm,
            "review_advice": advice,
        }
        fields.update(
            review_display_summary(
                reagent=reagent,
                name_result=name_result,
                search_result=search_result,
                extracted=extracted,
                classification=classification,
                evidence_fields=fields,
                reason=reason,
            )
        )
        return fields


def review_display_summary(
    *,
    reagent: dict[str, Any] | None = None,
    name_result: dict[str, Any] | None = None,
    search_result: dict[str, Any] | None = None,
    extracted: dict[str, Any] | None = None,
    classification: dict[str, Any] | None = None,
    evidence_fields: dict[str, Any] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    reagent = reagent or {}
    name_result = name_result or {}
    search_result = search_result or {}
    extracted = extracted or {}
    classification = classification or {}
    evidence_fields = evidence_fields or {}

    suggested = str(
        evidence_fields.get("suggested_category")
        or classification.get("final_category")
        or ", ".join(str(item) for item in classification.get("matched_categories", []) or [])
    ).strip()
    source_type = str(evidence_fields.get("evidence_source_type") or "").strip()
    source = str(search_result.get("source") or search_result.get("fallback_source") or "").strip()
    source_confidence = str(evidence_fields.get("source_confidence") or search_result.get("source_confidence") or "").strip()
    llm_confidence = str(evidence_fields.get("llm_confidence") or search_result.get("llm_confidence") or "").strip()
    raw_reason = " ".join(str(value or "") for value in (reason, search_result.get("failure_reason"), evidence_fields.get("property_summary")))
    text_for_salt_check = " ".join(
        str(value or "")
        for value in (
            reagent.get("试剂名称"),
            reagent.get("chemical_name"),
            reagent.get("reagent_name"),
            name_result.get("raw_name"),
            name_result.get("cleaned_name"),
            name_result.get("standard_name"),
            extracted.get("name"),
            search_result.get("name"),
        )
    )

    llm_failed = _llm_failed(raw_reason)
    salt_like = _looks_like_acid_salt(text_for_salt_check)
    trusted_web = source_type == "trusted_web" or source in {"Chemsrc", "ChemicalBook"}
    used_llm = source_type == "llm_fallback" or _truthy(evidence_fields.get("used_llm_knowledge_fallback"))

    detail_parts = []
    if evidence_fields.get("property_summary"):
        property_summary = str(evidence_fields.get("property_summary"))
        detail_parts.append(_compact_error(property_summary, limit=320) if _llm_failed(property_summary) else property_summary)
    if raw_reason.strip():
        detail_parts.append(_compact_error(raw_reason, limit=320))
    detail_summary = " | ".join(part for part in detail_parts if part)

    if salt_like and not trusted_web:
        return {
            "display_suggestion": "暂无可靠建议",
            "display_reason": "名称包含盐酸盐/无机酸盐结构，不能直接按对应无机酸判定；需人工核对物化特性。",
            "evidence_status": "证据不足",
            "detail_summary": detail_summary,
            "allow_suggestion_preselect": False,
        }

    if llm_failed and not trusted_web:
        return {
            "display_suggestion": "暂无可靠建议",
            "display_reason": f"网站未查到可信资料，{_llm_failure_label(raw_reason)}，需人工核对。",
            "evidence_status": "证据不足",
            "detail_summary": detail_summary,
            "allow_suggestion_preselect": False,
        }

    if trusted_web:
        evidence_status = f"{source or '可信网站'}"
        if source_confidence:
            evidence_status = f"{evidence_status}，资料可信度 {source_confidence}"
        return {
            "display_suggestion": suggested or "需人工判定",
            "display_reason": f"根据 {source or '可信网站'} 资料和规则判断，需人工确认后入库。",
            "evidence_status": evidence_status,
            "detail_summary": detail_summary,
            "allow_suggestion_preselect": bool(suggested),
        }

    if used_llm and suggested:
        evidence_status = "LLM辅助"
        if llm_confidence:
            evidence_status = f"{evidence_status}，置信度 {llm_confidence}"
        return {
            "display_suggestion": f"LLM辅助建议：{suggested}",
            "display_reason": "未查到可信网站资料，LLM仅给出辅助判断，需人工核验。",
            "evidence_status": evidence_status,
            "detail_summary": detail_summary,
            "allow_suggestion_preselect": True,
        }

    if suggested:
        return {
            "display_suggestion": suggested,
            "display_reason": "仅命中本地规则或证据不足，需人工确认后再入库。",
            "evidence_status": "规则辅助，需人工确认",
            "detail_summary": detail_summary,
            "allow_suggestion_preselect": True,
        }

    return {
        "display_suggestion": "暂无可靠建议",
        "display_reason": "缺少可信网站资料或可用的辅助判断，需人工核对。",
        "evidence_status": "证据不足",
        "detail_summary": detail_summary,
        "allow_suggestion_preselect": False,
    }


def review_display_summary_from_row(row: Any, reason: str = "") -> dict[str, Any]:
    def value(key: str) -> str:
        try:
            return str(row.get(key) or "").strip()
        except AttributeError:
            return ""

    evidence_fields = {column: value(column) for column in REVIEW_EVIDENCE_COLUMNS}
    classification = {
        "final_category": value("suggested_category"),
        "confidence": value("classification_confidence"),
    }
    search_result = {
        "source": value("source"),
        "source_confidence": value("source_confidence"),
        "llm_confidence": value("llm_confidence"),
        "failure_reason": reason,
        "used_llm_knowledge_fallback": value("used_llm_knowledge_fallback"),
    }
    extracted = {
        "name": value("chemical_name") or value("reagent_name"),
        "flash_point": value("flash_point"),
        "boiling_point": value("boiling_point"),
        "toxicity": value("toxicity"),
    }
    return review_display_summary(
        reagent={"chemical_name": value("chemical_name") or value("试剂名称") or value("reagent_name")},
        name_result={"standard_name": value("standard_name"), "cleaned_name": value("cleaned_name")},
        search_result=search_result,
        extracted=extracted,
        classification=classification,
        evidence_fields=evidence_fields,
        reason=reason,
    )


def _looks_like_acid_salt(text: str) -> bool:
    normalized = str(text or "").lower().replace(" ", "")
    return any(
        token in normalized
        for token in (
            "盐酸盐",
            "盐酸苯肼",
            "hydrochloride",
            "nitratesalt",
            "sulfatesalt",
            "sulphatesalt",
        )
    )


def _llm_failed(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        token in lowered
        for token in (
            "llm extraction failed",
            "llm knowledge fallback failed",
            "error code: 402",
            "balance is insufficient",
            "insufficient",
            "api key",
            "timeout",
        )
    )


def _llm_failure_label(text: str) -> str:
    lowered = str(text or "").lower()
    if "402" in lowered or "balance is insufficient" in lowered or "insufficient" in lowered:
        return "LLM辅助失败：账户余额不足"
    if "api key" in lowered:
        return "LLM辅助失败：API Key未配置"
    if "timeout" in lowered:
        return "LLM辅助失败：调用超时"
    return "LLM辅助失败"


def _compact_error(text: str, limit: int = 220) -> str:
    compact = " ".join(str(text or "").split())
    if _llm_failed(compact):
        compact = _llm_failure_label(compact)
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}
