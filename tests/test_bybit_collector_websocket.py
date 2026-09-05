from __future__ import annotations

import json

import pytest

from mrs3.bybit_collector.websocket import (
    BybitWebSocketSession,
    MAX_SUBSCRIBE_CHARS,
    build_subscribe_messages,
    decode_orderbook_frame,
)


def test_subscription_has_no_artificial_ten_symbol_cap() -> None:
    messages = build_subscribe_messages([f"S{i}USDT" for i in range(30)])
    assert len(messages) == 1
    payload = json.loads(messages[0])
    assert len(payload["args"]) == 30
    assert payload["args"][0] == "orderbook.1000.S0USDT"
    assert len(messages[0]) < MAX_SUBSCRIBE_CHARS


def test_subscription_splits_only_when_protocol_character_cap_requires_it() -> None:
    symbols = [f"LONGSYMBOL{i:04d}USDT" for i in range(2_000)]
    messages = build_subscribe_messages(symbols)
    assert len(messages) > 1
    assert sum(len(json.loads(message)["args"]) for message in messages) == len(symbols)
    assert all(len(message) <= MAX_SUBSCRIBE_CHARS for message in messages)


def test_decode_orderbook_frame_rejects_non_orderbook_or_bad_json() -> None:
    with pytest.raises(ValueError):
        decode_orderbook_frame("not json")
    with pytest.raises(ValueError):
        decode_orderbook_frame(json.dumps({"op": "subscribe", "success": True}))


def test_decode_orderbook_frame_returns_symbol_and_message() -> None:
    message = {
        "topic": "orderbook.1000.BTCUSDT",
        "type": "snapshot",
        "data": {"s": "BTCUSDT", "u": 1, "b": [["100", "2"]], "a": [["101", "3"]]},
    }
    assert decode_orderbook_frame(json.dumps(message)) == message


def test_session_ignores_control_frames_and_reports_lifecycle() -> None:
    class Transport:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.frames = [
                json.dumps({"op": "subscribe", "success": True}),
                json.dumps({"op": "ping", "ret_msg": "pong"}),
                json.dumps({"topic": "tickers.BTCUSDT", "data": {}}),
                json.dumps({"topic": "orderbook.1000.BTCUSDT", "type": "snapshot", "data": {"s": "BTCUSDT", "u": 1, "b": [["100", "1"]], "a": [["101", "1"]]}}),
            ]

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float = 1) -> str | None:
            return self.frames.pop(0) if self.frames else None

        def close(self) -> None:
            pass

    transport = Transport()
    stop = False
    received: list[dict] = []
    lifecycle: list[str] = []

    def on_message(message: dict) -> None:
        nonlocal stop
        received.append(message)
        stop = True

    session = BybitWebSocketSession(["BTCUSDT"], lambda: transport)
    session.run_once(on_message, stop=lambda: stop, on_connect=lambda: lifecycle.append("up"), on_disconnect=lambda: lifecycle.append("down"))
    assert len(received) == 1
    assert lifecycle == ["up", "down"]


def test_subscription_uses_supported_linear_orderbook_1000_topic() -> None:
    payload = json.loads(build_subscribe_messages(["BTCUSDT"])[0])

    assert payload["args"] == ["orderbook.1000.BTCUSDT"]


def test_session_handles_ping_pong_and_interleaved_data_before_subscribe_ack() -> None:
    incoming = iter(
        [
            json.dumps({"op": "ping"}),
            json.dumps(
                {
                    "topic": "orderbook.1000.BTCUSDT",
                    "type": "snapshot",
                    "data": {"s": "BTCUSDT", "u": 1, "b": [["100", "2"]], "a": [["101", "3"]]},
                }
            ),
            json.dumps({"op": "subscribe", "success": True}),
        ]
    )

    class Transport:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, payload: str) -> None:
            self.sent.append(payload)

        def recv(self, timeout: float = 1) -> str | None:
            return next(incoming)

        def close(self) -> None:
            pass

    transport = Transport()
    seen: list[object] = []
    connected = False

    def on_connect() -> None:
        nonlocal connected
        connected = True

    BybitWebSocketSession(
        ["BTCUSDT"], lambda: transport, on_connect=on_connect
    ).run_once(seen.append, stop=lambda: connected)

    assert len(seen) == 1
    assert json.loads(transport.sent[0])["args"] == ["orderbook.1000.BTCUSDT"]
    assert json.loads(transport.sent[1]) == {"op": "pong"}


def test_session_reconnects_and_calls_lifecycle_callbacks() -> None:
    calls: list[str] = []
    attempts = 0

    class Transport:
        def __init__(self, first: bool) -> None:
            self.first = first
            self.ack = False

        def send(self, _payload: str) -> None:
            pass

        def recv(self, timeout: float = 1) -> str | None:
            if not self.ack:
                self.ack = True
                return json.dumps({"op": "subscribe", "success": True})
            if self.first:
                raise RuntimeError("connection lost")
            return None

        def close(self) -> None:
            pass

    def factory() -> Transport:
        nonlocal attempts
        attempts += 1
        return Transport(attempts == 1)

    def on_connect() -> None:
        calls.append("connect")

    def on_disconnect() -> None:
        calls.append("disconnect")

    stop = lambda: attempts >= 2 and calls.count("connect") >= 2
    BybitWebSocketSession(
        ["BTCUSDT"], factory, on_connect=on_connect, on_disconnect=on_disconnect
    ).run_forever(lambda _frame: None, stop=stop, retry_seconds=0)

    assert calls == ["connect", "disconnect", "connect", "disconnect"]


def test_decode_orderbook_frame_rejects_wrong_topic_and_missing_update_id() -> None:
    with pytest.raises(ValueError):
        decode_orderbook_frame(
            json.dumps(
                {
                    "topic": "orderbook.500.BTCUSDT",
                    "type": "snapshot",
                    "data": {"s": "BTCUSDT", "u": 1, "b": [], "a": []},
                }
            )
        )
    with pytest.raises(ValueError):
        decode_orderbook_frame(
            json.dumps(
                {
                    "topic": "orderbook.1000.BTCUSDT",
                    "type": "snapshot",
                    "data": {"s": "BTCUSDT", "b": [], "a": []},
                }
            )
        )
