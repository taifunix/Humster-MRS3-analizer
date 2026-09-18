from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
import pandas as pd
import pytest

from mrs3.screener.errors import ScreenerEvaluationError
from mrs3.screener.listing import resolve_listing_dates


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _instruments_payload(items: list[dict]) -> dict:
    return {"retCode": 0, "result": {"list": items}}


def _instrument(symbol: str, launch_ms: int) -> dict:
    return {"symbol": symbol, "launchTime": str(launch_ms)}


@pytest.fixture(autouse=True)
def _reset_throttle_state(monkeypatch: pytest.MonkeyPatch):
    import mrs3.screener.listing as listing_module

    listing_module._last_request_monotonic = None
    # Keep the rest of this file's tests fast/deterministic by default; the
    # dedicated throttle-timing test below overrides this with a fake clock.
    monkeypatch.setattr("mrs3.screener.listing.time.sleep", lambda seconds: None)
    yield
    listing_module._last_request_monotonic = None


def _build_registry(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Пары"
    sheet.append(["Пара", "Дата листинга на Bybit (UTC)"])
    for symbol, date in rows:
        sheet.append([symbol, date])
    path = tmp_path / "registry.xlsx"
    workbook.save(path)
    return path


def _build_dates_xlsx(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    path = tmp_path / "dates.xlsx"
    pd.DataFrame(rows).to_excel(path, index=False, header=False)
    return path


def test_resolve_listing_dates_prefers_registry_over_dates_xlsx(tmp_path: Path) -> None:
    registry_path = _build_registry(tmp_path, [("SOXLUSDT", "2026-05-19")])
    dates_path = _build_dates_xlsx(tmp_path, [("SOXLUSDT", "2020-01-01")])

    result = resolve_listing_dates(
        ("SOXLUSDT",), registry_path=registry_path, dates_path=dates_path
    )

    assert result == {"SOXLUSDT": pd.Timestamp("2026-05-19", tz="UTC")}


def test_resolve_listing_dates_falls_back_to_dates_xlsx_for_missing_symbol(
    tmp_path: Path,
) -> None:
    registry_path = _build_registry(tmp_path, [("SOXLUSDT", "2026-05-19")])
    dates_path = _build_dates_xlsx(tmp_path, [("AAAUSDT", "2026-07-01")])

    result = resolve_listing_dates(
        ("SOXLUSDT", "AAAUSDT"), registry_path=registry_path, dates_path=dates_path
    )

    assert result == {
        "SOXLUSDT": pd.Timestamp("2026-05-19", tz="UTC"),
        "AAAUSDT": pd.Timestamp("2026-07-01", tz="UTC"),
    }


def test_resolve_listing_dates_matches_dates_xlsx_case_insensitively(tmp_path: Path) -> None:
    dates_path = _build_dates_xlsx(tmp_path, [("soxlusdt", "2026-05-19")])

    result = resolve_listing_dates(("SOXLUSDT",), registry_path=None, dates_path=dates_path)

    assert result == {"SOXLUSDT": pd.Timestamp("2026-05-19", tz="UTC")}


def test_resolve_listing_dates_does_not_read_dates_xlsx_when_registry_covers_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry_path = _build_registry(tmp_path, [("SOXLUSDT", "2026-05-19")])
    # A dates.xlsx with a malformed, unrelated row: if it were read at all,
    # this would raise even though the registry already resolved SOXLUSDT.
    dates_path = tmp_path / "dates.xlsx"
    pd.DataFrame([["ZZZUSDT", "not-a-date"]]).to_excel(dates_path, index=False, header=False)

    result = resolve_listing_dates(
        ("SOXLUSDT",), registry_path=registry_path, dates_path=dates_path
    )

    assert result == {"SOXLUSDT": pd.Timestamp("2026-05-19", tz="UTC")}


def test_resolve_listing_dates_rejects_case_insensitive_duplicate_in_dates_xlsx(
    tmp_path: Path,
) -> None:
    dates_path = _build_dates_xlsx(
        tmp_path, [("SOXLUSDT", "2026-05-19"), ("soxlusdt", "2026-07-01")]
    )

    with pytest.raises(ScreenerEvaluationError, match="case-insensitive duplicate"):
        resolve_listing_dates(("SOXLUSDT",), registry_path=None, dates_path=dates_path)


def test_resolve_listing_dates_ignores_unrelated_duplicate_in_dates_xlsx(
    tmp_path: Path,
) -> None:
    dates_path = _build_dates_xlsx(
        tmp_path,
        [
            ("AAAUSDT", "2026-07-01"),
            ("BBBUSDT", "2026-01-01"),
            ("bbbusdt", "2026-02-01"),
        ],
    )

    result = resolve_listing_dates(("AAAUSDT",), registry_path=None, dates_path=dates_path)

    assert result == {"AAAUSDT": pd.Timestamp("2026-07-01", tz="UTC")}


def test_resolve_listing_dates_ignores_dates_xlsx_duplicate_for_symbol_registry_already_resolved(
    tmp_path: Path,
) -> None:
    registry_path = _build_registry(tmp_path, [("SOXLUSDT", "2026-05-19")])
    # dates.xlsx has a case-insensitive duplicate for SOXLUSDT (irrelevant,
    # the registry already resolved it) plus a valid, unambiguous AAAUSDT row.
    dates_path = _build_dates_xlsx(
        tmp_path,
        [
            ("SOXLUSDT", "2020-01-01"),
            ("soxlusdt", "2020-02-01"),
            ("AAAUSDT", "2026-07-01"),
        ],
    )

    result = resolve_listing_dates(
        ("SOXLUSDT", "AAAUSDT"), registry_path=registry_path, dates_path=dates_path
    )

    assert result == {
        "SOXLUSDT": pd.Timestamp("2026-05-19", tz="UTC"),
        "AAAUSDT": pd.Timestamp("2026-07-01", tz="UTC"),
    }


def test_resolve_listing_dates_wraps_malformed_dates_xlsx_error(tmp_path: Path) -> None:
    dates_path = tmp_path / "dates.xlsx"
    pd.DataFrame([["AAAUSDT", "not-a-date"]]).to_excel(dates_path, index=False, header=False)

    with pytest.raises(ScreenerEvaluationError):
        resolve_listing_dates(("AAAUSDT",), registry_path=None, dates_path=dates_path)


def test_resolve_listing_dates_ignores_unrelated_malformed_registry_row(
    tmp_path: Path,
) -> None:
    registry_path = _build_registry(
        tmp_path, [("SOXLUSDT", "2026-05-19"), ("BADUSDT", "not-a-date")]
    )

    result = resolve_listing_dates(
        ("SOXLUSDT",), registry_path=registry_path, dates_path=None
    )

    assert result == {"SOXLUSDT": pd.Timestamp("2026-05-19", tz="UTC")}


def test_resolve_listing_dates_normalizes_requested_symbol_casing(tmp_path: Path) -> None:
    registry_path = _build_registry(tmp_path, [("SOXLUSDT", "2026-05-19")])

    result = resolve_listing_dates(
        (" soxlusdt ",), registry_path=registry_path, dates_path=None
    )

    assert result == {"SOXLUSDT": pd.Timestamp("2026-05-19", tz="UTC")}


def test_resolve_listing_dates_bybit_picks_matching_symbol_from_multiple_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url, params, timeout):
        return _FakeResponse(
            _instruments_payload(
                [_instrument("OTHERUSDT", 999), _instrument(params["symbol"], 123)]
            )
        )

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    result = resolve_listing_dates(("MMMUSDT",), registry_path=None, dates_path=None)

    assert result == {"MMMUSDT": pd.Timestamp(123, unit="ms", tz="UTC")}


def test_resolve_listing_dates_bybit_no_item_matches_requested_symbol_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url, params, timeout):
        return _FakeResponse(_instruments_payload([_instrument("OTHERUSDT", 999)]))

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    with pytest.raises(ScreenerEvaluationError, match="NNNUSDT"):
        resolve_listing_dates(("NNNUSDT",), registry_path=None, dates_path=None)


