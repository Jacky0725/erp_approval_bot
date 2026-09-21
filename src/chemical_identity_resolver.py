from __future__ import annotations

"""Bounded LLM assistance for resolving non-standard reagent names.

The resolver deliberately produces *search candidates*, not authoritative
chemical identities.  ``ChemicalSearcher`` must still verify every candidate
against an external chemical source before it can affect a decision.
"""

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import threading
from typing import Any, Callable, ClassVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from enrichment_metrics import EnrichmentMetrics


_NON_COMPOUND_MARKERS = (
    "未知", "没写", "未写", "自制", "自配", "lot#", "lot ", "批号",
    "白瓶", "红盖", "样品", "溶液", "混合", "清洗剂", "助剂", "油品",
)
_CAS_PATTERN = re.compile(r"^\d{2,7}-\d{2}-\d$")


def _valid_cas(value: str) -> bool:
    candidate = str(value or "").strip()
    if not _CAS_PATTERN.fullmatch(candidate):
        return False
    digits = candidate.replace("-", "")
    checksum = sum(int(digit) * index for index, digit in enumerate(reversed(digits[:-1]), start=1)) % 10
    return checksum == int(digits[-1])


def _string(value: Any, *, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


@dataclass
class ChemicalIdentityResolver:
    """Produce conservative, structured name/CAS search candidates.

    The caller may inject ``requester`` in tests.  It receives a ``Request``
    and a timeout and must return an object exposing ``read()``.
    """

    settings: dict[str, Any] | None = None
    metrics: EnrichmentMetrics | None = None
    requester: Callable[..., Any] = urlopen
    root_dir: Path | None = None
    _usage_lock: ClassVar[threading.Lock] = threading.Lock()

    @property
    def config(self) -> dict[str, Any]:
        approval = (self.settings or {}).get("approval", {}) or {}
        configured = approval.get("identity_enrichment", {}) or {}
        return configured if isinstance(configured, dict) else {}

    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def resolve(
        self,
        *,
        raw_name: str,
        cleaned_name: str = "",
        standard_name: str = "",
    ) -> dict[str, Any]:
        raw_name, cleaned_name, standard_name = (
            _string(raw_name),
            _string(cleaned_name),
            _string(standard_name),
        )
        result = self._empty_result(raw_name, cleaned_name, standard_name)
        if not self.enabled():
            return result
        if self._non_compound_reason(raw_name, cleaned_name, standard_name):
            result.update({"status": "skipped", "reason": self._non_compound_reason(raw_name, cleaned_name, standard_name)})
            return result
        if not any((raw_name, cleaned_name, standard_name)):
            result.update({"status": "skipped", "reason": "试剂名称为空，无法生成身份补全候选。"})
            return result

        key = os.getenv(str(self.config.get("api_key_env") or "DASHSCOPE_API_KEY"), "").strip()
        if not key:
            result.update({"status": "unavailable", "reason": "百炼身份补全 API Key 未配置。"})
            return result
        if not self._reserve_daily_call():
            result.update({"status": "budget_exhausted", "reason": "身份补全已达到每日调用上限。"})
            return result

        started = time.monotonic()
        try:
            response = self.requester(self._request(raw_name, cleaned_name, standard_name, key), timeout=self._timeout_seconds())
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
            parsed = self._parse_response(payload)
        except HTTPError as error:
            parsed = None
            result.update({"status": "failure", "reason": f"身份补全请求失败：HTTP {error.code}"})
        except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
            parsed = None
            result.update({"status": "failure", "reason": f"身份补全请求失败：{type(error).__name__}"})

        elapsed_ms = round((time.monotonic() - started) * 1000)
        if parsed is None:
            if result["status"] == "disabled":
                result.update({"status": "failure", "reason": "身份补全返回未通过 JSON Schema 校验。"})
            result["attempted"] = True
            result["elapsed_ms"] = elapsed_ms
            self._record("failure", elapsed_ms)
            return result
        result.update(parsed)
        result["attempted"] = True
        result["elapsed_ms"] = elapsed_ms
        self._record("success" if result.get("status") == "resolved" else "unresolved", elapsed_ms)
        return result

    @staticmethod
    def _empty_result(raw_name: str, cleaned_name: str, standard_name: str) -> dict[str, Any]:
        return {
            "attempted": False,
            "status": "disabled",
            "raw_name": raw_name,
            "cleaned_name": cleaned_name,
            "standard_name": standard_name,
            "resolved_standard_name": "",
            "english_name": "",
            "candidate_cas": [],
            "name_type": "unresolved",
            "confidence": 0.0,
            "reason": "",
            "elapsed_ms": 0,
        }

    @staticmethod
    def _non_compound_reason(*values: str) -> str:
        text = " ".join(value.casefold() for value in values if value).strip()
        if not text:
            return ""
        for marker in _NON_COMPOUND_MARKERS:
            if marker in text:
                return f"名称含“{marker}”，可能为商品名、混合物或描述性名称，不生成 CAS 候选。"
        return ""

    def _request(self, raw_name: str, cleaned_name: str, standard_name: str, api_key: str) -> Request:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["standard_name", "english_name", "candidate_cas", "name_type", "confidence", "reason"],
            "properties": {
                "standard_name": {"type": "string"},
                "english_name": {"type": "string"},
                "candidate_cas": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "name_type": {"type": "string", "enum": ["single_compound", "mixture", "product", "custom_solution", "unresolved"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是化学实体标准化助手。只依据名称语言知识生成待外部核验的检索候选，绝不把候选当作事实。"
                    "对无歧义的单一化学品及其常见中文别名/缩写，应返回标准中文名、英文名和已知候选 CAS；"
                    "候选随后会由 PubChem 等外部来源验证。"
                    "商品名、混合物、自制溶液、描述性名称、Lot/批号必须返回对应类型，candidate_cas 为空。"
                    "对无法可靠识别的名称，name_type=unresolved，candidate_cas 为空。"
                ),
            },
            {"role": "user", "content": json.dumps({"erp_raw_name": raw_name, "cleaned_name": cleaned_name, "standard_name": standard_name}, ensure_ascii=False)},
        ]
        body = {
            "model": str(self.config.get("model") or "qwen3.7-flash"),
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": {"name": "chemical_identity_candidate", "strict": True, "schema": schema}},
            "enable_thinking": False,
        }
        base_url = str(self.config.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        return Request(
            f"{base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    def _parse_response(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            content = payload["choices"][0]["message"]["content"]
            value = json.loads(str(content or ""))
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        name_type = _string(value.get("name_type"), limit=40)
        if name_type not in {"single_compound", "mixture", "product", "custom_solution", "unresolved"}:
            return None
        try:
            confidence = max(0.0, min(1.0, float(value.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            return None
        candidate_cas = list(dict.fromkeys(
            cas for cas in (_string(item, limit=24) for item in (value.get("candidate_cas") or [])) if _valid_cas(cas)
        ))[:3]
        if name_type != "single_compound":
            candidate_cas = []
        english_name = _string(value.get("english_name"))
        standard_name = _string(value.get("standard_name"))
        resolved = bool(name_type == "single_compound" and english_name and confidence >= 0.6)
        return {
            "status": "resolved" if resolved else "unresolved",
            "resolved_standard_name": standard_name,
            "english_name": english_name if resolved else "",
            "candidate_cas": candidate_cas if resolved else [],
            "name_type": name_type,
            "confidence": confidence,
            "reason": _string(value.get("reason"), limit=500),
        }

    def _timeout_seconds(self) -> float:
        try:
            return max(3.0, min(15.0, float(self.config.get("timeout_seconds", 10))))
        except (TypeError, ValueError):
            return 10.0

    def _reserve_daily_call(self) -> bool:
        try:
            limit = max(0, int(self.config.get("max_calls_per_day", 0)))
        except (TypeError, ValueError):
            limit = 0
        if limit == 0:
            return True
        root = Path(self.root_dir or Path.cwd())
        path = root / "data" / "logs" / "identity_enrichment_usage.json"
        today = date.today().isoformat()
        with self._usage_lock:
            try:
                current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, ValueError, json.JSONDecodeError):
                current = {}
            count = int(current.get("count") or 0) if current.get("date") == today else 0
            if count >= limit:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"date": today, "count": count + 1}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        return True

    def _record(self, status: str, elapsed_ms: int) -> None:
        if self.metrics is not None:
            self.metrics.record_llm(operation="identity_enrichment", status=status, elapsed_ms=elapsed_ms)
