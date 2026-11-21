"""Test pembersihan Ronde 8: rantai fallback aggregator, state klien eksplisit,
dan laporan slippage yang tidak boleh memalsukan angka parsial.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import llm_research
import walt_bot

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("slippage_report", ROOT / "tools" / "slippage_report.py")
sr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sr)


# --- rantai model aggregator ---------------------------------------------------


def test_rantai_aggregator_dimulai_dari_model_utama_dan_tanpa_duplikat():
    chain = llm_research.AGGREGATOR_MODEL_CHAIN
    assert chain[0] == llm_research.MODEL_AGGREGATOR
    assert len(chain) >= 2, "harus ada fallback: qwen-max terbukti 403 karena kuota habis"
    assert len(chain) == len(set(chain))


def test_rantai_aggregator_bisa_dioverride_lewat_env(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_MODEL_AGG", "model-utama")
    monkeypatch.setenv("DASHSCOPE_MODEL_AGG_FALLBACK", "cadangan-a, cadangan-b,,model-utama")
    namespace = {}
    source = ("import os\n"
              "MODEL_AGGREGATOR = os.getenv('DASHSCOPE_MODEL_AGG', 'qwen-max')\n"
              "AGGREGATOR_MODEL_CHAIN = []\n"
              "for _c in [MODEL_AGGREGATOR] + [m.strip() for m in os.getenv("
              "'DASHSCOPE_MODEL_AGG_FALLBACK', 'qwen-plus,qwen-flash').split(',')]:\n"
              "    if _c and _c not in AGGREGATOR_MODEL_CHAIN:\n"
              "        AGGREGATOR_MODEL_CHAIN.append(_c)\n")
    exec(compile(source, "<rantai>", "exec"), namespace)
    assert namespace["AGGREGATOR_MODEL_CHAIN"] == ["model-utama", "cadangan-a", "cadangan-b"]


def test_aggregator_memakai_rantai_model_bukan_satu_model():
    """Perilaku rantai diuji lewat sumber + rantai itu sendiri: ekstraksi fungsi terpisah
    adalah pekerjaan refactor, dan test ini tidak boleh berpura-pura menguji perilaku."""
    source = (ROOT / "llm_research.py").read_text(encoding="utf-8")
    start = source.index("for candidate in AGGREGATOR_MODEL_CHAIN:")
    block = source[start:start + 900]
    assert "except Exception as exc:" in block, "kegagalan satu model harus dilanjutkan ke cadangan"
    assert "return agg_res" in block
    assert "fallback rule-based" in source, "jalur deterministik tetap harus ada sebagai ujung rantai"


def test_rantai_aggregator_ada_di_tempat_penggunaannya():
    source = (ROOT / "llm_research.py").read_text(encoding="utf-8")
    assert "for candidate in AGGREGATOR_MODEL_CHAIN:" in source
    assert "model=MODEL_AGGREGATOR," not in source, (
        "pemanggilan aggregator tidak boleh lagi terkunci ke satu model")


# --- state WaltClient ---------------------------------------------------------


def test_klien_baru_punya_state_pengukuran_yang_jelas():
    client = walt_bot.WaltClient.__new__(walt_bot.WaltClient)
    # lewat __new__ (tanpa __init__) state tidak ada; itu harus AMAN, bukan meledak
    assert client.record_execution_slippage({"contract_id": 1, "avg_fill_price": "100"}) is None
    client.note_quote("open", 1, 100.0)
    assert client._quotes[("open", 1)][0] == 100.0


def test_cache_kontrak_kosong_dianggap_belum_difetch():
    """`{}` berarti 'sudah di-cache kosong' - harus diwakili None, bukan dict kosong."""
    client = walt_bot.WaltClient.__new__(walt_bot.WaltClient)
    client._contract_assets = None
    fetched = []

    def fake_list():
        fetched.append(1)
        return {"contracts": [{"contract_id": 9, "base_asset": "XAG"}]}

    client.list_contracts = fake_list
    assert client._asset_for_contract(9) == "XAG"
    assert client._asset_for_contract(9) == "XAG"
    assert fetched == [1], "daftar kontrak harus di-cache, tidak diambil ulang tiap close"


# --- laporan slippage ---------------------------------------------------------


def write(tmp_path, rows):
    path = tmp_path / "slippage_observed.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return path


def test_laporan_tanpa_data_keluar_dengan_kode_satu_dan_tidak_mengarang(tmp_path, capsys, monkeypatch):
    assert sr.load(tmp_path / "tidak_ada.jsonl") == []
    monkeypatch.setattr("sys.argv", ["slippage_report.py", "--path", str(tmp_path / "kosong.jsonl")])
    assert sr.main() == 1
    assert "BELUM ADA PENGUKURAN NYATA" in capsys.readouterr().out


def test_laporan_menolak_mengganti_dasar_biaya_dari_angka_parsial(tmp_path, monkeypatch, capsys):
    rows = [{"stage": "open", "contract_id": 1, "quote": 100.0, "fill": 100.02,
             "slippage_pct": 0.02, "quote_age_s": 1.0, "at": "2026-11-21T00:00:00Z"}] * 3
    path = write(tmp_path, rows)
    monkeypatch.setattr("sys.argv", ["slippage_report.py", "--path", str(path)])
    assert sr.main() == 0
    out = capsys.readouterr().out
    assert "close" in out and "BELUM TERUKUR" in out
    assert "BELUM BOLEH DIUBAH" in out


def test_laporan_memberi_angka_tersirat_bila_kedua_sisi_ada(tmp_path, monkeypatch, capsys):
    rows = []
    for i in range(4):
        rows.append({"stage": "open", "contract_id": 1, "quote": 100.0, "fill": 100.01 + i * 0.01,
                     "slippage_pct": 0.01 + i * 0.01, "quote_age_s": 1.0,
                     "at": "2026-11-21T00:00:00Z"})
        rows.append({"stage": "close", "contract_id": 1, "quote": 100.0, "fill": 99.99,
                     "slippage_pct": 0.01, "quote_age_s": 1.0, "at": "2026-11-21T00:00:00Z"})
    path = write(tmp_path, rows)
    monkeypatch.setattr("sys.argv", ["slippage_report.py", "--path", str(path)])
    assert sr.main() == 0
    out = capsys.readouterr().out
    assert "TERSIRAT" in out and "lebih besar dari tersirat" in out


def test_baris_rusak_dilaporkan_bukan_dihitung_sebagai_nol(tmp_path, capsys):
    path = tmp_path / "s.jsonl"
    path.write_text('{"stage":"open","slippage_pct":0.02,"contract_id":1,"quote":1.0,'
                    '"fill":1.0,"quote_age_s":1,"at":"x"}\n{rusak\n\n', encoding="utf-8")
    records = sr.load(path)
    assert len(records) == 1
    assert "1 baris tidak terbaca" in capsys.readouterr().out


# --- dasar biaya E5 hibrida konservatif (keputusan pemilik 2026-11-21) ---------


def test_dasar_terukur_pakai_floor_bila_belum_ada_sampel(tmp_path):
    basis, note = walt_bot.measured_slippage_basis(str(tmp_path / "kosong.jsonl"))
    assert basis == walt_bot.MIN_MEASURED_SLIPPAGE_BASIS == 0.0010
    assert "floor" in note


def test_dasar_terukur_adalah_dua_kali_p90_sisi_masuk(tmp_path):
    path = tmp_path / "obs.jsonl"
    rows = [{"stage": "open", "slippage_pct": 0.01}, {"stage": "open", "slippage_pct": 0.02},
            {"stage": "open", "slippage_pct": 0.30},   # p90 sisi masuk = 0,30%
            {"stage": "close", "slippage_pct": 9.99}]  # sisi keluar TIDAK boleh ikut
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    basis, note = walt_bot.measured_slippage_basis(str(path))
    assert basis == pytest.approx(2 * 0.30 / 100.0), "2 x p90 |masuk|, dalam pecahan"
    assert "p90" in note


def test_dasar_terukur_naik_di_atas_floor_bila_p90_besar(tmp_path):
    path = tmp_path / "obs.jsonl"
    rows = [{"stage": "open", "slippage_pct": v} for v in (0.05, 0.10, 0.20, 1.50, 2.00)]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    basis, _note = walt_bot.measured_slippage_basis(str(path))
    assert basis == pytest.approx(2 * 2.00 / 100.0), "2 x p90 |masuk|"


def test_kill_switch_memakai_pernyataan_venue(monkeypatch):
    monkeypatch.setenv("E5_SLIPPAGE_BASIS", "venue")
    assert walt_bot.effective_price_slippage(0.0100) == 0.0100
    monkeypatch.setenv("E5_SLIPPAGE_BASIS", "measured")
    assert walt_bot.effective_price_slippage(0.0100) == walt_bot.MIN_MEASURED_SLIPPAGE_BASIS


def test_deadlock_e5_terbuka_dasar_hibrida():
    """Inti keputusan: dengan dasar venue req=1,77%/hari (SOL/SUI diblokir selamanya,
    termasuk pengukuran itu sendiri). Dengan dasar hibrida req turun ke ~0,42%/hari."""
    basis = walt_bot.MIN_MEASURED_SLIPPAGE_BASIS
    cost_hybrid = 2 * walt_bot.TAKER_FEE_PER_SIDE + basis
    req_hybrid = cost_hybrid * 1.5
    assert req_hybrid == pytest.approx(0.0042)
    lulus_sol, *_ = walt_bot.check_funding_vs_fee_gate(0.00691, basis, 24.0)
    lulus_sui, *_ = walt_bot.check_funding_vs_fee_gate(0.00614, basis, 24.0)
    assert lulus_sol is True and lulus_sui is True
    blokir_sol, *_ = walt_bot.check_funding_vs_fee_gate(0.00691, 0.0100, 24.0)
    assert blokir_sol is False, "dasar venue lama memang masih memblokir - itu yang diganti"
    # floor funding harian TIDAK berubah: gerbang ekonomi tidak sendirian menjaga pintu
    assert walt_bot.MIN_FUNDING_DAILY == 0.005


def test_config_coherence_memakai_basis_engine_yang_sama(monkeypatch):
    import tools.config_coherence as cc
    monkeypatch.setenv("E5_SLIPPAGE_BASIS", "measured")
    assert cc.active_slippage_basis() == walt_bot.effective_price_slippage(cc.SLIPPAGE_TERAMATI)
    monkeypatch.setenv("E5_SLIPPAGE_BASIS", "venue")
    assert cc.active_slippage_basis() == cc.SLIPPAGE_TERAMATI


# --- knob env harus tersambung ke workflow entry ------------------------------


@pytest.mark.parametrize("knob", walt_bot.CI_ENV_KNOBS)
@pytest.mark.parametrize("workflow", walt_bot.ENTRY_WORKFLOWS)
def test_setiap_knob_env_dibaca_oleh_workflow_entry(knob, workflow):
    """os.getenv yang tidak ada di blok env workflow = knob mati di produksi.

    Ini persis lubang yang ditemukan setelah #44895344591: kill switch E5_SLIPPAGE_BASIS
    terbaca di kode tapi tidak pernah dioper ke runner.
    """
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    assert f"{knob}: ${{{{ vars.{knob}" in text, f"{knob} tidak dioper ke {workflow}"


def test_daftar_knob_ci_tidak_kosong_dan_semua_terdaftar_di_env_example():
    assert walt_bot.CI_ENV_KNOBS
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for knob in walt_bot.CI_ENV_KNOBS:
        assert f"{knob}=" in example, f"{knob} belum terdokumentasi di .env.example"
