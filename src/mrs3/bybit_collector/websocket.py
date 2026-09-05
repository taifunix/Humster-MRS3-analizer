"""Small protocol helpers for one Bybit public linear WebSocket."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
import time
import threading
from typing import Any


MAX_SUBSCRIBE_CHARS = 21_000
ORDERBOOK_DEPTH = 1000


def build_subscribe_messages(symbols: Iterable[str]) -> tuple[str, ...]:
    """Build acknowledged subscription payloads, splitting only by Bybit's cap."""

    values = tuple(symbols)
    if not values or any(not isinstance(symbol, str) or not symbol for symbol in values):
        raise ValueError("symbols must contain non-empty strings")
    if len(values) != len(set(values)):
        raise ValueError("symbols must not contain duplicates")
    messages: list[str] = []
    batch: list[str] = []
    for symbol in values:
        candidate = _subscribe_payload(batch + [symbol])
        if batch and len(candidate) > MAX_SUBSCRIBE_CHARS:
            messages.append(_subscribe_payload(batch))
            batch = [symbol]
        elif len(candidate) > MAX_SUBSCRIBE_CHARS:
            raise ValueError(f"subscription for {symbol!r} exceeds protocol limit")
        else:
            batch.append(symbol)
    if batch:
        messages.append(_subscribe_payload(batch))
    return tuple(messages)


def heartbeat_message() -> str:
    return json.dumps({"op": "ping"}, separators=(",", ":"))


class BybitWebSocketSession:
    """Transport-neutral one-connection loop; the application supplies a WS transport."""

    def __init__(
        self,
        symbols: Iterable[str],
        transport_factory: Any,
        *,
        on_connect: Any = None,
        on_disconnect: Any = None,
    ) -> None:
        self.symbols = tuple(symbols)
        self.transport_factory = transport_factory
        self._messages = build_subscribe_messages(self.symbols)
        self._lock = threading.RLock()
        self._reconnect_requested = False
        self._session_established = False
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

    def run_once(
        self,
        on_message: Any,
        *,
        stop: Any = None,
        on_connect: Any = None,
        on_disconnect: Any = None,
    ) -> None:
        on_connect = self.on_connect if on_connect is None else on_connect
        on_disconnect = self.on_disconnect if on_disconnect is None else on_disconnect
        transport = None
        try:
            transport = self.transport_factory()
            if on_connect is not None:
                # Bybit may deliver the initial snapshot before its subscribe
                # acknowledgement. Mark the transport live first so the
                # runtime does not invalidate that snapshot after applying it.
                on_connect()
            with self._lock:
                messages = self._messages
                self._reconnect_requested = False
            for payload in messages:
                transport.send(payload)
                deadline = time.monotonic() + 10
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Bybit subscription acknowledgement timed out")
                    raw = transport.recv(timeout=min(1, max(0.1, deadline - time.monotonic())))
                    if not raw:
                        continue
                    ack = json.loads(raw)
                    if isinstance(ack, Mapping) and ack.get("op") == "ping":
                        transport.send(json.dumps({"op": "pong"}, separators=(",", ":")))
                        continue
                    if isinstance(ack, Mapping) and ack.get("op") == "subscribe":
                        if ack.get("success") is not True:
                            raise RuntimeError("Bybit subscription was not acknowledged")
                        break
                    if isinstance(ack, Mapping) and str(ack.get("topic", "")).startswith("orderbook."):
                        on_message(decode_orderbook_frame(ack))
            self._session_established = True
            last_ping = time.monotonic()
            while not _stopped(stop):
                payload = transport.recv(timeout=1)
                if payload:
                    try:
                        parsed = json.loads(payload)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed = None
                    if isinstance(parsed, Mapping) and parsed.get("op") == "ping":
                        transport.send(json.dumps({"op": "pong"}, separators=(",", ":")))
                    elif not _is_control_frame(parsed) and isinstance(parsed, Mapping) and str(parsed.get("topic", "")).startswith("orderbook."):
                        try:
                            on_message(decode_orderbook_frame(parsed))
                        except (TypeError, ValueError):
                            pass
                with self._lock:
                    if self._reconnect_requested:
                        raise ConnectionError("subscription set changed")
                if time.monotonic() - last_ping >= 20:
                    transport.send(heartbeat_message())
                    last_ping = time.monotonic()
        finally:
            if on_disconnect is not None:
                on_disconnect()
            close = getattr(transport, "close", None)
            if close is not None:
                close()

    def run_forever(
        self,
        on_message: Any,
        *,
        stop: Any = None,
        retry_seconds: float = 1.0,
        on_connect: Any = None,
        on_disconnect: Any = None,
    ) -> None:
        backoff = retry_seconds
        while not _stopped(stop):
            try:
                self.run_once(
                    on_message,
                    stop=stop,
                    on_connect=on_connect,
                    on_disconnect=on_disconnect,
                )
            except Exception:
                if _stopped(stop):
                    return
                if self._session_established:
                    backoff = retry_seconds
                    self._session_established = False
                if self._reconnect_requested:
                    backoff = retry_seconds
                    continue
                time.sleep(backoff)
                backoff = min(60.0, max(retry_seconds, backoff * 2 or 0.1))

    def update_symbols(self, symbols: Iterable[str]) -> None:
        values = tuple(symbols)
        messages = build_subscribe_messages(values)
        with self._lock:
            self.symbols = values
            self._messages = messages

    def request_reconnect(self) -> None:
        with self._lock:
            self._reconnect_requested = True


def decode_orderbook_frame(payload: str | bytes | bytearray | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        frame = payload
    else:
        try:
            frame = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("WebSocket frame is not valid JSON") from exc
    if not isinstance(frame, Mapping):
        raise ValueError("WebSocket frame must be an object")
    topic = frame.get("topic")
    kind = frame.get("type")
    data = frame.get("data")
    if (
        not isinstance(topic, str)
        or not topic.startswith("orderbook.")
        or kind not in {"snapshot", "delta"}
        or not isinstance(data, Mapping)
    ):
        raise ValueError("frame is not an orderbook update")
    parts = topic.split(".")
    if len(parts) != 3 or parts[1] != str(ORDERBOOK_DEPTH):
        raise ValueError("unsupported orderbook depth")
    symbol = data.get("s")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("orderbook frame has no symbol")
    if "u" not in data:
        raise ValueError("orderbook frame has no update id")
    return frame


def _subscribe_payload(symbols: list[str]) -> str:
    return json.dumps(
        {"op": "subscribe", "args": [f"orderbook.{ORDERBOOK_DEPTH}.{symbol}" for symbol in symbols]},
        separators=(",", ":"),
    )


def _is_control_frame(frame: Any) -> bool:
    if not isinstance(frame, Mapping):
        return False
    return (
        frame.get("op") in {"ping", "pong", "subscribe"}
        or frame.get("ret_msg") == "pong"
        or ("success" in frame and "topic" not in frame)
    )


def _stopped(stop: Any) -> bool:
    return bool(stop()) if callable(stop) else bool(stop)


__all__ = [
    "MAX_SUBSCRIBE_CHARS",
    "ORDERBOOK_DEPTH",
    "build_subscribe_messages",
    "decode_orderbook_frame",
    "heartbeat_message",
    "BybitWebSocketSession",
]
