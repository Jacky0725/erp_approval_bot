from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
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
    "used_llm_rule_fallback",
    "used_llm_manual_review_advice",
    "llm_rule_confidence",
    "llm_rule_reason",
    "llm_rule_matched_rule",
    "llm_rule_evidence_type",
    "llm_rule_must_manual_review",
    "llm_advisory_category",
    "llm_advisory_summary_cn",
    "llm_advisory_reason_cn",
    "llm_advisory_rule_cn",
    "llm_advisory_uncertainties_cn",
    "llm_advisory_confidence",
    "llm_advisory_evidence_basis",
    "llm_advisory_only",
    "llm_advisory_high_risk",
    "llm_model",
    "llm_provider",
    "llm_generated_at",
    "llm_rules_fingerprint",
    "reason_raw",
    "review_advice",
    "original_erp_cas",
    "corrected_cas",
    "cas_name_conflict",
    "cas_correction_applied",
    "cas_correction_reason",
    "cas_correction_source",
    "cas_correction_url",
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

_MIGRATED_REVIEW_SIGNATURES: dict[str, int] = {}


REVIEW_QUEUE_COLUMN_ALIASES = {
    "�Լ��嵥��": "试剂清单号",
    "�Լ�����": "试剂名称",
    "���": "序号",
    "CAS��": "CAS号",
    "����": "规格",
    "������λ": "规格单位",
    "��׼����": "standard_name",
    "��ϴ������": "cleaned_name",
}


