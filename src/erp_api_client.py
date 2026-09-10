from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Page


ALLOWED_ERP_WRITE_BACKENDS = {"web_ui", "api_read_web_write", "api_write_with_web_verify"}


def normalize_erp_write_backend(value: Any, *, default: str = "web_ui") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in ALLOWED_ERP_WRITE_BACKENDS:
        return normalized
    return default if default in ALLOWED_ERP_WRITE_BACKENDS else "web_ui"


@dataclass
class ApiWriteResult:
    attempted: bool
    saved: bool
    verified: bool
    detail: str = ""
    fallback_to_web: bool = True
    record_id: str = ""


class ErpApiUnsupported(RuntimeError):
    pass


class ErpApiClient:
    """Configuration-driven ERP API adapter.

    The adapter fails closed until real ERP endpoints and field mappings have
    been discovered and explicitly enabled. It reuses Playwright's logged-in
    request context, so it does not bypass ERP permissions.
    """

    DEFAULT_RECORD_ID_FIELDS = ("id", "recordId", "record_id", "detailId", "detail_id", "reagentId", "reagent_id")
    DEFAULT_RECORD_LIST_KEYS = ("records", "rows", "data", "list", "items")
    DEFAULT_PROPERTY_FIELDS = ("物化特性", "physicochemicalProperty", "property", "propertyName")

    def __init__(self, page: Page, settings: dict[str, Any] | None = None):
        self.page = page
        self.settings = settings or {}
        self.api_settings = self.settings.get("erp_api") or {}
        self.endpoints = self.api_settings.get("endpoints") or {}
        self.field_mapping = self.api_settings.get("field_mapping") or {}

    def configuration_status(self) -> dict[str, Any]:
        missing = []
        for name in ("reagent_detail", "save_property"):
            if not self._endpoint(name):
                missing.append(f"endpoints.{name}")
        if not self._record_id_fields():
            missing.append("record_id_fields")
        if not self._property_fields():
            missing.append("property_fields")
        return {
            "configured": not missing,
            "write_enabled": self.write_enabled(),
            "missing": missing,
        }

    def write_enabled(self) -> bool:
        return bool(self.api_settings.get("enabled_for_write") is True)

    def fetch_todo_tasks(self) -> list[dict[str, Any]]:
        payload = self._get_json_endpoint("todo_tasks")
        return [self.normalize_record(record) for record in self._extract_records(payload)]

    def fetch_reagent_detail(self, list_number: str) -> list[dict[str, Any]]:
        endpoint = self._endpoint("reagent_detail")
        if not endpoint:
            raise ErpApiUnsupported("ERP API reagent_detail endpoint is not configured.")

        records: list[dict[str, Any]] = []
        pagination = self.api_settings.get("pagination") or {}
        page_param = str(pagination.get("page_param") or "pageNum")
        page_size_param = str(pagination.get("page_size_param") or "pageSize")
        list_number_param = str(pagination.get("list_number_param") or "listNumber")
        page_size = int(pagination.get("page_size") or 200)
        max_pages = int(pagination.get("max_pages") or 50)

        for page_number in range(1, max_pages + 1):
            params = {
                list_number_param: list_number,
                page_param: page_number,
                page_size_param: page_size,
            }
            method = str(self.api_settings.get("reagent_detail_method") or "GET").strip().upper()
            if method not in {"GET", "POST"}:
                raise ErpApiUnsupported(f"Unsupported ERP API reagent detail method: {method}")
            payload = self._request_json(method, endpoint, **({"params": params} if method == "GET" else {"data": params}))
            page_records = self._extract_records(payload)
            records.extend(self.normalize_record(record) for record in page_records)
            if not self._has_next_page(payload, page_number, page_size, len(page_records)):
                break
        return records

    def resolve_reagent_record_id(
        self,
        suggestion: dict[str, Any],
        records: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        candidates = records
        if candidates is None:
            list_number = str(suggestion.get("试剂清单号") or suggestion.get("清单号") or "").strip()
            if not list_number:
                raise ErpApiUnsupported("ERP API record lookup needs a list number.")
            candidates = self.fetch_reagent_detail(list_number)

        expected_sequence = self._text(suggestion.get("序号"))
        expected_name = self._text(
            suggestion.get("试剂名称")
            or suggestion.get("ERP原始名")
            or suggestion.get("清洗后名称")
            or suggestion.get("标准化名称")
        )
        expected_cas = self._normalize_cas(suggestion.get("CAS号"))

        best: tuple[int, dict[str, Any]] | None = None
        for record in candidates:
            score = 0
            record_sequence = self._text(record.get("序号") or record.get("sequence"))
            record_name = self._text(record.get("试剂名称") or record.get("name") or record.get("reagent_name"))
            record_cas = self._normalize_cas(record.get("CAS号") or record.get("cas"))
            if expected_sequence and record_sequence == expected_sequence:
                score += 10
            if expected_name and record_name and (expected_name == record_name or expected_name in record_name or record_name in expected_name):
                score += 4
            if expected_cas and record_cas and expected_cas == record_cas:
                score += 3
            if score and (best is None or score > best[0]):
                best = (score, record)

        if not best or best[0] < 10:
            raise ErpApiUnsupported("ERP API could not resolve a stable reagent record id from detail records.")

        record_id = self.record_id_from_record(best[1])
        if not record_id:
            raise ErpApiUnsupported("ERP API detail record matched identity but does not contain a stable record id.")
        return record_id, best[1]

    def record_id_from_record(self, record: dict[str, Any]) -> str:
        for key in self._record_id_fields():
            value = record.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    def save_physicochemical_property(
        self,
        suggestion: dict[str, Any],
        erp_category: str,
        *,
        allow_canary: bool = False,
    ) -> ApiWriteResult:
        if not self._endpoint("save_property"):
            return ApiWriteResult(
                attempted=False,
                saved=False,
                verified=False,
                detail="ERP API save_property endpoint is not configured.",
                fallback_to_web=True,
            )
        if not self.write_enabled() and not allow_canary:
            return ApiWriteResult(
                attempted=False,
                saved=False,
                verified=False,
                detail="ERP API write is disabled by erp_api.enabled_for_write=false.",
                fallback_to_web=True,
            )
        if not self._identity_is_complete(suggestion):
            return ApiWriteResult(
                attempted=True,
                saved=False,
                verified=False,
                detail="ERP API write blocked because reagent identity is incomplete.",
                fallback_to_web=True,
            )
        try:
            record_id = str(suggestion.get("_erp_record_id") or "").strip()
            record = suggestion.get("_erp_record")
            if not record_id or not isinstance(record, dict):
                records = self.fetch_reagent_detail(
                    str(suggestion.get("试剂清单号") or suggestion.get("清单号") or "").strip()
                )
                record_id, record = self.resolve_reagent_record_id(suggestion, records)
            return self.save_physicochemical_property_by_id(
                record_id, erp_category, suggestion, record, allow_canary=allow_canary
            )
        except ErpApiUnsupported as error:
            return ApiWriteResult(
                attempted=True,
                saved=False,
                verified=False,
                detail=str(error),
                fallback_to_web=True,
            )

    def save_physicochemical_property_by_id(
        self,
        record_id: str,
        erp_category: str,
        identity: dict[str, Any],
        record: dict[str, Any] | None = None,
        *,
        allow_canary: bool = False,
    ) -> ApiWriteResult:
        if not self.write_enabled() and not allow_canary:
            return ApiWriteResult(
                attempted=False,
                saved=False,
                verified=False,
                detail="ERP API write is disabled by erp_api.enabled_for_write=false.",
                fallback_to_web=True,
                record_id=record_id,
            )
        endpoint = self._endpoint("save_property")
        if not endpoint:
            return ApiWriteResult(False, False, False, "ERP API save_property endpoint is not configured.", True, record_id)
        if record and not self.identity_matches_record(identity, record):
            return ApiWriteResult(True, False, False, "ERP API write blocked because identity does not match detail record.", True, record_id)

        payload = self._save_payload(record_id, erp_category, identity)
        method = str(self.api_settings.get("save_method") or "POST").strip().upper()
        if method not in {"POST", "PUT", "PATCH"}:
            return ApiWriteResult(True, False, False, f"Unsupported ERP API save method: {method}", True, record_id)
        response_payload = self._request_json(method, endpoint, **self._save_request_kwargs(payload))
        if not self._response_successful(response_payload):
            return ApiWriteResult(True, False, False, "ERP API save_property response did not match success indicators.", True, record_id)
        verified = self.verify_physicochemical_property(record_id, erp_category, identity)
        return ApiWriteResult(True, True, verified, "ERP API write saved and verified." if verified else "ERP API write saved but verify failed.", True, record_id)

    def verify_physicochemical_property(
        self,
        record_id: str,
        expected: str,
        identity: dict[str, Any] | None = None,
    ) -> bool:
        endpoint = self._endpoint("verify_property") or self._endpoint("reagent_detail")
        if not endpoint:
            return False
        params = {"id": record_id}
        if identity:
            list_number = str(identity.get("试剂清单号") or identity.get("清单号") or "").strip()
            if list_number:
                params["listNumber"] = list_number
        method = str(self.api_settings.get("reagent_detail_method") or "GET").strip().upper()
        if method not in {"GET", "POST"}:
            return False
        payload = self._request_json(method, endpoint, **({"params": params} if method == "GET" else {"data": params}))
        records = [self.normalize_record(record) for record in self._extract_records(payload)]
        if not records and isinstance(payload, dict):
            records = [self.normalize_record(payload)]
        for record in records:
            if self.record_id_from_record(record) and self.record_id_from_record(record) != str(record_id):
                continue
            value = self._record_property_value(record)
            if self._text(value) == self._text(expected):
                return True
        return False

    def identity_matches_record(self, identity: dict[str, Any], record: dict[str, Any]) -> bool:
        expected_sequence = self._text(identity.get("序号"))
        record_sequence = self._text(record.get("序号") or record.get("sequence"))
        if expected_sequence and record_sequence and expected_sequence != record_sequence:
            return False
        expected_name = self._text(identity.get("试剂名称") or identity.get("ERP原始名"))
        record_name = self._text(record.get("试剂名称") or record.get("name") or record.get("reagent_name"))
        if expected_name and record_name and expected_name not in record_name and record_name not in expected_name:
            return False
        expected_cas = self._normalize_cas(identity.get("CAS号"))
        record_cas = self._normalize_cas(record.get("CAS号") or record.get("cas"))
        if expected_cas and record_cas and expected_cas != record_cas:
            return False
        return True

    def normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(record)
        for standard_key, source_keys in self.field_mapping.items():
            if standard_key in normalized:
                continue
            for source_key in self._as_list(source_keys):
                if source_key in record:
                    normalized[standard_key] = record.get(source_key)
                    break
        record_id = self.record_id_from_record(normalized)
        if record_id:
            normalized["_erp_record_id"] = record_id
        return normalized

    def _save_payload(self, record_id: str, erp_category: str, identity: dict[str, Any]) -> dict[str, Any]:
        payload_mapping = self.api_settings.get("save_payload_mapping") or {}
        id_key = str(payload_mapping.get("record_id") or "id")
        property_key = str(payload_mapping.get("property") or "physicochemicalProperty")
        payload: dict[str, Any] = {}
        self._set_payload_path(payload, id_key, record_id)
        self._set_payload_path(payload, property_key, erp_category)
        optional_sources = {
            "list_number": identity.get("试剂清单号") or identity.get("清单号"),
            "sequence": identity.get("序号"),
            "name": identity.get("试剂名称") or identity.get("ERP原始名"),
            "cas": identity.get("CAS号"),
        }
        for logical_key, value in optional_sources.items():
            target_key = payload_mapping.get(logical_key)
            if target_key and value not in (None, ""):
                self._set_payload_path(payload, str(target_key), value)
        static_payload = self.api_settings.get("save_static_payload") or {}
        if isinstance(static_payload, dict):
            payload.update(static_payload)
        return payload

    def _save_request_kwargs(self, payload: dict[str, Any]) -> dict[str, Any]:
        content_type = str(self.api_settings.get("save_content_type") or "application/json").lower()
        if "application/x-www-form-urlencoded" in content_type:
            return {"form": payload}
        if "multipart/form-data" in content_type:
            return {"multipart": payload}
        return {"data": payload}

    @staticmethod
    def _set_payload_path(payload: dict[str, Any], dotted_key: str, value: Any) -> None:
        parts = [part for part in str(dotted_key or "").split(".") if part]
        if not parts or any(part.isdigit() for part in parts):
            raise ErpApiUnsupported(f"Unsupported ERP API payload path: {dotted_key}")
        target = payload
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[parts[-1]] = value

    def _record_property_value(self, record: dict[str, Any]) -> Any:
        for key in self._property_fields():
            if key in record:
                return record.get(key)
        return ""

    def _get_json_endpoint(self, name: str) -> Any:
        endpoint = self._endpoint(name)
        if not endpoint:
            raise ErpApiUnsupported(f"ERP API {name} endpoint is not configured.")
        return self._request_json("GET", endpoint)

    def _request_json(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        url = self._absolute_url(endpoint)
        response = self.page.context.request.fetch(url, method=method, **kwargs)
        if not response.ok:
            raise ErpApiUnsupported(f"ERP API {method} {endpoint} returned HTTP {response.status}.")
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("_erp_http_status", response.status)
        return payload

    def _endpoint(self, name: str) -> str:
        return str(self.endpoints.get(name) or "").strip()

    def _absolute_url(self, endpoint: str) -> str:
        if endpoint.startswith(("http://", "https://")):
            return endpoint
        base_url = str(self.api_settings.get("base_url") or "").strip()
        if not base_url:
            base_url = self.page.url
        return urljoin(base_url, endpoint)

    def _record_id_fields(self) -> list[str]:
        values = self.api_settings.get("record_id_fields") or self.DEFAULT_RECORD_ID_FIELDS
        return [str(value) for value in self._as_list(values) if str(value).strip()]

    def _property_fields(self) -> list[str]:
        values = self.api_settings.get("property_fields") or self.DEFAULT_PROPERTY_FIELDS
        return [str(value) for value in self._as_list(values) if str(value).strip()]

    def _has_next_page(self, payload: Any, page_number: int, page_size: int, page_record_count: int) -> bool:
        if page_record_count <= 0:
            return False
        if page_record_count < page_size:
            return False
        if not isinstance(payload, dict):
            return False
        total = self._first_int(payload, ("total", "totalCount", "count"))
        if total is not None:
            return page_number * page_size < total
        pages = self._first_int(payload, ("pages", "totalPages", "pageCount"))
        if pages is not None:
            return page_number < pages
        return False

    @classmethod
    def _extract_records(cls, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in cls.DEFAULT_RECORD_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = cls._extract_records(value)
                if nested:
                    return nested
        return []

    def _response_successful(self, payload: Any) -> bool:
        indicators = self.api_settings.get("success_indicators") or [
            {"field": "success", "value": True},
            {"field": "ok", "value": True},
            {"field": "code", "value": 0},
            {"field": "code", "value": "0"},
        ]
        if not isinstance(payload, dict):
            return False
        for indicator in indicators:
            if not isinstance(indicator, dict):
                continue
            field = str(indicator.get("field") or "").strip()
            if not field:
                continue
            expected = indicator.get("value")
            if self._nested_get(payload, field) == expected:
                return True
        return False

    @staticmethod
    def _nested_get(payload: dict[str, Any], dotted_key: str) -> Any:
        value: Any = payload
        for part in dotted_key.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    @staticmethod
    def _identity_is_complete(suggestion: dict[str, Any]) -> bool:
        return bool(
            str(suggestion.get("序号") or "").strip()
            and str(suggestion.get("试剂名称") or suggestion.get("ERP原始名") or "").strip()
            and str(suggestion.get("最终建议类别") or suggestion.get("规则判定类别") or "").strip()
        )

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _normalize_cas(value: Any) -> str:
        return str(value or "").strip().replace(" ", "")

    @staticmethod
    def _first_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
        for key in keys:
            value = payload.get(key)
            try:
                if value not in (None, ""):
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None
