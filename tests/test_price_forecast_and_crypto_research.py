"""Unit tests untuk perbaikan riset Crypto + modul price_forecast & walk-forward backtester."""

from unittest.mock import MagicMock

import pytest

import llm_research
import price_forecast
from market_gravity import GravitySnapshot, Magnet
from tools.backtest_forecast_24h import collect_trades_for_asset, simulate_trade_24h, summarise_trades


def _synthetic_candles(
    n: int = 120,
    start_price: float = 100.0,
    drift_per_bar: float = 0.0,
    last_jump_pct: float = 0.0,
    last_vol_mult: float = 1.0,
    jump_bars: int = 20,
) -> list[dict]:
    candles = []
    price = start_price
    jump_start = max(0, n - max(1, jump_bars))
    for i in range(n):
        wiggle = (0.004 if i % 2 == 0 else -0.0032)
        o = price
        jump_step = (last_jump_pct / max(1, jump_bars)) if (i >= jump_start and last_jump_pct != 0.0) else 0.0
        c = o * (1.0 + drift_per_bar + wiggle + jump_step)
        hi = max(o, c) * 1.003
        lo = min(o, c) * 0.997
        vol = 1000.0 * (last_vol_mult if i >= n - 24 else 1.0)
        candles.append({
            "t": 1763500000000 + i * 3_600_000,
            "o": o,
            "h": hi,
            "l": lo,
            "c": c,
            "v": vol,
        })
        price = c
    return candles


# ---------------------------------------------------------------------------
# 1. Regresi Riset Crypto: Yahoo `-USD` fallback & OAK MCP anchor (#45129246139)
# ---------------------------------------------------------------------------


def test_fetch_headlines_crypto_tries_usd_suffix_first(monkeypatch):
    queries_seen = []

    def fake_get(url, params=None, headers=None, timeout=15.0):
        queries_seen.append(params["q"])
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if params["q"] == "AAVE-USD":
            resp.json.return_value = {"news": [{"title": "AAVE protocol V4 upgrade boosts yields"}]}
        else:
            resp.json.return_value = {"news": []}
        return resp

    monkeypatch.setattr(llm_research.httpx, "get", fake_get)
    titles = llm_research.fetch_headlines("AAVE")
    assert queries_seen[0] == "AAVE-USD"
    assert titles == ["AAVE protocol V4 upgrade boosts yields"]


def test_research_stock_uses_oak_mcp_when_yahoo_headlines_empty(monkeypatch):
    import oak_mcp_client

    monkeypatch.setenv("RESEARCH_MODE", "swarm")

    monkeypatch.setattr(llm_research, "fetch_earnings_days_with_status", lambda t: (None, "no_data"))
    monkeypatch.setattr(llm_research, "fetch_headlines", lambda t, limit=10: [])
    monkeypatch.setattr(oak_mcp_client, "is_oak_mcp_configured", lambda: True)
    monkeypatch.setattr(
        oak_mcp_client,
        "fetch_oak_market_context",
        lambda t: {
            "available": True,
            "headlines": [],
            "overview": {"totalMarketCapUsd": {"value": 2.4e12}},
            "calendar": [],
            "asset": {"price_change_percentage_24h": 4.2},
            "smart_money": "",
            "errors": [],
        },
    )
    monkeypatch.setattr(
        oak_mcp_client,
        "format_oak_context_for_prompt",
        lambda ctx: "[DATA REAL-TIME OAK RESEARCH MCP]\n- AAVE 24h=+4.20%",
    )

    voters_called = []

    def fake_voter(name):
        def _run(client, ticker, headlines, strategy_context=""):
            voters_called.append((name, ticker, len(headlines), "[DATA REAL-TIME OAK RESEARCH MCP]" in strategy_context))
            return {
                "voter_id": name,
                "overall_voter_confidence": 0.80,
                "params": {
                    p: {"score": 0.45 if "polarity" in p or "momentum" in p else 0.5, "reasoning": "ok"}
                    for p in llm_research.PARAM_WEIGHTS
                },
            }
        return _run

    monkeypatch.setattr(llm_research, "run_voter_1", fake_voter("V1"))
    monkeypatch.setattr(llm_research, "run_voter_2", fake_voter("V2"))
    monkeypatch.setattr(llm_research, "run_voter_3", fake_voter("V3"))
    monkeypatch.setattr(llm_research, "run_voter_4", fake_voter("V4"))
    monkeypatch.setattr(
        llm_research,
        "run_meta_aggregator",
        lambda *a, **kw: {
            "final_score": 0.55,
            "overall_confidence": 0.78,
            "overall_consensus_level": "UNANIMOUS",
            "vendor_alignment": "all_agree",
            "most_influential_parameter": "P1_sentiment_polarity",
            "disagreement_summary": [],
            "justification": "Didukung data real-time OAK MCP.",
            "per_parameter_final": {
                p: {"final_score": 0.5, "trusted_voter": "CONSENSUS", "rationale": "ok"}
                for p in llm_research.PARAM_WEIGHTS
            },
        },
    )

    res = llm_research.research_stock("AAVE", qwen=object(), gemini=object(), funding_daily=0.0052, recommended_side="short")
    assert res.consensus != "NO_DATA"
    assert res.voters_count == 4
    assert all(ticker == "AAVE" and h_count >= 1 and has_ctx for _n, ticker, h_count, has_ctx in voters_called)


