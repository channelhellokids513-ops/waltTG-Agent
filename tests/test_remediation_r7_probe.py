"""Ronde 7 — probe jalur baca riset harus bisa dijalankan TANPA menyentuh refresh token.

Alasan kejadian nyata: webhook riset Apps Script membalas non-JSON dan tidak ada satu pun
tempat yang mengatakannya. Setelah diperbaiki/di-redeploy, verifikasinya harus semurah
mungkin — dispatch workflow entry hanya untuk menguji justru berisiko membakar token
single-use yang sedang dipakai risk-monitor (terbukti 16:00Z).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

import sheet_logger

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "research_probe", REPO_ROOT / "tools" / "research_probe.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = _load_probe()


@pytest.fixture(autouse=True)
def _bersihkan_status(monkeypatch):
    sheet_logger.reset_ledger_warning_state()
    monkeypatch.setattr(sheet_logger, "_research_read_diagnosis", "", raising=False)
    yield


def _stub(monkeypatch, status, diagnosis="", detail="", aset=("NVDA", "AAPL")):
    monkeypatch.setattr(
        sheet_logger, "fetch_latest_research_from_sheet",
        lambda *a, **k: {t: object() for t in aset},
    )
    monkeypatch.setattr(sheet_logger, "get_research_read_status", lambda: status)
    monkeypatch.setattr(sheet_logger, "get_research_read_diagnosis", lambda: diagnosis)
    monkeypatch.setattr(sheet_logger, "get_research_read_detail", lambda: detail)


@pytest.mark.parametrize("status", ["ok", "empty"])
def test_status_sehat_keluar_nol(monkeypatch, status, capsys):
    _stub(monkeypatch, status)
    assert rp.main() == 0
    out = capsys.readouterr().out
    assert "verdict              = SEHAT" in out
    assert "aset_segar           = 2" in out


@pytest.mark.parametrize(
    "status", ["html_or_unparsable", "local_csv", "http_error", "no_config", "unknown", "exception"]
)
def test_status_sakit_keluar_satu_dan_menyebut_tindakan(monkeypatch, status, capsys):
    _stub(monkeypatch, status, diagnosis="deployment_tanpa_doGet", detail="content_type=text/html")
    assert rp.main() == 1, "run hijau dengan jalur baca mati adalah laporan yang menipu"
    out = capsys.readouterr().out
    assert "SAKIT" in out
    assert "Manage deployments" in out, "probe harus memberi langkah perbaikan, bukan cuma status"
    assert "deployment_tanpa_doGet" in out
    assert "content_type=text/html" in out


def test_probe_mengenal_kosong_lebih_dulu_dari_sehat():
    """`empty` = webhook menjawab tapi riset basi -> sehat; bukan kegagalan sensor."""
    assert "empty" in rp.HEALTHY_STATUSES and "local_csv" not in rp.HEALTHY_STATUSES


def test_no_config_menyebut_kunci_yang_belum_dipasang(monkeypatch, capsys):
    _stub(monkeypatch, "no_config", detail="GOOGLE_SHEET_WEBHOOK_URL tidak diset")
    assert rp.main() == 1
    out = capsys.readouterr().out
    assert "pasang secret `GOOGLE_SHEET_WEBHOOK_URL`" in out, (
        "menyuruh redeploy padahal kuncinya belum dipasang = saran yang menyesatkan"
    )


def test_probe_tidak_pernah_menyentuh_jalur_oauth():
    src = (REPO_ROOT / "tools" / "research_probe.py").read_text(encoding="utf-8")
    for banned in ("walt_bot", "WaltClient", "walletbot.me", "WALT_REFRESH_TOKEN", "TokenManager"):
        assert banned not in src, f"probe baca riset tidak boleh menyentuh {banned}"


# ------------------------------------------------------------------------------
# Wiring CI
# ------------------------------------------------------------------------------
def _diag_steps():
    wf = yaml.safe_load((REPO_ROOT / ".github/workflows/diagnostics.yml").read_text(encoding="utf-8"))
    return wf, next(iter(wf["jobs"].values()))["steps"]


def test_diagnostics_memuat_probe_riset_dan_tanpa_oauth():
    wf, steps = _diag_steps()
    idx = next((i for i, s in enumerate(steps) if "tools/research_probe.py" in (s.get("run") or "")), None)
    assert idx is not None, "research_probe belum dipasang di diagnostics.yml"
    step = steps[idx]
    assert "if" not in step, "probe wajib jalan di SETIAP diagnostics, bukan hanya saat diminta"
    env = step.get("env") or {}
    assert "GOOGLE_SHEET_WEBHOOK_URL" in env and "GOOGLE_SHEET_ID" in env
    assert not any(k.startswith("WALT_") for k in env), "probe tidak boleh butuh kredensial Walt"

    # wajib selesai sebelum langkah pertama yang menyentuh token
    walt_idx = [i for i, s in enumerate(steps) if "WALT_REFRESH_TOKEN" in (s.get("env") or {})]
    assert walt_idx and idx < min(walt_idx), "probe harus tetap menghasilkan data saat token mati"
    assert wf["permissions"] == {"contents": "read"}


def test_hasil_probe_naik_ke_step_summary():
    _, steps = _diag_steps()
    step = next(s for s in steps if "tools/research_probe.py" in (s.get("run") or ""))
    run = step["run"]
    assert "GITHUB_STEP_SUMMARY" in run, "diagnosis harus terbaca di Step Summary, bukan hanya di log"
    assert "tee /dev/stderr" in run, "galat tetap harus terlihat di log langkah"
    assert run.strip().startswith("set -o pipefail") or "set -o pipefail" in run, (
        "pipefail wajib supaya kode keluar 1 dari probe tidak tertelan pipeline"
    )


def test_mode_read_only_menjalankan_nol_langkah_oauth():
    """Inti perubahan: mode tanpa-OAuth harus benar-benar nol, bukan 'kebetulan' lewat `!=`."""
    wf, steps = _diag_steps()
    conds = [s.get("if") for s in steps if "WALT_REFRESH_TOKEN" in (s.get("env") or {})]
    options = set(wf[True]["workflow_dispatch"]["inputs"]["oauth_step"]["options"])
    tercakup = {c.split("==")[1].strip().strip("'") for c in conds}
    for mode in sorted(options - tercakup):
        for c in conds:
            assert f"'{mode}'" not in c, f"mode {mode} tidak boleh memicu langkah ber-OAuth"
