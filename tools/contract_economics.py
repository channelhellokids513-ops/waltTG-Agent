"""contract_economics - ukur BIAYA ROUND-TRIP NYATA, ganti angka anggapan.

Masalah yang ditutupi. Semua kesimpulan ekonomi di repo ini (rr_scan, gravity_strategy,
gravity_walkforward) berdiri pada dua angka yang belum pernah diukur:
  - fee saja 0,18% (2 x TAKER_FEE_PER_SIDE 0,09%), dan
  - "slippage venue" 1,00% -> total 1,18%.
Yang kedua selama ini hanya ANGKA YANG DIANGGEP SERAGAM untuk semua aset, padahal
`perps_list_contracts` mengembalikan `price_slippage` PER ASET. Alat ini menggantinya
dengan tiga pengukuran yang saling menguji:

  A. apa yang bursa NYATAKAN per aset (price_slippage, funding_rate_hourly,
     min_size_base, max_leverage) dan konsekuensinya untuk ambang E5;
  B. carry yang NYATA kita bayar: `funding_paid_quote` vs `margin_quote` dan durasi
     posisi pada riwayat tertutup (menguji asumsi 0,52%/hari);
  C. devisi entry NYATA: `avg_entry_price` posisi tertutup dibandingkan harga keputusan
     kita di Daily_Scans (gviz, tanpa token) - ini yang membuktikan slippage venue
     terlalu besar atau terlalu kecil untuk clip kita.

KESELAMATAN (ditegakkan di kode, dites di tests/test_contract_economics.py):
  - hanya tool MCP read-only yang boleh dipanggil; daftar hitam mencakup
    `perps_execute_operation` (mengisi order) DAN `perps_prepare_order_place`
    (langkah tepat sebelum mengisi) - pemanggilannya mengangkat RuntimeError;
  - tidak ada satu pun tulis: tidak ledger, tidak sheet, tidak order;
  - data yang tidak terukur dilaporkan sebagai TIDAK TERUKUR, tidak pernah diisi
    angka netral atau nol.

Pemakaian (di GitHub Actions, konsumen OAuth yang sah; lihat diagnostics.yml):
    python tools/contract_economics.py
Lokal butuh WALT_CLIENT_ID + WALT_REFRESH_TOKEN yang masih hidup; refresh token bersifat
single-use, jadi jalankan dari CI, bukan dari laptop.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sheet_logger  # noqa: E402
from walt_bot import EXPECTED_HOLD_HOURS, TAKER_FEE_PER_SIDE  # noqa: E402

READ_ONLY_TOOLS = frozenset({
    "perps_list_contracts", "perps_latest_rate", "perps_list_positions",
    "perps_readiness", "balance_get",
})
FORBIDDEN_TOOLS = frozenset({
    "perps_execute_operation", "perps_prepare_order_place",
    "perps_prepare_position_close", "perps_prepare_auto_close_set",
})

UNIVERSE_HINT = {"BTC", "ETH", "SOL", "BNB", "XRP", "AVAX", "SUI", "LINK", "NEAR", "AAVE",
                 "ARB", "OP", "DOGE", "ADA", "DOT", "ATOM", "LTC", "UNI", "APT", "SEI"}
MATCH_WINDOW_H = 36.0


class ReadOnlyViolation(RuntimeError):
    """Percobaan memanggil tool MCP yang bisa mengubah keadaan."""


def guard_client(client: Any) -> Any:
    """Bungkus WaltClient._call supaya hanya tool read-only yang bisa lewat.

    Penegakan di lapisan pemanggil berarti alat ini tetap aman walaupun seseorang
    menambahkan panggilan baru di bawah.
    """
    original = client._call

    def guarded(tool: str, args: dict | None = None, _retry: bool = False):
        if tool in FORBIDDEN_TOOLS:
            raise ReadOnlyViolation(f"{tool} DILARANG di alat read-only (menulis keadaan bursa)")
        if tool not in READ_ONLY_TOOLS:
            raise ReadOnlyViolation(f"{tool} tidak termasuk daftar baca-saja {sorted(READ_ONLY_TOOLS)}")
        return original(tool, args, _retry)

    client._call = guarded
    return client


def to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def to_time(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    for attempt in (lambda text: datetime.fromisoformat(text.replace("Z", "+00:00")),):
        try:
            stamp = attempt(raw)
            return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def positions_of(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        items = payload.get("positions") or payload.get("data") or []
    else:
        items = payload or []
    return [item for item in items if isinstance(item, dict)]


def quantiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    def at(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]
    return {"n": len(ordered), "median": statistics.median(ordered), "p10": at(0.10),
            "p90": at(0.90), "min": ordered[0], "max": ordered[-1]}


def print_cost(venue_slip: Optional[float], funding_daily: Optional[float], hold_h: float) -> str:
    """Biaya dan ambang E5 menurut gerbang engine, memakai slippage per aset bila ada."""
    fee = 2.0 * TAKER_FEE_PER_SIDE
    if venue_slip is None:
        return f"fee={fee * 100:.2f}% slippage=TIDAK DILAPORKAN"
    cost = fee + max(0.0, venue_slip)
    required_daily = cost * 1.5 / (hold_h / 24.0) if hold_h else float("inf")
    verdict = "LOLOS" if funding_daily is not None and abs(funding_daily) >= required_daily else "SKIP"
    return (f"slip={venue_slip * 100:.3f}% cost={cost * 100:.3f}% "
            f"req={required_daily * 100:.3f}%/hari funding={'' if funding_daily is None else f'{funding_daily * 100:.3f}%/hari'} {verdict}")


def section_a(client: Any, hold_h: float) -> dict:
    print("A. apa yang bursa nyatakan PER ASET (bukan angka seragam yang kita anggap)")
    payload = client.list_contracts()
    if isinstance(payload, dict):
        contracts = payload.get("contracts") or payload.get("data") or []
    else:
        contracts = payload or []
    contracts = [item for item in contracts if isinstance(item, dict)]
    rows, slips = [], []
    for contract in contracts:
        asset = str(contract.get("base_asset") or contract.get("name") or "").upper()
        slip = to_float(contract.get("price_slippage"))
        funding_daily_raw = to_float(contract.get("funding_rate_hourly"))
        funding_daily = funding_daily_raw * 24.0 if funding_daily_raw is not None else None
        if not asset:
            continue
        if slip is not None:
            slips.append(slip)
        rows.append({"asset": asset, "slip": slip, "funding_daily": funding_daily,
                     "min_size_base": to_float(contract.get("min_size_base")),
                     "max_leverage": to_float(contract.get("max_leverage")),
                     "volume_24h_usd": to_float(contract.get("volume_24h_usd"))})
    rows.sort(key=lambda row: (row["slip"] is None, -(row["slip"] or 0.0)))
    show = [row for row in rows if row["asset"] in UNIVERSE_HINT] or rows
    print(f"   {'aset':<7}{'slippage bursa':>16}   evaluasi E5 pada hold {hold_h:.0f} bar")
    for row in show[:24]:
        print(f"   {row['asset']:<7}{('TIDAK DILAPORKAN' if row['slip'] is None else f'{row["slip"] * 100:.3f}%'):>16}   "
              + print_cost(row["slip"], row["funding_daily"], hold_h))
    stats = quantiles(slips)
    if stats:
        print(f"   sebaran slippage {len(slips)} aset: median={stats['median'] * 100:.3f}% "
              f"p10={stats['p10'] * 100:.3f}% p90={stats['p90'] * 100:.3f}% "
              f"min={stats['min'] * 100:.3f}% max={stats['max'] * 100:.3f}%")
        if stats["p10"] == stats["p90"]:
            print("   PERINGATAN: p10 == p90 - nilai ini SERAGAM di hampir semua aset, yang adalah")
            print("   tanda konstanta DEFAULT bursa, bukan pengukuran per aset. Angka 1,18% kita")
            print("   cocok dengan itu, tetapi kecocokan dengan konstanta orang lain BUKAN bukti.")
            stats["uniform"] = True
    print("   arti: angka 1,18% yang dipakai semua simulasi adalah slippage TERBESAR yang")
    print("   pernah terlihat di log, diterapkan seragam. Bila median sebenarnya jauh lebih")
    print("   kecil, simulasi kita terlalu pesimis; bila lebih besar, terlalu optimis.")
    return {"rows": rows, "stats": stats}


def section_b(client: Any) -> dict:
    print("\nB. carry yang NYATA kita bayar (riwayat tertutup, tanpa asumsi)")
    payload = client.list_positions(statuses=["closed"])
    closed = positions_of(payload)
    rows, per_day_margin, per_day_notional = [], [], []
    missing = 0
    for item in closed:
        funding = to_float(item.get("funding_paid_quote"))
        margin = to_float(item.get("margin_quote"))
        opened, updated = to_time(item.get("created_at")), to_time(item.get("updated_at"))
        leverage = to_float(item.get("leverage")) or 1.0
        if funding is None or margin is None or margin <= 0 or opened is None or updated is None:
            missing += 1
            continue
        days = (updated - opened).total_seconds() / 86400.0
        if days <= 0:
            continue
        per_day_margin.append(abs(funding) / margin / days * 100.0)
        per_day_notional.append(abs(funding) / (margin * leverage) / days * 100.0)
        rows.append((str(item.get("contract_base_asset") or "").upper(), funding, margin, days))
    print(f"   posisi tertutup terbaca={len(closed)} terpakai={len(rows)} tidak lengkap={missing}")
    if not rows:
        print("   TIDAK TERUKUR: tidak ada posisi tertutup dengan funding + durasi yang terbaca.")
        return {"measured": False}
    stats_margin = quantiles(per_day_margin)
    stats_notional = quantiles(per_day_notional)
    net = sum(funding for _a, funding, _m, _d in rows)
    print(f"   funding dibayar bersih pada sampel ini = {net:+.4f} USD "
          f"({sum(1 for _a, f, _m, _d in rows if f > 0)} posisi membayar, "
          f"{sum(1 for _a, f, _m, _d in rows if f < 0)} menerima)")
    if stats_notional:
        print(f"   carry per hari dari NOTIONAL: median={stats_notional['median']:.4f}% "
              f"p10={stats_notional['p10']:.4f}% p90={stats_notional['p90']:.4f}% "
              f"maks={stats_notional['max']:.4f}%")
        print(f"   carry per hari dari MARGIN  : median={stats_margin['median']:.4f}% "
              f"p90={stats_margin['p90']:.4f}%")
        assume = 0.52
        print(f"   pembanding: asumsi yang dipakai rr_scan/gravity = {assume:.2f}%/hari "
              f"-> median nyata {stats_notional['median']:.4f}%/hari, "
              f"rasio {stats_notional['median'] / assume:.2f}x")
    print("   arti: bila angka nyata jauh di bawah asumsi, biaya carry yang kita kenakan ke")
    print("   setiap simulasi terlalu besar (dan sebaliknya).")
    return {"measured": True, "rows": rows, "per_day_notional": stats_notional}


def section_c(client: Any) -> dict:
    print("\nC. devisi entry NYATA: harga fill vs harga keputusan kita (Daily_Scans, tanpa token)")
    rows, status = sheet_logger.fetch_sheet_csv_rows_ex("Daily_Scans")
    print(f"   baca Daily_Scans: status={status} baris={len(rows)}")
    if status not in ("ok", "local_csv") or len(rows) < 2:
        print("   TIDAK TERUKUR: sheet keputusan tidak terbaca - tidak ada angka yang dibuat-buat.")
        return {"measured": False, "sheet_status": status}
    header = [str(cell).strip().lower() for cell in rows[0]]

    def find_column(candidates: tuple[str, ...], contains: tuple[str, ...]) -> Optional[int]:
        """Cocokkan kolom dengan nama persis ATAU kata kunci - sheet ini punya header
        terlokalisasi ('timestamp (utc)', 'live price ($)'), jadi nama kanonis dari
        payload tidak selalu ada. Tidak ketemu -> None, bukan kolom 0."""
        for name in candidates:
            if name in header:
                return header.index(name)
        for index, cell in enumerate(header):
            if any(token in cell for token in contains):
                return index
        return None

    idx_time = find_column(("timestamp_utc", "timestamp (utc)", "waktu (utc)"), ("timestamp", "waktu"))
    idx_asset = find_column(("asset", "aset"), ("asset", "aset"))
    idx_price = find_column(("price", "live price ($)", "harga"), ("price", "harga"))
    missing = [label for label, index in (("waktu", idx_time), ("aset", idx_asset), ("harga", idx_price))
               if index is None]
    if missing:
        print(f"   TIDAK TERUKUR: kolom {', '.join(missing)} tidak ada di header: {header[:12]}")
        return {"measured": False, "sheet_status": status, "header": header[:12]}
    scans: dict[str, list[tuple[datetime, float]]] = {}
    for row in rows[1:]:
        if len(row) <= max(idx_time, idx_asset, idx_price):
            continue
        stamp, asset, price = to_time(row[idx_time]), str(row[idx_asset]).upper(), to_float(row[idx_price])
        if stamp is None or price is None or price <= 0 or not asset:
            continue
        scans.setdefault(asset, []).append((stamp, price))
    closed = positions_of(client.list_positions(statuses=["closed"]))
    deviations: list[float] = []
    signed: list[float] = []
    lags_hours: list[float] = []
    unmatched = 0
    for item in closed:
        asset = str(item.get("contract_base_asset") or "").upper()
        entry = to_float(item.get("avg_entry_price"))
        opened = to_time(item.get("created_at"))
        if not asset or entry is None or entry <= 0 or opened is None:
            unmatched += 1
            continue
        candidates = [(stamp, price) for stamp, price in scans.get(asset, [])
                      if stamp <= opened and (opened - stamp).total_seconds() / 3600.0 <= MATCH_WINDOW_H]
        if not candidates:
            unmatched += 1
            continue
        stamp, decision = max(candidates, key=lambda pair: pair[0])
        deviation = (entry - decision) / decision
        deviations.append(abs(deviation) * 100.0)
        signed.append(deviation * 100.0)
        lags_hours.append((opened - stamp).total_seconds() / 3600.0)
    print(f"   pasangan terukur={len(deviations)} tanpa padanan={unmatched} "
          f"(jendela cocok +/-{MATCH_WINDOW_H:.0f} jam sebelum fill)")
    if not deviations:
        print("   TIDAK TERUKUR: tidak ada posisi tertutup yang bisa dipasangkan ke baris scan.")
        return {"measured": False, "sheet_status": status}
    stats = quantiles(deviations)
    lag = quantiles(lags_hours)
    print(f"   |deviasi fill vs keputusan| : median={stats['median']:.3f}% p90={stats['p90']:.3f}% "
          f"maks={stats['max']:.3f}%  (berarti signed median={statistics.median(signed):+.3f}%)")
    print(f"   jeda waktu scan->fill      : median={lag['median']:.2f} jam p90={lag['p90']:.2f} jam "
          f"maks={lag['max']:.2f} jam")
    print("   PENTING: deviasi ini adalah SELISIH HARGA antara keputusan dan fill, jadi ia")
    print("   BATAS ATAS biaya eksekusi - sebagian bisa berupa pergerakan pasar selama jeda")
    print("   tersebut, bukan spread/impact. Semakin kecil jedanya, semakin angka ini")
    print("   mendekati slippage murni.")
    venue = 1.00
    print(f"   pembanding: slippage venue yang kita anggap seragam = {venue:.2f}%")
    if lag["median"] > 1.0:
        print("   jeda median > 1 jam -> angka ini TIDAK boleh dipakai sebagai slippage;")
        print("   ukur slippage murni dengan membandingkan fill vs order book saat eksekusi.")
    elif stats["median"] < venue / 3.0:
        print("   kesimpulan sementara: asumsi 1,00% TERLALU PESIMIS untuk clip kita.")
    elif stats["median"] > venue:
        print("   kesimpulan sementara: asumsi 1,00% masih LEBIH KECIL dari kenyataan.")
    else:
        print("   kesimpulan sementara: asumsi 1,00% ada dalam orde yang benar.")
    return {"measured": True, "stats": stats, "lag_hours": lag}


def main() -> int:
    parser = argparse.ArgumentParser(description="Ukur biaya round-trip nyata (read-only, tidak pernah menulis apa pun).")
    parser.add_argument("--hold-hours", type=float, default=EXPECTED_HOLD_HOURS)
    args = parser.parse_args()

    try:
        from walt_bot import WaltClient
    except Exception as exc:  # pragma: no cover - lingkungan impor
        print(f"WaltClient tidak bisa diimpor: {exc}")
        return 1
    client = WaltClient()
    try:
        client = guard_client(client)
        print("contract_economics - pengukuran biaya read-only "
              f"(fee round-trip = {2 * TAKER_FEE_PER_SIDE * 100:.2f}%, hold {args.hold_hours:.0f} jam)")
        print("=" * 96)
        summary_a = section_a(client, args.hold_hours)
        summary_b = section_b(client)
        summary_c = section_c(client)
        print("=" * 96)
        measured = [name for name, item in (("A", summary_a), ("B", summary_b), ("C", summary_c))
                    if item.get("stats") or item.get("measured")]
        print(f"Terukur: {', '.join(measured) if measured else 'TIDAK ADA - jangan ganti asumsi apa pun.'}")
        if "C" in measured:
            print("Langkah berikutnya: substitusikan slippage TERUKUR ke simulasi "
                  "(tools/gravity_strategy.py dan rr_scan) sebelum menyentuh parameter live.")
        else:
            print("Langkah berikutnya: belum ada hak mengubah angka biaya di simulasi.")
        print("Tidak ada yang ditulis: tidak ledger, tidak sheet, tidak order.")
        return 0
    except ReadOnlyViolation as exc:
        print(f"DITOLAK (pelanggaran read-only): {exc}")
        return 2
    except RuntimeError as exc:
        print(f"Gagal: {exc}")
        print("Bila ini OAuth: refresh token single-use harus dirotasi oleh CI, bukan lokal. "
              "Jalankan diagnostics.yml dengan oauth_step=contract_economics.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
