"""Sensus IDENTITAS baris ledger `Closed_Positions` (read-only, tanpa kredensial Walt).

Kenapa alat ini ada: `reconcile_history` mende-dupusi riwayat bursa dengan `Position ID`. Sensus
2026-11-20 17:54Z membuktikan sheet menyimpan 17 baris tapi HANYA 6 yang punya Position ID —
sisanya baris warisan dari sebelum kolom itu diisi. Dedupe berbasis id saja therefore buta terhadap
11 baris itu, dan mirror backfill bisa menulis trade yang sama dua kali ke ledger yang dipakai
mengukur performansi.

Tidak mencetak PnL/margin/saldo: yang perlu dilihat di sini adalah KEY, bukan angka hasil.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sheet_logger  # noqa: E402

COL_ID = "position id"
COL_ASSET = "asset"
COL_SIDE = "side"
COL_ENTRY = "entry price ($)"
COL_OPENED = "opened at (exchange)"


def composite_key(asset: Any, side: Any, entry: Any, opened: Any) -> Optional[str]:
    """Kunci cadangan untuk baris tanpa Position ID: (aset, sisi, harga masuk, menit buka).

    Waktu dinormalisasi lewat `parse_sheet_timestamp` lalu dipotong ke MENIT, karena Sheets
    menulis ulang ISO ke bentuknya sendiri (terbukti 16:51Z). Tanpa normalisasi, kunci tidak akan
    pernah cocok dan baris lama tetap dianggap 'baru'.
    """
    a = str(asset or "").strip().upper()
    s = str(side or "").strip().upper()
    dt = sheet_logger.parse_sheet_timestamp(opened)
    try:
        e = round(float(str(entry).replace(",", "")), 4)
    except (TypeError, ValueError):
        e = None
    if not a or e is None or dt is None:
        return None
    return f"{a}|{s}|{e:.4f}|{dt.strftime('%Y-%m-%dT%H:%M')}"


def coarse_key(asset: Any, side: Any, entry: Any) -> Optional[str]:
    """Identitas tertangguh untuk baris warisan TANPA Position ID dan TANPA waktu bursa.

    (aset, sisi, harga masuk 4 desimal). Sengaja TIDAK memakai waktu: 11 baris warisan
    (tersensus 17:57Z) kolom `Opened At (exchange)`-nya kosong, jadi kunci berbasis waktu tidak
    akan pernah cocok. Konservatif: kalau ada kandidat baru yang kebetulan punya kunci sama,
    ia DIANGGAP sudah tercatat — tidak menulis lebih baik daripada menulis duplikat.
    """
    a = str(asset or "").strip().upper()
    s = str(side or "").strip().upper()
    try:
        e = round(float(str(entry).replace(",", "")), 4)
    except (TypeError, ValueError):
        return None
    if not a or e is None:
        return None
    return f"{a}|{s}|{e:.4f}"


def census(rows: list[list[str]]) -> dict[str, Any]:
    col_map, data, used_fallback = sheet_logger.resolve_closed_positions_columns(rows)
    id_idx = col_map.get(COL_ID)
    with_id: list[str] = []
    tanpa_id: list[dict[str, Any]] = []
    for r in data:
        pid = str(r[id_idx]).strip() if id_idx is not None and len(r) > id_idx else ""
        if pid:
            with_id.append(pid)
            continue
        get = lambda k: (r[col_map[k]] if k in col_map and len(r) > col_map[k] else "")
        asset, side, entry, opened = (
            get(COL_ASSET), get(COL_SIDE), get(COL_ENTRY), get(COL_OPENED)
        )
        tanpa_id.append({
            "asset": str(asset).upper(),
            "side": str(side).upper(),
            "entry": str(entry),
            "opened": str(opened),
            "keys": sheet_logger.close_identity_keys(asset, side, entry),
            "key_waktu": composite_key(asset, side, entry, opened),
        })
    kasar = {k for t in tanpa_id for k in t["keys"]}
    return {
        "baris_total": len(data),
        "punya_position_id": len(with_id),
        "tanpa_position_id": len(tanpa_id),
        "terkunci_waktu": sum(1 for t in tanpa_id if t["key_waktu"]),
        "kunci_identitas_unik": len(kasar),
        "tanpa_sisi": sum(1 for t in tanpa_id if not t["side"]),
        "tak_ternavigasi": sum(1 for t in tanpa_id if not t["keys"]),
        "fallback_indeks_header": used_fallback,
        "contoh_tanpa_id": tanpa_id[:6],
    }


def _muat_env_lokal() -> None:
    """Isi GOOGLE_SHEET_ID dari .env bila belum ada di environment (tanpa dependensi baru).

    Sengaja tidak memakai walt_bot.load_local_env: alat sensus harus tetap bisa jalan walau modul
    utama bermasalah, dan ia hanya butuh SATU kunci non-rahasia.
    """
    if os.environ.get("GOOGLE_SHEET_ID"):
        return
    try:
        with open(".env", encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                key, sep, val = raw.partition("=")
                if not sep or key.strip().startswith("#"):
                    continue
                if key.strip() == "GOOGLE_SHEET_ID":
                    os.environ["GOOGLE_SHEET_ID"] = val.strip().strip('"').strip("'")
                    return
    except OSError:
        pass


def main() -> int:
    _muat_env_lokal()
    rows, status = sheet_logger.fetch_sheet_csv_rows_ex(
        "Closed_Positions", expected_col_name="Asset"
    )
    print(f"sumber             = gviz Closed_Positions (status={status})")
    if status not in ("ok", "local_csv") or not rows:
        print("verdict            = TIDAK TERBACA — sensus tidak bisa disimpulkan dari sheet kosong/gagal")
        return 1
    hasil = census(rows)
    for k, v in hasil.items():
        if k == "contoh_tanpa_id":
            print(f"{k:<19} = {len(v)} contoh (aset/sisi/harga/waktu saja, tanpa PnL):")
            for c in v:
                print(f"  - {c['asset']} sisi={c['side'] or '-'} entry={c['entry'] or '-'} "
                      f"opened={c['opened'] or '-'} kunci={len(c['keys'])}")
        else:
            print(f"{k:<19} = {v}")
    buta = hasil["tanpa_position_id"]
    print(
        "verdict            = "
        + (
            "AMAN — setiap baris punya Position ID"
            if buta == 0
            else f"BERISIKO — {buta} baris tak terdedupe-by-id; {hasil['kunci_identitas_unik']} kunci "
                 f"identitas tersedia sebagai penahan duplikat, {hasil['tak_ternavigasi']} tak terbaca"
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
