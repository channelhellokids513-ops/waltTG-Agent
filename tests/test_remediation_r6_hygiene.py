"""Ronde 6 — Item 5 [R6-5]: sisa kebersihan (jalur state terpantau, notifier aman-encoding, purge id)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import conftest  # tests/conftest.py: memuat daftar state yang dipantau guard sesi

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1) _STATE_FILES lengkap (red-proof per file dilakukan terpisah, lihat laporan)
# ---------------------------------------------------------------------------
def test_state_files_melindungi_dua_jalur_tulis_baru():
    files = set(conftest._STATE_FILES)
    assert "security_audit.jsonl" in files, "V1: audit telegram_auth tidak terpantau"
    assert "_tmp_sheet_batch.json" in files, "V1: batch gsheets CLI tidak terpantau"
    assert len(files) >= 13, f"daftar state menyusut: {len(files)} nama"


def test_tmp_sheet_batch_hormat_fallback_log_dir(monkeypatch, tmp_path):
    """Path file harus bisa diarahkan fixture isolasi (sebelumnya literal 'logs/...')."""
    import sheet_logger

    iso = tmp_path / "iso_logs"
    iso.mkdir()
    monkeypatch.setattr(sheet_logger, "FALLBACK_LOG_DIR", str(iso))
    monkeypatch.setattr(sheet_logger, "get_spreadsheet_id", lambda explicit=None: "id-palsu")

    called: dict[str, object] = {}

    seen: dict[str, object] = {}

    def fake_run(cmd, *a, **k):
        # File batch harus berada di bawah FALLBACK_LOG_DIR yang bisa diarahkan (bukan literal
        # logs/), dan memang ada di sana SAAT CLI dipanggil — ia dibersihkan di blok finally.
        seen["cmd"] = cmd
        seen["during_call"] = (iso / "_tmp_sheet_batch.json").exists()
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(sheet_logger.subprocess, "run", fake_run)
    assert sheet_logger.log_scan_via_gsheets_cli([["a", "b"]], "id-palsu") is True
    assert str(iso) in str(seen["cmd"]), "file batch masih ditulis ke path literal logs/"
    assert seen["during_call"] is True, "file batch tidak pernah dibuat di direktori yang diarahkan"
    assert not (iso / "_tmp_sheet_batch.json").exists(), "tmp file harus dibersihkan setelah CLI"


# ---------------------------------------------------------------------------
# 2) Notifier aman-encoding (repro cp1252 harus rc 0)
# ---------------------------------------------------------------------------
def test_safe_print_menelan_emoji_tanpa_menghancurkan_pemanggil():
    import notifier

    assert notifier._safe_print("✅ halo \U0001f680") is None


def test_repro_cp1252_jadi_rc_nol():
    """
    Regresi V2: dengan code page Windows warisan, preview ber-emoji dulu melempar
    UnicodeEncodeError dan MENGHENTIKAN pemanggil (rc=1). Kini harus rc=0.
    """
    env = dict(os.environ)
    env.update(
        {
            "PYTHONIOENCODING": "cp1252",
            "TELEGRAM_BOT_TOKEN": "dummy",
            "TELEGRAM_CHAT_ID": "dummy",
        }
    )
    code = (
        "import notifier\n"
        "ok = notifier.send_telegram('\\u2705 siklus selesai — win 3 | loss 1')\n"
        "print('SEND_RETURNED', ok)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=90,
    )
    assert proc.returncode == 0, f"rc={proc.returncode}\nSTDERR:\n{proc.stderr[-800:]}"
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "UnicodeEncodeError" not in combined
    assert "siklus selesai" in combined, f"pesan hilang: {combined[-400:]}"
    assert "SEND_RETURNED False" in combined, "cabang dummy harus tetap skip + return False"


def test_cabang_dummy_tetap_kembali_false():
    import notifier

    assert notifier.send_telegram("pesan biasa") is False


# ---------------------------------------------------------------------------
# 3) purge-fixture-ids
# ---------------------------------------------------------------------------
def _load_migrate():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "migrate_closed_ledger", REPO_ROOT / "tools" / "migrate_closed_ledger.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mcl = _load_migrate()

FIXTURE_IDS = ["1:100.0:", "1:100.0", "NVDA|1|100.0"]
REAL_IDS = ["501:90000.5:2026-11-18T07:00:00Z", "1:100.5:2026-11-18T07:00:00Z", "42:7.25:2026-11-19T01:02:03Z"]


def _write_ids(path: Path, ids: list[str]) -> None:
    path.write_text(json.dumps(ids, indent=2), encoding="utf-8")


def test_purge_fixture_ids_dry_run_tidak_menulis(tmp_path):
    p = tmp_path / "closed_ids.json"
    _write_ids(p, FIXTURE_IDS + REAL_IDS)
    before = p.read_text(encoding="utf-8")

    res = mcl.purge_fixture_ids(ids_path=str(p))

    assert res["total_ids"] == 6 and res["matched_count"] == 3
    assert res["kept_count"] == 3 and res["applied"] is False
    assert res["backup_path"] is None
    assert sorted(res["candidates_sample"]) == sorted(FIXTURE_IDS)
    assert p.read_text(encoding="utf-8") == before, "dry-run tidak boleh menulis"


def test_purge_fixture_ids_apply_dan_idempoten(tmp_path):
    p = tmp_path / "closed_ids.json"
    _write_ids(p, FIXTURE_IDS + REAL_IDS)

    first = mcl.purge_fixture_ids(ids_path=str(p), apply=True)
    assert first["applied"] is True and first["backup_path"]
    assert Path(first["backup_path"]).exists()
    assert sorted(json.loads(p.read_text(encoding="utf-8"))) == sorted(REAL_IDS)

    second = mcl.purge_fixture_ids(ids_path=str(p), apply=True)
    assert second["matched_count"] == 0 and second["applied"] is False
    assert sorted(json.loads(p.read_text(encoding="utf-8"))) == sorted(REAL_IDS)


def test_purge_fixture_ids_hanya_membuang_pola_terdaftar(tmp_path):
    """Kunci riil tidak boleh ikut hilang — termasuk yang mirip tapi beda angka."""
    p = tmp_path / "closed_ids.json"
    near_misses = [
        "1:100.000001:",          # entry berbeda
        "11:100.0:2026-11-18",    # contract berbeda
        "1:100.0:2026-11-18T07:00:00Z|extra",  # bukan pola yang didaftarkan
        "NVDA|1|100.05",
        "TOOLONGASSETNAME|1|100.0",
    ]
    _write_ids(p, near_misses)

    res = mcl.purge_fixture_ids(ids_path=str(p), apply=True)

    assert res["matched_count"] == 0, res["candidates_sample"]
    assert sorted(json.loads(p.read_text(encoding="utf-8"))) == sorted(near_misses)


def test_main_subcommand_purge_fixture_ids(tmp_path, capsys):
    p = tmp_path / "closed_ids.json"
    _write_ids(p, ["1:100.0:", "9:1.0:x"])
    rc = mcl.main(["purge-fixture-ids", "--ids-path", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN purge-fixture-ids" in out
    assert "matched=1" in out and "kept=1" in out
    assert p.read_text(encoding="utf-8").count("1:100.0") == 1


# ---------------------------------------------------------------------------
# 4) Artefak probe ronde sebelumnya harus bersih
# ---------------------------------------------------------------------------
def test_logs_tanpa_artefak_probe():
    logs = REPO_ROOT / "logs"
    leftovers = sorted(p.name for p in logs.glob("*.py"))
    assert leftovers == [], f"skrip probe tertinggal di logs/: {leftovers}"
    assert not (logs / "_ch_backup.csv").exists(), "salinan ledger dari probe ronde 5 masih ada"
    assert sorted(p.name for p in logs.glob("*.txt")) == []
