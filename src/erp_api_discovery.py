from __future__ import annotations

import json
import os
import re
import threading
import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, parse_qs, urlsplit

import yaml

from playwright.sync_api import Error, Page, Request, Response


SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-csrf-token",
    "x-xsrf-token",
    "csrf-token",
    "proxy-authorization",
}
SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|pwd|token|secret|cookie|authorization|session|csrf|xsrf|credential|key)",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[\w.\-+%]+@[\w.\-]+\.[A-Za-z]{2,}")


def sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("<redacted>" if SENSITIVE_KEY_RE.search(str(key)) else sanitize_value(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item) for item in value[:50]]
    if isinstance(value, str):
        text = PHONE_RE.sub("<phone>", value)
        text = EMAIL_RE.sub("<email>", text)
        return text[:2000]
    return value


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADER_NAMES or SENSITIVE_KEY_RE.search(key):
            sanitized[key] = "<redacted>"
        else:
            sanitized[key] = sanitize_value(value)
    return sanitized


def parse_request_payload(request: Request) -> Any:
    method = request.method.upper()
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return {}
    try:
        data = request.post_data
    except Error:
        return {}
    if not data:
        return {}
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        parsed = parse_qs(data, keep_blank_values=True)
        if parsed:
            return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}
        # Unknown encodings (especially multipart bodies) may contain secrets.
        # Keep only the fact that a body existed; never persist the raw body.
        return {"unparsed_body": True}


