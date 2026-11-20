"""Probe sekali-baca jalur BACA RISET (webhook Apps Script `doGet` + fallback CSV lokal).

Sengaja TANPA OAuth Walt: deployment Apps Script harus bisa diverifikasi berulang tanpa
membakar refresh token single-use (yang akan membuat workflow lain gagal `invalid_grant`).

Keluar: status ternama + diagnosis + jumlah aset segar. Kode keluar 1 bila jalur bacanya
sendiri sakit (mis. balasan non-JSON), supaya kegagalan tidak bisa menyamar sebagai run hijau.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sheet_logger  # noqa: E402

# `ok` = webhook menjawab JSON sukses; `empty` = webhook sehat tapi risetnya basi (bukan outage).
HEALTHY_STATUSES = ("ok", "empty")


def main() -> int:
    researches = sheet_logger.fetch_latest_research_from_sheet()
    status = sheet_logger.get_research_read_status()
    diagnosis = sheet_logger.get_research_read_diagnosis()
    detail = sheet_logger.get_research_read_detail()

    nama = sorted(researches)
    tampil = ", ".join(nama[:8]) + (" ..." if len(nama) > 8 else "")
    print(f"research_read_status = {status}")
    print(f"aset_segar           = {len(nama)}" + (f" ({tampil})" if nama else ""))
    if diagnosis:
        print(f"diagnosis            = {diagnosis}")
    if detail:
        print(f"detail               = {detail[:240]}")

    healthy = status in HEALTHY_STATUSES
    print(
        "verdict              = "
        + ("SEHAT (jalur baca riset berfungsi)" if healthy else "SAKIT (perlu tindakan)")
    )
    if not healthy:
        # Saran disesuaikan sebab: redeploy tidak berguna kalau kuncinya memang belum dipasang.
        if status == "no_config":
            print(
                "tindakan             = pasang secret `GOOGLE_SHEET_WEBHOOK_URL` di lingkungan ini "
                "(URL /exec deployment Apps Script)."
            )
        print(
            "tindakan             = Apps Script -> Deploy -> Manage deployments -> Edit -> New version;\n"
            "                       pastikan Who has access = Anyone. Jika sheet ingin tetap privat,\n"
            "                       alihkan pembacaan Daily_Scans ke gviz (sudah terbukti `ok`)."
        )
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
