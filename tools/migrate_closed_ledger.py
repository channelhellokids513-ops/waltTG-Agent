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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrasi bentuk kolom logs/closed_history.csv ke header 15 kolom (dry-run default)."
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
    res = migrate_closed_ledger(csv_path=args.path, apply=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] path={args.path} | total_data_rows={res['total_rows']} | "
        f"header_updated={res['header_updated']} | padded_rows={res['padded_rows']} | "
        f"changed={res['changed']} | backup={res['backup_path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