def parse_response_payload(response: Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return {}
    try:
        return response.json()
    except Exception:
        return {}


def response_field_shape(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {str(key): response_field_shape(value) for key, value in list(payload.items())[:80]}
    if isinstance(payload, list):
        if not payload:
            return []
        return [response_field_shape(payload[0])]
    return type(payload).__name__


def candidate_api_fields(payload: Any) -> dict[str, list[str]]:
    keys: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                keys.add(str(key))
                collect(item)
        elif isinstance(value, list):
            for item in value[:3]:
                collect(item)

    collect(payload)
    lowered = {key.lower(): key for key in keys}
    return {
        "record_id_fields": [
            lowered[key]
            for key in ("id", "recordid", "record_id", "detailid", "detail_id", "reagentid", "reagent_id")
            if key in lowered
        ],
        "property_fields": [
            key
            for key in keys
            if key in {"物化特性", "physicochemicalProperty", "property", "propertyName"}
            or "physicochemical" in key.lower()
            or "property" in key.lower()
        ],
        "list_fields": [
            key
            for key in keys
            if key in {"records", "rows", "data", "list", "items"} or key.lower() in {"records", "rows", "items"}
        ],
        "sequence_fields": [
            key for key in keys
            if key.lower() in {"sequence", "seq", "seqno", "serialno", "index", "rowno"} or key == "序号"
        ],
        "name_fields": [
            key for key in keys
            if key.lower() in {"name", "reagentname", "chemicalname", "materialname"} or key == "试剂名称"
        ],
        "cas_fields": [
            key for key in keys if key.lower().replace("_", "") in {"cas", "casno", "casnumber"} or key == "CAS号"
        ],
        "list_number_fields": [
            key for key in keys
            if key.lower().replace("_", "") in {"listnumber", "approvalno", "applicationno"} or key == "清单号"
        ],
    }


def classify_api_action(method: str, path: str, request_payload: Any, response_payload: Any) -> str:
    text = " ".join(
        [
            method,
            path,
            json.dumps(request_payload, ensure_ascii=False, default=str)[:1000],
            json.dumps(response_field_shape(response_payload), ensure_ascii=False, default=str)[:1000],
        ]
    ).lower()
    if any(word in text for word in ("save", "update", "edit", "approve", "approval", "判定", "审批", "物化", "property")):
        if method in {"POST", "PUT", "PATCH"}:
            return "possible_write_or_save"
    if any(word in text for word in ("detail", "info", "明细", "详情")):
        return "possible_detail_read"
    if any(word in text for word in ("todo", "list", "page", "待办", "清单")):
        return "possible_list_read"
    return "other_api"


@dataclass
class ApiDiscoveryRecorder:
    log_dir: Path
    max_events: int = 200
    events: list[dict[str, Any]] = field(default_factory=list)
    _save_window_start: int | None = field(default=None, init=False, repr=False)
    _save_context: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _save_window_id: int = field(default=0, init=False, repr=False)
    _request_contexts: dict[int, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    def attach(self, page: Page) -> None:
        page.on("request", self._record_request_context)
        page.on("response", self._record_response)

    def _record_request_context(self, request: Request) -> None:
        if self._save_window_start is None:
            return
        self._request_contexts[id(request)] = {
            "save_context": sanitize_value(self._save_context),
            "in_save_window": True,
            "save_window_id": self._save_window_id,
            "web_save_verified": None,
        }

    def _record_response(self, response: Response) -> None:
        if len(self.events) >= self.max_events:
            return
        request = response.request
        resource_type = str(getattr(request, "resource_type", "") or "").lower()
        if resource_type and resource_type not in {"xhr", "fetch"}:
            return
        url = request.url
        parsed = urlsplit(url)
        if not self._looks_like_api(parsed.path):
            return
        request_payload = sanitize_value(parse_request_payload(request))
        response_payload = sanitize_value(parse_response_payload(response))
        query = sanitize_value(dict(parse_qsl(parsed.query, keep_blank_values=True)))
        event = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "method": request.method,
            "origin": f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "",
            "path": parsed.path,
            "query_keys": sorted(query.keys()),
            "query": query,
            "status": response.status,
            "request_headers": sanitize_headers(request.headers),
            "request_payload": request_payload,
            "response_payload": response_payload,
            "response_shape": response_field_shape(response_payload),
            "candidate_fields": candidate_api_fields(response_payload),
            "action_hint": classify_api_action(request.method.upper(), parsed.path, request_payload, response_payload),
        }
        request_context = self._request_contexts.pop(id(request), None)
        if request_context is not None:
            event.update(request_context)
        elif self._save_window_start is not None:
            event["save_context"] = sanitize_value(self._save_context)
            event["in_save_window"] = True
            event["save_window_id"] = self._save_window_id
        self.events.append(event)

    def begin_save_window(self, identity: dict[str, Any], expected_property: str) -> None:
        self._save_window_id += 1
        self._save_window_start = len(self.events)
        self._save_context = {
            "sequence": identity.get("序号") or identity.get("sequence"),
            "name": identity.get("试剂名称") or identity.get("ERP原始名") or identity.get("name"),
            "cas": identity.get("CAS号") or identity.get("cas"),
            "list_number": identity.get("试剂清单号") or identity.get("清单号") or identity.get("listNumber"),
            "record_id": identity.get("_erp_record_id") or identity.get("record_id"),
            "expected_property": expected_property,
        }

    def end_save_window(self, web_save_verified: bool) -> None:
        if self._save_window_start is None:
            return
        for event in self.events[self._save_window_start:]:
            event["web_save_verified"] = bool(web_save_verified)
        for context in self._request_contexts.values():
            if context.get("save_window_id") == self._save_window_id:
                context["web_save_verified"] = bool(web_save_verified)
        self._save_window_start = None
        self._save_context = {}

    @staticmethod
    def _looks_like_api(path: str) -> bool:
        lowered = path.lower()
        return any(marker in lowered for marker in ("/api/", "/ajax/", "/rest/", "/graphql", ".json")) or not any(
            lowered.endswith(suffix) for suffix in (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2")
        )

    def save(self) -> tuple[Path, Path]:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = self.log_dir / f"erp_api_discovery_{stamp}.json"
        md_path = self.log_dir / f"erp_api_discovery_{stamp}.md"
        payload = {"events": self.events}
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(self._markdown_summary(), encoding="utf-8")
        return json_path, md_path

    def _markdown_summary(self) -> str:
        lines = ["# ERP API Discovery", "", f"Captured events: {len(self.events)}", ""]
        for index, event in enumerate(self.events, start=1):
            lines.extend(
                [
                    f"## {index}. {event['method']} {event['path']}",
                    f"- Status: {event['status']}",
                    f"- Action hint: {event['action_hint']}",
                    f"- Query keys: {', '.join(event.get('query_keys') or []) or '-'}",
                    f"- Request payload: `{json.dumps(event.get('request_payload') or {}, ensure_ascii=False, default=str)[:500]}`",
                    f"- Response shape: `{json.dumps(event.get('response_shape') or {}, ensure_ascii=False, default=str)[:500]}`",
                    f"- Candidate fields: `{json.dumps(event.get('candidate_fields') or {}, ensure_ascii=False, default=str)[:500]}`",
                    "",
                ]
            )
        return "\n".join(lines).strip() + "\n"


@dataclass(frozen=True)
class DiscoveryCandidate:
    status: str
    confidence: float
    config: dict[str, Any]
    evidence: list[str]
    missing: list[str]


class ApiDiscoveryAnalyzer:
    """Infer a fail-closed ERP API configuration from correlated browser traffic."""

    WRITE_METHODS = {"POST", "PUT", "PATCH"}
    RECORD_ID_NAMES = ("id", "recordId", "record_id", "detailId", "detail_id", "reagentId", "reagent_id")

    def analyze(self, events: list[dict[str, Any]], required_write_samples: int = 2) -> DiscoveryCandidate:
        write_events = [event for event in events if self._is_verified_property_write(event)]
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for event in write_events:
            key = (str(event.get("origin") or ""), str(event.get("method") or "").upper(), str(event.get("path") or ""))
            grouped.setdefault(key, []).append(event)
        ranked = sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True)
        evidence: list[str] = []
        missing: list[str] = []
        if not ranked:
            return DiscoveryCandidate("pending_capture", 0.0, {}, [], ["save_property"])
        (origin, method, save_path), samples = ranked[0]
        distinct_sequences = {
            str((sample.get("save_context") or {}).get("sequence") or "") for sample in samples
        } - {""}
        if len(samples) < required_write_samples or len(distinct_sequences) < required_write_samples:
            missing.append(f"verified_write_samples:{len(distinct_sequences)}/{required_write_samples}")

        property_field = self._consistent_matching_field(samples, "expected_property")
        record_id_field = self._record_id_field(samples)
        detail_event = self._detail_event(events, origin)
        if not property_field:
            missing.append("property_field")
        if not record_id_field:
            missing.append("record_id_field")
        if not detail_event:
            missing.append("reagent_detail")
        success_indicators = self._success_indicators(samples)
        if not success_indicators:
            missing.append("success_indicator")

        field_mapping: dict[str, list[str]] = {}
        save_mapping: dict[str, str] = {}
        if record_id_field:
            save_mapping["record_id"] = record_id_field
        if property_field:
            save_mapping["property"] = property_field
        for logical, context_key in (
            ("list_number", "list_number"),
            ("sequence", "sequence"),
            ("name", "name"),
            ("cas", "cas"),
        ):
            matched = self._consistent_matching_field(samples, context_key)
            if matched:
                save_mapping[logical] = matched

        if detail_event:
            candidate_fields = detail_event.get("candidate_fields") or {}
            if candidate_fields.get("record_id_fields"):
                field_mapping["record_id"] = list(candidate_fields["record_id_fields"])
            if candidate_fields.get("property_fields"):
                field_mapping["物化特性"] = list(candidate_fields["property_fields"])
            for standard_key, candidate_key in (
                ("序号", "sequence_fields"),
                ("试剂名称", "name_fields"),
                ("CAS号", "cas_fields"),
                ("清单号", "list_number_fields"),
            ):
                values = candidate_fields.get(candidate_key) or []
                if values:
                    field_mapping[standard_key] = list(values)
        has_sequence = bool(field_mapping.get("序号"))
        has_secondary_identity = bool(field_mapping.get("试剂名称") or field_mapping.get("CAS号"))
        if detail_event and (not has_sequence or not has_secondary_identity):
            missing.append("detail_identity_fields")

        config = {
            "base_url": origin,
            "save_method": method,
            "save_content_type": self._content_type(samples[0]),
            "reagent_detail_method": str((detail_event or {}).get("method") or "GET").upper(),
            "endpoints": {
                "reagent_detail": str((detail_event or {}).get("path") or ""),
                "save_property": save_path,
                "verify_property": str((detail_event or {}).get("path") or ""),
            },
            "record_id_fields": list(dict.fromkeys([record_id_field, *(field_mapping.get("record_id") or [])])) if record_id_field else [],
            "property_fields": list(dict.fromkeys([property_field, *(field_mapping.get("物化特性") or [])])) if property_field else [],
            "field_mapping": field_mapping,
            "save_payload_mapping": save_mapping,
            "save_static_payload": self._common_static_payload(samples, set(save_mapping.values())),
            "success_indicators": success_indicators,
        }
        odoo_samples = [self._odoo_line_update(sample) for sample in samples]
        if odoo_samples and all(odoo_samples):
            config.update({
                "protocol": "odoo_jsonrpc",
                "record_id_fields": ["id"],
                "property_fields": ["phchproperty_id", "phchproperty_name"],
                "rpc": {
                    "endpoint": save_path,
                    "models": {
                        "list_model": "reagent.list",
                        "line_model": "reagent.list.line",
                        "property_model": "reagent.phchproperty",
                    },
                },
            })
        if not origin.startswith(("http://", "https://")):
            missing.append("base_url")
        if config["reagent_detail_method"] not in {"GET", "POST"}:
            missing.append("reagent_detail_method")
        evidence.extend([
            f"write={method} {save_path}",
            f"verified_samples={len(distinct_sequences)}",
            f"detail={config['endpoints']['reagent_detail'] or '-'}",
        ])
        confidence = 1.0 if not missing else max(0.0, 0.9 - 0.15 * len(missing))
        return DiscoveryCandidate("candidate" if not missing else "pending_capture", confidence, config, evidence, missing)

    def _is_verified_property_write(self, event: dict[str, Any]) -> bool:
        if str(event.get("method") or "").upper() not in self.WRITE_METHODS:
            return False
        if not event.get("in_save_window") or not event.get("web_save_verified"):
            return False
        expected = str((event.get("save_context") or {}).get("expected_property") or "").strip()
        if expected and self._find_matching_fields(event.get("request_payload"), expected):
            return True
        return bool(expected and self._odoo_line_update(event))

    @staticmethod
    def _odoo_line_update(event: dict[str, Any]) -> dict[str, Any] | None:
        """Return a correlated Odoo one2many line update from a verified save window."""
        payload = event.get("request_payload") or {}
        params = payload.get("params") if isinstance(payload, dict) else None
        if not isinstance(params, dict) or params.get("method") != "write":
            return None
        args = params.get("args")
        if not isinstance(args, list) or len(args) < 2 or not isinstance(args[1], dict):
            return None
        commands = args[1].get("reagent_list_line_ids")
        if not isinstance(commands, list):
            return None
        context = event.get("save_context") or {}
        expected_record_id = str(context.get("record_id") or "").strip()
        expected_sequence = str(context.get("sequence") or "").strip()
        expected_name = str(context.get("name") or "").strip()
        expected_cas = str(context.get("cas") or "").strip()
        for index, command in enumerate(commands):
            if not isinstance(command, list) or len(command) < 3 or command[0] != 1:
                continue
            line_id, values = command[1], command[2]
            if not isinstance(values, dict) or values.get("phchproperty_id") in (None, "", False):
                continue
            if expected_record_id and str(line_id) != expected_record_id:
                continue
            comparisons = (
                (expected_sequence, values.get("sequence")),
                (expected_name, values.get("name")),
                (expected_cas, values.get("cas_code")),
            )
            if any(
                ApiDiscoveryAnalyzer._identity_text(expected)
                and ApiDiscoveryAnalyzer._identity_text(actual)
                and ApiDiscoveryAnalyzer._identity_text(actual) != ApiDiscoveryAnalyzer._identity_text(expected)
                for expected, actual in comparisons
            ):
                continue
            base = f"params.args.1.reagent_list_line_ids.{index}"
            return {
                "record_id": str(line_id),
                "record_id_field": f"{base}.1",
                "property_id": values.get("phchproperty_id"),
                "property_field": f"{base}.2.phchproperty_id",
                "line_values": values,
            }
        return None

    @staticmethod
    def _identity_text(value: Any) -> str:
        """Normalize missing ERP identity values before correlating a save request."""
        if value is None or value is False:
            return ""
        text = str(value).strip()
        return "" if text.lower() in {"", "-", "n/a", "none", "null", "false"} else text

    @classmethod
    def _flatten(cls, value: Any, prefix: str = "") -> list[tuple[str, Any]]:
        output: list[tuple[str, Any]] = []
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{prefix}.{key}" if prefix else str(key)
                output.extend(cls._flatten(item, child))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                output.extend(cls._flatten(item, f"{prefix}.{index}"))
        else:
            output.append((prefix, value))
        return output

    @classmethod
    def _find_matching_fields(cls, payload: Any, expected: Any) -> list[str]:
        expected_text = str(expected or "").strip()
        return [path for path, value in cls._flatten(payload) if expected_text and str(value or "").strip() == expected_text]

    def _consistent_matching_field(self, samples: list[dict[str, Any]], context_key: str) -> str:
        if context_key == "expected_property":
            odoo_fields = [
                str((self._odoo_line_update(sample) or {}).get("property_field") or "")
                for sample in samples
            ]
            if odoo_fields and all(odoo_fields) and len(set(odoo_fields)) == 1:
                return odoo_fields[0]
        fields: list[str] = []
        for sample in samples:
            expected = (sample.get("save_context") or {}).get(context_key)
            matches = self._find_matching_fields(sample.get("request_payload"), expected)
            if len(matches) != 1:
                return ""
            fields.append(matches[0])
        return fields[0] if fields and len(set(fields)) == 1 else ""

    def _record_id_field(self, samples: list[dict[str, Any]]) -> str:
        odoo_fields = [
            str((self._odoo_line_update(sample) or {}).get("record_id_field") or "")
            for sample in samples
        ]
        if odoo_fields and all(odoo_fields) and len(set(odoo_fields)) == 1:
            return odoo_fields[0]
        context_match = self._consistent_matching_field(samples, "record_id")
        if context_match:
            return context_match
        candidates: list[str] = []
        for sample in samples:
            paths = [path for path, _ in self._flatten(sample.get("request_payload"))]
            matches = [path for path in paths if path.rsplit(".", 1)[-1] in self.RECORD_ID_NAMES]
            if len(matches) != 1:
                return ""
            candidates.append(matches[0])
        return candidates[0] if candidates and len(set(candidates)) == 1 else ""

    @staticmethod
    def _detail_event(events: list[dict[str, Any]], origin: str) -> dict[str, Any] | None:
        candidates = []
        for event in events:
            fields = event.get("candidate_fields") or {}
            if str(event.get("origin") or "") != origin or not 200 <= int(event.get("status") or 0) < 300:
                continue
            if fields.get("record_id_fields") and fields.get("property_fields"):
                candidates.append(event)
        candidates.sort(
            key=lambda item: (
                0 if (item.get("candidate_fields") or {}).get("sequence_fields") else 1,
                0
                if (
                    (item.get("candidate_fields") or {}).get("name_fields")
                    or (item.get("candidate_fields") or {}).get("cas_fields")
                )
                else 1,
                0 if item.get("action_hint") == "possible_detail_read" else 1,
            )
        )
        return candidates[0] if candidates else None

    @staticmethod
    def _success_indicators(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        preferred_names = {"success", "ok", "code", "status", "statuscode", "result"}
        accepted_values = {True, 0, "0", 200, "200", "success", "ok"}
        flattened = [dict(ApiDiscoveryAnalyzer._flatten(sample.get("response_payload") or {})) for sample in samples]
        if flattened:
            for path, value in flattened[0].items():
                leaf = path.rsplit(".", 1)[-1].lower()
                if leaf not in preferred_names or value not in accepted_values:
                    continue
                if all(item.get(path) == value for item in flattened[1:]):
                    return [{"field": path, "value": value}]
        statuses = {int(sample.get("status") or 0) for sample in samples}
        if len(statuses) == 1 and all(200 <= status < 300 for status in statuses):
            return [{"field": "_erp_http_status", "value": statuses.pop()}]
        return []

    def _common_static_payload(self, samples: list[dict[str, Any]], dynamic_paths: set[str]) -> dict[str, Any]:
        if not samples:
            return {}
        flattened = [dict(self._flatten(sample.get("request_payload"))) for sample in samples]
        common: dict[str, Any] = {}
        for path, value in flattened[0].items():
            if path in dynamic_paths or "." in path or SENSITIVE_KEY_RE.search(path):
                continue
            if not isinstance(value, (str, int, float, bool)) or value in (None, ""):
                continue
            if all(item.get(path) == value for item in flattened[1:]):
                common[path] = value
        return common

    @staticmethod
    def _content_type(event: dict[str, Any]) -> str:
        headers = event.get("request_headers") or {}
        for key, value in headers.items():
            if str(key).lower() == "content-type":
                return str(value).split(";", 1)[0].strip().lower()
        return "application/json"

    @staticmethod
    def _nested_get(payload: Any, dotted_key: str) -> Any:
        value = payload
        for part in dotted_key.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value


class ErpApiConfigurator:
    """Persist discovery evidence and atomically promote fail-closed settings."""

    _lock = threading.Lock()

    def __init__(self, root_dir: Path, settings_path: Path | None = None):
        self.root_dir = Path(root_dir)
        self.settings_path = settings_path or self.root_dir / "config" / "settings.yaml"

    def save_candidate(self, candidate: DiscoveryCandidate, events: list[dict[str, Any]]) -> Path:
        settings = self._load_settings()
        discovery = ((settings.get("erp_api") or {}).get("discovery") or {})
        configured = discovery.get("candidate_path") or "data/logs/erp_api_candidate.yaml"
        path = self.root_dir / str(configured)
        payload = {
            "status": candidate.status,
            "confidence": candidate.confidence,
            "config": candidate.config,
            "evidence": candidate.evidence,
            "missing": candidate.missing,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "events": events,
        }
        self._atomic_yaml(path, payload)
        return path

    def load_events(self) -> list[dict[str, Any]]:
        settings = self._load_settings()
        discovery = ((settings.get("erp_api") or {}).get("discovery") or {})
        configured = discovery.get("candidate_path") or "data/logs/erp_api_candidate.yaml"
        path = self.root_dir / str(configured)
        if not path.exists():
            return []
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return []
        events = payload.get("events") or []
        return [event for event in events if isinstance(event, dict)][-500:]

    def promote_pending(self, candidate: DiscoveryCandidate) -> bool:
        if candidate.status != "candidate" or candidate.confidence < 0.9 or candidate.missing:
            return False
        with self._lock:
            settings = self._load_settings()
            erp_api = settings.setdefault("erp_api", {})
            discovery = erp_api.setdefault("discovery", {})
            if str(discovery.get("status") or "") in {"pending_canary", "verified", "active"}:
                return False
            self._backup_settings(discovery)
            discovery["previous_config"] = copy.deepcopy(
                {key: value for key, value in erp_api.items() if key != "discovery"}
            )
            for key, value in candidate.config.items():
                if key == "field_mapping":
                    merged = dict(erp_api.get(key) or {})
                    merged.update(value or {})
                    erp_api[key] = merged
                else:
                    erp_api[key] = copy.deepcopy(value)
            erp_api["enabled_for_write"] = False
            discovery.update({
                "status": "pending_canary",
                "confidence": candidate.confidence,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            self._atomic_yaml(self.settings_path, settings)
        return True

    def activate(self) -> None:
        self._set_activation("verified", True, "")

    def reject(self, reason: str) -> None:
        with self._lock:
            settings = self._load_settings()
            erp_api = settings.setdefault("erp_api", {})
            discovery = erp_api.setdefault("discovery", {})
            previous = discovery.get("previous_config")
            if isinstance(previous, dict):
                for key in list(erp_api):
                    if key != "discovery":
                        erp_api.pop(key, None)
                erp_api.update(copy.deepcopy(previous))
            erp_api["enabled_for_write"] = False
            discovery.update({
                "status": "rejected",
                "last_result": reason,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            erp_api["discovery"] = discovery
            self._atomic_yaml(self.settings_path, settings)

    def _set_activation(self, status: str, enabled: bool, reason: str) -> None:
        with self._lock:
            settings = self._load_settings()
            erp_api = settings.setdefault("erp_api", {})
            erp_api["enabled_for_write"] = enabled
            discovery = erp_api.setdefault("discovery", {})
            discovery.update({
                "status": status,
                "last_result": reason,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            self._atomic_yaml(self.settings_path, settings)

    def _backup_settings(self, discovery: dict[str, Any]) -> Path:
        configured = discovery.get("backup_dir") or "data/logs/erp_api_config_backups"
        backup_dir = self.root_dir / str(configured)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"settings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
        if self.settings_path.exists():
            backup.write_bytes(self.settings_path.read_bytes())
        return backup

    def _load_settings(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return {}
        return yaml.safe_load(self.settings_path.read_text(encoding="utf-8")) or {}

    @staticmethod
    def _atomic_yaml(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as file:
                yaml.safe_dump(payload, file, allow_unicode=True, sort_keys=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
