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


# ------------------------------------------------------------------------------
# Ronde 7: sumber KEDUA = gviz Daily_Scans (webhook Apps Script tidak bisa dipercaya buta)
# ------------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone  # noqa: E402

H = sheet_logger.DAILY_SCANS_HEADERS


def _ts(offset_hours: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).isoformat()


class _Resp:
    def __init__(self, status_code=200, payload=None, raise_json=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json
        self.headers = headers or {"content-type": "text/plain; charset=utf-8"}
        self.text = text

    def json(self):
        if self._raise_json:
            raise self._raise_json
        return self._payload


def _row(asset, ts, **over):
    d = dict.fromkeys(H, "")
    d.update({
        "timestamp_utc": ts, "balance_usdc": "15.26", "asset": asset, "side": "short",
        "swarm_score": "+0.42", "sentiment": "bullish", "consensus": "STRONG",
        "vendor_alignment": "both_agree", "p1_polarity": "0.5", "p4_momentum": "0.3",
        "p5_catalysts": "0.2", "p6_contrarian": "0.1", "verdict": "GO", "narrative": "narisi",
    })
    d.update(over)
    return [str(d[h]) for h in H]


def _stub_gviz(monkeypatch, rows, status="ok"):
    monkeypatch.setattr(
        sheet_logger, "fetch_sheet_csv_rows_ex",
        lambda sheet_name, **kw: (rows, status),
    )


def test_gviz_ambil_baris_terbaru_per_aset_dan_tanpa_saldo(monkeypatch):
    lama, baru = _ts(-5), _ts(-1)
    rows = [H, _row("NVDA", lama, swarm_score="+0.10"), _row("NVDA", baru), _row("AAPL", baru)]
    _stub_gviz(monkeypatch, rows)
    res, status = sheet_logger.fetch_latest_research_from_gviz(max_age_hours=6.0)
    assert status == "ok" and sorted(res) == ["AAPL", "NVDA"]
    assert res["NVDA"].fetched_at == baru, "baris terakhir menang (urutan tab = kronologis)"
    assert res["NVDA"].final_score == 0.42
    assert res["NVDA"].p1_to_p7_scores["P1_sentiment_polarity"] == 0.5
    for r in res.values():
        assert "15.26" not in str(vars(r)), "saldo tidak boleh ikut terbaca ke objek riset"


def test_gviz_menyaring_riset_basi(monkeypatch):
    rows = [H, _row("NVDA", _ts(-40)), _row("AAPL", _ts(-1))]
    _stub_gviz(monkeypatch, rows)
    res, status = sheet_logger.fetch_latest_research_from_gviz(max_age_hours=6.0)
    assert sorted(res) == ["AAPL"] and status == "ok"


def test_gviz_status_kegagalan_diteruskan_tanpa_dibungkus_palsu(monkeypatch):
    _stub_gviz(monkeypatch, [H, _row("NVDA", _ts())], status="http_error")
    assert sheet_logger.fetch_latest_research_from_gviz() == ({}, "http_error")
    _stub_gviz(monkeypatch, [H], status="ok")
    assert sheet_logger.fetch_latest_research_from_gviz() == ({}, "empty")


def test_gviz_menjadi_sumber_ketika_webhook_membalas_teks_sambutan(monkeypatch):
    """Kasus produksi 16:34Z: body 42 karakter 'Walt Perps ... is active!' -> harus jatuh ke gviz."""
    html = _Resp(200, raise_json=ValueError("Expecting value: line 1 column 1"),
                 text="Walt Perps Sheet Logger Webhook is active!",
                 headers={"content-type": "text/plain; charset=utf-8"})
    monkeypatch.setattr(sheet_logger.httpx, "get", lambda url, **k: html)
    _stub_gviz(monkeypatch, [H, _row("NVDA", _ts(-1))])

    res = sheet_logger.fetch_latest_research_from_sheet(
        webhook_url="https://example.invalid/exec", csv_path="logs/_tidak_ada_r7.csv",
        max_age_hours=6.0,
    )
    assert "NVDA" in res
    assert sheet_logger.get_research_read_status() == "ok"
    assert sheet_logger.get_research_read_source() == "gviz"


def test_webhook_sehat_tetap_menjadi_sumber_utama(monkeypatch):
    from datetime import datetime as _dt
    fresh = _dt.now(timezone.utc).isoformat()
    ok = _Resp(200, {"status": "success", "data": {"AAPL": {"timestamp": fresh, "sentiment": "neutral"}}})
    monkeypatch.setattr(sheet_logger.httpx, "get", lambda url, **k: ok)
    dipanggil = []
    monkeypatch.setattr(
        sheet_logger, "fetch_latest_research_from_gviz",
        lambda *a, **k: dipanggil.append(1) or ({}, "empty"),
    )
    res = sheet_logger.fetch_latest_research_from_sheet(
        webhook_url="https://example.invalid/exec", csv_path="logs/_tidak_ada_r7.csv"
    )
    assert "AAPL" in res and sheet_logger.get_research_read_source() == "webhook"
    assert dipanggil == [], "gviz tidak boleh menambah panggilan HTTP bila webhook sudah cukup"


def test_csv_lokal_hanya_dipakai_setelah_webhook_dan_gviz_gagal(monkeypatch):
    html = _Resp(200, raise_json=ValueError("Expecting value"), text="Walt Perps ... is active!")
    monkeypatch.setattr(sheet_logger.httpx, "get", lambda url, **k: html)
    _stub_gviz(monkeypatch, [], status="http_error")
    res = sheet_logger.fetch_latest_research_from_sheet(
        webhook_url="https://example.invalid/exec", csv_path="logs/_tidak_ada_r7.csv"
    )
    assert res == {} and sheet_logger.get_research_read_source() == ""


def test_probe_menyebut_sumber_yang_terpakai(monkeypatch, capsys):
    _stub(monkeypatch, "ok")
    monkeypatch.setattr(sheet_logger, "get_research_read_source", lambda: "gviz")
    assert rp.main() == 0
    assert "sumber_terpakai      = gviz" in capsys.readouterr().out


def test_header_mismatch_melaporkan_kolom_yang_sebenarnya_terlihat(monkeypatch, capsys):
    """Dua sebab (tab belum ada vs skema beda) harus bisa dibedakan dari Step Summary."""
    _stub(monkeypatch, "html_or_unparsable")
    monkeypatch.setattr(
        sheet_logger, "get_last_header_mismatch",
        lambda name: ["Timestamp", "Balance", "Asset", "Side"] if name == "Daily_Scans" else [],
    )
    assert rp.main() == 1
    out = capsys.readouterr().out
    assert "kolom_terlihat       = Timestamp, Balance, Asset, Side" in out


def test_perekaman_kolom_datari_pembacaan_gviz_sebenarnya(monkeypatch):
    """Bukan hanya getter: jalur gviz nyata harus mengisi daftar kolom saat header tidak cocok."""
    class _R:
        status_code = 200
        headers = {"content-type": "text/csv"}
        # header TANPA kolom 'asset' -> tab Daily_Scans belum ada / skemanya lain
        text = "Timestamp (UTC),Asset Name,Margin ($)\n2026-11-20T00:00:00Z,NVDA,2.50\n"

    monkeypatch.setenv("GOOGLE_SHEET_ID", "id-untuk-test-bukan-id-nyata")
    monkeypatch.setattr(sheet_logger.httpx, "get", lambda url, **kw: _R())
    sheet_logger._last_header_mismatch.pop("Daily_Scans", None)

    rows, status = sheet_logger.fetch_sheet_csv_rows_ex("Daily_Scans", expected_col_name="asset")
    assert rows == [] and status == "header_mismatch"
    assert sheet_logger.get_last_header_mismatch("Daily_Scans") == [
        "Timestamp (UTC)", "Asset Name", "Margin ($)"
    ], "nama kolom yang terlihat wajib terekam supaya penyebabnya bisa dibedakan"
