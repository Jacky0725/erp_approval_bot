from __future__ import annotations

import asyncio
import contextlib
import json
import random
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dingtalk_notifier import post_json, response_body


class StreamConnection:
    """Manage sockets and shutdown; frame parsing and ACKs remain in the SDK."""

    def __init__(self, client: Any, observe: Callable[..., None], *, connector: Any = None,
                 negotiate: Callable[..., dict[str, Any]] = post_json, base_delay: float = 1) -> None:
        if connector is None:
            from websockets.asyncio.client import connect
            connector = connect
        self.client = client
        self.observe = observe
        self.connector = connector
        self.negotiate = negotiate
        self.base_delay = base_delay
        self.stopping = asyncio.Event()
        self.websocket: Any = None
        self.tasks: set[asyncio.Task] = set()

    def open_connection(self) -> dict[str, Any]:
        payload = {"clientId": self.client.credential.client_id,
                   "clientSecret": self.client.credential.client_secret,
                   "subscriptions": [{"type": "CALLBACK", "topic": topic} for topic in self.client.callback_handler_map],
                   "ua": "reagent-approval-bot"}
        result = self.negotiate("https://api.dingtalk.com/v1.0/gateway/connections/open", payload, 8)
        connection = response_body(result)
        endpoint = urlparse(str(connection.get("endpoint") or ""))
        if not result.get("sent") or endpoint.scheme != "wss" or not (endpoint.hostname or "").endswith(".dingtalk.com") or not connection.get("ticket"):
            raise ConnectionError("Stream connection negotiation failed")
        return connection

    async def stop(self) -> None:
        self.stopping.set()
        self.observe("stopping")
        for task in list(self.tasks):
            task.cancel()
        if self.websocket is not None:
            await self.websocket.close()

    async def dispatch(self, message: dict[str, Any], websocket: Any) -> None:
        try:
            result = await self.client.route_message(message)
            if result == self.client.TAG_DISCONNECT:
                await websocket.close()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.observe("message_error", error=type(error).__name__)

    async def run(self) -> None:
        self.client.pre_start()
        failures = 0
        try:
            while not self.stopping.is_set():
                self.observe("connecting")
                try:
                    connection = await asyncio.to_thread(self.open_connection)
                    if self.stopping.is_set():
                        break
                    endpoint = urlparse(connection["endpoint"])
                    query = urlencode([*parse_qsl(endpoint.query), ("ticket", str(connection["ticket"]))])
                    uri = urlunparse(endpoint._replace(query=query))
                    async with self.connector(uri, open_timeout=8, close_timeout=3,
                                             ping_interval=20, ping_timeout=20, max_size=2 ** 20) as websocket:
                        self.websocket = self.client.websocket = websocket
                        self.observe("connected")
                        try:
                            if self.stopping.is_set():
                                await websocket.close()
                                break
                            async for raw in websocket:
                                if self.stopping.is_set():
                                    break
                                self.observe("received")
                                failures = 0
                                try:
                                    message = json.loads(raw)
                                    if not isinstance(message, dict):
                                        raise ValueError("not an object")
                                except (ValueError, TypeError):
                                    self.observe("message_error", error="InvalidFrame")
                                    continue
                                while len(self.tasks) >= 32 and not self.stopping.is_set():
                                    await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
                                if self.stopping.is_set():
                                    break
                                task = asyncio.create_task(self.dispatch(message, websocket))
                                self.tasks.add(task)
                                task.add_done_callback(self.tasks.discard)
                        finally:
                            pending = list(self.tasks)
                            for task in pending:
                                task.cancel()
                            if pending:
                                await asyncio.gather(*pending, return_exceptions=True)
                            self.tasks.clear()
                            self.websocket = self.client.websocket = None
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.observe("disconnected", error=type(error).__name__)
                if not self.stopping.is_set():
                    failures += 1
                    self.observe("reconnecting")
                    delay = min(30, self.base_delay * 2 ** min(failures - 1, 5)) + random.uniform(0, self.base_delay)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.stopping.wait(), timeout=delay)
        finally:
            self.observe("stopped")
