from __future__ import annotations

import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dingtalk_conversation import ChatUnavailable, ConversationStore, chat_config, contains_secret, infer_intent
from dingtalk_stream_bot import DingTalkCommandService, DingTalkStreamBot
from llm_providers import LLM_PROVIDERS


@pytest.fixture(autouse=True)
def isolated_llm_environment(monkeypatch):
    for name in {"LLM_API_KEY", "DINGTALK_CHAT_API_KEY", *(provider.api_key_env for provider in LLM_PROVIDERS.values())}:
        monkeypatch.setenv(name, "test-only-api-key")
    for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def harness():
    settings = {"dingtalk_chat": {"enabled": True}, "llm": {"provider": "siliconflow", "model": "test"}}
    now = [100.0]
    manager = SimpleNamespace(start=Mock(return_value={"started": True}))
    status = {"running": False}
    resolver = Mock(return_value={"intent": "run", "pipeline": "workbuddy", "list_number": ""})
    options = {"APPROVAL_WRITE_MODE": "disabled", "DINGTALK_TRIAL_MODE": "true"}
    audit = Mock()
    service = DingTalkCommandService(job_manager=manager, status_provider=lambda: status,
        run_options_builder=lambda number, pipeline: {**options, "TARGET_LIST_NUMBERS": number},
        settings_loader=lambda: settings, intent_resolver=resolver, clock=lambda: now[0], audit=audit)
    return SimpleNamespace(service=service, settings=settings, manager=manager, resolver=resolver,
                           now=now, options=options, status=status, audit=audit)


KEY = ("group-a", "user-a")


def proposal(h):
    reply = h.service.handle_text("请帮我处理所有待办", session_key=KEY)
    assert "尚未启动" in reply
    h.manager.start.assert_not_called()
    return re.search(r"确认 ([A-F0-9]{6})", reply).group(1)


def test_natural_run_requires_one_time_confirmation(harness):
    h = harness
    code = proposal(h)
    assert "已启动" in h.service.handle_text(f"确认 {code}", session_key=KEY)
    assert h.manager.start.call_args.args[0] == "workbuddy_loop"
    assert "不存在" in h.service.handle_text(f"确认 {code}", session_key=KEY)
    assert h.manager.start.call_count == 1


@pytest.mark.parametrize("key", [("group-a", "user-b"), ("group-b", "user-a"), ("", "")])
def test_confirmation_cannot_cross_user_or_group(harness, key):
    h = harness
    code = proposal(h)
    h.service.handle_text(f"确认 {code}", session_key=key)
    h.manager.start.assert_not_called()
    assert "已启动" in h.service.handle_text(f"确认 {code}", session_key=KEY)


@pytest.mark.parametrize("change", ["expired", "config", "running", "disabled", "cancel"])
def test_confirmation_revalidates_boundaries(harness, change):
    h = harness
    code = proposal(h)
    if change == "expired":
        h.now[0] += 121
    elif change == "config":
        h.options["APPROVAL_WRITE_MODE"] = "generate_library"
    elif change == "running":
        h.status["running"] = True
    elif change == "disabled":
        h.settings["dingtalk_chat"]["enabled"] = False
    else:
        h.service.handle_text("取消", session_key=KEY)
    h.service.handle_text(f"确认 {code}", session_key=KEY)
    h.manager.start.assert_not_called()


def test_concurrent_confirmation_only_starts_once(harness):
    h = harness
    code = proposal(h)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: h.service.handle_text(f"确认 {code}", session_key=KEY), range(2)))
    assert h.manager.start.call_count == 1
    assert sum("已启动" in reply for reply in replies) == 1


def test_duplicate_callback_never_restarts_finished_task(harness):
    h = harness
    data = {"conversationId": "group-a", "senderStaffId": "user-a", "msgId": "m1", "text": {"content": "执行审批"}}
    first = h.service.handle_message(data)
    assert first == h.service.handle_message(data)
    assert h.manager.start.call_count == 1
    h.resolver.assert_not_called()


def test_exact_and_invalid_commands_do_not_depend_on_llm(harness):
    h = harness
    h.service.handle_text("执行审批V1 SJ123", session_key=KEY)
    assert h.manager.start.call_args.args[0] == "suggestions"
    assert "格式有误" in h.service.handle_text("执行审批 SJ123 附加参数", session_key=KEY)
    h.service.handle_text("状态", session_key=KEY)
    h.resolver.assert_not_called()


