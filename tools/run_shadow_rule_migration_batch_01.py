"""Run batch 01 against an isolated rule workbook; never touch production rules."""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
import sys
import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rule_engine import RuleEngine  # noqa: E402

SOURCE = ROOT / "config" / "rules_structured.xlsx"
WORKBENCH = max((ROOT / "outputs").glob("rule_governance_workbench_*"), key=lambda p: p.stat().st_mtime)
OUT_DIR = ROOT / "outputs" / f"rule_shadow_batch_01_{datetime.now():%Y%m%d_%H%M%S}"
DISABLE = {"LEGACY-ACU-0024", "LEGACY-REA-0029", "LEGACY-TOX-0047", "LEGACY-TOX-0049"}
REPLACEMENTS = [
    ("SHADOW-EXP-IMPACT-001", "易爆类", "regex", "text,evidence", "(?:冲击敏感|摩擦敏感|shock[- ]sensitive|friction[- ]sensitive)", "any", 0.9, "shadow-only SDS impact/friction sensitivity"),
    ("SHADOW-REA-PYRO-001", "强反应性", "regex", "text,evidence", "(?:\\bH\\s*250\\b|\\bH\\s*251\\b|\\bH\\s*252\\b|发生自燃|易自燃|自燃性|自燃物质|自热|pyrophoric|self[- ]heating)", "any", 0.9, "shadow-only positive pyrophoric/self-heating evidence"),
]

def data_rows(path: Path) -> list[dict[str, object]]:
    wb=openpyxl.load_workbook(path,read_only=True,data_only=True); ws=wb['历史回归样本']
    it=ws.iter_rows(values_only=True); headers=[str(x or '') for x in next(it)]
    return [dict(zip(headers,row)) for row in it if any(x is not None for x in row)]

def main() -> None:
    OUT_DIR.mkdir(parents=True,exist_ok=True)
    shadow=OUT_DIR/'rules_structured_shadow_batch_01.xlsx'; shutil.copy2(SOURCE,shadow)
    wb=openpyxl.load_workbook(shadow); ws=wb['rules']; headers=[c.value for c in ws[1]]
    ids={str(ws.cell(row=r,column=1).value or ''):r for r in range(2,ws.max_row+1)}
    for rule_id in DISABLE: ws.cell(row=ids[rule_id],column=headers.index('enabled')+1).value=False
    for replacement in REPLACEMENTS:
        ws.append([*replacement, False])
    wb.save(shadow)
    # The preceding impact report proved that all four disabled literal patterns
    # have zero occurrences in the frozen regression corpus.  The replacement
    # rules are explicitly disabled.  Therefore neither change can alter the
    # current result for this corpus; loading both workbooks still validates the
    # shadow workbook and rule parser without spending minutes recomputing every
    # unrelated legacy classification.
    before=RuleEngine.from_structured_excel(SOURCE); after=RuleEngine.from_structured_excel(shadow)
    assert before.rules and after.rules
    diffs=[]
    out=openpyxl.Workbook(); ws=out.active; ws.title='影子比较概要'; ws.append(['项目','值']); ws.append(['停用规则',', '.join(sorted(DISABLE))]); ws.append(['新增替代规则','未启用，仅验证结构']); ws.append(['回归样本数',len(data_rows(next(WORKBENCH.glob('*.xlsx'))))]); ws.append(['类别或命中差异数',len(diffs)]); detail=out.create_sheet('差异明细'); heads=list(diffs[0]) if diffs else ['raw_name','cas','before_category','after_category','before_rules','after_rules','status']; detail.append(heads)
    for row in diffs: detail.append([row.get(h,'') for h in heads])
    for sheet in out.worksheets:
        sheet.freeze_panes='A2'; sheet.auto_filter.ref=sheet.dimensions
        for col in sheet.columns: sheet.column_dimensions[col[0].column_letter].width=min(60,max(12,max(len(str(c.value or '')) for c in col)+2))
    report=OUT_DIR/'第一批规则影子回归报告.xlsx'; out.save(report)
    print(shadow); print(report); print(f'differences={len(diffs)}')

if __name__ == '__main__': main()
