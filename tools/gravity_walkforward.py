"""gravity_walkforward - uji OUT-OF-SAMPLE aturan fade rejected sweep.

Kenapa alat ini ada: bentuk target (R1 vs R3) dan lebar stop (0,5 ATR) dipilih SETELAH
melihat seluruh sampel (AUDIT 3m). Melaporkan angka per-fold tanpa MENGULANG pemilihan
itu bukan out-of-sample - itu contekan. Maka untuk tiap fold:

  1. parameter (mode target x buffer stop) DIPILIH hanya dari JENDELA LATIH, memakai EV
     bersih pada biaya yang lebih buruk (1,18%) supaya tidak curang memilih biaya murah;
  2. jendela latih di-PURGE di kedua sisi fold uji: trade yang masih terbuka saat fold
     uji dimulai tidak boleh ikut membentuk pilihan;
  3. trade fold uji hanya boleh DIBUKA di dalam fold itu (bounds pada run_rule);
  4. hasil tiap fold dilaporkan sendiri (stabilitas pilihan itu sendiri adalah bukti),
     lalu digabung; interval dihitung dengan cluster-bootstrap PER ASET, karena trade
     dalam satu aset berkorelasi dan bootstrap i.i.d. mengecilkan ketidakpastian palsu.

Read-only. Tidak menulis ledger, sheet, atau order.

Pemakaian:
    python tools/gravity_walkforward.py
    python tools/gravity_walkforward.py --folds 6 --interval 4h --limit 900
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_chart import fetch_candles  # noqa: E402
from market_gravity import GravityParams, analyze_market_gravity  # noqa: E402
from tools.gravity_strategy import CRYPTO_UNIVERSE, run_rule, summarise  # noqa: E402

MODES = ("R1", "R2", "R3", "magnet")
BUFFERS = (0.25, 0.5, 1.0)


def load(assets, interval, limit, params) -> list:
    data = []
    for asset in assets:
        candles, source = fetch_candles(asset, interval=interval, limit=limit)
        if len(candles) < 250:
            print(f"  {asset:<6} dilewati: {len(candles)} bar ({source})")
            continue
        result = analyze_market_gravity(candles, params, trace_every=1)
        if not result.usable or len(result.trace) < 120:
            print(f"  {asset:<6} dilewati: status={result.status} trace={len(result.trace)}")
            continue
        data.append((asset, candles, result.trace))
    return data


def window_pairs(total: int, lo: int, hi: int, purge: int) -> list[tuple[int, int]]:
    """Jendela latih DI KIRI dan DI KANAN fold uji, dipangkas `purge` bar."""
    pairs: list[tuple[int, int]] = []
    left_end = lo - purge
    if left_end > 2:
        pairs.append((1, left_end))
    right_start = hi + purge
    if right_start < total - 2:
        pairs.append((right_start, total - 1))
    return pairs


def collect(data, mode: str, side: str, buffer_atr: float, windows: dict) -> list[dict]:
    """Jalankan aturan pada jendela yang diberikan (asset -> daftar (lo, hi))."""
    trades: list[dict] = []
    for asset, _candles, _snapshots in data:
        subset = [entry for entry in data if entry[0] == asset]
        for lo, hi in windows.get(asset, []):
            trades.extend(run_rule(subset, mode, side, buffer_atr, bounds=(lo, hi)))
    return trades


def choose_parameters(data, train_windows: dict, min_trades: int):
    """Pilih (mode, buffer) maximizing EV bersih pada biaya terburuk, HANYA di latih."""
    ranked = []
    for mode in MODES:
        for buffer_atr in BUFFERS:
            summary = summarise(collect(data, mode, "fade", buffer_atr, train_windows))
            if summary["trades"] >= min_trades:
                ranked.append((summary["net_venue_r"], mode, buffer_atr, summary))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    _ev, mode, buffer_atr, summary = ranked[0]
    return mode, buffer_atr, summary, ranked


def cluster_bootstrap(trades: list, draws: int = 2000, seed: int = 20261121):
    by_asset: dict[str, list] = {}
    for trade in trades:
        by_asset.setdefault(trade["asset"], []).append(trade)
    assets = sorted(by_asset)
    if len(assets) < 3:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pooled: list[dict] = []
        for _pick in range(len(assets)):
            pooled.extend(by_asset[assets[rng.randrange(len(assets))]])
        if pooled:
            means.append((sum(t["outcome_r"] for t in pooled) - sum(t["cost_venue_r"] for t in pooled)) / len(pooled))
    means.sort()
    return (means[int(len(means) * 0.05)], means[len(means) // 2],
            means[min(len(means) - 1, int(len(means) * 0.95))])


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward ber-purge untuk aturan fade rejected sweep (read-only).")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--limit", type=int, default=750)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--purge", type=int, default=12)
    parser.add_argument("--min-trades", type=int, default=40)
    parser.add_argument("--assets", default=",".join(CRYPTO_UNIVERSE))
    args = parser.parse_args()

    params = GravityParams()
    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    data = load(assets, args.interval, args.limit, params)
    if len(data) < 5:
        print("Aset terpakai terlalu sedikit - walk-forward tidak bisa dipercaya.")
        return 1
    print(f"Walk-forward {args.folds} fold | {len(data)} aset | bar {args.interval} x {args.limit} | purge {args.purge} bar")
    print("Parameter DIPILIH di jendela latih (berbiaya 1,18%), hanya dievaluasi di fold uji.\n")

    shortest = min(len(snapshots) for _a, _c, snapshots in data)
    edges = [int(round(shortest * k / args.folds)) for k in range(args.folds + 1)]

    oos: list[dict] = []
    oos_null: list[dict] = []
    picks: list[str] = []
    print(f"   {'fold':<7}{'bar uji':<14}{'dipilih (latih)':<18}{'EV latih':>10}{'trade uji':>11}{'win%':>7}{'net@1,18%':>11}")
    for fold in range(args.folds):
        lo, hi = edges[fold], edges[fold + 1]
        train_windows = {asset: window_pairs(len(snapshots), lo, hi, args.purge)
                         for asset, _c, snapshots in data}
        test_windows = {asset: [(lo, hi)] for asset, _c, snapshots in data}
        chosen = choose_parameters(data, train_windows, args.min_trades)
        if not chosen:
            print(f"   fold {fold + 1:<3}{f'{lo}-{hi}':<14}tidak ada kandidat >= {args.min_trades} trade")
            continue
        mode, buffer_atr, train_summary, ranked = chosen
        test = collect(data, mode, "fade", buffer_atr, test_windows)
        summary = summarise(test)
        picks.append(f"{mode}@{buffer_atr}")
        oos.extend(test)
        oos_null.extend(collect(data, mode, "null", buffer_atr, test_windows))
        if test:
            margin = ""
            if len(ranked) > 1:
                margin = f"  (pilihan ke-2 {ranked[1][0]:+.3f}R)"
            print(f"   fold {fold + 1:<3}{f'{lo}-{hi}':<14}{f'{mode}@{buffer_atr}':<18}"
                  f"{train_summary['net_venue_r']:+10.3f}{summary['trades']:11}{summary['win_pct']:7.1f}"
                  f"{summary['net_venue_r']:+11.3f}{margin}")
        else:
            print(f"   fold {fold + 1:<3}{f'{lo}-{hi}':<14}{f'{mode}@{buffer_atr}':<18}"
                  f"{train_summary['net_venue_r']:+10.3f}   tidak ada trade uji")

    print()
    overall = summarise(oos)
    null_overall = summarise(oos_null)
    if not overall["trades"]:
        print("Tidak ada satu pun trade out-of-sample - kesimpulan tidak bisa ditarik.")
        return 1
    print("GABUNGAN OUT-OF-SAMPLE (parameter dipilih per fold, tanpa contekan):")
    print(f"   trades={overall['trades']}  win={overall['win_pct']:.1f}%  gross={overall['gross_r']:+.3f}R  "
          f"net@0,18%={overall['net_fee_r']:+.3f}R  net@1,18%={overall['net_venue_r']:+.3f}R  "
          f"tahan={overall['hold']:.1f} bar  stop={overall['risk_pct']:.2f}%")
    print(f"   cerminan (null) pada aturan terpilih: trades={null_overall['trades']} "
          f"win={null_overall['win_pct']:.1f}% net@1,18%={null_overall['net_venue_r']:+.3f}R")
    print(f"   selisih arah di luar sampel = {overall['net_venue_r'] - null_overall['net_venue_r']:+.3f}R")
    interval = cluster_bootstrap(oos)
    if interval:
        low, mid, high = interval
        verdict = "TIDAK berbeda dari nol" if low <= 0 <= high else "berbeda dari nol"
        print(f"   interval 90% cluster-bootstrap (per aset) net@1,18% = [{low:+.3f}, {high:+.3f}] R (median {mid:+.3f}) -> {verdict}")
    print(f"   pilihan per fold: {', '.join(picks) if picks else '-'}")
    if len(set(picks)) > 1:
        print("   pilihan TIDAK stabil antar fold: tepi yang terukur rapuh terhadap periode,")
        print("   dan itu sendiri adalah alasan untuk tidak menaikkan ukuran posisi.")
    print("   Read-only: tidak ada yang ditulis ke ledger, sheet, atau bursa.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