@pytest.mark.parametrize("result", [{"intent": "shell", "reply": "rm"},
    {"intent": "run", "pipeline": "v3"}, {"intent": "run", "pipeline": "v1", "list_number": "SJ999"}])
def test_model_cannot_invent_actions_or_list_numbers(harness, result):
    h = harness
    h.resolver.return_value = result
    reply = h.service.handle_text("帮我处理 SJ123", session_key=KEY)
    assert "确认 " not in reply
    h.manager.start.assert_not_called()


def test_missing_identity_and_secret_input_never_reach_model(harness, monkeypatch):
    h = harness
    h.service.handle_text("你好")
    h.service.handle_text("API Key=secret-value-12345", session_key=KEY)
    monkeypatch.setenv("DINGTALK_CHAT_API_KEY", "bare-secret-value")
    h.service.handle_text("bare-secret-value", session_key=KEY)
    h.resolver.assert_not_called()


def test_history_isolated_bounded_and_clearable(harness):
    h = harness
    h.resolver.return_value = {"intent": "chat", "reply": "你好"}
    h.service.handle_text("第一条", session_key=KEY)
    h.service.handle_text("第二条", session_key=KEY)
    assert h.resolver.call_args.args[2][0]["content"] == "第一条"
    h.service.handle_text("第三条", session_key=("group-a", "user-b"))
    assert h.resolver.call_args.args[2] == []
    h.service.handle_text("清空对话", session_key=KEY)
    h.service.handle_text("重新开始", session_key=KEY)
    assert h.resolver.call_args.args[2] == []


def test_model_error_does_not_block_precise_commands(harness):
    h = harness
    h.resolver.side_effect = ChatUnavailable("服务不可用")
    assert "不可用" in h.service.handle_text("你好", session_key=KEY)
    assert "已启动" in h.service.handle_text("执行审批", session_key=KEY)
    assert not h.service.conversations.sessions[KEY].busy


def test_cancel_inflight_model_does_not_create_pending_action(harness):
    h = harness
    entered, resume = threading.Event(), threading.Event()
    def delayed(*args):
        entered.set()
        assert resume.wait(5)
        return {"intent": "run", "pipeline": "workbuddy", "list_number": ""}
    h.resolver.side_effect = delayed
    with ThreadPoolExecutor() as pool:
        future = pool.submit(h.service.handle_text, "帮我处理所有待办", session_key=KEY)
        assert entered.wait(5)
        h.service.handle_text("取消", session_key=KEY)
        resume.set()
        assert "已取消" in future.result(timeout=5)
    assert not h.service._pending
    h.manager.start.assert_not_called()


def test_rate_limit_does_not_apply_to_status(harness):
    h = harness
    h.settings["dingtalk_chat"]["requests_per_minute"] = 1
    h.resolver.return_value = {"intent": "chat", "reply": "你好"}
    h.service.handle_text("你好", session_key=KEY)
    assert "频繁" in h.service.handle_text("再问一个", session_key=KEY)
    assert "当前任务状态" in h.service.handle_text("状态", session_key=KEY)
    assert h.resolver.call_count == 1


def test_model_only_receives_allowlisted_status(monkeypatch):
    import openai
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"intent": "status"})))])
    constructor = Mock(return_value=client)
    monkeypatch.setattr(openai, "OpenAI", constructor)
    monkeypatch.setenv("DINGTALK_CHAT_API_KEY", "fake-key")
    settings = {"dingtalk_chat": {"use_main_llm": False, "provider": "openai_compatible", "base_url": "https://example.test/v1", "model": "test"}}
    assert infer_intent(settings, "查下状态", [], {"running": True, "error": "password=secret", "log_tail": ["credentials"]}) == {"intent": "status"}
    request = client.chat.completions.create.call_args.kwargs
    assert "secret" not in json.dumps(request)
    assert "credentials" not in json.dumps(request)
    assert constructor.call_args.kwargs["max_retries"] == 0


