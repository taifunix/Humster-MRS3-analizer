from __future__ import annotations

import time

import pytest

from mrs3.bybit_collector.core import InvalidationCause, OrderBook, ResetCause


def test_snapshot_replaces_the_in_memory_book() -> None:
    book = OrderBook("BTCUSDT")

    assert book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    assert book.valid
    assert book.bids == {100.0: 2.0}
    assert book.asks == {101.0: 3.0}

    assert book.apply_snapshot([("99", "4")], [("102", "5")], update_id=2)
    assert book.bids == {99.0: 4.0}
    assert book.asks == {102.0: 5.0}


def test_snapshot_without_update_id_is_malformed() -> None:
    book = OrderBook("BTCUSDT")

    assert not book.apply_snapshot([("100", "2")], [("101", "3")])
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert not book.valid


def test_snapshot_message_without_update_id_is_malformed() -> None:
    book = OrderBook("BTCUSDT")

    assert not book.apply_message(
        {"type": "snapshot", "data": {"s": "BTCUSDT", "b": [["100", "2"]], "a": [["101", "3"]]}}
    )
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value


def test_delta_upserts_and_deletes_levels() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2"), ("99", "1")], [("101", "3")], update_id=10)

    assert book.apply_delta([("100", "4"), ("98", "2")], [("101", "0"), ("102", "5")], update_id=11)

    assert book.bids == {100.0: 4.0, 99.0: 1.0, 98.0: 2.0}
    assert book.asks == {102.0: 5.0}
    assert book.valid


def test_silence_does_not_make_a_synced_book_stale() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    before = book.state()
    time.sleep(0.01)

    assert book.valid
    assert book.state() == before


@pytest.mark.parametrize(
    ("method_name", "cause"),
    [
        ("on_disconnect", InvalidationCause.DISCONNECT),
        ("on_reconnect", InvalidationCause.RECONNECT),
        ("on_ping_failure", InvalidationCause.PING_FAILURE),
        ("on_resubscribe", InvalidationCause.RESUBSCRIBE),
    ],
)
def test_lifecycle_wrappers_invalidate_with_their_cause(
    method_name: str, cause: InvalidationCause
) -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    getattr(book, method_name)()

    assert not book.valid
    assert book.invalidation_cause == cause.value


def test_disconnect_requires_fresh_snapshot_and_snapshot_clears_cause() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    book.on_disconnect()

    assert not book.apply_delta([("100", "4")], [], update_id=2)
    assert not book.valid

    assert book.apply_snapshot([("99", "4")], [("101", "3")], update_id=2)
    assert book.valid
    assert book.invalidation_cause is None


def test_clock_discontinuity_requires_fresh_snapshot() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    book.on_clock_discontinuity()

    assert not book.apply_delta([("100", "4")], [], update_id=2)
    assert book.invalidation_cause == InvalidationCause.CLOCK_DISCONTINUITY.value

    assert book.apply_snapshot([("100", "2")], [("101", "3")], update_id=2)
    assert book.valid
    assert book.invalidation_cause is None


@pytest.mark.parametrize(
    "call",
    [
        lambda book: book.apply_delta([("100", "4")], [], update_id=None),
        lambda book: book.apply_delta({"b": [["100", "4"]], "a": []}),
    ],
)
def test_delta_without_update_id_is_malformed_and_does_not_mutate(call) -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert not call(book)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert book.bids == {100.0: 2.0}
    assert book.asks == {101.0: 3.0}
    assert book.last_update_id == 1


def test_delta_message_without_u_is_malformed_and_does_not_mutate() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert not book.apply_message(
        {"type": "delta", "data": {"b": [["100", "4"]], "a": []}}
    )
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert book.bids == {100.0: 2.0}
    assert book.last_update_id == 1


@pytest.mark.parametrize("update_id", [10, "11"])
def test_integer_and_string_update_ids_are_accepted(update_id: int | str) -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert book.apply_delta([("100", "4")], [], update_id=update_id)
    assert book.last_update_id == int(update_id)


def test_level_mapping_without_envelope_keys_is_not_rejected() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert book.apply_delta({"100": "4"}, update_id=2)
    assert book.bids == {100.0: 4.0}
    assert book.asks == {101.0: 3.0}


def test_delta_sequence_and_level_mapping_omit_asks_identically() -> None:
    sequence_book = OrderBook("BTCUSDT")
    mapping_book = OrderBook("BTCUSDT")
    for book in (sequence_book, mapping_book):
        book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert sequence_book.apply_delta([("100", "4")], update_id=2)
    assert mapping_book.apply_delta({"100": "4"}, update_id=2)
    assert sequence_book.state() == mapping_book.state()


