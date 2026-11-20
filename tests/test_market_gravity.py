"""Parity and invariant tests for the Market Gravity port.

The arithmetic assertions are hand-computable from MG_PORT_SPEC.md, so a regression in
either the transcription or the implementation fails here instead of silently shifting
a diagnostic number in the ledger.
"""

from __future__ import annotations

import json
import math
import re
import sys
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


def test_decision_modules_never_touch_gravity_output():
    """Fase 4 penjaga: modul keputusan buta terhadap Market Gravity."""
    for name in ("walt_bot.py", "llm_research.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "market_gravity" not in text, f"{name} tidak boleh mengimpor model gravitasi"
        assert "mg_" not in text, f"{name} tidak boleh membaca kolom diagnostik mg_*"


def test_gravity_import_is_confined_to_the_logging_layer():
    """Satu-satunya tempat boleh mengimpor market_gravity: gravity_diagnostics()."""
    text = (ROOT / "sheet_logger.py").read_text(encoding="utf-8")
    occurrences = [match.start() for match in re.finditer(r"market_gravity", text)]
    assert occurrences, "diagnostik seharusnya memanggil model dari lapisan logging"
    body_start = text.index("def gravity_diagnostics(")
    body_end = text.index("def gravity_diagnostics_summary(")
    stray = [pos for pos in occurrences if not body_start < pos < body_end]
    assert not stray, f"impor market_gravity di luar fungsi diagnostik: {len(stray)} tempat"


def _scan_row(tmp_path, monkeypatch, diagnostics: dict):
    import sheet_logger

    monkeypatch.setattr(sheet_logger, "gravity_diagnostics", lambda asset: dict(diagnostics))
    summary = {
        "ts": "2026-11-21T00:00:00+00:00",
        "balance": [{"balance_available": "100.0"}],
        "positions_count": 0,
        "top_opportunities": [{"asset": "TEST", "side": "long", "funding_daily_pct": 0.5, "price": 100.0}],
    }
    stamp = "on" if any(diagnostics.get(name) for name in sheet_logger.MG_COLUMNS) else "off"
    rows = sheet_logger.log_scan_to_csv(summary, {}, filepath=str(tmp_path / f"scan_{stamp}.csv"))
    assert rows, "satu peluang harus menghasilkan satu baris"
    return rows[0]


def test_diagnostic_columns_cannot_move_decision_values(tmp_path, monkeypatch):
    """Kolom mg_* berubah -> kolom keputusan (verdict/margin/reason) harus identik."""
    import sheet_logger

    blank = _scan_row(tmp_path, monkeypatch, dict(sheet_logger.MG_BLANK))
    filled = dict(sheet_logger.MG_BLANK)
    filled.update({"mg_status": "ok", "mg_net_gravity": "42.0", "mg_regime": "Trend",
                   "mg_upper_1_price": "123.45", "mg_upper_1_score": "77.7"})
    row = _scan_row(tmp_path, monkeypatch, filled)

    width = len(sheet_logger.DAILY_SCANS_HEADERS)
    assert len(blank) == width and len(row) == width
    mg_start = width - len(sheet_logger.MG_COLUMNS)
    assert blank[:mg_start] == row[:mg_start], "kolom non-diagnostik bergeser saat mg_* berubah"
    assert row[mg_start:] == [filled[name] for name in sheet_logger.MG_COLUMNS]
    # Jendela kosong harus tetap kosong, bukan 0.0 yang terbaca sebagai "tidak ada magnet".
    assert blank[sheet_logger.DAILY_SCANS_HEADERS.index("mg_net_gravity")] == ""


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if payload is not None else "<html>Webhook is active!</html>"

    def json(self):
        if self._payload is None:
            raise ValueError("bukan JSON")
        return self._payload


def test_webhook_write_must_be_proven_not_just_accepted():
    import sheet_logger

    ok, status, _ = sheet_logger._interpret_write_response(
        _Resp({"status": "success", "version": "2026-11-21.1", "rows_added": 2, "verified": True}), 2, "Daily_Scans")
    assert ok and status == "ok"

    ok, status, detail = sheet_logger._interpret_write_response(_Resp(None), 2, "Daily_Scans")
    assert not ok and status == "unverified_legacy_response"
    assert "Redeploy" in detail

    ok, status, _ = sheet_logger._interpret_write_response(
        _Resp({"status": "success", "rows_added": 1, "verified": True}), 2, "Daily_Scans")
    assert not ok and status == "not_verified"          # baris hilang: tidak boleh dilaporkan sukses

    ok, status, _ = sheet_logger._interpret_write_response(
        _Resp({"status": "success", "rows_added": 2, "verified": True,
               "mismatch_columns": [{"column": "price"}]}), 2, "Daily_Scans")
    assert not ok and status == "not_verified"          # read-back beda: tulis tidak terbukti

    ok, status, _ = sheet_logger._interpret_write_response(
        _Resp({"status": "success", "rows_added": 0, "rows_skipped": ["x", "y"]}), 2, "Daily_Scans")
    assert ok and status.startswith("ok")                 # dedupe: bukan kegagalan


def test_parity_tool_refuses_to_compare_different_moments():
    """Angka dari dua waktu berbeda tidak boleh menghasilkan kesimpulan harga."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("gravity_parity_check", ROOT / "tools" / "gravity_parity_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ours = 1_764_000_000.0
    aligned, why = module._within_asof_window(ours, None, 2)
    assert not aligned and "asof_utc" in why

    aligned, why = module._within_asof_window(ours, ours + 14 * 3600, 2)
    assert not aligned and "14.0 jam" in why

    aligned, why = module._within_asof_window(ours, ours + 600, 2)
    assert aligned and "menit" in why

    assert module._parse_time("2026-11-20T13:50:45Z") == 1795182645.0
    assert module._parse_time(1_764_000_000_000) == 1_764_000_000.0
    assert module._parse_time("bukan waktu") is None


def test_parity_tool_template_is_printable(capsys):
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gravity_parity_check.py"), "--template"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "tolerance_pct" in proc.stdout and "Google Finance" in proc.stdout


def test_scan_payload_is_name_keyed_for_header_reconciliation():

    import sheet_logger

    sent = {}

    def fake_post(payload, url):
        sent.update(payload)
        return True

    original = sheet_logger._post_webhook
    sheet_logger._post_webhook = fake_post
    try:
        sheet_logger.log_scan_via_webhook(
            [["ts", "1.0", "0", "BTC", "LONG"] + [""] * (len(sheet_logger.DAILY_SCANS_HEADERS) - 5)],
            "https://script.google.com/macros/s/x/exec",
        )
    finally:
        sheet_logger._post_webhook = original

    assert sent["header"] == sheet_logger.DAILY_SCANS_HEADERS
    assert len(sent["records"]) == 1
    assert sent["records"][0]["asset"] == "BTC"
    assert "mg_status" in sent["records"][0]
    assert all(name in sent["header"] for name in sheet_logger.MG_COLUMNS)



def test_source_notice_and_spec_are_retained():
    text = (ROOT / "market_gravity.py").read_text(encoding="utf-8")
    assert "Mozilla Public License 2.0" in text
    assert "not probabilities" in text.lower()
    spec = (ROOT / "MG_PORT_SPEC.md").read_text(encoding="utf-8")
    assert "0.6931471805599453" in spec and "L1567" in spec

