"""rr_scan - uji kelayakan R:R 1:3 di atas bar yang benar-benar kita perdagangkan.

Pertanyaan pemilik: "strategi dengan RR 1:3 yang winrate-nya di atas 30%".
Alat ini tidak mencari angka yang enak didengar; ia menjawab tiga hal sekaligus:
  1. win rate bruto tiap aturan entry pada TP = 3 x SL
  2. win rate IMPAS pada biaya round-trip nyata (0,18% fee saja vs 1,18% fee+slippage)
  3. EV bersih per trade dalam satuan R

Aturan resolusi: konservatif. Bila TP dan SL tersentuh pada bar yang sama, trade
dihitung RUGI. Satu aset hanya memegang satu posisi pada satu waktu (tidak ada
tumpang-tindih trade, sehingga jumlah trade tidak menggelembung palsu).

Jujur soal batasnya: carry funding TIDAK ikut dihitung, karena kita tidak punya
deret funding historis yang layak (kolom funding di Daily_Scans satuannya campur).
EV harga di sini karena itu UNDERSTATEMENT terhadap strategi carry - bukan pengganti.

Pemakaian:
    python tools/rr_scan.py                      # 4h, 1000 bar, 20 koin
    python tools/rr_scan.py --interval 1h --sl 0.015,0.03
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_chart import fetch_candles  # noqa: E402
from market_gravity import atr_series, sma, _series  # noqa: E402

CRYPTO_UNIVERSE = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "AVAX", "SUI", "LINK", "NEAR", "AAVE",
    "ARB", "OP", "DOGE", "ADA", "DOT", "ATOM", "LTC", "UNI", "APT", "SEI",
]

COSTS = {"fee_saja": 0.0018, "fee_slippage_venue": 0.0118}


def breakeven_win_rate(risk: float, reward_multiple: float, cost: float) -> float:
    """p_impas = (R + c) / ((k+1) R) untuk TP = k x R."""
    return (risk + cost) / ((reward_multiple + 1.0) * risk)


def signals(high, low, close, atr, length: int = 20):
    """Kembalikan {rule: {bar_index: +1 long / -1 short}} yang hanya memakai data <= bar."""
    out: dict[str, dict[int, int]] = {
        "meanrev_long_only": {}, "momentum_breakout": {}, "momentum_breakdown": {},
        "reversal_wick": {}, "random_control": {},
    }
    mean = sma(close, length)
    atr_now = atr
    for i in range(length, len(close)):
        if mean[i] is None or atr_now[i] is None or atr_now[i] <= 0:
            continue
        dist = (close[i] - mean[i]) / atr_now[i]
        if dist <= -1.5:
            out["meanrev_long_only"][i] = 1
        if close[i] > max(high[i - length:i]):
            out["momentum_breakout"][i] = 1
        if close[i] < min(low[i - length:i]):
            out["momentum_breakdown"][i] = -1
        body_top = max(close[i], close[i - 1])
        wick = (high[i] - body_top) / (high[i] - low[i]) if high[i] > low[i] else 0.0
        if wick >= 0.5 and dist < -0.5:
            out["reversal_wick"][i] = 1
        # kontrol acak deterministik: bukan nomor acak, melainkan hash posisi yang stabil
        if ((i * 2654435761) % 997) < 12 and i > length:
            out["random_control"][i] = 1 if close[i] >= close[i - 1] else -1
    return out


def resolve(candles, start: int, direction: int, risk: float, multiple: float) -> tuple[float, int]:
    """Kembalikan (hasil dalam fraksi harga, jumlah bar ditahan) atau (None, 0)."""
    entry = candles[start]["c"]
    tp = entry * (1 + direction * risk * multiple)
    sl = entry * (1 - direction * risk)
    for offset in range(1, 400):
        index = start + offset
        if index >= len(candles):
            return None, 0
        high, low = candles[index]["h"], candles[index]["l"]
        hit_tp = high >= tp if direction == 1 else low <= tp
        hit_sl = low <= sl if direction == 1 else high >= sl
        if hit_sl:          # termasuk kasus keduanya tersentuh: kalah = konservatif
            return -risk, offset
        if hit_tp:
            return risk * multiple, offset
    return None, 0


def scan_asset(asset: str, interval: str, limit: int, risks: list[float], multiple: float):
    candles, source = fetch_candles(asset, interval=interval, limit=limit)
    if len(candles) < 120:
        return source, {}
    high, low, close = _series(candles, "h"), _series(candles, "l"), _series(candles, "c")
    close = [candles[i]["c"] for i in range(len(candles))]
    atr = atr_series([candles[i]["h"] for i in range(len(candles))],
                     [candles[i]["l"] for i in range(len(candles))], close, 14)
    results: dict[str, dict[float, list[float]]] = {}
    for rule, table in signals(high, low, close, atr).items():
        results[rule] = {risk: [0.0, 0.0, 0.0, 0.0] for risk in risks}  # trades, wins, grossR, hold
        for risk in risks:
            i, last = 60, -1
            ordered = sorted(table)
            for i in ordered:
                if i <= last:            # satu posisi per aset: lewati sinyal saat masih terbuka
                    continue
                outcome, held = resolve(candles, i, table[i], risk, multiple)
                if outcome is None:
                    continue
                bucket = results[rule][risk]
                bucket[0] += 1
                bucket[1] += 1 if outcome > 0 else 0
                bucket[2] += outcome / risk          # hasil dalam satuan R
                bucket[3] += held
                last = i + held
    return source, results


def net_of(risk: float, cost: float) -> float:
    """Biaya round-trip dinyatakan dalam satuan R (1 R = jarak stop)."""
    return cost / risk


def carry_adjusted_breakeven(risk: float, hold_days: float, funding_per_day: float, cost: float, multiple: float = 3.0) -> float:
    """Win rate impas bila posisi MENANGKAR funding selama hold_days.

    Carry masuk sebagai arus terpisah dari arah harga: EV = [p.kR - (1-p)R] - c + f*T.
    Karena itu f*T menurunkan win rate yang dibutuhkan, bukan menaikkan TP.
    """
    carry = funding_per_day * hold_days
    return (risk + cost - carry) / ((multiple + 1.0) * risk)


def main() -> int:
    parser = argparse.ArgumentParser(description="Uji kelayakan R:R 1:3 pada bar nyata (read-only).")
    parser.add_argument("--interval", default="4h")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--multiple", type=float, default=3.0)
    parser.add_argument("--sl", default="0.015,0.03,0.05,0.08")
    parser.add_argument("--assets", default=",".join(CRYPTO_UNIVERSE))
    args = parser.parse_args()

    risks = [float(x) for x in args.sl.split(",") if x.strip()]
    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]

    print(f"=== R:R 1:{args.multiple:g} | bar {args.interval} x {args.limit} | {len(assets)} aset ===\n")
    print("Win rate IMPAS (bruto, tanpa alpha) sebagai fungsi lebar stop:")
    print(f"{'SL':>7} {'TP':>8} " + " ".join(f"{name:>22}" for name in COSTS))
    for risk in risks:
        row = f"{risk * 100:6.2f}% {risk * args.multiple * 100:7.2f}% "
        row += " ".join(f"{breakeven_win_rate(risk, args.multiple, c) * 100:20.1f}%" for c in COSTS.values())
        print(row)
    print("\nSyarat 'winrate > 30%' hanya bernilai bila melampaui kolom biaya yang NYATA.\n")

    aggregate: dict[str, dict[float, list[float]]] = {}
    used = 0
    for asset in assets:
        source, results = scan_asset(asset, args.interval, args.limit, risks, args.multiple)
        if not results:
            print(f"  {asset:<6} tanpa data cukup ({source})")
            continue
        used += 1
        for rule, table in results.items():
            bucket = aggregate.setdefault(rule, {risk: [0.0, 0.0, 0.0, 0.0] for risk in risks})
            for risk, values in table.items():
                for slot in range(4):
                    bucket[risk][slot] += values[slot]
    print(f"Aset terpakai: {used}/{len(assets)}\n")

    # --- apakah win rate yang dicari itu alpha, atau sekadar sifat alami pasar? ---
    control = aggregate.get("random_control", {})
    if control:
        print("Pembanding NULL (entry acak deterministik, aturan pasar sama):")
        for risk in risks:
            trades, wins, gross_r, hold = control[risk]
            if trades:
                print(f"  SL {risk * 100:4.1f}% -> win {wins / trades * 100:5.1f}%, gross {gross_r / trades:+.3f}R, tahan {hold / trades:4.1f} bar")
        print("  Win rate yang hanya melampaui 25% (impas bruto 1:3) BELUM berarti strategi:")
        print("  ia harus melampaui baris null di atas pada lebar stop yang sama.\n")

    # --- carry funding: arus yang tidak tampak di kolom netR ---
    bars_per_bar = {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}.get(args.interval, 4.0)
    print("Dampak MENANGKAR funding terhadap win rate yang dibutuhkan (RR 1:3):")
    print(f"{'SL':>7}{'tahan bar':>11}{'hari':>7}{'biaya(R)':>10}{'impas':>8}{'impas+f0,52%':>14}{'impas+f0,85%':>14}")
    for risk in risks:
        hold_bars = 0.0
        count = 0.0
        for rule in aggregate:
            trades, _wins, _gr, hold = aggregate[rule][risk]
            if trades:
                hold_bars += hold
                count += trades
        avg_bars = hold_bars / count if count else 0.0
        days = avg_bars * bars_per_bar / 24.0
        base = breakeven_win_rate(risk, args.multiple, COSTS["fee_slippage_venue"])
        with_low = max(0.0, carry_adjusted_breakeven(risk, days, 0.0052, COSTS["fee_slippage_venue"]))
        with_high = max(0.0, carry_adjusted_breakeven(risk, days, 0.0085, COSTS["fee_slippage_venue"]))
        print(f"{risk * 100:6.1f}%{avg_bars:11.1f}{days:7.1f}{net_of(risk, COSTS['fee_slippage_venue']):10.2f}"
              f"{base * 100:7.1f}%{with_low * 100:13.1f}%{with_high * 100:13.1f}%")
    print("  Kolom terakhir memakai funding HARIAN yang benar-benar teramati (0,52%-0,85%).")
    print("  Bila impas < 30%, 'RR 1:3 + winrate 30%' hidup - tapi karena carry, bukan arah harga.\n")

    print("Hasil gabungan (gross = sebelum biaya; netR = R bersih setelah biaya nyata 1,18%)")
    print(f"{'aturan':<20}{'SL':>7}{'trades':>8}{'win%':>7}{'grossR':>9}{'netR':>8}{'hold(bar)':>11}")
    winner = []
    for rule in sorted(aggregate):
        for risk in risks:
            trades, wins, gross_r, hold = aggregate[rule][risk]
            if trades < 10:
                print(f"{rule:<20}{risk * 100:6.1f}%{int(trades):8}   sampel terlalu kecil")
                continue
            win_rate = wins / trades
            cost_r = (COSTS["fee_slippage_venue"] / risk)          # biaya sebagai fraksi dari 1R
            net_r = gross_r / trades - cost_r
            avg_hold = hold / trades
            flag = " *jujur*" if win_rate > 0.30 and net_r > 0 else ""
            if win_rate > 0.30 and net_r > 0:
                winner.append((rule, risk, win_rate, net_r))
            print(f"{rule:<20}{risk * 100:6.1f}%{int(trades):8}{win_rate * 100:6.1f}%{gross_r / trades:9.3f}{net_r:8.3f}{avg_hold:11.1f}{flag}")

    print()
    if winner:
        print("Lulus dua syarat (winrate>30% DAN EV>0 setelah biaya 1,18%):")
        for rule, risk, wr, net in sorted(winner, key=lambda item: -item[3]):
            print(f"  {rule} SL {risk * 100:.1f}% / TP {risk * args.multiple * 100:.1f}% -> win {wr * 100:.1f}%, net {net:+.3f}R")
        print("\nCatatan: ini IN-SAMPLE pada ~4 bulan bar. Bukan bukti, tapi penyaring hypotheses.")
    else:
        print("TIDAK ADA kombinasi yang lulus kedua syarat pada asumsi biaya 1,18%.")
        print("Artinya: RR 1:3 + winrate 30% tidak runtuh karena aturannya, tapi karena BIAYA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
