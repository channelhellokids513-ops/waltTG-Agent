"""Unit tests untuk SL/TP dinamis berbasis ATR 24h, trailing proporsional, L2/off-hours buffer, & round_price."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import price_forecast
import walt_bot
from walt_bot import (
    Opportunity,
    Position,
    apply_layer2_funding,
    compute_dynamic_tpsl,
    evaluate_and_apply_position_risk,
    evaluate_off_hours_position_policy,
    round_price,
)


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    client.preview_order.return_value = {"ok": True, "confirmationToken": "tok_open"}
    client.preview_auto_close.return_value = {"ok": True, "confirmationToken": "tok_ac"}
    client.preview_close.return_value = {"ok": True, "confirmationToken": "tok_close"}
    client.execute.return_value = {"ok": True, "order": {"order_id": "ord_dyn_1"}}
    return client


def test_compute_dynamic_tpsl_scales_with_atr_and_respects_fixed_killswitch(monkeypatch):
    monkeypatch.setenv("DYNAMIC_SL_MODE", "atr")

    # 1. ATR rendah (1.0%) -> pakai floor 1.5%
    tp1, sl1, tp1_pct, sl1_pct = compute_dynamic_tpsl("long", 100.0, asset="SOL", atr_24h_pct=0.010)
    assert sl1_pct == pytest.approx(0.015)
    assert tp1_pct == pytest.approx(0.0203)  # max(2.0%, 1.35 * 1.5%)
    assert sl1 == pytest.approx(98.5)

    # 2. ATR menengah (2.8%) -> 0.85 * 2.8% = 2.38%, TP = 1.35 * 2.38% = 3.21%
    tp2, sl2, tp2_pct, sl2_pct = compute_dynamic_tpsl("long", 100.0, asset="SOL", atr_24h_pct=0.028)
    assert sl2_pct == pytest.approx(0.0238)
    assert tp2_pct == pytest.approx(round(1.35 * 0.0238, 4))
    assert sl2 == pytest.approx(97.62)
    assert tp2 == pytest.approx(round(100.0 * (1.0 + tp2_pct), 4))

    # 3. ATR ekstrem (6.0%) -> plafon 2.0% untuk aset kalem (SPY), 2.5% untuk volatil (SOL/NVDA)
    #    K3-3 (2025-11-25): plafon diturunkan dari 2.5%/3.5% agar risiko per trade <= 0.75% ekuitas.
    _, sl_calm, _, sl_calm_pct = compute_dynamic_tpsl("long", 100.0, asset="SPY", atr_24h_pct=0.060)
    assert sl_calm_pct == pytest.approx(0.020)
    assert sl_calm == pytest.approx(98.0)

    _, sl_vol, tp_vol_pct, sl_vol_pct = compute_dynamic_tpsl("short", 100.0, asset="NVDA", atr_24h_pct=0.060)
    assert sl_vol_pct == pytest.approx(0.025)
    assert tp_vol_pct == pytest.approx(round(1.35 * 0.025, 4))
    assert sl_vol == pytest.approx(102.5)

    # 4. Fallback saat ATR tidak tersedia: kalem 1.5%, crypto 2.2%, saham volatil 2.5%
    assert compute_dynamic_tpsl("long", 100.0, asset="XAU", atr_24h_pct=None)[3] == pytest.approx(0.015)
    assert compute_dynamic_tpsl("long", 100.0, asset="ETH", atr_24h_pct=None)[3] == pytest.approx(0.022)
    assert compute_dynamic_tpsl("long", 100.0, asset="TSLA", atr_24h_pct=None)[3] == pytest.approx(0.025)

    # 5. Kill switch DYNAMIC_SL_MODE=fixed -> kembali ke baseline 1.5% seragam
    monkeypatch.setenv("DYNAMIC_SL_MODE", "fixed")
    _, sl_fix, _, sl_fix_pct = compute_dynamic_tpsl("long", 100.0, asset="NVDA", atr_24h_pct=0.050)
    assert sl_fix_pct == pytest.approx(0.015)
    assert sl_fix == pytest.approx(98.5)


def test_proportional_trailing_tiers_give_breathing_room():
    # Posisi ATR dinamis dengan planned_sl_pct = 3.0% (t1 = 1.65%, lock1 = 0.60%, t2 = 2.70%, lock2 = 1.50%, t3 = 3.60%, trail3 = 1.20%)
    pos_low = Position(
        contract_id=1, base_asset="SOL", side="long", leverage=2, margin=3.0,
        entry_price=100.0, current_price=101.20, tp=104.05, sl=97.0,
        funding_paid=0.0, liquidation_price=50.0, planned_sl_pct=0.030, planned_tp_pct=0.0405,
    )
    c1 = _make_mock_client()
    assert evaluate_and_apply_position_risk(c1, pos_low, current_funding_daily=-0.006) is None

    pos_t1 = Position(
        contract_id=1, base_asset="SOL", side="long", leverage=2, margin=3.0,
        entry_price=100.0, current_price=101.70, tp=104.05, sl=97.0,
        funding_paid=0.0, liquidation_price=50.0, planned_sl_pct=0.030, planned_tp_pct=0.0405,
    )
    r1 = evaluate_and_apply_position_risk(_make_mock_client(), pos_t1, current_funding_daily=-0.006)
    assert r1 is not None and r1["sl"] == pytest.approx(100.60)

    pos_t3 = Position(
        contract_id=1, base_asset="SOL", side="long", leverage=2, margin=3.0,
        entry_price=100.0, current_price=103.70, tp=104.05, sl=101.50,
        funding_paid=0.0, liquidation_price=50.0, planned_sl_pct=0.030, planned_tp_pct=0.0405,
    )
    r3 = evaluate_and_apply_position_risk(_make_mock_client(), pos_t3, current_funding_daily=-0.006)
    assert r3 is not None and r3["sl"] == pytest.approx(round(103.70 * (1.0 - 0.012), 4))


def test_l2_breakeven_and_off_hours_early_lock_require_buffer():
    # L2 Breakeven: profit +0.20% (< +0.50%) TIDAK menggeser SL ke 100.0
    pos_tiny = Position(
        contract_id=2, base_asset="BTC", side="long", leverage=2, margin=3.0,
        entry_price=100.0, current_price=100.20, tp=103.0, sl=97.8,
        funding_paid=0.0, liquidation_price=50.0, planned_sl_pct=0.022, planned_tp_pct=0.030,
    )
    assert evaluate_and_apply_position_risk(_make_mock_client(), pos_tiny, current_funding_daily=0.0005) is None
    assert apply_layer2_funding(_make_mock_client(), pos_tiny, current_funding_daily=0.0005) is None

    # Profit +0.60% (>= +0.50%) -> geser SL ke breakeven 100.0
    pos_safe = Position(
        contract_id=2, base_asset="BTC", side="long", leverage=2, margin=3.0,
        entry_price=100.0, current_price=100.60, tp=103.0, sl=97.8,
        funding_paid=0.0, liquidation_price=50.0, planned_sl_pct=0.022, planned_tp_pct=0.030,
    )
    r_be = evaluate_and_apply_position_risk(_make_mock_client(), pos_safe, current_funding_daily=0.0005)
    assert r_be is not None and r_be["sl"] == pytest.approx(100.0)

    # Off-Hours Early Lock: profit +0.45% (< +0.70%) belum mengunci +0.25%; +0.75% mengunci
    mon_pre = datetime(2026, 11, 23, 20, 45, tzinfo=timezone.utc)
    pos_oh_early = Position(
        contract_id=3, base_asset="NVDA", side="long", leverage=2, margin=3.0,
        entry_price=100.0, current_price=100.45, tp=103.5, sl=97.5,
        funding_paid=0.0, liquidation_price=50.0,
    )
    assert evaluate_off_hours_position_policy(pos_oh_early, current_funding_daily=-0.003, now_utc=mon_pre)["early_lock_sl"] is None

    pos_oh_ok = Position(
        contract_id=3, base_asset="NVDA", side="long", leverage=2, margin=3.0,
        entry_price=100.0, current_price=100.75, tp=103.5, sl=97.5,
        funding_paid=0.0, liquidation_price=50.0,
    )
    assert evaluate_off_hours_position_policy(pos_oh_ok, current_funding_daily=-0.003, now_utc=mon_pre)["early_lock_sl"] == pytest.approx(100.25)


def test_round_price_and_engine1_dynamic_sl_meta_persistence(monkeypatch):
    assert round_price(150.123456) == 150.1235
    assert round_price(0.25123456) == 0.251235
    assert round_price(0.0000123456) == 0.00001235

    # Memecoin sub-sen di Engine 2: SL/TP tetap presisi 8 desimal & berbeda dari entry
    opp_meme = Opportunity(
        contract_id=44, base_asset="PEPE", funding_daily=0.0, volume_24h=20_000_000.0,
        max_leverage=2, side="long", min_size_base=1000.0, price_slippage=0.0005,
        signal_source="price_forecast", forecast_setup="V5_4H_REJ_COMFORT",
        planned_sl_pct=0.035, planned_tp_pct=0.035, max_hold_hours=72.0,
    )
    c_meme = _make_mock_client()
    c_meme.latest_rate.return_value = {"rate": "0.000012"}
    res_meme = walt_bot.open_position(c_meme, opp_meme, margin=3.0, leverage=2, resolved=(3.0, 0.000012))
    assert res_meme is not None
    assert res_meme["sl"] < 0.000012 < res_meme["tp"]
    assert res_meme["sl"] == pytest.approx(round(0.000012 * 0.965, 8))

    # Persistensi dynamic SL Engine 1 -> risk-monitor tidak memangkas balik ke 1.5%
    monkeypatch.setattr(walt_bot, "_estimate_live_atr_24h_pct", lambda asset: 0.033)
    opp_e1 = Opportunity(
        contract_id=55, base_asset="SOL", funding_daily=0.008, volume_24h=30_000_000.0,
        max_leverage=2, side="short", min_size_base=0.01, price_slippage=0.0005,
    )
    c_e1 = _make_mock_client()
    c_e1.latest_rate.return_value = {"rate": "150.0"}
    res_e1 = walt_bot.open_position(c_e1, opp_e1, margin=3.0, leverage=2, resolved=(3.0, 150.0))
    assert res_e1 is not None
    # K3-3: ATR 3.3% x 0.85 = 2.81% DIPANGKAS ke plafon volatil 2.5% (bukan 2.81%).
    assert res_e1["sl_pct"] == pytest.approx(0.025)
    walt_bot._record_forecast_position_meta(opp_e1.contract_id, opp_e1, sl_pct=res_e1["sl_pct"], tp_pct=res_e1["tp_pct"])
    loaded = walt_bot._load_forecast_position_meta()["55"]
    assert loaded["signal_source"] == "funding"
    assert loaded["planned_sl_pct"] == pytest.approx(res_e1["sl_pct"])


def test_estimate_asset_atr_24h_pct_computes_positive_fraction():
    candles = []
    px = 100.0
    for i in range(120):
        step = 0.008 if i % 2 == 0 else -0.0075
        o = px
        c = o * (1.0 + step)
        candles.append({
            "t": 1763500000000 + i * 3_600_000,
            "o": o, "h": max(o, c) * 1.003, "l": min(o, c) * 0.997, "c": c, "v": 2000.0,
        })
        px = c
    atr = price_forecast.estimate_asset_atr_24h_pct("SOL", asset_class="crypto", fetcher=lambda *a: candles)
    assert atr is not None and atr > 0.005
