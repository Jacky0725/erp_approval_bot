import json
from pathlib import Path
import sys

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workbuddy_bridge import WorkBuddyBridge
from workbuddy_rules import PART_LIMIT, build_sections


RULES = Path(__file__).resolve().parents[1] / "config/rules_structured.xlsx"


def test_real_workbook_sections_preserve_cell_values_and_are_bounded():
    source = RULES.read_bytes()
    parts = build_sections(source)
    assert parts == build_sections(source)
    parsed = {}
    for part in parts:
        assert len(part.encode("utf-8")) <= PART_LIMIT
        heading, _, *lines = part.splitlines()
        sheet = parsed.setdefault(heading.removeprefix("工作表 "), {})
        for line in lines:
            index, cells = line.split(" ", 1)
            sheet[int(index)] = json.loads(cells)
    with RULES.open("rb") as stream:
        book = load_workbook(stream, read_only=True, data_only=True)
        try:
            for sheet in book:
                expected = {}
                for number, values in enumerate(sheet.values, 1):
                    values = list(values)
                    while values and values[-1] is None:
                        values.pop()
                    if values:
                        expected[number] = json.loads(json.dumps(values, default=str))
                assert parsed[sheet.title] == expected
        finally:
            book.close()


@pytest.mark.parametrize("failure", ["missing", "corrupt_cache", "corrupt_part", "disabled"])
def test_dispatch_falls_back_to_original_workbook(tmp_path, failure):
    bridge = WorkBuddyBridge(tmp_path, {"paths": {"structured_rules_excel": str(RULES)}, "workbuddy": {
        "compact_rules": True, "prompt_mode": "project",
    }})
    if failure != "missing":
        cache = bridge.publish_compact_rules()
        if failure == "corrupt_cache":
            cache.write_text("[]", encoding="utf-8")
    state = bridge.prepare([{"试剂清单号": "SJ1", "_erp_record_id": "1", "试剂名称": "虚拟试剂"}], compact=True)
    if failure == "corrupt_part":
        assert bridge.dispatch_prompts(state)[2] == "compact"
        part = next((bridge.batch_path(state) / "rules_compact").glob("part-*.txt"))
        part.write_text("modified", encoding="utf-8")
        with pytest.raises(ValueError, match="分段摘要"):
            bridge.load_results(state)
    if failure == "disabled":
        bridge.config["compact_rules"] = False
    prompt, _, mode = bridge.dispatch_prompts(state)
    assert "分段完整读取规则" in prompt and mode == "xlsx"
    assert "compact_rules_sha256" not in state
