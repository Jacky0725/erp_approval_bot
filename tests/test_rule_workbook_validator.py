from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from rule_workbook_validator import validate_rule_workbook  # noqa: E402


def test_checked_in_rule_workbook_is_valid() -> None:
    path = ROOT_DIR / "config" / "rules_structured.xlsx"
    assert validate_rule_workbook(path).valid
    examples = pd.read_excel(path, sheet_name="examples", dtype=str).fillna("")
    assert "expected_final_category" in examples.columns
    assert examples["expected_final_category"].str.strip().ne("").all()


def test_rejects_unknown_category_reference(tmp_path: Path) -> None:
    copied = tmp_path / "rules.xlsx"
    shutil.copyfile(ROOT_DIR / "config" / "rules_structured.xlsx", copied)
    book = pd.read_excel(copied, sheet_name=None, dtype=str, engine="openpyxl")
    book["rules"].loc[0, "category"] = "不存在类别"
    with pd.ExcelWriter(copied, engine="openpyxl") as writer:
        for name, frame in book.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    result = validate_rule_workbook(copied)
    assert not result.valid
    assert any("未知类别" in error for error in result.errors)


def test_rejects_rule_semantics_not_supported_by_executor(tmp_path: Path) -> None:
    copied = tmp_path / "rules.xlsx"
    shutil.copyfile(ROOT_DIR / "config" / "rules_structured.xlsx", copied)
    book = pd.read_excel(copied, sheet_name=None, dtype=str, engine="openpyxl")
    book["rules"].loc[0, "match_type"] = "fuzzy_magic"
    with pd.ExcelWriter(copied, engine="openpyxl") as writer:
        for name, frame in book.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    result = validate_rule_workbook(copied)
    assert not result.valid
    assert any("match_type" in error for error in result.errors)