def canonicalize_review_queue_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize legacy mojibake review-queue headers without dropping data."""
    if frame.empty:
        return frame
    result = frame.copy()
    for alias, canonical in REVIEW_QUEUE_COLUMN_ALIASES.items():
        if alias not in result.columns:
            continue
        if canonical in result.columns:
            result[canonical] = result[canonical].where(
                result[canonical].astype(str).str.strip() != "",
                result[alias],
            )
            result = result.drop(columns=[alias])
        else:
            result = result.rename(columns={alias: canonical})
    return result


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
            queue = canonicalize_review_queue_columns(pd.read_excel(review_queue_path, dtype=str).fillna(""))
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

        status_column = next((column for column in ("status", "状态", "处理状态") if column in queue.columns), "")
        if status_column:
            normalized_status = queue[status_column].astype(str).str.strip().str.lower()
            matched = matched & normalized_status.isin(BLOCKING_REVIEW_STATUSES)

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
            queue = canonicalize_review_queue_columns(pd.read_excel(review_queue_path, dtype=str).fillna(""))
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
        migrate_pending_review_reasons(review_queue_path)

        try:
            queue = (
                canonicalize_review_queue_columns(pd.read_excel(review_queue_path, dtype=str).fillna(""))
                if review_queue_path.exists()
                else pd.DataFrame()
            )
        except Exception:
            queue = pd.DataFrame()

        list_number = detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7", "")
        chemical_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "")
        cas = (
            (search_result or {}).get("corrected_cas")
            or name_result.get("corrected_cas")
            or (search_result or {}).get("cas")
            or name_result.get("cas")
            or reagent.get("CAS\u53f7", "")
        )
        sequence = reagent.get("\u5e8f\u53f7", "")
        specification = reagent.get("\u89c4\u683c", "")
        unit = reagent.get("\u89c4\u683c\u5355\u4f4d", "")
        raw_reason = str(reason or "Manual review is required before writing this reagent to ERP.")
        reason = manual_review_reason_cn(
            raw_reason,
            evidence_source_type=str((search_result or {}).get("evidence_source_type") or ""),
        )
        evidence_fields = self._manual_review_evidence_fields(
            reagent=reagent,
            name_result=name_result,
            search_result=search_result or {},
            extracted=extracted or {},
            classification=classification or {},
            reason=reason,
        )
        evidence_fields["reason_raw"] = raw_reason if raw_reason != reason else ""

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
            sequence_column = next((column for column in sequence_columns if column in queue.columns), "")
            if list_column and sequence_column and sequence:
                existing_match = (
                    (queue[list_column].astype(str).str.strip() == list_number)
                    & (queue[sequence_column].astype(str).str.strip() == str(sequence or "").strip())
                )
            elif list_column and name_column:
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
                if column == "reason_raw" and str(queue.at[index, column] or "").strip():
                    continue
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
        used_llm_rule = bool(search_result.get("used_llm_rule_fallback") or extracted.get("used_llm_rule_fallback"))
        used_llm = bool(search_result.get("used_llm_knowledge_fallback") or extracted.get("used_llm_knowledge_fallback"))
        used_llm_advice = bool(
            search_result.get("used_llm_manual_review_advice")
            or search_result.get("llm_advisory_only")
            or extracted.get("llm_advisory_only")
        )
        source = str(search_result.get("source") or search_result.get("fallback_source") or "").strip()
        if used_llm_advice:
            evidence_source_type = "llm_manual_review_advice"
        elif used_llm_rule:
            evidence_source_type = "llm_rule_fallback"
        elif used_llm:
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
            ("闪点", "flash_point"),
            ("沸点", "boiling_point"),
            ("毒性", "toxicity"),
            ("腐蚀性", "corrosive"),
            ("氧化性", "oxidizing"),
            ("易燃性", "flammable"),
            ("遇水反应", "water_reactive"),
            ("爆炸风险", "explosive_risk"),
        ):
            value = extracted.get(key)
            if value not in ("", None, []):
                property_parts.append(f"{label}：{localize_review_detail_text(value)}")
        if evidence_text:
            property_parts.append(f"证据：{localize_review_detail_text(evidence_text[:300])}")
        if used_llm_rule:
            if search_result.get("llm_rule_reason"):
                property_parts.append(f"llm_rule_reason={search_result.get('llm_rule_reason')}")
            if search_result.get("llm_rule_matched_rule"):
                property_parts.append(f"llm_rule_matched_rule={search_result.get('llm_rule_matched_rule')}")

        advice = "需人工确认物化特性后再处理，系统不会自动写入 ERP。"
        if used_llm_advice:
            advice = "LLM 仅提供第二意见，需人工主动选择物化特性，系统不会自动预选或写入 ERP。"
        elif used_llm_rule:
            advice = "LLM 规则辅助意见仅供参考，需人工确认后再处理。"
        elif used_llm:
            advice = "网页证据缺失或不足，LLM 辅助意见仅供参考，必须人工核验。"
        elif evidence_source_type == "trusted_web":
            advice = "已有可信网站资料，请人工核对建议类别和来源证据。"
        elif evidence_source_type == "web_fallback":
            advice = "已有网页兜底资料，但内容可能不完整，请人工核验后确认。"

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
            "used_llm_rule_fallback": used_llm_rule,
            "used_llm_manual_review_advice": used_llm_advice,
            "llm_rule_confidence": search_result.get("llm_rule_confidence")
            or extracted.get("llm_rule_confidence", ""),
            "llm_rule_reason": search_result.get("llm_rule_reason") or extracted.get("llm_rule_reason", ""),
            "llm_rule_matched_rule": search_result.get("llm_rule_matched_rule")
            or extracted.get("llm_rule_matched_rule", ""),
            "llm_rule_evidence_type": search_result.get("llm_rule_evidence_type") or "",
            "llm_rule_must_manual_review": search_result.get("llm_rule_must_manual_review")
            or extracted.get("llm_rule_must_manual_review", False),
            "llm_advisory_category": search_result.get("llm_advisory_category")
            or extracted.get("llm_advisory_category", ""),
            "llm_advisory_summary_cn": search_result.get("llm_advisory_summary_cn")
            or extracted.get("llm_advisory_summary_cn", ""),
            "llm_advisory_reason_cn": search_result.get("llm_advisory_reason_cn")
            or extracted.get("llm_advisory_reason_cn", ""),
            "llm_advisory_rule_cn": search_result.get("llm_advisory_rule_cn")
            or extracted.get("llm_advisory_rule_cn", ""),
            "llm_advisory_uncertainties_cn": search_result.get("llm_advisory_uncertainties_cn")
            or "；".join(extracted.get("llm_advisory_uncertainties_cn", []) or []),
            "llm_advisory_confidence": search_result.get("llm_advisory_confidence")
            or extracted.get("llm_advisory_confidence", ""),
            "llm_advisory_evidence_basis": search_result.get("llm_advisory_evidence_basis", ""),
            "llm_advisory_only": used_llm_advice,
            "llm_advisory_high_risk": search_result.get("llm_advisory_high_risk", False),
            "llm_model": search_result.get("llm_model", ""),
            "llm_provider": search_result.get("llm_provider", ""),
            "llm_generated_at": search_result.get("llm_generated_at", ""),
            "llm_rules_fingerprint": search_result.get("llm_rules_fingerprint", ""),
            "review_advice": advice,
            "original_erp_cas": search_result.get("original_erp_cas") or name_result.get("original_erp_cas") or "",
            "corrected_cas": search_result.get("corrected_cas") or name_result.get("corrected_cas") or "",
            "cas_name_conflict": search_result.get("cas_name_conflict") or name_result.get("cas_name_conflict") or False,
            "cas_correction_applied": search_result.get("cas_correction_applied")
            or name_result.get("cas_correction_applied")
            or False,
            "cas_correction_reason": search_result.get("cas_correction_reason")
            or name_result.get("cas_correction_reason")
            or "",
            "cas_correction_source": search_result.get("cas_correction_source")
            or name_result.get("cas_correction_source")
            or "",
            "cas_correction_url": search_result.get("cas_correction_url") or name_result.get("cas_correction_url") or "",
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
    detail_reason = " ".join(str(value or "") for value in (reason, search_result.get("failure_reason")))
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
    used_llm_rule = source_type == "llm_rule_fallback" or _truthy(evidence_fields.get("used_llm_rule_fallback"))
    used_llm_advice = source_type == "llm_manual_review_advice" or _truthy(
        evidence_fields.get("llm_advisory_only")
    )
    advisory_category = str(evidence_fields.get("llm_advisory_category") or "").strip()
    advisory_summary = str(evidence_fields.get("llm_advisory_summary_cn") or "").strip()
    advisory_reason = str(evidence_fields.get("llm_advisory_reason_cn") or "").strip()
    advisory_rule = str(evidence_fields.get("llm_advisory_rule_cn") or "").strip()
    advisory_uncertainties = str(evidence_fields.get("llm_advisory_uncertainties_cn") or "").strip()
    advisory_confidence = _float(evidence_fields.get("llm_advisory_confidence"), 0.0)
    advisory_basis = str(evidence_fields.get("llm_advisory_evidence_basis") or "证据不足").strip()
    llm_rule_confidence = _float(evidence_fields.get("llm_rule_confidence"), 0.0)
    llm_rule_reason = str(evidence_fields.get("llm_rule_reason") or "").strip()
    llm_rule_matched_rule = str(evidence_fields.get("llm_rule_matched_rule") or "").strip()
    cas_correction_applied = _truthy(evidence_fields.get("cas_correction_applied"))
    original_erp_cas = str(evidence_fields.get("original_erp_cas") or "").strip()
    corrected_cas = str(evidence_fields.get("corrected_cas") or "").strip()
    acid_suggestion = suggested in {"常规酸", "特殊酸"} or advisory_category in {"常规酸", "特殊酸"}

    detail_parts = []
    if evidence_fields.get("property_summary"):
        property_summary = str(evidence_fields.get("property_summary"))
        if _llm_failed(property_summary):
            detail_parts.append(_compact_error(property_summary, limit=320))
        else:
            detail_parts.append(localize_review_detail_text(property_summary))
    if cas_correction_applied:
        detail_parts.append(
            f"原 CAS：{original_erp_cas or '-'}；修正 CAS：{corrected_cas or '-'}；"
            f"来源：{evidence_fields.get('cas_correction_source') or source or '-'}"
        )
    if detail_reason.strip():
        detail_parts.append(localize_review_detail_text(_compact_error(detail_reason, limit=320)))
    detail_summary = " | ".join(part for part in detail_parts if part)

    if cas_correction_applied and corrected_cas:
        return {
            "display_suggestion": "CAS 已按试剂名称自动修正",
            "display_reason": (
                "ERP CAS 与试剂名称不匹配，已按试剂名称在可信网站查到更匹配 CAS；"
                f"原 CAS：{original_erp_cas or '-'}，修正 CAS：{corrected_cas}。"
            ),
            "evidence_status": f"{evidence_fields.get('cas_correction_source') or source or '可信网站'}，CAS修正已应用",
            "detail_summary": detail_summary,
            "allow_suggestion_preselect": bool(suggested),
        }

    if salt_like and acid_suggestion and not trusted_web:
        return {
            "display_suggestion": "暂无可靠建议",
            "display_reason": "名称包含酸盐/有机酸盐结构，不能直接按对应无机酸判定；需人工核对物化特性。",
            "evidence_status": "证据不足",
            "detail_summary": detail_summary,
            "allow_suggestion_preselect": False,
        }

    if used_llm_advice:
        confidence_text = f"{advisory_confidence:.2f}" if advisory_confidence else "未知"
        advisory_details = [
            f"物性意见：{advisory_summary}" if advisory_summary else "",
            f"规则依据：{advisory_rule}" if advisory_rule else "",
            f"不确定项：{advisory_uncertainties}" if advisory_uncertainties else "",
        ]
        return {
            "display_suggestion": f"LLM第二意见：{advisory_category or '暂无可靠建议'}",
            "display_reason": advisory_reason or "LLM 未形成可靠建议，请人工核对物化特性。",
            "evidence_status": f"LLM第二意见，{advisory_basis}，置信度 {confidence_text}，仅供复核",
            "detail_summary": " | ".join([detail_summary, *advisory_details]).strip(" |"),
            "allow_suggestion_preselect": False,
        }

    if used_llm_rule:
        confidence_text = f"{llm_rule_confidence:.2f}" if llm_rule_confidence else "未知"
        rule_detail = []
        if llm_rule_reason:
            rule_detail.append(f"依据：{llm_rule_reason}")
        if llm_rule_matched_rule:
            rule_detail.append(f"匹配规则：{llm_rule_matched_rule}")
        return {
            "display_suggestion": f"LLM按规则辅助建议：{suggested or '暂无可靠建议'}",
            "display_reason": llm_rule_reason or "没有足够可信网页证据，LLM 仅按规则给出辅助判断，需人工确认。",
            "evidence_status": f"LLM规则辅助，置信度 {confidence_text}，需人工确认",
            "detail_summary": " | ".join([detail_summary, *rule_detail]).strip(" |"),
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
            "allow_suggestion_preselect": False,
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
    salt_terms = (
        "盐酸盐",
        "硝酸盐",
        "硫酸盐",
        "磷酸盐",
        "磺酸盐",
        "羧酸盐",
        "钠盐",
        "钾盐",
        "铵盐",
        "酚钠盐",
        "酚钠",
        "hydrochloride",
        "nitrate",
        "sulfate",
        "sulphate",
        "sulfonate",
        "sulphonate",
        "carboxylate",
        "phenolate",
        "urate",
        "acidsalt",
        "ammoniumsalt",
        "sodiumsalt",
        "potassiumsalt",
    )
    if any(token in normalized for token in salt_terms):
        return True
    return bool(
        re.search(r"酸(钠|钾|铵|銨|氨)", normalized)
        or re.search(r"(盐酸|硝酸|硫酸|磷酸|磺酸|羧酸|酚).{0,12}(钠|钾|铵|銨|氨)", normalized)
        or re.search(r"(钠|钾|铵|銨|氨).{0,12}(盐酸|硝酸|硫酸|磷酸|磺酸|羧酸|酚)", normalized)
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


def localize_review_detail_text(text: Any) -> str:
    detail = str(text or "").strip()
    if not detail:
        return ""

    key_labels = {
        "flash_point": "闪点",
        "boiling_point": "沸点",
        "toxicity": "毒性",
        "corrosive": "腐蚀性",
        "oxidizing": "氧化性",
        "flammable": "易燃性",
        "water_reactive": "遇水反应",
        "explosive_risk": "爆炸风险",
        "evidence": "证据",
        "llm_rule_reason": "LLM规则辅助依据",
        "llm_rule_matched_rule": "LLM匹配规则",
    }

    value_replacements = [
        ("No specific acute toxicity data provided.", "未提供明确的急性毒性数据。"),
        ("No specific acute toxicity data provided", "未提供明确的急性毒性数据"),
        ("LLM fallback evidence is advisory only and requires manual review.", "LLM 辅助证据仅供参考，需要人工复核。"),
        ("LLM fallback evidence is advisory only and requires manual review", "LLM 辅助证据仅供参考，需要人工复核"),
        ("Chemsrc 和 ChemicalBook 均查询失败或无有效结果。", "Chemsrc 和 ChemicalBook 均查询失败或无有效结果。"),
        ("No trusted web evidence was found, so low-confidence LLM knowledge fallback was used.", "未找到可信网页证据，因此使用低置信度的 LLM 知识兜底。"),
        ("No trusted web evidence was found", "未找到可信网页证据"),
        ("No web evidence found.", "未找到网页证据。"),
        ("Summary is based on conservative LLM fallback.", "摘要基于保守的 LLM 兜底判断。"),
        ("Based on general chemical knowledge", "根据通用化学知识"),
        ("it is expected to be", "预计为"),
        ("It is likely highly soluble in water", "可能高度溶于水"),
        ("due to its ionic nature and polar sulfonate group", "因为其离子特性和极性磺酸基"),
        ("It is probably hygroscopic.", "可能具有吸湿性。"),
        ("Its melting point is uncertain", "熔点不确定"),
        ("white to off-white crystalline solid or powder", "白色至类白色结晶固体或粉末"),
        ("at room temperature", "在室温下"),
    ]

    def localize_value(value: str) -> str:
        localized = value.strip()
        for source, target in value_replacements:
            localized = localized.replace(source, target)
        return localized

    parts = []
    for raw_part in re.split(r"\s*\|\s*", detail):
        part = raw_part.strip()
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            label = key_labels.get(key.strip(), key.strip())
            parts.append(f"{label}：{localize_value(value)}")
        else:
            parts.append(localize_value(part))
    result = "；".join(parts)
    language_check = re.sub(
        r"https?://\S+|\b(?:LLM|API|Key|ERP|CAS|SDS|MSDS|Chemsrc|ChemicalBook)\b|\b[A-Z][A-Za-z0-9()+\-]{0,12}\b",
        "",
        result,
        flags=re.I,
    )
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", language_check))
    latin_words = len(re.findall(r"[A-Za-z]{3,}", language_check))
    if latin_words >= 4 and cjk_count < 6:
        return "原始资料包含较多英文内容，请结合来源链接或原始审计信息人工核对。"
    return result


def manual_review_reason_cn(text: Any, evidence_source_type: str = "") -> str:
    """Return a concise Chinese user-facing reason while preserving raw text elsewhere."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return "缺少足够可信的物性证据，需要人工确认物化特性。"
    localized = localize_review_detail_text(raw)
    replacements = [
        ("Manual review is required before writing this reagent to ERP.", "写入 ERP 前需要人工确认物化特性。"),
        ("Rule classification, name normalization, or source evidence requires manual review.", "规则判定、名称标准化或来源证据存在不确定性，需要人工复核。"),
        ("pending manual review", "存在待处理的人工复核项"),
        ("needs manual review", "需要人工复核"),
        ("manual review", "人工复核"),
        ("No trusted web evidence was found", "未找到可信网页证据"),
        ("no trusted web evidence", "未找到可信网页证据"),
        ("Website evidence is missing or insufficient", "网页证据缺失或不足"),
        ("insufficient website evidence", "网页证据不足"),
        ("low confidence", "置信度较低"),
        ("uncertain evidence", "证据存在不确定性"),
        ("LLM/classification failed", "LLM 提取或规则判定失败"),
        ("LLM extraction failed", "LLM 物性提取失败"),
        ("candidate category matched", "命中候选类别"),
        ("trusted evidence", "已有可信证据"),
        ("could not select", "无法选择"),
        ("could not open", "无法打开"),
        ("could not verify", "无法验证"),
        ("write failed", "写入失败"),
    ]
    for source, target in replacements:
        localized = localized.replace(source, target)
    language_check = re.sub(
        r"https?://\S+|\b(?:LLM|API|Key|ERP|CAS|SDS|MSDS|Chemsrc|ChemicalBook)\b",
        "",
        localized,
        flags=re.I,
    )
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", language_check))
    latin_words = len(re.findall(r"[A-Za-z]{3,}", language_check))
    if (cjk_count >= 6 and latin_words <= 2) or latin_words == 0:
        return localized
    source_type = str(evidence_source_type or "").strip()
    if source_type in {"llm_fallback", "llm_rule_fallback", "llm_manual_review_advice"} or "llm" in raw.lower():
        return "LLM 辅助信息仅供参考，现有依据不足以自动判定，请人工核对物化特性。"
    if source_type == "trusted_web":
        return "已有可信网页资料，但规则判定仍存在不确定性，需要人工核对物化特性。"
    if source_type == "web_fallback":
        return "网页兜底资料可能不完整，需要人工核对试剂身份和物化特性。"
    return "试剂名称、来源证据或规则判定存在不确定性，需要人工核对物化特性。"


