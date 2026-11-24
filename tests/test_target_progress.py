"""Test pelacak target 100 USD/bulan (`tools/target_progress.py`).

Yang dikunci: metrik tidak boleh mengarang, gate §7 harus gagal pada data yang memang gagal,
dan estimasi kapasitas harus konsisten dengan aritmetika modal (leverage 2, buffer 15%).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tools import target_progress as tp


def _trade(pnl, funding=0.0, margin=2.5, reason="SL", hold_h=1.0):
    opened = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    return {
        "asset": "AAA",
        "margin": margin,
        "pnl": pnl,
        "funding": funding,
        "reason": reason,
        "opened": opened,
        "closed": opened + timedelta(hours=hold_h),
    }


def test_metrics_menghitung_expectancy_dan_payoff():
    trades = [_trade(+0.10, reason="TP"), _trade(-0.05), _trade(-0.05), _trade(+0.10, reason="TP")]
    m = tp.compute_metrics(trades)
    assert m["n"] == 4
    assert m["win_rate"] == pytest.approx(0.5)
    assert m["avg_win"] == pytest.approx(0.10)
    assert m["avg_loss"] == pytest.approx(0.05)
    assert m["payoff"] == pytest.approx(2.0)
    assert m["expectancy"] == pytest.approx(0.5 * 0.10 - 0.5 * 0.05)
    assert m["net"] == pytest.approx(0.10)


def test_metrics_porsi_funding_dihitung_dari_inflow_bukan_net():
    # inflow = gross_win 0.20 + funding 0.20 = 0.40 -> porsi funding 50%
    trades = [_trade(+0.10, funding=0.10), _trade(+0.10, funding=0.10), _trade(-0.05)]
    m = tp.compute_metrics(trades)
    assert m["funding_share"] == pytest.approx(0.5)


def test_metrics_tanpa_hold_dan_tanpa_sl_tidak_mengarang():
    trades = [{"margin": 2.5, "pnl": 0.0, "funding": 0.0, "reason": "TP", "opened": None, "closed": None}]
    m = tp.compute_metrics(trades)
    assert m["median_hold_h"] is None
    assert m["worst_sl_loss_frac"] == 0.0
    assert m["expectancy"] == pytest.approx(0.0)


def test_equity_series_mendukung_header_lama_dan_baru():
    old = [["Tanggal (UTC)", "Saldo Awal ($)"], ["2026-11-15", "15.58"]]
    new = [["Tanggal", "Total Ekuitas ($)"], ["2026-11-24", "19.51"]]
    assert tp.equity_series(old) == [("2026-11-15", 15.58)]
    assert tp.equity_series(new) == [("2026-11-24", 19.51)]


def test_max_drawdown_memakai_puncak_berjalan():
    series = [("d1", 10.0), ("d2", 12.0), ("d3", 9.0), ("d4", 11.0)]
    assert tp.max_drawdown(series) == pytest.approx((12.0 - 9.0) / 12.0)


def test_kapasitas_aritmetika_konsisten():
    cap = tp.compute_capacity(300.0, turns_per_day=1.0)
    expected_notional = 300.0 * (1 - tp.CASH_BUFFER) * tp.MAX_LEVERAGE
    assert cap["notional"] == pytest.approx(expected_notional)
    assert cap["net_daily_pct"] == pytest.approx(
        tp.CARRY_DAILY - (tp.FEE_ROUNDTRIP + tp.SLIPPAGE_PROXY)
    )
    # Dibutuhkan ekuitas ~ $545 untuk $100/bulan pada asumsi ini (bukan $20).
    assert 500 < cap["needed_equity"] < 600


def _metrics_ok():
    return {
        "n": 25, "wins": 15, "losses": 10, "win_rate": 0.6, "net": 0.5,
        "gross_win": 1.0, "gross_loss": 0.5, "avg_win": 0.0667, "avg_loss": 0.05,
        "payoff": 1.33, "expectancy": 0.02, "funding": 1.2, "funding_share": 0.55,
        "median_hold_h": 7.0, "hold_samples": 20, "worst_sl_loss_frac": 0.02,
        "sl_loss_samples": 5,
    }


def test_gate_semua_lulus_pada_data_sehat():
    gates = tp.evaluate(_metrics_ok(), drawdown=0.03, equity=600.0)
    assert all(g["ok"] for g in gates), [g["name"] for g in gates if not g["ok"]]


def test_gate_gagal_sesuai_bukti_2026_11_24():
    """Data nyata 21-24 Nov: expectancy negatif, hold < 6 jam, SL 8.8% margin, ekuitas kecil."""
    metrics = {
        "n": 20, "wins": 8, "losses": 12, "win_rate": 0.4, "net": -0.7064,
        "gross_win": 0.3509, "gross_loss": 1.0573, "avg_win": 0.0439, "avg_loss": 0.0881,
        "payoff": 0.50, "expectancy": -0.0353, "funding": 0.0169, "funding_share": 0.046,
        "median_hold_h": 0.95, "hold_samples": 12, "worst_sl_loss_frac": 0.0884,
        "sl_loss_samples": 9,
    }
    gates = {g["name"]: g["ok"] for g in tp.evaluate(metrics, drawdown=0.0284, equity=19.51)}
    assert gates["jumlah trade"] is True
    assert gates["max drawdown"] is True
    for name in ("expectancy/trade", "porsi funding", "median hold",
                 "rugi SL terburuk", "kapasitas bulanan"):
        assert gates[name] is False, name


def test_parse_closed_melewati_baris_tanpa_pnl():
    rows = [
        ["Asset", "Sisi", "C", "D", "Entry Price ($)", "Exit Price ($)", "Margin ($)",
         "PnL ($)", "Funding ($)", "Alasan Close", "K", "Closed At (exchange)", "M",
         "Opened At (exchange)"],
        ["AAA", "LONG", "1", "x", "10", "11", "2.5", "0.25", "0.01", "TP",
         "p", "2026-11-24T11:00:00Z", "v", "2026-11-24T10:00:00Z"],
        ["BBB", "LONG", "2", "x", "10", "", "2.5", "", "0", "backfill",
         "q", "2026-11-24T12:00:00Z", "v", "2026-11-24T10:00:00Z"],
    ]
    trades = tp.parse_closed(rows)
    assert len(trades) == 1, "baris backfill tanpa PnL bukan trade"
    assert trades[0]["pnl"] == pytest.approx(0.25)
    assert trades[0]["closed"] is not None and trades[0]["opened"] is not None