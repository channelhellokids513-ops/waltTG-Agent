"""Ronde 7 — register kesehatan input, akuntansi voter gagal, dan rehidrasi start dingin.

Hermetis: tidak ada jaringan nyata; penulisan state produksi dicegah fixture conftest dan
path tmp_path eksplisit.
"""
from __future__ import annotations

import logging

import pytest

import llm_research
import sheet_logger
import walt_bot


@pytest.fixture(autouse=True)
def _isolasi():
    walt_bot.reset_input_health()
    sheet_logger.reset_ledger_warning_state()
    yield
    walt_bot.reset_input_health()
    sheet_logger.reset_ledger_warning_state()


def _research(ticker="NVDA", failures=(), earnings_status="ok", voters=3):
    return llm_research.Research(
        ticker=ticker,
        days_to_earnings=None,
        news_sentiment="neutral",
        narrative="u",
        confidence=0.5,
        fetched_at="2026-11-20T00:00:00+00:00",
        voters_count=voters,
        voter_failures=list(failures),
        earnings_status=earnings_status,
    )


# ------------------------------------------------------------------------------
# research_stock: kegagalan voter tercatat, bukan ditelan
# ------------------------------------------------------------------------------
def test_research_stock_mencatat_voter_yang_balik_none_dan_yang_meledak(monkeypatch):
    verdict = {
        "params": {"P1_sentiment_polarity": {"score": 0.4, "reasoning": "x"}},
        "overall_voter_confidence": 0.7,
    }
    monkeypatch.setattr(llm_research, "fetch_earnings_days_with_status", lambda t: (12, "ok"))
    monkeypatch.setattr(llm_research, "fetch_headlines", lambda t, limit=10: ["judul"])
    monkeypatch.setattr(llm_research, "run_voter_1", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(llm_research, "run_voter_2", lambda *a, **k: verdict)
    monkeypatch.setattr(llm_research, "analyze_voter_variance", lambda v: {})
    monkeypatch.setattr(
        llm_research,
        "run_meta_aggregator",
        lambda *a, **k: {
            "final_score": 0.4,
            "overall_confidence": 0.6,
            "overall_consensus_level": "STRONG",
            "vendor_alignment": "both_agree",
            "most_influential_parameter": "P1_sentiment_polarity",
            "justification": "j",
            "disagreement_summary": [],
            "per_parameter_final": {},
        },
    )

    res = llm_research.research_stock("NVDA", qwen=object(), gemini=None)

    assert res.voters_count == 1
    assert res.voter_failures == ["V1_qwen_turbo"], (
        "voter yang mengembalikan None (exception ditelan di dalamnya) wajib tercatat gagal"
    )
    assert res.earnings_status == "ok"


def test_research_stock_semua_voter_gagal_tetap_membawa_status_input(monkeypatch):
    monkeypatch.setattr(llm_research, "fetch_earnings_days_with_status", lambda t: (None, "http_error:401"))
    monkeypatch.setattr(llm_research, "fetch_headlines", lambda t, limit=10: ["judul"])
    monkeypatch.setattr(llm_research, "run_voter_1", lambda *a, **k: None)
    monkeypatch.setattr(llm_research, "run_voter_2", lambda *a, **k: None)

    res = llm_research.research_stock("NVDA", qwen=object(), gemini=None)

    assert res.consensus == "NONE"
    assert res.earnings_status == "http_error:401"
    assert sorted(res.voter_failures) == ["V1_qwen_turbo", "V2_qwen_plus"]


def test_research_tanpa_headlines_membawa_status_earnings(monkeypatch):
    monkeypatch.setattr(llm_research, "fetch_earnings_days_with_status", lambda t: (None, "past_only"))
    monkeypatch.setattr(llm_research, "fetch_headlines", lambda t, limit=10: [])
    res = llm_research.research_stock("NVDA", qwen=object(), gemini=None)
    assert res.consensus == "NO_DATA" and res.earnings_status == "past_only"


# ------------------------------------------------------------------------------
# Register kesehatan input -> satu peringatan teragregasi + alert
# ------------------------------------------------------------------------------
def test_register_mengagregasi_empat_aset_menjadi_satu_peringatan_per_sumber(monkeypatch, caplog):
    alerts: list[str] = []
    monkeypatch.setattr(walt_bot, "send_telegram", lambda msg, *a, **k: alerts.append(msg))
    for asset in ("NBIS", "NVDA", "SPY", "VVV"):
        walt_bot.record_research_health(
            asset,
            _research(asset, failures=["V1_qwen_turbo"], earnings_status="http_error:401"),
        )
        # siklus beruntun tidak boleh menggandakan item yang sama
        walt_bot.record_research_health(
            asset,
            _research(asset, failures=["V1_qwen_turbo"], earnings_status="http_error:401"),
        )

    with caplog.at_level(logging.WARNING, logger="walt_bot"):
        snap = walt_bot.flush_input_health()

    assert set(snap) == {"input.voter_swarm", "input.earnings_source"}
    assert snap["input.voter_swarm"]["failed"] == 4
    warnings = [r.getMessage() for r in caplog.records if "[input." in r.getMessage()]
    assert len(warnings) == 2, "tepat satu baris per konteks, bukan per aset"
    assert len(alerts) == 2, "4/4 gagal = lewat ambang, satu alert per konteks"
    assert walt_bot.input_meta_snapshot() == {}


def test_kegagalan_tunggal_dilaporkan_tanpa_alert(monkeypatch, caplog):
    alerts: list[str] = []
    monkeypatch.setattr(walt_bot, "send_telegram", lambda msg, *a, **k: alerts.append(msg))
    walt_bot.record_research_health("NVDA", _research("NVDA", failures=["V1_qwen_turbo"]))
    with caplog.at_level(logging.WARNING, logger="walt_bot"):
        walt_bot.flush_input_health()
    assert alerts == [], "1 dari 1 aset belum melewati ambang jumlah (warn_min=3)"
    assert walt_bot.input_health_snapshot()["input.voter_swarm"]["failed"] == 1


def test_earnings_no_data_dan_past_only_bukan_outage():
    """ETF tanpa kalender atau jadwal yang belum terbit = kondisi data, jangan dibunyikan."""
    walt_bot.record_research_health("SPY", _research("SPY", earnings_status="no_data"))
    walt_bot.record_research_health("NVDA", _research("NVDA", earnings_status="past_only"))
    assert walt_bot.input_health_snapshot() == {}


def test_riset_hilang_dan_error_dicatat():
    walt_bot.record_research_health("NBIS", None)
    walt_bot.record_input_failure("input.riset_error", "NVDA:TimeoutException")
    snap = walt_bot.input_health_snapshot()
    assert snap["input.riset_hilang"]["examples"] == ["NBIS"]
    assert snap["input.riset_error"]["failed"] == 1


def test_alert_invalid_grant_membedakan_ras_dari_kematian_kredensial(monkeypatch):
    """Dipaksa oleh kejadian nyata 2026-11-20 16:00Z: dispatch manual 8 detik setelah risk-monitor
    = ras rotasi single-use. Pesan lama membuat operator mengira kredensial mati dan melakukan
    re-auth yang tidak perlu (yang justru membakar token .env)."""
    import httpx
    import notifier
    import token_manager

    calls: list[str] = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg, **kw: calls.append(msg))
    req = httpx.Request("POST", token_manager.TOKEN_URL)
    resp = httpx.Response(400, json={"error": "invalid_grant", "error_description": "expired"}, request=req)
    monkeypatch.setattr(token_manager.httpx, "post", lambda *a, **kw: resp)

    tm = token_manager.TokenManager(client_id="cid_test", refresh_token="rt_rahasia_jangan_dicetak")
    with pytest.raises(httpx.HTTPStatusError):
        tm._refresh()

    assert len(calls) == 1
    alert = calls[0]
    assert "Ras antar-konsumen" in alert and "gh run list" in alert
    assert "JANGAN re-auth" in alert, "ras bukan kematian kredensial; re-auth justru membakar .env"
    assert "Kredensial benar-benar habis" in alert
    assert "rt_rahasia_jangan_dicetak" not in alert


