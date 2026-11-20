"""
Migrasi sekali-jalan & pemeliharaan ledger lokal logs/closed_history.csv.

Default: --dry-run (tidak menyentuh file). Gunakan --apply secara eksplisit untuk menulis.
Backup bertimestamp UTC selalu dibuat sebelum file dimodifikasi.
DILARANG melakukan operasi destruktif terhadap Google Sheet — skrip ini hanya untuk file lokal.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import sheet_logger


def _is_header_row(row: list[str]) -> bool:
    if not row:
        return False
    c0 = str(row[0]).strip().lower()
    c1 = str(row[1]).strip().lower() if len(row) > 1 else ""
    return c0 == "timestamp (utc)" or (c0.startswith("timestamp") and c1 == "asset")


def migrate_closed_ledger(
    csv_path: str = "logs/closed_history.csv",
    apply: bool = False,
) -> dict[str, Any]:
    """
    Samakan bentuk semua baris di `csv_path` ke panjang header CLOSED_POSITIONS_HEADERS (15 kolom)
    dengan mem-pad baris yang lebih pendek memakai string kosong tanpa mengubah nilai lama.
    Idempoten: dijalankan dua kali tidak menghasilkan perubahan pada run kedua.
    """
    target_headers = list(sheet_logger.CLOSED_POSITIONS_HEADERS)
    target_len = len(target_headers)
    path = Path(csv_path)

    if not path.exists():
        return {
            "exists": False,
            "applied": False,
            "changed": False,
            "backup_path": None,
            "total_rows": 0,
            "padded_rows": 0,
            "header_updated": False,
        }

    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))

    if not rows:
        return {
            "exists": True,
            "applied": False,
            "changed": False,
            "backup_path": None,
            "total_rows": 0,
            "padded_rows": 0,
            "header_updated": False,
        }

    has_header = _is_header_row(rows[0])
    header_updated = (not has_header) or (rows[0] != target_headers)
    data_rows = rows[1:] if has_header else rows

    new_rows: list[list[str]] = [target_headers]
    padded_rows = 0
    for r in data_rows:
        if len(r) < target_len:
            new_rows.append(list(r) + [""] * (target_len - len(r)))
            padded_rows += 1
        else:
            new_rows.append(list(r))

    changed = header_updated or padded_rows > 0
    backup_path: str | None = None

    if apply and changed:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = path.parent / f"{path.name}.bak-{ts}"
        shutil.copy2(path, bak)
        backup_path = str(bak)
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(new_rows)

    return {
        "exists": True,
        "applied": bool(apply and changed),
        "changed": changed,
        "backup_path": backup_path,
        "total_rows": len(data_rows),
        "padded_rows": padded_rows,
        "header_updated": header_updated,
        "target_columns": target_len,
    }


def is_synthetic_fixture_row(row: list[str], col_map: dict[str, int]) -> bool:
    """
    Kriteria konservatif baris sintetis dari fixture test_risk.py:
      - Asset == 'NVDA'
      - Contract ID == '1'
      - Entry Price == 100.0
      - Margin == 2.5
      - Position ID == '1:100.0:' ATAU kosong '' (baris 10-kolom pra-D2 dari fixture yang sama)
    """
    def _c(name: str, default_idx: int) -> str:
        idx = col_map.get(name, default_idx)
        return str(row[idx]).strip() if 0 <= idx < len(row) else ""

    asset = _c("asset", 1).upper()
    cid = _c("contract id", 3)
    pid = _c("position id", 10)
    if asset != "NVDA" or cid != "1" or pid not in ("1:100.0:", ""):
        return False
    try:
        entry = float(_c("entry price ($)", 4))
        margin = float(_c("margin ($)", 6))
    except ValueError:
        return False
    return abs(entry - 100.0) < 1e-9 and abs(margin - 2.5) < 1e-9


def purge_synthetic_rows(
    csv_path: str = "logs/closed_history.csv",
    apply: bool = False,
) -> dict[str, Any]:
    """
    Hapus baris artefak fixture test dari ledger lokal `csv_path` secara konservatif.
    Default dry-run; --apply membuat backup .bak-<UTC> terlebih dahulu.
    """
    path = Path(csv_path)
    if not path.exists():
        return {
            "exists": False,
            "applied": False,
            "removed_count": 0,
            "kept_count": 0,
            "backup_path": None,
            "candidates_sample": [],
        }

    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))

    if not rows:
        return {
            "exists": True,
            "applied": False,
            "removed_count": 0,
            "kept_count": 0,
            "backup_path": None,
            "candidates_sample": [],
        }

    col_map, data_rows, _used_fb = sheet_logger.resolve_closed_positions_columns(rows)
    header_row = rows[0] if _is_header_row(rows[0]) else list(sheet_logger.CLOSED_POSITIONS_HEADERS)

    kept_rows: list[list[str]] = []
    removed_rows: list[list[str]] = []
    for r in data_rows:
        if _is_header_row(r):
            continue
        if is_synthetic_fixture_row(r, col_map):
            removed_rows.append(r)
        else:
            kept_rows.append(r)

    backup_path: str | None = None
    if apply and removed_rows:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = path.parent / f"{path.name}.bak-{ts}"
        shutil.copy2(path, bak)
        backup_path = str(bak)
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header_row)
            writer.writerows(kept_rows)

    sample = [
        {
            "asset": r[col_map.get("asset", 1)] if len(r) > 1 else "",
            "contract_id": r[col_map.get("contract id", 3)] if len(r) > 3 else "",
            "entry": r[col_map.get("entry price ($)", 4)] if len(r) > 4 else "",
            "reason": r[col_map.get("alasan close", 9)] if len(r) > 9 else "",
            "position_id": r[col_map.get("position id", 10)] if len(r) > 10 else "",
        }
        for r in removed_rows[:5]
    ]
    return {
        "exists": True,
        "applied": bool(apply and removed_rows),
        "removed_count": len(removed_rows),
        "kept_count": len(kept_rows),
        "backup_path": backup_path,
        "candidates_sample": sample,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrasi & pembersihan artefak fixture pada ledger lokal logs/closed_history.csv (dry-run default)."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="migrate",
        choices=("migrate", "purge-synthetic"),
        help="Subcommand: 'migrate' (default) atau 'purge-synthetic'.",
    )
    parser.add_argument(
        "--path",
        default="logs/closed_history.csv",
        help="Path file CSV lokal (default: logs/closed_history.csv)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Terapkan perubahan ke file lokal (membuat backup .bak-<UTC> terlebih dahulu).",
    )
    args = parser.parse_args(argv)
    mode = "APPLY" if args.apply else "DRY-RUN"

    if args.command == "purge-synthetic":
        res = purge_synthetic_rows(csv_path=args.path, apply=args.apply)
        print(
            f"[{mode} purge-synthetic] path={args.path} | removed={res['removed_count']} | "
            f"kept={res['kept_count']} | applied={res['applied']} | backup={res['backup_path']}"
        )
        if res["candidates_sample"]:
            print("Contoh kandidat (maks 5):")
            for item in res["candidates_sample"]:
                print(f"  - {item}")
        return 0

    res = migrate_closed_ledger(csv_path=args.path, apply=args.apply)
    print(
        f"[{mode} migrate] path={args.path} | total_data_rows={res['total_rows']} | "
        f"header_updated={res['header_updated']} | padded_rows={res['padded_rows']} | "
        f"changed={res['changed']} | backup={res['backup_path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
