"""Check the critical missing-concentration case in an isolated WorkBuddy benchmark."""

import argparse
import json
import re
from pathlib import Path


CASE_NAME = "高氯酸滴定液,乙酸基质"
EXPECTED_CATEGORY = "易爆类"
REQUIRED_RULE = "SPECIAL-EXP-004"
MISSING_CONCENTRATION = re.compile(
    r"(?:输入|记录|本批|批内|原始数据)[^。；]{0,16}"
    r"(?:未(?:注明|标明|标注|提供|给出)|没有|无|缺少)[^。；]{0,8}浓度"
    r"|(?:输入|记录|本批|批内|原始数据)[^。；]{0,16}浓度[^。；]{0,6}(?:缺失|未知|不明|未给出)"
    r"|(?:输入|记录|本批|批内|原始数据)[^。；]{0,40}"
    r"(?:无法|不能|未能|尚未)核实(?:其|实际|本品|该品)?浓度"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    args = parser.parse_args()
    report = json.loads(args.benchmark.read_text(encoding="utf-8"))
    rounds = report["rounds"]
    if report.get("error") or report.get("erp_write") is not False or len(rounds) < 2:
        print("未通过：需要至少两轮完整的隔离对照。")
        return 1

    failed = False
    compared_sizes = None
    input_order = None
    for trial in rounds:
        arms = trial["arms"]
        sizes = {arm["size"] for arm in arms}
        if len(arms) != 2 or len(sizes) != 2 or 20 not in sizes or (
            compared_sizes is not None and sizes != compared_sizes
        ):
            print(f"第 {trial['round']} 轮未通过。两轮须使用每批 20 条基线和同一候选批量。")
            return 1
        compared_sizes = sizes
        for arm in arms:
            prefix = f"第 {trial['round']} 轮，每批 {arm['size']} 条"
            batches = arm["batches"]
            if not batches or max(batch["size"] for batch in batches) != arm["size"] or any(
                len(batch.get("rows", [])) != batch["size"] for batch in batches
            ):
                print(f"{prefix}未通过。批次未按配置完成。")
                return 1
            rows = [row for batch in batches for row in batch["rows"]]
            order = [(row["record"], row["name"]) for row in rows]
            if input_order is not None and order != input_order:
                print(f"{prefix}未通过。对照输入的记录或顺序不同。")
                return 1
            input_order = order
            cases = [row for row in rows if row["name"] == CASE_NAME]
            if arm["seconds"] is None or len(cases) != 1:
                print(f"{prefix}：未通过，测试未完成或用例缺失、重复。")
                failed = True
                continue
            case = cases[0]
            basis = case["basis"]
            if (case["erp_category"] != EXPECTED_CATEGORY
                    or REQUIRED_RULE not in basis
                    or not MISSING_CONCENTRATION.search(basis)):
                print(f"{prefix}：未通过，类别为「{case['erp_category']}」，须按 {REQUIRED_RULE} 说明输入缺少浓度。")
                failed = True
                continue
            print(f"{prefix}：通过，依据确认输入缺少浓度。")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
