import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import dingtalk_notifier as notifier


SETTINGS = {"notification": {"dingtalk": {"enabled": True, "at_all": True}},
            "dingtalk_stream": {"enabled": True, "open_conversation_id": "wrong-global-group"}}
TARGET = {"sessionWebhook": "https://example.test/session", "openConversationId": "fixed-group", "robotCode": "robot"}
MESSAGE = notifier.build_task_result_message(action="workbuddy_loop", success=True)


def accepted(body):
    return {"sent": True, "status": 200, "body": json.dumps(body)}


@pytest.mark.parametrize("body", [{"errcode": 310000, "errmsg": "secret-query-token"}, {"code": "AccessDenied"}, {"success": False}])
def test_http_200_business_failure_is_not_success(body):
    result = notifier.classify_response(200, json.dumps(body))
    assert not result["sent"]
    assert "secret-query-token" not in str(result)


def test_expired_session_uses_fixed_group_and_preserves_markdown(monkeypatch, tmp_path):
    monkeypatch.setattr(notifier, "get_dingtalk_access_token", lambda *args: "fake-access-token")
    post = Mock(return_value=accepted({"processQueryKey": "receipt"}))
    monkeypatch.setattr(notifier, "post_json", post)
    target = {**TARGET, "sessionWebhookExpiredTime": 1}
    result = notifier.send_task_result_notification(SETTINGS, MESSAGE, root_dir=tmp_path, binding=target)
    assert result["sent"]
    assert post.call_count == 1
    payload = post.call_args.args[1]
    assert payload["openConversationId"] == "fixed-group"
    assert payload["msgKey"] == "sampleMarkdown"
    assert json.loads(payload["msgParam"]) == {"title": MESSAGE.title, "text": MESSAGE.markdown}
    assert result["at_all_supported"] is False
    assert result["warnings"]


def test_explicit_session_rejection_can_fall_back_to_same_group(monkeypatch):
    monkeypatch.setattr(notifier, "get_dingtalk_access_token", lambda *args: "fake-token")
    post = Mock(side_effect=[notifier.classify_response(200, '{"errcode":310000}'), accepted({"processQueryKey": "receipt"})])
    monkeypatch.setattr(notifier, "post_json", post)
    result = notifier.send_task_result_notification(SETTINGS, MESSAGE, binding=TARGET)
    assert result["sent"]
    assert post.call_args_list[1].args[1]["openConversationId"] == "fixed-group"


@pytest.mark.parametrize("result", [{"sent": False, "delivery_unknown": True}, accepted({}), notifier.classify_response(503, '{"code":"ServiceUnavailable"}')])
def test_ambiguous_delivery_is_not_retried_or_sent_via_another_channel(monkeypatch, result):
    post = Mock(return_value=result)
    token = Mock()
    monkeypatch.setattr(notifier, "post_json", post)
    monkeypatch.setattr(notifier, "get_dingtalk_access_token", token)
    result = notifier.send_task_result_notification(SETTINGS, MESSAGE, binding=TARGET)
    assert not result["sent"]
    assert result["delivery_unknown"]
    assert post.call_count == 1
    token.assert_not_called()


def test_rate_limit_has_three_attempts_and_bounded_backoff(monkeypatch):
    limited = notifier.classify_response(429, '{"code":"Throttling"}')
    post = Mock(side_effect=[limited, limited, accepted({"errcode": 0})])
    sleep = Mock()
    monkeypatch.setattr(notifier, "post_json", post)
    monkeypatch.setattr(notifier.time, "sleep", sleep)
    result = notifier.send_task_result_notification(SETTINGS, MESSAGE, binding=TARGET)
    assert result["sent"]
    assert result["attempts"] == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    assert post.call_args.args[1]["at"]["isAtAll"] is True


def test_exhausted_rate_limit_does_not_switch_channels(monkeypatch):
    post = Mock(return_value=notifier.classify_response(429, '{"code":"Throttling"}'))
    monkeypatch.setattr(notifier, "post_json", post)
    monkeypatch.setattr(notifier.time, "sleep", lambda seconds: None)
    token = Mock()
    monkeypatch.setattr(notifier, "get_dingtalk_access_token", token)
    result = notifier.send_task_result_notification(SETTINGS, MESSAGE, binding=TARGET)
    assert not result["sent"]
    assert post.call_count == 3
    token.assert_not_called()


def test_token_uses_official_app_credentials_without_corp_id(monkeypatch):
    monkeypatch.setenv("TEST_DD_ID", "fake-app-id")
    monkeypatch.setenv("TEST_DD_SECRET", "fake-app-secret")
    post = Mock(return_value=accepted({"accessToken": "fake-token", "expireIn": 7200}))
    monkeypatch.setattr(notifier, "post_json", post)
    assert notifier.get_dingtalk_access_token({"client_id_env": "TEST_DD_ID", "client_secret_env": "TEST_DD_SECRET"}, 8) == "fake-token"
    assert post.call_args.args[:2] == ("https://api.dingtalk.com/v1.0/oauth2/accessToken", {"appKey": "fake-app-id", "appSecret": "fake-app-secret"})


def test_transport_errors_do_not_echo_secret_urls(monkeypatch):
    monkeypatch.setattr(notifier.urllib.request, "urlopen", Mock(side_effect=TimeoutError("secret-url-ticket")))
    result = notifier.post_json("https://example.test/?ticket=secret", {}, 1)
    assert not result["sent"]
    assert result["delivery_unknown"]
    assert "secret-url-ticket" not in str(result)


def test_http_error_response_checks_business_code(monkeypatch):
    error = urllib.error.HTTPError("https://example.test/?secret=hidden", 403, "forbidden", {}, io.BytesIO(b'{"code":"AccessDenied","message":"secret"}'))
    monkeypatch.setattr(notifier.urllib.request, "urlopen", Mock(side_effect=error))
    result = notifier.post_json("https://example.test", {}, 1)
    assert not result["sent"]
    assert not result["retryable"]
    assert "hidden" not in str(result)
