from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from erp_api_client import ErpApiClient, ErpApiUnsupported, normalize_erp_write_backend
from erp_api_discovery import (  # noqa: E402
    ApiDiscoveryRecorder,
    ApiDiscoveryAnalyzer,
    DiscoveryCandidate,
    ErpApiConfigurator,
    classify_api_action,
    sanitize_headers,
    sanitize_value,
)


def test_api_write_requires_verified_discovery_state() -> None:
    client = ErpApiClient(
        page=object(),  # type: ignore[arg-type]
        settings={
            "erp_api": {
                "enabled_for_write": True,
                "discovery": {"status": "pending_capture"},
                "endpoints": {"save_property": "/api/save"},
            }
        },
    )

    assert client.write_enabled() is False
    assert client.configuration_status()["write_verified"] is False


def test_odoo_verify_retries_targeted_line_until_property_changes(monkeypatch) -> None:
    client = ErpApiClient(
        page=object(),  # type: ignore[arg-type]
        settings={
            "erp_api": {
                "protocol": "odoo_jsonrpc",
                "verify_attempts": 3,
                "verify_delays_seconds": [0, 0],
            }
        },
    )
    records = iter(
        [
            {"phchproperty_id": False, "phchproperty_name": ""},
            {"phchproperty_id": [25, "普通类"], "phchproperty_name": "普通类"},
        ]
    )
    monkeypatch.setattr(client, "_odoo_property_id", lambda _category: 25)
    monkeypatch.setattr(client, "_read_odoo_line_property", lambda _record_id: next(records))
    monkeypatch.setattr("erp_api_client.time.sleep", lambda _seconds: None)

    assert client.verify_physicochemical_property("123", "普通类", {"清单号": "SJ1"}) is True


def test_sanitize_headers_redacts_auth_cookie_and_token() -> None:
    headers = {
        "Authorization": "Bearer secret",
        "Cookie": "sid=secret",
        "X-CSRF-Token": "abc",
        "User-Agent": "browser",
    }

    assert sanitize_headers(headers) == {
        "Authorization": "<redacted>",
        "Cookie": "<redacted>",
        "X-CSRF-Token": "<redacted>",
        "User-Agent": "browser",
    }


def test_sanitize_value_redacts_sensitive_keys_and_contact_values() -> None:
    payload = {
        "password": "secret",
        "contact": "张三 13812345678 user@example.com",
        "nested": {"token": "secret", "name": "丙酮"},
    }

    assert sanitize_value(payload) == {
        "password": "<redacted>",
        "contact": "张三 <phone> <email>",
        "nested": {"token": "<redacted>", "name": "丙酮"},
    }


def test_classify_api_action_marks_property_save_candidate() -> None:
    action = classify_api_action(
        "POST",
        "/api/reagent/approval/save",
        {"physicochemicalProperty": "未知类"},
        {"success": "bool"},
    )

    assert action == "possible_write_or_save"


def test_erp_write_backend_normalizes_unknown_to_web_ui() -> None:
    assert normalize_erp_write_backend("api_write_with_web_verify") == "api_write_with_web_verify"
    assert normalize_erp_write_backend("unknown") == "web_ui"


def test_api_client_fails_closed_without_configured_endpoint() -> None:
    client = ErpApiClient(page=object(), settings={})  # type: ignore[arg-type]

    result = client.save_physicochemical_property(
        {"序号": "1", "试剂名称": "丙酮", "最终建议类别": "易燃类"},
        "易燃类",
    )

    assert result.attempted is False
    assert result.saved is False
    assert result.verified is False
    assert result.fallback_to_web is True


def test_api_client_read_endpoint_extracts_nested_records() -> None:
    class FakeResponse:
        ok = True
        status = 200

        def json(self):
            return {"data": {"records": [{"listNumber": "SJ1"}]}}

    class FakeRequest:
        def fetch(self, url, method, **kwargs):  # noqa: ANN001
            assert url == "https://erp.example.com/api/todos"
            assert method == "GET"
            return FakeResponse()

    class FakeContext:
        request = FakeRequest()

    class FakePage:
        url = "https://erp.example.com/app"
        context = FakeContext()

    client = ErpApiClient(
        page=FakePage(),  # type: ignore[arg-type]
        settings={"erp_api": {"base_url": "https://erp.example.com/", "endpoints": {"todo_tasks": "/api/todos"}}},
    )

    assert client.fetch_todo_tasks() == [{"listNumber": "SJ1"}]