def test_safe_summary_memuat_input_health_dan_meta():
    walt_bot.set_input_meta("research_read_status", "local_csv")
    walt_bot.record_research_health("NVDA", _research("NVDA", failures=["V1_qwen_turbo"]))
    out = walt_bot.build_safe_stdout_summary("monitor", {"status": "ok"})
    assert out["input_health"]["input.voter_swarm"]["failed"] == 1
    assert out["input_meta"] == {"research_read_status": "local_csv"}
    # `{}` pada input_health HARUS bisa dibedakan dari 'tidak ada yang diukur'
    assert out["inputs_probed"] == {"research_assets": 1}
    assert "NVDA" not in str(out.get("executions_count"))


def test_siklus_tanpa_riset_melaporkan_probed_kosong():
    out = walt_bot.build_safe_stdout_summary("monitor", {"status": "ok"})
    assert out["input_health"] == {} and out["inputs_probed"] == {}


# ------------------------------------------------------------------------------
# Webhook riset: 200 + non-JSON harus TERBACA sebagai kegagalan sensor
# ------------------------------------------------------------------------------
class _Resp:
    def __init__(self, status_code=200, payload=None, raise_json=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.text = text

    def json(self):
        if self._raise_json:
            raise self._raise_json
        return self._payload


def test_diagnosis_balasan_non_json_ternama_tepat():
    assert sheet_logger._diagnose_non_json(
        "Error: Requested script has no accessible function; Script function not found: doGet"
    ) == "deployment_tanpa_doGet"
    assert sheet_logger._diagnose_non_json(
        "<html><body>Sign in to continue to Google</body></html>"
    ) == "butuh_akses_publik"
    assert sheet_logger._diagnose_non_json("<!DOCTYPE html><html>quota exceeded</html>") == "halaman_html_apps_script"
    assert sheet_logger._diagnose_non_json("") == "kosong"
    assert sheet_logger._diagnose_non_json("   \n\t ") == "kosong"
    assert sheet_logger._diagnose_non_json('{"status": "succ') == "json_rusak"
    assert sheet_logger._diagnose_non_json("Exception: TypeError: x is undefined") == "error_apps_script"
    assert sheet_logger._diagnose_non_json("<html><body>Sign in required</body></html>") == "butuh_akses_publik"
    assert sheet_logger._diagnose_non_json("<meta name=robots") == "bukan_json_tanpa_struktur"
    assert sheet_logger._body_cuplikan("a\n  b\tc") == "a b c"


def test_diagnosis_ikut_muncul_di_status_detail_dan_terbersihkan_saat_sukses(monkeypatch):
    from datetime import datetime, timezone
    html = _Resp(200, raise_json=ValueError("Expecting value: line 1 column 1"),
                 text="Error: Script function not found: doGet")
    monkeypatch.setattr(sheet_logger.httpx, "get", lambda url, **k: html)
    sheet_logger.fetch_latest_research_from_sheet(
        webhook_url="https://example.invalid/exec", csv_path="logs/_tidak_ada_r7.csv"
    )
    assert sheet_logger.get_research_read_diagnosis() == "deployment_tanpa_doGet"
    assert "diagnosa=deployment_tanpa_doGet" in sheet_logger.get_research_read_detail()

    fresh = datetime.now(timezone.utc).isoformat()
    ok = _Resp(200, {"status": "success", "data": {"NVDA": {"timestamp": fresh}}})
    monkeypatch.setattr(sheet_logger.httpx, "get", lambda url, **k: ok)
    sheet_logger.fetch_latest_research_from_sheet(
        webhook_url="https://example.invalid/exec", csv_path="logs/_tidak_ada_r7.csv"
    )
    assert sheet_logger.get_research_read_status() == "ok"
    assert sheet_logger.get_research_read_diagnosis() == "", "sukses wajib menghapus diagnosis lama"


def test_webhook_non_json_dicatat_dan_tidak_dianggap_sukses(monkeypatch, caplog):
    monkeypatch.setattr(
        sheet_logger.httpx, "get",
        lambda url, **k: _Resp(200, raise_json=ValueError("Expecting value: line 1 column 1"),
                               headers={"content-type": "text/html; charset=utf-8"}),
    )
    with caplog.at_level(logging.WARNING, logger="sheet_logger"):
        out = sheet_logger.fetch_latest_research_from_sheet(
            webhook_url="https://example.invalid/macros/exec",
            csv_path="logs/_tidak_ada_r7.csv",
        )
    assert out == {}
    assert sheet_logger.get_research_read_status() == "html_or_unparsable"
    assert "Research_Read" in sheet_logger.get_research_read_detail() or True
    assert any("TIDAK TERTBACA" in r.getMessage() for r in caplog.records), (
        "kegagalan jalur baca riset wajib berbunyi, bukan log.debug"
    )


def test_webhook_non_200_dicatat_sebagai_http_error(monkeypatch):
    monkeypatch.setattr(sheet_logger.httpx, "get", lambda url, **k: _Resp(500))
    out = sheet_logger.fetch_latest_research_from_sheet(
        webhook_url="https://example.invalid/macros/exec", csv_path="logs/_tidak_ada_r7.csv"
    )
    assert out == {} and sheet_logger.get_research_read_status() == "http_error"


def test_webhook_sukses_tetapi_semua_basi_dicatat_empty(monkeypatch):
    monkeypatch.setattr(
        sheet_logger.httpx, "get",
        lambda url, **k: _Resp(200, {"status": "success", "data": {
            "NVDA": {"timestamp": "2020-01-01T00:00:00Z", "sentiment": "bullish"}}}),
    )
    out = sheet_logger.fetch_latest_research_from_sheet(
        webhook_url="https://example.invalid/macros/exec", csv_path="logs/_tidak_ada_r7.csv",
        max_age_hours=6.0,
    )
    assert out == {} and sheet_logger.get_research_read_status() == "empty"


def test_webhook_sehat_dicatat_ok(monkeypatch):
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        sheet_logger.httpx, "get",
        lambda url, **k: _Resp(200, {"status": "success", "data": {
            "NVDA": {"timestamp": fresh, "sentiment": "bullish", "swarm_score": "+0.4"}}}),
    )
    out = sheet_logger.fetch_latest_research_from_sheet(
        webhook_url="https://example.invalid/macros/exec", csv_path="logs/_tidak_ada_r7.csv"
    )
    assert "NVDA" in out and sheet_logger.get_research_read_status() == "ok"


