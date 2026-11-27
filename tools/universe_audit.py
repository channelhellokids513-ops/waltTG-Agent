"""universe_audit - audit read-only whitelist vs peluang funding terkuat.

Pertanyaan yang dijawab (evaluasi 27 Nov 2026):
  1. Apakah whitelist 31 aset memuat kontrak dengan funding terkuat?
  2. Kontrak likuid mana yang lolos ambang 0.50%/hari tetapi berada DI LUAR whitelist?
  3. Aset whitelist mana yang volumenya sudah turun di bawah floor $1 juta (drift likuiditas)?

Alat ini TIDAK menyentuh bursa dan TIDAK mengubah konfigurasi. Ia membaca snapshot JSON
hasil `list_contracts` (Walt) supaya bisa dijalankan lokal tanpa merotasi refresh token.

Mendapatkan snapshot (di CI, kredensial sudah tersinkron):
    python -c "import json,walt_bot;c=walt_bot.WaltClient();print(json.dumps(c.list_contracts()))" > logs/contracts.json
atau langsung tanpa snapshot (memakai kredensial Walt di lingkungan yang aman):
    python walt_bot.py universe_audit

Pemakaian:
    python tools/universe_audit.py logs/contracts.json
    python tools/universe_audit.py logs/contracts.json --top 15
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import walt_bot  # noqa: E402


def load_contracts(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        contracts = payload.get("contracts", [])
    else:
        contracts = payload
    if not isinstance(contracts, list):
        raise ValueError("Snapshot harus berupa list kontrak atau {'contracts': [...]}")
    return contracts


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whitelist vs funding terkuat (read-only).")
    parser.add_argument("snapshot", help="Path JSON hasil list_contracts")
    parser.add_argument("--top", type=int, default=10, help="Jumlah kontrak teratas yang ditampilkan")
    args = parser.parse_args()

    result = walt_bot.audit_universe(load_contracts(args.snapshot), top_n=args.top)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    print("\n-- Ringkasan --")
    print(result["verdict"])
    print(
        f"Kontrak diperiksa: {result['total_contracts']} | likuid: {result['liquid_contracts']} | "
        f"whitelist likuid: {result['whitelist_liquid_assets']}/{result['whitelist_size']} | "
        f"lolos funding di whitelist: {result['eligible_inside_whitelist']}"
    )
    below = result["whitelisted_below_volume_floor"]
    if below:
        print("Drift likuiditas (whitelist di bawah floor): " + ", ".join(
            f"{row['asset']}=${row['volume_24h_usd']:,.0f}" for row in below
        ))
    outside = result["top_outside_whitelist_passing_funding"]
    if outside:
        print("Peluang di luar whitelist: " + ", ".join(
            f"{row['asset']} {row['funding_daily_pct']:+.2f}%/hari" for row in outside
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())