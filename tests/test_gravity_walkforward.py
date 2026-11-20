"""Test mekanisme walk-forward: yang diuji adalah ANTI-CONTEKAN, bukan hasilnya.

Kalau bounds bocor (trade dibuka di luar fold) atau jendela latih menyentuh fold uji,
angka out-of-sample kehilangan artinya dan kita akan memuji diri sendiri dengan percaya
diri. Karena itu penjaganya diuji langsung.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import market_gravity as mg

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gravity_walkforward", ROOT / "tools" / "gravity_walkforward.py")
wf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wf)


def magnet(price: float, atr: float = 2.0) -> mg.Magnet:
    return mg.Magnet(price=price, zone_low=price - 0.5, zone_high=price + 0.5, score=60.0,
                     state="Active", strength=55.0, distance_atr=1.0, accessibility=0.5,
                     freshness=0.9, member_count=2, touches=1, age=10, sources="Swing High")


def snap(index: int, close: float, *, rejected_up=(), rejected_down=(), upper=(), lower=()) -> mg.GravitySnapshot:
    return mg.GravitySnapshot(bar_index=index, close=close, atr=2.0, trend_bias=0.0, regime="Range",
                              net_gravity=0.0, bullish_pressure=50.0, upper=list(upper), lower=list(lower),
                              rejected_up=list(rejected_up), rejected_down=list(rejected_down))


def synthetic(asset: str, bars: int = 120):
    """Zig-zag dengan sweep+rejection terjadwal supaya sinyal pasti muncul."""
    candles, snapshots = [], []
    for index in range(bars):
        close = 100.0 + (index % 7) * 0.8 - 2.0
        candles.append({"c": close, "h": close + 1.0, "l": close - 1.0,
                        "t": 1_700_000_000_000 + index * 86_400_000})
        if index % 11 == 5:
            snapshots.append(snap(index, close, rejected_up=[close + 3.0], upper=[magnet(close + 4.0)],
                                  lower=[magnet(close - 6.0)]))
        else:
            snapshots.append(snap(index, close, upper=[magnet(close + 4.0)], lower=[magnet(close - 6.0)]))
    return (asset, candles, snapshots)


def test_window_pairs_excludes_the_test_fold_and_its_purge_margin():
    pairs = wf.window_pairs(total=100, lo=40, hi=60, purge=5)
    assert pairs == [(1, 35), (65, 99)], pairs
    # run_rule memakai rentang [lo, hi), jadi batas yang boleh disentuh entry adalah
    # lo-1; uji terhadap batas itu, bukan terhadap angka mentah jendela.
    for start, end in pairs:
        assert end - 1 <= 40 - 5 or start >= 60 + 5, (start, end)


def test_window_pairs_drops_windows_that_would_be_too_small():
    # Fold yang hampir menutup seluruh rentang tidak menyisakan jendela latih apa pun.
    assert wf.window_pairs(total=100, lo=0, hi=99, purge=5) == []
    # Sisi kanan tetap boleh dipakai walau kiri habis.
    assert wf.window_pairs(total=100, lo=0, hi=10, purge=5) == [(15, 99)]


def test_run_rule_never_opens_a_trade_outside_its_bounds():
    data = [synthetic("AAA"), synthetic("BBB")]
    for mode in wf.MODES:
        for buffer_atr in wf.BUFFERS:
            for trade in wf.run_rule(data, mode, "fade", buffer_atr, bounds=(30, 70)):
                assert 30 <= trade["index"] < 70, (mode, buffer_atr, trade["index"])


def test_run_rule_rejects_an_unknown_target_mode():
    try:
        wf.run_rule([synthetic("AAA")], "R7", "fade", 0.5)
    except ValueError as exc:
        assert "R7" in str(exc)
    else:
        raise AssertionError("mode tak dikenal harus ditolak, tidak boleh diam-diam")


def test_null_side_is_the_exact_mirror_of_the_fade_side():
    data = [synthetic("AAA")]
    fade = wf.run_rule(data, "R1", "fade", 1.0)
    null = wf.run_rule(data, "R1", "null", 1.0)
    assert fade and null, "sinyal contoh harus menghasilkan trade di kedua sisi"
    assert len(fade) == len(null)
    for left, right in zip(fade, null):
        assert left["index"] == right["index"]
        assert left["direction"] == -right["direction"]
        # jarak stop identik: satu-satunya yang boleh berbeda adalah arahnya
        assert abs(left["risk_pct"] - right["risk_pct"]) < 1e-9, (left, right)


def test_collect_only_uses_the_windows_it_is_given():
    data = [synthetic("AAA"), synthetic("BBB")]
    windows = {"AAA": [(10, 40)], "BBB": [(70, 100)]}
    trades = wf.collect(data, "R1", "fade", 1.0, windows)
    for trade in trades:
        if trade["asset"] == "AAA":
            assert 10 <= trade["index"] < 40
        else:
            assert 70 <= trade["index"] < 100


def test_choose_parameters_picks_the_best_training_ev_and_respects_min_trades():
    data = [synthetic("AAA"), synthetic("BBB")]
    chosen = wf.choose_parameters(data, {"AAA": [(1, 119)], "BBB": [(1, 119)]}, min_trades=1)
    assert chosen is not None
    mode, buffer_atr, summary, ranked = chosen
    assert mode in wf.MODES and buffer_atr in wf.BUFFERS
    assert summary["net_venue_r"] >= max(item[0] for item in ranked) - 1e-12
    assert wf.choose_parameters(data, {"AAA": [(1, 119)], "BBB": [(1, 119)]}, min_trades=10**6) is None


def test_cluster_bootstrap_is_deterministic_and_ordered():
    data = [synthetic("AAA"), synthetic("BBB"), synthetic("CCC")]
    trades = wf.collect(data, "R1", "fade", 1.0, {"AAA": [(1, 119)], "BBB": [(1, 119)], "CCC": [(1, 119)]})
    assert trades, "sinyal contoh harus menghasilkan trade agar test bootstrap tidak hampa"
    first = wf.cluster_bootstrap(trades, draws=200)
    second = wf.cluster_bootstrap(trades, draws=200)
    assert first == second, "bootstrap harus deterministik (seed tetap) supaya bisa direproduksi"
    assert first is None or first[0] <= first[1] <= first[2]
    assert wf.cluster_bootstrap([{"asset": "AAA", "outcome_r": 1.0, "cost_venue_r": 0.1}]) is None


def test_summarise_handles_no_trades_without_dividing_by_zero():
    summary = wf.summarise([])
    assert summary["trades"] == 0 and summary["net_venue_r"] == 0.0
