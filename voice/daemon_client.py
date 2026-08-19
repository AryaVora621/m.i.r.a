"""Real WebSocket client to the Rust mira-daemon. Owns a background asyncio event loop thread so
VoiceLoop (synchronous by design — see Task 12) can call it with plain blocking method calls."""

import asyncio
import json
import threading
import time
import uuid
from typing import Optional

import websockets


class DaemonClient:
    def __init__(self, node_id: str, uri: str = "ws://127.0.0.1:8080/ws"):
        self.node_id = node_id
        self.uri = uri
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._ws = None
        self._connected = threading.Event()
        self._pending: dict[str, "asyncio.Future"] = {}
        self._thread.start()

    def connect(self, timeout_s: float = 5.0) -> None:
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        future.result(timeout=timeout_s)

    async def _connect(self) -> None:
        self._ws = await websockets.connect(self.uri)
        register = self._envelope("REGISTER", "system", {"topics": ["voice", "capability"]})
        await self._ws.send(json.dumps(register))
        await self._ws.recv()  # REGISTER_ACK
        self._connected.set()
        asyncio.ensure_future(self._read_loop(), loop=self._loop)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                envelope = json.loads(message)
                if envelope.get("event_kind") == "CAPABILITY_RECEIPT":
                    ack_ref = envelope.get("ack_ref")
                    future = self._pending.pop(ack_ref, None)
                    if future is not None and not future.done():
                        future.set_result(envelope["payload"])
        except websockets.ConnectionClosed:
            self._connected.clear()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def close(self) -> None:
        if self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop).result(timeout=2.0)
        self._loop.call_soon_threadsafe(self._loop.stop)

    def publish_state(self, state: str, transcript: Optional[str] = None, is_final: Optional[bool] = None) -> None:
        envelope = self._envelope(
            "EVENT", "voice",
            {"node_id": self.node_id, "timestamp": time.time(), "state": state, "transcript": transcript, "is_final": is_final},
        )
        asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(envelope)), self._loop).result(timeout=2.0)

    def request_capability(self, capability: str, arguments: dict, timeout_s: float = 5.0) -> dict:
        event_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        envelope = self._envelope(
            "CAPABILITY_REQUEST", "capability",
            {
                "request_id": request_id,
                "trace_id": trace_id,
                "sender_node": self.node_id,
                "capability": capability,
                "arguments": arguments,
                "reversibility": "REVERSIBLE",
                "requested_at": time.time(),
            },
            event_id=event_id,
            trace_id=trace_id,
        )

        future: "asyncio.Future" = self._loop.create_future()

        def register_pending():
            self._pending[event_id] = future

        self._loop.call_soon_threadsafe(register_pending)
        asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(envelope)), self._loop).result(timeout=2.0)

        return asyncio.run_coroutine_threadsafe(asyncio.wait_for(future, timeout_s), self._loop).result(timeout=timeout_s + 1)

    def _envelope(self, event_kind: str, topic: str, payload: dict, event_id: Optional[str] = None, trace_id: Optional[str] = None) -> dict:
        return {
            "protocol_version": 2,
            "event_id": event_id or str(uuid.uuid4()),
            "trace_id": trace_id or str(uuid.uuid4()),
            "topic": topic,
            "event_kind": event_kind,
            "sender_node": self.node_id,
            "timestamp": time.time(),
            "auth_context": {"node_id": self.node_id, "auth_mode": "LOOPBACK_TRUSTED", "token": None, "cert_fingerprint": None},
            "payload": payload,
            "ack_ref": None,
        }
