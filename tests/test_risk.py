"""
Unit tests untuk manajemen risiko, position sizing, dan logging Walt Perps Bot.
Berjalan 100% offline tanpa koneksi jaringan (WaltClient di-mock).
"""

import csv
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import walt_bot
from walt_bot import (
    DEFAULT_MARGIN,
    FUNDING_FLIP_CLOSE_THRESHOLD,
    TRAILING_TIERS,
    Position,
    _sl_is_improvement,
    apply_layer2_funding,
    apply_layer3_volatility,
    apply_layer4_trailing,
    calculate_dynamic_margin,
    compute_baseline_tpsl,
    evaluate_and_apply_position_risk,
    get_asset_baseline_tpsl_pct,
    get_total_equity,
)
from sheet_logger import log_execution


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    client.preview_auto_close.return_value = {"ok": True, "confirmationToken": "tok_ac"}
    client.preview_close.return_value = {"ok": True, "confirmationToken": "tok_close"}
    client.execute.return_value = {"ok": True, "order": {"order_id": "ord_123"}}
    return client


def _make_pos(
    side: str = "long",
    entry: float = 100.0,
    current: float = 100.0,
    tp: float | None = 101.2,
    sl: float | None = 99.0,
    asset: str = "NVDA",
    margin: float = 2.50,
    leverage: int = 2,
) -> Position:
    return Position(
        contract_id=1,
        base_asset=asset,
        side=side,  # type: ignore
        leverage=leverage,
        margin=margin,
        entry_price=entry,
        current_price=current,
        tp=tp,
        sl=sl,
        funding_paid=0.05,
        liquidation_price=50.0 if side == "long" else 150.0,
    )


# -----------------------------------------------------------------------------
# 1. compute_baseline_tpsl: SL 1% di semua aset, TP 1.0%/1.2%, Windfall TP
# -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "asset,expected_tp_pct",
    [
        ("SPY", 0.0055),
        ("US100", 0.0055),
        ("AAPL", 0.0055),
        ("BTC", 0.0060),
        ("SOL", 0.0060),
        ("XAU", 0.0065),
        ("XAG", 0.0065),
        ("NVDA", 0.0075),
        ("NBIS", 0.0075),
        (None, 0.0075),
    ],
)
def test_baseline_tpsl_sl_is_always_1_percent_and_tp_wick_catcher(asset, expected_tp_pct):
    tp_pct, sl_pct = get_asset_baseline_tpsl_pct(asset)
    assert sl_pct == pytest.approx(0.010)
    assert tp_pct == pytest.approx(expected_tp_pct)

    entry = 100.0
    tp_long, sl_long = compute_baseline_tpsl("long", entry, asset=asset, funding_daily=0.0)
    assert sl_long == pytest.approx(99.0)
    assert tp_long == pytest.approx(round(entry * (1 + expected_tp_pct), 4))

    tp_short, sl_short = compute_baseline_tpsl("short", entry, asset=asset, funding_daily=0.0)
    assert sl_short == pytest.approx(101.0)
    assert tp_short == pytest.approx(round(entry * (1 - expected_tp_pct), 4))


def test_windfall_tp_advances_tp_when_15x_funding_between_045_and_baseline():
    # NVDA baseline TP = 0.75% (0.0075). Funding 0.4%/hari -> windfall_tp = 1.5 * 0.4% = 0.60% (0.0060) < 0.0075
    tp_long, sl_long = compute_baseline_tpsl("long", 100.0, asset="NVDA", funding_daily=-0.004)
    assert tp_long == pytest.approx(100.6)
    assert sl_long == pytest.approx(99.0)

    tp_short, sl_short = compute_baseline_tpsl("short", 100.0, asset="NVDA", funding_daily=0.004)
    assert tp_short == pytest.approx(99.4)
    assert sl_short == pytest.approx(101.0)


# -----------------------------------------------------------------------------
# 2. _sl_is_improvement
# -----------------------------------------------------------------------------

