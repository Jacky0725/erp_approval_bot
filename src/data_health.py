from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from reagent_memory import ReagentMemory
from review_queue import canonicalize_review_queue_columns


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

MEMORY_TEXT_FIELDS = (
    "raw_name",
    "cleaned_name",
    "standard_name",
    "final_category",
    "reason",
    "source",
    "url",
)
REVIEW_LIST_COLUMNS = ("试剂清单号", "当前清单号", "清单号", "list_number")
REVIEW_SEQUENCE_COLUMNS = ("序号", "sequence", "index")
REVIEW_STATUS_COLUMNS = ("status", "状态", "处理状态")
REVIEW_CATEGORY_COLUMNS = ("manual_result", "confirmed_category", "final_category", "suggested_category")


@dataclass(frozen=True)
class DataHealthPaths:
    root_dir: Path
    memory_path: Path
    review_queue_path: Path
    suggestions_path: Path
    log_dir: Path


def data_health_paths(root_dir: Path, settings: dict[str, Any] | None = None) -> DataHealthPaths:
    settings = settings or {}
    paths = settings.get("paths", {}) or {}
    return DataHealthPaths(
        root_dir=Path(root_dir),
        memory_path=Path(root_dir) / paths.get("reagent_memory_sqlite", "data/reagent_memory.sqlite"),
        review_queue_path=Path(root_dir) / paths.get("review_queue_excel", "data/review_queue.xlsx"),
        suggestions_path=Path(root_dir) / paths.get("approval_suggestions_excel", "data/logs/approval_suggestions.xlsx"),
        log_dir=Path(root_dir) / "data" / "logs",
    )


def mojibake_score(value: Any) -> int:
    text = str(value or "")
    return sum(text.count(marker) for marker in MOJIBAKE_MARKERS) + text.count("?")


def looks_mojibake(value: Any) -> bool:
    return mojibake_score(value) > 0


def repair_text(value: Any) -> str:
    text = str(value or "")
    if not looks_mojibake(text):
        return text
    candidates = {text}
    for encoding in ("latin1", "cp1252", "gbk", "cp936"):
        for errors in ("strict", "replace"):
            try:
                candidates.add(text.encode(encoding, errors=errors).decode("utf-8", errors=errors))
            except UnicodeError:
                continue
    return min(candidates, key=lambda item: (mojibake_score(item), len(item)))


def is_recoverable_mojibake(value: Any) -> bool:
    text = str(value or "")
    repaired = repair_text(text)
    return bool(text and repaired != text and mojibake_score(repaired) < mojibake_score(text))


def data_health_summary(root_dir: Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    report = audit_data_health(root_dir, settings=settings, include_issue_rows=False)
    summary = report["summary"]
    return {
        "memory_exists": summary["memory_exists"],
        "review_queue_exists": summary["review_queue_exists"],
        "memory_mojibake_records": summary["memory_mojibake_records"],
        "reusable_mojibake_records": summary["reusable_mojibake_records"],
        "unrecoverable_reusable_mojibake_records": summary[
            "unrecoverable_reusable_mojibake_records"
        ],
        "conflicting_memory_records": summary["conflicting_memory_records"],
        "confirmed_review_missing_memory": summary["confirmed_review_missing_memory"],
        "duplicate_review_items": summary["duplicate_review_items"],
        "last_checked_at": summary["checked_at"],
    }


def audit_data_health(
    root_dir: Path,
    *,
    settings: dict[str, Any] | None = None,
    include_issue_rows: bool = True,
) -> dict[str, Any]:
    paths = data_health_paths(root_dir, settings)
    summary: dict[str, Any] = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "memory_path": str(paths.memory_path),
        "review_queue_path": str(paths.review_queue_path),
        "suggestions_path": str(paths.suggestions_path),
        "memory_exists": paths.memory_path.exists(),
        "review_queue_exists": paths.review_queue_path.exists(),
        "suggestions_exists": paths.suggestions_path.exists(),
        "memory_rows": 0,
        "memory_mojibake_records": 0,
        "reusable_mojibake_records": 0,
        "unrecoverable_reusable_mojibake_records": 0,
        "unsafe_reusable_records": 0,
        "conflicting_memory_records": 0,
        "review_rows": 0,
        "confirmed_review_rows": 0,
        "confirmed_review_missing_memory": 0,
        "duplicate_review_items": 0,
        "suggestion_rows": 0,
        "suggestion_mojibake_records": 0,
    }
    issues: list[dict[str, Any]] = []
    memory_issues = _audit_memory(paths, settings or {}, include_issue_rows)
    review_issues = _audit_review_queue(paths, settings or {}, include_issue_rows)
    suggestion_issues = _audit_suggestions(paths, include_issue_rows)
    for partial in (memory_issues, review_issues, suggestion_issues):
        summary.update(partial["summary"])
        issues.extend(partial.get("issues", []))
    return {
        "summary": summary,
        "issues": issues,
        "memory_issues": memory_issues.get("issues", []),
        "review_issues": review_issues.get("issues", []),
        "suggestion_issues": suggestion_issues.get("issues", []),
    }


