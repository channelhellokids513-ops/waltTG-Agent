"""Unit tests untuk aktivasi Engine 2 Saham AS (`STOCK_FORECAST_ENGINE=on`, LONG-ONLY Dip-Buy)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import llm_research
import price_forecast
import walt_bot
from walt_bot import Opportunity, Position, evaluate_and_apply_position_risk, evaluate_off_hours_position_policy


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    client.preview_order.return_value = {"ok": True, "confirmationToken": "tok_open"}
    client.preview_auto_close.return_value = {"ok": True, "confirmationToken": "tok_ac"}
    client.preview_close.return_value = {"ok": True, "confirmationToken": "tok_close"}
    client.execute.return_value = {"ok": True, "order": {"order_id": "ord_stock_e2_1"}}
    return client


def _flat_candles(n: int = 160, base: float = 100.0) -> list[dict]:
    out = []
    px = base
    for i in range(n):
        step = 0.0025 if i % 2 == 0 else -0.0022
        o = px
        c = o * (1.0 + step)
        out.append({
            "t": 1763500000000 + i * 3_600_000,
            "o": o,
            "h": max(o, c) * 1.002,
            "l": min(o, c) * 0.998,
            "c": c,
            "v": 5000.0,
        })
        px = c
    return out


def _dip_candles(n: int = 160, base: float = 100.0, drop_bars: int = 5, step_drop: float = -0.015) -> list[dict]:
    candles = _flat_candles(n, base=base)
    px = candles[-drop_bars - 1]["c"]
    for j in range(len(candles) - drop_bars, len(candles)):
        o = px
        c = o * (1.0 + step_drop)
        candles[j] = {
            "t": candles[j]["t"],
            "o": o,
            "h": o * 1.001,
            "l": c * 0.998,
            "c": c,
            "v": 8000.0,
        }
        px = c
    return candles


def _rally_candles(n: int = 160, base: float = 100.0) -> list[dict]:
    candles = _flat_candles(n, base=base)
    px = candles[-7]["c"]
    for j in range(len(candles) - 6, len(candles)):
        o = px
        c = o * 1.018
        candles[j] = {
            "t": candles[j]["t"],
            "o": o,
            "h": c * 1.002,
            "l": o * 0.999,
            "c": c,
            "v": 8000.0,
        }
        px = c
    return candles


def _stock_research(
    asset: str = "NVDA",
    sentiment: str = "neutral",
    consensus: str = "STRONG",
    alignment: str = "aligned",
    days_to_earnings: int | None = 20,
    p5: float = 0.25,
) -> llm_research.Research:
    return llm_research.Research(
        ticker=asset,
        days_to_earnings=days_to_earnings,
        news_sentiment=sentiment,
        narrative="Ringkasan riset saham",
        confidence=0.75,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        consensus=consensus,
        overall_consensus_level=consensus,
        vendor_alignment=alignment,
        final_score=0.10 if sentiment != "bearish" else -0.45,
        p1_to_p7_scores={"P5_risk_catalysts": p5},
    )


# ---------------------------------------------------------------------------
# 1. Deteksi Sinyal Saham AS (`S1_1H_LONG_DIP` & `S2_1D_LONG_DIP`)
# ---------------------------------------------------------------------------


def test_evaluate_stock_live_dip_signal_long_only_and_funding_cap():
    c1h_dip = _dip_candles(160, base=120.0)
    c1d_dip = _dip_candles(160, base=120.0)
    c1h_rally = _rally_candles(160, base=120.0)
    c1d_rally = _rally_candles(160, base=120.0)

    # Prioritas 1: S1_1H_LONG_DIP saat oversold 1h
    sig_s1 = price_forecast.evaluate_stock_live_dip_signal(
        "NVDA", candles_1h=c1h_dip, candles_1d=c1d_dip, funding_daily=-0.0005
    )
    assert sig_s1 is not None
    assert sig_s1["setup"] == "S1_1H_LONG_DIP"
    assert sig_s1["side"] == "long"
    assert sig_s1["max_hold_hours"] == 48.0
    assert 0.015 <= sig_s1["sl_pct"] <= price_forecast.FORECAST_SL_CAP_PCT
    assert sig_s1["expected_net_edge_pct"] > 0.0

    # Prioritas 2: S2_1D_LONG_DIP jika 1h kosong tapi 1d oversold
    sig_s2 = price_forecast.evaluate_stock_live_dip_signal(
        "NVDA", candles_1h=None, candles_1d=c1d_dip, funding_daily=0.0010
    )
    assert sig_s2 is not None
    assert sig_s2["setup"] == "S2_1D_LONG_DIP"
    assert sig_s2["side"] == "long"
    assert sig_s2["max_hold_hours"] == 120.0

    # Tidak pernah mengeluarkan SHORT saat saham overbought/rally
    sig_rally = price_forecast.evaluate_stock_live_dip_signal(
        "NVDA", candles_1h=c1h_rally, candles_1d=c1d_rally, funding_daily=0.0
    )
    assert sig_rally is None

    # Ditolak jika adverse funding untuk LONG melebihi 0.15%/hari (funding_daily > +0.0015)
    sig_adv = price_forecast.evaluate_stock_live_dip_signal(
        "NVDA", candles_1h=c1h_dip, candles_1d=None, funding_daily=0.0020
    )
    assert sig_adv is None


def test_scan_stock_price_opportunities_filters_allowed_exclude_volume(monkeypatch):
    c1h_dip = _dip_candles(160, base=100.0)
    contracts = [
        {"contract_id": 10, "base_asset": "NVDA", "volume_24h_usd": "40000000", "funding_rate_hourly": "-0.00002", "price_slippage": "0.0004"},
        {"contract_id": 11, "base_asset": "AAPL", "volume_24h_usd": "20000000", "funding_rate_hourly": "-0.00002", "price_slippage": "0.0004"},
        {"contract_id": 12, "base_asset": "MSTR", "volume_24h_usd": "200000", "funding_rate_hourly": "-0.00002", "price_slippage": "0.0004"},
        {"contract_id": 13, "base_asset": "SOL", "volume_24h_usd": "60000000", "funding_rate_hourly": "-0.00002", "price_slippage": "0.0004"},
    ]
    res = price_forecast.scan_stock_price_opportunities(
        contracts,
        allowed_assets=walt_bot.ALLOWED_STOCKS,
        exclude_assets={"AAPL"},
        min_vol_24h=1_000_000.0,
        fetcher=lambda asset, interval, limit: (c1h_dip if interval == "1h" else []),
    )
    assert [x["asset"] for x in res] == ["NVDA"]
    assert res[0]["side"] == "long"


# ---------------------------------------------------------------------------
# 2. Guard decide_go_nogo Khusus Engine 2 Saham AS
# ---------------------------------------------------------------------------


def test_decide_go_nogo_stock_engine2_strict_guards():
    opp_long = Opportunity(
        contract_id=10,
        base_asset="NVDA",
        funding_daily=-0.0004,
        volume_24h=40_000_000.0,
        max_leverage=2,
        side="long",
        signal_source="price_forecast",
        forecast_setup="S1_1H_LONG_DIP",
        planned_sl_pct=0.030,
        planned_tp_pct=0.039,
        max_hold_hours=48.0,
        expected_net_edge_pct=0.0040,
    )
    opp_short = Opportunity(
        contract_id=10,
        base_asset="NVDA",
        funding_daily=0.0004,
        volume_24h=40_000_000.0,
        max_leverage=2,
        side="short",
        signal_source="price_forecast",
        forecast_setup="S1_1H_LONG_DIP",
    )
    # 1. SHORT pada Saham AS Engine 2 -> SKIP
    assert walt_bot.decide_go_nogo(opp_short, _stock_research())[0] == "SKIP"
    # 2. Riset None (E3) -> SKIP
    assert walt_bot.decide_go_nogo(opp_long, None)[0] == "SKIP"
    # 3. Earnings H-7 -> SKIP
    assert walt_bot.decide_go_nogo(opp_long, _stock_research(days_to_earnings=5))[0] == "SKIP"
    # 4. NO_DATA -> SKIP
    assert walt_bot.decide_go_nogo(opp_long, _stock_research(consensus="NO_DATA", sentiment="unknown"))[0] == "SKIP"
    # 5. Bearish (falling knife) -> SKIP
    assert walt_bot.decide_go_nogo(opp_long, _stock_research(sentiment="bearish"))[0] == "SKIP"
    # 6. P5 tinggi -> SKIP
    assert walt_bot.decide_go_nogo(opp_long, _stock_research(p5=0.80))[0] == "SKIP"
    # 7. SPLIT -> HALF
    assert walt_bot.decide_go_nogo(opp_long, _stock_research(consensus="SPLIT", alignment="split"))[0] == "HALF"
    # 8. Neutral / Bullish + aligned + E3 segar -> GO
    assert walt_bot.decide_go_nogo(opp_long, _stock_research(sentiment="neutral"))[0] == "GO"
    assert walt_bot.decide_go_nogo(opp_long, _stock_research(sentiment="bullish"))[0] == "GO"

    # Komoditas ditolak pada Engine 2
    opp_comm = Opportunity(
        contract_id=99,
        base_asset="XAU",
        funding_daily=0.0,
        volume_24h=30_000_000.0,
        max_leverage=2,
        side="long",
        signal_source="price_forecast",
    )
    assert walt_bot.decide_go_nogo(opp_comm, _stock_research(asset="XAU"))[0] == "SKIP"


# ---------------------------------------------------------------------------
# 3. market_execute (Jam Bursa AS) & Kill Switch STOCK_FORECAST_ENGINE=off
# ---------------------------------------------------------------------------


def test_market_execute_opens_stock_engine2_during_us_hours(monkeypatch):
    import sheet_logger

    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda *a, **kw: True)
    monkeypatch.setattr(walt_bot, "check_daily_circuit_breaker", lambda *a, **kw: (False, "OK"))
    monkeypatch.setattr(walt_bot, "check_cold_state_entry_gate", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "_is_asset_in_cooldown", lambda *a, **kw: (False, 0.0))
    monkeypatch.setattr(walt_bot, "check_entry_frequency_limits", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "record_entry_execution", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **kw: True)
    monkeypatch.setattr(sheet_logger, "fetch_latest_research_from_sheet", lambda **kw: {"NVDA": _stock_research("NVDA")})
    monkeypatch.setattr(sheet_logger, "log_execution", lambda *a, **kw: {"executions_count": 1})

    monkeypatch.setattr(
        price_forecast,
        "scan_stock_price_opportunities",
        lambda contracts, **kw: [
            {
                "contract_id": 88,
                "asset": "NVDA",
                "side": "long",
                "setup": "S1_1H_LONG_DIP",
                "sl_pct": 0.030,
                "tp_pct": 0.039,
                "max_hold_hours": 48.0,
                "expected_net_edge_pct": 0.0040,
                "funding_daily": -0.0005,
                "volume_24h": 45_000_000.0,
                "max_leverage": 2,
                "min_size_base": 0.01,
                "price_slippage": 0.0004,
                "reason": "S1 1h Stock Dip LONG",
            }
        ],
    )

    client = _make_mock_client()
    client.balance.return_value = {"accounts": [{"balance_available": "25.0", "currency": "USDC"}]}
    client.list_positions.return_value = {"positions": []}
    client.latest_rate.return_value = {"rate": "140.0"}
    client.list_contracts.return_value = {
        "contracts": [
            {
                "contract_id": 88,
                "base_asset": "NVDA",
                "volume_24h_usd": "45000000",
                "funding_rate_hourly": "-0.00002",
                "max_leverage": 2,
                "min_size_base": "0.01",
                "price_slippage": "0.0004",
            }
        ]
    }

    # Senin 15:00 UTC (bursa AS buka) + STOCK_FORECAST_ENGINE=off -> dilewati
    us_open_dt = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("STOCK_FORECAST_ENGINE", "off")
    res_off = walt_bot.market_execute(client, dry_run=False, universe={"NVDA"}, now_utc=us_open_dt)
    assert res_off.get("status") == "gear_0_idle" or not [
        e for e in res_off.get("executions", []) if e.get("verdict") == "EXECUTED"
    ]

    # Senin 22:00 UTC (bursa AS tutup) + STOCK_FORECAST_ENGINE=on -> tetap dilewati
    us_closed_dt = datetime(2026, 11, 23, 22, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("STOCK_FORECAST_ENGINE", "on")
    res_closed = walt_bot.market_execute(client, dry_run=False, universe={"NVDA"}, now_utc=us_closed_dt)
    assert not [e for e in res_closed.get("executions", []) if e.get("verdict") == "EXECUTED"]

    # Senin 15:00 UTC + STOCK_FORECAST_ENGINE=on -> EXECUTED & metadata tersimpan
    res_on = walt_bot.market_execute(client, dry_run=False, universe={"NVDA"}, now_utc=us_open_dt)
    execs = [e for e in res_on["executions"] if e["asset"] == "NVDA" and e["verdict"] == "EXECUTED"]
    assert len(execs) == 1
    assert execs[0]["side"] == "long"
    assert execs[0]["sl"] == pytest.approx(round(140.0 * 0.970, 4))
    assert execs[0]["tp"] == pytest.approx(round(140.0 * 1.039, 4))
    meta = walt_bot._load_forecast_position_meta()
    assert "88" in meta and meta["88"]["setup"] == "S1_1H_LONG_DIP"


# ---------------------------------------------------------------------------
# 4. Off-Hours Policy & Risk Monitor Engine 2 Saham AS
# ---------------------------------------------------------------------------


def test_stock_engine2_off_hours_overnight_hold_weekend_close_and_timestop():
    mon_preclose = datetime(2026, 11, 23, 20, 45, tzinfo=timezone.utc)
    fri_preclose = datetime(2026, 11, 27, 20, 45, tzinfo=timezone.utc)
    open_time = (mon_preclose - timedelta(hours=4)).isoformat()

    pos_flat = Position(
        contract_id=88,
        base_asset="NVDA",
        side="long",
        leverage=2,
        margin=3.0,
        entry_price=100.0,
        current_price=100.2,
        tp=103.9,
        sl=97.0,
        funding_paid=0.0,
        liquidation_price=50.0,
        opened_at=open_time,
        signal_source="price_forecast",
        planned_sl_pct=0.030,
        planned_tp_pct=0.039,
        max_hold_hours=48.0,
    )

    # 1. Senin PRE_CLOSE_OVERNIGHT dengan adverse funding kecil (0.05%/hari < 0.25%) -> HOLD (tidak tutup malam 1)
    pol_hold = evaluate_off_hours_position_policy(pos_flat, current_funding_daily=0.0005, now_utc=mon_preclose)
    assert pol_hold["action"] == "HOLD_PROTECT"

    # 2. Senin PRE_CLOSE_OVERNIGHT profit +0.75% (>= +0.70%) -> Early Lock SL +0.25% (100.25)
    pos_early = Position(
        contract_id=88,
        base_asset="NVDA",
        side="long",
        leverage=2,
        margin=3.0,
        entry_price=100.0,
        current_price=100.75,
        tp=103.9,
        sl=97.0,
        funding_paid=0.0,
        liquidation_price=50.0,
        opened_at=open_time,
        signal_source="price_forecast",
        planned_sl_pct=0.030,
        planned_tp_pct=0.039,
        max_hold_hours=48.0,
    )
    c_early = _make_mock_client()
    r_early = evaluate_and_apply_position_risk(c_early, pos_early, current_funding_daily=0.0005, now_utc=mon_preclose)
    assert r_early is not None and r_early["action"] == "UPDATE_AUTO_CLOSE"
    assert r_early["sl"] == pytest.approx(100.25)

    # 3. Adverse funding >= 0.25%/hari (0.30%) -> CLOSE paksa
    c_adv = _make_mock_client()
    r_adv = evaluate_and_apply_position_risk(c_adv, pos_flat, current_funding_daily=0.0030, now_utc=mon_preclose)
    assert r_adv is not None and r_adv["action"] == "CLOSE"

    # 4. Jumat PRE_CLOSE_WEEKEND (dibuka 4 jam lalu, belum profit-locked) -> CLOSE hindari weekend gap 64.5h
    pos_fri = Position(
        contract_id=88,
        base_asset="NVDA",
        side="long",
        leverage=2,
        margin=3.0,
        entry_price=100.0,
        current_price=100.2,
        tp=103.9,
        sl=97.0,
        funding_paid=0.0,
        liquidation_price=50.0,
        opened_at=(fri_preclose - timedelta(hours=4)).isoformat(),
        signal_source="price_forecast",
        planned_sl_pct=0.030,
        planned_tp_pct=0.039,
        max_hold_hours=48.0,
    )
    c_wk = _make_mock_client()
    r_wk = evaluate_and_apply_position_risk(c_wk, pos_fri, current_funding_daily=0.0, now_utc=fri_preclose)
    assert r_wk is not None and r_wk["action"] == "CLOSE"
    assert "Weekend" in r_wk["detail"]

    # 5. Umur 49h >= Time-Stop 48h saat pasar buka & profit datar -> CLOSE Time-Stop
    mon_open_later = datetime(2026, 11, 25, 16, 0, tzinfo=timezone.utc)
    pos_ts = Position(
        contract_id=88,
        base_asset="NVDA",
        side="long",
        leverage=2,
        margin=3.0,
        entry_price=100.0,
        current_price=100.2,
        tp=103.9,
        sl=97.0,
        funding_paid=0.0,
        liquidation_price=50.0,
        opened_at=(mon_open_later - timedelta(hours=49)).isoformat(),
        signal_source="price_forecast",
        planned_sl_pct=0.030,
        planned_tp_pct=0.039,
        max_hold_hours=48.0,
    )
    c_ts = _make_mock_client()
    r_ts = evaluate_and_apply_position_risk(c_ts, pos_ts, current_funding_daily=0.0, now_utc=mon_open_later)
    assert r_ts is not None and r_ts["action"] == "CLOSE"
    assert "Time-Stop" in r_ts["detail"]
