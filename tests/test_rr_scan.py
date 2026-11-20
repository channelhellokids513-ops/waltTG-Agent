"""Matematika R:R di tools/rr_scan.py.

Rumus impas ini yang dipakai untuk memutuskan apakah sebuah target win rate berarti.
Kalau salah tanda saja, seluruh kesimpulan strategi di AUDIT 3l ikut salah, jadi ia
diuji dengan angka yang bisa dihitung tangan.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rr_scan", ROOT / "tools" / "rr_scan.py")
rr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rr)


def test_breakeven_without_cost_is_one_over_one_plus_multiple():
    # RR 1:3 tanpa biaya: butuh 1/(1+3) = 25%.
    assert abs(rr.breakeven_win_rate(0.015, 3.0, 0.0) - 0.25) < 1e-12
    # Biaya menambah kebutuhan secara linear terhadap rasio biaya/stop.
    assert abs(rr.breakeven_win_rate(0.015, 3.0, 0.0118) - 0.4466666667) < 1e-9
    # Stop lebih lebar pada biaya yang sama -> biaya jadi porsi lebih kecil dari 1R.
    assert rr.breakeven_win_rate(0.05, 3.0, 0.0118) < rr.breakeven_win_rate(0.015, 3.0, 0.0118)


def test_cost_is_expressed_in_units_of_r():
    assert abs(rr.net_of(0.015, 0.0118) - 0.7866666667) < 1e-9
    assert abs(rr.net_of(0.08, 0.0118) - 0.1475) < 1e-9


def test_carry_lowers_the_win_rate_required():
    # SL 5%, tahan 5.9 hari, funding 0,85%/hari => carry 5,01% menutup hampir seluruh risiko.
    base = rr.breakeven_win_rate(0.05, 3.0, 0.0118)
    carried = rr.carry_adjusted_breakeven(0.05, 5.9, 0.0085, 0.0118)
    assert carried < base
    assert abs(carried - 0.0590) < 0.002


def test_carry_never_flips_the_sign_of_the_requirement_in_the_formula():
    # Carry melebihi (R + c) membuat impas negatif -> pemanggil wajib clamp ke 0.
    assert rr.carry_adjusted_breakeven(0.08, 30.0, 0.0085, 0.0118) < 0.0


def _entry_bar():
    """Bar entry yang jinak: tidak menyentuh TP maupun SL-nya sendiri."""
    return {"c": 100.0, "h": 100.5, "l": 99.5}


def test_entry_bar_is_never_used_to_resolve():
    """Trade dibuka pada close bar itu: bar entry tidak boleh memutuskan hasilnya."""
    single = [_entry_bar()]
    assert rr.resolve(single, 0, 1, 0.05, 3.0) == (None, 0)


def test_same_bar_touch_counts_as_loss():
    """Resolusi intrabar konservatif: TP dan SL tersentuh pada bar yang sama = rugi."""
    both = [_entry_bar(), {"c": 100.0, "h": 105.0, "l": 94.0}]     # SL 95 dan TP 115 tersentuh
    outcome, held = rr.resolve(both, 0, 1, 0.05, 3.0)
    assert held == 1 and abs(outcome + 0.05) < 1e-12

    only_tp = [_entry_bar(), {"c": 100.0, "h": 116.0, "l": 99.0}]
    outcome, _held = rr.resolve(only_tp, 0, 1, 0.05, 3.0)
    assert abs(outcome - 0.15) < 1e-12

    only_sl = [_entry_bar(), {"c": 100.0, "h": 101.0, "l": 94.0}]
    outcome, _held = rr.resolve(only_sl, 0, 1, 0.05, 3.0)
    assert abs(outcome + 0.05) < 1e-12


def test_short_side_geometry_is_mirrored():
    entry = 100.0     # short: TP di 85, SL di 105
    down = [_entry_bar(), {"c": entry, "h": 101.0, "l": 84.0}]
    outcome, _held = rr.resolve(down, 0, -1, 0.05, 3.0)
    assert abs(outcome - 0.15) < 1e-12

    up = [_entry_bar(), {"c": entry, "h": 106.5, "l": 99.0}]
    outcome, _held = rr.resolve(up, 0, -1, 0.05, 3.0)
    assert abs(outcome + 0.05) < 1e-12

    touching_both_short = [_entry_bar(), {"c": entry, "h": 106.0, "l": 84.0}]
    outcome, _held = rr.resolve(touching_both_short, 0, -1, 0.05, 3.0)
    assert outcome < 0



def test_signals_only_look_backwards():
    """Sinyal pada bar i tidak boleh memakai bar > i (anti-lookahead)."""
    high = [10.0 + (i % 5) for i in range(80)]
    low = [h - 1.0 for h in high]
    close = [(h + l) / 2 for h, l in zip(high, low)]
    atr = [1.0] * 80
    table = rr.signals(high, low, close, atr)
    for rule, entries in table.items():
        for index in entries:
            assert 20 <= index < len(close), rule