def test_research_stock_returns_no_data_when_both_yahoo_and_oak_empty(monkeypatch):
    import oak_mcp_client

    monkeypatch.setattr(llm_research, "fetch_earnings_days_with_status", lambda t: (None, "no_data"))
    monkeypatch.setattr(llm_research, "fetch_headlines", lambda t, limit=10: [])
    monkeypatch.setattr(oak_mcp_client, "is_oak_mcp_configured", lambda: False)
    res = llm_research.research_stock("AAVE", qwen=object(), gemini=object())
    assert res.consensus == "NO_DATA"


# ---------------------------------------------------------------------------
# 2. Modul price_forecast.py: 24h Delta, Breakout, Mean-Reversion & Causality
# ---------------------------------------------------------------------------


def test_compute_price_delta_24h_metrics_and_stock_vs_crypto_bars():
    candles = _synthetic_candles(120, start_price=100.0, drift_per_bar=0.0005, last_vol_mult=1.5)
    d_crypto = price_forecast.compute_price_delta_24h("SOL", candles, interval="1h")
    d_stock = price_forecast.compute_price_delta_24h("NVDA", candles, interval="1h")
    assert d_crypto is not None and d_stock is not None
    assert d_crypto.bars_24h == 24
    assert d_stock.bars_24h == 7
    assert d_crypto.ret_24h_pct > 0
    assert d_crypto.vol_ratio_24h == pytest.approx(1.5, rel=0.05)


def test_forecast_detects_momentum_breakout_long_and_causality():
    candles = _synthetic_candles(140, start_price=100.0, drift_per_bar=0.0004, last_jump_pct=0.025, last_vol_mult=1.6, jump_bars=24)
    close = candles[-1]["c"]
    snap = GravitySnapshot(
        bar_index=len(candles) - 1,
        close=close,
        atr=close * 0.005,
        trend_bias=0.5,
        regime="Trend",
        net_gravity=30.0,
        bullish_pressure=70.0,
        upper=[Magnet(price=close * 1.03, zone_low=close * 1.028, zone_high=close * 1.032, score=65.0, state="ACTIVE", strength=60.0, distance_atr=2.0, accessibility=0.8, freshness=0.9, member_count=2, touches=1, age=10, sources="swing")],
        lower=[],
    )
    res_now = price_forecast.forecast_24h("SOL", candles, gravity_snapshot=snap, ai_score=0.45, min_net_edge_pct=0.004)
    assert res_now is not None
    assert res_now.setup == "MOMENTUM_BREAKOUT"
    assert res_now.side == "long"
    assert res_now.tradeable is True

    # Kausalitas ketat: menambah bar masa depan yang berlawanan tidak boleh mengubah forecast bar `len(candles)-1`
    future = list(candles) + [{"t": candles[-1]["t"] + 3_600_000, "o": close, "h": close, "l": close * 0.5, "c": close * 0.5, "v": 999999.0}]
    res_prefix = price_forecast.forecast_24h("SOL", future[:-1], gravity_snapshot=snap, ai_score=0.45, min_net_edge_pct=0.004)
    assert res_prefix == res_now


