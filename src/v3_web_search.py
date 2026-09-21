"""Alibaba Responses/web_search adapter for the efficient V3 pipeline."""
from __future__ import annotations

import json
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from reagent_name_rules import unknown_reagent_name_reason


class V3WebSearchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


V3_SHARED_BEIJING_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
V3_RECOMMENDED_MODELS = ("qwen3.7-plus", "qwen3.7-flash", "qwen3.7-max", "qwen3.8-flash", "qwen3.8-max")
FLASH_POINT_STATUSES = {"measured", "not_applicable", "unknown", "unreliable"}
HAZARD_STATUSES = {"present", "absent", "unknown", "unreliable"}
TOXICITY_STATUSES = {"measured", "classified", "unknown", "unreliable"}
V3_RETRIEVAL_MODES = {"model_first", "always_web"}


def resolve_v3_base_url(mode: str, workspace_id: str = "", custom_url: str = "") -> str:
    selected = str(mode or "shared_beijing").strip().lower()
    if selected == "shared_beijing":
        return V3_SHARED_BEIJING_URL
    if selected == "workspace_beijing":
        workspace = str(workspace_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}", workspace):
            raise V3WebSearchError("业务空间 ID 格式无效。")
        return f"https://{workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    if selected == "custom":
        return validate_v3_base_url(custom_url)
    raise V3WebSearchError("未知的 V3 接入地址模式。")


def validate_v3_base_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if not url or parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or parsed.path != "/compatible-mode/v1":
        raise V3WebSearchError("Responses Base URL 必须为 HTTPS 地址且以 /compatible-mode/v1 结尾。")
    return url


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _valid_cas(value: str) -> bool:
    value = str(value or "").strip()
    if not re.fullmatch(r"\d{2,7}-\d{2}-\d", value):
        return False
    digits = "".join(value.split("-")[:-1])
    return sum((index + 1) * int(digit) for index, digit in enumerate(reversed(digits))) % 10 == int(value[-1])