def test_resolve_listing_dates_skips_missing_registry_and_dates_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    def fake_get(url, params, timeout):
        calls.append(dict(params))
        return _FakeResponse(_instruments_payload([_instrument("BBBUSDT", 1700000000000)]))

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    result = resolve_listing_dates(
        ("BBBUSDT",),
        registry_path=tmp_path / "missing_registry.xlsx",
        dates_path=tmp_path / "missing_dates.xlsx",
    )

    assert result == {"BBBUSDT": pd.Timestamp(1700000000000, unit="ms", tz="UTC")}
    assert calls == [{"category": "linear", "symbol": "BBBUSDT"}]


def test_resolve_listing_dates_bybit_fallback_one_request_per_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def fake_get(url, params, timeout):
        calls.append(dict(params))
        return _FakeResponse(_instruments_payload([_instrument(params["symbol"], 1600000000000)]))

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    result = resolve_listing_dates(
        ("CCCUSDT", "DDDUSDT"), registry_path=None, dates_path=None
    )

    assert result == {
        "CCCUSDT": pd.Timestamp(1600000000000, unit="ms", tz="UTC"),
        "DDDUSDT": pd.Timestamp(1600000000000, unit="ms", tz="UTC"),
    }
    assert calls == [
        {"category": "linear", "symbol": "CCCUSDT"},
        {"category": "linear", "symbol": "DDDUSDT"},
    ]


def test_resolve_listing_dates_bybit_missing_symbol_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url, params, timeout):
        return _FakeResponse(_instruments_payload([]))

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    with pytest.raises(ScreenerEvaluationError, match="FFFUSDT"):
        resolve_listing_dates(("FFFUSDT",), registry_path=None, dates_path=None)


