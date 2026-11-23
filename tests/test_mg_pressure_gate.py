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


def _cfg_gates(monkeypatch, lt, st, min_s=None, max_p=None, curve=None):
    payload = {"grav_long_min_bull_pressure": lt, "grav_short_min_bear_pressure": st}
    if min_s is not None:
        payload["grav_boost_min_score"] = min_s
    if max_p is not None:
        payload["grav_boost_max_pct"] = max_p
    if curve is not None:
        payload["grav_boost_curve"] = curve
    monkeypatch.setenv("STRATEGY_CONFIG_JSON", json.dumps(payload))
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


def test_long_fade_structure_only_informs_never_vetoes(monkeypatch):
    _cfg_gates(monkeypatch, 52.0, 0.0)
    c4 = _flat_candles()
    c1 = _stretched(_flat_candles(), 0.986)  # flush ekstrem -> V4 LONG
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 40.0)
    sig = pf.evaluate_crypto_live_fade_signal("SOL", c4, c1, funding_daily=0.0005)
    assert sig is not None and sig["side"] == "long"  # TIDAK diveto struktur
    assert sig["bull_pressure"] == 40.0
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 60.0)
    sig2 = pf.evaluate_crypto_live_fade_signal("SOL", c4, c1, funding_daily=0.0005)
    assert sig2 is not None and sig2["bull_pressure"] == 60.0


def test_short_fade_structure_only_informs_never_vetoes(monkeypatch):
    _cfg_gates(monkeypatch, 0.0, 55.0)
    c4 = _flat_candles()
    c1 = _stretched(_flat_candles(), 1.014)  # spike -> V4 SHORT
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 80.0)  # bear 20 < 55
    sig = pf.evaluate_crypto_live_fade_signal("SOL", c4, c1, funding_daily=0.0005)
    assert sig is not None and sig["side"] == "short" and sig["bull_pressure"] == 80.0
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 20.0)  # bear 80 >= 55
    sig2 = pf.evaluate_crypto_live_fade_signal("SOL", c4, c1, funding_daily=0.0005)
    assert sig2 is not None and sig2["bull_pressure"] == 20.0


def test_stock_dip_structure_only_informs_never_vetoes(monkeypatch):
    _cfg_gates(monkeypatch, 52.0, 0.0)
    dip = _stretched(_flat_candles(), 0.985, tail=5, vol=8000.0)
    monkeypatch.setattr(pf, "_gravity_bull_pressure", lambda candles: 45.0)
    sig = pf.evaluate_stock_live_dip_signal("NVDA", candles_1h=dip, candles_1d=None,
                                            funding_daily=-0.0005)
    assert sig is not None and sig["side"] == "long" and sig["bull_pressure"] == 45.0


def _research_neutral(consensus="STRONG"):
    return llm_research.Research(
        ticker="SOL", days_to_earnings=None, news_sentiment="neutral", narrative="x",
        confidence=0.7, fetched_at="2026-11-24T00:00:00+00:00", consensus=consensus,
        overall_consensus_level=consensus, vendor_alignment="aligned", final_score=0.05)


def _opp_e2(side="long", grav=None):
    return Opportunity(contract_id=1, base_asset="SOL", funding_daily=0.0005, volume_24h=1e7,
                       max_leverage=2, side=side, signal_source="price_forecast",
                       forecast_setup="V5_4H_REJ_COMFORT", grav_bull_pressure=grav)