def test_api_client_read_endpoint_requires_configuration() -> None:
    client = ErpApiClient(page=object(), settings={})  # type: ignore[arg-type]

    try:
        client.fetch_todo_tasks()
    except ErpApiUnsupported as error:
        assert "todo_tasks" in str(error)
    else:
        raise AssertionError("missing endpoint should fail closed")


def test_api_client_fetches_all_paginated_detail_records() -> None:
    class FakeResponse:
        ok = True
        status = 200

        def __init__(self, page_num: int):
            self.page_num = page_num

        def json(self):
            records = [{"id": f"r{self.page_num}", "sequence": str(self.page_num), "reagentName": f"试剂{self.page_num}"}]
            return {"data": {"records": records}, "total": 2}

    class FakeRequest:
        def fetch(self, url, method, **kwargs):  # noqa: ANN001
            assert method == "GET"
            return FakeResponse(int(kwargs["params"]["pageNum"]))

    class FakeContext:
        request = FakeRequest()

    class FakePage:
        url = "https://erp.example.com/app"
        context = FakeContext()

    client = ErpApiClient(
        page=FakePage(),  # type: ignore[arg-type]
        settings={
            "erp_api": {
                "base_url": "https://erp.example.com/",
                "endpoints": {"reagent_detail": "/api/reagents"},
                "pagination": {"page_size": 1, "max_pages": 5},
                "field_mapping": {"序号": ["sequence"], "试剂名称": ["reagentName"]},
            }
        },
    )

    assert client.fetch_reagent_detail("SJ1") == [
        {"id": "r1", "sequence": "1", "reagentName": "试剂1", "序号": "1", "试剂名称": "试剂1", "_erp_record_id": "r1"},
        {"id": "r2", "sequence": "2", "reagentName": "试剂2", "序号": "2", "试剂名称": "试剂2", "_erp_record_id": "r2"},
    ]


def test_api_client_resolves_record_id_with_identity_check() -> None:
    client = ErpApiClient(page=object(), settings={})  # type: ignore[arg-type]

    record_id, record = client.resolve_reagent_record_id(
        {"序号": "2", "试剂名称": "丙酮", "CAS号": "67-64-1"},
        [
            {"id": "wrong", "序号": "1", "试剂名称": "乙醇", "CAS号": "64-17-5"},
            {"id": "right", "序号": "2", "试剂名称": "丙酮", "CAS号": "67-64-1"},
        ],
    )

    assert record_id == "right"
    assert record["试剂名称"] == "丙酮"


def test_api_client_blocks_write_when_identity_mismatches_record() -> None:
    client = ErpApiClient(
        page=object(),  # type: ignore[arg-type]
        settings={"erp_api": {"enabled_for_write": True, "discovery": {"status": "verified"}, "endpoints": {"save_property": "/api/save"}}},
    )

    result = client.save_physicochemical_property_by_id(
        "r1",
        "易燃类",
        {"序号": "1", "试剂名称": "丙酮", "CAS号": "67-64-1"},
        {"id": "r1", "序号": "1", "试剂名称": "乙醇", "CAS号": "64-17-5"},
    )

    assert result.attempted is True
    assert result.saved is False
    assert "identity" in result.detail


def test_api_client_write_success_requires_verify_property() -> None:
    calls: list[tuple[str, str, dict]] = []

    class FakeResponse:
        ok = True
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeRequest:
        def fetch(self, url, method, **kwargs):  # noqa: ANN001
            calls.append((method, url, kwargs))
            if method == "POST":
                return FakeResponse({"success": True})
            return FakeResponse({"data": {"records": [{"id": "r1", "physicochemicalProperty": "易燃类"}]}})

    class FakeContext:
        request = FakeRequest()

    class FakePage:
        url = "https://erp.example.com/app"
        context = FakeContext()

    client = ErpApiClient(
        page=FakePage(),  # type: ignore[arg-type]
        settings={
            "erp_api": {
                "enabled_for_write": True,
                "discovery": {"status": "verified"},
                "base_url": "https://erp.example.com/",
                "endpoints": {"save_property": "/api/save", "verify_property": "/api/read"},
            }
        },
    )

    result = client.save_physicochemical_property_by_id("r1", "易燃类", {"序号": "1", "试剂名称": "丙酮"})

    assert result.attempted is True
    assert result.saved is True
    assert result.verified is True
    assert result.record_id == "r1"
    assert calls[0][0] == "POST"
    assert calls[1][0] == "GET"


