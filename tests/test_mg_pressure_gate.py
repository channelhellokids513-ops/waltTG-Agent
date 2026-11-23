"""Tes gerbang tekanan Market Gravity (mg_long/short_min_*_pressure) & bahan AI-nya."""
import json

import pytest

import llm_research
import price_forecast as pf
import walt_bot
from walt_bot import Opportunity


def _flat_candles(n=160, base=100.0):
    out, px = [], base
    for i in range(n):
        step = 0.003 if i % 2 == 0 else -0.0028
        o = px
        c = o * (1.0 + step)
        out.append({"t": 1763500000000 + i * 3_600_000, "o": o,
                    "h": max(o, c) * 1.003, "l": min(o, c) * 0.997, "c": c, "v": 2000.0})
        px = c
    return out


def _stretched(candles, factor, tail=8, vol=4000.0):
    px = candles[-tail - 1]["c"]
    for j in range(len(candles) - tail, len(candles)):
        o = px
        c = o * factor
        candles[j] = {"t": candles[j]["t"], "o": o, "h": max(o, c) * 1.002,
                      "l": min(o, c) * 0.999, "c": c, "v": vol}
        px = c
    return candles


def _cfg_gates(monkeypatch, lt, st):
    monkeypatch.setenv("STRATEGY_CONFIG_JSON", json.dumps(
        {"grav_long_min_bull_pressure": lt, "grav_short_min_bear_pressure": st}))
    monkeypatch.setattr(pf, "_MG_GATES_CACHE", (0.0, 0.0, 0.0))


def test_gates_read_config_with_ttl_cache(monkeypatch):
    _cfg_gates(monkeypatch, 52.0, 55.0)
    assert pf._mg_pressure_gates() == (52.0, 55.0)
    _cfg_gates(monkeypatch, 0.0, 0.0)
    assert pf._mg_pressure_gates() == (0.0, 0.0)


def test_gate_zero_defaults_keep_old_behavior(monkeypatch):
    monkeypatch.delenv("STRATEGY_CONFIG_JSON", raising=False)
    monkeypatch.setattr(pf, "_MG_GATES_CACHE", (0.0, 0.0, 0.0))
    c4 = _flat_candles()
    c1 = _stretched(_flat_candles(), 1.014)
    sig = pf.evaluate_crypto_live_fade_signal("SOL", c4, c1, funding_daily=0.0005)
    assert sig is not None and sig["side"] == "short"
    assert "bull_pressure" in sig  # field bahan AI selalu ada


def test_long_fade_vetoed_by_bearish_structure_and_allowed_by_bullish(monkeypatch):
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 40.0)
    _cfg_gates(monkeypatch, 52.0, 0.0)
    c4 = _flat_candles()
    c1 = _stretched(_flat_candles(), 0.986)  # flush ekstrem -> V4 LONG
    base_sig = pf.evaluate_crypto_live_fade_signal("SOL", c4, c1, funding_daily=0.0005)
    assert base_sig is None  # bull 40 < 52 -> struktur memveto LONG
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 60.0)
    sig = pf.evaluate_crypto_live_fade_signal("SOL", c4, c1, funding_daily=0.0005)
    assert sig is not None and sig["side"] == "long" and sig["bull_pressure"] == 60.0


def test_short_fade_requires_bear_pressure_threshold(monkeypatch):
    _cfg_gates(monkeypatch, 0.0, 55.0)
    c4 = _flat_candles()
    c1 = _stretched(_flat_candles(), 1.014)  # spike -> V4 SHORT
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 80.0)  # bear 20 < 55
    assert pf.evaluate_crypto_live_fade_signal("SOL", c4, c1, funding_daily=0.0005) is None
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 20.0)  # bear 80 >= 55
    sig = pf.evaluate_crypto_live_fade_signal("SOL", c4, c1, funding_daily=0.0005)
    assert sig is not None and sig["side"] == "short" and sig["bull_pressure"] == 20.0


def test_stock_dip_long_gated_by_structure(monkeypatch):
    _cfg_gates(monkeypatch, 52.0, 0.0)
    dip = _stretched(_flat_candles(), 0.985, tail=5, vol=8000.0)
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 45.0)
    assert pf.evaluate_stock_live_dip_signal("NVDA", candles_1h=dip, candles_1d=None,
                                             funding_daily=-0.0005) is None
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 58.0)
    sig = pf.evaluate_stock_live_dip_signal("NVDA", candles_1h=dip, candles_1d=None,
                                           funding_daily=-0.0005)
    assert sig is not None and sig["side"] == "long" and sig["bull_pressure"] == 58.0


def test_context_block_carries_pressure_and_gates_as_ai_material(monkeypatch):
    _cfg_gates(monkeypatch, 52.0, 55.0)
    txt = llm_research._build_strategy_context_block(
        "SOL", funding_daily=0.0005, recommended_side="long", grav_pressure=63.4)
    assert "bullish_pressure=63.4%" in txt
    assert "LONG butuh bull pressure >= 52%" in txt
    assert "P4" in txt  # eksplisit memerintahkan voter memperhitungkannya
    plain = llm_research._build_strategy_context_block(
        "SOL", funding_daily=0.0005, recommended_side="long")
    assert "Market Gravity" not in plain


def test_research_stock_wrapper_threads_mg_pressure(monkeypatch):
    captured = {}

    def fake_research(ticker, **kw):
        captured.update(kw)
        return llm_research.Research(
            ticker=ticker, days_to_earnings=None, news_sentiment="neutral",
            narrative="x", confidence=0.5, fetched_at="2026-11-23T00:00:00+00:00",
            consensus="STRONG")

    monkeypatch.setattr(llm_research, "research_stock", fake_research)
    walt_bot.research_stock("SOL", qwen=object(), grav_pressure=61.5)
    assert captured.get("grav_pressure") == 61.5


def test_opportunity_defaults_and_fc_passthrough():
    opp = Opportunity(contract_id=1, base_asset="SOL", funding_daily=0.001,
                      volume_24h=1e6, max_leverage=2, side="long")
    assert opp.grav_bull_pressure is None
    opp2 = Opportunity(contract_id=1, base_asset="SOL", funding_daily=0.001,
                       volume_24h=1e6, max_leverage=2, side="long", grav_bull_pressure=64.6)
    assert opp2.grav_bull_pressure == 64.6
