"""gravity_parity_check - bandingkan jangkar harga Google Finance dengan bar yang kita pakai.

Konteks (Ronde 8): Market Gravity menghitung struktur dari OHLCV venue. Kalau harga
venue kita menyimpang dari sumber independen, SELURUH skor gravitasi ikut salah tanpa
terlihat. Google Finance dipakai sebagai sumber harga independen; ia TIDAK memuat
deret historis maupun nilai indikator Market Gravity, jadi yang dibandingkan di sini
adalah jangkar harga (spot / prev close / high / low), bukan skor.

Format berkas input (JSON):
{
  "tolerance_pct": 0.5,
  "assets": [
    {"asset": "BTC",  "kind": "crypto", "spot": 65000.0, "rolling_24h_high": 66000.0,
     "rolling_24h_low": 64000.0, "prev_close": 64500.0},
    {"asset": "AAPL", "kind": "stock",  "spot": 349.21,  "day_high": 352.0,
     "day_low": 347.0, "prev_close": 349.0}
  ]
}

Bidang yang tidak diisi dianggap TIDAK DIUKUR (dilaporkan SKIP), bukan diasumsikan nol.

Pemakaian:
    python tools/gravity_parity_check.py --template
    python tools/gravity_parity_check.py google_finance.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_chart import fetch_candles  # noqa: E402

TEMPLATE = {
    "tolerance_pct": 0.5,
    "assets": [
        {
            "asset": "BTC",
            "kind": "crypto",
            "asof_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "spot": None,
            "rolling_24h_high": None,
            "rolling_24h_low": None,
            "prev_close": None,
        }
    ],
}

# (bidang Google Finance, pertanyaan yang perlu dijawab Gemini)
ASK = [
    ("spot", "harga terakhir saat ini"),
    ("prev_close", "penutupan hari sebelumnya (previous close)"),
    ("day_high / rolling_24h_high", "tertinggi hari ini (saham) atau 24 jam terakhir (kripto)"),
    ("day_low / rolling_24h_low", "terendah hari ini (saham) atau 24 jam terakhir (kripto)"),
]


def _rolling(candles: list[dict], bars: int) -> list[dict]:
    return candles[-bars:] if len(candles) >= bars else candles


def _compare(label: str, ours: float | None, theirs: float | None, tolerance_pct: float) -> tuple[str, str]:
    if theirs is None:
        return "SKIP", f"{label}: tidak ada angka Google Finance (tidak diukur, bukan nol)"
    if ours is None:
        return "FAIL", f"{label}: bar kita tidak menyediakan pembanding"
    deviation = abs(ours - theirs) / abs(theirs) * 100.0 if theirs else abs(ours - theirs) * 100.0
    if deviation <= tolerance_pct:
        return "PASS", f"{label}: ours={ours:.6g} gf={theirs:.6g} selisih {deviation:.3f}%"
    return "FAIL", f"{label}: ours={ours:.6g} gf={theirs:.6g} selisih {deviation:.3f}% > {tolerance_pct}%"


def _parse_time(value) -> float | None:
    """ISO-8601 atau epoch ( detik / ms ) menjadi epoch detik; None bila tidak terbaca."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e12 else number
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _within_asof_window(ours_epoch: float, gf_epoch: float | None, max_gap_hours: float) -> tuple[bool, str]:
    """Harga intraday hanya boleh dibandingkan bila kedua sisi diukur hampir bersamaan."""
    if gf_epoch is None:
        return False, "asof_utc Google Finance tidak diisi/tidak terbaca: harga intraday tidak bisa dibandingkan"
    gap_hours = abs(ours_epoch - gf_epoch) / 3600.0
    if gap_hours > max_gap_hours:
        return False, f"titik waktu berbeda {gap_hours:.1f} jam (> {max_gap_hours:g} jam): bandingkan pada saat yang sama"
    return True, f"titik waktu sejalan (selisih {gap_hours * 60:.0f} menit)"


