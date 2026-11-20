"""Parity and invariant tests for the Market Gravity port.

The arithmetic assertions are hand-computable from MG_PORT_SPEC.md, so a regression in
either the transcription or the implementation fails here instead of silently shifting
a diagnostic number in the ledger.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import market_gravity as mg

PARAMS = mg.GravityParams()
ROOT = Path(__file__).resolve().parents[1]


def candles(count: int, start_time: int = 1_700_000_000, interval: int = 3600) -> list[dict]:
    """Deterministic synthetic series with real structure: drift, oscillation, spike."""
    out = []
    for index in range(count):
        drift = index * 0.05
        wave = math.sin(index / 6.0) * 1.2
        spike = 6.0 if index == count // 2 else 0.0
        close = 100.0 + drift + wave + spike
        open_ = close - 0.1
        high = max(open_, close) + 0.4 + spike * 0.6
        low = min(open_, close) - 0.4
        out.append({
            "time": start_time + index * interval,
            "open": open_, "high": high, "low": low, "close": close,
            "volume": 1000.0 + (500.0 if spike else 0.0) + (index % 7) * 10.0,
        })
    return out


# --- arithmetic parity -----------------------------------------------------


def test_density_and_touch_parity():
    # densityScore(n) = clamp01(0.20 + 0.80*(1 - e^(-0.70*(n-1))))
    assert mg.density_score(0) == 0.0
    assert math.isclose(mg.density_score(1), 0.200, abs_tol=1e-9)
    assert math.isclose(mg.density_score(2), 0.603, abs_tol=1e-3)
    assert math.isclose(mg.density_score(3), 0.803, abs_tol=1e-3)
    assert math.isclose(mg.density_score(5), 0.951, abs_tol=1e-3)
    # touchQuality(t) = clamp01(0.20 + 0.80*(1 - e^(-0.80*(t-1))))
    assert math.isclose(mg.touch_quality_score(1), 0.200, abs_tol=1e-9)
    assert math.isclose(mg.touch_quality_score(2), 0.641, abs_tol=1e-3)
    assert math.isclose(mg.touch_quality_score(3), 0.839, abs_tol=1e-3)
    assert mg.density_score(10_000) == 1.0 and mg.touch_quality_score(10_000) == 1.0


def _pool(**overrides):
    base = dict(
        centroid=120.0, origin_bar=0, active_bar=0, legacy_strength=0.5, legacy_volume=0.5,
        sources="Swing High", zone_low=119.5, zone_high=120.5, member_min=119.5, member_max=120.5,
        member_count=1, total_weight=1.0, last_touch_bar=0, pool_atr=1.0, independent_touches=1,
        max_swing_prominence=0.0, swing_evidence_count=0, volume_evidence_sum=0.0, volume_evidence_count=0,
    )
    base.update(overrides)
    return mg.LiquidityPool(**base)


def test_accessibility_is_exactly_half_at_k_atr():
    # normalizedDistance == 1 -> accessibility 1/(1+1) = 0.5, i.e. half at K = 2 ATR.
    pool = _pool(member_count=3, independent_touches=3, max_swing_prominence=0.5, swing_evidence_count=3)
    close = pool.zone_high + PARAMS.accessibility_k_atr * pool.pool_atr
    parts = mg.gravity_components(pool, 0, close, 1.0, 0.0, 1, "Range", 3600.0, PARAMS)
    assert parts["distance_atr"] == PARAMS.accessibility_k_atr
    assert math.isclose(parts["accessibility"], 0.5, abs_tol=1e-12)
    assert math.isclose(parts["distance_multiplier"], 0.775, abs_tol=1e-12)


def test_freshness_halves_at_effective_half_life():
    pool = _pool()
    strength = mg.strength_components(pool, PARAMS)["liquidity_strength"]
    fresh = mg.freshness_value(pool, strength, 0, PARAMS)
    assert fresh["base_half_life"] == float(PARAMS.swing_half_life_bars)
    assert mg.base_half_life(_pool(sources="Previous Day Low"), PARAMS) == 300.0
    assert mg.base_half_life(_pool(sources="Previous Week High"), PARAMS) == 500.0
    assert mg.base_half_life(_pool(sources="Equal Low"), PARAMS) == 250.0
    aged = mg.freshness_value(pool, strength, pool.active_bar + round(fresh["effective_half_life"]), PARAMS)
    assert math.isclose(aged["freshness"], 0.5, abs_tol=1e-3)


def test_strength_weights_and_timeframe_importance():
    assert mg.timeframe_importance_score("Swing High") == 0.35
    assert mg.timeframe_importance_score("Swing Low + Previous Day Low") == 0.70
    assert mg.timeframe_importance_score("Previous Week High") == 1.00
    assert not mg.contains_source("Previous Week High", "Previous Day High")
    assert mg.contains_source("Previous Week High + Swing High", "Swing High")
    # A swing-only pool of one member: density .20, no prominence, touch .20, tf .35, no volume
    pool = _pool()
    parts = mg.strength_components(pool, PARAMS)
    expected = 100.0 * (0.20 * 0.30 + 0.0 + 0.20 * 0.20 + 0.35 * 0.15 + 0.0)
    assert math.isclose(parts["liquidity_strength"], expected, abs_tol=1e-12)


def test_modifier_bounds():
    for bias in (-5.0, -1.0, 0.0, 1.0, 5.0):
        for direction in (1, -1):
            assert 0.75 <= mg.trend_modifier(bias, direction, 3600.0) <= 1.25
            assert 0.80 <= mg.regime_modifier("Trend Expansion", bias, direction, "Equal High") <= 1.20
    assert mg.adaptive_trend_scale(300.0) == 2.00
    assert mg.adaptive_trend_scale(3600.0) == 2.50
    assert mg.adaptive_trend_scale(86_400.0) == 1.00


def test_gravity_is_clamped_to_100():
    pool = _pool(
        sources="Previous Week High + Equal High", zone_low=99.9, zone_high=100.1,
        centroid=100.0, member_min=99.9, member_max=100.1, member_count=8, total_weight=8.0,
        independent_touches=8, max_swing_prominence=1.0, swing_evidence_count=8,
        volume_evidence_sum=8.0, volume_evidence_count=8,
    )
    parts = mg.gravity_components(pool, 0, 100.0, 1.0, 1.0, 1, "Trend Expansion", 3600.0, PARAMS)
    # Components approach 1.0 asymptotically, so strength is just under 100 - and the
    # trend (1.25) x regime (1.20) product is what forces the clamp.
    assert 99.5 <= parts["liquidity_strength"] < 100.0
    assert parts["trend_modifier"] == 1.25
    assert parts["regime_modifier"] > 1.0
    assert parts["gravity"] <= 100.0


# --- structural behaviour --------------------------------------------------


def test_zone_bounds_isolated_clustered_and_capped():
    low, high = mg.zone_bounds(100.0, 100.0, 1, 100.0, 1.0, PARAMS)
    assert math.isclose(high - low, 2 * PARAMS.isolated_zone_half_atr)
    # Padding applies outside member extremes; the width cap is 0.50 ATR, so the span
    # here must stay below it or re-centring takes over.
    low, high = mg.zone_bounds(100.0, 100.05, 2, 100.025, 1.0, PARAMS)
    assert math.isclose(low, 100.0 - PARAMS.cluster_zone_padding_atr)
    assert math.isclose(high, 100.05 + PARAMS.cluster_zone_padding_atr)
    # A 10-unit span with ATR 1.0 is re-centred onto the 0.50 ATR cap.
    low, high = mg.zone_bounds(100.0, 110.0, 5, 105.0, 1.0, PARAMS)
    assert math.isclose(high - low, PARAMS.max_zone_width_atr)
    assert math.isclose((low + high) / 2.0, 105.0)
    assert math.isnan(mg.zone_bounds(100.0, 100.0, 1, 100.0, 0.0, PARAMS)[0])


def test_new_pool_starts_with_one_touch_and_forming_state():
    side = mg.SidePools(PARAMS)
    side.add_member(price=150.0, new_bar=10, new_active_bar=15, strength=0.6, volume=0.7,
                    swing_prominence=0.6, volume_score=0.7, source="Swing High",
                    member_weight=1.0, new_atr=1.0, counts_as_touch=True, touch_bar=10,
                    tolerance=0.15)
    assert len(side.pools) == 1
    pool = side.pools[0]
    assert pool.independent_touches == 1
    assert pool.state == mg.FORMING
    assert pool.member_count == 1


def test_merge_requires_tolerance_and_rejects_resolved_pools():
    side = mg.SidePools(PARAMS)
    common = dict(new_bar=10, new_active_bar=15, strength=0.6, volume=0.7, swing_prominence=0.6,
                  volume_score=0.7, source="Swing High", member_weight=1.0, new_atr=1.0,
                  counts_as_touch=True, touch_bar=10, tolerance=0.15)
    side.add_member(price=150.0, **common)
    side.add_member(price=150.05, **common)          # within 0.15 ATR -> merge
    assert len(side.pools) == 1
    assert side.pools[0].member_count == 2
    side.add_member(price=151.0, **common)           # 1 ATR away -> new pool
    assert len(side.pools) == 2
    side.pools[0].state = mg.ACCEPTED                 # resolved pools must not absorb members
    side.add_member(price=150.01, **common)
    assert len(side.pools) == 3


def test_independent_touch_needs_bar_separation():
    side = mg.SidePools(PARAMS)
    common = dict(new_bar=10, new_active_bar=15, strength=0.6, volume=0.7, swing_prominence=0.6,
                  volume_score=0.7, source="Swing High", member_weight=1.0, new_atr=1.0,
                  counts_as_touch=True, tolerance=0.15)
    side.add_member(price=150.0, touch_bar=100, **common)
    side.add_member(price=150.02, touch_bar=105, **common)   # only 5 bars apart -> no extra touch
    assert side.pools[0].independent_touches == 1
    side.add_member(price=150.04, touch_bar=120, **common)   # 20 bars apart -> counts
    assert side.pools[0].independent_touches == 2


def test_store_recent_pivot_memory_is_bounded():
    side = mg.SidePools(PARAMS)
    for index in range(PARAMS.recent_pivot_memory + 25):
        side.store_recent_pivot(float(index), index)
    assert len(side.recent_prices) == PARAMS.recent_pivot_memory
    assert side.recent_prices[-1] == float(PARAMS.recent_pivot_memory + 24)


def test_pivots_never_leak_before_confirmation():
    series = candles(420)
    spike = len(series) // 2
    spike_price = series[spike]["high"]
    before = mg.analyze_market_gravity(series[:spike], PARAMS)
    assert before.status in {"ok", "insufficient_data"}
    for magnet in before.upper:
        assert not math.isclose(magnet.price, spike_price, rel_tol=1e-9)


def test_deterministic_and_stateless():
    series = candles(500)
    first = mg.analyze_market_gravity(series, PARAMS).to_scan_fields()
    second = mg.analyze_market_gravity(list(series), PARAMS).to_scan_fields()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_short_window_is_honest_not_zero():
    result = mg.analyze_market_gravity(candles(30), PARAMS)
    assert result.status == "insufficient_data"
    fields = result.to_scan_fields()
    assert fields["mg_net_gravity"] == ""      # never 0.0 pretending to be a measurement
    assert result.usable is False


def test_full_window_produces_ranked_magnets():
    result = mg.analyze_market_gravity(candles(600), PARAMS)
    assert result.status == "ok", result.notes
    assert result.regime in {"Trend Expansion", "Trend", "Range", "Compression"}
    assert 0.0 <= result.bullish_pressure <= 100.0
    assert math.isclose(result.bullish_pressure + result.bearish_pressure, 100.0)
    assert math.isclose(result.bullish_pressure, 50.0 + result.net_gravity / 2.0, abs_tol=1e-9)
    assert len(result.upper) <= 3 and len(result.lower) <= 3
    for magnet in result.upper + result.lower:
        assert magnet.state in {"Active", "Approaching"}   # resolved/forming pools never rank
        assert 0.0 <= magnet.score <= 100.0


def test_missing_timestamps_disable_htf_sources_instead_of_guessing():
    series = [dict(candle) for candle in candles(300)]
    for candle in series:
        candle.pop("time")
    result = mg.analyze_market_gravity(series, PARAMS)
    assert any("timestamps" in note for note in result.notes)


# --- integration invariants ------------------------------------------------


def test_decision_path_does_not_import_market_gravity():
    """Fase 4 guard: diagnostics only. Entry logic must not consume this module yet."""
    for name in ("walt_bot.py", "llm_research.py", "sheet_logger.py", "strategy_config.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+market_gravity", text, re.MULTILINE), name


def test_source_notice_and_spec_are_retained():
    text = (ROOT / "market_gravity.py").read_text(encoding="utf-8")
    assert "Mozilla Public License 2.0" in text
    assert "not probabilities" in text.lower()
    spec = (ROOT / "MG_PORT_SPEC.md").read_text(encoding="utf-8")
    assert "0.6931471805599453" in spec and "L1567" in spec

