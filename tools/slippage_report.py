"""slippage_report - ubah pengukuran produksi menjadi angka yang bisa dipakai.

sumber data: logs/slippage_observed.jsonl, ditulis WaltClient setiap execute nyata
(kuotasi saat preview vs harga fill yang dilaporkan bursa).

Ini penutup loop "perbaikan berkelanjutan": tanpa alat ini, file itu hanya menumpuk.
Yang dilaporkan:
  - sebaran slippage per tahap (open/close) dalam bps, dengan tanda yang benar
  - umur kuotasi (kalau besar, selisihnya bukan slippage murni - kita laporkan, bukan sembunyikan)
  - biaya round-trip tersirat = 2 x taker + p90(open) + p90(close), vs dasar 1,18% yang
    dipakai E5 hari ini
  - apa yang MASIH belum terukur - dan karena itu apa yang belum boleh diubah

Read-only. Tidak menulis apa pun.

Pemakaian:
    python tools/slippage_report.py
    python tools/slippage_report.py --path logs/slippage_observed.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from walt_bot import TAKER_FEE_PER_SIDE  # noqa: E402

BASIS_E5_HARI_INI = 0.0118  # 2 x 0,09% + 1,00% slippage venue seragam


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(record, dict) and "slippage_pct" in record:
            records.append(record)
        else:
            bad += 1
    if bad:
        print(f"Catatan: {bad} baris tidak terbaca dan DIABAIKAN (tidak dihitung sebagai nol).")
    return records


def describe(rows: list[dict]) -> dict:
    values = [row["slippage_pct"] for row in rows]
    ages = [float(row.get("quote_age_s") or 0.0) for row in rows]
    ordered = sorted(abs(v) for v in values)
    index90 = min(len(ordered) - 1, max(0, int(round(0.90 * (len(ordered) - 1)))))
    return {
        "n": len(values),
        "median_bps": statistics.median(values) * 100.0,
        "mean_abs_bps": statistics.fmean(ordered) * 100.0,
        "p90_abs_bps": ordered[index90] * 100.0,
        "max_abs_bps": ordered[-1] * 100.0,
        "median_age_s": statistics.median(ages),
        "assets": sorted({str(row.get("contract_id")) for row in rows}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ringkas pengukuran slippage nyata (read-only).")
    parser.add_argument("--path", default=str(ROOT / "logs" / "slippage_observed.jsonl"))
    args = parser.parse_args()

    records = load(Path(args.path))
    print(f"slippage_report - {len(records)} pengukuran dari {args.path}")
    print("=" * 84)
    if not records:
        print("BELUM ADA PENGUKURAN NYATA. Artinya: belum ada order yang dieksekusi lewat")
        print("jalur produksi sejak instrumentasi dipasang (E5 memblokir semuanya, atau belum")
        print("ada penutupan). Jangan ganti dasar biaya apa pun sebelum file ini berisi.")
        return 1

    fee_pct = 2 * TAKER_FEE_PER_SIDE * 100.0
    print(f"Fee round-trip (field bursa, terkonfirmasi): {fee_pct:.3f}%")
    print(f"Dasar biaya E5 hari ini: {BASIS_E5_HARI_INI * 100:.2f}% "
          f"(= fee + 1,00% slippage seragam yang bursa nyatakan)")
    print()
    print(f"{'tahap':<8}{'n':>5}{'median bps':>12}{'rata| |bps':>12}{'p90| |bps':>11}"
          f"{'maks| |bps':>12}{'umur kuotasi':>14}")
    for stage in ("open", "close"):
        rows = [r for r in records if r.get("stage") == stage]
        if not rows:
            print(f"{stage:<8}{0:5}   BELUM TERUKUR - tidak ada satu pun {stage} nyata terinstrumentasi")
            continue
        stats = describe(rows)
        print(f"{stage:<8}{stats['n']:5}{stats['median_bps']:+12.2f}{stats['mean_abs_bps']:12.2f}"
              f"{stats['p90_abs_bps']:11.2f}{stats['max_abs_bps']:12.2f}"
              f"{stats['median_age_s']:12.1f}s")

    open_rows = [r for r in records if r.get("stage") == "open"]
    close_rows = [r for r in records if r.get("stage") == "close"]
    print()
    if open_rows and close_rows:
        implied = (fee_pct + describe(open_rows)["p90_abs_bps"] / 100.0
                   + describe(close_rows)["p90_abs_bps"] / 100.0)
        ratio = (BASIS_E5_HARI_INI * 100.0) / implied if implied > 0 else 0.0
        print(f"Biaya round-trip TERSIRAT (fee + p90 kedua sisi) = {implied:.3f}%")
        print(f"Dasar E5 hari ini {BASIS_E5_HARI_INI * 100:.2f}% = {ratio:.1f}x lebih besar dari tersirat.")
        print("Bila Anda ingin E5 memakai angka terukur, itu keputusan pemilik - alat ini")
        print("hanya menyediakannya, tidak mengubah apa pun.")
    else:
        kurang = "close" if not close_rows else "open"
        print(f"Belum lengkap: sisi {kurang} belum terukur, sehingga dasar biaya E5 BELUM BOLEH DIUBAH.")
        print("Angka parsial akan menggeser gerbang trading tanpa bukti dua sisi - itu justru")
        print("kelas kesalahan yang sedang kita berantas.")
    print("\nRead-only: tidak ada yang ditulis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
