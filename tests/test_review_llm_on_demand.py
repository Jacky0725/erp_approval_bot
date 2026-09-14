from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from web_runner import generate_review_llm_advice  # noqa: E402


class FakeExtractor:
    calls = 0

    def __init__(self, settings=None):
        self.settings = settings

    def generate_identity_second_opinion(self, reagent_info):
        type(self).calls += 1
        return {
            "used_llm": True,
            "candidate_category": "普通类",
            "physicochemical_summary_cn": "名称可能对应乙醇。",
            "reason_cn": "模型知识第二意见，仍需人工确认。",
            "matched_rule_summary_cn": "未命中高风险规则。",
            "uncertainties_cn": ["CAS 缺失"],
            "advisory_confidence": 0.6,
            "evidence_basis": "模型知识",
            "advisory_only": True,
            "identity_opinion": "CAS缺失",
            "name_identity_opinion": "名称可能为乙醇",
            "cas_identity_opinion": "无 CAS 可核验",
            "model": "fake-model",
            "provider": "fake-provider",
            "generated_at": "2026-09-12T12:00:00",
            "rules_fingerprint": reagent_info["rules_fingerprint"],
        }

    def generate_manual_review_advice(self, reagent_info):
        raise AssertionError("CAS 缺失必须使用身份第二意见")


def test_on_demand_llm_advice_is_cached_and_remains_advisory(tmp_path: Path) -> None:
    FakeExtractor.calls = 0
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    shutil.copyfile(ROOT_DIR / "config" / "rules_structured.xlsx", tmp_path / "config" / "rules_structured.xlsx")
    (tmp_path / "config" / "settings.yaml").write_text(
        yaml.safe_dump({"paths": {"structured_rules_excel": "config/rules_structured.xlsx"}}, allow_unicode=True),
        encoding="utf-8",
    )
    pd.DataFrame([{
        "试剂清单号": "SJ-1",
        "试剂名称": "乙醇",
        "CAS号": "",
        "standard_name": "乙醇",
        "identity_status": "cas_missing",
        "status": "pending",
        "reason": "CAS 缺失",
    }]).to_excel(tmp_path / "data" / "review_queue.xlsx", index=False)
    payload = {"review_key": "SJ-1||乙醇|乙醇"}
    with patch("web_runner.LlmExtractor", FakeExtractor):
        first = generate_review_llm_advice(payload, root_dir=tmp_path)
        second = generate_review_llm_advice(payload, root_dir=tmp_path)
    assert first["generated"] is True
    assert second["cached"] is True
    assert FakeExtractor.calls == 1
    saved = pd.read_excel(tmp_path / "data" / "review_queue.xlsx", dtype=str).fillna("")
    assert saved.loc[0, "llm_advisory_only"].lower() == "true"
    assert saved.loc[0, "llm_advisory_evidence_basis"] == "模型知识"
    assert saved.loc[0, "llm_identity_opinion"] == "CAS缺失"