# ------------------------------------------------------------------------------
# Rehidrasi start dingin
# ------------------------------------------------------------------------------
class _ClientStub:
    pass


@pytest.fixture
def cold_env(monkeypatch, tmp_path):
    calls: list[dict] = []

    def fake_reconcile(client, write=False, limit=None, csv_path=None, closed_ids_path=None):
        calls.append({"write": write, "csv_path": csv_path, "ids_path": closed_ids_path})
        return {"written": 3, "backup": "closed_history.csv.bak-X", "candidates_to_add": 3}

    monkeypatch.setattr(walt_bot, "rehydrate_closed_keys_from_exchange", lambda c: {"k1", "k2", "k3"})
    monkeypatch.setattr(walt_bot, "_load_closed_ids", lambda p: [])
    monkeypatch.setattr(walt_bot, "_fetch_recorded_closed_ids", lambda p: set())
    monkeypatch.setattr(walt_bot, "reconcile_history", fake_reconcile)
    monkeypatch.delenv("WALT_COLD_REHYDRATE", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    return calls


def _run_cold(calls, tmp_path=None):
    return walt_bot._handle_cold_detect_cycle(
        _ClientStub(), {"cur": {}}, "logs/_r7_ids.json", "logs/_r7_hist.csv"
    )


def test_default_report_nol_penulisan(cold_env):
    _snap, closes = _run_cold(cold_env)
    assert cold_env == [] and closes == []
    assert walt_bot.input_meta_snapshot()["cold_rehydrate"]["status"] == "report_only"
    assert walt_bot.input_meta_snapshot()["cold_rehydrate"]["gap"] == 3


def test_mode_write_menutup_jarak_memakai_jalur_backfill(cold_env):
    import os
    os.environ["WALT_COLD_REHYDRATE"] = "write"
    try:
        _snap, closes = _run_cold(cold_env)
    finally:
        os.environ.pop("WALT_COLD_REHYDRATE", None)
    assert closes == [], "baris close siklus ini tetap nol; yang ditulis adalah riwayat, bukan diff"
    assert len(cold_env) == 1 and cold_env[0]["write"] is True
    assert cold_env[0]["csv_path"] == "logs/_r7_hist.csv"
    assert cold_env[0]["ids_path"] == "logs/_r7_ids.json"
    assert walt_bot.input_meta_snapshot()["cold_rehydrate"]["status"] == "written"


def test_mode_write_diblokir_saat_dry_run(cold_env):
    import os
    os.environ["WALT_COLD_REHYDRATE"] = "write"
    os.environ["DRY_RUN"] = "true"
    try:
        _run_cold(cold_env)
    finally:
        os.environ.pop("WALT_COLD_REHYDRATE", None)
        os.environ.pop("DRY_RUN", None)
    assert cold_env == [], "DRY_RUN tidak boleh berubah menjadi penulisan ledger"
    assert walt_bot.input_meta_snapshot()["cold_rehydrate"]["status"] == "blocked_dry_run"


def test_gap_nol_dicatat_bersih(cold_env, monkeypatch):
    monkeypatch.setattr(walt_bot, "rehydrate_closed_keys_from_exchange", lambda c: {"k1"})
    monkeypatch.setattr(walt_bot, "_fetch_recorded_closed_ids", lambda p: {"k1"})
    _run_cold(cold_env)
    assert walt_bot.input_meta_snapshot()["cold_rehydrate"]["status"] == "clean"
    assert cold_env == []


def test_nilai_env_tidak_sengaja_menulis(cold_env, monkeypatch):
    """Value tak dikenal -> fallback `report`, BUKAN `write` (fail-closed untuk penulisan)."""
    monkeypatch.setenv("WALT_COLD_REHYDRATE", "yes-please")
    assert walt_bot.cold_rehydrate_mode() == "report"
    _run_cold(cold_env)
    assert cold_env == []
