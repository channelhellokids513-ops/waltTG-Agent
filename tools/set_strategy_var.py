"""Perbarui variable GitHub `STRATEGY_CONFIG_JSON` dan BUKTIKAN terbaca balik sebagai JSON valid.

Kenapa skrip, bukan `gh variable set --body '{...}'` di PowerShell: PowerShell mengunyah tanda kutip
ganda di dalam argumen, jadi JSON ter-panggil berubah jadi string rusak dan TERPANGGIL tersimpan
rusak (terjadi nyata 17:10Z). Kirim lewat stdin = tidak ada opportunity kutip ulang.

Nilai yang ditulis berasal dari DEFAULT_STRATEGY_CONFIG (kode) supaya test yang sama menjaga
keduanya tetap sinkron, bukan angka yang diketik manual di terminal.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy_config import DEFAULT_STRATEGY_CONFIG  # noqa: E402


def main() -> int:
    cfg = dict(DEFAULT_STRATEGY_CONFIG)
    body = json.dumps(cfg, sort_keys=True)

    tulis = subprocess.run(
        ["gh", "variable", "set", "STRATEGY_CONFIG_JSON"],
        input=body.encode("utf-8"), capture_output=True,
    )
    if tulis.returncode != 0:
        print("GAGAL menyetel variable:", tulis.stderr.decode("utf-8", "ignore").strip())
        return 1

    baca = subprocess.run(
        ["gh", "variable", "get", "STRATEGY_CONFIG_JSON"], capture_output=True,
    )
    tersimpan = baca.stdout.decode("utf-8").strip()
    if not tersimpan:
        print("GAGAL membaca balik variable:", baca.stderr.decode("utf-8", "ignore").strip())
        return 1
    try:
        hasil = json.loads(tersimpan)
    except json.JSONDecodeError as e:
        # Inilah kegagalan yang mau dicegah: tersimpan, tapi tidak bisa diurai saat runtime.
        print(f"RUSAK: variable tersimpan tapi BUKAN JSON valid ({e})\nisi = {tersimpan[:200]}")
        return 1

    sama = hasil == json.loads(body)
    print(f"kunci             = {len(hasil)}")
    print(f"max_research_age  = {hasil.get('max_research_age_hours')}")
    print(f"max_slots_cap     = {hasil.get('max_slots_cap')}")
    print(f"sama_dengan_kode  = {sama}")
    if not sama:
        print("PERINGATAN: variable produksi berbeda dari DEFAULT_STRATEGY_CONFIG -> cek kunci:")
        for k in sorted(set(hasil) ^ set(json.loads(body))):
            print(f"  - {k}: var={hasil.get(k)} kode={json.loads(body).get(k)}")
    return 0 if sama else 1


if __name__ == "__main__":
    raise SystemExit(main())
