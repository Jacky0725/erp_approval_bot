from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


CANDIDATE_COLUMNS = [
    "timestamp",
    "reagent_name",
    "standard_name",
    "cas",
    "current_suggestion",
    "manual_result",
    "candidate_category",
    "reason",
    "evidence",
    "source_url",
    "status",
    "reviewer",
    "reviewed_at",
]


@dataclass
class RuleMaintainer:
    root_dir: Path
    settings: dict[str, Any]

    @classmethod
    def from_settings(cls, settings: dict[str, Any], root_dir: Path) -> "RuleMaintainer":
        return cls(root_dir=root_dir, settings=settings)

    @property
    def candidates_path(self) -> Path:
        paths = self.settings.get("paths", {})
        return self.root_dir / paths.get("rule_candidates_excel", "config/rule_candidates.xlsx")

    @property
    def structured_rules_path(self) -> Path:
        paths = self.settings.get("paths", {})
        return self.root_dir / paths.get("structured_rules_excel", "config/rules_structured.xlsx")

    def ensure_candidate_file(self) -> Path:
        self.candidates_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.candidates_path.exists():
            pd.DataFrame(columns=CANDIDATE_COLUMNS).to_excel(self.candidates_path, index=False)
        return self.candidates_path

    def record_candidate(
        self,
        reagent: dict[str, Any],
        name_result: dict[str, Any],
        search_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
    ) -> bool:
        if not classification.get("need_manual_review") and not search_result.get("need_manual_review"):
            return False

        self.ensure_candidate_file()
        candidates = pd.read_excel(self.candidates_path, dtype=str).fillna("")
        row = self._candidate_row(reagent, name_result, search_result, extracted, classification)
        duplicate = (
            (candidates["reagent_name"].astype(str).str.strip() == row["reagent_name"])
            & (candidates["standard_name"].astype(str).str.strip() == row["standard_name"])
            & (candidates["cas"].astype(str).str.strip() == row["cas"])
            & (candidates["status"].astype(str).str.strip().str.lower().isin({"pending", ""}))
        )
        if not candidates.empty and bool(duplicate.any()):
            return False

        candidates = pd.concat([candidates, pd.DataFrame([row])], ignore_index=True)
        candidates = candidates.reindex(columns=CANDIDATE_COLUMNS)
        candidates.to_excel(self.candidates_path, index=False)
        return True

    def promote_approved_candidates(self) -> int:
        self.ensure_candidate_file()
        candidates = pd.read_excel(self.candidates_path, dtype=str).fillna("")
        approved = candidates[candidates["status"].str.lower() == "approved"].copy()
        if approved.empty:
            return 0

        rules_book = pd.read_excel(self.structured_rules_path, sheet_name=None, dtype=str, engine="openpyxl")
        rules = rules_book.get("rules", pd.DataFrame()).fillna("")
        examples = rules_book.get("examples", pd.DataFrame()).fillna("")

        promoted = 0
        for index, row in approved.iterrows():
            category = str(row.get("manual_result") or row.get("candidate_category") or "").strip()
            standard_name = str(row.get("standard_name") or row.get("reagent_name") or "").strip()
            if not category or not standard_name:
                continue
            if not self._example_exists(examples, category, standard_name):
                examples = pd.concat(
                    [
                        examples,
                        pd.DataFrame(
                            [
                                {
                                    "category": category,
                                    "example_name": standard_name,
                                    "match_mode": "exact",
                                    "enabled": True,
                                    "source": "manual_review",
                                    "notes": str(row.get("reason") or "").strip(),
                                }
                            ]
                        ),
                    ],
                    ignore_index=True,
                )
                promoted += 1
            candidates.at[index, "status"] = "promoted"

        if promoted:
            rules_book["rules"] = rules
            rules_book["examples"] = examples
            with pd.ExcelWriter(self.structured_rules_path, engine="openpyxl") as writer:
                for sheet_name, dataframe in rules_book.items():
                    dataframe.to_excel(writer, sheet_name=sheet_name, index=False)
            candidates.to_excel(self.candidates_path, index=False)
        return promoted

    @staticmethod
    def _example_exists(examples: pd.DataFrame, category: str, example_name: str) -> bool:
        if examples.empty or "category" not in examples.columns or "example_name" not in examples.columns:
            return False
        return bool(
            (
                examples["category"].astype(str).str.strip().eq(category)
                & examples["example_name"].astype(str).str.strip().eq(example_name)
            ).any()
        )

    @staticmethod
    def _candidate_row(
        reagent: dict[str, Any],
        name_result: dict[str, Any],
        search_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
    ) -> dict[str, Any]:
        evidence = extracted.get("evidence", []) or []
        if isinstance(evidence, list):
            evidence_text = " | ".join(str(item) for item in evidence)
        else:
            evidence_text = str(evidence)
        return {
            "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
            "reagent_name": str(reagent.get("\u8bd5\u5242\u540d\u79f0") or reagent.get("reagent_name") or ""),
            "standard_name": str(name_result.get("standard_name") or ""),
            "cas": str(search_result.get("cas") or reagent.get("CAS\u53f7") or ""),
            "current_suggestion": str(classification.get("final_category") or ""),
            "manual_result": "",
            "candidate_category": ", ".join(classification.get("matched_categories", []) or []),
            "reason": str(classification.get("reason") or search_result.get("failure_reason") or ""),
            "evidence": evidence_text,
            "source_url": str(search_result.get("url") or search_result.get("fallback_url") or ""),
            "status": "pending",
            "reviewer": "",
            "reviewed_at": "",
        }
