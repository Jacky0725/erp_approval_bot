from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class LlmProvider:
    id: str
    label: str
    base_url: str
    api_key_env: str
    default_models: tuple[str, ...] = ()
    requires_api_key: bool = True
    notes: str = ""


LLM_PROVIDERS: dict[str, LlmProvider] = {
    "siliconflow": LlmProvider(
        id="siliconflow",
        label="硅基流动 SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        api_key_env="SILICONFLOW_API_KEY",
        default_models=("deepseek-ai/DeepSeek-V3.2", "deepseek-ai/DeepSeek-V3"),
    ),
    "deepseek": LlmProvider(
        id="deepseek",
        label="DeepSeek 官方",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        default_models=("deepseek-chat", "deepseek-reasoner"),
    ),
    "aliyun_bailian": LlmProvider(
        id="aliyun_bailian",
        label="阿里云百炼 DashScope",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        default_models=("qwen-plus", "qwen-max", "qwen-turbo"),
    ),
    "baidu_qianfan": LlmProvider(
        id="baidu_qianfan",
        label="百度千帆",
        base_url="https://qianfan.baidubce.com/v2",
        api_key_env="QIANFAN_API_KEY",
        default_models=("ernie-4.5-turbo-128k", "ernie-4.0-turbo-8k"),
    ),
    "volcengine_ark": LlmProvider(
        id="volcengine_ark",
        label="火山方舟 Ark",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key_env="ARK_API_KEY",
        default_models=(),
    ),
    "moonshot": LlmProvider(
        id="moonshot",
        label="Moonshot / Kimi",
        base_url="https://api.moonshot.ai/v1",
        api_key_env="MOONSHOT_API_KEY",
        default_models=("moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"),
    ),
    "openai": LlmProvider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_models=("gpt-4.1", "gpt-4.1-mini"),
    ),
    "openai_compatible": LlmProvider(
        id="openai_compatible",
        label="OpenAI 兼容自定义",
        base_url="",
        api_key_env="LLM_API_KEY",
        default_models=(),
        notes="需要手动填写 Base URL",
    ),
    "ollama": LlmProvider(
        id="ollama",
        label="Ollama 本地",
        base_url="http://localhost:11434/v1",
        api_key_env="LLM_API_KEY",
        default_models=("qwen2.5:7b", "llama3.1:8b"),
        requires_api_key=False,
    ),
    "lmstudio": LlmProvider(
        id="lmstudio",
        label="LM Studio 本地",
        base_url="http://localhost:1234/v1",
        api_key_env="LLM_API_KEY",
        default_models=(),
        requires_api_key=False,
    ),
}


def get_llm_provider(provider_id: str | None) -> LlmProvider:
    provider_id = (provider_id or "").strip()
    return LLM_PROVIDERS.get(provider_id) or LLM_PROVIDERS["siliconflow"]


def provider_options() -> list[dict[str, Any]]:
    return [
        {
            "id": provider.id,
            "label": provider.label,
            "base_url": provider.base_url,
            "api_key_env": provider.api_key_env,
            "default_models": list(provider.default_models),
            "requires_api_key": provider.requires_api_key,
            "notes": provider.notes,
        }
        for provider in LLM_PROVIDERS.values()
    ]


def provider_base_url(provider_id: str | None, configured_base_url: str = "") -> str:
    configured_base_url = (configured_base_url or "").strip()
    if configured_base_url:
        return configured_base_url
    return get_llm_provider(provider_id).base_url


def provider_default_model(provider_id: str | None) -> str:
    provider = get_llm_provider(provider_id)
    return provider.default_models[0] if provider.default_models else ""


def resolve_llm_api_key(provider_id: str | None, supplied_api_key: str = "") -> str:
    supplied_api_key = (supplied_api_key or "").strip()
    if supplied_api_key:
        return supplied_api_key
    provider = get_llm_provider(provider_id)
    candidates = [
        provider.api_key_env,
        "LLM_API_KEY",
        "SILICONFLOW_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        "QIANFAN_API_KEY",
        "ARK_API_KEY",
        "MOONSHOT_API_KEY",
    ]
    for env_name in dict.fromkeys(candidates):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""


def configured_llm_api_key(provider_id: str | None) -> bool:
    return bool(resolve_llm_api_key(provider_id))


def fetch_provider_models(
    provider_id: str,
    base_url: str,
    api_key: str = "",
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    provider = get_llm_provider(provider_id)
    resolved_base_url = provider_base_url(provider.id, base_url).rstrip("/")
    resolved_api_key = resolve_llm_api_key(provider.id, api_key)
    if not resolved_base_url:
        return {"ok": False, "models": list(provider.default_models), "error": "请先填写 LLM Base URL。"}
    if provider.requires_api_key and not resolved_api_key:
        return {"ok": False, "models": list(provider.default_models), "error": "请先填写该平台的 API Key。"}

    request = Request(f"{resolved_base_url}/models", headers={"Accept": "application/json"})
    if resolved_api_key:
        request.add_header("Authorization", f"Bearer {resolved_api_key}")

    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - operator supplied local/provider URL
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as error:
        return {
            "ok": False,
            "models": list(provider.default_models),
            "error": f"模型列表读取失败：HTTP {error.code}",
        }
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        return {
            "ok": False,
            "models": list(provider.default_models),
            "error": f"模型列表读取失败：{error}",
        }

    models = parse_models_response(payload)
    if not models:
        return {"ok": False, "models": list(provider.default_models), "error": "接口返回中没有可识别的模型。"}
    return {"ok": True, "models": models, "error": ""}


def parse_models_response(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            models = []
            for item in data:
                if isinstance(item, dict):
                    model_id = str(item.get("id") or item.get("model") or "").strip()
                else:
                    model_id = str(item or "").strip()
                if model_id:
                    models.append(model_id)
            return sorted(dict.fromkeys(models))
        models = payload.get("models")
        if isinstance(models, list):
            return sorted(dict.fromkeys(str(item).strip() for item in models if str(item).strip()))
    if isinstance(payload, list):
        return sorted(dict.fromkeys(str(item).strip() for item in payload if str(item).strip()))
    return []
