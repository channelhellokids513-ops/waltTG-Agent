"""Ronde 8 (utang terakhir): deployment Apps Script terbukti, ISI kolom mg_* belum.

Probe versi saja tidak bisa membedakan "kolom ada tapi kosong" dari "kolom terisi", padahal
prasyarat P8 (AUDIT 3p.4) adalah yang kedua. Test ini mengunci penghitung fill-rate dan,
yang lebih penting, mengikat nama kolom probe ke daftar nama di sheet_logger supaya perubahan
nama di satu sisi tidak membuat pengukuran di sisi lain diam-diam selalu nol.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import sheet_logger  # noqa: E402
from tools import webhook_probe as probe  # noqa: E402


def _row(**kv):
    base = {c: "" for c in probe.MG_COLUMNS}
    base.update(kv)
    return base


def test_nama_kolom_probe_masih_sama_dengan_jalur_tulis():
    # Kalau seseorang mengganti/menghapus nama di sheet_logger.MG_COLUMNS, probe harus merah,
    # bukan melaporkan nol selamanya.
    for col in probe.MG_COLUMNS:
        assert col in sheet_logger.MG_COLUMNS, f"{col} bukan kolom Market Gravity yang ditulis"
    assert all(col in sheet_logger.DAILY_SCANS_HEADERS for col in probe.MG_COLUMNS)


def test_kolom_terisi_dihitung_per_aset():
    payload = {"data": {"BTC": _row(mg_status="ok", mg_net_gravity="0.42"),
                        "ETH": _row(mg_status="ok")}}
    n, filled = probe.mg_fill_report(payload)
    assert n == 2
    assert filled["mg_status"] == 2
    assert filled["mg_net_gravity"] == 1
    assert filled["mg_regime"] == 0


def test_penanda_kosong_bursa_data_tidak_dihitung_terisi():
    payload = {"data": {"X": _row(mg_status="-", mg_net_gravity="nan", mg_regime=None,
                                  mg_trend_bias="   ", mg_bull_pressure="null")}}
    n, filled = probe.mg_fill_report(payload)
    assert n == 1
    assert all(filled[c] == 0 for c in probe.MG_COLUMNS), "nilai kosong jangan jadi bukti"


def test_tanpa_field_data_dilaporkan_terbuka_bukan_nol():
    # "belum terukur" harus tetap bisa dibedakan dari "terukur nol".
    assert probe.mg_fill_report({"status": "empty"}) == (0, dict.fromkeys(probe.MG_COLUMNS, 0))
    assert probe.mg_fill_report(None)[0] == 0


def test_record_bukan_dict_diabaikan():
    assert probe.mg_fill_report({"data": {"A": "teks"}})[1]["mg_status"] == 0