def rule_bundle(rule_engine: Any) -> dict[str, Any]:
    """Build the complete, compact V3 rule pack from the active local engine.

    The pack intentionally contains rule semantics rather than an Excel export:
    aliases have already been applied during name normalization, while every
    enabled matcher, example and threshold remains available to the model.
    """
    rule_columns = ["id", "category", "match_type", "field_scope", "condition", "patterns", "examples", "example_match_modes", "description"]
    rules = []
    for rule in getattr(rule_engine, "rules", []) or []:
        patterns = [str(value).strip() for value in getattr(rule, "explanation_keywords", ()) or () if str(value).strip()]
        examples = [str(value).strip() for value in getattr(rule, "example_keywords", ()) or () if str(value).strip()]
        rules.append([
            str(getattr(rule, "rule_id", "") or ""), str(getattr(rule, "category", "") or ""),
            str(getattr(rule, "match_type", "keyword") or "keyword"), list(getattr(rule, "field_scope", ()) or ()),
            str(getattr(rule, "condition", "any") or "any"), patterns, examples,
            list(getattr(rule, "example_match_modes", ()) or ()),
            str(getattr(rule, "explanation", "") or "")[:240] if not patterns else "",
        ])
    threshold_columns = ["id", "category", "field", "operator", "value", "unit", "description"]
    thresholds = [[
            str(getattr(item, "threshold_id", "") or ""), str(getattr(item, "category", "") or ""),
            str(getattr(item, "field", "") or ""), str(getattr(item, "operator", "") or ""),
            str(getattr(item, "value", "") or ""), str(getattr(item, "unit", "") or ""),
            str(getattr(item, "description", "") or "")[:300],
        ]
        for item in getattr(rule_engine, "thresholds", []) or []
    ]
    pack = {
        "priority": list(getattr(rule_engine, "priority", []) or []),
        "manual_review_categories": sorted(str(value) for value in (getattr(rule_engine, "manual_review_categories", set()) or set())),
        "rule_columns": rule_columns,
        "rules": rules,
        "threshold_columns": threshold_columns,
        "thresholds": thresholds,
        "guardrails": [
            "按 priority 从高到低选择类别；本规则包外不得自行发明项目类别。",
            "名称或 CAS 身份不可靠、物性不能支持结论时，review_required 必须为 true。",
            "含汞名称属于拒收规则；酸盐、盐酸盐、硝酸盐和硫酸盐不是对应无机酸本体。",
            "普通类仅能在未命中任何更高优先级风险规则时使用。",
            "规则引擎会再次复算；模型类别只是一项候选审批意见。",
        ],
    }
    fingerprint = hashlib.sha256(json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    return {
        "rule_version": str(getattr(rule_engine, "rule_version", "")),
        "fingerprint": f"v3:{fingerprint}",
        "priority": pack["priority"],
        "model_rule_pack": pack,
    }


class V3WebSearchClient:
    def __init__(self, settings: dict[str, Any], root_dir: Path | None = None, requester: Callable[..., Any] | None = None) -> None:
        self.config = ((settings.get("approval", {}) or {}).get("v3", {}) or {})
        self.root_dir = root_dir or Path.cwd()
        self.requester = requester or urlopen

    def configured(self) -> tuple[bool, str]:
        try:
            validate_v3_base_url(str(self.config.get("base_url") or ""))
        except V3WebSearchError as error:
            return False, str(error)
        env_name = str(self.config.get("api_key_env") or "DASHSCOPE_API_KEY")
        if not os.getenv(env_name, "").strip() and not str(self.config.get("api_key") or "").strip():
            return False, f"V3 百炼密钥未配置（{env_name}）"
        return True, ""

    @staticmethod
    def _valid_cas(value: str) -> bool:
        """Compatibility shim for older callers; V3 only uses it as fallback."""
        return _valid_cas(value)

    def request_batch(
        self,
        items: list[dict[str, Any]],
        bundle: dict[str, Any],
        list_number: str = "",
        *,
        alternate: bool = False,
        use_web_search: bool | None = None,
    ) -> dict[str, Any]:
        configured_mode = str(self.config.get("retrieval_mode") or "model_first").strip().lower()
        if configured_mode not in V3_RETRIEVAL_MODES:
            configured_mode = "model_first"
        use_web = configured_mode == "always_web" if use_web_search is None else bool(use_web_search)
        configured, reason = self.configured()
        if not configured:
            return {"ok": False, "error": reason, "items": self._failed(items, reason), "retrieval_mode": "web_search" if use_web else "model_only"}
        expected = [self._input_item(item, alternate=alternate) for item in items]
        pending = [item for item in expected if item["identity_basis"] != "unresolved"]
        immediate = [self._failed_from_input(item, "名称不可识别且 CAS 无效，已快速转人工复核") for item in expected if item["identity_basis"] == "unresolved"]
        if not pending:
            return {"ok": True, "items": immediate, "web_search_calls": 0, "elapsed_seconds": 0.0, "rules_fingerprint": bundle.get("fingerprint") or bundle.get("rule_version", "")}
        categories = list(bundle.get("priority") or [])
        prompt = {
            "task": "检索每条试剂的物化特性，并按允许类别给出推荐审批类别。" if use_web else "根据已知化学知识识别每条试剂，并按允许类别给出推荐审批类别。",
            "list_number": list_number,
            "allowed_categories": categories,
            "project_rule_pack": bundle.get("model_rule_pack") or {},
            "items": [{key: value for key, value in item.items() if key != "fallback_search_name"} for item in pending],
            "requirements": [
                "identified_cas 必须是你识别出的 CAS；与 erp_cas 相同填 cas_status=matched，不同填 conflict，无法确认填 unknown。",
                "无法可靠识别身份、CAS 或类别时 review_required 必须为 true。",
                "证据不足时 review_required 必须为 true。",
                "flash_point 和 boiling_point 的 measured 值必须以摄氏度字符串表示，例如 13°C；不适用或未知时使用对应状态。",
                "必须按 project_rule_pack 给出 recommended_category；不得以常识覆盖其中的优先级、排除条件或阈值。",
                "project_rule_pack.rules 和 thresholds 为行数组；其字段顺序分别由 rule_columns 和 threshold_columns 定义。",
            ],
        }
        if use_web:
            prompt["requirements"].extend([
                "必须使用 web_search；每项结论必须给出本次搜索返回来源 URL 和非空来源摘要。",
                "每个来源摘要限 240 个字符以内，只保留支撑类别或物性结论的直接事实。",
            ])
        else:
            prompt["requirements"].extend([
                "本次没有联网工具。source_urls、source_url、source_text 必须为空，不能编造网页来源。",
                "所有非 unknown 的物性结论必须在 reason 中说明知识依据或不确定性。",
            ])
        body = {
            "model": str(self.config.get("model") or "qwen3.7-plus"),
            "instructions": (
                "你是试剂物性联网检索器。必须调用 web_search，并严格返回 JSON Schema 指定的数据。不要输出 Markdown。"
                if use_web else
                "你是试剂物性分类器。不得编造网页来源；不确定时要求人工复核。严格返回 JSON Schema 指定的数据，不要输出 Markdown。"
            ),
            "input": json.dumps(prompt, ensure_ascii=False),
            "response_format": self._response_format(categories),
            "store": False,
        }
        if use_web:
            body["tools"] = [{"type": "web_search"}]
        timeout = max(10, min(90, int(self.config.get("timeout_seconds", 45))))
        started = time.monotonic()
        max_retries = max(0, min(2, int(self.config.get("max_retries", 0))))
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self._post(body, timeout)
                break
            except V3WebSearchError as error:
                if error.retryable and attempts <= max_retries:
                    time.sleep(min(2.0, 0.5 * attempts))
                    continue
                return {
                    "ok": False,
                    "error": str(error),
                    "items": immediate + self._failed_from_expected(pending, str(error)),
                    "elapsed_seconds": time.monotonic() - started,
                    "attempts": attempts,
                    "network_unstable": bool(error.retryable),
                    "retrieval_mode": "web_search" if use_web else "model_only",
                }
        called, source_urls = self._search_details(response)
        parsed = self._json_output(response)
        if use_web and not called:
            reason = "Responses 返回中未发现成功的 web_search_call"
            return {"ok": False, "error": reason, "items": immediate + self._failed_from_expected(pending, reason), "elapsed_seconds": time.monotonic() - started, "attempts": attempts, "network_unstable": False, "retrieval_mode": "web_search"}
        answers = self._normalize_provider_output(parsed, pending, source_urls, categories, allow_legacy=use_web)
        if answers is None:
            reason = "模型输出未满足 V3 JSON Schema 条目协议"
            return {"ok": False, "error": reason, "items": immediate + self._failed_from_expected(pending, reason), "elapsed_seconds": time.monotonic() - started, "attempts": attempts, "network_unstable": False, "retrieval_mode": "web_search" if use_web else "model_only"}
        return {
            "ok": True,
            "items": immediate + self._validate(pending, answers, source_urls, categories, require_web_sources=use_web),
            "usage": response.get("usage") or {},
            "web_search_calls": 1 if use_web else 0,
            "retrieval_mode": "web_search" if use_web else "model_only",
            "elapsed_seconds": time.monotonic() - started,
            "rules_fingerprint": bundle.get("fingerprint") or bundle.get("rule_version", ""), "attempts": attempts, "network_unstable": False,
        }

    @staticmethod
    def _response_format(categories: list[str]) -> dict[str, Any]:
        fact = {"type": "object", "properties": {"status": {"type": "string"}, "value": {"type": "string"}, "source_url": {"type": "string"}, "source_text": {"type": "string"}, "reason": {"type": "string"}}, "required": ["status", "value", "source_url", "source_text", "reason"], "additionalProperties": False}
        field_names = ("flash_point", "boiling_point", "toxicity", "corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal")
        properties = {name: fact for name in field_names}
        properties["hazard_text"] = {"type": "string"}
        item = {"type": "object", "properties": {"sequence": {"type": "string"}, "raw_name": {"type": "string"}, "identity_basis": {"type": "string", "enum": ["name", "cas_fallback"]}, "identified_cas": {"type": "string"}, "cas_status": {"type": "string", "enum": ["matched", "unknown", "conflict"]}, "recommended_category": {"type": "string", "enum": categories}, "review_required": {"type": "boolean"}, "source_urls": {"type": "array", "items": {"type": "string"}}, "category_evidence": fact, "properties": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}, "reason": {"type": "string"}, "uncertainties": {"type": "array", "items": {"type": "string"}}}, "required": ["sequence", "raw_name", "identity_basis", "identified_cas", "cas_status", "recommended_category", "review_required", "source_urls", "category_evidence", "properties", "reason", "uncertainties"], "additionalProperties": False}
        return {"type": "json_schema", "json_schema": {"name": "reagent_web_facts", "strict": True, "schema": {"type": "object", "properties": {"items": {"type": "array", "items": item}}, "required": ["items"], "additionalProperties": False}}}

    def _post(self, body: dict[str, Any], timeout: int) -> dict[str, Any]:
        endpoint = str(self.config.get("base_url") or "").rstrip("/") + "/responses"
        key = str(self.config.get("api_key") or "").strip() or os.getenv(str(self.config.get("api_key_env") or "DASHSCOPE_API_KEY"), "").strip()
        # Each approval batch is independent. Reusing a provider-side session can
        # retain an obsolete output contract after a V3 schema change.
        request = Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with self.requester(request, timeout=timeout) as result:
                return json.loads(result.read().decode("utf-8"))
        except HTTPError as exc:
            raise V3WebSearchError(
                f"百炼 Responses 请求失败（{exc.code}）：{exc.read().decode('utf-8', 'replace')[:300]}",
                retryable=exc.code == 429 or 500 <= exc.code < 600,
            ) from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise V3WebSearchError(f"百炼 Responses 请求失败：{exc}", retryable=True) from exc

    @staticmethod
    def _input_item(item: dict[str, Any], *, alternate: bool = False) -> dict[str, str]:
        reagent, normalized = item["reagent"], item.get("name_result") or {}
        raw = str(reagent.get("试剂名称") or "").strip()
        cleaned, standard = str(normalized.get("cleaned_name") or "").strip(), str(normalized.get("standard_name") or "").strip()
        unusable = bool(unknown_reagent_name_reason(raw, cleaned, standard) or normalized.get("need_manual_review", False))
        names = list(dict.fromkeys(value for value in (standard, cleaned, raw) if value and value != "-"))
        cas = str(reagent.get("CAS号") or "").strip()
        if not unusable and names:
            selected = names[1] if alternate and len(names) > 1 else names[0]
            return {"sequence": str(reagent.get("序号") or ""), "raw_name": raw, "search_name": selected, "fallback_search_name": names[1] if len(names) > 1 else "", "identity_basis": "name", "erp_cas": cas if _valid_cas(cas) else ""}
        if _valid_cas(cas):
            return {"sequence": str(reagent.get("序号") or ""), "raw_name": raw, "search_name": cas, "fallback_search_name": "", "identity_basis": "cas_fallback", "erp_cas": cas}
        return {"sequence": str(reagent.get("序号") or ""), "raw_name": raw, "search_name": "", "fallback_search_name": "", "identity_basis": "unresolved", "erp_cas": ""}

    @staticmethod
    def _json_output(response: dict[str, Any]) -> Any:
        text = str(response.get("output_text") or "").strip()
        if not text:
            text = "".join(str(content.get("text") or "") for item in response.get("output") or [] for content in item.get("content") or [] if content.get("type") in {"output_text", "text"})
        try:
            return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _search_details(response: dict[str, Any]) -> tuple[bool, set[str]]:
        urls, called = set(), False
        for item in response.get("output") or []:
            if item.get("type") != "web_search_call":
                continue
            called = called or str(item.get("status") or "").lower() in {"completed", "success"}
            for source in ((item.get("action") or {}).get("sources") or item.get("sources") or []):
                url = str(source.get("url") if isinstance(source, dict) else source).strip()
                if url:
                    urls.add(url)
        return called, urls

    def _normalize_provider_output(
        self,
        parsed: Any,
        expected: list[dict[str, str]],
        source_urls: set[str],
        categories: list[str],
        *,
        allow_legacy: bool,
    ) -> list[dict[str, Any]] | None:
        """Normalize a provider response before enforcing the V3 contract.

        The current Responses + web_search provider sometimes ignores its JSON
        Schema parameter and emits its older single-item JSON shape.  We do not
        accept arbitrary prose: this adapter only reconstructs the missing
        contract fields from provider fields that are still independently
        checked against the current web_search_call sources.
        """
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            return parsed["items"]
        if isinstance(parsed, list):
            answers = parsed
        elif isinstance(parsed, dict) and parsed.get("sequence") is not None:
            answers = [parsed]
        else:
            return None
        expected_by_sequence = {item["sequence"]: item for item in expected}
        normalized: list[dict[str, Any]] = []
        for answer in answers:
            if not isinstance(answer, dict):
                return None
            sequence = str(answer.get("sequence") or "")
            item = expected_by_sequence.get(sequence)
            if item is None:
                return None
            if "recommended_category" in answer and "properties" in answer and "category_evidence" in answer:
                normalized.append(answer)
                continue
            if not allow_legacy:
                return None
            legacy = self._normalize_legacy_answer(answer, item, source_urls, categories)
            if legacy is None:
                return None
            normalized.append(legacy)
        return normalized

    @staticmethod
    def _normalized_category(value: Any, categories: list[str]) -> str:
        category = str(value or "").strip()
        if category in categories:
            return category
        aliases = {"易燃液体": "易燃类", "易燃类": "易燃液体", "强反应性": "强反应", "强反应": "强反应性", "不建议接收类": "拒收类", "拒收类": "不建议接收类"}
        mapped = aliases.get(category, "")
        return mapped if mapped in categories else ""

    def _normalize_legacy_answer(
        self,
        answer: dict[str, Any],
        item: dict[str, str],
        source_urls: set[str],
        categories: list[str],
    ) -> dict[str, Any] | None:
        search_name = str(answer.get("search_name") or "").strip()
        if _compact(search_name) != _compact(item["search_name"]):
            return None
        category = self._normalized_category(answer.get("recommended_category") or answer.get("category"), categories)
        review_required = answer.get("review_required")
        source_url = str(answer.get("source_url") or "").strip()
        urls = [str(url).strip() for url in answer.get("source_urls") or [] if str(url).strip()]
        if source_url and source_url not in urls:
            urls.append(source_url)
        source_text = str(answer.get("source_summary") or answer.get("source_chemical_facts") or answer.get("hazard_summary") or "").strip()
        if not category or not isinstance(review_required, bool) or not urls or not source_text:
            return None
        if any(url not in source_urls for url in urls):
            return None
        evidence = {"status": "present", "value": "", "source_url": urls[0], "source_text": source_text, "reason": ""}
        properties = {
            name: {"status": "unknown", "value": "", "source_url": "", "source_text": "", "reason": "资料未覆盖"}
            for name in ("flash_point", "boiling_point", "toxicity", "corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal")
        }
        positive_fields = {
            "易燃液体": "flammable", "易燃类": "flammable", "氧化剂": "oxidizing",
            "重金属类": "heavy_metal", "强反应性": "water_reactive", "强反应": "water_reactive",
            "易爆类": "explosive_risk", "常规酸": "corrosive", "特殊酸": "corrosive",
            "常规碱": "corrosive", "高毒类": "toxicity",
        }
        field = positive_fields.get(category)
        if field:
            status = "classified" if field == "toxicity" else "present"
            properties[field] = {**evidence, "status": status}
        properties["hazard_text"] = source_text
        return {
            "sequence": item["sequence"], "raw_name": item["raw_name"], "identity_basis": item["identity_basis"],
            "identified_cas": "", "cas_status": "unknown",
            "recommended_category": category, "review_required": review_required, "source_urls": urls,
            "category_evidence": evidence, "properties": properties,
            "reason": str(answer.get("reason") or source_text), "uncertainties": answer.get("uncertainties") or [],
            "provider_legacy_shape": True,
        }

    def _validate(
        self,
        expected: list[dict[str, str]],
        returned: list[Any],
        source_urls: set[str],
        categories: list[str],
        *,
        require_web_sources: bool,
    ) -> list[dict[str, Any]]:
        by_sequence = {str(row.get("sequence") or ""): row for row in returned if isinstance(row, dict)}
        if len(by_sequence) != len(expected) or set(by_sequence) != {row["sequence"] for row in expected}:
            return self._failed_from_expected(expected, "模型条目数量、序号或重复项不匹配")
        results = []
        allowed_item_fields = {
            "sequence", "raw_name", "identity_basis", "identified_cas", "cas_status", "recommended_category", "review_required",
            "source_urls", "category_evidence", "properties", "reason", "uncertainties",
            "provider_legacy_shape",
        }
        for item in expected:
            answer = by_sequence[item["sequence"]]
            if set(answer) - allowed_item_fields:
                results.append(self._failed_from_input(item, "模型条目包含未定义字段")); continue
            if _compact(answer.get("raw_name")) != _compact(item["raw_name"]):
                results.append(self._failed_from_input(item, "模型返回试剂名称与 ERP 原文不一致")); continue
            if answer.get("identity_basis") != item["identity_basis"]:
                results.append(self._failed_from_input(item, "模型身份依据与请求不一致")); continue
            category = str(answer.get("recommended_category") or "").strip()
            if category not in categories or not isinstance(answer.get("review_required"), bool):
                results.append(self._failed_from_input(item, "模型类别或复核标记无效")); continue
            identified_cas = str(answer.get("identified_cas") or "").strip()
            cas_status = str(answer.get("cas_status") or "").strip()
            erp_cas = str(item.get("erp_cas") or "").strip()
            if cas_status not in {"matched", "unknown", "conflict"}:
                results.append(self._failed_from_input(item, "模型 CAS 状态无效")); continue
            if cas_status == "matched" and (not _valid_cas(identified_cas) or identified_cas != erp_cas):
                results.append(self._failed_from_input(item, "模型 CAS 标记为匹配但未与 ERP CAS 一致")); continue
            if cas_status == "unknown" and identified_cas:
                results.append(self._failed_from_input(item, "模型 CAS 状态为未知时不得提供候选 CAS")); continue
            if cas_status == "conflict" and (not _valid_cas(identified_cas) or identified_cas == erp_cas):
                results.append(self._failed_from_input(item, "模型 CAS 冲突信息无效")); continue
            urls = [str(url).strip() for url in answer.get("source_urls") or [] if str(url).strip()]
            evidence = answer.get("category_evidence") or {}
            if require_web_sources:
                if not urls or any(url not in source_urls for url in urls) or not self._valid_source(evidence, source_urls):
                    results.append(self._failed_from_input(item, "模型来源 URL 缺失或类别来源摘要不可核验")); continue
                if _compact(item["search_name"]) not in _compact(evidence.get("source_text")):
                    results.append(self._failed_from_input(item, "来源摘要未确认检索身份")); continue
            elif urls or str(evidence.get("source_url") or "").strip() or str(evidence.get("source_text") or "").strip():
                results.append(self._failed_from_input(item, "未联网模型结果不得声明网页来源")); continue
            error = self._validate_properties(answer.get("properties"), source_urls, require_sources=require_web_sources)
            if error:
                results.append(self._failed_from_input(item, error)); continue
            properties = answer.get("properties") or {}
            ordinary_complete = all((properties.get(field) or {}).get("status") == "absent" for field in ("corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal"))
            results.append({"ok": True, "sequence": item["sequence"], "candidate_category": category, "review_required": answer["review_required"], "identity_basis": item["identity_basis"], "identity_status": "cas_verified" if cas_status == "matched" else "unresolved", "identified_cas": identified_cas, "cas_status": cas_status, "name_evidence_valid": require_web_sources, "category_evidence_valid": require_web_sources, "category_evidence": evidence, "properties": properties, "ordinary_evidence_complete": ordinary_complete, "reason": str(answer.get("reason") or ""), "uncertainties": answer.get("uncertainties") or [], "source_urls": urls, "search_name": item["search_name"], "retrieval_mode": "web_search" if require_web_sources else "model_only"})
        return results

    @staticmethod
    def _valid_source(value: Any, source_urls: set[str]) -> bool:
        return isinstance(value, dict) and bool(str(value.get("source_text") or "").strip() and str(value.get("source_url") or "").strip() in source_urls)

    def _validate_properties(self, properties: Any, source_urls: set[str], *, require_sources: bool) -> str:
        if not isinstance(properties, dict):
            return "模型 properties 必须为对象。"
        required = {"flash_point", "boiling_point", "toxicity", "corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal", "hazard_text"}
        if required - set(properties):
            return "模型 properties 缺少固定字段。"
        if set(properties) - required:
            return "模型 properties 包含未定义字段。"
        for field in ("flash_point", "boiling_point"):
            value = properties.get(field) or {}; status = str(value.get("status") or "") if isinstance(value, dict) else ""
            if status not in FLASH_POINT_STATUSES or (require_sources and status == "measured" and not self._valid_source(value, source_urls)):
                return f"模型 {field} 缺少有效状态或来源摘要。"
            if not require_sources and (str(value.get("source_url") or "").strip() or str(value.get("source_text") or "").strip()):
                return f"未联网模型 {field} 不得提供来源 URL。"
        for field in ("corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal"):
            value = properties.get(field) or {}; status = str(value.get("status") or "") if isinstance(value, dict) else ""
            if status not in HAZARD_STATUSES or (require_sources and status in {"present", "absent"} and not self._valid_source(value, source_urls)):
                return f"模型 {field} 缺少有效状态或来源摘要。"
            if not require_sources and (str(value.get("source_url") or "").strip() or str(value.get("source_text") or "").strip()):
                return f"未联网模型 {field} 不得提供来源 URL。"
        toxicity = properties.get("toxicity") or {}; status = str(toxicity.get("status") or "") if isinstance(toxicity, dict) else ""
        if status not in TOXICITY_STATUSES or (require_sources and status in {"measured", "classified"} and not self._valid_source(toxicity, source_urls)):
            return "模型 toxicity 缺少有效状态或来源摘要。"
        if not require_sources and (str(toxicity.get("source_url") or "").strip() or str(toxicity.get("source_text") or "").strip()):
            return "未联网模型 toxicity 不得提供来源 URL。"
        return ""

    @staticmethod
    def _failed(items: list[dict[str, Any]], reason: str) -> list[dict[str, Any]]:
        return [V3WebSearchClient._failed_from_input(V3WebSearchClient._input_item(item), reason) for item in items]

    @staticmethod
    def _failed_from_expected(expected: list[dict[str, str]], reason: str) -> list[dict[str, Any]]:
        return [V3WebSearchClient._failed_from_input(item, reason) for item in expected]

    @staticmethod
    def _failed_from_input(item: dict[str, str], reason: str) -> dict[str, Any]:
        return {"ok": False, "sequence": item["sequence"], "candidate_category": "", "identity_basis": item.get("identity_basis", "unresolved"), "identity_status": "unresolved", "reason": reason, "properties": {}, "uncertainties": [], "source_urls": []}


