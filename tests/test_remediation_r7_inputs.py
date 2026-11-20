"""Ronde 7 — input yang mati harus BERBUNYI, dan start dingin tidak lagi berarti amnesia.

Tiga kelas perbaikan yang diuji di sini:
  1. Voter swarm: model V1 tidak lagi mengacu ke `qwen-turbo` (404 model_not_found di hulu),
     dapat dioverride lewat variable, dan 404 memicu fallback yang sama seperti 429.
  2. Kalender earnings: satu sumber (Yahoo + cookie/crumb), tanggal di masa lalu TIDAK dibaca
     sebagai "H-0", dan status sumber terbaca.
  3. Kesehatan input: kegagalan voter/earnings/webhook riset teragregasi sekali per siklus dan
     muncul di safe summary; `WALT_COLD_REHYDRATE=write` menutup jarak ledger-vs-bursa memakai
     jalur backfill yang sudah ada (dan tetap diblokir saat DRY_RUN).

Hermetis: tidak ada jaringan nyata, tidak ada penulisan ke state produksi.
"""
from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime, timedelta

import pytest

import llm_research
import sheet_logger
import walt_bot


@pytest.fixture(autouse=True)
def _isolasi_register_input():
    walt_bot.reset_input_health()
    yield
    walt_bot.reset_input_health()
    sheet_logger.reset_ledger_warning_state()


# ------------------------------------------------------------------------------
# 1. MODEL V1
# ------------------------------------------------------------------------------
def test_v1_tidak_lagi_memakai_model_yang_dipensiunkan():
    assert llm_research.MODEL_V1 != "qwen-turbo", (
        "qwen-turbo menjawab 404 model_not_found di dashscope-intl (probe 2026-11-20)"
    )


