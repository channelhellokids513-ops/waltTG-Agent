"""Jalankan harness Apps Script (google_apps_script.js) dari suite pytest.

Alasannya: webhook adalah satu-satunya penulis ledger. Kalau rekonsiliasi header atau
baca-ulang tulis rusak, gejalanya baru terlihat berhari-hari kemudian sebagai baris
yang hilang/bergeser. Maka logikanya diuji di CI, bukan hanya saat seseorang iseng.

Harness ini membutuhkan node. Bila node tidak terpasang (mis. runner tanpa node),
test di-skip dengan alasan yang terbaca - bukan dianggap lulus.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tools" / "test_apps_script.js"
SCRIPT = ROOT / "google_apps_script.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node tidak tersedia di environment ini")
def test_apps_script_harness_passes():
    assert HARNESS.exists(), "harness apps script hilang"
    assert SCRIPT.exists(), "google_apps_script.js hilang"
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"harness gagal:\n{proc.stdout}\n{proc.stderr}"
    assert "GAGAL" not in proc.stdout
    assert "semua pemeriksaan apps script lulus" in proc.stdout


def test_webhook_version_is_stamped_in_the_script():
    """Versi wajib ada di sumber supaya `?probe=1` bisa membuktikan deployment aktif."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "WALT_WEBHOOK_VERSION" in text
    assert "params.probe" in text


def test_webhook_contract_matches_python_payload():
    """Kolom mg_* yang dikirim Python harus sama dengan yang dikenali modul gravitasi."""
    import re

    import sheet_logger
    from market_gravity import MarketGravityResult

    fields = MarketGravityResult(status="ok", notes=[]).to_scan_fields()
    assert set(fields) - set(sheet_logger.MG_COLUMNS) <= {"mg_status"}, (
        f"to_scan_fields menghasilkan kolom di luar MG_COLUMNS: {set(fields) - set(sheet_logger.MG_COLUMNS)}"
    )
    for name in sheet_logger.MG_COLUMNS:
        assert re.fullmatch(r"mg_[a-z0-9_]+", name), name
    assert sheet_logger.DAILY_SCANS_HEADERS[-len(sheet_logger.MG_COLUMNS):] == sheet_logger.MG_COLUMNS