def test_api_client_write_saved_but_unverified_is_not_successful() -> None:
    class FakeResponse:
        ok = True
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeRequest:
        def fetch(self, url, method, **kwargs):  # noqa: ANN001
            if method == "POST":
                return FakeResponse({"success": True})
            return FakeResponse({"data": {"records": [{"id": "r1", "physicochemicalProperty": "-"}]}})

    class FakeContext:
        request = FakeRequest()

    class FakePage:
        url = "https://erp.example.com/app"
        context = FakeContext()

    client = ErpApiClient(
        page=FakePage(),  # type: ignore[arg-type]
        settings={
            "erp_api": {
                "enabled_for_write": True,
                "discovery": {"status": "verified"},
                "base_url": "https://erp.example.com/",
                "endpoints": {"save_property": "/api/save", "verify_property": "/api/read"},
            }
        },
    )

    result = client.save_physicochemical_property_by_id("r1", "易燃类", {"序号": "1", "试剂名称": "丙酮"})

    assert result.saved is True
    assert result.verified is False


def _discovery_events() -> list[dict]:
    detail = {
        "method": "GET",
        "origin": "https://erp.example.com",
        "path": "/api/reagents/detail",
        "status": 200,
        "candidate_fields": {
            "record_id_fields": ["id"],
            "property_fields": ["physicochemicalProperty"],
            "sequence_fields": ["sequence"],
            "name_fields": ["reagentName"],
        },
        "action_hint": "possible_detail_read",
    }
    writes = []
    for sequence, record_id, category in (("1", "r1", "易燃类"), ("2", "r2", "普通类")):
        writes.append(
            {
                "method": "PATCH",
                "origin": "https://erp.example.com",
                "path": "/api/reagents/property",
                "status": 200,
                "in_save_window": True,
                "web_save_verified": True,
                "request_headers": {"content-type": "application/json"},
                "request_payload": {
                    "id": record_id,
                    "physicochemicalProperty": category,
                    "sequence": sequence,
                },
                "response_payload": {"success": True},
                "save_context": {
                    "record_id": record_id,
                    "sequence": sequence,
                    "expected_property": category,
                },
            }
        )
    return [detail, *writes]


def test_discovery_analyzer_requires_two_verified_distinct_saves() -> None:
    candidate = ApiDiscoveryAnalyzer().analyze(_discovery_events(), required_write_samples=2)

    assert candidate.status == "candidate"
    assert candidate.confidence == 1.0
    assert candidate.config["base_url"] == "https://erp.example.com"
    assert candidate.config["save_method"] == "PATCH"
    assert candidate.config["endpoints"]["save_property"] == "/api/reagents/property"
    assert candidate.config["save_payload_mapping"] == {
        "record_id": "id",
        "property": "physicochemicalProperty",
        "sequence": "sequence",
    }
    assert candidate.config["field_mapping"]["序号"] == ["sequence"]
    assert candidate.config["field_mapping"]["试剂名称"] == ["reagentName"]


def test_discovery_analyzer_does_not_promote_one_save() -> None:
    candidate = ApiDiscoveryAnalyzer().analyze(_discovery_events()[:2], required_write_samples=2)

    assert candidate.status == "pending_capture"
    assert "verified_write_samples:1/2" in candidate.missing


def test_configurator_promotes_fail_closed_then_can_activate(tmp_path: Path) -> None:
    settings_path = tmp_path / "config" / "settings.yaml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "erp_api": {
                    "enabled_for_write": False,
                    "base_url": "",
                    "discovery": {"status": "pending_capture", "backup_dir": "backups"},
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    candidate = ApiDiscoveryAnalyzer().analyze(_discovery_events())
    configurator = ErpApiConfigurator(tmp_path, settings_path)

    assert configurator.promote_pending(candidate) is True
    pending = yaml.safe_load(settings_path.read_text(encoding="utf-8"))["erp_api"]
    assert pending["enabled_for_write"] is False
    assert pending["discovery"]["status"] == "pending_canary"
    assert pending["endpoints"]["save_property"] == "/api/reagents/property"
    assert list((tmp_path / "backups").glob("settings_*.yaml"))

    configurator.activate()
    active = yaml.safe_load(settings_path.read_text(encoding="utf-8"))["erp_api"]
    assert active["enabled_for_write"] is True
    assert active["discovery"]["status"] == "verified"


