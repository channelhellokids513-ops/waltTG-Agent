"""gravity_strategy - kalibrasi, validasi, dan uji aturan Market Gravity pada data nyata.

Dokumen referensi (penelitian_model_market_gravity_implementasi_python.md) menambahkan
dua hal di atas port kita: Comfort Point dan Monte Carlo first-passage. Bentuknya benar;
yang belum ada adalah ANGKA kalibrasinya - dokumen memakai alpha=0.12, beta=0.08,
gamma=0.65 yang tidak pernah difit ke data. Alat ini:

  1. mengulang bar secara kausal (trace per bar) untuk mendapatkan snapshot yang hanya
     memakai informasi sampai bar itu;
  2. MEMFIT alpha/beta/gamma dari realised 1-bar return (dan melaporkan r2, karena
     regresi yang lemah harus terlihat lemah);
  3. VALIDASI probabilitas first-passage: bandingkan prediksi vs kejadian nyata per
     decile. Bila prediksi 70% hanya terjadi 52%, angka itu tidak boleh dipakai
     sebagai dasar posisi;
  4. menguji aturan "fade rejected sweep -> target Comfort Point" dengan resolusi
     intrabar konservatif yang sama seperti rr_scan, plus pembanding null.

Read-only: tidak menulis ledger, sheet, atau order.

Pemakaian:
    python tools/gravity_strategy.py                     # 20 koin, bar 1d
    python tools/gravity_strategy.py --interval 4h --limit 900 --horizon 10
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_chart import fetch_candles  # noqa: E402
from market_gravity import (  # noqa: E402
    GravityParams,
    analyze_market_gravity,
    calibrate_diffusion,
    comfort_points,
    forecast_first_passage,
)

CRYPTO_UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "AVAX", "SUI", "LINK", "NEAR", "AAVE",
                  "ARB", "OP", "DOGE", "ADA", "DOT", "ATOM", "LTC", "UNI", "APT", "SEI"]
COSTS = {"fee_saja": 0.0018, "fee_slippage_venue": 0.0118}


def resolve_abs(candles, start, direction, tp_price, sl_price, max_bars=400):
    """(r_multiple, bars) terhadap jarak SL; None bila tidak selesai. SL dicek lebih dulu."""
    entry = candles[start]["c"]
    risk = abs(entry - sl_price)
    if risk <= 0:
        return None, 0
    for offset in range(1, max_bars):
        index = start + offset
        if index >= len(candles):
            return None, 0
        high, low = candles[index]["h"], candles[index]["l"]
        hit_sl = low <= sl_price if direction == 1 else high >= sl_price
        hit_tp = high >= tp_price if direction == 1 else low <= tp_price
        if hit_sl:
            return -1.0, offset
        if hit_tp:
            gain = abs(tp_price - entry)
            return gain / risk, offset
    return None, 0


def realised_first(snapshots, candles, index, horizon):
    """Sisi mana yang benar-benar tersentuh lebih dulu dalam horizon (None bila tidak ada dua-duanya)."""
    snapshot = snapshots[index]
    if not snapshot.upper or not snapshot.lower:
        return None
    up = snapshot.upper[0].zone_low
    dn = snapshot.lower[0].zone_high
    for offset in range(1, horizon + 1):
        position = index + offset
        if position >= len(candles):
            return None
        high, low = candles[position]["h"], candles[position]["l"]
        up_hit, dn_hit = high >= up, low <= dn
        if up_hit and dn_hit:
            return "both"
        if up_hit:
            return "upper"
        if dn_hit:
            return "lower"
    return "none"


def run_rule(data, mode: str, side: str, buffer_atr: float, bounds=None) -> list[dict]:
    """Satu pass aturan fade-rejected-sweep untuk satu bentuk target.

    Kausal: sinyal pada bar i hanya memakai snapshot bar i (yang sendiri sudah
    dihasilkan dari data <= i), dieksekusi pada close bar i, dan diselesaikan pada
    bar > i. `bounds=(lo, hi)` MEMBATASI BAR ENTRY - inilah yang membuat walk-forward
    jujur: trade sebuah fold hanya boleh dibuka di dalam fold itu.
    `side="null"` memancarkan segalanya lewat harga entry (arah, target, stop dibalik),
    sehingga satu-satunya yang berbeda adalah arahnya.
    """
    multiples = {"R1": 1.0, "R2": 2.0, "R3": 3.0}
    if mode not in multiples and mode != "magnet":
        raise ValueError(f"mode tidak dikenal: {mode}")
    trades: list[dict] = []
    for asset, candles, snapshots in data:
        open_until = -1
        span = range(1, len(snapshots) - 1) if bounds is None else range(max(1, bounds[0]), min(len(snapshots) - 1, bounds[1]))
        for index in span:
            if index <= open_until:
                continue
            snapshot = snapshots[index]
            comfort = comfort_points(snapshot)
            entry = snapshot.close
            buffer_price = params_atr(snapshot) * buffer_atr
            if snapshot.rejected_up and comfort["structural"] < entry:
                direction, stop = -1, max(snapshot.rejected_up) + buffer_price
            elif snapshot.rejected_down and comfort["structural"] > entry:
                direction, stop = 1, min(snapshot.rejected_down) - buffer_price
            else:
                continue
            distance = abs(entry - stop)
            if distance <= 0:
                continue
            if mode == "magnet":
                magnet = None
                # Target magnet untuk FADE ada di SEARAH posisi: short setelah rejected
                # upside sweep mengejar magnet bawah, long mengejar magnet atas.
                if direction == -1 and snapshot.lower:
                    magnet = snapshot.lower[0].zone_high
                elif direction == 1 and snapshot.upper:
                    magnet = snapshot.upper[0].zone_low
                if magnet is None or (magnet - entry) * direction <= 0:
                    continue
                tp_price = magnet
            else:
                tp_price = entry + direction * multiples[mode] * distance
            if side == "null":
                direction, tp_price, stop = -direction, 2 * entry - tp_price, 2 * entry - stop
            outcome, held = resolve_abs(candles, index, direction, tp_price, stop)
            held = held or 1
            open_until = index + max(1, held)
            if outcome is None:
                continue
            risk = abs(entry - stop)
            trades.append({
                "asset": asset, "index": index, "direction": direction, "entry": entry,
                "outcome_r": outcome, "held": held,
                "cost_fee_r": COSTS["fee_saja"] * entry / risk,
                "cost_venue_r": COSTS["fee_slippage_venue"] * entry / risk,
                "risk_pct": risk / entry * 100.0,
            })
    return trades


def summarise(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "win_pct": 0.0, "gross_r": 0.0, "net_fee_r": 0.0,
                "net_venue_r": 0.0, "hold": 0.0, "risk_pct": 0.0}
    n = len(trades)
    gross = sum(t["outcome_r"] for t in trades) / n
    return {
        "trades": n,
        "win_pct": sum(1 for t in trades if t["outcome_r"] > 0) / n * 100.0,
        "gross_r": gross,
        "net_fee_r": gross - sum(t["cost_fee_r"] for t in trades) / n,
        "net_venue_r": gross - sum(t["cost_venue_r"] for t in trades) / n,
        "hold": sum(t["held"] for t in trades) / n,
        "risk_pct": sum(t["risk_pct"] for t in trades) / n,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Kalibrasi + validasi + uji aturan Market Gravity (read-only).")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--limit", type=int, default=750)
    parser.add_argument("--horizon", type=int, default=10)
    parser.add_argument("--sims", type=int, default=800)
    parser.add_argument("--atr-buffer", type=float, default=0.5)
    parser.add_argument("--assets", default=",".join(CRYPTO_UNIVERSE))
    args = parser.parse_args()

    params = GravityParams()
    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    data: list[tuple[str, list, list]] = []
    for asset in assets:
        candles, source = fetch_candles(asset, interval=args.interval, limit=args.limit)
        if len(candles) < 200:
            print(f"  {asset:<6} dilewati: hanya {len(candles)} bar ({source})")
            continue
        result = analyze_market_gravity(candles, params, trace_every=1)
        if not result.usable or len(result.trace) < 60:
            print(f"  {asset:<6} dilewati: status={result.status} trace={len(result.trace)}")
            continue
        data.append((asset, candles, result.trace))
    if not data:
        print("Tidak ada aset dengan data cukup - kesimpulan tidak bisa ditarik.")
        return 1
    print(f"Kalibrasi & validasi pada {len(data)} aset, bar {args.interval}, horizon {args.horizon} bar\n")

    samples = []
    for _asset, _candles, snapshots in data:
        for index in range(len(snapshots) - 1):
            forward = snapshots[index + 1].close - snapshots[index].close
            samples.append((snapshots[index].net_gravity, snapshots[index].trend_bias,
                            snapshots[index].atr, forward))
    fit = calibrate_diffusion(samples)
    print("1) Kalibrasi drift-diffusion dari data (bukan asumsi dokumen):")
    print(f"   n={fit['n']}  alpha={fit['alpha']:+.5f}  beta={fit['beta']:+.5f}  gamma={fit['gamma']:.5f}  R2={fit['r2']:.4f}")
    print("   pembanding dokumen referensi: alpha=+0.12  beta=+0.08  gamma=0.65  (tidak difit)")
    if fit["r2"] < 0.01:
        print("   R2 mendekati nol: gravity/trend TIDAK menjelaskan return 1 bar ke depan.")
        print("   Artinya probabilitas Monte Carlo dari model ini tidak boleh dipakai sebagai")
        print("   dasar ukuran posisi - hanya sebagai ilustrasi sebarannya saja.")
    print()

    # 2) Validasi first-passage: prediksi vs kejadian nyata, per decile.
    #    Sampel diambil berjarak: Monte Carlo murni Python mahal, dan 400 prediksi
    #    sudah cukup untuk melihat apakah kalibrasi probabilitasnya berarti.
    usable_snapshots = sum(max(0, len(snapshots) - args.horizon) for _a, _c, snapshots in data)
    stride = max(1, math.ceil(usable_snapshots / 400))
    print(f"2) Validasi probabilitas 'magnet atas tersentuh lebih dulu' "
          f"(sampel tiap {stride} bar dari {usable_snapshots} snapshot):")
    buckets: dict[int, list[tuple[float, int]]] = {}
    forecasts = 0
    for _asset, candles, snapshots in data:
        for index in range(0, len(snapshots) - args.horizon, stride):
            snapshot = snapshots[index]
            if not snapshot.upper or not snapshot.lower:
                continue
            fc = forecast_first_passage(snapshot, alpha=fit["alpha"], beta=fit["beta"], gamma=fit["gamma"],
                                        horizon_bars=args.horizon, simulations=args.sims)
            if not fc.get("usable"):
                continue
            forecasts += 1
            actual = realised_first(snapshots, candles, index, args.horizon)
            if actual not in ("upper", "lower"):
                continue
            # Bandingkan secara apples-to-apples: model juga harus dikondisikan pada
            # "salah satu magnet pasti tersentuh", kalau tidak pembandingnya bias.
            pu = fc["prob_upper_magnet_first"]
            pl = fc["prob_lower_magnet_first"]
            if pu + pl <= 0:
                continue
            predicted = pu / (pu + pl) * 100.0
            bucket = min(9, int(predicted // 10))
            buckets.setdefault(bucket, []).append((predicted, 1 if actual == "upper" else 0))
    print(f"   {'decile':<10}{'n':>6}{'prediksi%':>11}{'kenyataan%':>12}{'selisih':>9}")
    calibration_error = 0.0
    total_n = 0
    for bucket in sorted(buckets):
        rows = buckets[bucket]
        n = len(rows)
        if n < 25:
            continue
        predicted = sum(p for p, _y in rows) / n
        realised = sum(y for _p, y in rows) / n * 100.0
        calibration_error += abs(predicted - realised) * n
        total_n += n
        print(f"   {bucket * 10}-{bucket * 10 + 9}%{n:6}{predicted:11.1f}{realised:12.1f}{realised - predicted:+9.1f}")
    if total_n:
        print(f"   rata-rata |selisih| kalibrasi = {calibration_error / total_n:.1f} poin persentase (n={total_n})")
    else:
        print("   tidak cukup sampel untuk menilai kalibrasi")
    print()

    # 3) Aturan: fade rejected sweep. Arah dari struktur, BENTUK TARGET diuji beberapa
    #    variasi - hasil pertama menunjukkan arahnya benar, tapi target Comfort Point
    #    terlalu dekat untuk membayar biayanya.
    modes = ("R1", "R2", "R3", "magnet")
    results = {f"{side}_{mode}": run_rule(data, mode, side, args.atr_buffer)
               for mode in modes for side in ("fade", "null")}
    print("3) Aturan 'fade rejected sweep', tiap bentuk target dinilai dengan penjaga")
    print(f"   satu-posisi-per-aset-nya SENDIRI (SL = sweep +/- {args.atr_buffer} ATR):")
    print(f"   {'aturan':<14}{'trades':>8}{'win%':>7}{'grossR':>9}{'net@0,18%':>11}{'net@1,18%':>11}{'tahan':>7}")
    for row in [f"fade_{m}" for m in modes] + [f"null_{m}" for m in modes]:
        summary = summarise(results[row])
        if not summary["trades"]:
            print(f"   {row:<14}{0:8}   tidak ada trade yang terselesaikan dalam jendela")
            continue
        print(f"   {row:<14}{summary['trades']:8}{summary['win_pct']:7.1f}{summary['gross_r']:+9.3f}"
              f"{summary['net_fee_r']:+11.3f}{summary['net_venue_r']:+11.3f}{summary['hold']:7.1f}")
    print("\n   null_* = arah DIBALIK pada entry/SL/jarak TP yang sama. fade harus melampaui")
    print("   baris null-nya; kalau tidak, yang bekerja bukan strukturnya.")
    print("   net di sini MASIH TANPA carry funding - carry dihitung terpisah di rr_scan.")
    print(f"   Semua angka IN-SAMPLE pada {args.limit} bar {args.interval}; bukan bukti, bukan walk-forward.")
    return 0


def params_atr(snapshot) -> float:
    return snapshot.atr if snapshot.atr > 0 else 1.0


if __name__ == "__main__":
    raise SystemExit(main())
