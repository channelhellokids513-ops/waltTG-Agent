"""(a) Kadens vs jendela kesegaran: angka produksi dan default kode harus SALING mungkin.

Latar: `max_research_age_hours=6.0` (variable) dengan kadens daily-scan 12 jam membuat riset
 hasil scan sendiri basi sebelum siklus berikutnya — terbukti di CI sebagai `segar=0, basi=39`.
Masing-masing angka sah di rentangnya; yang salah adalah PASANGANNYA. Rentang tidak menangkap
ini, koherensi menangkapnya.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import sheet_logger
import strategy_config
from strategy_config import DEFAULT_RESEARCH_AGE_HOURS, DEFAULT_STRATEGY_CONFIG

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "config_coherence", REPO_ROOT / "tools" / "config_coherence.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cc = _load_tool()
BASE = dict(DEFAULT_STRATEGY_CONFIG)


def test_default_kode_koheren():
    assert cc.check(BASE) == [], "default kode harus lulus pemeriksaannya sendiri"


def test_jendela_riset_mengikuti_kadens_scan():
    assert DEFAULT_RESEARCH_AGE_HOURS >= strategy_config.DAILY_SCAN_CADENCE_HOURS
    assert DEFAULT_STRATEGY_CONFIG["max_research_age_hours"] == DEFAULT_RESEARCH_AGE_HOURS


def test_default_masih_dalam_rentang_spesifikasinya_sendiri():
    """Jendela 13j akan DITOLAK senyap kalau batas atas spec lebih kecil — ini pengaman koplingnya."""
    for kunci, nilai in DEFAULT_STRATEGY_CONFIG.items():
        spec = strategy_config.STRATEGY_PARAM_SPECS.get(kunci)
        if not spec or isinstance(nilai, bool) or not isinstance(nilai, (int, float)):
            continue
        assert spec["min"] <= nilai <= spec["max"], f"{kunci}={nilai} keluar rentang {spec['min']}-{spec['max']}"
    assert strategy_config.STRATEGY_PARAM_SPECS["max_research_age_hours"]["max"] >= DEFAULT_RESEARCH_AGE_HOURS


def test_taker_fee_tool_sama_dengan_mesin():
    """Alat koherensi menyalin konstanta biaya; kalau walt_bot berubah, alatnya wajib ikut."""
    import walt_bot
    assert cc.TAKER_FEE_PER_SIDE == walt_bot.TAKER_FEE_PER_SIDE


@pytest.mark.parametrize(
    "ubah, frasa_kunci",
    [
        ({"max_research_age_hours": 6.0}, "kadens daily-scan"),
        ({"min_funding_daily": 0.001}, "ekspektasi NEGATIF"),
        ({"tp_calm_pct": 0.0010}, "TP bersih negatif"),
        ({"daily_loss_limit_pct": 0.005}, "circuit breaker"),
        ({"expected_hold_hours": 0.0}, "wajib > 0"),
    ],
)
def test_pemeriksa_merah_untuk_pasangan_angga_yang_mustahil(ubah, frasa_kunci):
    galat = cc.check({**BASE, **ubah})
    assert galat, f"konfigurasi {ubah} seharusnya dilaporkan"
    assert any(frasa_kunci in g for g in galat), f"{galat} tidak menyebut '{frasa_kunci}'"


def test_ubah_kadens_memaksa_jendela_diperiksa_ulang(monkeypatch):
    """Kalau scan dipercepat, jendela lama tidak boleh lolos sebagai 'koheren' secara palsu."""
    monkeypatch.setattr(cc, "DAILY_SCAN_CADENCE_HOURS", 24.0)
    galat = cc.check(BASE)  # jendela 13j < kadens 24j
    assert any("kadens daily-scan" in g for g in galat)


def test_main_keluar_nol_dan_satu(monkeypatch, capsys):
    monkeypatch.delenv("STRATEGY_CONFIG_JSON", raising=False)
    assert cc.main() == 0
    assert "KOHEREN" in capsys.readouterr().out

    monkeypatch.setenv("STRATEGY_CONFIG_JSON", json.dumps({**BASE, "max_research_age_hours": 2.0}))
    assert cc.main() == 1
    assert "TIDAK KOHEREN" in capsys.readouterr().out

    monkeypatch.setenv("STRATEGY_CONFIG_JSON", "{rusak tanpa kutip}")
    assert cc.main() == 1
    assert "tidak bisa diurai" in capsys.readouterr().out


# ------------------------------------------------------------------------------
# Bukti PERILAKU: jendela 13 jam benar-benar dipakai saat membaca riset
# ------------------------------------------------------------------------------
H = sheet_logger.DAILY_SCANS_HEADERS


def _baris(asset, umur_jam):
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(hours=umur_jam)).isoformat()
    d = dict.fromkeys(H, "")
    d.update({"timestamp_utc": ts, "balance_usdc": "15.26", "asset": asset, "swarm_score": "0.3",
              "sentiment": "neutral", "consensus": "MIXED"})
    return [str(d[h]) for h in H]


def _stub_rows(monkeypatch, rows):
    monkeypatch.setattr(sheet_logger, "fetch_sheet_csv_rows_ex", lambda name, **kw: (rows, "ok"))
    monkeypatch.setattr(sheet_logger, "get_spreadsheet_id", lambda sid=None: "id-uji")


def test_riset_umur_12jam_dipakai_dan_14jam_dibuang(monkeypatch):
    """Sebelum (a): 12j sudah dibuang (jendela 6j). Sekarang 12j terpakai, 14j tetap basi."""
    monkeypatch.delenv("STRATEGY_CONFIG_JSON", raising=False)
    _stub_rows(monkeypatch, [H, _baris("NVDA", 12.0), _baris("AAPL", 14.0)])
    res = sheet_logger.fetch_latest_research_from_sheet(
        webhook_url="", csv_path=str(REPO_ROOT / "logs" / "_tidak_ada_r7cfg.csv")
    )
    assert list(res) == ["NVDA"], f"perilaku jendela salah: {sheet_logger.get_gviz_scan_stats()}"
    assert sheet_logger.get_research_read_source() == "gviz"


def test_jendela_eksplisit_tetap_menang_atas_default(monkeypatch):
    """Pemanggil yang menyebut max_age_hours tidak boleh diam-diam dapat perilaku 13 jam."""
    _stub_rows(monkeypatch, [H, _baris("NVDA", 2.0), _baris("AAPL", 5.0)])
    luas, _ = sheet_logger.fetch_latest_research_from_gviz(max_age_hours=12.0)
    assert sorted(luas) == ["AAPL", "NVDA"]
    sempit, _ = sheet_logger.fetch_latest_research_from_gviz(max_age_hours=3.0)
    assert sorted(sempit) == ["NVDA"]