def test_configurator_reject_restores_previous_api_config(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    original = {
        "erp_api": {
            "enabled_for_write": False,
            "base_url": "",
            "endpoints": {"reagent_detail": "", "save_property": ""},
            "discovery": {"status": "pending_capture"},
        }
    }
    settings_path.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")
    configurator = ErpApiConfigurator(tmp_path, settings_path)
    assert configurator.promote_pending(ApiDiscoveryAnalyzer().analyze(_discovery_events())) is True

    configurator.reject("canary mismatch")

    restored = yaml.safe_load(settings_path.read_text(encoding="utf-8"))["erp_api"]
    assert restored["base_url"] == ""
    assert restored["endpoints"]["save_property"] == ""
    assert restored["enabled_for_write"] is False
    assert restored["discovery"]["status"] == "rejected"
    assert restored["discovery"]["last_result"] == "canary mismatch"


def test_api_client_canary_supports_patch_and_form_payload() -> None:
    calls: list[tuple[str, dict]] = []

    class FakeResponse:
        ok = True
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeRequest:
        def fetch(self, url, method, **kwargs):  # noqa: ANN001
            calls.append((method, kwargs))
            if method == "PATCH":
                return FakeResponse({"success": True})
            return FakeResponse({"id": "r1", "physicochemicalProperty": "易燃类"})

    class FakeContext:
        request = FakeRequest()

    class FakePage:
        url = "https://erp.example.com/app"
        context = FakeContext()

    client = ErpApiClient(
        page=FakePage(),  # type: ignore[arg-type]
        settings={
            "erp_api": {
                "enabled_for_write": False,
                "base_url": "https://erp.example.com",
                "save_method": "PATCH",
                "save_content_type": "application/x-www-form-urlencoded",
                "endpoints": {"save_property": "/save", "verify_property": "/read"},
            }
        },
    )

    result = client.save_physicochemical_property_by_id(
        "r1", "易燃类", {"序号": "1", "试剂名称": "丙酮"}, allow_canary=True
    )

    assert result.saved is True
    assert result.verified is True
    assert calls[0][0] == "PATCH"
    assert calls[0][1]["form"] == {"id": "r1", "physicochemicalProperty": "易燃类"}


def test_odoo_jsonrpc_reads_full_list_and_saves_by_stable_line_id() -> None:
    calls: list[dict] = []

    class FakeResponse:
        ok = True
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeRequest:
        def fetch(self, url, method, **kwargs):  # noqa: ANN001
            assert url == "https://erp.example.com/api/dataset/cors_call_kw"
            assert method == "POST"
            assert kwargs["headers"]["x-openerp-session-id"] == "runtime-only"
            params = kwargs["data"]["params"]
            calls.append(params)
            if params["model"] == "reagent.list" and params["method"] == "web_search_read":
                return FakeResponse(
                    {
                        "result": {
                            "records": [
                                {"id": 3838, "name": "SJ1", "reagent_list_line_ids": [638684]}
                            ]
                        }
                    }
                )
            if params["model"] == "reagent.list.line":
                return FakeResponse(
                    {
                        "result": {
                            "records": [
                                {
                                    "id": 638684,
                                    "name": "丙酮",
                                    "cas_code": "67-64-1",
                                    "sequence": 1,
                                    "phchproperty_name": "易燃类",
                                    "reagent_list_id": [3838, "SJ1"],
                                }
                            ]
                        }
                    }
                )
            if params["model"] == "reagent.phchproperty":
                return FakeResponse({"result": [[25, "普通类"], [51, "易燃类"]]})
            if params["model"] == "reagent.list" and params["method"] == "write":
                return FakeResponse({"result": True})
            raise AssertionError(params)

    class FakeContext:
        request = FakeRequest()

    class FakePage:
        url = "https://erp.example.com/app"
        context = FakeContext()
        _erp_runtime_headers = {"x-openerp-session-id": "runtime-only"}
        _erp_runtime_rpc_context = {"lang": "zh_CN", "tz": "Asia/Shanghai", "uid": 7}

    settings = {
        "erp_api": {
            "protocol": "odoo_jsonrpc",
            "enabled_for_write": False,
            "base_url": "https://erp.example.com",
            "endpoints": {
                "reagent_detail": "/api/dataset/cors_call_kw",
                "save_property": "/api/dataset/cors_call_kw",
                "verify_property": "/api/dataset/cors_call_kw",
            },
            "rpc": {"endpoint": "/api/dataset/cors_call_kw"},
            "field_mapping": {
                "序号": ["sequence"],
                "试剂名称": ["name"],
                "CAS号": ["cas_code"],
                "物化特性": ["phchproperty_name"],
            },
        }
    }
    client = ErpApiClient(page=FakePage(), settings=settings)  # type: ignore[arg-type]
    records = client.fetch_reagent_detail("SJ1")
    suggestion = {
        "清单号": "SJ1",
        "序号": "1",
        "试剂名称": "丙酮",
        "CAS号": "67-64-1",
        "最终建议类别": "易燃类",
    }
    record_id, record = client.resolve_reagent_record_id(suggestion, records)
    result = client.save_physicochemical_property_by_id(
        record_id,
        "易燃类",
        suggestion,
        record,
        allow_canary=True,
    )

    assert record_id == "638684"
    assert result.saved is True
    assert result.verified is True
    write = next(call for call in calls if call["method"] == "write")
    command = write["args"][1]["reagent_list_line_ids"][0]
    assert command[:2] == [1, 638684]
    assert command[2]["id"] == 638684
    assert command[2]["name"] == "丙酮"
    assert command[2]["cas_code"] == "67-64-1"
    assert command[2]["reagent_list_id"] == 3838
    assert command[2]["phchproperty_id"] == 51
    assert write["kwargs"] == {"context": {"lang": "zh_CN", "tz": "Asia/Shanghai", "uid": 7}}


def test_discovery_analyzer_recognizes_verified_odoo_numeric_property_writes() -> None:
    detail = {
        "method": "POST",
        "origin": "https://erp.example.com",
        "path": "/api/dataset/cors_call_kw",
        "status": 200,
        "candidate_fields": {
            "record_id_fields": ["id"],
            "property_fields": ["phchproperty_id", "phchproperty_name"],
            "sequence_fields": ["sequence"],
            "name_fields": ["name"],
            "cas_fields": ["cas_code"],
            "list_number_fields": ["name"],
        },
        "action_hint": "possible_detail_read",
    }
    events = [detail]
    for sequence, line_id, name, cas, property_id, category in (
        ("1", 101, "丙酮", "67-64-1", 51, "易燃类"),
        ("2", 102, "水", "7732-18-5", 25, "普通类"),
    ):
        events.append({
            "method": "POST",
            "origin": "https://erp.example.com",
            "path": "/api/dataset/cors_call_kw",
            "status": 200,
            "in_save_window": True,
            "web_save_verified": True,
            "request_headers": {"content-type": "application/json"},
            "request_payload": {
                "params": {
                    "model": "reagent.list",
                    "method": "write",
                    "args": [[88], {"reagent_list_line_ids": [[1, line_id, {
                        "id": line_id,
                        "name": name,
                        "cas_code": cas,
                        "sequence": int(sequence),
                        "phchproperty_id": property_id,
                    }]]}],
                    "kwargs": {"context": {"lang": "zh_CN", "uid": 7}},
                }
            },
            "response_payload": {"result": True},
            "save_context": {
                "record_id": str(line_id),
                "sequence": sequence,
                "name": name,
                "cas": cas,
                "expected_property": category,
            },
        })

    candidate = ApiDiscoveryAnalyzer().analyze(events, required_write_samples=2)

    assert candidate.status == "candidate"
    assert candidate.confidence == 1.0
    assert candidate.config["protocol"] == "odoo_jsonrpc"
    assert candidate.config["rpc"]["endpoint"] == "/api/dataset/cors_call_kw"
    assert candidate.config["record_id_fields"] == ["id"]
    assert candidate.config["property_fields"] == ["phchproperty_id", "phchproperty_name"]


def test_discovery_recorder_keeps_save_context_until_late_response() -> None:
    callbacks: dict[str, object] = {}

    class FakePage:
        def on(self, event, callback):  # noqa: ANN001
            callbacks[event] = callback

    class FakeRequest:
        method = "POST"
        url = "https://erp.example.com/api/save"
        resource_type = "xhr"
        headers = {"content-type": "application/json"}
        post_data = '{"property":"未知类"}'

    class FakeResponse:
        status = 200
        headers = {"content-type": "application/json"}

        def __init__(self, request):
            self.request = request

        def json(self):
            return {"success": True}

    recorder = ApiDiscoveryRecorder(Path("."))
    recorder.attach(FakePage())  # type: ignore[arg-type]
    request = FakeRequest()
    recorder.begin_save_window({"序号": "1", "试剂名称": "样品"}, "未知类")
    callbacks["request"](request)  # type: ignore[operator]
    recorder.end_save_window(True)
    callbacks["response"](FakeResponse(request))  # type: ignore[operator]

    assert recorder.events[0]["in_save_window"] is True
    assert recorder.events[0]["web_save_verified"] is True
    assert recorder.events[0]["save_context"]["sequence"] == "1"