def test_resolve_listing_dates_bybit_bad_retcode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, params, timeout):
        return _FakeResponse({"retCode": 10001, "result": {}})

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    with pytest.raises(ScreenerEvaluationError, match="retCode"):
        resolve_listing_dates(("GGGUSDT",), registry_path=None, dates_path=None)


def test_resolve_listing_dates_bybit_request_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def fake_get(url, params, timeout):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    with pytest.raises(ScreenerEvaluationError, match="request failed"):
        resolve_listing_dates(("HHHUSDT",), registry_path=None, dates_path=None)


def test_resolve_listing_dates_bybit_403_stops_immediately_with_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get(url, params, timeout):
        calls.append(params["symbol"])
        return _FakeResponse({}, status_code=403)

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    with pytest.raises(ScreenerEvaluationError, match="10 minutes"):
        resolve_listing_dates(("AAAUSDT", "BBBUSDT"), registry_path=None, dates_path=None)

    # Must not keep hammering a blocked IP with more symbol requests.
    assert calls == ["AAAUSDT"]


def test_throttle_enforces_minimum_interval_between_bybit_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clock = [0.0]
    sleep_calls: list[float] = []
    call_times: list[float] = []

    def fake_monotonic() -> float:
        return fake_clock[0]

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        fake_clock[0] += seconds

    def fake_get(url, params, timeout):
        call_times.append(fake_clock[0])
        fake_clock[0] += 0.01  # simulate a small request duration
        return _FakeResponse(_instruments_payload([_instrument(params["symbol"], 1)]))

    monkeypatch.setattr("mrs3.screener.listing.time.monotonic", fake_monotonic)
    monkeypatch.setattr("mrs3.screener.listing.time.sleep", fake_sleep)
    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    resolve_listing_dates(("AAAUSDT", "BBBUSDT", "CCCUSDT"), registry_path=None, dates_path=None)

    assert len(sleep_calls) == 2  # no wait before the first request only
    assert call_times[1] - call_times[0] == pytest.approx(0.25)
    assert call_times[2] - call_times[1] == pytest.approx(0.25)


def test_throttle_persists_across_separate_resolve_listing_dates_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clock = [0.0]
    call_times: list[float] = []

    def fake_monotonic() -> float:
        return fake_clock[0]

    def fake_sleep(seconds: float) -> None:
        fake_clock[0] += seconds

    def fake_get(url, params, timeout):
        call_times.append(fake_clock[0])
        fake_clock[0] += 0.01
        return _FakeResponse(_instruments_payload([_instrument(params["symbol"], 1)]))

    monkeypatch.setattr("mrs3.screener.listing.time.monotonic", fake_monotonic)
    monkeypatch.setattr("mrs3.screener.listing.time.sleep", fake_sleep)
    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    # Two SEPARATE calls, not two symbols in one call — the gap between the
    # last request of the first call and the first request of the second
    # must still be enforced, since Bybit's IP ban is keyed off a rolling
    # window, not a "call".
    resolve_listing_dates(("AAAUSDT",), registry_path=None, dates_path=None)
    resolve_listing_dates(("BBBUSDT",), registry_path=None, dates_path=None)

    assert len(call_times) == 2
    assert call_times[1] - call_times[0] == pytest.approx(0.25)


def test_resolve_listing_dates_bybit_invalid_launch_time_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url, params, timeout):
        return _FakeResponse(
            _instruments_payload([{"symbol": params["symbol"], "launchTime": "not-a-number"}])
        )

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    with pytest.raises(ScreenerEvaluationError, match="invalid Bybit launchTime"):
        resolve_listing_dates(("KKKUSDT",), registry_path=None, dates_path=None)


def test_resolve_listing_dates_bybit_out_of_bounds_launch_time_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url, params, timeout):
        return _FakeResponse(
            _instruments_payload([{"symbol": params["symbol"], "launchTime": str(10**30)}])
        )

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    with pytest.raises(ScreenerEvaluationError, match="invalid Bybit launchTime"):
        resolve_listing_dates(("PPPUSDT",), registry_path=None, dates_path=None)


def test_resolve_listing_dates_bybit_non_dict_payload_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url, params, timeout):
        return _FakeResponse(["unexpected", "list", "payload"])

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    with pytest.raises(ScreenerEvaluationError, match="unexpected response shape"):
        resolve_listing_dates(("QQQUSDT",), registry_path=None, dates_path=None)


def test_resolve_listing_dates_bybit_zero_launch_time_treated_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url, params, timeout):
        return _FakeResponse(
            _instruments_payload([{"symbol": params["symbol"], "launchTime": "0"}])
        )

    monkeypatch.setattr("mrs3.screener.listing.httpx.get", fake_get)

    with pytest.raises(ScreenerEvaluationError, match="not found on Bybit"):
        resolve_listing_dates(("LLLUSDT",), registry_path=None, dates_path=None)
