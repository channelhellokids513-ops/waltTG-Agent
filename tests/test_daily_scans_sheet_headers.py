"""Simulasi lookup PERSIS `record[header[c]]` dari doPost Apps Script v2 yang live.

Kelas kegagalan yang dikunci: mengirim nama internal snake_case ke sheet berlabel Indonesia.
Four kolom kebetulan cocok namanya -> tertulis KOSONG; sisanya dianggap kolom baru ->
`ensureHeader_()` menambah duplikat permanen di kanan `Daily_Scans`.
"""

import json

import pytest

import sheet_logger as sl


LIVE_DAILY_SCANS_HEADER = [
    "Timestamp (UTC)", "Saldo (USDC)", "Posisi Terbuka", "Asset", "Sisi",
    "Funding/Hari ()", "Live Price ($)", "Swarm Score", "Sentimen", "Konsensus",
    "Vendor Alignment", "P1 (Polaritas)", "P4 (Momentum)", "P5 (Katalis)",
    "P6 (Crowded)", "Vonis", "Alokasi Margin ($)", "Alasan Keputusan",
    "Narasi Swarm AI",
]


def _fake_row():
    base = ["2026-11-22T00:17:00Z", "100.00", "0", "BTC", "SHORT", "+0.050%", "100000.0000",
            "+0.42", "bearish", "SINGLE_MODEL", "both_agree", "+0.10", "+0.20", "0.30", "0.40",
            "GO", "2.50", "alasan", "narasi"]
    return base + ["ok"] * len(sl.MG_COLUMNS)


def test_panjang_kolom_sama_dengan_daftar_internal():
    assert len(sl.DAILY_SCANS_SHEET_HEADERS) == len(sl.DAILY_SCANS_HEADERS) == 30


def test_19_kolom_lama_sama_persis_dengan_sheet_live_tanpa_duplikat():
    lama = sl.DAILY_SCANS_SHEET_HEADERS[:19]
    assert lama == LIVE_DAILY_SCANS_HEADER, "label harus sama byte-per-byte dengan header live"
    assert len(set(sl.DAILY_SCANS_SHEET_HEADERS)) == 30, "tidak boleh ada nama kolom dobel"
    assert sl.DAILY_SCANS_SHEET_HEADERS[19:] == sl.MG_COLUMNS


@pytest.mark.parametrize("label", LIVE_DAILY_SCANS_HEADER + list(sl.MG_COLUMNS))
def test_exact_lookup_doPost_v2_menemukan_setiap_kolom(label):
    rec = dict(zip(sl.DAILY_SCANS_SHEET_HEADERS, _fake_row()))
    assert label in rec, f"{label!r} tidak akan ketemu oleh record[header[c]]"
    assert str(rec[label]).strip() != "", f"{label!r} tertulis kosong"


def test_tidak_ada_kolom_baru_yang_ditambah_ensureheader():
    """Kolom yang dikirim tapi tidak ada di header live = bahan duplikat permanen."""
    unknown = [c for c in sl.DAILY_SCANS_SHEET_HEADERS if c not in LIVE_DAILY_SCANS_HEADER]
    assert set(unknown) == set(sl.MG_COLUMNS), "hanya mg_* yang boleh baru"


def test_payload_webhook_memakai_label_sheet_bukan_nama_internal(monkeypatch):
    seen = {}

    def _capture(payload, url):
        seen["payload"] = payload
        return True

    monkeypatch.setattr(sl, "_post_webhook", _capture)
    assert sl.log_scan_via_webhook([_fake_row()], "https://example.com/exec") is True
    p = seen["payload"]
    assert p["header"] == sl.DAILY_SCANS_SHEET_HEADERS
    for label in p["header"]:
        assert label in p["records"][0]
    assert "timestamp_utc" not in p["header"], "nama internal tidak boleh jadi header sheet"
    assert p["rows"][0] == _fake_row(), "jalur kompatibilitas v1 tetap utuh"


class _Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    @property
    def text(self):
        return json.dumps(self._body)

    def json(self):
        return self._body

    headers = {"content-type": "application/json"}


def test_baca_riset_memakai_key_label_sheet_v2(monkeypatch):
    ts = sl.datetime.now(sl.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = dict(zip(sl.DAILY_SCANS_SHEET_HEADERS, _fake_row()))
    rec["Timestamp (UTC)"] = ts
    rec["Vonis"] = "GO"
    body = {"status": "success", "data": {"BTC": rec}}
    monkeypatch.setattr(sl.httpx, "get", lambda *a, **k: _Resp(body))
    out = sl.fetch_latest_research_from_sheet(webhook_url="https://example.com/exec",
                                              max_age_hours=6.0)
    assert sl._research_read_source == "webhook", "jalur webhook harus terpakai, bukan gviz"
    res = out.get("BTC")
    assert res is not None and res.final_score == pytest.approx(0.42)
    assert res.news_sentiment == "bearish"
    assert res.p1_to_p7_scores["P1_sentiment_polarity"] == pytest.approx(0.10)


def test_normalisasi_tetap_kompatibel_dengan_key_internal_lama():
    old = {"timestamp": "2026-11-22T00:00:00Z", "swarm_score": "+0.30", "sentiment": "neutral"}
    assert sl._normalize_research_item(old) == old
    assert sl._normalize_research_item("bukan dict") == {}
