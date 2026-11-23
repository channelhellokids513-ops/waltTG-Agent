"""Unit tests untuk Kebijakan Menahan Posisi Saat Pasar Tutup (Hybrid Cost-Aware Opsi B)."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import walt_bot
from walt_bot import (
    Position,
    _run_single_monitor_pass,
    classify_asset_market_session,
    evaluate_and_apply_position_risk,
    evaluate_off_hours_position_policy,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_client() -> MagicMock:
    client = MagicMock()
    client.preview_auto_close.return_value = {"ok": True, "confirmationToken": "tok_ac"}
    client.preview_close.return_value = {"ok": True, "confirmationToken": "tok_close"}
    client.execute.return_value = {"ok": True, "order": {"order_id": "ord_offhours"}}
    return client


def _make_pos(
    asset: str = "NVDA",
    side: str = "long",
    entry: float = 100.0,
    current: float = 100.0,
    tp: float | None = 102.5,
    sl: float | None = 98.5,
) -> Position:
    return Position(
        contract_id=42,
        base_asset=asset,
        side=side,  # type: ignore[arg-type]
        leverage=2,
        margin=3.0,
        entry_price=entry,
        current_price=current,
        tp=tp,
        sl=sl,
        funding_paid=0.02,
        liquidation_price=50.0 if side == "long" else 150.0,
        position_id=f"{asset}_{side}_42",
    )


# ---------------------------------------------------------------------------
# 1. Klasifikasi Sesi Pasar per Kelas Aset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "asset,dt_utc,expected_session",
    [
        # Saham AS (ALLOWED_STOCKS)
        ("NVDA", datetime(2026, 11, 23, 13, 30, tzinfo=timezone.utc), "OPEN"),
        ("NVDA", datetime(2026, 11, 23, 20, 34, tzinfo=timezone.utc), "OPEN"),
        ("NVDA", datetime(2026, 11, 23, 20, 35, tzinfo=timezone.utc), "PRE_CLOSE_OVERNIGHT"),
        ("NVDA", datetime(2026, 11, 26, 20, 59, tzinfo=timezone.utc), "PRE_CLOSE_OVERNIGHT"),
        ("NVDA", datetime(2026, 11, 26, 21, 0, tzinfo=timezone.utc), "CLOSED_OVERNIGHT"),
        ("NVDA", datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc), "CLOSED_OVERNIGHT"),
        ("NVDA", datetime(2026, 11, 27, 20, 40, tzinfo=timezone.utc), "PRE_CLOSE_WEEKEND"),
        ("NVDA", datetime(2026, 11, 27, 21, 0, tzinfo=timezone.utc), "CLOSED_WEEKEND"),
        ("NVDA", datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc), "CLOSED_WEEKEND"),
        ("NVDA", datetime(2026, 11, 29, 18, 0, tzinfo=timezone.utc), "CLOSED_WEEKEND"),
        ("NVDA", datetime(2026, 11, 23, 13, 29, tzinfo=timezone.utc), "CLOSED_WEEKEND"),
        # Komoditas (ALLOWED_COMMODITIES): overnight Mon-Thu OPEN, khusus weekend ditutup
        ("XAU", datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc), "OPEN"),
        ("XAG", datetime(2026, 11, 25, 20, 45, tzinfo=timezone.utc), "OPEN"),
        ("BRENTOIL", datetime(2026, 11, 27, 20, 45, tzinfo=timezone.utc), "PRE_CLOSE_WEEKEND"),
        ("XAU", datetime(2026, 11, 27, 21, 0, tzinfo=timezone.utc), "CLOSED_WEEKEND"),
        ("XAU", datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc), "CLOSED_WEEKEND"),
        ("XAU", datetime(2026, 11, 29, 21, 59, tzinfo=timezone.utc), "CLOSED_WEEKEND"),
        ("XAU", datetime(2026, 11, 29, 22, 0, tzinfo=timezone.utc), "OPEN"),
        # Crypto (ALLOWED_CRYPTO): selalu OPEN 24/7
        ("BTC", datetime(2026, 11, 27, 20, 45, tzinfo=timezone.utc), "OPEN"),
        ("SOL", datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc), "OPEN"),
        ("ETH", datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc), "OPEN"),
    ],
)
def test_classify_asset_market_session_boundaries(asset, dt_utc, expected_session):
    session, _note = classify_asset_market_session(asset, now_utc=dt_utc)
    assert session == expected_session, f"{asset} @ {dt_utc.isoformat()} -> {session}"


# ---------------------------------------------------------------------------
# 2. Zero-Tolerance Biaya Inap (< 0%) saat Pasar Tutup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dt_utc",
    [
        datetime(2026, 11, 24, 2, 0, tzinfo=timezone.utc),   # CLOSED_OVERNIGHT (Selasa 02:00 UTC)
        datetime(2026, 11, 28, 14, 0, tzinfo=timezone.utc),  # CLOSED_WEEKEND (Sabtu)
        datetime(2026, 11, 23, 20, 45, tzinfo=timezone.utc), # PRE_CLOSE_OVERNIGHT (Senin)
        datetime(2026, 11, 27, 20, 45, tzinfo=timezone.utc), # PRE_CLOSE_WEEKEND (Jumat)
    ],
)
def test_biaya_inap_zero_tolerance_closes_stock_long_and_short(monkeypatch, dt_utc):
    monkeypatch.delenv("OFF_HOURS_HOLD_POLICY", raising=False)
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)

    # LONG dengan funding_daily = +0.0004 (membayar -0.04%/hari, di bawah threshold L2 0.10%)
    c_long = _make_client()
    p_long = _make_pos(asset="NVDA", side="long", entry=100.0, current=100.1, sl=98.5)
    res_long = evaluate_and_apply_position_risk(c_long, p_long, current_funding_daily=0.0004, now_utc=dt_utc)
    assert res_long is not None
    assert res_long["action"] == "CLOSE"
    assert "Biaya Inap" in res_long["detail"]
    c_long.preview_close.assert_called_once()

    # SHORT dengan funding_daily = -0.0004 (membayar -0.04%/hari)
    c_short = _make_client()
    p_short = _make_pos(asset="NVDA", side="short", entry=100.0, current=99.9, sl=101.5)
    res_short = evaluate_and_apply_position_risk(c_short, p_short, current_funding_daily=-0.0004, now_utc=dt_utc)
    assert res_short is not None
    assert res_short["action"] == "CLOSE"
    assert "Biaya Inap" in res_short["detail"]
    c_short.preview_close.assert_called_once()


def test_small_negative_funding_during_open_or_247_assets_uses_standard_hysteresis(monkeypatch):
    monkeypatch.delenv("OFF_HOURS_HOLD_POLICY", raising=False)
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)

    # 1. NVDA saat pasar buka reguler (Rabu 16:00 UTC) -> TIDAK tutup, hanya geser SL breakeven
    wed_open = datetime(2026, 11, 25, 16, 0, tzinfo=timezone.utc)
    c_open = _make_client()
    p_nvda = _make_pos(asset="NVDA", side="long", entry=100.0, current=100.2, sl=98.5)
    res_open = evaluate_and_apply_position_risk(c_open, p_nvda, current_funding_daily=0.0004, now_utc=wed_open)
    assert res_open is not None and res_open["action"] == "UPDATE_AUTO_CLOSE"
    assert res_open["sl"] == pytest.approx(100.0)
    c_open.preview_close.assert_not_called()

    # 2. BTC pada hari Sabtu -> tetap 24/7, TIDAK tutup oleh kebijakan off-hours
    sat = datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc)
    c_btc = _make_client()
    p_btc = _make_pos(asset="BTC", side="long", entry=100.0, current=100.2, sl=98.5)
    res_btc = evaluate_and_apply_position_risk(c_btc, p_btc, current_funding_daily=0.0004, now_utc=sat)
    assert res_btc is not None and res_btc["action"] == "UPDATE_AUTO_CLOSE"
    c_btc.preview_close.assert_not_called()

    # 3. XAU Selasa 03:00 UTC (OPEN) tidak tutup; Sabtu (CLOSED_WEEKEND) langsung CLOSE
    tue_night = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
    c_xau_open = _make_client()
    p_xau = _make_pos(asset="XAU", side="long", entry=100.0, current=100.2, sl=98.5)
    res_xau_open = evaluate_and_apply_position_risk(c_xau_open, p_xau, current_funding_daily=0.0004, now_utc=tue_night)
    assert res_xau_open is not None and res_xau_open["action"] == "UPDATE_AUTO_CLOSE"

    c_xau_wknd = _make_client()
    res_xau_wknd = evaluate_and_apply_position_risk(c_xau_wknd, p_xau, current_funding_daily=0.0004, now_utc=sat)
    assert res_xau_wknd is not None and res_xau_wknd["action"] == "CLOSE"
    assert "Biaya Inap" in res_xau_wknd["detail"]


# ---------------------------------------------------------------------------
# 3. Pre-Close Overnight (Senin-Kamis 20:35-21:00 UTC, Saham AS)
# ---------------------------------------------------------------------------


def test_pre_close_overnight_holds_strong_carry_and_locks_early_profit(monkeypatch):
    monkeypatch.delenv("OFF_HOURS_HOLD_POLICY", raising=False)
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)

    mon_preclose = datetime(2026, 11, 23, 20, 45, tzinfo=timezone.utc)

    # Carry +0.25%/hari (>= 0.20%) & harga flat -> TAHAN tanpa menutup
    c_hold = _make_client()
    p_flat = _make_pos(asset="NVDA", side="long", entry=100.0, current=100.0, sl=98.5)
    res_hold = evaluate_and_apply_position_risk(c_hold, p_flat, current_funding_daily=-0.0025, now_utc=mon_preclose)
    assert res_hold is None
    c_hold.preview_close.assert_not_called()

    # Carry +0.25%/hari & profit +0.50% (>= +0.40%, < Tier 1 +0.80%) -> kunci SL dini di +0.25%
    c_lock = _make_client()
    p_profit = _make_pos(asset="NVDA", side="long", entry=100.0, current=100.50, sl=98.5)
    res_lock = evaluate_and_apply_position_risk(c_lock, p_profit, current_funding_daily=-0.0025, now_utc=mon_preclose)
    assert res_lock is not None
    assert res_lock["action"] == "UPDATE_AUTO_CLOSE"
    assert res_lock["sl"] == pytest.approx(100.25)

    # SHORT dengan profit +0.50% -> kunci SL dini di 99.75
    c_lock_s = _make_client()
    p_profit_s = _make_pos(asset="NVDA", side="short", entry=100.0, current=99.50, tp=97.5, sl=101.5)
    res_lock_s = evaluate_and_apply_position_risk(c_lock_s, p_profit_s, current_funding_daily=0.0025, now_utc=mon_preclose)
    assert res_lock_s is not None
    assert res_lock_s["action"] == "UPDATE_AUTO_CLOSE"
    assert res_lock_s["sl"] == pytest.approx(99.75)

    # Carry lemah +0.10%/hari (< 0.20%) & rugi -0.20% -> TUTUP sebelum overnight
    c_close = _make_client()
    p_weak = _make_pos(asset="NVDA", side="long", entry=100.0, current=99.80, sl=98.5)
    res_close = evaluate_and_apply_position_risk(c_close, p_weak, current_funding_daily=-0.0010, now_utc=mon_preclose)
    assert res_close is not None
    assert res_close["action"] == "CLOSE"
    assert "Pre-Close Overnight" in res_close["detail"]

    # Carry lemah +0.10%/hari TETAPI profit sudah +0.50% -> TIDAK ditutup, langsung lock SL +0.25%
    c_keep_profit = _make_client()
    res_keep = evaluate_and_apply_position_risk(c_keep_profit, p_profit, current_funding_daily=-0.0010, now_utc=mon_preclose)
    assert res_keep is not None
    assert res_keep["action"] == "UPDATE_AUTO_CLOSE"
    assert res_keep["sl"] == pytest.approx(100.25)


# ---------------------------------------------------------------------------
# 4. Pre-Close Weekend (Jumat 20:35-21:00 UTC, Saham AS & Komoditas)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asset", ["NVDA", "XAU"])
def test_pre_close_weekend_rules_for_stocks_and_commodities(monkeypatch, asset):
    monkeypatch.delenv("OFF_HOURS_HOLD_POLICY", raising=False)
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)

    fri_preclose = datetime(2026, 11, 27, 20, 45, tzinfo=timezone.utc)

    # 1. Carry +0.30%/hari (< +0.50%/hari) & belum profit-locked -> CLOSE sebelum libur weekend
    c_low = _make_client()
    p_flat = _make_pos(asset=asset, side="long", entry=100.0, current=100.0, sl=98.5)
    res_low = evaluate_and_apply_position_risk(c_low, p_flat, current_funding_daily=-0.0030, now_utc=fri_preclose)
    assert res_low is not None
    assert res_low["action"] == "CLOSE"
    assert "Pre-Close Weekend" in res_low["detail"]

    # 2. Carry +0.60%/hari (>= +0.50%) TETAPI rugi -0.80% (< -0.50%) -> CLOSE (hindari gap rugi weekend)
    c_loss = _make_client()
    p_loss = _make_pos(asset=asset, side="long", entry=100.0, current=99.20, sl=98.5)
    res_loss = evaluate_and_apply_position_risk(c_loss, p_loss, current_funding_daily=-0.0060, now_utc=fri_preclose)
    assert res_loss is not None
    assert res_loss["action"] == "CLOSE"
    assert "Pre-Close Weekend" in res_loss["detail"]

    # 3. Carry +0.60%/hari (>= +0.50%) & rugi ringan -0.20% (>= -0.50%) -> TAHAN (tp=101.5 agar tidak windfall tighten)
    c_ok = _make_client()
    p_ok = _make_pos(asset=asset, side="long", entry=100.0, current=99.80, tp=101.5, sl=98.5)
    res_ok = evaluate_and_apply_position_risk(c_ok, p_ok, current_funding_daily=-0.0060, now_utc=fri_preclose)
    assert res_ok is None
    c_ok.preview_close.assert_not_called()

    # 4. Carry +0.10%/hari namun profit +0.50% (>= +0.40%) -> TAHAN + kunci SL +0.25%
    c_locked = _make_client()
    p_gain = _make_pos(asset=asset, side="long", entry=100.0, current=100.50, sl=98.5)
    res_locked = evaluate_and_apply_position_risk(c_locked, p_gain, current_funding_daily=-0.0010, now_utc=fri_preclose)
    assert res_locked is not None
    assert res_locked["action"] == "UPDATE_AUTO_CLOSE"
    assert res_locked["sl"] == pytest.approx(100.25)


# ---------------------------------------------------------------------------
# 5. Monotonitas SL: L4 Trailing Tier (+0.35%) Mengalahkan Early Lock (+0.25%)
# ---------------------------------------------------------------------------


def test_l4_trailing_tier_beats_off_hours_early_lock():
    tue_overnight = datetime(2026, 11, 24, 2, 0, tzinfo=timezone.utc)
    client = _make_client()
    pos = _make_pos(asset="NVDA", side="long", entry=100.0, current=101.00, sl=98.5)
    res = evaluate_and_apply_position_risk(client, pos, current_funding_daily=-0.0050, now_utc=tue_overnight)
    assert res is not None
    assert res["action"] == "UPDATE_AUTO_CLOSE"
    assert res["sl"] == pytest.approx(100.35)


# ---------------------------------------------------------------------------
# 6. Kill Switch, Propagasi _run_single_monitor_pass, & Wiring CI/.env.example
# ---------------------------------------------------------------------------


def test_kill_switch_off_disables_off_hours_policy(monkeypatch):
    monkeypatch.setenv("OFF_HOURS_HOLD_POLICY", "off")
    sat = datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc)
    pos = _make_pos(asset="NVDA", side="long", entry=100.0, current=100.2, sl=98.5)
    policy = evaluate_off_hours_position_policy(pos, current_funding_daily=0.0004, now_utc=sat)
    assert policy["action"] == "NONE"
    assert policy["session"] == "DISABLED"

    client = _make_client()
    res = evaluate_and_apply_position_risk(client, pos, current_funding_daily=0.0004, now_utc=sat)
    assert res is not None
    assert res["action"] == "UPDATE_AUTO_CLOSE"
    client.preview_close.assert_not_called()


def test_run_single_monitor_pass_propagates_now_utc_for_off_hours(monkeypatch, tmp_path):
    monkeypatch.delenv("OFF_HOURS_HOLD_POLICY", raising=False)
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_detect_and_log_exchange_closes", lambda *a, **kw: ({}, []))

    client = _make_client()
    client.list_positions.return_value = {
        "positions": [
            {
                "contract_id": 42,
                "contract_base_asset": "NVDA",
                "side": "long",
                "leverage": 2,
                "margin_quote": "3.0",
                "avg_entry_price": "100.0",
                "liquidation_price": "50.0",
                "funding_paid_quote": "0.01",
                "auto_close": {
                    "take_profit": {"trigger_price_quote": "102.5"},
                    "stop_loss": {"trigger_price_quote": "98.5"},
                },
            }
        ]
    }
    client.latest_rate.return_value = {"rate": "100.1"}
    contracts_by_id = {42: {"contract_id": 42, "funding_rate_hourly": str(0.0004 / 24.0)}}

    sat = datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc)
    remaining, _snap, actions, raw_count = _run_single_monitor_pass(
        client,
        contracts_by_id,
        snapshot_path=str(tmp_path / "open_snapshot.json"),
        now_utc=sat,
    )
    assert raw_count == 1
    assert remaining == []
    assert len(actions) == 1
    assert actions[0]["action"] == "CLOSE"
    assert "Biaya Inap" in actions[0]["detail"]


def test_off_hours_knob_wired_to_risk_monitor_and_env_example():
    wf_text = (ROOT / ".github" / "workflows" / "risk-monitor.yml").read_text(encoding="utf-8")
    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for knob in walt_bot.MONITOR_ENV_KNOBS:
        assert f"{knob}: ${{{{ vars.{knob}" in wf_text, f"{knob} belum dioper ke risk-monitor.yml"
        assert f"{knob}=" in env_text, f"{knob} belum didokumentasikan di .env.example"
