from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from approval_flow import ApprovalFlowMixin
from erp_api_client import ErpApiClient
from v3_web_search import V3WebSearchClient, V3WebSearchError, resolve_v3_base_url, rule_bundle, test_v3_model_connection as run_v3_model_test, test_v3_web_search_connection as run_v3_connection_test
from web_runner import v3_invalid_results_summary


class FakeResponse:
    def __init__(self, payload: dict): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self): return json.dumps(self.payload).encode("utf-8")


def settings() -> dict:
    return {"approval": {"v3": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3.7-plus", "retrieval_mode": "always_web", "timeout_seconds": 45, "max_retries": 0}}}


def item(name: str = "乙醇", cas: str = "64-17-5", normalized: dict | None = None) -> dict:
    return {"reagent": {"序号": "1", "试剂名称": name, "CAS号": cas}, "name_result": normalized or {"standard_name": name, "cleaned_name": name}}


def fact(status: str = "unknown", source: str = "https://source.example", text: str = "乙醇的检索来源摘要") -> dict:
    return {"status": status, "value": "", "source_url": source if status in {"present", "absent", "measured", "classified"} else "", "source_text": text if status in {"present", "absent", "measured", "classified"} else "", "reason": "资料不足" if status in {"unknown", "unreliable", "not_applicable"} else ""}


def properties(name: str = "乙醇") -> dict:
    source = "https://source.example"
    return {"flash_point": fact("unknown", source, name), "boiling_point": fact("unknown", source, name), "toxicity": fact("unknown", source, name), "corrosive": fact("unknown", source, name), "oxidizing": fact("unknown", source, name), "flammable": fact("present", source, name), "water_reactive": fact("unknown", source, name), "explosive_risk": fact("unknown", source, name), "heavy_metal": fact("unknown", source, name), "hazard_text": "易燃"}


def response(answer: dict, source: str = "https://source.example") -> dict:
    return {"output_text": json.dumps({"items": [answer]}, ensure_ascii=False), "output": [{"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": source}]}}]}


def answer(name: str = "乙醇", basis: str = "name", category: str = "易燃类", review: bool = False, cas: str = "64-17-5") -> dict:
    source = "https://source.example"
    return {"sequence": "1", "raw_name": name, "identity_basis": basis, "identified_cas": cas, "cas_status": "matched", "recommended_category": category, "review_required": review, "source_urls": [source], "category_evidence": {"status": "present", "value": "", "source_url": source, "source_text": name if basis == "name" else cas, "reason": ""}, "properties": properties(name if basis == "name" else cas), "reason": "有来源的易燃证据", "uncertainties": []}


