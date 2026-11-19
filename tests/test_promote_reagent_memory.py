import sqlite3

from reagent_memory import ReagentMemory
from scripts.promote_reagent_memory import plan_promotion, run


def test_plan_excludes_unresolved_unsynced_and_identity_conflicts():
    def row(record_id, name, category, *, source="manual", reason="", review=0, reusable=0):
        return {
            "id": record_id, "raw_name_key": name, "cleaned_name_key": "",
            "standard_name_key": "", "cas_key": "", "final_category": category,
            "source": source, "reason": reason, "need_manual_review": review,
            "reusable": reusable, "conflict": 0,
        }

    rows = [
        row(1, "a", "普通类"),
        row(2, "b", "易燃类", source="workbuddy:batch:2", reason="ERP同步=待同步"),
        row(3, "c", "需人工复核", review=1),
        row(4, "d", "普通类", reusable=1),
        row(5, "d", "易燃类"),
    ]
    plan = plan_promotion(rows, {})
    assert plan["promote_ids"] == {1}
    assert plan["conflict_ids"] == {4, 5}
    assert plan["held"]["awaiting_erp_sync"] == 1
    assert plan["held"]["unresolved"] == 1


def test_apply_backs_up_and_promotes_only_nonconflicting_records(tmp_path):
    memory = ReagentMemory(tmp_path)
    memory.record_workbuddy_result(
        "batch", "one", {"试剂名称": "安全样品", "CAS号": ""},
        {"物化特性类别": "普通类", "判断依据": "已确认"}, synced=True,
    )
    memory.record_workbuddy_result(
        "batch", "two", {"试剂名称": "待同步样品", "CAS号": ""},
        {"物化特性类别": "普通类", "判断依据": "未完成"},
    )
    with sqlite3.connect(memory.path) as conn:
        conn.execute("UPDATE reagent_memory SET reusable=0 WHERE raw_name_key=?", ("安全样品",))
    preview = run(memory.path, {})
    assert preview["to_promote"] == 1
    assert preview["applied"] is False
    assert memory.find_any(raw_name="安全样品")["reusable"] == 0

    applied = run(memory.path, {}, apply=True)
    assert applied["backup"]
    assert memory.find_any(raw_name="安全样品")["reusable"] == 1
    assert memory.find_any(raw_name="待同步样品")["reusable"] == 0
    with sqlite3.connect(applied["backup"]) as backup:
        assert backup.execute("SELECT COUNT(*) FROM reagent_memory WHERE reusable=1").fetchone()[0] == 0