def check_asset(entry: dict, tolerance_pct: float, interval: str, limit: int) -> list[tuple[str, str, str]]:
    asset = str(entry.get("asset") or "").upper()
    kind = str(entry.get("kind") or "crypto").lower()
    candles, source = fetch_candles(asset, interval=interval, limit=limit)
    if not candles:
        return [("FAIL", "bars", f"{asset}: tidak ada bar dari venue mana pun")]

    last = candles[-1]
    ours_spot = float(last["c"])
    if kind == "crypto":
        per_day = max(1, int(24 * 60 / {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}.get(interval, 60)))
        window = _rolling(candles, per_day)
        ours_high = max(float(c["h"]) for c in window)
        ours_low = min(float(c["l"]) for c in window)
        ours_prev = float(window[0]["c"])
        ours_high_label, ours_low_label = "rolling_24h_high", "rolling_24h_low"
    else:
        # Untuk saham, "hari ini" = tanggal UTC bar terakhir. Sesi bursa lokal bisa
        # ber beda zona, jadi hasil untuk saham bersifat perkiraan dan itu dilaporkan.
        by_day: dict = {}
        for candle in candles:
            day = datetime.fromtimestamp(candle["t"] / 1000, tz=timezone.utc).date()
            by_day.setdefault(day, []).append(candle)
        days = sorted(by_day)
        day_bars = by_day[days[-1]]
        ours_high = max(float(c["h"]) for c in day_bars)
        ours_low = min(float(c["l"]) for c in day_bars)
        ours_prev = float(by_day[days[-2]][-1]["c"]) if len(days) > 1 else None
        ours_high_label, ours_low_label = "day_high", "day_low"

    rows = []
    rows.append(_compare("prev_close", ours_prev, entry.get("prev_close"), tolerance_pct))

    gf_asof = _parse_time(entry.get("asof_utc"))
    ours_epoch = float(last["t"]) / 1000.0
    aligned, why = _within_asof_window(ours_epoch, gf_asof, float(entry.get("max_asof_gap_hours") or 2))
    for label, ours, theirs in (
        ("spot", ours_spot, entry.get("spot")),
        (ours_high_label, ours_high, entry.get(ours_high_label)),
        (ours_low_label, ours_low, entry.get(ours_low_label)),
    ):
        if not aligned:
            # Harga bergerak; membandingkan dua waktu berbeda menghasilkan kesimpulan palsu.
            rows.append(("SKIP", f"{label}: {why}"))
            continue
        rows.append(_compare(label, ours, theirs, tolerance_pct))
    if not aligned:
        rows.append(("INFO", f"bar terakhir {interval} kita pada {datetime.fromtimestamp(ours_epoch, tz=timezone.utc).isoformat()}"))
    if kind != "crypto":
        rows.append(("SKIP", "zona waktu: pembanding saham memakai tanggal UTC, bukan sesi bursa lokal"))
    return [(status, f"{asset}/{source}/{interval}", detail) for status, detail in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="Bandingkan jangkar harga Google Finance dengan bar venue (read-only).")
    parser.add_argument("payload", nargs="?", help="berkas JSON berisi angka Google Finance")
    parser.add_argument("--template", action="store_true", help="cetak format + daftar angka yang perlu diminta")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    if args.template or not args.payload:
        print(json.dumps(TEMPLATE, indent=2))
        print("\nAngka yang perlu diambil dari Google Finance (mis. lewat Gemini):")
        for field, question in ASK:
            print(f"  - {field:<28} : {question}")
        print("\nCatatan: Google Finance tidak menyediakan deret historis maupun skor Market Gravity.")
        print("Alat ini memeriksa KONSISTENSI HARGA sumber data, bukan paritas skor indikator.")
        return 0

    data = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    tolerance = float(data.get("tolerance_pct") or 0.5)
    tally = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for entry in data.get("assets") or []:
        for status, subject, detail in check_asset(entry, tolerance, args.interval, args.limit):
            tally[status] = tally.get(status, 0) + 1
            print(f"  {status:<5} {subject:<34} {detail}")

    print(f"\nringkasan: PASS={tally.get('PASS', 0)} FAIL={tally.get('FAIL', 0)} SKIP={tally.get('SKIP', 0)}")
    if tally.get("FAIL"):
        print("Harga venue dan Google Finance tidak sepakat: skor gravitasi tidak layak dipakai"
              " sebelum selisih ini dijelaskan.")
        return 1
    if not tally.get("PASS"):
        print("Tidak ada satu pun pembanding yang terisi: kesimpulan TIDAK bisa ditarik.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