def test_config_does_not_use_another_provider_key(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "other-provider-secret")
    assert chat_config({"llm": {"provider": "deepseek"}})["api_key"] == ""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "selected-provider-secret")
    assert chat_config({"llm": {"provider": "deepseek"}})["api_key"] == "selected-provider-secret"


def test_session_storage_is_bounded_and_expires():
    now = [100.0]
    store = ConversationStore(clock=lambda: now[0])
    config = chat_config({})
    config["global_requests_per_minute"] = 120
    for index in range(140):
        now[0] += 1
        key = ("g", str(index))
        _, generation = store.begin(key, config)
        store.finish(key, "hi", "reply", remember=True, generation=generation)
    assert len(store.sessions) == 128
    now[0] += 1801
    _, generation = store.begin(KEY, config)
    store.finish(KEY, "hi", "reply", remember=False, generation=generation)
    assert list(store.sessions) == [KEY]


def test_chat_settings_persist_without_exposing_or_erasing_key(tmp_path, monkeypatch):
    import web_runner
    settings = {}
    updates = []
    monkeypatch.setattr(web_runner, "load_settings", lambda: settings)
    monkeypatch.setattr(web_runner, "save_settings", lambda value: None)
    monkeypatch.setattr(web_runner, "runtime_config_snapshot", lambda: {})
    monkeypatch.setattr(web_runner, "update_env_file", lambda path, values: updates.append(values))
    form = {"dingtalk_chat_enabled": "true", "dingtalk_chat_use_main_llm": "false", "dingtalk_chat_api_key": "test-new-key",
            "dingtalk_chat_base_url": "https://example.test/v1", "dingtalk_chat_model": "test-model", "dingtalk_chat_timeout_seconds": "999"}
    web_runner.save_runtime_config(form)
    assert settings["dingtalk_chat"]["enabled"] is True
    assert settings["dingtalk_chat"]["use_main_llm"] is False
    assert settings["dingtalk_chat"]["timeout_seconds"] == 60
    assert "test-new-key" not in json.dumps(settings)
    assert updates[-1]["DINGTALK_CHAT_API_KEY"] == "test-new-key"
    form["dingtalk_chat_api_key"] = ""
    web_runner.save_runtime_config(form)
    assert "DINGTALK_CHAT_API_KEY" not in updates[-1]


def test_settings_route_forwards_chat_fields(monkeypatch):
    from fastapi.testclient import TestClient
    import web_app
    save = Mock(return_value={})
    monkeypatch.setattr(web_app.manager, "status", lambda **kw: {"running": False})
    monkeypatch.setattr(web_app, "save_runtime_config", save)
    monkeypatch.setattr(web_app.scheduler, "reload", lambda: None)
    monkeypatch.setattr(web_app.scheduler, "status", lambda: {})
    monkeypatch.setattr(web_app.dingtalk_stream_bot, "reload", lambda: None)
    monkeypatch.setattr(web_app.dingtalk_stream_bot, "status", lambda: {})
    monkeypatch.setattr(web_app.scheduler, "start", lambda: None)
    monkeypatch.setattr(web_app.scheduler, "stop", lambda: None)
    monkeypatch.setattr(web_app.dingtalk_stream_bot, "start", lambda: None)
    monkeypatch.setattr(web_app.dingtalk_stream_bot, "stop", lambda: None)
    data = {"dingtalk_chat_enabled": "true", "dingtalk_chat_use_main_llm": "true", "dingtalk_chat_model": "test-model",
            "dingtalk_chat_timeout_seconds": "15", "dingtalk_chat_api_key": "test-key"}
    with TestClient(web_app.app, base_url="http://localhost:8000") as client:
        response = client.post("/api/settings", data=data, headers={"Origin": "http://localhost:8000", "x-reagent-csrf": web_app.security.csrf_token})
    assert response.status_code == 200
    assert all(save.call_args.args[0][key] == value for key, value in data.items())


def test_bot_status_never_exposes_model_key(harness):
    h = harness
    bot = DingTalkStreamBot(settings_loader=lambda: h.settings, job_manager=h.manager,
        run_options_builder=lambda number, pipeline: {}, status_provider=lambda: {})
    status = bot.status()
    assert status["chat_enabled"] is True
    assert status["chat_configured"] is True
    assert "test-only-api-key" not in json.dumps(status)
