from pathlib import Path

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"

def _replace_metric(name: str, value: str) -> bytes:
    source = FIXTURE.read_text(encoding="utf-8")
    marker = f"<td>{name}</td><td>"
    start = source.index(marker) + len(marker)
    end = source.index("</td>", start)
    return (source[:start] + value + source[end:]).encode("utf-8")


def test_recovery_factor_skips_non_numeric_drawdown() -> None:
    from mrs3.source_v6 import normalize_source_v6

    source = FIXTURE.read_text(encoding="utf-8").replace(
        '<tr><td>Max Drawdown</td><td>30</td></tr>',
        '<tr><td>Max Drawdown</td><td>n/a</td></tr>'
        '<tr><td>Recovery Factor (Total PnL / Max DD)</td><td>1.0</td></tr>',
    ).encode("utf-8")
    normalize_source_v6(source)


def test_positive_only_tester_profit_factor_zero_is_preserved() -> None:
    from mrs3.source_v6 import normalize_source_v6

    normalize_source_v6(
        (Path(__file__).parent / "fixtures" / "performance" / "source_v6_position_across_orders.html").read_bytes()
    )


def test_normalization_rejects_total_fees_mismatch() -> None:
    from mrs3.source_v6 import SourceV6Error, normalize_source_v6

    with pytest.raises(SourceV6Error, match=r"M7.*Total fees"):
        normalize_source_v6(_replace_metric("Total fees", "0.21"), source_name="bad-fees.html")


def test_m7_uses_raw_declared_exponent_and_half_up() -> None:
    from mrs3.source_v6 import normalize_source_v6

    # Derived PnL is 5.8, which rounds to 5.80 at the raw token's exponent.
    assert normalize_source_v6(_replace_metric("Total PnL", "5.80")).metrics["Total PnL"] == "5.80"


def test_m7_total_pnl_anchors_to_declared_initial_balance() -> None:
    from mrs3.source_v6 import normalize_source_v6

    source = FIXTURE.read_text(encoding="utf-8").replace(
        'const walletSeries = [[1767225600000,"1000"]',
        'const walletSeries = [[1767225600000,"999.99"]',
    ).replace('<td>Total PnL</td><td>5.8</td>', '<td>Total PnL</td><td>5.80</td>').encode("utf-8")
    normalize_source_v6(source)


def test_m7_total_pnl_uses_declared_final_balance_when_wallet_series_is_stale() -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    from mrs3.source_v6 import _validate_m7

    report = SimpleNamespace(
        metrics={"Total PnL": "5.80", "Final balance": "1005.80"},
        wallet_series=((1, Decimal("1000")), (2, Decimal("1004.80"))),
        equity_series=(),
    )

    _validate_m7(
        report,
        (),
        initial_balance=Decimal("1000"),
        source_sha256="a" * 64,
        source_name="stale-wallet.html",
    )


def test_m7_falls_back_to_last_wallet_sample_without_final_balance() -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    from mrs3.source_v6 import _validate_m7

    report = SimpleNamespace(
        metrics={"Total PnL": "4.80"},
        wallet_series=((1, Decimal("1000")), (2, Decimal("1004.80"))),
        equity_series=(),
    )

    _validate_m7(
        report,
        (),
        initial_balance=Decimal("1000"),
        source_sha256="a" * 64,
        source_name="wallet-fallback.html",
    )


def test_m7_recovery_factor_uses_declared_final_balance_and_equity_series() -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    from mrs3.source_v6 import _validate_m7

    report = SimpleNamespace(
        metrics={
            "Total PnL": "5.80", "Final balance": "1005.80",
            "Max Drawdown": "30", "Recovery Factor": "0.1933",
        },
        wallet_series=((1, Decimal("1000")), (2, Decimal("1004.80"))),
        equity_series=((1, Decimal("1000")), (2, Decimal("970"))),
    )

    _validate_m7(
        report,
        (),
        initial_balance=Decimal("1000"),
        source_sha256="a" * 64,
        source_name="stale-wallet-recovery.html",
    )


def test_m7_recovery_factor_uses_declared_final_balance_when_wallet_series_is_stale() -> None:
    from mrs3.source_v6 import normalize_source_v6

    source = FIXTURE.read_text(encoding="utf-8").replace(
        '[1767873600000,"1005.8"]',
        '[1767873600000,"1005.795"]',
    ).replace('<td>Total PnL</td><td>5.8</td>', '<td>Total PnL</td><td>5.80</td>').replace(
        '<tr><td>Max Drawdown</td><td>30</td></tr>',
        '<tr><td>Max Drawdown</td><td>30</td></tr>'
        '<tr><td>Recovery Factor (Total PnL / Max DD)</td><td>0.1932</td></tr>',
    ).encode("utf-8")
    normalize_source_v6(source)