def test_v3_web_search_connection(settings: dict[str, Any], *, base_url: str, model: str, api_key: str = "", timeout_seconds: int = 30, requester: Callable[..., Any] | None = None) -> dict[str, Any]:
    effective_url = validate_v3_base_url(base_url)
    config = {"approval": {"v3": {"base_url": effective_url, "model": str(model or "").strip(), "api_key": api_key, "timeout_seconds": max(10, min(60, int(timeout_seconds)))} }}
    if not config["approval"]["v3"]["model"]:
        raise V3WebSearchError("请选择或填写 V3 模型。")
    client = V3WebSearchClient(config, requester=requester)
    started = time.monotonic()
    response = client._post({"model": config["approval"]["v3"]["model"], "input": "请使用 web_search 查询 water 并仅回答 OK。", "tools": [{"type": "web_search"}], "store": False}, config["approval"]["v3"]["timeout_seconds"])
    called, sources = client._search_details(response)
    if not called:
        raise V3WebSearchError("模型响应未触发成功的 web_search_call；请更换支持 Responses 联网搜索的模型。")
    return {"ok": True, "base_url": effective_url, "model": config["approval"]["v3"]["model"], "elapsed_ms": round((time.monotonic() - started) * 1000), "web_search_calls": 1, "source_count": len(sources)}


