"""Gerbang sanitasi jadi permanen + ditegakkan CI + diuji balik (apakah ia benar-benar menangkap?).

Tanpa berkas ini, pemeriksaan rahasia hanya ada di tangan inspector yang ingat menjalankannya.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "sanitize_gate", REPO_ROOT / "tools" / "sanitize_gate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()

# Dirangkai dari potongan: berkas test ini sendiri tidak boleh memuat string yang cocok dengan
# detektornya (kalau boleh, gerbang akan merah pada repo yang sebenarnya bersih).
FAKE_API_KEY = "AIza" + "SyPlantedForTestOnly12345678"
FAKE_SHEET_ID = "1ag6Mq" + "PlantedForTestOnlyNotReal000000"


@pytest.fixture(autouse=True)
def _di_root(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    yield


def test_repo_terkini_lulus_gerbang_sanitasi():
    paths = gate._tracked_files()
    assert paths and len(paths) > 30, "cakupan git ls-files mencurigakan (terlalu sedikit berkas)"
    problems = gate.scan_files(paths, gate._forbidden_values())
    assert problems == [], f"ada nilai sensitif ter-commit: {problems}"


def test_gerbang_menguji_detektor_nya_sendiri():
    assert gate.self_test() == [], "detektor buta / allowlist salah: perbaiki sebelum melanjutkan"


def test_canary_memicu_hampir_semua_kelas_pola():
    found = {n for n, rx in gate.PATTERNS.items() if rx.findall(gate.SELF_TEST_CANARY)}
    assert len(found) >= gate.SELF_TEST_MIN_CLASSES, f"canary hanya memicu {sorted(found)}"


def test_gerbang_merah_bila_kredensial_ditanam_di_berkas_baru(tmp_path):
    """Uji balik: tanpa ini, 'repo bersih' bisa berarti 'gerbangnya tidak berfungsi'."""
    bocor = tmp_path / "bocor.py"
    bocor.write_text(f'API_KEY = "{FAKE_API_KEY}"\nSHEET = "{FAKE_SHEET_ID}"\n', encoding="utf-8")
    problems = gate.scan_files([str(bocor)], {})
    joined = " ".join(problems)
    assert len(problems) == 2, f"harus menangkap kedua penanaman: {problems}"
    assert "google_api_key" in joined and "sheet_id" in joined
    assert FAKE_API_KEY not in joined and FAKE_SHEET_ID not in joined, "laporan tidak boleh mengulang nilainya"


def test_gerbang_merah_bila_berisi_nilai_env_aktif(tmp_path):
    bocor = tmp_path / "salin.env.txt"
    bocor.write_text("token = " + FAKE_API_KEY + "\n", encoding="utf-8")
    problems = gate.scan_files([str(bocor)], {"DASHSCOPE_API_KEY": FAKE_API_KEY})
    assert any("DASHSCOPE_API_KEY" in p for p in problems)
    assert not any(FAKE_API_KEY in p for p in problems)


def test_placeholder_yang_sah_tidak_dihitung():
    assert gate._is_allowed("email", "petugas" + "@" + "example.com")
    assert gate._is_allowed("email", "peminjam" + "@" + "localhost")
    assert not gate._is_allowed("email", "pelanggan" + "@" + "perusahaan.co.id")
    assert gate._is_allowed(
        "apps_script_deployment", "script.google.com/macros/s/" + "your-deployment-id"
    )


def test_kelas_identitas_data_tidak_bisa_dikecualikan():
    """Pengecualian hanya untuk kelas contoh-angka; ID sheet / kunci tidak punya jalur keluar."""
    for keras in ("sheet_id", "google_api_key", "dashscope_key", "github_token", "telegram_bot",
                  "private_key", "session_token_param"):
        assert keras not in gate.CLASS_SKIP and keras not in gate.CLASS_SKIP_PREFIX
