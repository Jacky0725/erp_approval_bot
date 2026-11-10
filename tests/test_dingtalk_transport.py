import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dingtalk_transport import StreamConnection
from dingtalk_stream_bot import DingTalkStreamBot


class Socket:
    def __init__(self, frames=()):
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if frame is None:
            raise StopAsyncIteration
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def close(self):
        self.closed = True
        self.frames.put_nowait(None)


class Context:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *args):
        await self.socket.close()


def client():
    return SimpleNamespace(credential=SimpleNamespace(client_id="fake-app", client_secret="fake-secret"),
                           callback_handler_map={"topic": object()}, pre_start=Mock(), websocket=None, TAG_DISCONNECT="disconnect")


def negotiation(*args):
    return {"sent": True, "body": json.dumps({"endpoint": "wss://gateway.dingtalk.com/connect", "ticket": "sensitive-ticket"})}


def test_disconnect_reconnects_and_shutdown_closes_connection():
    async def scenario():
        sdk = client()
        first = Socket([json.dumps({"type": "CALLBACK"}), OSError("offline-sensitive-url")])
        second = Socket()
        sockets = iter([first, second])
        routes = []
        async def route(message):
            routes.append(message)
        sdk.route_message = route
        events = []
        ready = asyncio.Event()
        def observe(state, **kw):
            events.append((state, kw))
            if state == "connected" and sum(s == "connected" for s, _ in events) == 2:
                ready.set()
        connection = StreamConnection(sdk, observe, connector=lambda *args, **kw: Context(next(sockets)), negotiate=negotiation, base_delay=0)
        running = asyncio.create_task(connection.run())
        await asyncio.wait_for(ready.wait(), 2)
        await connection.stop()
        await asyncio.wait_for(running, 2)
        assert second.closed
        assert sdk.websocket is None
        assert any(state == "reconnecting" for state, _ in events)
        assert "offline-sensitive-url" not in str(events)
    asyncio.run(scenario())


def test_stop_interrupts_reconnect_backoff():
    async def scenario():
        waiting = asyncio.Event()
        def observe(state, **kw):
            if state == "reconnecting":
                waiting.set()
        negotiate = Mock(return_value={"sent": False})
        connection = StreamConnection(client(), observe, connector=Mock(), negotiate=negotiate, base_delay=30)
        running = asyncio.create_task(connection.run())
        await asyncio.wait_for(waiting.wait(), 2)
        await connection.stop()
        await asyncio.wait_for(running, 2)
        assert negotiate.call_count == 1
    asyncio.run(scenario())


def test_shutdown_cancels_inflight_callbacks():
    async def scenario():
        sdk = client()
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def route(message):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        sdk.route_message = route
        socket = Socket(['{"type":"CALLBACK"}'])
        connection = StreamConnection(sdk, lambda *args, **kw: None, connector=lambda *args, **kw: Context(socket), negotiate=negotiation)
        running = asyncio.create_task(connection.run())
        await asyncio.wait_for(entered.wait(), 2)
        await connection.stop()
        await asyncio.wait_for(running, 2)
        assert cancelled.is_set()
        assert not connection.tasks
    asyncio.run(scenario())


def test_invalid_json_does_not_kill_connection():
    async def scenario():
        sdk = client()
        received = asyncio.Event()
        async def route(message):
            received.set()
        sdk.route_message = route
        socket = Socket(["not-json", '{"type":"CALLBACK"}'])
        events = []
        connection = StreamConnection(sdk, lambda state, **kw: events.append(state), connector=lambda *args, **kw: Context(socket), negotiate=negotiation)
        running = asyncio.create_task(connection.run())
        await asyncio.wait_for(received.wait(), 2)
        await connection.stop()
        await asyncio.wait_for(running, 2)
        assert "message_error" in events
    asyncio.run(scenario())


def test_reload_waits_for_old_connection_and_uses_new_credentials(monkeypatch):
    import dingtalk_stream
    import dingtalk_stream_bot
    active = [0]
    peak = [0]
    credentials = []
    connected = threading.Event()
    class Client:
        def __init__(self, credential, **kw):
            self.credential = credential
        def register_callback_handler(self, *args):
            pass
    class Connection:
        def __init__(self, sdk, observe):
            self.sdk, self.observe = sdk, observe
            self.stopped = asyncio.Event()
        async def run(self):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            credentials.append(self.sdk.credential.client_id)
            self.observe("connected")
            connected.set()
            try:
                await self.stopped.wait()
            finally:
                active[0] -= 1
                self.observe("stopped")
        async def stop(self):
            self.stopped.set()
    monkeypatch.setattr(dingtalk_stream, "DingTalkStreamClient", Client)
    monkeypatch.setattr(dingtalk_stream_bot, "StreamConnection", Connection)
    monkeypatch.setenv("TEST_STREAM_ID", "first")
    monkeypatch.setenv("TEST_STREAM_SECRET", "fake-secret")
    settings = {"dingtalk_stream": {"enabled": True, "client_id_env": "TEST_STREAM_ID", "client_secret_env": "TEST_STREAM_SECRET"}}
    bot = DingTalkStreamBot(settings_loader=lambda: settings, job_manager=Mock(), run_options_builder=Mock(), status_provider=lambda: {})
    try:
        bot.start()
        assert connected.wait(2)
        assert bot.status()["connected"]
        connected.clear()
        monkeypatch.setenv("TEST_STREAM_ID", "second")
        bot.reload()
        assert connected.wait(2)
        assert credentials == ["first", "second"]
        assert peak[0] == 1
    finally:
        assert bot.stop()
    assert not bot.status()["connected"]
    assert not bot.status()["running"]


def test_status_distinguishes_thread_alive_from_connected():
    bot = DingTalkStreamBot(settings_loader=lambda: {}, job_manager=Mock(), run_options_builder=Mock(), status_provider=lambda: {})
    bot._thread = SimpleNamespace(is_alive=lambda: True)
    bot._observe_connection("connecting")
    assert bot.status()["running"]
    assert not bot.status()["connected"]
    bot._observe_connection("connected")
    assert bot.status()["last_connected_at"]
    bot._observe_connection("received")
    assert bot.status()["last_received_at"]
    bot._observe_connection("reconnecting")
    assert not bot.status()["connected"]
    assert bot.status()["reconnect_count"] == 1


def test_real_sdk_parses_callback_and_acks_original_delivery():
    import dingtalk_stream
    async def scenario():
        delivered = asyncio.Event()
        acknowledgements = []
        class AckSocket(Socket):
            async def send(self, payload):
                acknowledgements.append(json.loads(payload))
                delivered.set()
        seen = []
        class Handler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback):
                seen.append(callback.data["text"]["content"])
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"
        sdk = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential("fake-app", "fake-secret"))
        topic = dingtalk_stream.chatbot.ChatbotMessage.TOPIC
        sdk.register_callback_handler(topic, Handler())
        frame = {"type": "CALLBACK", "specVersion": "1.0", "headers": {"topic": topic, "messageId": "original-delivery"},
                 "data": json.dumps({"text": {"content": "状态"}})}
        socket = AckSocket([json.dumps(frame)])
        connection = StreamConnection(sdk, lambda *args, **kw: None, connector=lambda *args, **kw: Context(socket), negotiate=negotiation)
        running = asyncio.create_task(connection.run())
        await asyncio.wait_for(delivered.wait(), 2)
        await connection.stop()
        await asyncio.wait_for(running, 2)
        assert seen == ["状态"]
        assert acknowledgements[0]["headers"]["messageId"] == "original-delivery"
        assert acknowledgements[0]["code"] == 200
    asyncio.run(scenario())
