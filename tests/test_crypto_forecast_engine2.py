"""Unit tests untuk aktivasi Engine 2 Crypto (`V5_4H_REJ_COMFORT` + `V4_1H_EXTREME_FADE`)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import llm_research
import price_forecast
import walt_bot
from walt_bot import Opportunity, Position, evaluate_and_apply_position_risk


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    client.preview_order.return_value = {"ok": True, "confirmationToken": "tok_open"}
    client.preview_auto_close.return_value = {"ok": True, "confirmationToken": "tok_ac"}
    client.preview_close.return_value = {"ok": True, "confirmationToken": "tok_close"}
    client.execute.return_value = {"ok": True, "order": {"order_id": "ord_e2_1"}}
    return client


def _flat_candles(n: int = 120, base: float = 100.0) -> list[dict]:
    out = []
    px = base
    for i in range(n):
        step = 0.003 if i % 2 == 0 else -0.0028
        o = px
        c = o * (1.0 + step)
        out.append({
            "t": 1763500000000 + i * 3_600_000,
            "o": o,
            "h": max(o, c) * 1.003,
            "l": min(o, c) * 0.997,
            "c": c,
            "v": 2000.0,
        })
        px = c
    return out


# ---------------------------------------------------------------------------
# 1. Deteksi Sinyal Engine 2 (`V4_1H_EXTREME_FADE` & `V5_4H_REJ_COMFORT`)
# ---------------------------------------------------------------------------


def test_evaluate_crypto_live_fade_signal_v4_1h_and_v5_4h(monkeypatch):
    c4 = _flat_candles(160, base=100.0)
    c1 = _flat_candles(160, base=100.0)
    # Buat lonjakan ekstrem pada 8 bar terakhir c1 -> memicu V4_1H_EXTREME_FADE SHORT
    px = c1[-9]["c"]
    for j in range(len(c1) - 8, len(c1)):
        o = px
        c = o * 1.014
        c1[j] = {"t": c1[j]["t"], "o": o, "h": c * 1.002, "l": o * 0.999, "c": c, "v": 4000.0}
        px = c

    sig_v4 = price_forecast.evaluate_crypto_live_fade_signal(
        "SOL", c4, c1, funding_daily=0.0005  # funding rendah (< 0.50%/hari)
    )
    assert sig_v4 is not None
    assert sig_v4["setup"] == "V4_1H_EXTREME_FADE"
    assert sig_v4["side"] == "short"
    assert sig_v4["max_hold_hours"] == 24.0
    assert 0.015 <= sig_v4["sl_pct"] <= price_forecast.FORECAST_SL_CAP_PCT
    assert sig_v4["expected_net_edge_pct"] > 0.0

    # Uji V5_4H_REJ_COMFORT SHORT dengan stub struktur magnet Rejected (RSI >= 50)
    c4_up = _flat_candles(160, base=100.0)
    c4_up[-1]["c"] = c4_up[-2]["c"] * 1.006
    c4_up[-1]["h"] = c4_up[-1]["c"] * 1.002
    monkeypatch.setattr(
        price_forecast,
        "_extract_gravity_fade_state",
        lambda candles: (float(candles[-1]["c"]) * 0.96, True, False, [float(candles[-1]["c"]) * 1.002], []),
    )
    sig_v5 = price_forecast.evaluate_crypto_live_fade_signal(
        "ETH", c4_up, candles_1h=None, funding_daily=-0.0010  # adverse funding 0.10%/hari <= 0.30%/hari
    )
    assert sig_v5 is not None
    assert sig_v5["setup"] == "V5_4H_REJ_COMFORT"
    assert sig_v5["side"] == "short"
    assert sig_v5["max_hold_hours"] == 72.0
    assert 0.015 <= sig_v5["sl_pct"] <= price_forecast.FORECAST_SL_CAP_PCT

    # Tolak bila adverse funding melampaui 0.30%/hari
    sig_adverse = price_forecast.evaluate_crypto_live_fade_signal(
        "ETH", c4_up, candles_1h=None, funding_daily=-0.0040
    )
    assert sig_adverse is None


def test_scan_crypto_price_opportunities_filters_volume_and_excluded(monkeypatch):
    monkeypatch.setattr(
        price_forecast,
        "evaluate_crypto_live_fade_signal",
        lambda asset, c4, c1, **kw: {
            "asset": asset,
            "side": "long",
            "setup": "V5_4H_REJ_COMFORT",
            "sl_pct": 0.030,
            "tp_pct": 0.030,
            "max_hold_hours": 72.0,
            "expected_net_edge_pct": 0.0025 if asset == "SOL" else 0.0020,
            "reason": f"fade {asset}",
        },
    )
    contracts = [
        {"contract_id": 1, "base_asset": "BTC", "volume_24h_usd": "50000000", "funding_rate_hourly": "0.00002", "max_leverage": 2, "min_size_base": "0.001", "price_slippage": "0.0005"},
        {"contract_id": 2, "base_asset": "SOL", "volume_24h_usd": "20000000", "funding_rate_hourly": "0.00003", "max_leverage": 2, "min_size_base": "0.01", "price_slippage": "0.0005"},
        {"contract_id": 3, "base_asset": "AAVE", "volume_24h_usd": "250000", "funding_rate_hourly": "0.00001", "max_leverage": 2, "min_size_base": "0.01", "price_slippage": "0.0005"},
    ]
    res = price_forecast.scan_crypto_price_opportunities(
        contracts,
        allowed_assets={"BTC", "SOL", "AAVE"},
        exclude_assets={"BTC"},
        min_vol_24h=1_000_000.0,
        fetcher=lambda asset, interval, limit: ([], "stub"),
    )
    assert [x["asset"] for x in res] == ["SOL"]


# ---------------------------------------------------------------------------
# 2. Integrasi market_execute(ALLOWED_CRYPTO): Eksekusi Saat Funding < 0.50%
# ---------------------------------------------------------------------------


def test_crypto_execute_opens_engine2_trade_and_saves_meta(monkeypatch):
    monkeypatch.setenv("CRYPTO_FORECAST_ENGINE", "on")
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda *a, **kw: True)
    monkeypatch.setattr(walt_bot, "check_daily_circuit_breaker", lambda *a, **kw: (False, "OK"))
    monkeypatch.setattr(walt_bot, "check_cold_state_entry_gate", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "_is_asset_in_cooldown", lambda *a, **kw: (False, 0.0))
    monkeypatch.setattr(walt_bot, "check_entry_frequency_limits", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "record_entry_execution", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **kw: True)
    monkeypatch.setattr(walt_bot, "crypto_research_set", lambda *a, **kw: {"SOL"})

    import sheet_logger
    monkeypatch.setattr(sheet_logger, "fetch_latest_research_from_sheet", lambda **kw: {})
    monkeypatch.setattr(sheet_logger, "log_execution", lambda *a, **kw: {"executions_count": 1})

    monkeypatch.setattr(
        walt_bot,
        "research_stock",
        lambda asset, **kw: llm_research.Research(
            ticker=asset,
            days_to_earnings=None,
            news_sentiment="neutral",
            narrative="Netral",
            confidence=0.70,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            consensus="STRONG",
            overall_consensus_level="STRONG",
            vendor_alignment="aligned",
            final_score=0.05,
        ),
    )

    import price_forecast as pf_mod
    monkeypatch.setattr(
        pf_mod,
        "scan_crypto_price_opportunities",
        lambda contracts, **kw: [
            {
                "contract_id": 22,
                "asset": "SOL",
                "side": "short",
                "setup": "V5_4H_REJ_COMFORT",
                "sl_pct": 0.032,
                "tp_pct": 0.032,
                "max_hold_hours": 72.0,
                "expected_net_edge_pct": 0.0022,
                "funding_daily": 0.0008,  # Hanya 0.08%/hari (< 0.50%/hari Engine 1)
                "volume_24h": 30_000_000.0,
                "max_leverage": 2,
                "min_size_base": 0.01,
                "price_slippage": 0.0005,
                "reason": "V5 4h Fade SHORT",
            }
        ],
    )

    client = _make_mock_client()
    client.balance.return_value = {"accounts": [{"balance_available": "25.0", "currency": "USDC"}]}
    client.list_positions.return_value = {"positions": []}
    client.latest_rate.return_value = {"rate": "150.0"}
    client.list_contracts.return_value = {
        "contracts": [
            {
                "contract_id": 22,
                "base_asset": "SOL",
                "volume_24h_usd": "30000000",
                "funding_rate_hourly": "0.0000333",
                "max_leverage": 2,
                "min_size_base": "0.01",
                "price_slippage": "0.0005",
            }
        ]
    }

    res = walt_bot.crypto_execute(client, dry_run=False)
    execs = [e for e in res["executions"] if e["asset"] == "SOL" and e["verdict"] == "EXECUTED"]
    assert len(execs) == 1
    assert execs[0]["side"] == "short"
    # SL/TP memakai rencana ATR Engine 2 (3.2%), BUKAN SL 1.5% Engine 1
    assert execs[0]["sl"] == pytest.approx(round(150.0 * 1.032, 4))
    assert execs[0]["tp"] == pytest.approx(round(150.0 * 0.968, 4))
    # Metadata tersimpan di logs/market_regime.json (isolated via conftest)
    meta = walt_bot._load_forecast_position_meta()
    assert "22" in meta
    assert meta["22"]["setup"] == "V5_4H_REJ_COMFORT"
    assert meta["22"]["max_hold_hours"] == 72.0


def test_crypto_execute_respects_kill_switch_off(monkeypatch):
    monkeypatch.setenv("CRYPTO_FORECAST_ENGINE", "off")
    import price_forecast as pf_mod

    called = []
    monkeypatch.setattr(pf_mod, "scan_crypto_price_opportunities", lambda *a, **kw: called.append(True) or [])
    monkeypatch.setattr(walt_bot, "_should_run_gear0_cycle", lambda *a, **kw: (False, 10.0))

    client = _make_mock_client()
    client.balance.return_value = {"accounts": [{"balance_available": "25.0", "currency": "USDC"}]}
    client.list_contracts.return_value = {"contracts": []}
    walt_bot.crypto_execute(client, dry_run=False)
    assert called == []


# ---------------------------------------------------------------------------
# 3. Monitor Risiko Engine 2: ATR SL, Adverse Funding 0.30%, & Time-Stop
# ---------------------------------------------------------------------------


def test_risk_monitor_engine2_respects_atr_sl_adverse_funding_and_time_stop(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)

    now_dt = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    recent_open = (now_dt - timedelta(hours=10)).isoformat()
    expired_open = (now_dt - timedelta(hours=73)).isoformat()

    # 1. Rugi -2.0% masih dalam SL ATR 4.0%, funding merugikan kecil 0.15%/hari (< 0.30%) -> TIDAK TUTUP
    c1 = _make_mock_client()
    p1 = Position(
        contract_id=91,
        base_asset="SOL",
        side="long",
        leverage=2,
        margin=3.0,
        entry_price=100.0,
        current_price=98.0,
        tp=104.0,
        sl=96.0,
        funding_paid=0.0,
        liquidation_price=50.0,
        opened_at=recent_open,
        signal_source="price_forecast",
        planned_sl_pct=0.040,
        planned_tp_pct=0.040,
        max_hold_hours=72.0,
    )
    res1 = evaluate_and_apply_position_risk(c1, p1, current_funding_daily=0.0015, now_utc=now_dt)
    assert res1 is None
    c1.preview_close.assert_not_called()

    # 2. Funding berbalik besar 0.35%/hari (>= 0.30%) -> TUTUP Engine 2 Adverse Funding
    c2 = _make_mock_client()
    res2 = evaluate_and_apply_position_risk(c2, p1, current_funding_daily=0.0035, now_utc=now_dt)
    assert res2 is not None and res2["action"] == "CLOSE"
    assert "Engine 2 Adverse Funding" in res2["detail"]

    # 3. Umur 73h >= 72h dengan profit datar (+0.20% < +0.80%) -> TUTUP Engine 2 Time-Stop
    c3 = _make_mock_client()
    p3 = Position(
        contract_id=91,
        base_asset="SOL",
        side="long",
        leverage=2,
        margin=3.0,
        entry_price=100.0,
        current_price=100.2,
        tp=104.0,
        sl=96.0,
        funding_paid=0.0,
        liquidation_price=50.0,
        opened_at=expired_open,
        signal_source="price_forecast",
        planned_sl_pct=0.040,
        planned_tp_pct=0.040,
        max_hold_hours=72.0,
    )
    res3 = evaluate_and_apply_position_risk(c3, p3, current_funding_daily=0.0, now_utc=now_dt)
    assert res3 is not None and res3["action"] == "CLOSE"
    assert "Time-Stop" in res3["detail"]

    # 4. Umur 73h TETAPI profit sudah +0.90% (>= +0.80%) -> TIDAK time-stop, biarkan Trailing L4 mengunci +0.35%
    c4 = _make_mock_client()
    p4 = Position(
        contract_id=91,
        base_asset="SOL",
        side="long",
        leverage=2,
        margin=3.0,
        entry_price=100.0,
        current_price=100.9,
        tp=104.0,
        sl=96.0,
        funding_paid=0.0,
        liquidation_price=50.0,
        opened_at=expired_open,
        signal_source="price_forecast",
        planned_sl_pct=0.040,
        planned_tp_pct=0.040,
        max_hold_hours=72.0,
    )
    res4 = evaluate_and_apply_position_risk(c4, p4, current_funding_daily=0.0, now_utc=now_dt)
    assert res4 is not None and res4["action"] == "UPDATE_AUTO_CLOSE"
    assert res4["sl"] == pytest.approx(100.35)


# ---------------------------------------------------------------------------
# 4. Integrasi daily_scan, sheet_logger.log_scan_to_csv, & scan_with_research
# ---------------------------------------------------------------------------


def test_daily_scan_includes_engine2_crypto_and_respects_toggle(monkeypatch):
    import sheet_logger

    monkeypatch.setattr(sheet_logger, "get_closed_funding_current_month", lambda **kw: 0.0)
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **kw: True)
    monkeypatch.setattr(
        price_forecast,
        "scan_crypto_price_opportunities",
        lambda contracts, **kw: [
            {
                "contract_id": 55,
                "asset": "SOL",
                "side": "short",
                "setup": "V5_4H_REJ_COMFORT",
                "sl_pct": 0.032,
                "tp_pct": 0.032,
                "max_hold_hours": 72.0,
                "expected_net_edge_pct": 0.0022,
                "funding_daily": 0.0008,
                "volume_24h": 30_000_000.0,
                "max_leverage": 2,
                "min_size_base": 0.01,
                "price_slippage": 0.0005,
                "reason": "V5 4h Fade SHORT",
            }
        ],
    )

    client = _make_mock_client()
    client.readiness.return_value = {"ok": True}
    client.balance.return_value = {"accounts": [{"balance_available": "25.0", "currency": "USDC"}]}
    client.list_positions.return_value = {"positions": []}
    client.latest_rate.return_value = {"rate": "150.0"}
    client.list_contracts.return_value = {
        "contracts": [
            {
                "contract_id": 55,
                "base_asset": "SOL",
                "volume_24h_usd": "30000000",
                "funding_rate_hourly": "0.0000333",
                "max_leverage": 2,
                "min_size_base": "0.01",
                "price_slippage": "0.0005",
            }
        ]
    }

    monkeypatch.setenv("CRYPTO_FORECAST_ENGINE", "on")
    summary_on = walt_bot.daily_scan(client)
    fc_items = [o for o in summary_on["top_opportunities"] if o.get("signal_source") == "price_forecast"]
    assert len(fc_items) == 1
    assert fc_items[0]["asset"] == "SOL"
    assert fc_items[0]["forecast_setup"] == "V5_4H_REJ_COMFORT"
    assert fc_items[0]["expected_net_edge_pct"] == pytest.approx(0.0022)

    monkeypatch.setenv("CRYPTO_FORECAST_ENGINE", "off")
    summary_off = walt_bot.daily_scan(client)
    assert all(o.get("signal_source") != "price_forecast" for o in summary_off["top_opportunities"])


def test_sheet_logger_log_scan_to_csv_engine2_row_has_30_cols_and_go(monkeypatch, tmp_path):
    import sheet_logger

    monkeypatch.setattr(sheet_logger, "gravity_diagnostics", lambda asset: {k: "" for k in sheet_logger.MG_COLUMNS})
    summary = {
        "ts": "2026-11-23T12:00:00+00:00",
        "balance": [{"balance_available": "25.0"}],
        "positions_count": 0,
        "top_opportunities": [
            {
                "contract_id": 55,
                "asset": "SOL",
                "side": "short",
                "funding_daily_pct": 0.08,
                "price": 150.0,
                "signal_source": "price_forecast",
                "forecast_setup": "V5_4H_REJ_COMFORT",
                "expected_net_edge_pct": 0.0022,
                "forecast_reason": "V5 4h Fade SHORT",
                "planned_sl_pct": 0.032,
                "planned_tp_pct": 0.032,
                "max_hold_hours": 72.0,
            }
        ],
    }
    res_obj = llm_research.Research(
        ticker="SOL",
        days_to_earnings=None,
        news_sentiment="neutral",
        narrative="Netral",
        confidence=0.70,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        consensus="STRONG",
        overall_consensus_level="STRONG",
        vendor_alignment="aligned",
        final_score=0.05,
    )
    rows = sheet_logger.log_scan_to_csv(
        summary, {"SOL": res_obj}, filepath=str(tmp_path / "scan_history.csv")
    )
    assert len(rows) == 1
    assert len(rows[0]) == len(sheet_logger.DAILY_SCANS_HEADERS) == 30
    verdict_idx = sheet_logger.DAILY_SCANS_HEADERS.index("verdict")
    reason_idx = sheet_logger.DAILY_SCANS_HEADERS.index("reason")
    assert rows[0][verdict_idx] == "GO"
    assert "V5_4H_REJ_COMFORT" in rows[0][reason_idx]


def test_scan_with_research_executes_and_logs_engine2_to_daily_scans(monkeypatch):
    import sheet_logger

    monkeypatch.setenv("CRYPTO_FORECAST_ENGINE", "on")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dummy_key")
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda *a, **kw: True)
    monkeypatch.setattr(walt_bot, "check_daily_circuit_breaker", lambda *a, **kw: (False, "OK"))
    monkeypatch.setattr(walt_bot, "check_cold_state_entry_gate", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "_is_asset_in_cooldown", lambda *a, **kw: (False, 0.0))
    monkeypatch.setattr(walt_bot, "check_entry_frequency_limits", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "record_entry_execution", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **kw: True)
    monkeypatch.setattr(sheet_logger, "get_closed_funding_current_month", lambda **kw: 0.0)

    logged_scan_summaries = []
    monkeypatch.setattr(
        sheet_logger,
        "log_scan",
        lambda summary, researches: logged_scan_summaries.append(summary) or {"rows_count": len(summary.get("top_opportunities", []))},
    )
    monkeypatch.setattr(sheet_logger, "log_pnl_tracker", lambda summary: {"ok": True})
    monkeypatch.setattr(sheet_logger, "log_execution", lambda execs: {"executions_count": len(execs)})

    monkeypatch.setattr(
        walt_bot,
        "research_stock",
        lambda asset, **kw: llm_research.Research(
            ticker=asset,
            days_to_earnings=None,
            news_sentiment="neutral",
            narrative="Netral",
            confidence=0.70,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            consensus="STRONG",
            overall_consensus_level="STRONG",
            vendor_alignment="aligned",
            final_score=0.05,
        ),
    )
    monkeypatch.setattr(
        price_forecast,
        "scan_crypto_price_opportunities",
        lambda contracts, **kw: [
            {
                "contract_id": 55,
                "asset": "SOL",
                "side": "short",
                "setup": "V5_4H_REJ_COMFORT",
                "sl_pct": 0.032,
                "tp_pct": 0.032,
                "max_hold_hours": 72.0,
                "expected_net_edge_pct": 0.0022,
                "funding_daily": 0.0008,
                "volume_24h": 30_000_000.0,
                "max_leverage": 2,
                "min_size_base": 0.01,
                "price_slippage": 0.0005,
                "reason": "V5 4h Fade SHORT",
            }
        ],
    )

    client = _make_mock_client()
    client.readiness.return_value = {"ok": True}
    client.balance.return_value = {"accounts": [{"balance_available": "25.0", "currency": "USDC"}]}
    client.list_positions.return_value = {"positions": []}
    client.latest_rate.return_value = {"rate": "150.0"}
    client.list_contracts.return_value = {
        "contracts": [
            {
                "contract_id": 55,
                "base_asset": "SOL",
                "volume_24h_usd": "30000000",
                "funding_rate_hourly": "0.0000333",
                "max_leverage": 2,
                "min_size_base": "0.01",
                "price_slippage": "0.0005",
            }
        ]
    }

    res = walt_bot.scan_with_research(client, auto_execute=True, dry_run=False)
    execs = [e for e in res["executions"] if e["asset"] == "SOL" and e["verdict"] == "EXECUTED"]
    assert len(execs) == 1
    assert "V5_4H_REJ_COMFORT" in execs[0]["reason"]
    meta = walt_bot._load_forecast_position_meta()
    assert "55" in meta and meta["55"]["setup"] == "V5_4H_REJ_COMFORT"
    assert len(logged_scan_summaries) == 1
    assert any(
        o["asset"] == "SOL" and o.get("signal_source") == "price_forecast"
        for o in logged_scan_summaries[0]["top_opportunities"]
    )
