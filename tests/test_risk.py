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
# 1. compute_baseline_tpsl: SL 1.5% di semua aset, TP 2.0%/2.5%, Windfall TP
# -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "asset,expected_tp_pct",
    [
        ("SPY", 0.020),
        ("US100", 0.020),
        ("AAPL", 0.020),
        ("BTC", 0.020),
        ("SOL", 0.020),
        ("XAU", 0.025),
        ("XAG", 0.025),
        ("NVDA", 0.025),
        ("NBIS", 0.025),
        (None, 0.025),
    ],
)
def test_baseline_tpsl_sl_is_always_1_5_percent_and_asymmetric_tp(asset, expected_tp_pct):
    tp_pct, sl_pct = get_asset_baseline_tpsl_pct(asset)
    assert sl_pct == pytest.approx(0.015)
    assert tp_pct == pytest.approx(expected_tp_pct)

    entry = 100.0
    tp_long, sl_long = compute_baseline_tpsl("long", entry, asset=asset, funding_daily=0.0)
    assert sl_long == pytest.approx(98.5)
    assert tp_long == pytest.approx(round(entry * (1 + expected_tp_pct), 4))

    tp_short, sl_short = compute_baseline_tpsl("short", entry, asset=asset, funding_daily=0.0)
    assert sl_short == pytest.approx(101.5)
    assert tp_short == pytest.approx(round(entry * (1 - expected_tp_pct), 4))


def test_windfall_tp_advances_tp_when_25x_funding_between_15_and_baseline():
    # NVDA baseline TP = 2.5% (0.025). Funding 0.7%/hari -> windfall_tp = 2.5 * 0.7% = 1.75% (0.0175) < 0.025
    tp_long, sl_long = compute_baseline_tpsl("long", 100.0, asset="NVDA", funding_daily=-0.007)
    assert tp_long == pytest.approx(101.75)
    assert sl_long == pytest.approx(98.5)

    tp_short, sl_short = compute_baseline_tpsl("short", 100.0, asset="NVDA", funding_daily=0.007)
    assert tp_short == pytest.approx(98.25)
    assert sl_short == pytest.approx(101.5)


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