@pytest.mark.parametrize("bids", [{"100": "2"}, [("100", "2")]])
def test_snapshot_without_asks_is_rejected_for_mapping_and_sequence(bids) -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    before = book.state()

    assert not book.apply_snapshot(bids, update_id=2)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert book.bids == before.bids
    assert book.asks == before.asks
    assert book.book_reset_count == before.book_reset_count


def test_crossed_snapshot_is_rejected_atomically() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    before = book.state()

    assert not book.apply_snapshot([("102", "4")], [("101", "3")], update_id=2)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.IMPOSSIBLE_LOCAL_STATE.value
    assert book.bids == before.bids
    assert book.asks == before.asks
    assert book.book_reset_count == before.book_reset_count
    assert book.reset_cause_counts == before.reset_cause_counts


def test_unknown_snapshot_reset_cause_is_malformed_without_mutation() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    before = book.state()

    assert not book.apply_snapshot(
        [("100", "4")], [("101", "5")], update_id=2, cause="not-a-cause"
    )
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert book.bids == before.bids
    assert book.asks == before.asks
    assert book.book_reset_count == before.book_reset_count
    assert book.reset_cause_counts == before.reset_cause_counts


def test_explicit_snapshot_reset_cause_is_attributed() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert book.apply_snapshot(
        [("100", "2")], [("101", "3")], update_id=2, cause=ResetCause.RECONNECT
    )
    assert book.last_reset_cause == ResetCause.RECONNECT.value
    assert book.reset_cause_counts[ResetCause.RECONNECT.value] == 1


def test_snapshot_envelope_symbol_mismatch_is_malformed_without_mutation() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    before = book.state()

    assert not book.apply_snapshot(
        {"b": [["100", "4"]], "a": [["101", "5"]], "s": "ETHUSDT", "u": 2}
    )
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert book.bids == before.bids
    assert book.asks == before.asks
    assert book.book_reset_count == before.book_reset_count
    assert book.reset_cause_counts == before.reset_cause_counts


@pytest.mark.parametrize("update_id", [True, "abc", ""])
def test_invalid_snapshot_update_id_is_malformed_without_mutation(update_id) -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    before = book.state()

    assert not book.apply_snapshot([("100", "4")], [("101", "5")], update_id=update_id)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert book.bids == before.bids
    assert book.asks == before.asks
    assert book.book_reset_count == before.book_reset_count
    assert book.reset_cause_counts == before.reset_cause_counts


def test_public_level_views_are_read_only_copies() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    old_bids = book.bids
    old_asks = book.asks

    with pytest.raises(TypeError):
        old_bids[100.0] = 9.0
    with pytest.raises(TypeError):
        old_asks[101.0] = 9.0

    assert book.apply_delta([("100", "4")], [("101", "5")], update_id=2)
    assert old_bids == {100.0: 2.0}
    assert old_asks == {101.0: 3.0}
    assert book.bids == {100.0: 4.0}
    assert book.asks == {101.0: 5.0}


def test_state_is_read_only_copy_and_retains_levels_when_invalid() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    saved = book.state()

    with pytest.raises(TypeError):
        saved.bids[100.0] = 9.0
    book.on_disconnect()
    current = book.state()

    assert saved.valid
    assert saved.bids == {100.0: 2.0}
    assert not current.valid
    assert current.bids == {100.0: 2.0}
    assert current.asks == {101.0: 3.0}


@pytest.mark.parametrize("cause", list(InvalidationCause))
def test_each_invalidation_cause_clears_valid_state(cause: InvalidationCause) -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    book.invalidate(cause)

    assert not book.valid
    assert book.invalidation_cause == cause.value
    assert book.bids == {100.0: 2.0}
    assert book.asks == {101.0: 3.0}


def test_delta_before_first_snapshot_is_rejected_and_invalidates() -> None:
    book = OrderBook("BTCUSDT")

    assert not book.apply_delta([("100", "2")], [("101", "3")], update_id=1)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.DELTA_BEFORE_SNAPSHOT.value
    assert book.book_reset_count == 0

    assert book.apply_snapshot([("100", "2")], [("101", "3")], update_id=2)
    assert book.valid


@pytest.mark.parametrize("quantity", [float("nan"), float("inf"), -1.0, 0.0])
def test_malformed_snapshot_invalidates_without_partial_mutation(quantity: float) -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert not book.apply_snapshot([("100", "9"), ("99", str(quantity))], [("101", "3")], update_id=2)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert book.bids == {100.0: 2.0}
    assert book.asks == {101.0: 3.0}
    assert book.book_reset_count == 1


def test_nonfinite_delta_invalidates_without_partial_mutation() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert not book.apply_delta([("100", "9"), ("99", "nan")], [], update_id=2)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert book.bids == {100.0: 2.0}
    assert book.asks == {101.0: 3.0}


def test_duplicate_price_levels_are_malformed_without_partial_mutation() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert not book.apply_delta([("100", "4"), ("100", "5")], [], update_id=2)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert book.bids == {100.0: 2.0}
    assert book.last_update_id == 1