def export_data_health_audit(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_rows = [{"metric": key, "value": value} for key, value in report["summary"].items()]
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="summary", index=False)
        pd.DataFrame(report.get("issues") or []).to_excel(writer, sheet_name="issues", index=False)
        pd.DataFrame(report.get("memory_issues") or []).to_excel(writer, sheet_name="memory", index=False)
        pd.DataFrame(report.get("review_issues") or []).to_excel(writer, sheet_name="review_queue", index=False)
        pd.DataFrame(report.get("suggestion_issues") or []).to_excel(writer, sheet_name="suggestions", index=False)
    return output_path


def run_data_health_audit(
    root_dir: Path,
    *,
    settings: dict[str, Any] | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    report = audit_data_health(root_dir, settings=settings)
    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = data_health_paths(root_dir, settings).log_dir / f"mojibake_audit_{timestamp}.xlsx"
    export_data_health_audit(report, output_path)
    report["output_path"] = str(output_path)
    return report


def apply_data_health_repairs(
    root_dir: Path,
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = data_health_paths(root_dir, settings)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backups: dict[str, str] = {}
    if paths.memory_path.exists():
        backup = paths.log_dir / f"reagent_memory_backup_before_data_health_{timestamp}.sqlite"
        shutil.copy2(paths.memory_path, backup)
        backups["memory"] = str(backup)
    if paths.review_queue_path.exists():
        backup = paths.log_dir / f"review_queue_backup_before_data_health_{timestamp}.xlsx"
        shutil.copy2(paths.review_queue_path, backup)
        backups["review_queue"] = str(backup)

    memory_result = _repair_memory(paths, settings or {})
    review_result = _repair_review_queue(paths, settings or {})
    return {
        "applied": True,
        "backups": backups,
        "memory": memory_result,
        "review_queue": review_result,
    }


def _audit_memory(paths: DataHealthPaths, settings: dict[str, Any], include_rows: bool) -> dict[str, Any]:
    summary = {
        "memory_rows": 0,
        "memory_mojibake_records": 0,
        "reusable_mojibake_records": 0,
        "unrecoverable_reusable_mojibake_records": 0,
        "unsafe_reusable_records": 0,
        "conflicting_memory_records": 0,
    }
    issues: list[dict[str, Any]] = []
    if not paths.memory_path.exists():
        return {"summary": summary, "issues": issues}

    memory = ReagentMemory.from_settings(settings, paths.root_dir)
    memory._ensure_schema()  # noqa: SLF001 - audit must inspect the current SQLite schema safely.
    with closing(sqlite3.connect(paths.memory_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM reagent_memory ORDER BY id").fetchall()
    summary["memory_rows"] = len(rows)
    summary["conflicting_memory_records"] = sum(1 for row in rows if int(row["conflict"] or 0))
    for row in rows:
        row_dict = dict(row)
        field_issues = [
            {
                "field": field,
                "value": str(row_dict.get(field) or ""),
                "repaired": repair_text(row_dict.get(field)),
                "recoverable": is_recoverable_mojibake(row_dict.get(field)),
            }
            for field in MEMORY_TEXT_FIELDS
            if looks_mojibake(row_dict.get(field))
        ]
        unsafe = bool(row_dict.get("reusable")) and memory.is_unsafe_reusable_evidence(row_dict)
        if unsafe:
            summary["unsafe_reusable_records"] += 1
        if not field_issues and not unsafe:
            continue
        if field_issues:
            summary["memory_mojibake_records"] += 1
            if int(row_dict.get("reusable") or 0):
                summary["reusable_mojibake_records"] += 1
                if any(not item["recoverable"] for item in field_issues):
                    summary["unrecoverable_reusable_mojibake_records"] += 1
        if include_rows:
            for item in field_issues:
                issues.append(
                    _issue_row(
                        source="memory",
                        issue_type="mojibake",
                        identifier=row_dict.get("id"),
                        field=item["field"],
                        value=item["value"],
                        repaired=item["repaired"],
                        recoverable=item["recoverable"],
                        action=(
                            "repair_text"
                            if item["recoverable"]
                            else "disable_reusable_if_currently_reusable"
                        ),
                        row=row_dict,
                    )
                )
            if unsafe:
                issues.append(
                    _issue_row(
                        source="memory",
                        issue_type="unsafe_reusable_evidence",
                        identifier=row_dict.get("id"),
                        field="reason/source",
                        value=f"{row_dict.get('source')}\n{row_dict.get('reason')}",
                        action="disable_reusable",
                        row=row_dict,
                    )
                )
    return {"summary": summary, "issues": issues}


def _audit_review_queue(paths: DataHealthPaths, settings: dict[str, Any], include_rows: bool) -> dict[str, Any]:
    summary = {
        "review_rows": 0,
        "confirmed_review_rows": 0,
        "confirmed_review_missing_memory": 0,
        "duplicate_review_items": 0,
    }
    issues: list[dict[str, Any]] = []
    if not paths.review_queue_path.exists():
        return {"summary": summary, "issues": issues}
    frame = canonicalize_review_queue_columns(pd.read_excel(paths.review_queue_path, dtype=str).fillna(""))
    summary["review_rows"] = int(len(frame))
    if frame.empty:
        return {"summary": summary, "issues": issues}

    statuses = _series_first(frame, REVIEW_STATUS_COLUMNS).str.lower()
    confirmed_mask = statuses.eq("confirmed")
    summary["confirmed_review_rows"] = int(confirmed_mask.sum())

    key_columns = [_first_present(frame, REVIEW_LIST_COLUMNS), _first_present(frame, REVIEW_SEQUENCE_COLUMNS)]
    if all(key_columns):
        duplicate_count = int(frame.duplicated(key_columns, keep=False).sum())
        summary["duplicate_review_items"] = duplicate_count
        if include_rows and duplicate_count:
            for index, row in frame[frame.duplicated(key_columns, keep=False)].iterrows():
                issues.append(
                    _issue_row(
                        source="review_queue",
                        issue_type="duplicate_list_sequence",
                        identifier=index,
                        field="+".join(key_columns),
                        value="|".join(str(row.get(column) or "") for column in key_columns),
                        action="manual_review_dedup_check",
                        row=row.to_dict(),
                    )
                )

    memory = ReagentMemory.from_settings(settings, paths.root_dir)
    for index, row in frame[confirmed_mask].iterrows():
        row_dict = row.to_dict()
        if any(looks_mojibake(name) for name in frame.columns) and include_rows:
            issues.append(
                _issue_row(
                    source="review_queue",
                    issue_type="mojibake_header",
                    identifier=index,
                    field="columns",
                    value=", ".join(map(str, frame.columns)),
                    action="canonicalize_header",
                    row=row_dict,
                )
            )
        for field, value in row_dict.items():
            if looks_mojibake(value) and include_rows:
                issues.append(
                    _issue_row(
                        source="review_queue",
                        issue_type="mojibake",
                        identifier=index,
                        field=field,
                        value=value,
                        repaired=repair_text(value),
                        recoverable=is_recoverable_mojibake(value),
                        action="repair_text_if_recoverable",
                        row=row_dict,
                    )
                )
        category = _first_value(row, REVIEW_CATEGORY_COLUMNS)
        if not category:
            continue
        found = memory.find_any(
            raw_name=_first_value(row, ("试剂名称", "chemical_name", "reagent_name")),
            cleaned_name=_first_value(row, ("cleaned_name", "清洗后名称")),
            standard_name=_first_value(row, ("standard_name", "标准化名称")),
            cas=_first_value(row, ("cas", "CAS号")),
            final_category=category,
        )
        if found:
            continue
        summary["confirmed_review_missing_memory"] += 1
        if include_rows:
            issues.append(
                _issue_row(
                    source="review_queue",
                    issue_type="confirmed_missing_memory",
                    identifier=index,
                    field="manual_result",
                    value=category,
                    action="rebuild_memory_from_confirmed_review",
                    row=row_dict,
                )
            )
    return {"summary": summary, "issues": issues}


def _audit_suggestions(paths: DataHealthPaths, include_rows: bool) -> dict[str, Any]:
    summary = {"suggestion_rows": 0, "suggestion_mojibake_records": 0}
    issues: list[dict[str, Any]] = []
    if not paths.suggestions_path.exists():
        return {"summary": summary, "issues": issues}
    frame = pd.read_excel(paths.suggestions_path, dtype=str).fillna("")
    summary["suggestion_rows"] = int(len(frame))
    for index, row in frame.iterrows():
        row_has_issue = False
        for field, value in row.to_dict().items():
            if not looks_mojibake(value):
                continue
            row_has_issue = True
            if include_rows:
                issues.append(
                    _issue_row(
                        source="approval_suggestions",
                        issue_type="mojibake",
                        identifier=index,
                        field=field,
                        value=value,
                        repaired=repair_text(value),
                        recoverable=is_recoverable_mojibake(value),
                        action="review_source_export",
                        row=row.to_dict(),
                    )
                )
        if row_has_issue:
            summary["suggestion_mojibake_records"] += 1
    return {"summary": summary, "issues": issues}


def _repair_memory(paths: DataHealthPaths, settings: dict[str, Any]) -> dict[str, int]:
    if not paths.memory_path.exists():
        return {"repaired_fields": 0, "disabled_records": 0, "unsafe_disabled": 0}
    memory = ReagentMemory.from_settings(settings, paths.root_dir)
    memory._ensure_schema()  # noqa: SLF001 - repair must ensure the table exists before scanning.
    repaired_fields = 0
    disabled_records: set[int] = set()
    unsafe_disabled: set[int] = set()
    with closing(memory._connect()) as conn:  # noqa: SLF001 - use project connection settings.
        rows = conn.execute("SELECT * FROM reagent_memory ORDER BY id").fetchall()
        with conn:
            for row in rows:
                updates: dict[str, Any] = {}
                unrecoverable = False
                for field in MEMORY_TEXT_FIELDS:
                    value = str(row[field] or "")
                    if not looks_mojibake(value):
                        continue
                    if is_recoverable_mojibake(value):
                        updates[field] = repair_text(value)
                        repaired_fields += 1
                    else:
                        unrecoverable = True
                if any(looks_mojibake(updates.get(field, row[field])) for field in MEMORY_TEXT_FIELDS):
                    unrecoverable = True
                unsafe = bool(row["reusable"]) and memory.is_unsafe_reusable_evidence(row)
                if unrecoverable and int(row["reusable"] or 0):
                    updates["reusable"] = 0
                    updates["conflict"] = 1
                    disabled_records.add(int(row["id"]))
                if unsafe:
                    updates["reusable"] = 0
                    updates["conflict"] = 1
                    unsafe_disabled.add(int(row["id"]))
                if updates:
                    updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    assignments = ", ".join(f"{column} = ?" for column in updates)
                    conn.execute(
                        f"UPDATE reagent_memory SET {assignments} WHERE id = ?",
                        [*updates.values(), int(row["id"])],
                    )
    return {
        "repaired_fields": repaired_fields,
        "disabled_records": len(disabled_records),
        "unsafe_disabled": len(unsafe_disabled),
    }


def _repair_review_queue(paths: DataHealthPaths, settings: dict[str, Any]) -> dict[str, int]:
    if not paths.review_queue_path.exists():
        return {"repaired_fields": 0, "rebuilt_memory": 0}
    frame = canonicalize_review_queue_columns(pd.read_excel(paths.review_queue_path, dtype=str).fillna(""))
    if frame.empty:
        return {"repaired_fields": 0, "rebuilt_memory": 0}
    repaired_fields = 0
    for column in list(frame.columns):
        repaired_column = repair_text(column)
        if repaired_column != column and repaired_column not in frame.columns:
            frame = frame.rename(columns={column: repaired_column})
    for column in frame.columns:
        repaired = frame[column].map(repair_text)
        changed = repaired != frame[column]
        repaired_fields += int(changed.sum())
        frame.loc[changed, column] = repaired[changed]
    rebuilt = _rebuild_confirmed_review_memory(frame, settings, paths.root_dir)
    frame.to_excel(paths.review_queue_path, index=False)
    return {"repaired_fields": repaired_fields, "rebuilt_memory": rebuilt}


def _rebuild_confirmed_review_memory(frame: pd.DataFrame, settings: dict[str, Any], root_dir: Path) -> int:
    memory = ReagentMemory.from_settings(settings, root_dir)
    statuses = _series_first(frame, REVIEW_STATUS_COLUMNS).str.lower()
    rebuilt = 0
    for _, row in frame[statuses.eq("confirmed")].iterrows():
        category = _first_value(row, REVIEW_CATEGORY_COLUMNS)
        if not category or looks_mojibake(category):
            continue
        raw_name = _first_value(row, ("试剂名称", "chemical_name", "reagent_name"))
        cleaned_name = _first_value(row, ("cleaned_name", "清洗后名称"))
        standard_name = _first_value(row, ("standard_name", "标准化名称"))
        cas = _first_value(row, ("cas", "CAS号"))
        if any(looks_mojibake(value) for value in (raw_name, cleaned_name, standard_name, cas)):
            continue
        existing = memory.find_any(
            raw_name=raw_name,
            cleaned_name=cleaned_name,
            standard_name=standard_name,
            cas=cas,
            final_category=category,
        )
        if existing:
            continue
        if memory.add_record(
            raw_name=raw_name,
            cleaned_name=cleaned_name,
            standard_name=standard_name,
            cas=cas,
            final_category=category,
            confidence=1.0,
            reason="历史人工复核已确认，数据健康修复时补入试剂记忆库。",
            source="data_health_repair",
            specification=_first_value(row, ("specification", "规格")),
            unit=_first_value(row, ("unit", "规格单位")),
            need_manual_review=False,
            manual_verified=True,
        ):
            rebuilt += 1
    return rebuilt


def _series_first(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    result = pd.Series([""] * len(frame), index=frame.index, dtype=str)
    for column in columns:
        if column in frame.columns:
            candidate = frame[column].astype(str).str.strip()
            result = result.where(result.astype(str).str.strip() != "", candidate)
    return result


def _first_present(frame: pd.DataFrame, columns: tuple[str, ...]) -> str:
    return next((column for column in columns if column in frame.columns), "")


def _first_value(row: pd.Series, columns: tuple[str, ...]) -> str:
    for column in columns:
        if column in row.index:
            value = str(row.get(column) or "").strip()
            if value:
                return value
    return ""


def _issue_row(
    *,
    source: str,
    issue_type: str,
    identifier: Any,
    field: str,
    value: Any,
    action: str,
    row: dict[str, Any],
    repaired: Any = "",
    recoverable: bool | str = "",
) -> dict[str, Any]:
    return {
        "source": source,
        "issue_type": issue_type,
        "identifier": identifier,
        "field": field,
        "value": value,
        "repaired": repaired,
        "recoverable": recoverable,
        "action": action,
        "list_number": _dict_first(row, REVIEW_LIST_COLUMNS),
        "sequence": _dict_first(row, REVIEW_SEQUENCE_COLUMNS),
        "reagent_name": _dict_first(row, ("试剂名称", "chemical_name", "reagent_name", "raw_name")),
        "cas": _dict_first(row, ("cas", "CAS号")),
        "standard_name": _dict_first(row, ("standard_name", "标准化名称")),
        "final_category": _dict_first(row, ("final_category", "manual_result", "suggested_category")),
        "status": _dict_first(row, REVIEW_STATUS_COLUMNS),
    }


def _dict_first(row: dict[str, Any], columns: tuple[str, ...]) -> str:
    for column in columns:
        value = str(row.get(column) or "").strip()
        if value:
            return value
    return ""
