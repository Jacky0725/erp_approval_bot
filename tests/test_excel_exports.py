from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from excel_exports import ExcelExportsMixin, atomic_excel_path, write_excel_atomic


def test_failed_serialization_preserves_previous_workbook(tmp_path):
    target = tmp_path / "review.xlsx"
    pd.DataFrame({"value": ["previous"]}).to_excel(target, index=False)
    original = target.read_bytes()

    def interrupted(self, path, **kwargs):
        Path(path).write_bytes(b"partial workbook")
        raise OSError("disk full")

    with patch.object(pd.DataFrame, "to_excel", interrupted), pytest.raises(OSError, match="disk full"):
        write_excel_atomic(pd.DataFrame({"value": ["new"]}), target)
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_success_replaces_complete_workbook(tmp_path):
    target = tmp_path / "review.xlsx"
    write_excel_atomic(pd.DataFrame({"value": ["previous"]}), target)
    write_excel_atomic(pd.DataFrame({"value": ["new"]}), target)
    assert pd.read_excel(target)["value"].tolist() == ["new"]
    assert list(tmp_path.iterdir()) == [target]


def test_locked_target_preserved_and_fallbacks_unique(tmp_path):
    import excel_exports
    target = tmp_path / "review.xlsx"
    write_excel_atomic(pd.DataFrame({"value": ["previous"]}), target)
    replace = excel_exports.os.replace

    def locked(source, destination):
        if Path(destination) == target:
            raise PermissionError("locked")
        return replace(source, destination)

    with patch.object(excel_exports.os, "replace", side_effect=locked):
        paths = [ExcelExportsMixin().write_excel_with_fallback(pd.DataFrame({"value": [value]}), target)
                 for value in ("first", "second")]
    assert paths[0] != paths[1]
    assert pd.read_excel(target)["value"].tolist() == ["previous"]
    assert [pd.read_excel(p)["value"].iloc[0] for p in paths] == ["first", "second"]
    assert len(list(tmp_path.iterdir())) == 3


def test_multi_sheet_failure_preserves_original(tmp_path):
    target = tmp_path / "rules.xlsx"
    write_excel_atomic(pd.DataFrame({"value": ["previous"]}), target)
    original = target.read_bytes()
    with pytest.raises(ValueError, match="second sheet"):
        with atomic_excel_path(target) as temporary:
            with pd.ExcelWriter(temporary, engine="openpyxl") as writer:
                pd.DataFrame({"rule": [1]}).to_excel(writer, sheet_name="rules", index=False)
                raise ValueError("second sheet")
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]