def test_forecast_detects_mean_reversion_fade_and_respects_p5_ai_veto():
    candles = _synthetic_candles(140, start_price=100.0, drift_per_bar=0.0002, last_jump_pct=0.08, last_vol_mult=2.0, jump_bars=6)
    close = candles[-1]["c"]
    snap_rej = GravitySnapshot(
        bar_index=len(candles) - 1,
        close=close,
        atr=close * 0.006,
        trend_bias=0.2,
        regime="Range",
        net_gravity=-35.0,
        bullish_pressure=30.0,
        upper=[],
        lower=[Magnet(price=close * 0.96, zone_low=close * 0.958, zone_high=close * 0.962, score=80.0, state="ACTIVE", strength=75.0, distance_atr=2.5, accessibility=0.9, freshness=0.9, member_count=2, touches=2, age=8, sources="swing")],
        rejected_up=[close * 1.002],
    )
    res_fade = price_forecast.forecast_24h("ETH", candles, gravity_snapshot=snap_rej, min_net_edge_pct=0.004)
    assert res_fade is not None
    assert res_fade.setup == "MEAN_REVERSION_FADE"
    assert res_fade.side == "short"
    assert res_fade.tradeable is True

    # Veto P5 >= 0.70 memblokir trade
    res_veto = price_forecast.forecast_24h("ETH", candles, gravity_snapshot=snap_rej, p5_risk=0.75, min_net_edge_pct=0.004)
    assert res_veto is not None and res_veto.tradeable is False

    # Veto AI berlawanan arah (bullish kuat menolak fade short)
    res_ai_veto = price_forecast.forecast_24h("ETH", candles, gravity_snapshot=snap_rej, ai_score=0.60, min_net_edge_pct=0.004)
    assert res_ai_veto is not None and res_ai_veto.tradeable is False


# ---------------------------------------------------------------------------
# 3. Walk-Forward Backtester: SL-before-TP, Time-Stop & Summary
# ---------------------------------------------------------------------------


def test_simulate_trade_24h_checks_sl_before_tp_and_time_stop():
    # Bar 1 mengenai SL DAN TP sekaligus -> konservatif wajib SL
    candles = [
        {"o": 100.0, "h": 100.2, "l": 99.8, "c": 100.0},
        {"o": 100.0, "h": 105.0, "l": 95.0, "c": 103.0},
    ]
    sim_sl = simulate_trade_24h(candles, entry_index=0, side="long", tp_pct=0.025, sl_pct=0.015, max_hold_bars=24)
    assert sim_sl is not None and sim_sl["outcome"] == "SL" and sim_sl["gross_pct"] == pytest.approx(-0.015)

    # Tidak menyentuh SL/TP hingga max_hold_bars=2 -> TIME_STOP pada close bar ke-2
    flat = [
        {"o": 100.0, "h": 100.2, "l": 99.8, "c": 100.0},
        {"o": 100.0, "h": 100.5, "l": 99.5, "c": 100.4},
        {"o": 100.4, "h": 100.8, "l": 99.9, "c": 100.6},
    ]
    sim_ts = simulate_trade_24h(flat, entry_index=0, side="long", tp_pct=0.025, sl_pct=0.015, max_hold_bars=2)
    assert sim_ts is not None and sim_ts["outcome"] == "TIME_STOP" and sim_ts["gross_pct"] == pytest.approx(0.006)


def test_collect_and_summarise_trades_offline():
    candles = _synthetic_candles(140, start_price=100.0, drift_per_bar=0.0005, last_jump_pct=0.02, last_vol_mult=1.5)
    trades = collect_trades_for_asset("BTC", candles, {}, interval="1h", min_net_edge_pct=0.002)
    summary = summarise_trades(trades)
    assert isinstance(summary["trades"], int)
    assert "avg_net_28_pct" in summary and "max_dd_28_pct" in summary