def test_malformed_level_tuple_is_rejected_without_partial_mutation() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert not book.apply_delta([("100", "4", "extra")], [], update_id=2)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert book.bids == {100.0: 2.0}
    assert book.last_update_id == 1


@pytest.mark.parametrize("update_id", [10, 9])
def test_non_increasing_delta_id_invalidates(update_id: int) -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=10)

    assert not book.apply_delta([("100", "4")], [], update_id=update_id)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.NON_INCREASING_UPDATE_ID.value
    assert book.bids == {100.0: 2.0}


def test_greater_delta_id_does_not_require_numeric_adjacency() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=10)

    assert book.apply_delta([("100", "4")], [], update_id=1000)
    assert book.valid
    assert book.bids == {100.0: 4.0}


def test_crossed_delta_is_rejected_atomically() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert not book.apply_delta([("102", "4")], [], update_id=2)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.IMPOSSIBLE_LOCAL_STATE.value
    assert book.bids == {100.0: 2.0}
    assert book.asks == {101.0: 3.0}


def test_malformed_delta_with_valid_updates_never_partially_mutates() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)

    assert not book.apply_delta([("100", "4"), ("99", "bad")], [("101", "0")], update_id=2)
    assert book.bids == {100.0: 2.0}
    assert book.asks == {101.0: 3.0}
    assert not book.valid


def test_snapshot_reset_attribution_uses_priority_and_counts_once() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    assert book.last_reset_cause == "initial"
    assert book.reset_cause_counts["initial"] == 1

    book.invalidate(InvalidationCause.RECONNECT)
    book.invalidate(InvalidationCause.RESUBSCRIBE)
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=2)
    assert book.last_reset_cause == "resubscribe"

    book.invalidate(InvalidationCause.RECONNECT)
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=3)
    assert book.last_reset_cause == "reconnect"

    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=4)
    assert book.last_reset_cause == "exchange_snapshot"
    assert book.book_reset_count == 4
    assert book.reset_cause_counts == {
        "initial": 1,
        "resubscribe": 1,
        "reconnect": 1,
        "exchange_snapshot": 1,
    }


def test_captured_reset_cause_counts_do_not_change_after_snapshot() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    captured = book.reset_cause_counts

    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=2)

    assert captured["initial"] == 1
    assert captured["exchange_snapshot"] == 0
    assert book.reset_cause_counts["exchange_snapshot"] == 1


def test_bybit_message_adapter_accepts_snapshot_and_delta() -> None:
    book = OrderBook("BTCUSDT")

    assert book.apply_message(
        {"topic": "orderbook.1000.BTCUSDT", "type": "snapshot", "data": {"s": "BTCUSDT", "b": [["100", "2"]], "a": [["101", "3"]], "u": 1}}
    )
    assert book.apply_message(
        {"topic": "orderbook.1000.BTCUSDT", "type": "delta", "data": {"s": "BTCUSDT", "b": [["100", "4"]], "a": [], "u": 3}}
    )
    assert book.bids == {100.0: 4.0}


def test_message_for_another_symbol_is_malformed_without_mutation() -> None:
    book = OrderBook("BTCUSDT")

    assert not book.apply_message(
        {"type": "snapshot", "data": {"s": "ETHUSDT", "b": [["100", "2"]], "a": [["101", "3"]], "u": 1}}
    )
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value
    assert not book.valid


def test_invalid_delta_after_invalidation_cannot_resync_book() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    book.invalidate(InvalidationCause.DISCONNECT)

    assert not book.apply_delta([("100", "4")], [], update_id=2)
    assert not book.valid
    assert book.bids == {100.0: 2.0}


def test_unknown_invalidation_cause_raises_without_mutation() -> None:
    book = OrderBook("BTCUSDT")
    book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    before = book.state()

    with pytest.raises(ValueError, match="unknown invalidation cause"):
        book.invalidate("not-a-cause")

    assert book.state() == before


@pytest.mark.parametrize("message", [None, {"type": "unknown", "data": {}}])
def test_non_mapping_or_unknown_type_message_is_rejected(message) -> None:
    book = OrderBook("BTCUSDT")

    assert not book.apply_message(message)
    assert not book.valid
    assert book.invalidation_cause == InvalidationCause.MALFORMED_UPDATE.value


@pytest.mark.parametrize("symbol", ["", 123])
def test_invalid_order_book_symbol_is_rejected(symbol) -> None:
    with pytest.raises(ValueError, match="symbol must be a non-empty string"):
        OrderBook(symbol)


def test_delta_after_snapshot_accepts_arbitrary_id() -> None:
    book = OrderBook("BTCUSDT")

    assert book.apply_snapshot([("100", "2")], [("101", "3")], update_id=1)
    assert book.apply_delta([("100", "4")], update_id=987654)
    assert book.last_update_id == 987654
