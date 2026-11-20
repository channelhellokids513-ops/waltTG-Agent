"""(b) Yang mengawal si pengawal: watchdog tidak boleh lagi bergantung pada cron GitHub semata.

Terukur: run `schedule` terakhir 09:55Z sementara push/CI jalan terus (~7 jam kosong), padahal
README §281 sudah lama mencatat cron GitHub bisa telat 7-8 jam — catatan itu diterapkan ke ENTRY
tapi tidak ke watchdog-nya sendiri.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tools import watchdog_check as wd

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)


class _P:
    def __init__(self, payload, returncode=0, stderr=""):
        self.stdout = json.dumps(payload)
        self.returncode = returncode
        self.stderr = stderr


def _runs(*menit_lalu):
    return [
        {"updatedAt": (NOW - timedelta(minutes=m)).isoformat(), "status": "completed"}
        for m in menit_lalu
    ]


def _stub(monkeypatch, runs_payload, dispatch_rc=0):
    """Kembalikan (run, panggilan): `run` menyuntik subprocess, `panggilan` mencatat argv."""
    panggilan = []

    def run(cmd, *a, **k):
        panggilan.append(list(cmd))
        if cmd[:3] == ["gh", "workflow", "run"]:
            return _P([], returncode=dispatch_rc, stderr="boom" if dispatch_rc else "")
        return _P(runs_payload)

    return run, panggilan


@pytest.fixture(autouse=True)
def _bersih(monkeypatch):
    for var in ("MAINTENANCE_MODE", "WATCHDOG_KEEPALIVE"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_usia_run_terbaru_tidak_memandang_status():
    """Watchdog yang gagal 5 menit lalu masih HIDUP; yang sukses 7 jam lalu tidak."""
    run, _ = _stub(None, _runs(5))
    assert wd.latest_run_age_minutes("watchdog.yml", NOW, run=run) == pytest.approx(5.0)
    run_kosong, _ = _stub(None, [])
    assert wd.latest_run_age_minutes("watchdog.yml", NOW, run=run_kosong) is None
    run_sampah, _ = _stub(None, [{"updatedAt": "bukan waktu"}])
    assert wd.latest_run_age_minutes("watchdog.yml", NOW, run=run_sampah) is None


def test_watchdog_muda_tidak_di_dispatch_ulang():
    run, panggilan = _stub(None, _runs(8, 70))
    assert wd.keep_watchdog_alive(now=NOW, run=run) == 0
    assert not any(c[:3] == ["gh", "workflow", "run"] for c in panggilan), \
        "baru jalan 8 menit lalu tidak perlu dispatch lagi"


def test_watchdog_tua_di_dispatch():
    run, panggilan = _stub(None, _runs(95))
    assert wd.keep_watchdog_alive(now=NOW, run=run) == 0
    assert any(c[:3] == ["gh", "workflow", "run"] and c[3] == "watchdog.yml"
               for c in panggilan), "cron menguap -> entry harus menutupi"


def test_belum_pernah_ada_run_di_dispatch():
    run, panggilan = _stub(None, [])
    assert wd.keep_watchdog_alive(now=NOW, run=run) == 0
    assert any(c[:3] == ["gh", "workflow", "run"] for c in panggilan)


def test_jendela_pemeliharaan_tidak_membangunkan_watchdog(monkeypatch):
    monkeypatch.setenv("MAINTENANCE_MODE", "true")
    run, panggilan = _stub(None, [])
    assert wd.keep_watchdog_alive(now=NOW, run=run) == 0
    assert not any(c[:3] == ["gh", "workflow", "run"] for c in panggilan), \
        "dispatch saat maintenance hanya membakar runner dan memicu alert palsu"


def test_pintu_darurat_manual(monkeypatch):
    monkeypatch.setenv("WATCHDOG_KEEPALIVE", "false")
    run, panggilan = _stub(None, [])
    assert wd.keep_watchdog_alive(now=NOW, run=run) == 0
    assert not any(c[:3] == ["gh", "workflow", "run"] for c in panggilan)


def test_kegagalan_dispatch_tidak_menjatuhkan_siklus_trading():
    """Keepalive itu fasilitas: kalau ia gagal, risk-monitor yang sedang menjaga posisi TETAP hijau."""
    run, panggilan = _stub(None, [], dispatch_rc=1)
    assert wd.keep_watchdog_alive(now=NOW, run=run) == 0
    assert any(c[:3] == ["gh", "workflow", "run"] for c in panggilan), "tetap mencoba, hanya tidak bikin merah"


@pytest.mark.parametrize("filename", ["risk-monitor.yml", "market-execute.yml"])
def test_langkah_keepalive_terpasang_benar(filename):
    wf = yaml.safe_load((REPO_ROOT / ".github/workflows" / filename).read_text(encoding="utf-8"))
    steps = next(iter(wf["jobs"].values()))["steps"]
    keep = [s for s in steps if "keep_watchdog_alive" in (s.get("run") or "")]
    assert len(keep) == 1, f"{filename}: harus tepat satu langkah keepalive"
    step = keep[0]
    assert step.get("if") == "always()", "justru saat langkah utama gagal watchdog harus tetap hidup"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}", "pakai token episodik, bukan PAT jangka panjang"
    assert not any(k.startswith("WALT_") for k in step["env"]), (
        "keepalive DILARANG menyentuh OAuth Walt: refresh token single-use, dan langkah ini ada di "
        "akhir job yang sudah membakarnya"
    )
    assert "GITHUB_STEP_SUMMARY" in step["run"], "hasil keepalive harus terbaca, bukan senyap"


def test_keepalive_tidak_menambah_langkah_oauth_kedua():
    """Aturan ronde 6 tetap hidup: tepat SATU langkah ber-refresh-token per job."""
    for filename in ("risk-monitor.yml", "market-execute.yml"):
        wf = yaml.safe_load((REPO_ROOT / ".github/workflows" / filename).read_text(encoding="utf-8"))
        steps = next(iter(wf["jobs"].values()))["steps"]
        ber_token = [s for s in steps if "WALT_REFRESH_TOKEN" in (s.get("env") or {})]
        assert len(ber_token) <= 1, f"{filename}: langkah ber-OAuth = {len(ber_token)} (ras token!)"
