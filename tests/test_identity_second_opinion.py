from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_extractor import LlmExtractor


def test_identity_second_opinion_is_advisory_and_labels_model_knowledge() -> None:
    payload = {
        "name_identity_opinion": "名称可能对应乙醇。",
        "cas_identity_opinion": "CAS 对应另一物质。",
        "identity_opinion": "冲突",
        "candidate_category": "易燃类",
        "physicochemical_summary_cn": "可能为易燃液体。",
        "reason_cn": "依据名称和通用化学知识。",
        "matched_rule_summary_cn": "涉及易燃规则。",
        "uncertainties_cn": ["需要人工确认身份"],
        "evidence_basis": "模型知识",
        "advisory_confidence": 0.9,
    }
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)))
    extractor = LlmExtractor(settings={})
    with patch.object(extractor, "_has_api_key", return_value=True), patch.object(extractor, "_client", return_value=client):
        result = extractor.generate_identity_second_opinion({"allowed_categories": ["易燃类"], "rules_fingerprint": "r1"})
    assert result["used_llm"] is True
    assert result["advisory_only"] is True
    assert result["must_manual_review"] is True
    assert result["evidence_basis"] == "模型知识"
    assert result["advisory_confidence"] <= 0.65