def test_guard_migrates_old_sl_and_closes_when_sl_15pct_breached_long(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    client = _make_mock_client()

    # Posisi lama LONG dengan SL 3% (97.0), harga sekarang 98.2 (rugi 1.8% > 1.5%)
    pos = _make_pos(side="long", entry=100.0, current=98.2, tp=102.5, sl=97.0)
    res = evaluate_and_apply_position_risk(client, pos, current_funding_daily=-0.006)

    assert res is not None
    assert res["action"] == "CLOSE"
    assert res["detail"] == "SL 1.5% breached (migrasi)"
    client.preview_close.assert_called_once_with(pos.contract_id)
    client.preview_auto_close.assert_not_called()


def test_guard_closes_when_sl_15pct_breached_short(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    client = _make_mock_client()

    # Posisi lama SHORT dengan SL 3% (103.0), harga sekarang 101.7 (rugi 1.7% > 1.5%)
    pos = _make_pos(side="short", entry=100.0, current=101.7, tp=97.5, sl=103.0)
    res = evaluate_and_apply_position_risk(client, pos, current_funding_daily=0.006)

    assert res is not None
    assert res["action"] == "CLOSE"
    assert res["detail"] == "SL 1.5% breached (migrasi)"
    client.preview_close.assert_called_once_with(pos.contract_id)
    client.preview_auto_close.assert_not_called()


def test_guard_tightens_old_sl_to_15pct_when_loss_under_15pct():
    client = _make_mock_client()

    # Posisi lama LONG dengan SL 2.5% (97.5), harga sekarang 99.1 (rugi 0.9% < 1.5%)
    pos = _make_pos(side="long", entry=100.0, current=99.1, tp=102.5, sl=97.5)
    res = evaluate_and_apply_position_risk(client, pos, current_funding_daily=-0.006)

    assert res is not None
    assert res["action"] == "UPDATE_AUTO_CLOSE"
    assert res["sl"] == pytest.approx(98.5)
    assert res["sl"] < pos.current_price
    client.preview_close.assert_not_called()


def test_guard_closes_when_tp_breached(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    client = _make_mock_client()

    pos = _make_pos(side="long", entry=100.0, current=102.6, tp=102.5, sl=98.5)
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
# 5. Trailing tiers True Zero-Risk (+0.35% / +0.85% / Trailing 0.60%)
# -----------------------------------------------------------------------------

def test_true_zero_risk_trailing_tiers():
    assert TRAILING_TIERS == [
        (0.0080, "lock_0035"),
        (0.0140, "lock_0085"),
        (0.0200, 0.0060),
    ]

    # Tier 1: profit +0.85% (>= 0.80%) pada LONG -> SL di entry +0.35% (100.35 — surplus bersih setelah fee 0.18%)
    client1 = _make_mock_client()
    pos_t1_long = _make_pos(side="long", entry=100.0, current=100.85, tp=102.5, sl=98.5)
    res1 = evaluate_and_apply_position_risk(client1, pos_t1_long, current_funding_daily=-0.006)
    assert res1 is not None
    assert res1["action"] == "UPDATE_AUTO_CLOSE"
    assert res1["sl"] == pytest.approx(100.35)

    # Tier 2: profit +1.50% (>= 1.40%) pada LONG -> SL di entry +0.85% (100.85)
    client2 = _make_mock_client()
    pos_t2_long = _make_pos(side="long", entry=100.0, current=101.50, tp=102.5, sl=100.35)
    res2 = evaluate_and_apply_position_risk(client2, pos_t2_long, current_funding_daily=-0.005)
    assert res2 is not None
    assert res2["action"] == "UPDATE_AUTO_CLOSE"
    assert res2["sl"] == pytest.approx(100.85)

    # Tier 2 pada SHORT: profit +1.50% (current=98.50) -> SL di entry -0.85% (99.15)
    client3 = _make_mock_client()
    pos_t2_short = _make_pos(side="short", entry=100.0, current=98.50, tp=97.5, sl=101.5)
    res3 = evaluate_and_apply_position_risk(client3, pos_t2_short, current_funding_daily=0.005)
    assert res3 is not None
    assert res3["action"] == "UPDATE_AUTO_CLOSE"
    assert res3["sl"] == pytest.approx(99.15)

    # Tier 3 Trailing 0.60%: profit +2.10% (current=102.10) -> SL di 102.10 * (1 - 0.006) = 101.4874
    client_t3 = _make_mock_client()
    pos_t3 = _make_pos(side="long", entry=100.0, current=102.10, tp=102.5, sl=100.85)
    res_t3 = evaluate_and_apply_position_risk(client_t3, pos_t3, current_funding_daily=-0.005)
    assert res_t3 is not None
    assert res_t3["action"] == "UPDATE_AUTO_CLOSE"
    assert res_t3["sl"] == pytest.approx(101.4874)

    # apply_layer4_trailing wrapper
    client4 = _make_mock_client()
    apply_layer4_trailing(client4, pos_t1_long)
    client4.preview_auto_close.assert_called_once_with(pos_t1_long.contract_id, sl=100.35)


# -----------------------------------------------------------------------------
# 6. Tidak ada jalur kode yang menghasilkan SL lebih longgar dari 1.5% dari entry
# -----------------------------------------------------------------------------

def test_no_code_path_produces_sl_wider_than_1_5_percent():
    client = _make_mock_client()
    pos = _make_pos(side="long", entry=100.0, current=97.5, tp=102.5, sl=98.5)
    # apply_layer3_volatility harus no-op (None)
    assert apply_layer3_volatility(client, pos) is None
    assert "deprecated" in (apply_layer3_volatility.__doc__ or "").lower()

    for price in (98.6, 99.1, 99.5, 100.0, 100.5, 101.0, 101.8):
        for funding in (-0.010, -0.005, -0.0005, 0.0, 0.0005):
            c = _make_mock_client()
            p_long = _make_pos(side="long", entry=100.0, current=price, tp=102.5, sl=None)
            r_long = evaluate_and_apply_position_risk(c, p_long, current_funding_daily=funding)
            if r_long and r_long.get("action") == "UPDATE_AUTO_CLOSE" and r_long.get("sl") is not None:
                assert r_long["sl"] >= 98.5
                assert r_long["sl"] < price

            c_s = _make_mock_client()
            p_short = _make_pos(side="short", entry=100.0, current=price, tp=97.5, sl=None)
            r_short = evaluate_and_apply_position_risk(c_s, p_short, current_funding_daily=-funding)
            if r_short and r_short.get("action") == "UPDATE_AUTO_CLOSE" and r_short.get("sl") is not None:
                assert r_short["sl"] <= 101.5
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
            expected_cap = max(DEFAULT_MARGIN, round(equity * 0.15, 2))
            if v_mult < 1.0 and m == 0.0:
                continue  # E1: HALF di bawah floor exchange ($2.50) dikembalikan sebagai 0.0 (SKIP)
            assert m >= DEFAULT_MARGIN
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
        "Entry Price ($)", "TP ($)", "SL ($)", "Order ID", "Status", "Keterangan",
        "Strategy Version"
    ]
    assert rows[0] == expected_headers
    for row in rows[1:]:
        assert len(row) == 13
        assert len(row[12]) == 8
    assert rows[1][1:12] == [
        "NVDA", "SHORT", "MARKET", "2", "2.5", "140.25", "138.57", "141.65", "ord_abc", "EXECUTED", "Swarm Strong Consensus"
    ]
    assert rows[2][1:4] == ["LIT", "LONG", "MARKET"]
    assert rows[2][10:12] == ["SKIP", "bearish news"]


# -----------------------------------------------------------------------------
# 9. Dynamic Adaptive Risk Monitor Cadence (5 menit vs 10 menit / 6x-12x per jam)
# -----------------------------------------------------------------------------

def test_dynamic_monitor_cadence_rules():
    from walt_bot import decide_dynamic_monitor_cadence

    # 1. 0 posisi aktif -> Smart Idle Mode
    sub, delay, reason = decide_dynamic_monitor_cadence([])
    assert sub is False
    assert delay == 0
    assert "Idle" in reason

    # 2. >= 3 posisi aktif -> selalu mode 5 menit (12x/jam)
    p1 = _make_pos(side="long", entry=100.0, current=100.05, tp=100.75, sl=99.0, asset="NVDA")
    p2 = _make_pos(side="long", entry=100.0, current=100.05, tp=100.60, sl=99.0, asset="BTC")
    p3 = _make_pos(side="short", entry=100.0, current=99.95, tp=99.35, sl=101.0, asset="XAU")
    p2.contract_id = 2
    p3.contract_id = 3
    sub3, delay3, reason3 = decide_dynamic_monitor_cadence([p1, p2, p3], {1: "GO", 2: "GO", 3: "GO"})
    assert sub3 is True
    assert 240 <= delay3 <= 295
    assert "Multi-Position" in reason3

    # 3. Kategori HALF (walaupun hanya 1 posisi & harga tenang) -> selalu mode 5 menit
    sub_half, delay_half, reason_half = decide_dynamic_monitor_cadence([p1], {1: "HALF"})
    assert sub_half is True
    assert 240 <= delay_half <= 295
    assert "HALF" in reason_half

    # 4. Dekat TP / Trailing (profit >= +0.70%) pada posisi GO -> mode 5 menit
    p_near_tp = _make_pos(side="long", entry=100.0, current=100.75, tp=102.5, sl=98.5, asset="NVDA")
    sub_tp, delay_tp, reason_tp = decide_dynamic_monitor_cadence([p_near_tp], {1: "GO"})
    assert sub_tp is True
    assert "Proximity TP" in reason_tp

    # 5. Dekat SL (loss <= -1.00%) pada posisi GO -> mode 5 menit
    p_near_sl = _make_pos(side="long", entry=100.0, current=98.95, tp=102.5, sl=98.5, asset="NVDA")
    sub_sl, delay_sl, reason_sl = decide_dynamic_monitor_cadence([p_near_sl], {1: "GO"})
    assert sub_sl is True
    assert "Proximity SL" in reason_sl

    # 6. Kategori GO tenang (< 3 posisi): tetap 10 menit (hemat API & tidak spam)
    sub_go_10, delay_go_10, reason_go_10 = decide_dynamic_monitor_cadence([p1], {1: "GO"})
    assert sub_go_10 is False
    assert delay_go_10 == 0
    assert "10 menit" in reason_go_10


# -----------------------------------------------------------------------------
# 10. Adaptive Execution Gear (60m / 120m) & Cooldown Re-Entry 3 Jam
# -----------------------------------------------------------------------------

def test_evaluate_execution_gear_and_cooldown(tmp_path):
    from walt_bot import Opportunity, evaluate_execution_gear, _set_asset_cooldown, _is_asset_in_cooldown

    def _opp(asset: str, funding: float) -> Opportunity:
        return Opportunity(
            contract_id=1,
            base_asset=asset,
            funding_daily=funding,
            volume_24h=5_000_000.0,
            max_leverage=2,
            side="short" if funding > 0 else "long",
        )

    # 1. Gear 2 Aktif (60m): >= 3 peluang >= 0.5%/hari atau >= 2 ekstrem >= 0.8%/hari
    opps_many = [_opp("NVDA", 0.006), _opp("SNDK", 0.0055), _opp("MSTR", 0.006)]
    gear, interval, _ = evaluate_execution_gear(opps_many)
    assert gear == "GEAR_2_ACTIVE_60M"
    assert interval == 60

    opps_extreme = [_opp("NVDA", 0.009), _opp("SNDK", 0.0085)]
    gear_ex, interval_ex, _ = evaluate_execution_gear(opps_extreme)
    assert gear_ex == "GEAR_2_ACTIVE_60M"
    assert interval_ex == 60

    # 2. Gear 1 Konservatif (120m / 2 jam): <= 2 peluang >= 0.5%/hari dan < 2 ekstrem
    opps_quiet = [_opp("AAPL", 0.0051), _opp("BTC", 0.0052)]
    gear_120, interval_120, _ = evaluate_execution_gear(opps_quiet)
    assert gear_120 == "GEAR_1_CONSERVATIVE_120M"
    assert interval_120 == 120

    # 3. Cooldown Re-Entry 3 Jam
    regime_file = str(tmp_path / "market_regime.json")
    in_cd, _ = _is_asset_in_cooldown("NVDA", regime_path=regime_file)
    assert in_cd is False

    _set_asset_cooldown("NVDA", cooldown_sec=10800, regime_path=regime_file)
    in_cd_after, rem_min = _is_asset_in_cooldown("NVDA", regime_path=regime_file)
    assert in_cd_after is True
    assert rem_min > 170.0
