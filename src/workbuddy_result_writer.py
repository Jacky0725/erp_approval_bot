"""Publish a WB-EXCEL-v1 result from the judgments in one batch directory."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from openpyxl import load_workbook


RESULT_COLUMNS = ["批次ID", "记录ID", "试剂名称", "物化特性类别", "判断依据"]
JUDGMENT_COLUMNS = RESULT_COLUMNS[1:2] + RESULT_COLUMNS[3:]
WRITER_NAME = "write_result.py"
JUDGMENTS_NAME = "judgments.json"
FIXED_WRITER_INSTRUCTIONS = (
    "本批使用项目提供的固定写表脚本。判定后在本批目录写 UTF-8 judgments.json，内容是 JSON 数组，"
    "每条只含三个字符串字段：记录ID、物化特性类别、判断依据。依据必须完整。"
    "然后在本批目录运行 python write_result.py；不要自行编写 Excel 写表脚本或直接写 result.xlsx。"
    "脚本成功发布后只回复【批次完成】及本批 ID，失败时说明原因。"
)


def valid_basis(value: str) -> bool:
    visible = [char for char in value if char.isprintable() and not char.isspace()]
    return len(visible) >= 2 and any(char.isalnum() for char in visible)


def read_judgments(path: Path, expected_ids: set[str]) -> dict[str, tuple[str, str]]:
    rows = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(rows, list):
        raise ValueError("judgments.json 必须是 JSON 数组。")
    results = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(JUDGMENT_COLUMNS) or not all(
            isinstance(row[field], str) for field in JUDGMENT_COLUMNS
        ):
            raise ValueError("每条判定必须包含记录ID、物化特性类别和判断依据三个字符串字段。")
        key, category, basis = (row[field] for field in JUDGMENT_COLUMNS)
        if key not in expected_ids or key in results:
            raise ValueError(f"重复或未知记录ID {key}。")
        if not category.strip():
            raise ValueError(f"记录 {key} 缺少物化特性类别。")
        if not valid_basis(basis):
            raise ValueError(f"记录 {key} 的判断依据为空、仅含符号或不足两个有效字符。")
        results[key] = (category, basis)
    missing = expected_ids - results.keys()
    if missing:
        raise ValueError(f"缺少记录ID {', '.join(sorted(missing))}。")
    return results


def _input_identities(path: Path, batch_id: str) -> dict[str, tuple[str, str, str]]:
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        if len(workbook.worksheets) != 1:
            raise ValueError("输入 Excel 必须只有一个工作表。")
        rows = iter(workbook.active.values)
        headers = next(rows, ())
        columns = [headers.index(field) for field in RESULT_COLUMNS[:3]]
        identities = {}
        for row in rows:
            batch, key, name = (row[column] for column in columns)
            if batch != batch_id or not key or not name or key in identities:
                raise ValueError("输入 Excel 存在错误批次、重复记录ID或缺失名称。")
            identities[key] = (batch, key, name)
        return identities
    finally:
        workbook.close()


def publish_result(directory: Path) -> None:
    result_path = directory / "result.xlsx"
    if result_path.exists():
        raise FileExistsError("result.xlsx 已存在，保留原文件。")
    identities = _input_identities(directory / "input.xlsx", directory.name)
    judgments = read_judgments(directory / JUDGMENTS_NAME, set(identities))
    expected = [(batch, key, name, *judgments[key]) for batch, key, name in identities.values()]
    workbook = load_workbook(directory / "result_template.xlsx", data_only=False)
    temporary = None
    try:
        sheet = workbook.active
        template_rows = list(sheet.values)
        if (
            len(workbook.worksheets) != 1 or sheet.merged_cells.ranges
            or template_rows != [tuple(RESULT_COLUMNS), *(row[:3] + (None, None) for row in expected)]
        ):
            raise ValueError("结果模板与本批输入身份或五列协议不一致。")
        for row_number, row in enumerate(expected, start=2):
            for column, value in enumerate(row[3:], start=4):
                cell = sheet.cell(row=row_number, column=column, value=value)
                cell.data_type = "s"
        with tempfile.NamedTemporaryFile(dir=directory, prefix="result.pending.", suffix=".xlsx", delete=False) as stream:
            temporary = Path(stream.name)
        workbook.save(temporary)
        workbook.close()
        with_file = load_workbook(temporary, read_only=True, data_only=False)
        try:
            rows = list(with_file.active.iter_rows())
            if any(cell.data_type == "f" for row in rows for cell in row) or [
                tuple(cell.value for cell in row) for row in rows
            ] != [tuple(RESULT_COLUMNS), *expected]:
                raise ValueError("结果 Excel 回读与完整判定不一致。")
        finally:
            with_file.close()
        # Windows rename fails if the destination exists. On POSIX, link
        # publishes atomically with the same no-overwrite guarantee.
        if os.name == "nt":
            temporary.rename(result_path)
        else:
            os.link(temporary, result_path)
    finally:
        workbook.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        publish_result(Path(__file__).resolve().parent)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    print("result.xlsx 已发布。")