def test_v1_dapat_dioverride_lewat_variable_tanpa_mengubah_kode(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_MODEL_V1", "qwen-flash-2026")
    monkeypatch.setenv("GEMINI_MODEL_V4", "gemini-pro-ucapan")
    monkeypatch.setenv("DASHSCOPE_MODEL_AGG", "qwen-max-2026")
    mod = importlib.reload(llm_research)
    try:
        assert mod.MODEL_V1 == "qwen-flash-2026"
        assert mod.MODEL_V4 == "gemini-pro-ucapan"
        assert mod.MODEL_AGGREGATOR == "qwen-max-2026"
    finally:
        monkeypatch.undo()
        importlib.reload(llm_research)


class _FakeResponse:
    def __init__(self, status_code, payload=None, raise_json=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._raise_json:
            raise self._raise_json
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakePostClient:
    """Merekam urutan payload; membalas 404 lalu 200 untuk menguji fallback voter."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, path, json=None):
        # Salin payload: produksi memutar kunci "model" pada dict yang sama untuk percobaan
        # ulang, jadi menyimpan referensi akan membuat kedua panggilan tampak identik.
        self.calls.append({"path": path, "payload": dict(json or {})})
        return self._responses.pop(0)


def test_404_model_v1_memicu_fallback_ke_v2():
    ok = _FakeResponse(200, {"choices": [{"message": {"content": json.dumps({"params": {}})}}]})
    client = llm_research.QwenClient(api_key="dummy-key")
    client.client = _FakePostClient([_FakeResponse(404), ok])
    out = client.chat_json([{"role": "user", "content": "x"}], model=llm_research.MODEL_V1)

    assert out == {"params": {}}
    assert len(client.client.calls) == 2, "404 harus memicu satu percobaan ulang"
    assert client.client.calls[0]["payload"]["model"] == llm_research.MODEL_V1
    assert client.client.calls[1]["payload"]["model"] == llm_research.MODEL_V2


def test_429_tetap_berfallback_dan_500_tidak_diulang():
    ok = _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})
    client = llm_research.QwenClient(api_key="dummy-key")
    client.client = _FakePostClient([_FakeResponse(429), ok])
    client.chat_json([{"role": "user", "content": "x"}], model=llm_research.MODEL_V1)
    assert client.client.calls[1]["payload"]["model"] == llm_research.MODEL_V2

    client2 = llm_research.QwenClient(api_key="dummy-key")
    client2.client = _FakePostClient([_FakeResponse(500)])
    with pytest.raises(Exception):
        client2.chat_json([{"role": "user", "content": "x"}], model=llm_research.MODEL_V1)
    assert len(client2.client.calls) == 1, "500 bukan sinyal 'model mati' — jangan ganti model"


# ------------------------------------------------------------------------------
# 2. KALENDER EARNINGS
# ------------------------------------------------------------------------------
def _payload(dates):
    return {"quoteSummary": {"result": [{"calendarEvents": {"earnings": {"earningsDate": dates}}}]}}


def test_earnings_date_masa_lalu_bukan_H0():
    """NVDA 2026-11-20: earningsDate = 2026-11-17 (jadwal berikutnya belum dipublikasi)."""
    now = datetime(2026, 11, 20, 12, 0, 0)
    past_ts = int(datetime(2026, 11, 17, 20, 0, 0).timestamp())
    days, status = llm_research._parse_next_earnings_days(_payload([{"raw": past_ts}]), now)
    assert (days, status) == (None, "past_only"), (
        "tanggal lampau tidak boleh jadi 0 — gerbang earnings H-7 akan mem-SKIP aset itu selamanya"
    )


def test_earnings_memilih_tanggal_depan_terdekat():
    now = datetime(2026, 11, 20, 12, 0, 0)
    dates = [
        {"raw": int((now - timedelta(days=3)).timestamp())},
        {"raw": int((now + timedelta(days=30)).timestamp())},
        {"raw": int((now + timedelta(days=70)).timestamp())},
    ]
    days, status = llm_research._parse_next_earnings_days(_payload(dates), now)
    assert status == "ok"
    assert 28 <= days <= 30, "yang diambil tanggal berikutnya yang paling dekat"


def test_earnings_etf_tanpa_kalender_itu_no_data_bukan_outage():
    days, status = llm_research._parse_next_earnings_days(
        {"quoteSummary": {"result": [{"calendarEvents": {}}]}}, datetime.now()
    )
    assert (days, status) == (None, "no_data")


class _FakeYahooClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.requests.append((url, params or {}))
        return self._responses.pop(0)


def test_fetch_earnings_meneruskan_crumb_dan_melaporkan_http_error():
    fake = _FakeYahooClient([_FakeResponse(401)])
    days, status = llm_research.fetch_earnings_days_with_status("NVDA", client=fake, crumb="abc")
    assert (days, status) == (None, "http_error:401")
    assert fake.requests[0][1].get("crumb") == "abc", "crumb wajib ikut dikirim"


def test_tanpa_crumb_fungsi_menyerah_sebelum_menyerang_endpoint(monkeypatch):
    monkeypatch.setattr(llm_research, "_yahoo_state", (None, None))
    days, status = llm_research.fetch_earnings_days_with_status("NVDA")
    assert (days, status) == (None, "no_crumb"), (
        "crumb terikat pada cookie sesi; memanggil quoteSummary tanpa sesi = 401 yang pasti"
    )


def test_kontrak_lama_fetch_earnings_days_tetap_kembali_angka_atau_none(monkeypatch):
    monkeypatch.setattr(
        llm_research, "fetch_earnings_days_with_status",
        lambda ticker, client=None, crumb=None: (5, "ok"),
    )
    assert llm_research.fetch_earnings_days("AAPL") == 5
    monkeypatch.setattr(
        llm_research, "fetch_earnings_days_with_status",
        lambda ticker, client=None, crumb=None: (None, "past_only"),
    )
    assert llm_research.fetch_earnings_days("NVDA") is None


def test_walt_bot_hanya_punya_satu_parser_earnings():
    """check_earnings_date tidak boleh menyalin parser sendiri (dua salinan = dua bug)."""
    names = walt_bot.check_earnings_date.__code__.co_names
    assert "fetch_earnings_days_with_status" in names
    joined = " ".join(names)
    assert "quoteSummary" not in joined and "yahoo" not in joined.lower(), (
        "URL hulu hanya boleh ada di llm_research"
    )


def test_404_itu_instrumen_tanpa_kalender_bukan_outage():
    """Dibuktikan live: SPY (ETF) menjawab 404 pada quoteSummary; 401/5xx tetap outage."""
    fake = _FakeYahooClient([_FakeResponse(404)])
    days, status = llm_research.fetch_earnings_days_with_status("SPY", client=fake, crumb="abc")
    assert (days, status) == (None, "no_data")
    walt_bot.record_research_health("SPY", llm_research.Research(
        ticker="SPY", days_to_earnings=None, news_sentiment="neutral", narrative="u",
        confidence=0.5, fetched_at="2026-11-20T00:00:00+00:00", earnings_status=status))
    assert walt_bot.input_health_snapshot() == {}, "404 ETF tidak boleh memicu alert"

    fake5 = _FakeYahooClient([_FakeResponse(503)])
    assert llm_research.fetch_earnings_days_with_status("NVDA", client=fake5, crumb="abc")[1] == "http_error:503"


def test_crumb_sesi_tidak_pernah_muncul_di_log():
    """URL Yahoo memuat crumb; httpx mencatat URL pada level INFO -> wajib disaring."""
    assert any(
        isinstance(f, llm_research._CrumbRedactingFilter) for f in logging.getLogger("httpx").filters
    ), "filter redaksi crumb harus terpasang saat modul diimpor"
    rec = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1,
        'HTTP Request: GET https://query1.finance.yahoo.com/v10/finance/quoteSummary/NVDA?crumb=%s "HTTP/1.1 200 OK"',
        ("SECRETCRUMB",), None,
    )
    assert llm_research._CrumbRedactingFilter().filter(rec) is True
    rendered = rec.getMessage()
    assert "SECRETCRUMB" not in rendered and "crumb=<redacted>" in rendered

    rec_bersih = logging.LogRecord("httpx", logging.INFO, __file__, 1, "GET /v8/finance/chart/NVDA", (), None)
    llm_research._CrumbRedactingFilter().filter(rec_bersih)
    assert rec_bersih.getMessage() == "GET /v8/finance/chart/NVDA", "baris lain tidak boleh berubah"
