"""contract_schema_probe - apakah payload kontrak mengekspos batas ATAS ukuran order?

Latar (produksi #44900424439): entry ANTHROPIC ditolak bursa dengan
`margin > contract.max_size_quote` padahal margin kita 2,50 USD pada leverage 2
(notional 5,00 USD). Artinya ada PLAFON ATAS yang tidak pernah kita periksa, sementara
`resolve_order_margin` hanya menegakkan batas BAWAH (`min_size_base`).

Untuk menulis gerbang pra-eksekusi yang simetris, satu fakta harus dibuktikan lebih
dulu: apakah field itu benar-benar ada di payload bursa. Kalau tidak, gerbang mana pun
yang kita tulis hanya menjadi kode mati.

Alat ini HANYA membaca (`list_contracts`) dan HANYA mencetak NAMA KUNCI - tidak pernah
mencetak nilai, harga, margin, order_id, atau token. Konsisten dengan kebijakan sensor
Ronde 6: bentuk respons boleh diamati, isinya tidak.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from walt_bot import WaltClient  # noqa: E402

SIZE_KEY_HINTS = ("size", "limit", "max", "min", "margin", "quote", "step")


def _as_contract_rows(payload: Any) -> list[dict]:
    """Terima bentuk apa pun (list, dict berisi list, dict-of-dict) jadi daftar dict."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            rows = _as_contract_rows(value)
            if rows:
                return rows
    return []


def main() -> int:
    client = WaltClient()
    try:
        payload = client.list_contracts()
    except Exception as exc:  # noqa: BLE001 - diagnostik tidak boleh menjatuhkan CI
        print(f"KONEKSI_KONTRAK_GAGAL: {type(exc).__name__}")
        return 1

    rows = _as_contract_rows(payload)
    if not rows:
        print(f"BENTUK_TIDAK_DIKENAL: {type(payload).__name__} (0 baris dict)")
        return 1

    all_keys: set[str] = set()
    for row in rows:
        all_keys.update(str(k) for k in row)
    sizeish = sorted(k for k in all_keys if any(h in k.lower() for h in SIZE_KEY_HINTS))

    print(f"KONTRAK_DIBACA        = {len(rows)}")
    print(f"KUNCI_UNIK            = {len(all_keys)}")
    print(f"KUNCI_BERKAIT_UKURAN  = {len(sizeish)}")
    for key in sizeish:
        present = sum(1 for row in rows if key in row)
        print(f"  - {key}  (ada di {present}/{len(rows)} baris)")

    has_upper = any("max_size" in k.lower() for k in all_keys)
    print("JAWABAN               = "
          + ("ADA field batas atas (max_size_*) -> gerbang pra-eksekusi BISA ditulis"
             if has_upper else
             "TIDAK ADA field batas atas -> max_size_quote hanya diketahui lewat error "
             "execute; gerbang pra-eksekusi TIDAK BISA ditulis dari metadata"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
