from contextlib import asynccontextmanager

import pytest


@pytest.fixture(autouse=True)
def isolate_web_background_connections(monkeypatch):
    # HTTP tests must not start the operator's real scheduler or DingTalk client.
    import web_app
    # Unit tests must explicitly install fake credentials when testing clients.
    # A developer's .env must never turn a fixture into a paid model request.
    for name in ("LLM_API_KEY", "SILICONFLOW_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY",
                 "QIANFAN_API_KEY", "ARK_API_KEY", "MOONSHOT_API_KEY", "DINGTALK_CHAT_API_KEY"):
        monkeypatch.setenv(name, "")
    @asynccontextmanager
    async def no_background_services(app):
        yield
    monkeypatch.setattr(web_app.app.router, "lifespan_context", no_background_services)
