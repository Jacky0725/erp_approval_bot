import sqlite3

from reagent_memory import ReagentMemory
from scripts.resolve_review_queue_by_rules import has_memory_conflict, reusable_memory_decision, rule_decision


class Engine:
    def __init__(self, result):
        self.result = result

    def classify(self, _):
        return self.result


def test_only_explicit_high_confidence_nonordinary_rule_is_resolved():
    settings = {"reagent": {"physicochemical_property_options": ["普通类", "重金属类"]}}
    row = {"试剂名称": "氯化镍", "cas": ""}
    result = {"final_category": "重金属类", "need_manual_review": False,
              "matched_rule_ids": ["METAL-1"], "confidence": 0.9, "reason": "含镍"}
    assert rule_decision(row, Engine(result), settings)["category"] == "重金属类"
    assert rule_decision(row, Engine({**result, "matched_rule_ids": []}), settings) is None
    assert rule_decision(row, Engine({**result, "confidence": 0.7}), settings) is None
    assert rule_decision(row, Engine({**result, "final_category": "普通类"}), settings) is None
    assert rule_decision(row, Engine({**result, "need_manual_review": True}), settings) is None


def test_existing_memory_conflict_blocks_rule_resolution():
    decision = {"name": "氯化镍", "cleaned": "", "standard": "", "category": "重金属类"}
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE reagent_memory(raw_name_key TEXT, cleaned_name_key TEXT, standard_name_key TEXT, final_category TEXT, conflict INTEGER)")
        conn.execute("INSERT INTO reagent_memory VALUES (?, '', '', ?, 0)", ("氯化镍", "普通类"))
        assert has_memory_conflict(conn, decision, {})


def test_reusable_memory_closes_only_matching_classification_review(tmp_path):
    memory = ReagentMemory(tmp_path)
    memory.add_record(raw_name="溴化钾", final_category="溴碘类", manual_verified=True)
    row = {"试剂名称": "溴化钾", "review_kind": "classification_review"}
    no_rule = Engine({"final_category": "", "need_manual_review": True})
    with sqlite3.connect(memory.path) as conn:
        decision = reusable_memory_decision(conn, row, no_rule, {})
        assert decision["category"] == "溴碘类"
        assert reusable_memory_decision(conn, row, Engine({"final_category": "普通类"}), {}) is None
        assert reusable_memory_decision(conn, {**row, "review_kind": "write_failure"}, no_rule, {}) is None
        conn.execute("UPDATE reagent_memory SET conflict=1 WHERE id=?", (decision["record_id"],))
        assert reusable_memory_decision(conn, row, no_rule, {}) is None