def migrate_pending_review_reasons(path: Path) -> bool:
    """Localize pending review reasons once, preserving original audit text."""
    path = Path(path)
    if not path.exists():
        return False
    cache_key = str(path.resolve())
    try:
        signature = path.stat().st_mtime_ns
    except OSError:
        return False
    if _MIGRATED_REVIEW_SIGNATURES.get(cache_key) == signature:
        return False
    try:
        frame = canonicalize_review_queue_columns(pd.read_excel(path, dtype=str).fillna(""))
    except Exception:
        return False
    if frame.empty or "reason" not in frame.columns:
        _MIGRATED_REVIEW_SIGNATURES[cache_key] = signature
        return False
    status_column = next((column for column in ("status", "状态", "处理状态") if column in frame.columns), "")
    if status_column:
        pending = frame[status_column].astype(str).str.strip().str.lower().isin(BLOCKING_REVIEW_STATUSES)
    else:
        pending = pd.Series(True, index=frame.index)
    changed = False
    if "reason_raw" not in frame.columns:
        frame["reason_raw"] = ""
    source_column = "evidence_source_type" if "evidence_source_type" in frame.columns else ""
    for index in frame[pending].index:
        old_reason = str(frame.at[index, "reason"] or "").strip()
        if not old_reason:
            continue
        source_type = str(frame.at[index, source_column] or "") if source_column else ""
        localized = manual_review_reason_cn(old_reason, evidence_source_type=source_type)
        if localized == old_reason:
            continue
        if not str(frame.at[index, "reason_raw"] or "").strip():
            frame.at[index, "reason_raw"] = old_reason
        frame.at[index, "reason"] = localized
        changed = True
    if not changed:
        _MIGRATED_REVIEW_SIGNATURES[cache_key] = signature
        return False
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}.before_chinese_remarks.{timestamp}{path.suffix}")
    try:
        shutil.copy2(path, backup)
        frame.to_excel(path, index=False)
    except Exception:
        return False
    try:
        _MIGRATED_REVIEW_SIGNATURES[cache_key] = path.stat().st_mtime_ns
    except OSError:
        pass
    return True


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