def test_sl_is_improvement():
    pos_no_sl = _make_pos(side="long", sl=None)
    assert _sl_is_improvement(pos_no_sl, 99.0) is True

    pos_long = _make_pos(side="long", sl=99.0)
    assert _sl_is_improvement(pos_long, 99.2) is True
    assert _sl_is_improvement(pos_long, 99.0) is False
    assert _sl_is_improvement(pos_long, 98.5) is False

    pos_short = _make_pos(side="short", sl=101.0)
    assert _sl_is_improvement(pos_short, 100.8) is True
    assert _sl_is_improvement(pos_short, 101.0) is False
    assert _sl_is_improvement(pos_short, 101.5) is False


# -----------------------------------------------------------------------------
# 3. Guard sisi salah & migrasi close (Poin 8)
# -----------------------------------------------------------------------------

def test_guard_migrates_old_sl_and_closes_when_sl_1pct_breached_long(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    client = _make_mock_client()

    # Posisi lama LONG dengan SL 3% (97.0), harga sekarang 98.5 (rugi 1.5% > 1%)
    pos = _make_pos(side="long", entry=100.0, current=98.5, tp=101.2, sl=97.0)
    res = evaluate_and_apply_position_risk(client, pos, current_funding_daily=-0.006)

    assert res is not None
    assert res["action"] == "CLOSE"
    assert res["detail"] == "SL 1% breached (migrasi)"
    client.preview_close.assert_called_once_with(pos.contract_id)
    client.preview_auto_close.assert_not_called()


def test_guard_closes_when_sl_1pct_breached_short(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    client = _make_mock_client()

    # Posisi lama SHORT dengan SL 3% (103.0), harga sekarang 101.4 (rugi 1.4% > 1%)
    pos = _make_pos(side="short", entry=100.0, current=101.4, tp=98.8, sl=103.0)
    res = evaluate_and_apply_position_risk(client, pos, current_funding_daily=0.006)

    assert res is not None
    assert res["action"] == "CLOSE"
    assert res["detail"] == "SL 1% breached (migrasi)"
    client.preview_close.assert_called_once_with(pos.contract_id)
    client.preview_auto_close.assert_not_called()


def test_guard_tightens_old_sl_to_1pct_when_loss_under_1pct():
    client = _make_mock_client()

    # Posisi lama LONG dengan SL 2.5% (97.5), harga sekarang 99.4 (rugi 0.6% < 1%)
    pos = _make_pos(side="long", entry=100.0, current=99.4, tp=101.2, sl=97.5)
    res = evaluate_and_apply_position_risk(client, pos, current_funding_daily=-0.006)

    assert res is not None
    assert res["action"] == "UPDATE_AUTO_CLOSE"
    assert res["sl"] == pytest.approx(99.0)
    assert res["sl"] < pos.current_price
    client.preview_close.assert_not_called()


def test_guard_closes_when_tp_breached(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    client = _make_mock_client()

    pos = _make_pos(side="long", entry=100.0, current=101.5, tp=101.2, sl=99.0)
    res = evaluate_and_apply_position_risk(client, pos, current_funding_daily=-0.006)

    assert res is not None
    assert res["action"] == "CLOSE"
    assert res["detail"] == "TP breached"
    client.preview_close.assert_called_once_with(pos.contract_id)
    client.preview_auto_close.assert_not_called()


# -----------------------------------------------------------------------------
# 4. Hysteresis funding flip (Poin 9)
# -----------------------------------------------------------------------------

def test_funding_flip_hysteresis_closes_only_at_or_above_threshold(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    assert FUNDING_FLIP_CLOSE_THRESHOLD == pytest.approx(0.001)

    # Flip >= 0.1%/hari pada LONG (funding positif +0.15%/hari) -> CLOSE
    client_close = _make_mock_client()
    pos = _make_pos(side="long", entry=100.0, current=100.2, tp=101.2, sl=99.0)
    res_close = evaluate_and_apply_position_risk(client_close, pos, current_funding_daily=0.0015)
    assert res_close is not None
    assert res_close["action"] == "CLOSE"
    client_close.preview_close.assert_called_once()

    # Flip kecil < 0.1%/hari pada LONG (funding positif +0.05%/hari) -> TIDAK CLOSE, geser SL ke breakeven (100.0)
    client_be = _make_mock_client()
    res_be = evaluate_and_apply_position_risk(client_be, pos, current_funding_daily=0.0005)
    assert res_be is not None
    assert res_be["action"] == "UPDATE_AUTO_CLOSE"
    assert res_be["sl"] == pytest.approx(100.0)
    client_be.preview_close.assert_not_called()

    # Uji juga di apply_layer2_funding
    client_l2 = _make_mock_client()
    apply_layer2_funding(client_l2, pos, current_funding_daily=0.0005)
    client_l2.preview_close.assert_not_called()
    client_l2.preview_auto_close.assert_called_once_with(pos.contract_id, sl=100.0)


# -----------------------------------------------------------------------------
# 5. Trailing tiers baru, Pinch Lock, & Instant Market-TP (Opsi 3 Hybrid)
# -----------------------------------------------------------------------------

def test_new_trailing_tiers_pinch_lock_and_instant_market_tp(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    assert TRAILING_TIERS == [
        (0.0025, "lock_0010"),
        (0.0035, "lock_0022"),
    ]

    # Tier 1: profit +0.28% (>= 0.25%) pada LONG -> SL di entry +0.10% (100.10)
    client1 = _make_mock_client()
    pos_t1_long = _make_pos(side="long", entry=100.0, current=100.28, tp=100.75, sl=99.0)
    res1 = evaluate_and_apply_position_risk(client1, pos_t1_long, current_funding_daily=-0.006)
    assert res1 is not None
    assert res1["action"] == "UPDATE_AUTO_CLOSE"
    assert res1["sl"] == pytest.approx(100.10)

    # Tier 2 Pinch Lock: profit +0.40% (>= 0.35% & < 0.50%) pada LONG -> SL di 100.22 & TP dirapatkan ke 100.50
    client2 = _make_mock_client()
    pos_t2_long = _make_pos(side="long", entry=100.0, current=100.40, tp=100.75, sl=100.10)
    res2 = evaluate_and_apply_position_risk(client2, pos_t2_long, current_funding_daily=-0.006)
    assert res2 is not None
    assert res2["action"] == "UPDATE_AUTO_CLOSE"
    assert res2["sl"] == pytest.approx(100.22)
    assert res2["tp"] == pytest.approx(100.50)

    # Tier 2 Pinch Lock pada SHORT: profit +0.40% (current=99.60) -> SL di 99.78 & TP dirapatkan ke 99.50
    client3 = _make_mock_client()
    pos_t2_short = _make_pos(side="short", entry=100.0, current=99.60, tp=99.25, sl=101.0)
    res3 = evaluate_and_apply_position_risk(client3, pos_t2_short, current_funding_daily=0.006)
    assert res3 is not None
    assert res3["action"] == "UPDATE_AUTO_CLOSE"
    assert res3["sl"] == pytest.approx(99.78)
    assert res3["tp"] == pytest.approx(99.50)

    # Tier 3 Instant Market-TP: profit +0.52% (>= 0.50%) tapi belum lewati TP bursa 100.75 -> langsung CLOSE!
    client_itp = _make_mock_client()
    pos_itp = _make_pos(side="long", entry=100.0, current=100.52, tp=100.75, sl=100.22)
    res_itp = evaluate_and_apply_position_risk(client_itp, pos_itp, current_funding_daily=-0.006)
    assert res_itp is not None
    assert res_itp["action"] == "CLOSE"
    assert res_itp["reason"] == "Instant Market-TP"
    client_itp.preview_close.assert_called_once_with(pos_itp.contract_id)

    # apply_layer4_trailing wrapper
    client4 = _make_mock_client()
    apply_layer4_trailing(client4, pos_t1_long)
    client4.preview_auto_close.assert_called_once_with(pos_t1_long.contract_id, sl=100.10)


# -----------------------------------------------------------------------------
# 6. Tidak ada jalur kode yang menghasilkan SL lebih longgar dari 1% dari entry
# -----------------------------------------------------------------------------

def test_no_code_path_produces_sl_wider_than_1_percent():
    client = _make_mock_client()
    pos = _make_pos(side="long", entry=100.0, current=97.5, tp=101.2, sl=99.0)
    # apply_layer3_volatility harus no-op (None)
    assert apply_layer3_volatility(client, pos) is None
    assert "deprecated" in (apply_layer3_volatility.__doc__ or "").lower()

    for price in (99.1, 99.5, 99.9, 100.0, 100.2, 100.5, 100.7):
        for funding in (-0.010, -0.005, -0.0005, 0.0, 0.0005):
            c = _make_mock_client()
            p_long = _make_pos(side="long", entry=100.0, current=price, tp=101.2, sl=None)
            r_long = evaluate_and_apply_position_risk(c, p_long, current_funding_daily=funding)
            if r_long and r_long.get("action") == "UPDATE_AUTO_CLOSE" and r_long.get("sl") is not None:
                assert r_long["sl"] >= 99.0
                assert r_long["sl"] < price

            c_s = _make_mock_client()
            p_short = _make_pos(side="short", entry=100.0, current=price, tp=98.8, sl=None)
            r_short = evaluate_and_apply_position_risk(c_s, p_short, current_funding_daily=-funding)
            if r_short and r_short.get("action") == "UPDATE_AUTO_CLOSE" and r_short.get("sl") is not None:
                assert r_short["sl"] <= 101.0
                assert r_short["sl"] > price


# -----------------------------------------------------------------------------
# 7. calculate_dynamic_margin & get_total_equity
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("equity", [10.0, 15.56, 25.0, 50.0, 100.0, 250.0, 500.0])
def test_calculate_dynamic_margin_bounds(equity):
    opp = SimpleNamespace(funding_daily=0.010, base_asset="BTC")
    res = SimpleNamespace(final_score=0.80, overall_consensus_level="UNANIMOUS")

    for active in (0, 2, 4):
        for v_mult in (0.5, 1.0):
            m = calculate_dynamic_margin(
                available_balance=equity * 0.8,
                active_slots=active,
                opp=opp,
                research=res,
                verdict_mult=v_mult,
                total_equity=equity,
            )
            assert m >= DEFAULT_MARGIN
            expected_cap = max(DEFAULT_MARGIN, round(equity * 0.15, 2))
            assert m <= expected_cap


def test_get_total_equity_sums_available_and_open_margins():
    balance_data = {"accounts": [{"balance_available": "10.50"}]}
    positions = {
        "positions": [
            {"contract_id": 1, "status": "open", "margin_quote": "2.50"},
            {"contract_id": 2, "status": "open", "margin_quote": "2.56"},
            {"contract_id": 3, "status": "closed", "margin_quote": "5.00"},
        ]
    }
    assert get_total_equity(balance_data, positions) == pytest.approx(15.56)


# -----------------------------------------------------------------------------
# 8. log_execution menghasilkan tepat 12 kolom
# -----------------------------------------------------------------------------

def test_log_execution_writes_exact_12_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_WEBHOOK_URL", "")
    csv_file = tmp_path / "executions_history.csv"

    executions = [
        {
            "asset": "NVDA",
            "side": "short",
            "leverage": 2,
            "margin": 2.50,
            "price": 140.25,
            "tp": 138.57,
            "sl": 141.65,
            "order_id": "ord_abc",
            "verdict": "EXECUTED",
            "reason": "Swarm Strong Consensus",
        },
        {
            "asset": "LIT",
            "side": "long",
            "leverage": 2,
            "verdict": "SKIP",
            "reason": "bearish news",
        },
    ]

    log_execution(executions, filepath=str(csv_file))

    with open(csv_file, mode="r", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    assert len(rows) == 3  # 1 header + 2 data rows
    expected_headers = [
        "Timestamp (UTC)", "Asset", "Sisi", "Order Type", "Leverage", "Margin ($)",
        "Entry Price ($)", "TP ($)", "SL ($)", "Order ID", "Status", "Keterangan"
    ]
    assert rows[0] == expected_headers
    for row in rows[1:]:
        assert len(row) == 12
    assert rows[1][1:12] == [
        "NVDA", "SHORT", "MARKET", "2", "2.5", "140.25", "138.57", "141.65", "ord_abc", "EXECUTED", "Swarm Strong Consensus"
    ]
    assert rows[2][1:4] == ["LIT", "LONG", "MARKET"]
    assert rows[2][10:12] == ["SKIP", "bearish news"]