def test_go_nogo_boost_only_above_score_80_and_grows_with_score(monkeypatch):
    _cfg_gates(monkeypatch, 52.0, 55.0)
    # 64.6 searah tapi <= 80 -> TANPA boost (pengaruh belum aktif)
    v0, r0, m0 = walt_bot.decide_go_nogo(_opp_e2("long", 64.6), _research_neutral())
    assert v0 == "GO" and m0 == pytest.approx(1.0) and "≤ ambang pengaruh 80" in r0
    # Kurva monoton naik: 85 -> +3.125%, 90 -> +8.84%, 100 -> +25% (cap)
    m85 = walt_bot.decide_go_nogo(_opp_e2("long", 85.0), _research_neutral())[2]
    m90 = walt_bot.decide_go_nogo(_opp_e2("long", 90.0), _research_neutral())[2]
    m100 = walt_bot.decide_go_nogo(_opp_e2("long", 100.0), _research_neutral())[2]
    assert m85 == pytest.approx(1.03125, rel=1e-3)
    assert m90 == pytest.approx(1.0884, rel=1e-3)
    assert m100 == pytest.approx(1.25, rel=1e-6)
    assert m85 < m90 < m100  # "semakin kuat pengaruhnya"
    # SHORT: bear 80 persis -> belum > 80 (tanpa boost); bear 85 -> boost kurva
    m_b80 = walt_bot.decide_go_nogo(_opp_e2("short", 20.0), _research_neutral())[2]
    m_b85 = walt_bot.decide_go_nogo(_opp_e2("short", 15.0), _research_neutral())[2]
    assert m_b80 == pytest.approx(1.0)
    assert m_b85 == pytest.approx(1.03125, rel=1e-3)
    # tak searah -> tanpa penalti, AI tetap pemutus
    v2, r2, m2 = walt_bot.decide_go_nogo(_opp_e2("long", 40.0), _research_neutral())
    assert v2 == "GO" and m2 == pytest.approx(1.0) and "tak searah" in r2
    # tanpa data gravity -> perilaku lama utuh
    _v5, r5, m5 = walt_bot.decide_go_nogo(_opp_e2("long", None), _research_neutral())
    assert m5 == pytest.approx(1.0) and "🧲" not in r5


def test_go_nogo_curve_configurable_half_and_skip_guards(monkeypatch):
    # Konfigurasi kustom: min 60, max 40%, kurva linear -> skor 80 = tengah = +20%
    _cfg_gates(monkeypatch, 52.0, 55.0, min_s=60.0, max_p=0.40, curve=1.0)
    _v, _r, m_custom = walt_bot.decide_go_nogo(_opp_e2("long", 80.0), _research_neutral())
    assert m_custom == pytest.approx(1.20, rel=1e-6)

    # HALF (SPLIT) + skor 100 -> 0.5 * 1.25 = 0.625
    _cfg_gates(monkeypatch, 52.0, 55.0)
    _v2, r_half, m_half = walt_bot.decide_go_nogo(_opp_e2("long", 100.0), _research_neutral(consensus="SPLIT"))
    assert m_half == pytest.approx(0.625, rel=1e-6) and "🧲" in r_half

    # SKIP (P5 tinggi) TIDAK boleh dinaikkan apa pun meski skor 100
    res_bad = llm_research.Research(
        ticker="SOL", days_to_earnings=None, news_sentiment="neutral", narrative="x",
        confidence=0.7, fetched_at="2026-11-24T00:00:00+00:00", consensus="STRONG",
        overall_consensus_level="STRONG", vendor_alignment="aligned", final_score=0.05,
        p1_to_p7_scores={"P5_risk_catalysts": 0.9})
    v3, r3, m3 = walt_bot.decide_go_nogo(_opp_e2("long", 100.0), res_bad)
    assert v3 == "SKIP" and m3 == 0.0 and "🧲" not in r3


def test_context_block_carries_pressure_and_gates_as_ai_material(monkeypatch):
    _cfg_gates(monkeypatch, 52.0, 55.0)
    txt = llm_research._build_strategy_context_block(
        "SOL", funding_daily=0.0005, recommended_side="long", grav_pressure=63.4)
    assert "bullish_pressure=63.4/100" in txt
    assert "total selalu 100" in txt
    assert "pemutus UTAMA" in txt          # AI (berita+OAK MCP) = pemutus utama
    assert "PENGUAT keputusan, bukan penentu" in txt
    assert "LONG bila bull >= 52" in txt
    assert "BARU aktif bila skor > 80" in txt
    assert "kurva ^1.5" in txt
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