def test_v3_model_connection(settings: dict[str, Any], *, base_url: str, model: str, api_key: str = "", timeout_seconds: int = 30, requester: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Verify the selected Responses model can return V3's strict JSON without web search."""
    effective_url = validate_v3_base_url(base_url)
    config = {"approval": {"v3": {"base_url": effective_url, "model": str(model or "").strip(), "api_key": api_key, "timeout_seconds": max(10, min(60, int(timeout_seconds)))}}}
    if not config["approval"]["v3"]["model"]:
        raise V3WebSearchError("请选择或填写 V3 模型。")
    config["approval"]["v3"]["retrieval_mode"] = "model_first"
    client = V3WebSearchClient(config, requester=requester)
    started = time.monotonic()
    result = client.request_batch(
        [{"reagent": {"序号": "1", "试剂名称": "water", "CAS号": "7732-18-5"}, "name_result": {"standard_name": "water", "cleaned_name": "water"}}],
        {"priority": ["普通类"], "rule_version": "connection-test"},
        use_web_search=False,
    )
    if not result.get("ok") or not (result.get("items") or [{}])[0].get("ok"):
        raise V3WebSearchError(str(result.get("error") or (result.get("items") or [{}])[0].get("reason") or "模型未返回有效的 V3 JSON Schema 数据。"))
    return {"ok": True, "base_url": effective_url, "model": config["approval"]["v3"]["model"], "elapsed_ms": round((time.monotonic() - started) * 1000), "web_search_calls": 0, "source_count": 0}