def test_v3_address_modes_and_connection():
    assert resolve_v3_base_url("shared_beijing").endswith("/compatible-mode/v1")
    assert resolve_v3_base_url("workspace_beijing", "workspace-1").startswith("https://workspace-1")
    payload = {"output_text": "OK", "output": [{"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": "https://source.example"}]}}]}
    result = run_v3_connection_test(settings(), base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen3.7-plus", api_key="secret", requester=lambda *_a, **_k: FakeResponse(payload))
    assert result["ok"] and "secret" not in repr(result)


def test_rule_bundle_contains_active_matchers_thresholds_and_stable_fingerprint():
    engine = SimpleNamespace(
        rule_version="sha256:test",
        priority=["拒收类", "普通类"],
        manual_review_categories={"拒收类"},
        rules=[SimpleNamespace(
            rule_id="R-1", category="拒收类", match_type="contains", field_scope=("name",),
            condition="any", explanation_keywords=("汞",), example_keywords=("氯化汞",),
            example_match_modes=("contains",), explanation="含汞拒收",
        )],
        thresholds=[SimpleNamespace(threshold_id="T-1", category="易燃类", field="flash_point", operator="<", value="60", unit="°C", description="低闪点")],
    )
    first, second = rule_bundle(engine), rule_bundle(engine)
    assert first["fingerprint"] == second["fingerprint"]
    assert first["model_rule_pack"]["rule_columns"] == ["id", "category", "match_type", "field_scope", "condition", "patterns", "examples", "example_match_modes", "description"]
    assert first["model_rule_pack"]["rules"][0][5] == ["汞"]
    assert first["model_rule_pack"]["thresholds"][0][4] == "60"


def test_v3_model_connection_uses_strict_json_without_web_search():
    captured = {}
    model_properties = {
        name: {"status": "unknown", "value": "", "source_url": "", "source_text": "", "reason": "模型无可靠结论"}
        for name in ("flash_point", "boiling_point", "toxicity", "corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal")
    }
    payload = {
        "output_text": json.dumps({"items": [{
            "sequence": "1", "raw_name": "water", "identity_basis": "name",
            "identified_cas": "7732-18-5", "cas_status": "matched",
            "recommended_category": "普通类", "review_required": False,
            "source_urls": [], "category_evidence": {"status": "unknown", "value": "", "source_url": "", "source_text": "", "reason": "未联网"},
            "properties": {**model_properties, "hazard_text": ""}, "reason": "水", "uncertainties": [],
        }]}, ensure_ascii=False),
        "output": [],
    }
    def requester(request, **_kwargs):
        captured.update(json.loads(request.data.decode("utf-8")))
        return FakeResponse(payload)
    result = run_v3_model_test(settings(), base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen3.7-plus", api_key="secret", requester=requester)
    assert result["ok"] and result["web_search_calls"] == 0
    assert "tools" not in captured


def test_usable_name_sends_erp_cas_and_schema(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    captured = {}
    def requester(request, **_kwargs):
        captured.update(json.loads(request.data.decode("utf-8")))
        return FakeResponse(response(answer(cas="67-56-1")))
    client = V3WebSearchClient(settings(), requester=requester)
    result = client.request_batch([item(cas="67-56-1")], {"priority": ["普通类", "易燃类"], "rule_version": "r1"})
    assert result["items"][0]["ok"]
    assert result["items"][0]["identity_basis"] == "name"
    assert '"erp_cas": "67-56-1"' in captured["input"]
    assert '"project_rule_pack"' in captured["input"]
    assert captured["response_format"]["type"] == "json_schema"


def test_unusable_name_uses_valid_cas_only(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    client = V3WebSearchClient(settings(), requester=lambda *_a, **_k: FakeResponse(response(answer("没写", "cas_fallback"))))
    result = client.request_batch([item("没写", "64-17-5", {"need_manual_review": True})], {"priority": ["普通类", "易燃类"], "rule_version": "r1"})
    assert result["items"][0]["ok"]
    assert result["items"][0]["identity_status"] == "cas_verified"


def test_unusable_name_without_valid_cas_skips_network(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    called = False
    def requester(*_args, **_kwargs):
        nonlocal called; called = True; raise AssertionError("network must not be called")
    client = V3WebSearchClient(settings(), requester=requester)
    result = client.request_batch([item("没写", "-", {"need_manual_review": True})], {"priority": ["普通类"], "rule_version": "r1"})
    assert result["ok"] and not result["items"][0]["ok"] and not called


def test_missing_source_summary_or_schema_items_fails_fast(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    broken = answer(); broken["category_evidence"]["source_text"] = ""
    client = V3WebSearchClient(settings(), requester=lambda *_a, **_k: FakeResponse(response(broken)))
    assert not client.request_batch([item()], {"priority": ["普通类", "易燃类"], "rule_version": "r1"})["items"][0]["ok"]
    client = V3WebSearchClient(settings(), requester=lambda *_a, **_k: FakeResponse({"output_text": "{}", "output": [{"type": "web_search_call", "status": "completed", "action": {"sources": []}}]}))
    assert not client.request_batch([item()], {"priority": ["普通类", "易燃类"], "rule_version": "r1"})["ok"]


def test_retryable_v3_failure_retries_once(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    configured = settings()
    configured["approval"]["v3"]["max_retries"] = 1
    client = V3WebSearchClient(configured)
    responses = iter([V3WebSearchError("temporary timeout", retryable=True), response(answer())])
    def post(*_args, **_kwargs):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(client, "_post", post)
    monkeypatch.setattr("v3_web_search.time.sleep", lambda _seconds: None)
    result = client.request_batch([item()], {"priority": ["普通类", "易燃类"], "rule_version": "r1"})
    assert result["ok"] and result["attempts"] == 2


def test_undefined_model_fields_are_invalid(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    broken = answer()
    broken["unrecognized"] = "must not be accepted"
    client = V3WebSearchClient(settings(), requester=lambda *_a, **_k: FakeResponse(response(broken)))
    result = client.request_batch([item()], {"priority": ["普通类", "易燃类"], "rule_version": "r1"})
    assert result["ok"]
    assert not result["items"][0]["ok"]
    assert "未定义字段" in result["items"][0]["reason"]


def test_v3_candidate_is_not_rule_input():
    extracted = ApprovalFlowMixin.v3_model_properties_to_extracted({"hazard_text": ""})
    assert extracted["suggested_categories"] == []


def test_model_first_sends_no_web_tool_and_rejects_claimed_url(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    configured = settings(); configured["approval"]["v3"]["retrieval_mode"] = "model_first"
    captured = {}
    no_web = answer(); no_web["source_urls"] = []
    no_web["category_evidence"] = fact("present", "", "")
    for value in no_web["properties"].values():
        if isinstance(value, dict):
            value["source_url"] = ""; value["source_text"] = ""
    def requester(request, **_kwargs):
        captured.update(json.loads(request.data.decode("utf-8")))
        return FakeResponse({"output_text": json.dumps({"items": [no_web]}, ensure_ascii=False), "output": []})
    result = V3WebSearchClient(configured, requester=requester).request_batch([item()], {"priority": ["普通类", "易燃类"], "rule_version": "r1"})
    assert result["items"][0]["ok"]
    assert result["web_search_calls"] == 0
    assert "tools" not in captured

    no_web["source_urls"] = ["https://invented.example"]
    bad = V3WebSearchClient(configured, requester=lambda *_a, **_k: FakeResponse({"output_text": json.dumps({"items": [no_web]}, ensure_ascii=False), "output": []})).request_batch([item()], {"priority": ["普通类", "易燃类"], "rule_version": "r1"})
    assert not bad["items"][0]["ok"]


def test_invalid_v3_result_is_recorded_outside_review_queue():
    class Bot(ApprovalFlowMixin):
        pass
    with TemporaryDirectory() as tmp:
        bot = Bot()
        bot.root_dir = Path(tmp)
        bot.record_v3_invalid_result(
            list_number="SJ1",
            batch_position=0,
            reagent={"序号": "1", "试剂名称": "乙醇", "CAS号": "64-17-5"},
            name_result={"standard_name": "乙醇"},
            result={"attempts": 1, "error": "模型条目包含未定义字段"},
            model={"ok": False, "reason": "模型条目包含未定义字段"},
        )
        payload = v3_invalid_results_summary(Path(tmp))
    assert payload["count"] == 1
    assert payload["rows"][0]["reagent_name"] == "乙醇"
    assert payload["rows"][0]["retryable"]


def test_provider_legacy_single_item_is_only_accepted_when_source_is_current(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    source = "https://source.example"
    legacy = {
        "sequence": "1", "search_name": "乙醇", "identity_basis": "name",
        "recommended_category": "易燃类", "review_required": False,
        "source_url": source, "source_summary": "乙醇是易燃液体。",
    }
    client = V3WebSearchClient(settings(), requester=lambda *_a, **_k: FakeResponse({
        "output_text": json.dumps(legacy, ensure_ascii=False),
        "output": [{"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": source}]}}],
    }))
    result = client.request_batch([item()], {"priority": ["普通类", "易燃类"], "rule_version": "r1"})
    assert result["items"][0]["ok"]
    assert result["items"][0]["properties"]["flammable"]["status"] == "present"

    wrong_source = {**legacy, "source_url": "https://unverified.example"}
    client = V3WebSearchClient(settings(), requester=lambda *_a, **_k: FakeResponse({
        "output_text": json.dumps(wrong_source, ensure_ascii=False),
        "output": [{"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": source}]}}],
    }))
    result = client.request_batch([item()], {"priority": ["普通类", "易燃类"], "rule_version": "r1"})
    assert not result["ok"]


def test_v3_direct_write_uses_name_or_cas_identity_but_not_cas_conflict():
    base = {"ok": True, "candidate_category": "易燃类", "review_required": False, "identified_cas": "64-17-5", "cas_status": "matched", "properties": {"flammable": {"status": "present"}}}
    assert ApprovalFlowMixin.v3_auto_decision_allowed({**base, "identity_basis": "name"}, {"试剂名称": "乙醇", "CAS号": "64-17-5"}, {"standard_name": "乙醇"}, {"final_category": "", "matched_rule_ids": []})
    assert ApprovalFlowMixin.v3_auto_decision_allowed({**base, "identity_basis": "cas_fallback"}, {"试剂名称": "没写", "CAS号": "64-17-5"}, {"need_manual_review": True}, {"final_category": "", "matched_rule_ids": []})
    assert not ApprovalFlowMixin.v3_auto_decision_allowed({**base, "identity_basis": "name"}, {"试剂名称": "乙醇", "CAS号": "67-56-1"}, {}, {"final_category": ""})
    assert not ApprovalFlowMixin.v3_auto_decision_allowed({**base, "identity_basis": "name"}, {"试剂名称": "乙醇", "CAS号": "64-17-5"}, {}, {"final_category": "普通类"})
    assert not ApprovalFlowMixin.v3_auto_decision_allowed({**base, "identity_basis": "name", "review_required": True}, {"试剂名称": "乙醇"}, {}, {})


def test_api_double_readback_rejects_old_value_and_wrong_identity(monkeypatch):
    client = ErpApiClient.__new__(ErpApiClient); client.protocol = "odoo_jsonrpc"
    monkeypatch.setattr(client, "_odoo_property_id", lambda _category: 12)
    monkeypatch.setattr(client, "identity_matches_record", lambda identity, record: identity["序号"] == str(record["sequence"]))
    monkeypatch.setattr("erp_api_client.time.sleep", lambda _seconds: None)
    records = iter([{"sequence": "7", "name": "乙醇", "phchproperty_id": [12, "易燃类"]}, {"sequence": "7", "name": "乙醇", "phchproperty_id": [12, "易燃类"]}])
    monkeypatch.setattr(client, "_read_odoo_line_property", lambda _record_id: next(records))
    assert client.confirm_property_twice_by_id("123", "易燃类", {"序号": "7"})