def test_m7_accepts_recovery_factor_mutation_as_non_blocking_diagnostic() -> None:
    from mrs3.source_v6 import normalize_source_v6

    source = FIXTURE.read_text(encoding="utf-8").replace(
        '<tr><td>Max Drawdown</td><td>30</td></tr>',
        '<tr><td>Max Drawdown</td><td>30</td></tr>'
        '<tr><td>Recovery Factor (Total PnL / Max DD)</td><td>1.0</td></tr>',
    ).encode("utf-8")
    normalize_source_v6(source)


def test_normalization_rejects_profit_factor_mutation() -> None:
    from mrs3.source_v6 import SourceV6Error, normalize_source_v6

    with pytest.raises(SourceV6Error, match=r"M7.*Profit Factor"):
        normalize_source_v6(_replace_metric("Profit Factor", "2.4"), source_name="bad-pf.html")


def test_m7_accepts_profit_factor_drift_up_to_one_hundredth() -> None:
    from types import SimpleNamespace
    from decimal import Decimal

    from mrs3.source_v6 import NormalizedAction, _validate_m7

    actions = (
        NormalizedAction("win", 1, "ONUSDT", "a", "closed", Decimal("0"), Decimal("398.1348"), None, None, None, "LONG"),
        NormalizedAction("loss", 2, "ONUSDT", "b", "closed", Decimal("0"), Decimal("-0.0640"), None, None, None, "LONG"),
    )
    report = SimpleNamespace(
        metrics={"Profit Factor": "6220.85"},
        wallet_series=((1, Decimal("1000")),),
        equity_series=(),
    )

    _validate_m7(
        report,
        actions,
        initial_balance=Decimal("1000"),
        source_sha256="a" * 64,
        source_name="pf-tolerance.html",
    )


def test_m7_rejects_profit_factor_drift_over_one_hundredth() -> None:
    from types import SimpleNamespace
    from decimal import Decimal

    from mrs3.source_v6 import NormalizedAction, SourceV6Error, _validate_m7

    actions = (
        NormalizedAction("win", 1, "ONUSDT", "a", "closed", Decimal("0"), Decimal("398.1348"), None, None, None, "LONG"),
        NormalizedAction("loss", 2, "ONUSDT", "b", "closed", Decimal("0"), Decimal("-0.0640"), None, None, None, "LONG"),
    )
    report = SimpleNamespace(metrics={"Profit Factor": "6220.84"}, wallet_series=((1, Decimal("1000")),), equity_series=())
    with pytest.raises(SourceV6Error, match=r"M7.*Profit Factor"):
        _validate_m7(report, actions, initial_balance=Decimal("1000"), source_sha256="a" * 64, source_name="pf-over-tolerance.html")


def test_import_quarantines_only_m7_report_and_exposes_detail(tmp_path: Path) -> None:
    from mrs3.source_v6_importer import import_source_v6, preflight_source_v6
    from mrs3.source_v6_storage import quarantine_details

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "good.html").write_bytes(FIXTURE.read_bytes())
    (reports / "bad.html").write_bytes(_replace_metric("Total fees", "0.21"))
    target = tmp_path / "source-v6.duckdb"

    result = import_source_v6(reports, target, preflight=preflight_source_v6(reports, target), workers=1)

    assert (result.accepted_count, result.quarantined_count, result.safe_to_delete) == (1, 1, "NO")
    details = quarantine_details(target)
    assert len(details) == 1
    assert details[0]["source_name"] == "bad.html"
    assert details[0]["source_sha256"]
    assert details[0]["fragment_id"]
    assert details[0]["fragment_id"] != details[0]["source_sha256"]
    assert "Total fees" in details[0]["reason"]


def test_quarantined_database_blocks_preflight_and_materialization(tmp_path: Path) -> None:
    from mrs3.source_v6_importer import import_source_v6, preflight_source_v6
    from mrs3.source_v6_materializer import materialize_source_v6_from_database
    from mrs3.panel_surfaces import LocalSurfacesService

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "good.html").write_bytes(FIXTURE.read_bytes())
    (reports / "bad.html").write_bytes(_replace_metric("Total PnL", "5.7"))
    target = tmp_path / "source-v6.duckdb"
    import_source_v6(reports, target, preflight=preflight_source_v6(reports, target), workers=1)

    service = LocalSurfacesService()
    preflight = service.preflight(target)
    assert preflight["rows"] and all(row["status"] != "READY" for row in preflight["rows"])
    with pytest.raises(ValueError, match=r"quarantine.*bad.html"):
        materialize_source_v6_from_database(target, ("ONUSDT|LONG|1h",))
